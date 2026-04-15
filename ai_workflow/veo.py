"""
VEO Video Generation Node for Nuke.
Creates a node in Node Graph for AI video generation using Google Veo 3.1 API.

Features:
- Three modes: Text (no inputs), Frames (2 inputs), Ingredients (3 inputs)
- Model selection: Google VEO 3.1 / Google VEO 3.1-Fast
- Resolution: 720P / 1080P
- Duration: 4 / 6 / 8 seconds
- Prompt history (saves last 10)
- Negative prompt support
- Async video generation with polling
- Video record (prompt history) nodes for regeneration

Mode input mapping:
  Text: No inputs (pure text-to-video)
  Frames: 2 inputs (A1=first frame, A2=last frame)
  Ingredients: 3 inputs (A1-A3 reference images)
"""

try:
    from PySide6 import QtWidgets, QtCore, QtGui
    from shiboken6 import isValid as _isValid
except ImportError:
    try:
        from PySide2 import QtWidgets, QtCore, QtGui
        from shiboken2 import isValid as _isValid
    except ImportError:
        from PySide import QtGui as QtWidgets
        from PySide import QtCore, QtGui
        def _isValid(obj):
            return True

import nuke
import nukescripts
import os
import json
import tempfile
import time
import datetime
import re

# Google GenAI SDK
from google import genai
from google.genai import types

# Import shared settings from nanobanana
from ai_workflow.nanobanana import (
    NanoBananaSettings,
    NANOBANANA_STYLE,
    get_temp_directory,
    get_input_directory,
    get_output_directory,
    render_input_to_file_silent,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
VEO_MODELS = {
    "Google VEO 3.1": "veo-3.1-generate-preview",
    "Google VEO 3.1-Fast": "veo-3.1-fast-generate-preview",
}
VEO_MODEL_DEFAULT = "Google VEO 3.1-Fast"

# Mode constants
VEO_MODE_TEXT = "Text"
VEO_MODE_FIRST_FRAME = "FirstFrame"
VEO_MODE_FRAMES = "Frames"
VEO_MODE_INGREDIENTS = "Ingredients"

# Input counts per mode
VEO_MODE_INPUT_COUNTS = {
    VEO_MODE_TEXT: 0,
    VEO_MODE_FIRST_FRAME: 1,
    VEO_MODE_FRAMES: 2,
    VEO_MODE_INGREDIENTS: 3,
}

# Max inputs needed (for node creation)
VEO_MAX_INPUTS = 3

# ---------------------------------------------------------------------------
# Module-level registry for active workers.
# Prevents garbage collection when the Widget is destroyed mid-generation.
# ---------------------------------------------------------------------------
_veo_active_workers = {}


class DropDownComboBox(QtWidgets.QComboBox):
    """QComboBox that always shows popup below the widget (not covering it)."""

    def showPopup(self):
        super(DropDownComboBox, self).showPopup()
        popup = self.view().window()
        # Move popup to just below the combo box
        pos = self.mapToGlobal(QtCore.QPoint(0, self.height()))
        popup.move(pos)



def _find_ffmpeg():
    """Try to locate ffmpeg executable.
    
    Search order:
    1. Same directory as this script (ai_workflow/ffmpeg.exe)
    2. Nuke's bundled ffmpeg (if exists)
    3. System PATH
    4. Common install locations on Windows
    Returns the path string or None.
    """
    import shutil

    # 1. Check same directory as this script (ai_workflow/)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    local_ffmpeg = os.path.join(script_dir, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if os.path.isfile(local_ffmpeg):
        return local_ffmpeg

    # 2. Check Nuke's own directory
    try:
        nuke_dir = os.path.dirname(nuke.EXE_PATH)
        nuke_ffmpeg = os.path.join(nuke_dir, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if os.path.isfile(nuke_ffmpeg):
            return nuke_ffmpeg
    except Exception:
        pass

    # 3. System PATH
    found = shutil.which("ffmpeg")
    if found:
        return found

    # 4. Common Windows locations
    if os.name == "nt":
        for candidate in [
            r"C:\ffmpeg\bin\ffmpeg.exe",
            r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
            r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
            os.path.expanduser(r"~\ffmpeg\bin\ffmpeg.exe"),
        ]:
            if os.path.isfile(candidate):
                return candidate

    return None


def _convert_mp4_to_prores(mp4_path, status_callback=None, codec=None):
    """Convert an MP4 file to ProRes MOV using ffmpeg.
    
    Args:
        mp4_path: Path to the source .mp4 file.
        status_callback: Optional callable(str) for status messages.
        codec: Optional str — ProRes profile name (default from NanoBananaSettings).
               Supported: "ProRes 422 HQ", "ProRes 422", "ProRes 422 LT",
               "ProRes 422 Proxy"
    
    Returns:
        mov_path (str) on success, or mp4_path unchanged if ffmpeg is unavailable.
    """
    import subprocess

    # Map user-friendly names to ffmpeg -profile:v values + pixel format
    _PROFILE_MAP = {
        "ProRes 422 HQ":   ("3", "yuva444p10le"),   # Profile 3 = HQ, ~184 Mbps
        "ProRes 422":      ("2", "yuva444p10le"),   # Profile 2 = Standard, ~122 Mbps
        "ProRes 422 LT":   ("1", "yuv422p10le"),    # Profile 1 = LT, ~85 Mbps
        "ProRes 422 Proxy":("0", "yuv422p10le"),    # Profile 0 = Proxy, ~38 Mbps
    }

    # Resolve codec: explicit arg → Settings → fallback to HQ
    if not codec:
        try:
            from ai_workflow.nanobanana import NanoBananaSettings
            codec = NanoBananaSettings().prores_codec
        except Exception:
            codec = "ProRes 422 HQ"

    profile_val, pix_fmt = _PROFILE_MAP.get(codec, _PROFILE_MAP["ProRes 422 HQ"])

    print("[NB Transcode] Converting '{}' to ProRes MOV | codec={} | profile={}".format(
        mp4_path, codec, profile_val))

    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        print("VEO: ffmpeg not found - keeping original MP4. "
              "Install ffmpeg and add it to PATH for ProRes conversion.")
        if status_callback:
            status_callback("Warning: ffmpeg not found, using MP4")
        return mp4_path

    mov_path = os.path.splitext(mp4_path)[0] + ".mov"

    cmd = [
        ffmpeg,
        "-y",                     # overwrite without asking
        "-i", mp4_path,           # input
        "-c:v", "prores_ks",      # ProRes encoder
        "-profile:v", profile_val,# Dynamic profile from settings
        "-pix_fmt", pix_fmt,      # Pixel format (HQ/Std=10bit+alpha, LT/Proxy=10bit)
        "-c:a", "pcm_s16le",      # audio codec (lossless PCM)
        mov_path,
    ]

    if status_callback:
        status_callback("Converting to ProRes MOV...")

    try:
        print("VEO: Running ffmpeg: {}".format(" ".join(cmd)))
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,           # 5 minute timeout
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode == 0 and os.path.exists(mov_path):
            print("VEO: ProRes conversion OK → {}".format(mov_path))
            print("VEO: Original MP4 kept at: {}".format(mp4_path))
            return mov_path
        else:
            print("VEO: ffmpeg failed (code {}): {}".format(
                result.returncode, result.stderr[:500]))
            if status_callback:
                status_callback("Warning: ProRes conversion failed, using MP4")
            return mp4_path
    except subprocess.TimeoutExpired:
        print("VEO: ffmpeg timed out after 300s")
        if status_callback:
            status_callback("Warning: ffmpeg timed out, using MP4")
        return mp4_path
    except Exception as e:
        print("VEO: ffmpeg error: {}".format(e))
        if status_callback:
            status_callback("Warning: ffmpeg error, using MP4")
        return mp4_path


def _create_genai_client(api_key):
    """Create a google-genai Client with the given API key."""
    return genai.Client(api_key=api_key)


def _load_image_for_sdk(image_path):
    """Load an image file and return a types.Image with base64-encoded bytes."""
    import base64
    ext = os.path.splitext(image_path)[1].lower()
    mime_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}
    mime_type = mime_map.get(ext, "image/png")
    with open(image_path, "rb") as f:
        raw_bytes = f.read()
    return types.Image(image_bytes=raw_bytes, mime_type=mime_type)






# ---------------------------------------------------------------------------
# VEO Generation Worker Thread (uses google-genai SDK)
# ---------------------------------------------------------------------------








class VeoWorker(QtCore.QThread):
    finished = QtCore.Signal(str, dict)   # video_path, metadata
    error = QtCore.Signal(str)
    status_update = QtCore.Signal(str)
    progress_update = QtCore.Signal(int)  # percentage 0-100

    def __init__(self, api_key, prompt,
                 reference_image_paths=None,
                 model="Google VEO 3.1-Fast",
                 aspect_ratio="16:9", duration="8",
                 resolution="720P",
                 mode="Text",
                 negative_prompt="",
                 temp_dir=None, gen_name="VEO_Generate"):
        super(VeoWorker, self).__init__()
        self.api_key = api_key
        self.prompt = prompt
        self.negative_prompt = negative_prompt
        self.reference_image_paths = reference_image_paths or []
        self.model = VEO_MODELS.get(model, VEO_MODELS[VEO_MODEL_DEFAULT])
        self.aspect_ratio = aspect_ratio
        self.duration = duration
        self.resolution = resolution
        self.mode = mode
        self.temp_dir = temp_dir or get_output_directory()
        self.gen_name = gen_name
        self.is_running = True

    def stop(self):
        self.is_running = False

    def run(self):
        try:
            client = _create_genai_client(self.api_key)

            # 1. Load reference images (up to 3)
            self.status_update.emit("Preparing inputs...")
            ref_images = []
            for rp in self.reference_image_paths:
                if rp and os.path.exists(rp):
                    ref_images.append(_load_image_for_sdk(rp))

            if not self.is_running:
                return

            # 2. Build generation config
            config_kwargs = {}
            if self.aspect_ratio:
                config_kwargs["aspect_ratio"] = self.aspect_ratio

            # Parse duration: "4" -> 4, "6" -> 6, "8" -> 8, "4s" -> 4
            dur_seconds = 8  # default
            if self.duration:
                dur_str = str(self.duration).replace("s", "").strip()
                try:
                    dur_val = int(dur_str)
                    if 4 <= dur_val <= 8:
                        dur_seconds = dur_val
                except (ValueError, TypeError):
                    pass

            # Build config and generate_kwargs based on mode
            has_refs = len(ref_images) > 0

            # Force 8s per Google Veo 3.1 API constraints:
            #   - Frames (first+last frame) mode: ALWAYS 8s (any resolution)
            #   - 1080p / 4k resolution: ALWAYS 8s (any mode)
            #   - Text / FirstFrame / Ingredients @ 720p: user can choose 4/6/8s
            if self.mode == "Frames":
                dur_seconds = 8
            if self.resolution and self.resolution.lower() in ("1080p", "4k"):
                dur_seconds = 8

            config_kwargs["duration_seconds"] = dur_seconds

            # Set resolution (API accepts "720p", "1080p", "4k")
            if self.resolution:
                config_kwargs["resolution"] = self.resolution.lower()

            generate_kwargs = {
                "model": self.model,
                "prompt": self.prompt,
            }

            if self.mode == "Frames" and len(ref_images) >= 2:
                # Frames mode: first frame as image param, last frame in config
                first_image = ref_images[0]
                last_image = ref_images[1]
                config_kwargs["last_frame"] = last_image
                generate_kwargs["image"] = first_image
                mode_str = "frames (first+last)"
            elif self.mode == "Frames" and len(ref_images) == 1:
                # Only first frame provided (fallback)
                generate_kwargs["image"] = ref_images[0]
                mode_str = "frames (first only)"
            elif self.mode == "FirstFrame" and len(ref_images) >= 1:
                # FirstFrame mode: only first frame as image param
                generate_kwargs["image"] = ref_images[0]
                mode_str = "first-frame"
            elif has_refs:
                # Ingredients mode: reference images as assets
                config_kwargs["reference_images"] = [
                    types.VideoGenerationReferenceImage(
                        image=ri, reference_type="asset"
                    ) for ri in ref_images
                ]
                mode_str = "ref-to-video ({} refs)".format(len(ref_images))
            else:
                mode_str = "text-to-video"

            config = types.GenerateVideosConfig(**config_kwargs)
            generate_kwargs["config"] = config

            # 3. Call generate_videos
            self.status_update.emit("Starting video generation ({})...".format(mode_str))
            print("VEO SDK: mode={}, model={}, prompt={}, config keys={}, ref_count={}, resolution={}, duration={}s (raw={})".format(
                mode_str, self.model, self.prompt[:80], list(config_kwargs.keys()), len(ref_images), self.resolution, dur_seconds, self.duration))

            # Save the full request payload as JSON for debugging
            try:
                request_log = {
                    "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "model": self.model,
                    "prompt": self.prompt,
                    "negative_prompt": self.negative_prompt,
                    "mode": self.mode,
                    "resolution": self.resolution,
                    "config": {
                        k: v for k, v in config_kwargs.items()
                        if k not in ("reference_images", "last_frame")
                    },
                    "reference_image_count": len(ref_images),
                    "reference_image_paths": self.reference_image_paths,
                }
                # Add reference_images info (paths only, not binary data)
                if has_refs:
                    request_log["config"]["has_reference_images"] = True
                    if self.mode == "Frames":
                        request_log["config"]["frames_mode"] = True
                        request_log["config"]["has_first_frame"] = True
                        request_log["config"]["has_last_frame"] = len(ref_images) >= 2
                    elif self.mode == "FirstFrame":
                        request_log["config"]["first_frame_mode"] = True

                log_filename = "veo_request_{}.json".format(
                    datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
                log_path = os.path.join(self.temp_dir, log_filename)
                with open(log_path, "w", encoding="utf-8") as f:
                    json.dump(request_log, f, indent=2, ensure_ascii=False)
                print("VEO SDK: Request payload saved to {}".format(log_path))
                print("VEO SDK: Request payload:\n{}".format(
                    json.dumps(request_log, indent=2, ensure_ascii=False)))
            except Exception as log_err:
                print("VEO SDK: Failed to save request log: {}".format(log_err))

            operation = client.models.generate_videos(**generate_kwargs)

            if not self.is_running:
                return

            # 4. Poll for completion
            self.status_update.emit("Generating video (this may take a few minutes)...")
            poll_count = 0
            max_polls = 60  # ~10 min

            while not operation.done and self.is_running and poll_count < max_polls:
                poll_count += 1
                progress = min(int((poll_count / max_polls) * 95), 95)
                self.progress_update.emit(progress)
                self.status_update.emit("Generating video... ({}/{}s)".format(
                    poll_count * 10, max_polls * 10))
                time.sleep(10)
                operation = client.operations.get(operation)

            if not self.is_running:
                return

            if not operation.done:
                self.error.emit("Video generation timed out")
                return

            # 5. Download video
            self.status_update.emit("Downloading video...")
            self.progress_update.emit(96)

            # Extract generated videos from response
            generated_videos = None
            rai_reasons = None
            resp = getattr(operation, 'response', None) or getattr(operation, 'result', None)
            if resp is not None:
                generated_videos = getattr(resp, 'generated_videos', None)
                rai_reasons = getattr(resp, 'rai_media_filtered_reasons', None)

            if not generated_videos:
                if rai_reasons:
                    reason_str = "; ".join(rai_reasons)
                    print("VEO SDK: Filtered by safety: {}".format(reason_str))
                    self.error.emit("Video blocked by safety filter:\n{}".format(reason_str))
                else:
                    self.error.emit("No video generated in response")
                return

            video = generated_videos[0]

            # Download and save
            client.files.download(file=video.video)

            frame_num = 1
            while True:
                filename = "{}_frame{}.mp4".format(self.gen_name, frame_num)
                output_path = os.path.join(self.temp_dir, filename)
                if not os.path.exists(output_path):
                    break
                frame_num += 1

            video.video.save(output_path)
            print("VEO SDK: Video saved to {}".format(output_path))

            # Convert MP4 → ProRes MOV for Nuke compatibility
            self.status_update.emit("Converting to ProRes MOV...")
            self.progress_update.emit(98)
            final_path = _convert_mp4_to_prores(
                output_path,
                status_callback=lambda msg: self.status_update.emit(msg),
            )

            self.progress_update.emit(100)
            self.status_update.emit("Video generated!")

            metadata = {
                "prompt": self.prompt,
                "negative_prompt": self.negative_prompt,
                "aspect_ratio": self.aspect_ratio,
                "duration": self.duration,
                "resolution": self.resolution,
                "mode": self.mode,
                "model": self.model,
                "ref_image_count": len(ref_images),
            }

            self.finished.emit(final_path, metadata)

        except Exception as e:
            import traceback
            traceback.print_exc()
            self.error.emit("Error: {}".format(str(e)))


# ---------------------------------------------------------------------------
# Send to Studio helper
# ---------------------------------------------------------------------------
_SEND_TO_STUDIO_SCRIPT = """
import nuke, socket, json, struct

node = nuke.thisNode()
file_path = node["file"].value()
if not file_path:
    nuke.message("No file path set on this Read node.")
else:
    data = json.dumps({
        "action": "add_clips",
        "clips": [{
            "file": file_path,
            "name": node.name(),
        }]
    }).encode("utf-8")
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(("127.0.0.1", 54321))
        s.sendall(struct.pack(">I", len(data)))
        s.sendall(data)
        resp = s.recv(1024).decode("utf-8")
        s.close()
        nuke.message("Sent to Studio: " + resp)
    except Exception as e:
        nuke.message("Failed to send: " + str(e))
"""


def _add_send_to_studio_knob(read_node):
    """Add a 'Send to Studio' Python button knob to the Read tab of a Read node."""
    if read_node is None:
        return
    try:
        tab = nuke.Tab_Knob("User", "Nuke Studio")
        read_node.addKnob(tab)
        btn = nuke.PyScript_Knob("send_to_studio", "Send to Studio", _SEND_TO_STUDIO_SCRIPT)
        read_node.addKnob(btn)
    except Exception as e:
        print("Warning: could not add Send to Studio knob: {}".format(e))


# ---------------------------------------------------------------------------
# VEO Player Group Node (wraps Read node with exposed knobs + Send to Studio)
# ---------------------------------------------------------------------------

# Send to Studio script for VEO Player Group — reads file from internal Read node
_VEO_PLAYER_SEND_SCRIPT = """
import nuke, socket, json, struct

group = nuke.thisNode()
group.begin()
read_node = nuke.toNode("InternalRead")
group.end()

if not read_node:
    nuke.message("Internal Read node not found.")
else:
    file_path = read_node["file"].value()
    if not file_path:
        nuke.message("No file path set.")
    else:
        data = json.dumps({
            "action": "add_clips",
            "clips": [{
                "file": file_path,
                "name": group.name(),
            }]
        }).encode("utf-8")
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect(("127.0.0.1", 54321))
            s.sendall(struct.pack(">I", len(data)))
            s.sendall(data)
            resp = s.recv(1024).decode("utf-8")
            s.close()
            nuke.message("Sent to Studio: " + resp)
        except Exception as e:
            nuke.message("Failed to send: " + str(e))
"""


def create_veo_player_node(video_path=None, name=None, xpos=None, ypos=None):
    """Create a VEO Player Group node that wraps a Read node.

    The Group exposes all Read-tab knobs using REAL knobs (NOT Link_Knob)
    so they survive rename-undo (Ctrl+Z).  A knobChanged callback keeps
    them synced with the internal Read node via name-based lookup
    (nuke.toNode('InternalRead')).

    Args:
        video_path: Optional path to a video file to load.
        name: Optional node name.
        xpos, ypos: Optional position.

    Returns:
        (group_node, internal_read_node) tuple.
    """
    # Wrap entire node creation as a single undo unit so that
    # subsequent rename-undo cannot partially break internal structure
    nuke.Undo.begin("Create VEO Viewer")
    try:
        # --- Create the Group shell ---
        _default_name = _next_veo_name()
        group = nuke.nodes.Group(name=(name or _default_name))
        group["tile_color"].setValue(0x00C878FF)  # Green tint
        group["label"].setValue("VEO Player")

        if xpos is not None:
            group["xpos"].setValue(int(xpos))
        if ypos is not None:
            group["ypos"].setValue(int(ypos))

        # --- Build internals: Read → Output ---
        group.begin()
        read_node = nuke.nodes.Read(name="InternalRead")
        out_node = nuke.nodes.Output(name="Output")
        out_node.setInput(0, read_node)
        group.end()

        # Load the video file AFTER group.end() so knobs are fully populated
        if video_path and os.path.exists(video_path):
            group.begin()
            read_node["file"].fromUserText(video_path)
            group.end()

        # --- Expose Read-tab knobs on the Group panel ---
        # Use REAL knobs (NOT Link_Knob) so they survive rename-undo.
        # Link_Knob stores hardcoded TCL paths like "NodeName.InternalRead.format"
        # which break after undo-rename. Real knobs store actual values,
        # and a knobChanged callback keeps them synced via name lookup.

        # Tab: Read
        tab_read = nuke.Tab_Knob("read_tab", "Read")
        group.addKnob(tab_read)

        # Track which knobs need syncing between Group panel <-> internal Read
        _read_sync_knobs = []

        # --- file knob (special: File_Knob) ---
        file_knob = nuke.File_Knob("veo_file", "file")
        if video_path:
            file_knob.setValue(video_path.replace("\\", "/"))
        group.addKnob(file_knob)

        # --- format (dropdown, like native Read) ---
        fmt_values = []
        try:
            for _f in nuke.formats():
                _fn = _f.name()
                if _fn:
                    fmt_values.append(_fn)
        except Exception:
            pass
        if not fmt_values:
            fmt_values = ["---"]
        fmt_current = "---"
        try:
            _fv = read_node["format"].value()
            if hasattr(_fv, 'width') and _fv.width() > 0:
                fmt_current = '%dx%d' % (_fv.width(), _fv.height())
            elif hasattr(_fv, 'name') and _fv.name():
                fmt_current = _fv.name()
            else:
                fmt_current = str(_fv)
        except Exception:
            pass
        if fmt_current and fmt_current not in fmt_values:
            fmt_values.append(fmt_current)
        format_knob = nuke.Enumeration_Knob("veo_format", "format", fmt_values)
        format_knob.setValue(fmt_current)
        group.addKnob(format_knob)
        _read_sync_knobs.append(("veo_format", "format"))

        # --- frame range knobs: first, last ---
        if "first" in read_node.knobs():
            k = nuke.Int_Knob("veo_first", "first")
            k.setFlag(nuke.STARTLINE)
            k.setValue(int(read_node["first"].value()))
            group.addKnob(k)
            _read_sync_knobs.append(("veo_first", "first"))
        if "last" in read_node.knobs():
            k = nuke.Int_Knob("veo_last", "last")
            k.clearFlag(nuke.STARTLINE)
            k.setValue(int(read_node["last"].value()))
            group.addKnob(k)
            _read_sync_knobs.append(("veo_last", "last"))

        # --- frame_mode and frame ---
        if "frame_mode" in read_node.knobs():
            k = nuke.Enumeration_Knob("veo_frame_mode", "frame mode",
                                      list(read_node["frame_mode"].values()) or [""])
            k.setFlag(nuke.STARTLINE)
            k.setValue(str(read_node["frame_mode"].value()))
            group.addKnob(k)
            _read_sync_knobs.append(("veo_frame_mode", "frame_mode"))
        if "frame" in read_node.knobs():
            k = nuke.Int_Knob("veo_frame", "frame")
            k.clearFlag(nuke.STARTLINE)
            k.setValue(int(read_node["frame"].value()))
            group.addKnob(k)
            _read_sync_knobs.append(("veo_frame", "frame"))

        # --- origfirst, origlast ---
        if "origfirst" in read_node.knobs():
            k = nuke.Int_Knob("veo_origfirst", "origfirst")
            k.setFlag(nuke.STARTLINE)
            k.setValue(int(read_node["origfirst"].value()))
            group.addKnob(k)
            _read_sync_knobs.append(("veo_origfirst", "origfirst"))
        if "origlast" in read_node.knobs():
            k = nuke.Int_Knob("veo_origlast", "origlast")
            k.clearFlag(nuke.STARTLINE)
            k.setValue(int(read_node["origlast"].value()))
            group.addKnob(k)
            _read_sync_knobs.append(("veo_origlast", "origlast"))

        # --- on_error ---
        if "on_error" in read_node.knobs():
            k = nuke.Enumeration_Knob("veo_on_error", "missing frames",
                                      list(read_node["on_error"].values()) or [""])
            k.setFlag(nuke.STARTLINE)
            k.setValue(str(read_node["on_error"].value()))
            group.addKnob(k)
            _read_sync_knobs.append(("veo_on_error", "on_error"))

        # --- colorspace (dropdown from Read's own enum values) ---
        if "colorspace" in read_node.knobs():
            cs_label = read_node["colorspace"].label() or "colorspace"
            cs_values = []
            try:
                _cs_k = read_node["colorspace"]
                if hasattr(_cs_k, "values") and callable(_cs_k.values):
                    cs_values = list(_cs_k.values()) or []
                elif hasattr(_cs_k, "enumerationItems") and callable(_cs_k.enumerationItems):
                    cs_values = list(_cs_k.enumerationItems()) or []
            except Exception:
                pass
            if not cs_values:
                cs_values = ["default", "linear", "sRGB", "Gamma1.8", "Gamma2.2",
                             "Rec709", "ACEScg", "ALEXAV3LogC"]
            current_cs = str(read_node["colorspace"].value())
            if current_cs not in cs_values:
                cs_values.insert(0, current_cs)
            cs_knob = nuke.Enumeration_Knob("veo_colorspace", cs_label, cs_values)
            cs_knob.setFlag(nuke.STARTLINE)
            cs_knob.setValue(current_cs)
            group.addKnob(cs_knob)
            _read_sync_knobs.append(("veo_colorspace", "colorspace"))

        # --- premultiplied, raw, auto_alpha ---
        for rk in ["premultiplied", "raw", "auto_alpha"]:
            if rk in read_node.knobs():
                rl = read_node[rk].label() or rk
                real_k = nuke.Boolean_Knob("veo_" + rk, rl)
                real_k.setValue(int(read_node[rk].value()))
                real_k.clearFlag(nuke.STARTLINE)
                group.addKnob(real_k)
                _read_sync_knobs.append(("veo_" + rk, rk))

        # --- MOV Options section ---
        mov_knob_names = [
            "ycbcr_matrix", "mov_data_range",
            "first_track_only", "metadata", "noprefix", "match_key_format",
            "mov64_decode_codec", "mov_decode_codec",
            "video_codec_knob",
        ]
        mov_divider_added = False
        for mk in mov_knob_names:
            if mk in read_node.knobs():
                if not mov_divider_added:
                    mov_div = nuke.Text_Knob("mov_divider", "MOV Options")
                    group.addKnob(mov_div)
                    mov_divider_added = True
                ml = read_node[mk].label() or mk
                mk_val = read_node[mk].value()
                if isinstance(mk_val, int):
                    real_mov_k = nuke.Boolean_Knob("veo_" + mk, ml)
                    real_mov_k.setValue(int(mk_val))
                elif isinstance(mk_val, str) and len(mk_val) < 256:
                    real_mov_k = nuke.String_Knob("veo_" + mk, ml)
                    real_mov_k.setValue(str(mk_val))
                else:
                    continue  # skip unsupported types
                group.addKnob(real_mov_k)
                _read_sync_knobs.append(("veo_" + mk, mk))

        # If MOV loaded, show video codec as text display
        if mov_divider_added:
            try:
                codec_val = ""
                if "video_codec_knob" in read_node.knobs():
                    codec_val = read_node["video_codec_knob"].value()
                elif "mov64_decode_codec" in read_node.knobs():
                    codec_val = read_node["mov64_decode_codec"].value()
                if codec_val:
                    codec_text = nuke.Text_Knob("video_codec_display", "Video Codec", str(codec_val))
                    group.addKnob(codec_text)
            except Exception:
                pass

        # --- Button to open internal Read node's full properties ---
        open_read_script = (
            "n = nuke.thisNode()\n"
            "n.begin()\n"
            "r = nuke.toNode('InternalRead')\n"
            "n.end()\n"
            "if r:\n"
            "    nuke.show(r)\n"
        )
        open_btn = nuke.PyScript_Knob("open_read_props", "Open Full Read Properties", open_read_script)
        open_btn.setFlag(nuke.STARTLINE)
        group.addKnob(open_btn)

        # --- knobChanged callback: sync ALL exposed knobs <-> internal Read ---
        # Uses name-based lookup (nuke.toNode('InternalRead')) so it survives
        # rename-undo.  When any exposed knob changes, push its value into
        # the corresponding internal Read knob.  For veo_file (file), also
        # pull fresh colorspace/format values back after loading.
        _sync_pairs_str = repr(_read_sync_knobs).replace("'", '"')
        kc_script = (
            "import nuke\n"
            "n = nuke.thisNode()\n"
            "k = nuke.thisKnob()\n"
            "kn = k.name()\n"
            "# ===== DEBUG: knobChanged fired =====\n"
            "_dbg = '[VEO-DEBUG] knobChanged: node=%s knob=%s value=%s' % (n.name(), kn, str(k.value())[:80])\n"
            "print(_dbg)\n"
            "n.begin()\n"
            "r = nuke.toNode('InternalRead')\n"
            "n.end()\n"
            "_dbg2 = '[VEO-DEBUG] InternalRead found=%s' % (r is not None)\n"
            "print(_dbg2)\n"
            "if not r:\n"
            "    print('[VEO-DEBUG] WARNING: InternalRead NOT FOUND! All internal nodes:')\n"
            "    n.begin()\n"
            "    for _dn in nuke.allNodes():\n"
            "        print('  [VEO-DEBUG]   internal: %s (%s)' % (_dn.name(), _dn.Class()))\n"
            "    n.end()\n"
            "    pass\n"
            "# File changed: load into Read + pull fresh values\n"
            "if kn == 'veo_file' and r:\n"
            "    n.begin()\n"
            "    r['file'].fromUserText(k.value())\n"
            "    n.end()\n"
            "    # Pull fresh format from Read\n"
            "    try:\n"
            "        _fv = r['format'].value()\n"
            "        _fmt_name = ''\n"
            "        if hasattr(_fv, 'name') and _fv.name():\n"
            "            _fmt_name = _fv.name()\n"
            "        elif hasattr(_fv, 'width') and _fv.width() > 0:\n"
            "            _fmt_name = '%dx%d' % (_fv.width(), _fv.height())\n"
            "        if _fmt_name and 'veo_format' in n.knobs():\n"
            "            _cur_vals = list(n['veo_format'].values())\n"
            "            if _fmt_name not in _cur_vals:\n"
            "                _cur_vals.append(_fmt_name)\n"
            "                n['veo_format'].setValues(_cur_vals)\n"
            "            n['veo_format'].setValue(_fmt_name)\n"
            "    except Exception:\n"
            "        pass\n"
            "    # Pull fresh frame range from Read\n"
            "    for _frk, _fgk in [('first','veo_first'),('last','veo_last'),('origfirst','veo_origfirst'),('origlast','veo_origlast')]:\n"
            "        try:\n"
            "            if _frk in r.knobs() and _fgk in n.knobs():\n"
            "                n[_fgk].setValue(int(r[_frk].value()))\n"
            "        except Exception:\n"
            "            pass\n"
            "    # Pull fresh colorspace from Read\n"
            "    try:\n"
            "        _cv = str(r['colorspace'].value())\n"
            "        n['veo_colorspace'].setValue(_cv)\n"
            "    except Exception:\n"
            "        pass\n"
            "    # Force postage stamp refresh\n"
            "    if 'postage_stamp' in n.knobs():\n"
            "        n['postage_stamp'].setValue(True)\n"
            "    try:\n"
            "        n.sample('red', 0, 0)\n"
            "    except Exception:\n"
            "        pass\n"
            "# Sync Group -> Read for all other exposed knobs\n"
            "_pairs = " + _sync_pairs_str + "\n"
            "for _gk, _rk in _pairs:\n"
            "    if kn == _gk and r and _rk in r.knobs():\n"
            "        try:\n"
            "            if _rk == 'format':\n"
            "                n.begin()\n"
            "                r['format'].setValue(k.value())\n"
            "                n.end()\n"
            "            elif isinstance(r[_rk].value(), int):\n"
            "                n.begin()\n"
            "                r[_rk].setValue(int(k.value()))\n"
            "                n.end()\n"
            "            else:\n"
            "                n.begin()\n"
            "                r[_rk].setValue(k.value())\n"
            "                n.end()\n"
            "            print('[VEO-DEBUG] Synced %s -> %s = %s' % (_gk, _rk, str(k.value())[:60]))\n"
            "        except Exception as _e:\n"
            "            print('[VEO-DEBUG] SYNC ERROR %s->%s: %s' % (_gk, _rk, _e))\n"
            "print('[VEO-DEBUG] knobChanged done for %s' % kn)\n"
        )
        group["knobChanged"].setValue(kc_script)

        # --- Divider + Send to Studio button ---
        divider = nuke.Text_Knob("studio_divider", "")
        group.addKnob(divider)

        btn = nuke.PyScript_Knob("send_to_studio", "Send to Studio", _VEO_PLAYER_SEND_SCRIPT)
        btn.setFlag(nuke.STARTLINE)
        group.addKnob(btn)

        # Hidden marker knob so we can identify VEO Player nodes later
        marker = nuke.Boolean_Knob("is_veo_player", "")
        marker.setValue(True)
        marker.setFlag(nuke.INVISIBLE)
        group.addKnob(marker)

        print("VEO: Created VEO Player '{}' with internal Read".format(group.name()))
        # DEBUG: dump all knobs on the group
        print("[VEO-DEBUG] Group knobs after creation:")
        for _gk in group.knobs():
            try:
                _gv = str(group[_gk].value())[:60]
            except Exception:
                _gv = "(no value)"
            print("  [VEO-DEBUG]   %s = %s (type=%s)" % (_gk, _gv, group[_gk].Class()))
        # DEBUG: verify InternalRead is accessible by name
        group.begin()
        _dr = nuke.toNode('InternalRead')
        group.end()
        print("[VEO-DEBUG] InternalRead accessible immediately after creation: %s" % (_dr is not None))
        if _dr:
            for _rk in ['file', 'format', 'first', 'last', 'colorspace']:
                if _rk in _dr.knobs():
                    try:
                        print("  [VEO-DEBUG]   Read.%s = %s" % (_rk, str(_dr[_rk].value())[:60]))
                    except Exception:
                        pass

        return group, read_node

    finally:
        nuke.Undo.end()


def _get_internal_read(player_group):
    """Get the internal Read node from a VEO Player/Viewer Group."""
    if player_group is None:
        return None
    try:
        player_group.begin()
        read_node = nuke.toNode("InternalRead")
        player_group.end()
        return read_node
    except Exception:
        return None


def _rebuild_veo_group_for_thumbnail(node, media_path=None):
    """'Replacement Jutsu' — rebuild the VEO Viewer Group node to force thumbnail refresh.

    Nuke's Group-node postage-stamp cache is bound to the C++ node instance
    and cannot be flushed via any public Python / Tcl API.  The only reliable
    way to make the DAG show a new thumbnail is to **replace the node with
    an identical copy** (same strategy proven in NanoBanana).

    Strategy:
      1. Save all upstream / downstream connections
      2. nuke.nodeCopy  -> clipboard  (serialises the Group + its internals)
      3. Delete old node
      4. nuke.nodePaste -> new node   (fresh C++ instance -> fresh thumbnail)
      5. Restore all connections & ensure the new node keeps the same name

    If *media_path* is given the InternalRead is pointed to that file
    **before** the copy so the pasted clone already has the right media.

    Returns the **new** Group node (the old reference is now invalid).
    Returns *None* on failure (caller should fall back to legacy approach).
    """
    if not node or node.Class() != "Group":
        return None
    if "is_veo_viewer" not in node.knobs():
        return None  # safety — only operate on VEO Viewer nodes

    _tag = "[VEO Rebuild]"

    try:
        node_name = node.name()
        print("{} START for '{}'".format(_tag, node_name))

        # Wrap in Undo group so the whole operation can be reverted if needed
        nuke.Undo.begin("VEO Rebuild Thumbnail")

        # --- 0. Set media_path on InternalRead BEFORE copy ---
        if media_path and os.path.isfile(media_path):
            ir = _get_internal_read(node)
            if ir:
                node.begin()
                ir["file"].fromUserText(media_path)
                # Force reload so the Read parses the new video file header
                if "reload" in ir.knobs():
                    try:
                        ir["reload"].execute()
                    except Exception:
                        pass
                # Sync first/last to origfirst/origlast so the Read plays full range
                try:
                    _of = int(ir["origfirst"].value()) if "origfirst" in ir.knobs() else None
                    _ol = int(ir["origlast"].value()) if "origlast" in ir.knobs() else None
                    if _of is not None and _ol is not None and _ol > _of:
                        if "first" in ir.knobs():
                            ir["first"].setValue(_of)
                        if "last" in ir.knobs():
                            ir["last"].setValue(_ol)
                        print("{}   InternalRead frame range: {}-{}".format(_tag, _of, _ol))
                except Exception:
                    pass
                node.end()
                # Also sync Group-level veo_file knob
                if "veo_file" in node.knobs():
                    node["veo_file"].setValue(media_path.replace("\\", "/"))
                if "veo_output_path" in node.knobs():
                    node["veo_output_path"].setValue(media_path.replace("\\", "/"))

        # --- 1. Save connections ---
        # Upstream (inputs to this node)
        upstream = {}  # {input_index: upstream_node}
        for i in range(node.inputs()):
            inp = node.input(i)
            if inp:
                upstream[i] = inp

        # Downstream (nodes that take this node as input)
        downstream = []  # [(dependent_node, input_index)]
        for dep in node.dependent(nuke.INPUTS | nuke.HIDDEN_INPUTS):
            for i in range(dep.inputs()):
                if dep.input(i) == node:
                    downstream.append((dep, i))

        # Save position
        xpos = int(node["xpos"].value())
        ypos = int(node["ypos"].value())

        # --- 2. Select only this node and copy to clipboard ---
        for n in nuke.allNodes():
            n.setSelected(False)
        node.setSelected(True)

        nuke.nodeCopy("%clipboard%")
        print("{}   nodeCopy OK".format(_tag))

        # --- 3. Delete old node ---
        nuke.delete(node)
        print("{}   deleted old '{}'".format(_tag, node_name))

        # --- 4. Paste from clipboard ---
        for n in nuke.allNodes():
            n.setSelected(False)

        nuke.nodePaste("%clipboard%")
        print("{}   nodePaste OK".format(_tag))

        # The pasted node(s) are selected — find our new Group
        new_node = None
        for n in nuke.selectedNodes():
            if n.Class() == "Group" and "is_veo_viewer" in n.knobs():
                new_node = n
                break

        if not new_node:
            print("{} ERROR: Could not find pasted node!".format(_tag))
            return None

        # --- 5. Restore name ---
        if new_node.name() != node_name:
            new_node["name"].setValue(node_name)
            print("{}   renamed to '{}'".format(_tag, node_name))

        # Restore position (nodePaste offsets by +10,+10)
        new_node["xpos"].setValue(xpos)
        new_node["ypos"].setValue(ypos)

        # --- 6. Restore connections ---
        for idx, up_node in upstream.items():
            try:
                new_node.setInput(idx, up_node)
            except Exception as e:
                print("{}   upstream input {} err: {}".format(_tag, idx, e))

        for dep_node, dep_idx in downstream:
            try:
                dep_node.setInput(dep_idx, new_node)
            except Exception as e:
                print("{}   downstream '{}' input {} err: {}".format(
                    _tag, dep_node.name(), dep_idx, e))

        # --- 7. Ensure postage_stamp is ON ---
        if "postage_stamp" in new_node.knobs():
            new_node["postage_stamp"].setValue(True)

        # --- 8. Verify internal Read frame range after paste ---
        # nodeCopy/nodePaste serialises knob values, so the range set in
        # step 0 should survive.  But as a safety net, re-check and fix.
        try:
            new_ir = _get_internal_read(new_node)
            if new_ir:
                new_node.begin()
                _of = int(new_ir["origfirst"].value()) if "origfirst" in new_ir.knobs() else None
                _ol = int(new_ir["origlast"].value()) if "origlast" in new_ir.knobs() else None
                _range_ok = False
                if _of is not None and _ol is not None and _ol > _of:
                    _cf = int(new_ir["first"].value()) if "first" in new_ir.knobs() else None
                    _cl = int(new_ir["last"].value()) if "last" in new_ir.knobs() else None
                    if _cf != _of or _cl != _ol:
                        if "first" in new_ir.knobs():
                            new_ir["first"].setValue(_of)
                        if "last" in new_ir.knobs():
                            new_ir["last"].setValue(_ol)
                        print("{}   fixed Read frame range: {}-{} (was {}-{})".format(
                            _tag, _of, _ol, _cf, _cl))
                    # Also sync to Group-level knobs
                    for _rk, _gk in [("first", "veo_first"), ("last", "veo_last"),
                                      ("origfirst", "veo_origfirst"), ("origlast", "veo_origlast")]:
                        if _rk in new_ir.knobs() and _gk in new_node.knobs():
                            new_node[_gk].setValue(int(new_ir[_rk].value()))
                    _range_ok = True
                # Fallback: Read origfirst/origlast still 1/1, use Group knobs
                if not _range_ok:
                    _gf = int(new_node["veo_first"].value()) if "veo_first" in new_node.knobs() else 1
                    _gl = int(new_node["veo_last"].value()) if "veo_last" in new_node.knobs() else 1
                    if _gl > _gf:
                        if "first" in new_ir.knobs():
                            new_ir["first"].setValue(_gf)
                        if "last" in new_ir.knobs():
                            new_ir["last"].setValue(_gl)
                        if "origfirst" in new_ir.knobs():
                            new_ir["origfirst"].setValue(_gf)
                        if "origlast" in new_ir.knobs():
                            new_ir["origlast"].setValue(_gl)
                        print("{}   pushed Group range {}-{} -> Read (fallback)".format(
                            _tag, _gf, _gl))
                new_node.end()
        except Exception as _vre:
            print("{}   frame range verify error: {}".format(_tag, _vre))

        # Deselect
        new_node.setSelected(False)

        print("{} DONE — new node '{}' created".format(_tag, new_node.name()))
        nuke.Undo.end()
        return new_node

    except Exception as e:
        import traceback
        print("{} FATAL ERROR: {}\n{}".format(_tag, e, traceback.format_exc()))
        try:
            nuke.Undo.cancel()
        except Exception:
            try:
                nuke.Undo.end()
            except Exception:
                pass
        return None


def _update_veo_thumbnail(node, media_path=None):
    """Enable Nuke postage-stamp thumbnail on a VEO Viewer Group node.

    Mirrors NanoBanana's ``_update_node_thumbnail`` but adapted for
    video (VEO) content.  The key steps are:
      1. Ensure InternalRead has the correct file loaded.
      2. Enable the ``postage_stamp`` knob.
      3. Force pixel computation so there's data to render.
      4. Trigger DAG refresh so the thumbnail appears immediately.

    NOTE: This is the *fallback* method.  For reliable thumbnail refresh,
    use ``_rebuild_veo_group_for_thumbnail`` (Replacement Jutsu) first.
    """
    if not node or node.Class() != "Group":
        print("[VEO] Thumbnail: skip — invalid node")
        return

    # 1. Ensure InternalRead points to the media file
    if media_path and os.path.isfile(media_path):
        try:
            ir = _get_internal_read(node)
            if ir:
                node.begin()
                ir["file"].fromUserText(media_path)
                node.end()
                print("[VEO] Thumbnail: InternalRead loaded {}".format(media_path))
            else:
                print("[VEO] Thumbnail: WARNING no InternalRead found")
        except Exception as e:
            print("[VEO] Thumbnail: load error: {}".format(e))

    # 2. Enable postage_stamp
    if "postage_stamp" in node.knobs():
        try:
            node["postage_stamp"].setValue(True)
            print("[VEO] Thumbnail: postage_stamp enabled on '{}'".format(
                node.name()))
        except Exception as e:
            print("[VEO] Thumbnail: postage_stamp error: {}".format(e))
    else:
        print("[VEO] Thumbnail: WARNING no postage_stamp knob")

    # 3. Force pixel computation
    try:
        node.sample("red", 0, 0)
    except Exception:
        pass

    # 4. Force DAG refresh — toggle postage_stamp off/on + modified()
    try:
        node["postage_stamp"].setValue(False)
        node["postage_stamp"].setValue(True)
        nuke.modified()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# VEO Viewer Node (unified: Read playback + record + regeneration in one node)
# Mirrors NanoBanana's Nano Viewer pattern.
# ---------------------------------------------------------------------------
def _next_veo_viewer_name():
    """Return the next available name like 'VEO_Viewer1', 'VEO_Viewer2', etc."""
    used = set()
    for node in nuke.allNodes():
        m = re.match(r"^VEO_Viewer(\d+)$", node.name())
        if m:
            used.add(int(m.group(1)))
    i = 1
    while i in used:
        i += 1
    return "VEO_Viewer{}".format(i)


def create_veo_viewer_node(generator_node, prompt, aspect_ratio, duration,
                           output_video_path,
                           reference_image_paths=None,
                           model="Google VEO 3.1-Fast",
                           resolution="720P",
                           mode="Text",
                           negative_prompt=""):
    """Create a unified VEO Viewer node (like NanoBanana's Nano Viewer).

    Combines the old sp (record) + VEO Player (Read) into a single Group:
      - Tab "Read":       Internal Read node with exposed knobs + Send to Studio
      - Tab "Regenerate": Generation record (read-only) + editable regeneration UI

    Node chain:  Generator → Dot → VEO_Viewer  (no separate sp / Player nodes)

    Returns:
        (viewer_node, internal_read_node)
    """
    gen_x = generator_node["xpos"].value()
    gen_y = generator_node["ypos"].value()
    gen_name = generator_node.name()

    # Find existing VEO Viewer nodes for THIS generator
    existing_viewers = []
    for node in nuke.allNodes("Group"):
        if "is_veo_viewer" in node.knobs():
            if "veo_generator" in node.knobs():
                if node["veo_generator"].value() == gen_name:
                    existing_viewers.append(node)

    # Calculate position
    if existing_viewers:
        last_viewer = max(existing_viewers, key=lambda n: n["ypos"].value())
        vx = last_viewer["xpos"].value()
        vy = last_viewer["ypos"].value() + 150
        connect_to = last_viewer
    else:
        vx = gen_x
        vy = gen_y + 150
        connect_to = generator_node

    # --- Dot node between generator/previous-viewer and this viewer ---
    dot_node = nuke.nodes.Dot()
    dot_x = int(vx) + 34
    dot_y = int(connect_to["ypos"].value()) + 80 if not existing_viewers else int(vy) - 50
    if not existing_viewers:
        dot_y = int(gen_y) + 100
    dot_node["xpos"].setValue(dot_x)
    dot_node["ypos"].setValue(dot_y)
    dot_node.setInput(0, connect_to)

    viewer_num = len(existing_viewers) + 1

    # ================================================================
    # Create the unified Group node
    # ================================================================
    nuke.Undo.begin("Create VEO Viewer")
    try:
        group = nuke.nodes.Group()
        group.setName(_next_veo_viewer_name())
        group["tile_color"].setValue(0x2E2E2EFF)  # Dark grey (same as Nano Viewer)
        group["xpos"].setValue(int(vx))
        group["ypos"].setValue(int(vy))

        # --- Build internals: Input → Read → Output ---
        group.begin()
        inp_node = nuke.nodes.Input(name="Input")
        read_node = nuke.nodes.Read(name="InternalRead")
        out_node = nuke.nodes.Output(name="Output")
        out_node.setInput(0, read_node)
        group.end()

        # Load the video file AFTER group.end() so knobs are fully populated
        if output_video_path and os.path.exists(output_video_path):
            group.begin()
            read_node["file"].fromUserText(output_video_path)
            # Force Read to re-parse the video so metadata (frame range etc.) is available
            if "reload" in read_node.knobs():
                try:
                    read_node["reload"].execute()
                except Exception:
                    pass
            # Explicitly set first/last = origfirst/origlast so video plays full range
            _range_set = False
            try:
                _of = int(read_node["origfirst"].value()) if "origfirst" in read_node.knobs() else None
                _ol = int(read_node["origlast"].value()) if "origlast" in read_node.knobs() else None
                if _of is not None and _ol is not None and _ol > _of:
                    if "first" in read_node.knobs():
                        read_node["first"].setValue(_of)
                    if "last" in read_node.knobs():
                        read_node["last"].setValue(_ol)
                    _range_set = True
                    print("[VEO] create_veo_viewer_node: Read frame range set to {}-{} (from origfirst/origlast)".format(_of, _ol))
                else:
                    print("[VEO] create_veo_viewer_node: origfirst/origlast = {}/{} — not usable".format(_of, _ol))
            except Exception as _fe:
                print("[VEO] create_veo_viewer_node: frame range error: {}".format(_fe))
            # Fallback: calculate frame range from duration × fps if Read didn't parse it
            if not _range_set and duration:
                try:
                    _fps = nuke.root()["fps"].value()
                    if not _fps or _fps <= 0:
                        _fps = 24.0
                    _dur_str = str(duration).replace("s", "").strip()
                    _dur_val = float(_dur_str)
                    _last_frame = int(round(_dur_val * _fps))
                    if _last_frame > 1:
                        if "first" in read_node.knobs():
                            read_node["first"].setValue(1)
                        if "last" in read_node.knobs():
                            read_node["last"].setValue(_last_frame)
                        if "origfirst" in read_node.knobs():
                            read_node["origfirst"].setValue(1)
                        if "origlast" in read_node.knobs():
                            read_node["origlast"].setValue(_last_frame)
                        _range_set = True
                        print("[VEO] create_veo_viewer_node: Read frame range set to 1-{} (from duration={}s × fps={})".format(
                            _last_frame, _dur_val, _fps))
                except Exception as _de:
                    print("[VEO] create_veo_viewer_node: duration fallback error: {}".format(_de))
            group.end()

        # Connect to the Dot
        group.setInput(0, dot_node)

        # ==============================================================
        # Tab 1: Read  (REAL knobs — NOT Link_Knob — survive rename-undo)
        # ==============================================================
        tab_read = nuke.Tab_Knob("read_tab", "Read")
        group.addKnob(tab_read)

        _read_sync_knobs = []  # (group_knob_name, internal_read_knob_name)

        # --- file knob ---
        file_knob = nuke.File_Knob("veo_file", "file")
        if output_video_path:
            file_knob.setValue(output_video_path.replace("\\", "/"))
        group.addKnob(file_knob)

        # --- format (dropdown, like native Read) ---
        # Format_Knob has no enumValues; use nuke.formats() to build dropdown.
        fmt_values = []
        try:
            for _f in nuke.formats():
                _fn = _f.name()
                if _fn:
                    fmt_values.append(_fn)
        except Exception:
            pass
        if not fmt_values:
            fmt_values = ["---"]
        fmt_current = "---"
        try:
            _fv = read_node["format"].value()
            if hasattr(_fv, 'width') and _fv.width() > 0:
                fmt_current = '%dx%d' % (_fv.width(), _fv.height())
            elif hasattr(_fv, 'name') and _fv.name():
                fmt_current = _fv.name()
            else:
                fmt_current = str(_fv)
        except Exception:
            pass
        if fmt_current and fmt_current not in fmt_values:
            fmt_values.append(fmt_current)
        format_knob = nuke.Enumeration_Knob("veo_format", "format", fmt_values)
        format_knob.setValue(fmt_current)
        group.addKnob(format_knob)
        _read_sync_knobs.append(("veo_format", "format"))

        # --- frame range knobs (Int_Knob) ---
        if "first" in read_node.knobs():
            try:
                fk = nuke.Int_Knob("veo_first", "first")
                fk.setValue(int(read_node["first"].value()))
                fk.setFlag(nuke.STARTLINE)
                group.addKnob(fk)
                _read_sync_knobs.append(("veo_first", "first"))
            except Exception:
                pass
        if "last" in read_node.knobs():
            try:
                lk = nuke.Int_Knob("veo_last", "last")
                lk.setValue(int(read_node["last"].value()))
                lk.clearFlag(nuke.STARTLINE)
                group.addKnob(lk)
                _read_sync_knobs.append(("veo_last", "last"))
            except Exception:
                pass

        # --- frame_mode / frame (Enumeration_Knob + Int_Knob) ---
        if "frame_mode" in read_node.knobs():
            try:
                fmk = nuke.Enumeration_Knob("veo_frame_mode", "frame mode", read_node["frame_mode"].enums())
                fmk.setValue(read_node["frame_mode"].value())
                fmk.setFlag(nuke.STARTLINE)
                group.addKnob(fmk)
                _read_sync_knobs.append(("veo_frame_mode", "frame_mode"))
            except Exception:
                pass
        if "frame" in read_node.knobs():
            try:
                frk = nuke.Int_Knob("veo_frame", "frame")
                frk.setValue(int(read_node["frame"].value()))
                frk.clearFlag(nuke.STARTLINE)
                group.addKnob(frk)
                _read_sync_knobs.append(("veo_frame", "frame"))
            except Exception:
                pass

        if "origfirst" in read_node.knobs():
            try:
                ofk = nuke.Int_Knob("veo_origfirst", "origfirst")
                ofk.setValue(int(read_node["origfirst"].value()))
                ofk.setFlag(nuke.STARTLINE)
                group.addKnob(ofk)
                _read_sync_knobs.append(("veo_origfirst", "origfirst"))
            except Exception:
                pass
        if "origlast" in read_node.knobs():
            try:
                olk = nuke.Int_Knob("veo_origlast", "origlast")
                olk.setValue(int(read_node["origlast"].value()))
                olk.clearFlag(nuke.STARTLINE)
                group.addKnob(olk)
                _read_sync_knobs.append(("veo_origlast", "origlast"))
            except Exception:
                pass

        # --- on_error (Enumeration_Knob) ---
        if "on_error" in read_node.knobs():
            try:
                oek = nuke.Enumeration_Knob("veo_on_error", "missing frames", read_node["on_error"].enums())
                oek.setValue(read_node["on_error"].value())
                oek.setFlag(nuke.STARTLINE)
                group.addKnob(oek)
                _read_sync_knobs.append(("veo_on_error", "on_error"))
            except Exception:
                pass

        # --- colorspace / Input Transform (Enumeration_Knob) ---
        # NOTE: Do NOT force colorspace to "default" — in OCIO mode that is
        # an invalid LUT name and causes "Invalid LUT selected : default".
        # Let Nuke keep whatever it auto-detected when loading the video.
        # In OCIO mode the label is "Input Transform"; we use the Read's own label.
        if "colorspace" in read_node.knobs():
            cs_label = read_node["colorspace"].label() or "colorspace"
            cs_values = []
            try:
                _cs_k = read_node["colorspace"]
                if hasattr(_cs_k, "values") and callable(_cs_k.values):
                    cs_values = list(_cs_k.values()) or []
                elif hasattr(_cs_k, "enumerationItems") and callable(_cs_k.enumerationItems):
                    cs_values = list(_cs_k.enumerationItems()) or []
            except Exception:
                pass
            if not cs_values:
                cs_values = ["default", "linear", "sRGB", "Gamma1.8", "Gamma2.2",
                             "Rec709", "ACEScg", "ALEXAV3LogC"]
            current_cs = str(read_node["colorspace"].value())
            if current_cs not in cs_values:
                cs_values.insert(0, current_cs)
            csk = nuke.Enumeration_Knob("veo_colorspace", cs_label, cs_values)
            csk.setValue(current_cs)
            csk.setFlag(nuke.STARTLINE)
            group.addKnob(csk)
            _read_sync_knobs.append(("veo_colorspace", "colorspace"))

        for _kname in ["premultiplied", "raw", "auto_alpha"]:
            if _kname in read_node.knobs():
                try:
                    _bk = nuke.Boolean_Knob("veo_" + _kname, read_node[_kname].label() or _kname)
                    _bk.setValue(int(bool(read_node[_kname].value())))
                    _bk.clearFlag(nuke.STARTLINE)
                    group.addKnob(_bk)
                    _read_sync_knobs.append(("veo_" + _kname, _kname))
                except Exception:
                    pass
        # --- MOV Options section (real knobs — NOT Link_Knob) ---
        _mov_knob_names = [
            "ycbcr_matrix", "mov_data_range",
            "first_track_only", "metadata", "noprefix", "match_key_format",
            "mov64_decode_codec", "mov_decode_codec",
            "video_codec_knob",
        ]
        mov_divider_added = False
        for kname in _mov_knob_names:
            if kname in read_node.knobs():
                if not mov_divider_added:
                    mov_div = nuke.Text_Knob("mov_divider", "MOV Options")
                    group.addKnob(mov_div)
                    mov_divider_added = True
                try:
                    _rk_obj = read_node[kname]
                    _cls_name = _rk_obj.Class()
                    if _cls_name in ("Enumeration_Knob",):
                        _ek = nuke.Enumeration_Knob("veo_" + kname, _rk_obj.label() or kname, _rk_obj.enums())
                        _ek.setValue(_rk_obj.value())
                        group.addKnob(_ek)
                        _read_sync_knobs.append(("veo_" + kname, kname))
                    elif _cls_name in ("String_Knob", "File_Knob", "WHK_Knob"):
                        _sk = nuke.String_Knob("veo_" + kname, _rk_obj.label() or kname)
                        _sk.setValue(str(_rk_obj.value()))
                        group.addKnob(_sk)
                        _read_sync_knobs.append(("veo_" + kname, kname))
                    else:
                        # Fallback: use string knob
                        _sk2 = nuke.String_Knob("veo_" + kname, _rk_obj.label() or kname)
                        _sk2.setValue(str(_rk_obj.value()))
                        group.addKnob(_sk2)
                        _read_sync_knobs.append(("veo_" + kname, kname))
                except Exception:
                    pass

        if mov_divider_added:
            try:
                codec_val = ""
                if "video_codec_knob" in read_node.knobs():
                    codec_val = read_node["video_codec_knob"].value()
                elif "mov64_decode_codec" in read_node.knobs():
                    codec_val = read_node["mov64_decode_codec"].value()
                if codec_val:
                    codec_text = nuke.Text_Knob("video_codec_display", "Video Codec", str(codec_val))
                    group.addKnob(codec_text)
            except Exception:
                pass

        # --- Open Full Read Properties button ---
        open_read_script = (
            "n = nuke.thisNode()\n"
            "n.begin()\n"
            "r = nuke.toNode('InternalRead')\n"
            "n.end()\n"
            "if r: nuke.show(r)"
        )
        open_btn = nuke.PyScript_Knob("open_read_props", "Open Full Read Properties", open_read_script)
        open_btn.setFlag(nuke.STARTLINE)
        group.addKnob(open_btn)

        # --- knobChanged callback: sync ALL exposed knobs <-> internal Read ---
        # Uses name-based lookup (nuke.toNode('InternalRead')) so it survives
        # rename-undo.  When any exposed knob changes, push its value into
        # the corresponding internal Read knob.
        _sync_pairs_str = repr(_read_sync_knobs).replace("'", '"')
        kc_script = (
            "import nuke\n"
            "n = nuke.thisNode()\n"
            "k = nuke.thisKnob()\n"
            "kn = k.name()\n"
            "# ===== DEBUG: knobChanged fired =====\n"
            "_dbg = '[VEO-DEBUG] knobChanged: node=%s knob=%s value=%s' % (n.name(), kn, str(k.value())[:80])\n"
            "print(_dbg)\n"
            "n.begin()\n"
            "r = nuke.toNode('InternalRead')\n"
            "n.end()\n"
            "_dbg2 = '[VEO-DEBUG] InternalRead found=%s' % (r is not None)\n"
            "print(_dbg2)\n"
            "if not r:\n"
            "    print('[VEO-DEBUG] WARNING: InternalRead NOT FOUND!')\n"
            "    pass\n"
            "# File changed: load into Read + pull fresh values\n"
            "if kn == 'veo_file' and r:\n"
            "    n.begin()\n"
            "    r['file'].fromUserText(k.value())\n"
            "    n.end()\n"
            "    # Pull fresh format from Read\n"
            "    try:\n"
            "        _fv = r['format'].value()\n"
            "        _fmt_name = ''\n"
            "        if hasattr(_fv, 'name') and _fv.name():\n"
            "            _fmt_name = _fv.name()\n"
            "        elif hasattr(_fv, 'width') and _fv.width() > 0:\n"
            "            _fmt_name = '%dx%d' % (_fv.width(), _fv.height())\n"
            "        if _fmt_name and 'veo_format' in n.knobs():\n"
            "            _cur_vals = list(n['veo_format'].values())\n"
            "            if _fmt_name not in _cur_vals:\n"
            "                _cur_vals.append(_fmt_name)\n"
            "                n['veo_format'].setValues(_cur_vals)\n"
            "            n['veo_format'].setValue(_fmt_name)\n"
            "    except Exception:\n"
            "        pass\n"
            "    # Pull fresh frame range from Read\n"
            "    for _frk, _fgk in [('first','veo_first'),('last','veo_last'),('origfirst','veo_origfirst'),('origlast','veo_origlast')]:\n"
            "        try:\n"
            "            if _frk in r.knobs() and _fgk in n.knobs():\n"
            "                n[_fgk].setValue(int(r[_frk].value()))\n"
            "        except Exception:\n"
            "            pass\n"
            "    # Pull fresh colorspace from Read\n"
            "    try:\n"
            "        _cv = str(r['colorspace'].value())\n"
            "        n['veo_colorspace'].setValue(_cv)\n"
            "    except Exception:\n"
            "        pass\n"
            "    # Force postage stamp refresh\n"
            "    if 'postage_stamp' in n.knobs():\n"
            "        n['postage_stamp'].setValue(True)\n"
            "    try:\n"
            "        n.sample('red', 0, 0)\n"
            "    except Exception:\n"
            "        pass\n"
            "# Sync Group -> Read for all other exposed knobs\n"
            "_pairs = " + _sync_pairs_str + "\n"
            "for _gk, _rk in _pairs:\n"
            "    if kn == _gk and r and _rk in r.knobs():\n"
            "        try:\n"
            "            if _rk == 'format':\n"
            "                n.begin()\n"
            "                r['format'].setValue(k.value())\n"
            "                n.end()\n"
            "            elif isinstance(r[_rk].value(), int):\n"
            "                n.begin()\n"
            "                r[_rk].setValue(int(k.value()))\n"
            "                n.end()\n"
            "            else:\n"
            "                n.begin()\n"
            "                r[_rk].setValue(k.value())\n"
            "                n.end()\n"
            "            print('[VEO-DEBUG] Synced %s -> %s = %s' % (_gk, _rk, str(k.value())[:60]))\n"
            "        except Exception as _e:\n"
            "            print('[VEO-DEBUG] SYNC ERROR %s->%s: %s' % (_gk, _rk, _e))\n"
            "print('[VEO-DEBUG] knobChanged done for %s' % kn)\n"
        )
        group["knobChanged"].setValue(kc_script)

        # --- Divider + Send to Studio button ---
        divider = nuke.Text_Knob("studio_divider", "")
        group.addKnob(divider)

        btn = nuke.PyScript_Knob("send_to_studio", "Send to Studio", _VEO_PLAYER_SEND_SCRIPT)
        btn.setFlag(nuke.STARTLINE)
        group.addKnob(btn)

        # ==============================================================
        # Tab 2: Regenerate  (generation record + editable regeneration UI)
        # ==============================================================
        tab_regen = nuke.Tab_Knob("veo_regen_tab", "Regenerate")
        group.addKnob(tab_regen)

        # --- Hidden knobs storing generation parameters ---
        gen_knob = nuke.String_Knob("veo_generator", "Generator")
        gen_knob.setValue(gen_name)
        gen_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(gen_knob)

        prompt_knob = nuke.Multiline_Eval_String_Knob("veo_prompt", "Prompt")
        prompt_knob.setValue(prompt)
        prompt_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(prompt_knob)

        ratio_knob = nuke.String_Knob("veo_ratio", "Aspect Ratio")
        ratio_knob.setValue(aspect_ratio)
        ratio_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(ratio_knob)

        dur_knob = nuke.String_Knob("veo_duration", "Duration")
        dur_knob.setValue(duration if duration else "8")
        dur_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(dur_knob)

        model_knob = nuke.String_Knob("veo_model", "Model")
        model_knob.setValue(model)
        model_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(model_knob)

        res_knob = nuke.String_Knob("veo_resolution", "Resolution")
        res_knob.setValue(resolution)
        res_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(res_knob)

        mode_knob = nuke.String_Knob("veo_mode", "Mode")
        mode_knob.setValue(mode)
        mode_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(mode_knob)

        neg_prompt_knob = nuke.Multiline_Eval_String_Knob("veo_neg_prompt", "Negative Prompt")
        neg_prompt_knob.setValue(negative_prompt)
        neg_prompt_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(neg_prompt_knob)

        output_knob = nuke.File_Knob("veo_output_path", "Output Video")
        output_knob.setValue(output_video_path.replace("\\", "/") if output_video_path else "")
        output_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(output_knob)

        # Store input reference images as JSON array (same format as NanoBanana)
        input_img_paths = list(reference_image_paths or [])
        print("[VEO Viewer] veo_input_images: storing {} paths".format(len(input_img_paths)))
        for _ip in input_img_paths:
            print("  [VEO Viewer]   -> {}".format(_ip))
        inputs_knob = nuke.Multiline_Eval_String_Knob("veo_input_images", "Input Images (JSON)")
        inputs_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(inputs_knob)
        inputs_knob.setValue(json.dumps(input_img_paths))

        # --- PyCustom_Knob for regenerate UI ---
        regen_divider = nuke.Text_Knob("regen_divider", "")
        group.addKnob(regen_divider)

        custom_knob = nuke.PyCustom_Knob(
            "veo_regen_ui", "",
            "ai_workflow.veo.VeoViewerRegenWidget()"
        )
        custom_knob.setFlag(nuke.STARTLINE)
        group.addKnob(custom_knob)

        # --- Hidden marker knob to identify VEO Viewer nodes ---
        marker = nuke.Boolean_Knob("is_veo_viewer", "")
        marker.setValue(True)
        marker.setFlag(nuke.INVISIBLE)
        group.addKnob(marker)

        # --- Enable postage-stamp thumbnail (like NanoBanana) ---
        _update_veo_thumbnail(group, output_video_path)

        # --- Final sync: push Group-level frame range INTO internal Read ---
        # At creation time, knobChanged doesn't fire for initial values,
        # so the internal Read may still have first/last=1 even though the
        # Group-level veo_first/veo_last are correct.  Push them explicitly.
        try:
            group.begin()
            for _gk, _rk in [("veo_first", "first"), ("veo_last", "last"),
                              ("veo_origfirst", "origfirst"), ("veo_origlast", "origlast")]:
                if _gk in group.knobs() and _rk in read_node.knobs():
                    _gv = int(group[_gk].value())
                    _rv = int(read_node[_rk].value())
                    if _gv != _rv:
                        read_node[_rk].setValue(_gv)
                        print("[VEO] create_veo_viewer_node: synced {} -> InternalRead.{} = {}".format(
                            _gk, _rk, _gv))
            group.end()
        except Exception as _sync_e:
            print("[VEO] create_veo_viewer_node: final sync error: {}".format(_sync_e))
            try:
                group.end()
            except Exception:
                pass

        print("VEO: Created VEO Viewer '{}' with internal Read for: {}".format(
            group.name(), output_video_path))
        return group, read_node
    finally:
        nuke.Undo.end()


def create_veo_viewer_standalone(xpos=None, ypos=None):
    """Manually create an empty VEO Viewer node (no generator, no video).

    Called from the toolbar menu for standalone testing / manual usage.
    Mirrors NanoBanana's ``create_nb_player_node(xpos, ypos)`` pattern.

    Returns:
        (viewer_group, internal_read_node)
    """
    nuke.Undo.begin("Create VEO Viewer")
    try:
        group = nuke.nodes.Group()
        group.setName(_next_veo_viewer_name())
        group["tile_color"].setValue(0x2E2E2EFF)  # Dark grey (same as Nano Viewer)

        if xpos is not None:
            group["xpos"].setValue(int(xpos))
        if ypos is not None:
            group["ypos"].setValue(int(ypos))

        # --- Build internals: Read → Output ---
        group.begin()
        read_node = nuke.nodes.Read(name="InternalRead")
        out_node = nuke.nodes.Output(name="Output")
        out_node.setInput(0, read_node)
        group.end()

        # ==============================================================
        # Tab 1: Read  (REAL knobs — NOT Link_Knob — survive rename-undo)
        # ==============================================================
        tab_read = nuke.Tab_Knob("read_tab", "Read")
        group.addKnob(tab_read)

        _read_sync_knobs = []  # (group_knob_name, internal_read_knob_name)

        # --- file knob ---
        file_knob = nuke.File_Knob("veo_file", "file")
        group.addKnob(file_knob)

        # --- format (dropdown, like native Read) ---
        # Format_Knob has no enumValues; use nuke.formats() to build dropdown.
        fmt_values = []
        try:
            for _f in nuke.formats():
                _fn = _f.name()
                if _fn:
                    fmt_values.append(_fn)
        except Exception:
            pass
        if not fmt_values:
            fmt_values = ["---"]
        fmt_current = "---"
        try:
            _fv = read_node["format"].value()
            if hasattr(_fv, 'width') and _fv.width() > 0:
                fmt_current = '%dx%d' % (_fv.width(), _fv.height())
            elif hasattr(_fv, 'name') and _fv.name():
                fmt_current = _fv.name()
            else:
                fmt_current = str(_fv)
        except Exception:
            pass
        if fmt_current and fmt_current not in fmt_values:
            fmt_values.append(fmt_current)
        format_knob = nuke.Enumeration_Knob("veo_format", "format", fmt_values)
        format_knob.setValue(fmt_current)
        group.addKnob(format_knob)
        _read_sync_knobs.append(("veo_format", "format"))

        # --- frame range knobs (Int_Knob) ---
        if "first" in read_node.knobs():
            try:
                fk = nuke.Int_Knob("veo_first", "first")
                fk.setValue(int(read_node["first"].value()))
                fk.setFlag(nuke.STARTLINE)
                group.addKnob(fk)
                _read_sync_knobs.append(("veo_first", "first"))
            except Exception:
                pass
        if "last" in read_node.knobs():
            try:
                lk = nuke.Int_Knob("veo_last", "last")
                lk.setValue(int(read_node["last"].value()))
                lk.clearFlag(nuke.STARTLINE)
                group.addKnob(lk)
                _read_sync_knobs.append(("veo_last", "last"))
            except Exception:
                pass

        # --- frame_mode / frame (Enumeration_Knob + Int_Knob) ---
        if "frame_mode" in read_node.knobs():
            try:
                fmk = nuke.Enumeration_Knob("veo_frame_mode", "frame mode", read_node["frame_mode"].enums())
                fmk.setValue(read_node["frame_mode"].value())
                fmk.setFlag(nuke.STARTLINE)
                group.addKnob(fmk)
                _read_sync_knobs.append(("veo_frame_mode", "frame_mode"))
            except Exception:
                pass
        if "frame" in read_node.knobs():
            try:
                frk = nuke.Int_Knob("veo_frame", "frame")
                frk.setValue(int(read_node["frame"].value()))
                frk.clearFlag(nuke.STARTLINE)
                group.addKnob(frk)
                _read_sync_knobs.append(("veo_frame", "frame"))
            except Exception:
                pass

        if "origfirst" in read_node.knobs():
            try:
                ofk = nuke.Int_Knob("veo_origfirst", "origfirst")
                ofk.setValue(int(read_node["origfirst"].value()))
                ofk.setFlag(nuke.STARTLINE)
                group.addKnob(ofk)
                _read_sync_knobs.append(("veo_origfirst", "origfirst"))
            except Exception:
                pass
        if "origlast" in read_node.knobs():
            try:
                olk = nuke.Int_Knob("veo_origlast", "origlast")
                olk.setValue(int(read_node["origlast"].value()))
                olk.clearFlag(nuke.STARTLINE)
                group.addKnob(olk)
                _read_sync_knobs.append(("veo_origlast", "origlast"))
            except Exception:
                pass

        # --- on_error (Enumeration_Knob) ---
        if "on_error" in read_node.knobs():
            try:
                oek = nuke.Enumeration_Knob("veo_on_error", "missing frames", read_node["on_error"].enums())
                oek.setValue(read_node["on_error"].value())
                oek.setFlag(nuke.STARTLINE)
                group.addKnob(oek)
                _read_sync_knobs.append(("veo_on_error", "on_error"))
            except Exception:
                pass

        # --- colorspace / Input Transform (Enumeration_Knob) ---
        # In OCIO mode the label is "Input Transform"; we use the Read's own label.
        if "colorspace" in read_node.knobs():
            cs_label = read_node["colorspace"].label() or "colorspace"
            cs_values = []
            try:
                _cs_k = read_node["colorspace"]
                if hasattr(_cs_k, "values") and callable(_cs_k.values):
                    cs_values = list(_cs_k.values()) or []
                elif hasattr(_cs_k, "enumerationItems") and callable(_cs_k.enumerationItems):
                    cs_values = list(_cs_k.enumerationItems()) or []
            except Exception:
                pass
            if not cs_values:
                cs_values = ["default", "linear", "sRGB", "Gamma1.8", "Gamma2.2",
                             "Rec709", "ACEScg", "ALEXAV3LogC"]
            current_cs = str(read_node["colorspace"].value())
            if current_cs not in cs_values:
                cs_values.insert(0, current_cs)
            csk = nuke.Enumeration_Knob("veo_colorspace", cs_label, cs_values)
            csk.setValue(current_cs)
            csk.setFlag(nuke.STARTLINE)
            group.addKnob(csk)
            _read_sync_knobs.append(("veo_colorspace", "colorspace"))

        for _kname in ["premultiplied", "raw", "auto_alpha"]:
            if _kname in read_node.knobs():
                try:
                    _bk = nuke.Boolean_Knob("veo_" + _kname, read_node[_kname].label() or _kname)
                    _bk.setValue(int(bool(read_node[_kname].value())))
                    _bk.clearFlag(nuke.STARTLINE)
                    group.addKnob(_bk)
                    _read_sync_knobs.append(("veo_" + _kname, _kname))
                except Exception:
                    pass

        # --- MOV Options section (real knobs — NOT Link_Knob) ---
        _mov_knob_names = [
            "ycbcr_matrix", "mov_data_range",
            "first_track_only", "metadata", "noprefix", "match_key_format",
            "mov64_decode_codec", "mov_decode_codec",
            "video_codec_knob",
        ]
        mov_divider_added = False
        for kname in _mov_knob_names:
            if kname in read_node.knobs():
                if not mov_divider_added:
                    mov_div = nuke.Text_Knob("mov_divider", "MOV Options")
                    group.addKnob(mov_div)
                    mov_divider_added = True
                try:
                    _rk_obj = read_node[kname]
                    _cls_name = _rk_obj.Class()
                    if _cls_name in ("Enumeration_Knob",):
                        _ek = nuke.Enumeration_Knob("veo_" + kname, _rk_obj.label() or kname, _rk_obj.enums())
                        _ek.setValue(_rk_obj.value())
                        group.addKnob(_ek)
                        _read_sync_knobs.append(("veo_" + kname, kname))
                    elif _cls_name in ("String_Knob", "File_Knob", "WHK_Knob"):
                        _sk = nuke.String_Knob("veo_" + kname, _rk_obj.label() or kname)
                        _sk.setValue(str(_rk_obj.value()))
                        group.addKnob(_sk)
                        _read_sync_knobs.append(("veo_" + kname, kname))
                    else:
                        _sk2 = nuke.String_Knob("veo_" + kname, _rk_obj.label() or kname)
                        _sk2.setValue(str(_rk_obj.value()))
                        group.addKnob(_sk2)
                        _read_sync_knobs.append(("veo_" + kname, kname))
                except Exception:
                    pass

        if mov_divider_added:
            try:
                codec_val = ""
                if "video_codec_knob" in read_node.knobs():
                    codec_val = read_node["video_codec_knob"].value()
                elif "mov64_decode_codec" in read_node.knobs():
                    codec_val = read_node["mov64_decode_codec"].value()
                if codec_val:
                    codec_text = nuke.Text_Knob("video_codec_display", "Video Codec", str(codec_val))
                    group.addKnob(codec_text)
            except Exception:
                pass

        # --- Open Full Read Properties button ---
        open_read_script = (
            "n = nuke.thisNode()\n"
            "n.begin()\n"
            "r = nuke.toNode('InternalRead')\n"
            "n.end()\n"
            "if r: nuke.show(r)"
        )
        open_btn = nuke.PyScript_Knob("open_read_props", "Open Full Read Properties", open_read_script)
        open_btn.setFlag(nuke.STARTLINE)
        group.addKnob(open_btn)

        # --- knobChanged callback: sync ALL exposed knobs <-> internal Read ---
        _sync_pairs_str = repr(_read_sync_knobs).replace("'", '"')
        kc_script = (
            "import nuke\n"
            "n = nuke.thisNode()\n"
            "k = nuke.thisKnob()\n"
            "kn = k.name()\n"
            "# ===== DEBUG: knobChanged fired =====\n"
            "_dbg = '[VEO-DEBUG] knobChanged: node=%s knob=%s value=%s' % (n.name(), kn, str(k.value())[:80])\n"
            "print(_dbg)\n"
            "n.begin()\n"
            "r = nuke.toNode('InternalRead')\n"
            "n.end()\n"
            "_dbg2 = '[VEO-DEBUG] InternalRead found=%s' % (r is not None)\n"
            "print(_dbg2)\n"
            "if not r:\n"
            "    print('[VEO-DEBUG] WARNING: InternalRead NOT FOUND!')\n"
            "    pass\n"
            "# File changed: load into Read + pull fresh values\n"
            "if kn == 'veo_file' and r:\n"
            "    n.begin()\n"
            "    r['file'].fromUserText(k.value())\n"
            "    n.end()\n"
            "    # Pull fresh format from Read\n"
            "    try:\n"
            "        _fv = r['format'].value()\n"
            "        _fmt_name = ''\n"
            "        if hasattr(_fv, 'name') and _fv.name():\n"
            "            _fmt_name = _fv.name()\n"
            "        elif hasattr(_fv, 'width') and _fv.width() > 0:\n"
            "            _fmt_name = '%dx%d' % (_fv.width(), _fv.height())\n"
            "        if _fmt_name and 'veo_format' in n.knobs():\n"
            "            _cur_vals = list(n['veo_format'].values())\n"
            "            if _fmt_name not in _cur_vals:\n"
            "                _cur_vals.append(_fmt_name)\n"
            "                n['veo_format'].setValues(_cur_vals)\n"
            "            n['veo_format'].setValue(_fmt_name)\n"
            "    except Exception:\n"
            "        pass\n"
            "    # Pull fresh frame range from Read\n"
            "    for _frk, _fgk in [('first','veo_first'),('last','veo_last'),('origfirst','veo_origfirst'),('origlast','veo_origlast')]:\n"
            "        try:\n"
            "            if _frk in r.knobs() and _fgk in n.knobs():\n"
            "                n[_fgk].setValue(int(r[_frk].value()))\n"
            "        except Exception:\n"
            "            pass\n"
            "    # Pull fresh colorspace from Read\n"
            "    try:\n"
            "        _cv = str(r['colorspace'].value())\n"
            "        n['veo_colorspace'].setValue(_cv)\n"
            "    except Exception:\n"
            "        pass\n"
            "    # Force postage stamp refresh\n"
            "    if 'postage_stamp' in n.knobs():\n"
            "        n['postage_stamp'].setValue(True)\n"
            "    try:\n"
            "        n.sample('red', 0, 0)\n"
            "    except Exception:\n"
            "        pass\n"
            "# Sync Group -> Read for all other exposed knobs\n"
            "_pairs = " + _sync_pairs_str + "\n"
            "for _gk, _rk in _pairs:\n"
            "    if kn == _gk and r and _rk in r.knobs():\n"
            "        try:\n"
            "            if _rk == 'format':\n"
            "                n.begin()\n"
            "                r['format'].setValue(k.value())\n"
            "                n.end()\n"
            "            elif isinstance(r[_rk].value(), int):\n"
            "                n.begin()\n"
            "                r[_rk].setValue(int(k.value()))\n"
            "                n.end()\n"
            "            else:\n"
            "                n.begin()\n"
            "                r[_rk].setValue(k.value())\n"
            "                n.end()\n"
            "            print('[VEO-DEBUG] Synced %s -> %s = %s' % (_gk, _rk, str(k.value())[:60]))\n"
            "        except Exception as _e:\n"
            "            print('[VEO-DEBUG] SYNC ERROR %s->%s: %s' % (_gk, _rk, _e))\n"
            "print('[VEO-DEBUG] knobChanged done for %s' % kn)\n"
        )
        group["knobChanged"].setValue(kc_script)

        # --- Divider + Send to Studio button ---
        divider = nuke.Text_Knob("studio_divider", "")
        group.addKnob(divider)

        btn = nuke.PyScript_Knob("send_to_studio", "Send to Studio", _VEO_PLAYER_SEND_SCRIPT)
        btn.setFlag(nuke.STARTLINE)
        group.addKnob(btn)

        # ==============================================================
        # Tab 2: Regenerate  (empty – no generation parameters for standalone)
        # ==============================================================
        tab_regen = nuke.Tab_Knob("veo_regen_tab", "Regenerate")
        group.addKnob(tab_regen)

        # Hidden knobs with empty defaults (so the widget and other code doesn't crash)
        for kn_name, kn_cls, default in [
            ("veo_generator", nuke.String_Knob, ""),
            ("veo_prompt", nuke.Multiline_Eval_String_Knob, ""),
            ("veo_ratio", nuke.String_Knob, "16:9"),
            ("veo_duration", nuke.String_Knob, "8"),
            ("veo_model", nuke.String_Knob, ""),
            ("veo_resolution", nuke.String_Knob, "720P"),
            ("veo_mode", nuke.String_Knob, "Text"),
            ("veo_neg_prompt", nuke.Multiline_Eval_String_Knob, ""),
        ]:
            k = kn_cls(kn_name, kn_name.replace("veo_", "").replace("_", " ").title())
            k.setValue(default)
            k.setFlag(nuke.INVISIBLE)
            group.addKnob(k)

        output_knob = nuke.File_Knob("veo_output_path", "Output Video")
        output_knob.setValue("")
        output_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(output_knob)

        inputs_knob = nuke.Multiline_Eval_String_Knob("veo_input_images", "Input Images (JSON)")
        inputs_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(inputs_knob)
        inputs_knob.setValue(json.dumps([]))

        # --- PyCustom_Knob for regenerate UI ---
        regen_divider = nuke.Text_Knob("regen_divider", "")
        group.addKnob(regen_divider)

        custom_knob = nuke.PyCustom_Knob(
            "veo_regen_ui", "",
            "ai_workflow.veo.VeoViewerRegenWidget()"
        )
        custom_knob.setFlag(nuke.STARTLINE)
        group.addKnob(custom_knob)

        # --- Hidden marker ---
        marker = nuke.Boolean_Knob("is_veo_viewer", "")
        marker.setValue(True)
        marker.setFlag(nuke.INVISIBLE)
        group.addKnob(marker)

        print("VEO: Created standalone VEO Viewer '{}'".format(group.name()))
        return group, read_node
    finally:
        nuke.Undo.end()


def update_veo_viewer_read(viewer_node, new_video_path):
    """Update the internal Read node of a VEO Viewer with a new video path.
    If the viewer node doesn't exist or is invalid, create a new VEO Viewer."""
    if viewer_node is not None:
        internal_read = _get_internal_read(viewer_node)
        if internal_read:
            viewer_node.begin()
            internal_read["file"].fromUserText(new_video_path)
            # Force Read to re-parse video metadata (format, frame range, etc.)
            # Without this, first/last/origfirst/origlast may stay at default=1
            # because Nuke hasn't finished parsing the new MOV file yet.
            if "reload" in internal_read.knobs():
                try:
                    internal_read["reload"].execute()
                    print("[VEO] update_veo_viewer_read: reload executed for '{}'".format(
                        new_video_path))
                except Exception as _re:
                    print("[VEO] update_veo_viewer_read: reload failed: {}".format(_re))
            # After reload, Nuke updates origfirst/origlast from the file header,
            # but first/last (the user-editable frame range) may NOT auto-sync.
            # We must explicitly set first/last = origfirst/origlast so the Read
            # node actually plays the full frame range of the video.
            try:
                _of = int(internal_read["origfirst"].value()) if "origfirst" in internal_read.knobs() else None
                _ol = int(internal_read["origlast"].value()) if "origlast" in internal_read.knobs() else None
                if _of is not None and _ol is not None and _ol > _of:
                    if "first" in internal_read.knobs():
                        internal_read["first"].setValue(_of)
                    if "last" in internal_read.knobs():
                        internal_read["last"].setValue(_ol)
                    print("[VEO] update_veo_viewer_read: Read frame range set to {}-{}".format(_of, _ol))
                else:
                    print("[VEO] update_veo_viewer_read: origfirst/origlast = {}/{} — skipping first/last sync".format(_of, _ol))
            except Exception as _fe:
                print("[VEO] update_veo_viewer_read: frame range sync error: {}".format(_fe))
            viewer_node.end()
            if "veo_file" in viewer_node.knobs():
                viewer_node["veo_file"].setValue(new_video_path.replace("\\", "/"))
            # Sync format from Read to Group
            try:
                _fv = internal_read["format"].value()
                _fmt_name = ""
                if hasattr(_fv, "name") and _fv.name():
                    _fmt_name = _fv.name()
                elif hasattr(_fv, "width") and _fv.width() > 0:
                    _fmt_name = "%dx%d" % (_fv.width(), _fv.height())
                if _fmt_name and "veo_format" in viewer_node.knobs():
                    _cur_vals = list(viewer_node["veo_format"].values())
                    if _fmt_name not in _cur_vals:
                        _cur_vals.append(_fmt_name)
                        viewer_node["veo_format"].setValues(_cur_vals)
                    viewer_node["veo_format"].setValue(_fmt_name)
            except Exception:
                pass
            # Sync frame range from Read to Group
            for _rk, _gk in [("first", "veo_first"), ("last", "veo_last"),
                              ("origfirst", "veo_origfirst"), ("origlast", "veo_origlast")]:
                try:
                    if _rk in internal_read.knobs() and _gk in viewer_node.knobs():
                        viewer_node[_gk].setValue(int(internal_read[_rk].value()))
                except Exception:
                    pass
            # Fallback: if Read first/last are still 1/1 but Group has correct values,
            # push Group -> Read (the Group may already have correct range from creation)
            try:
                _rf = int(internal_read["first"].value()) if "first" in internal_read.knobs() else 1
                _rl = int(internal_read["last"].value()) if "last" in internal_read.knobs() else 1
                if _rl <= _rf:
                    # Read range is bad — try to get correct values from Group knobs
                    _gf = int(viewer_node["veo_first"].value()) if "veo_first" in viewer_node.knobs() else 1
                    _gl = int(viewer_node["veo_last"].value()) if "veo_last" in viewer_node.knobs() else 1
                    if _gl > _gf:
                        viewer_node.begin()
                        internal_read["first"].setValue(_gf)
                        internal_read["last"].setValue(_gl)
                        if "origfirst" in internal_read.knobs():
                            internal_read["origfirst"].setValue(_gf)
                        if "origlast" in internal_read.knobs():
                            internal_read["origlast"].setValue(_gl)
                        viewer_node.end()
                        print("[VEO] update_veo_viewer_read: pushed Group range {}-{} -> Read".format(_gf, _gl))
            except Exception as _fb_e:
                print("[VEO] update_veo_viewer_read: fallback push error: {}".format(_fb_e))
            # Sync colorspace from Read to Group
            try:
                if "colorspace" in internal_read.knobs() and "veo_colorspace" in viewer_node.knobs():
                    _cv = str(internal_read["colorspace"].value())
                    viewer_node["veo_colorspace"].setValue(_cv)
            except Exception:
                pass
        if "veo_output_path" in viewer_node.knobs():
            viewer_node["veo_output_path"].setValue(new_video_path.replace("\\", "/"))
        return viewer_node
    return None


# ---------------------------------------------------------------------------
# VEO Main Widget (embedded in VEO_Generate node)
# ---------------------------------------------------------------------------
class VeoWidget(QtWidgets.QWidget):
    """Custom Qt widget embedded inside the VEO_Generate node."""

    def __init__(self, node=None, parent=None):
        super(VeoWidget, self).__init__(parent)
        # Cache a reference to the owning node so save/restore always
        # target the correct VEO_Generate even when another node
        # is selected in the DAG.
        if node is None:
            try:
                node = nuke.thisNode()
            except Exception:
                node = None
        self._node = node

        self.setObjectName("nanoBananaRoot")
        self.setStyleSheet(NANOBANANA_STYLE)
        self.setMinimumWidth(380)
        # Disable subpixel antialiasing to prevent coloured fringe on buttons
        font = self.font()
        font.setStyleStrategy(QtGui.QFont.NoSubpixelAntialias)
        self.setFont(font)
        self.settings = NanoBananaSettings()
        self.current_worker = None
        self._build_ui()
        self._restore_all_state_from_node()

    def _build_ui(self):
        main = QtWidgets.QVBoxLayout(self)
        main.setSpacing(8)
        main.setContentsMargins(8, 8, 8, 8)

        # === Row 1: Model / Aspect ratio / Resolution / Duration in one row ===
        config_row = QtWidgets.QHBoxLayout()
        config_row.setSpacing(12)

        # Model selection
        model_group = QtWidgets.QVBoxLayout()
        model_group.setSpacing(2)
        model_label = QtWidgets.QLabel("Model selection:")
        model_label.setStyleSheet("color: #aaa; font-size: 11px;")
        model_group.addWidget(model_label)
        self.model_combo = DropDownComboBox()
        self.model_combo.addItem("Google VEO 3.1-Fast", "veo-3.1-fast-generate-preview")
        self.model_combo.addItem("Google VEO 3.1", "veo-3.1-generate-preview")
        self.model_combo.currentIndexChanged.connect(lambda _: self._save_all_state_to_node())
        model_group.addWidget(self.model_combo)
        config_row.addLayout(model_group, 2)

        # Aspect ratio
        ratio_group = QtWidgets.QVBoxLayout()
        ratio_group.setSpacing(2)
        ratio_label = QtWidgets.QLabel("Aspect ratio:")
        ratio_label.setStyleSheet("color: #aaa; font-size: 11px;")
        ratio_group.addWidget(ratio_label)
        self.ratio_combo = DropDownComboBox()
        self.ratio_combo.addItems(["16:9", "9:16"])
        self.ratio_combo.currentIndexChanged.connect(lambda _: self._save_all_state_to_node())
        ratio_group.addWidget(self.ratio_combo)
        config_row.addLayout(ratio_group, 1)

        # Resolution
        res_group = QtWidgets.QVBoxLayout()
        res_group.setSpacing(2)
        res_label = QtWidgets.QLabel("Resolution:")
        res_label.setStyleSheet("color: #aaa; font-size: 11px;")
        res_group.addWidget(res_label)
        self.res_combo = DropDownComboBox()
        self.res_combo.addItems(["720P", "1080P"])
        self.res_combo.currentIndexChanged.connect(lambda _: self._update_duration_for_mode())
        self.res_combo.currentIndexChanged.connect(lambda _: self._save_all_state_to_node())
        res_group.addWidget(self.res_combo)
        config_row.addLayout(res_group, 1)

        # Duration
        dur_group = QtWidgets.QVBoxLayout()
        dur_group.setSpacing(2)
        dur_label = QtWidgets.QLabel("Duration:")
        dur_label.setStyleSheet("color: #aaa; font-size: 11px;")
        dur_group.addWidget(dur_label)
        self.dur_combo = DropDownComboBox()
        self.dur_combo.addItem("4", "4")
        self.dur_combo.addItem("6", "6")
        self.dur_combo.addItem("8", "8")
        self.dur_combo.setCurrentIndex(2)  # default 8
        self.dur_combo.currentIndexChanged.connect(lambda _: self._save_all_state_to_node())
        dur_group.addWidget(self.dur_combo)
        config_row.addLayout(dur_group, 1)

        main.addLayout(config_row)

        # === Row 3: Mode (dropdown) ===
        mode_row = QtWidgets.QHBoxLayout()
        mode_row.setSpacing(10)

        mode_label = QtWidgets.QLabel("Mode:")
        mode_label.setStyleSheet("font-weight: bold;")
        mode_row.addWidget(mode_label)

        self.mode_combo = DropDownComboBox()
        self.mode_combo.addItem("Text", VEO_MODE_TEXT)
        self.mode_combo.addItem("FirstFrame", VEO_MODE_FIRST_FRAME)
        self.mode_combo.addItem("Frames", VEO_MODE_FRAMES)
        self.mode_combo.addItem("Ingredients", VEO_MODE_INGREDIENTS)
        self.mode_combo.setCurrentIndex(1)  # Default to FirstFrame
        self.mode_combo.currentIndexChanged.connect(self._on_mode_combo_changed)
        self.mode_combo.currentIndexChanged.connect(lambda _: self._save_all_state_to_node())
        mode_row.addWidget(self.mode_combo, 1)

        mode_row.addStretch()
        main.addLayout(mode_row)

        # === Row 4: History + Prompt mode ===
        row_hist = QtWidgets.QHBoxLayout()
        row_hist.setSpacing(6)

        self.history_combo = DropDownComboBox()
        self.history_combo.addItem("Select from History...")
        for h in self.settings.veo_prompt_history[:10]:
            display = h[:40] + "..." if len(h) > 40 else h
            self.history_combo.addItem(display, h)
        self.history_combo.currentIndexChanged.connect(self._on_history_select)
        row_hist.addWidget(self.history_combo, 3)

        hist_clear_btn = QtWidgets.QPushButton("x")
        hist_clear_btn.setObjectName("secondaryBtn")
        hist_clear_btn.setFixedWidth(22)
        hist_clear_btn.clicked.connect(self._clear_history)
        row_hist.addWidget(hist_clear_btn)

        pm_label = QtWidgets.QLabel("Prompt mode:")
        pm_label.setStyleSheet("color: #aaa; font-size: 11px;")
        row_hist.addWidget(pm_label)

        self.prompt_mode_combo = DropDownComboBox()
        self.prompt_mode_combo.addItems(["Enter", "Standard"])
        self.prompt_mode_combo.currentIndexChanged.connect(self._on_prompt_mode_changed)
        row_hist.addWidget(self.prompt_mode_combo)

        main.addLayout(row_hist)

        # === Prompt: Enter mode (single large QTextEdit) ===
        self.prompt_edit = QtWidgets.QTextEdit()
        self.prompt_edit.setPlaceholderText("Enter your creative prompt here...")
        self.prompt_edit.setMinimumHeight(120)
        self.prompt_edit.textChanged.connect(self._save_all_state_to_node)
        main.addWidget(self.prompt_edit)

        # === Prompt: Standard mode (structured fields) ===
        self._std_prompt_container = QtWidgets.QWidget()
        std_layout = QtWidgets.QFormLayout(self._std_prompt_container)
        std_layout.setSpacing(4)
        std_layout.setContentsMargins(0, 0, 0, 0)
        std_layout.setLabelAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)

        _STD_FIELDS = [
            ("Subject:",      "subject",
             "描述主要主体，例如：一只金毛幼犬",           False),
            ("Action:",       "action",
             "描述动作，例如：穿过一片野花田奔跑",          False),
            ("Style:",        "style",
             "描述视觉风格，例如：电影感、写实、动漫风格",    False),
            ("Camera:",       "camera",
             "（可选）相机位置和运动，例如：低角度跟拍镜头",   True),
            ("Composition:",  "composition",
             "（可选）构图方式，例如：三分法、居中构图",     True),
            ("Lens Effects:", "lens",
             "（可选）对焦和镜头效果，例如：浅景深、虚化光斑", True),
            ("Mood:",         "mood",
             "（可选）氛围和情绪，例如：温暖的金色时光光线",  True),
        ]
        self._std_field_keys = []        # ordered list of attr names
        self._std_field_widgets = {}     # key -> QLineEdit
        for label_text, key, placeholder, _optional in _STD_FIELDS:
            field = QtWidgets.QLineEdit()
            field.setPlaceholderText(placeholder)
            field.setStyleSheet(
                "background: #2a2a2a; border: 1px solid #444; border-radius: 3px;"
                " color: #ddd; padding: 4px 6px; font-size: 12px;"
            )
            field.textChanged.connect(self._save_all_state_to_node)
            lbl = QtWidgets.QLabel(label_text)
            lbl.setStyleSheet("color: #ccc; font-size: 12px; font-weight: bold;")
            std_layout.addRow(lbl, field)
            self._std_field_keys.append(key)
            self._std_field_widgets[key] = field
            setattr(self, "_std_{}".format(key), field)  # self._std_subject, etc.

        self._std_prompt_container.setVisible(False)  # hidden by default (Enter mode)
        main.addWidget(self._std_prompt_container)

        # === Negative Prompt (shared by both modes) ===
        self._neg_prompt_label = QtWidgets.QLabel("Negative Prompt:")
        self._neg_prompt_label.setStyleSheet(
            "color: #ccc; font-size: 12px; font-weight: bold;")
        main.addWidget(self._neg_prompt_label)

        self.neg_prompt_edit = QtWidgets.QTextEdit()
        self.neg_prompt_edit.setPlaceholderText("（可选）需要排除的内容，例如：模糊、变形、画质差")
        self.neg_prompt_edit.setMinimumHeight(80)
        self.neg_prompt_edit.textChanged.connect(self._save_all_state_to_node)
        main.addWidget(self.neg_prompt_edit)

        # === Generate Button (yellow like NanoBanana) ===
        self.gen_btn = QtWidgets.QPushButton("GENERATE VIDEO")
        self.gen_btn.setObjectName("generateBtn")
        self.gen_btn.setMinimumHeight(42)
        self.gen_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.gen_btn.clicked.connect(self._start_generate)
        main.addWidget(self.gen_btn)

        # === Progress Bar ===
        self.pbar = QtWidgets.QProgressBar()
        self.pbar.setVisible(False)
        self.pbar.setTextVisible(True)
        self.pbar.setFixedHeight(12)
        self.pbar.setRange(0, 100)
        main.addWidget(self.pbar)

        # === Status ===
        self.status_label = QtWidgets.QLabel("Ready")
        self.status_label.setAlignment(QtCore.Qt.AlignCenter)
        self.status_label.setStyleSheet("color: #666; font-size: 11px;")
        main.addWidget(self.status_label)

    # --- Prompt mode switching ------------------------------------------------

    def _on_prompt_mode_changed(self, index):
        """Toggle between Enter (free-text) and Standard (structured fields)."""
        is_standard = (index == 1)
        self.prompt_edit.setVisible(not is_standard)
        self._std_prompt_container.setVisible(is_standard)
        # Show/hide the "Negative Prompt:" label only in Standard mode
        self._neg_prompt_label.setVisible(is_standard)
        self._save_all_state_to_node()

    def _get_assembled_prompt(self):
        """Return the final prompt string based on the current prompt mode.

        - **Enter** mode: returns ``prompt_edit.toPlainText()`` as-is.
        - **Standard** mode: joins the non-empty structured fields with
          ``, `` (comma + space) in order: Subject, Action, Style, Camera,
          Composition, Lens Effects, Mood.
        """
        if self.prompt_mode_combo.currentIndex() == 0:
            # Enter mode
            return self.prompt_edit.toPlainText().strip()
        else:
            # Standard mode – join non-empty fields with comma
            parts = []
            for key in self._std_field_keys:
                val = self._std_field_widgets[key].text().strip()
                if val:
                    parts.append(val)
            return ", ".join(parts)

    # --- Mode switching ---
    def _on_mode_combo_changed(self, index):
        mode = self.mode_combo.currentData() or VEO_MODE_TEXT
        self._update_node_inputs(mode)
        self._update_duration_for_mode(mode)

    def _update_duration_for_mode(self, mode=None):
        """Update duration combo based on current mode + resolution.

        API constraints (Google Veo 3.1 official docs):
          - Frames (first+last frame) mode: always 8s only (any resolution)
          - 1080p / 4k resolution: always 8s only (any mode)
          - Otherwise (Text/FirstFrame/Ingredients @ 720p): 4s, 6s, 8s
        """
        if mode is None:
            mode = self.mode_combo.currentData() or VEO_MODE_TEXT
        resolution = self.res_combo.currentText().lower()

        # Determine if duration must be locked to 8s
        must_lock_8s = False
        if mode == VEO_MODE_FRAMES:
            must_lock_8s = True  # Frames mode always requires 8s
        if resolution in ("1080p", "4k"):
            must_lock_8s = True  # 1080p/4k always requires 8s

        if must_lock_8s:
            self.dur_combo.blockSignals(True)
            self.dur_combo.setCurrentIndex(2)   # index 2 = "8"
            self.dur_combo.blockSignals(False)
            self.dur_combo.setEnabled(False)
        else:
            self.dur_combo.setEnabled(True)

    def _get_current_mode(self):
        return self.mode_combo.currentData() or VEO_MODE_TEXT

    def _update_node_inputs(self, mode):
        """Dynamically update the VEO node's internal Input count using
        the NanoBanana pattern (delete all + rebuild in reverse order).

        Layout: FirstFrame/img1(LEFT) -> ... -> EndFrame/imgN(RIGHT)
        Mapping: names[K] = node.input(count - 1 - K)
        """
        node = self._get_owner_node()
        if not node:
            return

        needed = VEO_MODE_INPUT_COUNTS.get(mode, 0)

        _INPUT_NAMES = {
            VEO_MODE_TEXT: [],
            VEO_MODE_FIRST_FRAME: ["FirstFrame"],
            VEO_MODE_FRAMES: ["FirstFrame", "EndFrame"],
            VEO_MODE_INGREDIENTS: ["img1", "img2", "img3"],
        }
        names = _INPUT_NAMES.get(mode, [])

        # Count existing internal Input nodes
        node.begin()
        existing_inputs = [n for n in nuke.allNodes("Input")]
        current_count = len(existing_inputs)
        print("[VEO DEBUG] _update_node_inputs: mode={} needed={} current={}".format(
            mode, needed, current_count))

        if needed != current_count and needed > 0:
            # Save existing connections: old port i -> connected node?
            # Old mapping was: names[K] = input(current_count - 1 - K)
            saved = {}
            if current_count > 0:
                old_names = sorted(existing_inputs, key=lambda n: int(n["xpos"].value()))
                for k, inp_node in enumerate(old_names):
                    old_port = current_count - 1 - k
                    conn = node.input(old_port) if 0 <= old_port < current_count else None
                    if conn is not None:
                        # Store by logical name so we can remap to new ports
                        saved[inp_node.name()] = conn
                        print("[VEO DEBUG]   save: '{}' <- '{}'".
                              format(inp_node.name(), conn.name()))

            # Delete all existing Input nodes
            del_names = [n.name() for n in existing_inputs]
            for inp in list(nuke.allNodes("Input")):
                nuke.delete(inp)
            print("[VEO DEBUG]   delete: {}".format(del_names))

            # Recreate using reverse order + number knob (NanoBanana pattern)
            for i in range(needed, 0, -1):
                inp = nuke.nodes.Input()
                label = names[i - 1]
                inp.setName(label)
                inp["number"].setValue(needed - i)
                inp["xpos"].setValue((i - 1) * 200)
                inp["ypos"].setValue(0)
                print("[VEO DEBUG]   create: '{}' num={} #{}"
                      .format(label, needed - i, needed - i + 1))
            node.end()

            # Restore connections using new mapping: names[K] = input(needed - 1 - K)
            set_indices = set()
            for k, label in enumerate(names):
                if label in saved:
                    new_port = needed - 1 - k
                    group_ref = self._get_owner_node()
                    if group_ref:
                        group_ref.setInput(new_port, saved[label])
                        set_indices.add(new_port)
                        print("[VEO DEBUG]   restore: {} <- input {} ('{}')"
                              .format(label, new_port, saved[label].name()))

            # Clear auto-filled indices (Nuke setInput fills 0..N-1)
            for i in range(needed):
                if i not in set_indices:
                    group_ref = self._get_owner_node()
                    if group_ref:
                        group_ref.setInput(i, None)

            print("[VEO DEBUG]   AFTER ({} inputs):".format(needed))
            group_ref = self._get_owner_node()
            if group_ref:
                for idx in range(needed):
                    c = group_ref.input(idx)
                    print("[VEO DEBUG]     input({}) <- {}".format(
                        idx, c.name() if c else "None"))
            return

        elif needed == 0 and current_count > 0:
            # Text mode: remove all inputs
            for inp in list(nuke.allNodes("Input")):
                nuke.delete(inp)
            print("[VEO DEBUG]   removed all inputs (Text mode)")
            node.end()
            return

        node.end()

    # --- History ---
    def _on_history_select(self, index):
        if index <= 0:
            return
        full_text = self.history_combo.itemData(index)
        if not full_text:
            return
        self.prompt_edit.setText(full_text)
        # Move selected item to top of history
        self._add_to_history(full_text)
        # Reset combo to first item after selecting
        self.history_combo.blockSignals(True)
        self.history_combo.setCurrentIndex(0)
        self.history_combo.blockSignals(False)

    def _add_to_history(self, prompt):
        if not prompt:
            return
        history = self.settings.veo_prompt_history
        # Remove duplicate if exists (will be re-inserted at top)
        if prompt in history:
            history.remove(prompt)
        history.insert(0, prompt)
        if len(history) > 10:
            history = history[:10]
        self.settings.veo_prompt_history = history

        self._refresh_history_combo(history)

    def _clear_history(self):
        self.settings.veo_prompt_history = []
        self._refresh_history_combo([])

    def _refresh_history_combo(self, history):
        """Refresh the history combo box with truncated display text."""
        self.history_combo.blockSignals(True)
        self.history_combo.clear()
        self.history_combo.addItem("Select from History...")
        for h in history:
            display = h[:40] + "..." if len(h) > 40 else h
            self.history_combo.addItem(display, h)
        self.history_combo.blockSignals(False)

    def _get_owner_node(self):
        """Return the cached owner node, validating it still exists."""
        try:
            if self._node is not None and self._node.name():
                return self._node
        except Exception:
            pass
        # Fallback: try nuke.thisNode() (works during knob callbacks)
        try:
            return nuke.thisNode()
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Ensure hidden knobs on the owning node (one per parameter)
    # ------------------------------------------------------------------
    def _ensure_int_knob(self, node, name, label=""):
        if name not in node.knobs():
            k = nuke.Int_Knob(name, label)
            k.setVisible(False)
            node.addKnob(k)

    def _ensure_text_knob(self, node, name, label=""):
        if name not in node.knobs():
            k = nuke.Multiline_Eval_String_Knob(name, label)
            k.setVisible(False)
            node.addKnob(k)

    # ------------------------------------------------------------------
    # Save / Restore ALL UI state using individual hidden knobs
    # ------------------------------------------------------------------
    def _save_all_state_to_node(self):
        """Persist every user-visible widget value into hidden knobs."""
        node = self._node
        if node is None:
            return
        try:
            _ = node.name()
        except Exception:
            return
        try:
            self._ensure_int_knob(node, "veo_s_model", "s_model")
            self._ensure_int_knob(node, "veo_s_ratio", "s_ratio")
            self._ensure_int_knob(node, "veo_s_res", "s_res")
            self._ensure_int_knob(node, "veo_s_dur", "s_dur")
            self._ensure_int_knob(node, "veo_s_mode", "s_mode")
            self._ensure_int_knob(node, "veo_s_pm", "s_pm")
            self._ensure_text_knob(node, "veo_s_prompt", "s_prompt")
            self._ensure_text_knob(node, "veo_s_neg", "s_neg")

            # Standard prompt fields (7 fields stored as pipe-separated string)
            self._ensure_text_knob(node, "veo_s_stdfields", "s_stdfields")

            node["veo_s_model"].setValue(self.model_combo.currentIndex())
            node["veo_s_ratio"].setValue(self.ratio_combo.currentIndex())
            node["veo_s_res"].setValue(self.res_combo.currentIndex())
            node["veo_s_dur"].setValue(self.dur_combo.currentIndex())
            node["veo_s_mode"].setValue(self.mode_combo.currentIndex())
            node["veo_s_pm"].setValue(self.prompt_mode_combo.currentIndex())
            node["veo_s_prompt"].setValue(self.prompt_edit.toPlainText())
            node["veo_s_neg"].setValue(self.neg_prompt_edit.toPlainText())

            # Save Standard mode fields as pipe-delimited string
            std_vals = "|".join(
                self._std_field_widgets[k].text() for k in self._std_field_keys
            )
            node["veo_s_stdfields"].setValue(std_vals)
        except Exception as e:
            print("[VEO] _save_all_state_to_node error: {}".format(e))

    def _restore_all_state_from_node(self):
        """Restore every widget value from the hidden knobs."""
        node = self._node
        if node is None:
            return
        try:
            _ = node.name()
        except Exception:
            return
        try:
            if "veo_s_model" not in node.knobs() and "veo_s_mode" not in node.knobs():
                print("[VEO] No saved state found on node '{}'".format(node.name()))
                return

            print("[VEO] Restoring state from node '{}'".format(node.name()))

            widgets = [self.model_combo, self.ratio_combo, self.res_combo,
                       self.dur_combo, self.mode_combo, self.prompt_mode_combo,
                       self.prompt_edit, self.neg_prompt_edit]
            # Also block signals on all standard-mode fields
            std_widgets = list(self._std_field_widgets.values())
            for w in widgets + std_widgets:
                w.blockSignals(True)

            # Model
            if "veo_s_model" in node.knobs():
                idx = int(node["veo_s_model"].value())
                if 0 <= idx < self.model_combo.count():
                    self.model_combo.setCurrentIndex(idx)

            # Ratio
            if "veo_s_ratio" in node.knobs():
                idx = int(node["veo_s_ratio"].value())
                if 0 <= idx < self.ratio_combo.count():
                    self.ratio_combo.setCurrentIndex(idx)

            # Resolution
            if "veo_s_res" in node.knobs():
                idx = int(node["veo_s_res"].value())
                if 0 <= idx < self.res_combo.count():
                    self.res_combo.setCurrentIndex(idx)

            # Duration
            if "veo_s_dur" in node.knobs():
                idx = int(node["veo_s_dur"].value())
                if 0 <= idx < self.dur_combo.count():
                    self.dur_combo.setCurrentIndex(idx)

            # Mode
            if "veo_s_mode" in node.knobs():
                idx = int(node["veo_s_mode"].value())
                if 0 <= idx < self.mode_combo.count():
                    self.mode_combo.setCurrentIndex(idx)

            # Prompt mode
            if "veo_s_pm" in node.knobs():
                idx = int(node["veo_s_pm"].value())
                if 0 <= idx < self.prompt_mode_combo.count():
                    self.prompt_mode_combo.setCurrentIndex(idx)

            # Prompts
            if "veo_s_prompt" in node.knobs():
                prompt = node["veo_s_prompt"].value()
                if prompt:
                    self.prompt_edit.setText(prompt)
            if "veo_s_neg" in node.knobs():
                neg = node["veo_s_neg"].value()
                if neg:
                    self.neg_prompt_edit.setText(neg)

            # Standard mode fields (pipe-separated)
            if "veo_s_stdfields" in node.knobs():
                raw = node["veo_s_stdfields"].value()
                if raw:
                    vals = raw.split("|")
                    for i, key in enumerate(self._std_field_keys):
                        if i < len(vals):
                            self._std_field_widgets[key].setText(vals[i])

            for w in widgets + std_widgets:
                w.blockSignals(False)

            # After restoring mode, update the node inputs accordingly
            mode = self.mode_combo.currentData() or VEO_MODE_TEXT
            self._update_node_inputs(mode)
            self._update_duration_for_mode(mode)

            # Apply prompt mode visibility
            pm_idx = self.prompt_mode_combo.currentIndex()
            self._on_prompt_mode_changed(pm_idx)

            print("[VEO] State restored successfully")

        except Exception as e:
            print("[VEO] _restore_all_state_from_node error: {}".format(e))

    # ------------------------------------------------------------------
    # Lifecycle hooks – guarantee save before widget is destroyed / hidden
    # ------------------------------------------------------------------
    def hideEvent(self, event):
        self._save_all_state_to_node()
        super(VeoWidget, self).hideEvent(event)

    def closeEvent(self, event):
        self._save_all_state_to_node()
        super(VeoWidget, self).closeEvent(event)

    def event(self, ev):
        """Catch DeferredDelete (Nuke's PyCustom_Knob doesn't always fire
        hideEvent / closeEvent before destroying the widget)."""
        if ev.type() == QtCore.QEvent.DeferredDelete:
            self._save_all_state_to_node()
        return super(VeoWidget, self).event(ev)

    def _start_generate(self):
        if not self.settings.api_key:
            self.status_label.setStyleSheet("color: #ef4444; font-size: 11px;")
            self.status_label.setText("Please set API key in Settings")
            nuke.message("API key not set.\nPlease open AI Workflow > Setting in the toolbar.")
            return

        if self.current_worker and self.current_worker.is_running:
            worker = self.current_worker
            worker.stop()
            # Disconnect all signals to prevent callbacks into dead/stale objects
            try:
                worker.finished.disconnect()
                worker.error.disconnect()
                worker.status_update.disconnect()
                worker.progress_update.disconnect()
            except (RuntimeError, TypeError):
                pass
            # Cancel the status bar progress task
            if hasattr(self, '_status_task_id') and self._status_task_id:
                try:
                    from ai_workflow.status_bar import task_progress_manager
                    task_progress_manager.cancel_task(
                        self._status_task_id, "Cancelled by user")
                except Exception:
                    pass
                self._status_task_id = None
            # IMPORTANT: The QThread must stay alive (prevent GC) until its
            # OS thread exits, otherwise Qt crashes.  Keep it in the global
            # registry and poll with a QTimer until isRunning() returns False.
            worker_id = id(worker)
            _cleanup_timer = QtCore.QTimer()
            _cleanup_timer.setInterval(500)  # check every 500ms
            def _poll_thread_exit():
                if not worker.isRunning():
                    _cleanup_timer.stop()
                    _veo_active_workers.pop(worker_id, None)
            _cleanup_timer.timeout.connect(_poll_thread_exit)
            _cleanup_timer.start()
            # Store timer ref in registry to prevent it from being GC'd
            _veo_active_workers.setdefault(worker_id, {})["_cleanup_timer"] = _cleanup_timer
            self.current_worker = None
            # Reset UI
            self.pbar.setVisible(False)
            self.pbar.setValue(0)
            self.status_label.setStyleSheet("color: #facc15; font-size: 11px;")
            self.status_label.setText("Generation cancelled")
            self._toggle_stop_ui(False)
            return

        prompt = self._get_assembled_prompt()
        neg_prompt = self.neg_prompt_edit.toPlainText().strip()
        if not prompt:
            self.status_label.setText("Please enter a prompt")
            self.status_label.setStyleSheet("color: #ef4444; font-size: 11px;")
            return

        self._add_to_history(prompt)

        node = self._get_owner_node()
        if not node:
            self.status_label.setText("VEO_Generate node not found")
            return

        input_dir = get_input_directory()
        output_dir = get_output_directory()
        gen_name = node.name()
        current_mode = self._get_current_mode()

        # Collect reference images based on mode
        self.status_label.setStyleSheet("color: #facc15; font-size: 11px;")
        self.status_label.setText("Collecting inputs...")

        reference_image_paths = []
        input_count = VEO_MODE_INPUT_COUNTS.get(current_mode, 0)

        # NanoBanana mapping: names[K] = node.input(input_count - 1 - K)
        # names[0]=FirstFrame/img1 = leftmost = highest port index
        _INPUT_NAMES = {
            VEO_MODE_TEXT: [],
            VEO_MODE_FIRST_FRAME: ["FirstFrame"],
            VEO_MODE_FRAMES: ["FirstFrame", "EndFrame"],
            VEO_MODE_INGREDIENTS: ["img1", "img2", "img3"],
        }
        names = _INPUT_NAMES.get(current_mode, [])

        for k in range(input_count):
            port_idx = input_count - 1 - k
            label = names[k] if k < len(names) else "input{}".format(k + 1)
            inp_ref = node.input(port_idx)
            print("[VEO DEBUG] _on_generate: {} <- input({}) -> node '{}'".format(
                label, port_idx, inp_ref.name() if inp_ref else "None"))
            if inp_ref:
                frame_idx = k + 1
                path = os.path.join(input_dir, "{}_{}_frame{}.png".format(
                    gen_name, label.replace("/", "_"), frame_idx))
                if render_input_to_file_silent(inp_ref, path, nuke.frame()):
                    reference_image_paths.append(path)
                else:
                    nuke.message("Error: Failed to render {}.".format(label))
                    self.status_label.setText("Error: {} render failed".format(label))
                    self._toggle_stop_ui(False)
                    return

        print("[VEO DEBUG] reference_image_paths order: {}".format(reference_image_paths))

        model_name = self.model_combo.currentText()
        ratio = self.ratio_combo.currentText()
        duration = self.dur_combo.currentData() or self.dur_combo.currentText()
        resolution = self.res_combo.currentText().lower()
        current_mode = self.mode_combo.currentData() or VEO_MODE_TEXT

        # Enforce API constraints before generation
        if resolution in ("1080p", "4k"):
            duration = "8"
        elif current_mode == VEO_MODE_FRAMES:
            duration = "8"

        self._toggle_stop_ui(True)
        self.status_label.setText("Starting generation...")

        # Store params for record node creation
        gen_params = {
            "generator_node": node,
            "prompt": prompt,
            "negative_prompt": neg_prompt,
            "ratio": ratio,
            "duration": duration,
            "resolution": resolution,
            "model": model_name,
            "mode": current_mode,
            "reference_image_paths": reference_image_paths,
        }
        self._gen_params = gen_params

        worker = VeoWorker(
            api_key=self.settings.api_key,
            prompt=prompt,
            reference_image_paths=reference_image_paths,
            model=model_name,
            aspect_ratio=ratio,
            duration=duration,
            resolution=resolution,
            mode=current_mode,
            negative_prompt=neg_prompt,
            temp_dir=output_dir,
            gen_name=gen_name,
        )
        self.current_worker = worker

        # Register worker in module-level dict to prevent GC when widget dies
        worker_id = id(worker)
        _veo_active_workers[worker_id] = {"worker": worker, "params": gen_params}

        # Capture references for closures so they don't depend on self
        widget_ref = self

        # --- Register task in global status bar progress manager ---
        try:
            from ai_workflow.status_bar import task_progress_manager
            status_task_id = task_progress_manager.add_task(
                node.name() if node else "VEO", "video")
            self._status_task_id = status_task_id  # Save for stop/cancel access
            worker.status_update.connect(
                lambda s: task_progress_manager.update_status(status_task_id, s))
            worker.progress_update.connect(
                lambda v: task_progress_manager.update_status(status_task_id, progress=v))
        except Exception:
            status_task_id = None
            self._status_task_id = None

        def _on_finished(path, metadata):
            """Called when generation finishes. Works even if widget is destroyed."""
            # Update global status bar
            if status_task_id:
                try:
                    from ai_workflow.status_bar import task_progress_manager as _tpm
                    _tpm.complete_task(status_task_id, "Done! Video: {}".format(
                        os.path.basename(path) if path else ""))
                except Exception:
                    pass

            try:
                _alive = _isValid(widget_ref)
                if _alive:
                    widget_ref._toggle_stop_ui(False)
                    widget_ref.status_label.setStyleSheet("color: #3CB371; font-size: 11px;")
                    widget_ref.status_label.setText("Done! Video: {}".format(os.path.basename(path)))
            except Exception:
                pass

            if path and os.path.exists(path):
                params = gen_params

                def _create_nodes():
                    try:
                        viewer_node, internal_read = create_veo_viewer_node(
                            params["generator_node"],
                            params["prompt"],
                            params["ratio"],
                            params["duration"],
                            path,
                            reference_image_paths=params.get("reference_image_paths"),
                            model=params.get("model", "Google VEO 3.1-Fast"),
                            resolution=params.get("resolution", "720P"),
                            mode=params.get("mode", VEO_MODE_TEXT),
                            negative_prompt=params.get("negative_prompt", ""),
                        )
                        if viewer_node:
                            try:
                                nuke.connectViewer(0, viewer_node)
                            except:
                                pass
                    except Exception as e:
                        import traceback
                        print("VEO: ERROR in _create_nodes: {}".format(e))
                        traceback.print_exc()
                    finally:
                        _veo_active_workers.pop(worker_id, None)
                nuke.executeInMainThread(_create_nodes)
            else:
                _veo_active_workers.pop(worker_id, None)
                nuke.executeInMainThread(
                    nuke.message,
                    args=("Video generation completed but no file was created.\nPath: {}".format(path),)
                )

        def _on_error(err):
            """Called on generation error. Works even if widget is destroyed."""
            # Update global status bar
            if status_task_id:
                try:
                    from ai_workflow.status_bar import task_progress_manager as _tpm
                    _tpm.error_task(status_task_id, str(err)[:80])
                except Exception:
                    pass

            try:
                _alive = _isValid(widget_ref)
                if _alive:
                    widget_ref._toggle_stop_ui(False)
                    widget_ref.status_label.setStyleSheet("color: #ef4444; font-size: 11px;")
                    widget_ref.status_label.setText("Error")
            except Exception:
                pass
            _veo_active_workers.pop(worker_id, None)
            nuke.executeInMainThread(nuke.message, args=("VEO Error:\n{}".format(err),))

        worker.finished.connect(_on_finished)
        worker.error.connect(_on_error)
        worker.status_update.connect(
            lambda s: widget_ref.status_label.setText(s) if _isValid(widget_ref) else None)
        worker.progress_update.connect(
            lambda v: widget_ref.pbar.setValue(v) if _isValid(widget_ref) else None)
        worker.start()

    def _toggle_stop_ui(self, is_running):
        if is_running:
            self.gen_btn.setText("STOP")
            self.gen_btn.setObjectName("stopBtn")
            self.gen_btn.setStyleSheet("")  # clear inline style
            self.gen_btn.style().unpolish(self.gen_btn)
            self.gen_btn.style().polish(self.gen_btn)
            self.pbar.setValue(0)
            self.pbar.setVisible(True)
        else:
            self.gen_btn.setText("GENERATE VIDEO")
            self.gen_btn.setObjectName("generateBtn")
            self.gen_btn.setStyleSheet("")  # clear inline style
            self.gen_btn.style().unpolish(self.gen_btn)
            self.gen_btn.style().polish(self.gen_btn)
            self.current_worker = None
            self.pbar.setVisible(False)
            self.pbar.reset()



# ---------------------------------------------------------------------------
# Helpers to collect input image paths for VEO Viewer (mirrors NanoBanana)
# ---------------------------------------------------------------------------

def _find_veo_generator(viewer_node):
    """Find the generator name for this VEO Viewer node.

    Priority 1: veo_generator knob (fast path).
    Priority 2: Walk upstream to find a VEO_Generate node.
    """
    if not viewer_node:
        return ""

    # Fast path: stored generator name
    if "veo_generator" in viewer_node.knobs():
        stored = viewer_node["veo_generator"].value() or ""
        if stored:
            return stored

    # Slow path: walk upstream
    try:
        visited = set()
        queue = [viewer_node]
        while queue:
            cur = queue.pop(0)
            name = cur.name() if hasattr(cur, "name") else "?"
            if name in visited:
                continue
            visited.add(name)
            if name.startswith("veo") or name.startswith("VEO"):
                if "is_veo_viewer" not in cur.knobs():
                    return name
            max_inputs = getattr(cur, "inputs", lambda: 0)()
            for i in range(max_inputs):
                inp = cur.input(i)
                if inp:
                    queue.append(inp)
    except Exception as e:
        print("[VEO InputScan] Error walking upstream: {}".format(e))

    return ""


def _collect_veo_input_images_for_round(gen_name):
    """Find cached input images for a specific VEO generation round.

    Scans get_input_directory() for files matching:
      {GenName}_{Label}_frame{N}.png
    """
    paths = []
    try:
        input_dir = get_input_directory()
        if not os.path.isdir(input_dir):
            return paths

        prefix = "{}_".format(gen_name)
        extensions = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
        for fname in sorted(os.listdir(input_dir)):
            if fname.startswith(prefix):
                ext = os.path.splitext(fname)[1].lower()
                if ext in extensions:
                    fpath = os.path.join(input_dir, fname).replace("\\", "/")
                    paths.append(fpath)

        print("[VEO InputScan] Found {} image(s) for gen='{}' in input dir".format(
            len(paths), gen_name))
        for p in paths:
            exists = os.path.exists(p)
            print("  [VEO InputScan]   -> {} [{}]".format(p, "OK" if exists else "MISSING"))
    except Exception as e:
        print("[VEO InputScan] Error scanning input dir for '{}': {}".format(gen_name, e))

    return paths


def _collect_veo_input_image_paths(node):
    """Main entry point: collect reference images for this VEO Viewer node.

    Priority 1 (primary): Read veo_input_images JSON knob.
    Priority 2 (fallback): Scan input cache dir via generator name.

    Returns list of file path strings (same format as NanoBanana).
    """
    # Step 1: PRIMARY — try JSON knob first
    if node and "veo_input_images" in node.knobs():
        try:
            raw = node["veo_input_images"].value()
            print("[VEO InputScan] Step 1: knob exists, raw length={} chars".format(
                len(raw) if raw else 0))
            if raw and raw.strip():
                parsed = json.loads(raw)
                # Support both formats:
                # New: plain array ["path1", "path2"]
                # Old: {"reference_images": ["path1", "path2"]}
                if isinstance(parsed, list):
                    paths = [p for p in parsed if p]
                elif isinstance(parsed, dict):
                    paths = [p for p in parsed.get("reference_images", []) if p]
                else:
                    paths = []
                if paths:
                    found_count = sum(1 for p in paths if os.path.exists(p))
                    print("[VEO InputScan] Primary (JSON): {} image(s), {} on disk".format(
                        len(paths), found_count))
                    return paths
                else:
                    print("[VEO InputScan] Step 1: JSON parsed but empty list")
            else:
                print("[VEO InputScan] Step 1: knob value is blank/empty")
        except Exception as e:
            print("[VEO InputScan] JSON knob parse error: {}".format(e))
    else:
        print("[VEO InputScan] Step 1: knob not found on node")

    # Step 2: FALLBACK — scan input cache directory by generator name
    gen_name = _find_veo_generator(node)
    if gen_name:
        paths = _collect_veo_input_images_for_round(gen_name)
        if paths:
            return paths

    print("[VEO InputScan] No images found for '{}'".format(
        node.name() if node else "?"))
    return []


# ---------------------------------------------------------------------------
# VEO Record Widget (read-only record + editable regeneration UI)
# ---------------------------------------------------------------------------
class VeoRecordWidget(QtWidgets.QWidget):
    """Widget for VEO video record nodes.
    Top section: read-only record of the original generation parameters.
    Bottom section: editable parameters (same as VEO_Generate) for regeneration."""

    def __init__(self, node, parent=None):
        super(VeoRecordWidget, self).__init__(parent)
        self.node = node
        self.setObjectName("nanoBananaRoot")
        self.setStyleSheet(NANOBANANA_STYLE)
        self.setMinimumWidth(380)
        # Disable subpixel antialiasing to prevent coloured fringe on buttons
        font = self.font()
        font.setStyleStrategy(QtGui.QFont.NoSubpixelAntialias)
        self.setFont(font)
        self.settings = NanoBananaSettings()
        self.current_worker = None
        self._build_ui()
        self._load_from_node()

    def _build_ui(self):
        main = QtWidgets.QVBoxLayout(self)
        main.setSpacing(6)
        main.setContentsMargins(8, 8, 8, 8)

        # ================================================================
        # TOP SECTION: Read-only Generation Record
        # ================================================================
        record_frame = QtWidgets.QFrame()
        record_frame.setStyleSheet("QFrame { background: #1a1a1a; border: 1px solid #444; border-radius: 4px; }")
        record_layout = QtWidgets.QVBoxLayout(record_frame)
        record_layout.setSpacing(4)
        record_layout.setContentsMargins(8, 8, 8, 8)

        header = QtWidgets.QLabel("VEO Video Record (Read Only)")
        header.setStyleSheet("color: #4169E1; font-weight: bold; font-size: 12px; background: transparent;")
        header.setAlignment(QtCore.Qt.AlignCenter)
        record_layout.addWidget(header)

        info_style = "color: #ccc; font-size: 11px; background: transparent;"
        label_style = "color: #888; font-size: 10px; font-weight: bold; background: transparent;"
        value_style = "color: #ccc; font-size: 11px; background: transparent; padding: 0px;"

        # Horizontal layout: Model | Ratio | Resolution | Duration | Mode side by side
        info_row = QtWidgets.QHBoxLayout()
        info_row.setSpacing(8)

        self._info_labels = {}
        fields = [
            ("Model", "model"),
            ("Ratio", "ratio"),
            ("Resolution", "resolution"),
            ("Duration", "duration"),
            ("Mode", "mode"),
        ]
        for col_idx, (display_name, key) in enumerate(fields):
            col_widget = QtWidgets.QVBoxLayout()
            col_widget.setSpacing(1)
            lbl = QtWidgets.QLabel(display_name)
            lbl.setStyleSheet(label_style)
            lbl.setAlignment(QtCore.Qt.AlignCenter)
            val = QtWidgets.QLabel("")
            val.setStyleSheet(value_style)
            val.setAlignment(QtCore.Qt.AlignCenter)
            val.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            col_widget.addWidget(lbl)
            col_widget.addWidget(val)
            info_row.addLayout(col_widget)
            self._info_labels[key] = val
            # Add vertical separator between columns (except after last)
            if col_idx < len(fields) - 1:
                sep = QtWidgets.QFrame()
                sep.setFrameShape(QtWidgets.QFrame.VLine)
                sep.setStyleSheet("color: #444;")
                info_row.addWidget(sep)

        record_layout.addLayout(info_row)

        # Read-only prompt display
        prompt_label = QtWidgets.QLabel("Prompt:")
        prompt_label.setStyleSheet(label_style)
        record_layout.addWidget(prompt_label)
        self.prompt_display = QtWidgets.QPlainTextEdit()
        self.prompt_display.setReadOnly(True)
        self.prompt_display.setMaximumHeight(80)
        self.prompt_display.setStyleSheet("background: #2a2a2a; border: 1px solid #444; color: #ccc; font-size: 11px;")
        record_layout.addWidget(self.prompt_display)

        # Read-only negative prompt display
        neg_label = QtWidgets.QLabel("Negative Prompt:")
        neg_label.setStyleSheet(label_style)
        record_layout.addWidget(neg_label)
        self.neg_prompt_display = QtWidgets.QPlainTextEdit()
        self.neg_prompt_display.setReadOnly(True)
        self.neg_prompt_display.setMaximumHeight(50)
        self.neg_prompt_display.setStyleSheet("background: #2a2a2a; border: 1px solid #444; color: #ccc; font-size: 11px;")
        record_layout.addWidget(self.neg_prompt_display)

        # Hidden cached info (kept for data but not displayed)
        self.cached_info_label = QtWidgets.QLabel("")
        self.cached_info_label.setVisible(False)
        record_layout.addWidget(self.cached_info_label)

        # Hidden Read Node reference (kept for data but not displayed)
        self.read_node_label = QtWidgets.QLabel("")
        self.read_node_label.setVisible(False)
        record_layout.addWidget(self.read_node_label)

        main.addWidget(record_frame)

        # ================================================================
        # Divider
        # ================================================================
        divider_line = QtWidgets.QFrame()
        divider_line.setFrameShape(QtWidgets.QFrame.HLine)
        divider_line.setStyleSheet("color: #555;")
        main.addWidget(divider_line)

        regen_header = QtWidgets.QLabel("🔄 Regenerate (edit params below)")
        regen_header.setStyleSheet("color: #facc15; font-weight: bold; font-size: 12px;")
        regen_header.setAlignment(QtCore.Qt.AlignCenter)
        main.addWidget(regen_header)

        # ================================================================
        # BOTTOM SECTION: Editable parameters for regeneration
        # ================================================================

        # === Row 1: Model / Aspect ratio / Resolution / Duration ===
        config_row = QtWidgets.QHBoxLayout()
        config_row.setSpacing(12)

        # Model selection
        model_group = QtWidgets.QVBoxLayout()
        model_group.setSpacing(2)
        model_lbl = QtWidgets.QLabel("Model selection:")
        model_lbl.setStyleSheet("color: #aaa; font-size: 11px;")
        model_group.addWidget(model_lbl)
        self.model_combo = DropDownComboBox()
        self.model_combo.addItem("Google VEO 3.1-Fast", "veo-3.1-fast-generate-preview")
        self.model_combo.addItem("Google VEO 3.1", "veo-3.1-generate-preview")
        model_group.addWidget(self.model_combo)
        config_row.addLayout(model_group, 2)

        # Aspect ratio
        ratio_group = QtWidgets.QVBoxLayout()
        ratio_group.setSpacing(2)
        ratio_lbl = QtWidgets.QLabel("Aspect ratio:")
        ratio_lbl.setStyleSheet("color: #aaa; font-size: 11px;")
        ratio_group.addWidget(ratio_lbl)
        self.ratio_combo = DropDownComboBox()
        self.ratio_combo.addItems(["16:9", "9:16"])
        ratio_group.addWidget(self.ratio_combo)
        config_row.addLayout(ratio_group, 1)

        # Resolution
        res_group = QtWidgets.QVBoxLayout()
        res_group.setSpacing(2)
        res_lbl = QtWidgets.QLabel("Resolution:")
        res_lbl.setStyleSheet("color: #aaa; font-size: 11px;")
        res_group.addWidget(res_lbl)
        self.res_combo = DropDownComboBox()
        self.res_combo.addItems(["720P", "1080P"])
        self.res_combo.currentIndexChanged.connect(lambda _: self._update_duration_constraints())
        res_group.addWidget(self.res_combo)
        config_row.addLayout(res_group, 1)

        # Duration
        dur_group = QtWidgets.QVBoxLayout()
        dur_group.setSpacing(2)
        dur_lbl = QtWidgets.QLabel("Duration:")
        dur_lbl.setStyleSheet("color: #aaa; font-size: 11px;")
        dur_group.addWidget(dur_lbl)
        self.dur_combo = DropDownComboBox()
        self.dur_combo.addItem("4", "4")
        self.dur_combo.addItem("6", "6")
        self.dur_combo.addItem("8", "8")
        self.dur_combo.setCurrentIndex(2)  # default 8
        dur_group.addWidget(self.dur_combo)
        config_row.addLayout(dur_group, 1)

        main.addLayout(config_row)

        # === Mode ===
        mode_row = QtWidgets.QHBoxLayout()
        mode_row.setSpacing(10)
        mode_label = QtWidgets.QLabel("Mode:")
        mode_label.setStyleSheet("font-weight: bold;")
        mode_row.addWidget(mode_label)
        self.mode_combo = DropDownComboBox()
        self.mode_combo.addItem("Text", VEO_MODE_TEXT)
        self.mode_combo.addItem("FirstFrame", VEO_MODE_FIRST_FRAME)
        self.mode_combo.addItem("Frames", VEO_MODE_FRAMES)
        self.mode_combo.addItem("Ingredients", VEO_MODE_INGREDIENTS)
        self.mode_combo.setCurrentIndex(1)  # Default to FirstFrame
        self.mode_combo.currentIndexChanged.connect(lambda _: self._update_duration_constraints())
        mode_row.addWidget(self.mode_combo, 1)
        mode_row.addStretch()
        main.addLayout(mode_row)

        # === Editable Prompt ===
        self.prompt_edit = QtWidgets.QTextEdit()
        self.prompt_edit.setPlaceholderText("Edit prompt and regenerate...")
        self.prompt_edit.setMinimumHeight(120)
        main.addWidget(self.prompt_edit)

        # === Editable Negative Prompt ===
        self.neg_prompt_edit = QtWidgets.QTextEdit()
        self.neg_prompt_edit.setPlaceholderText("Negative prompt (optional)...")
        self.neg_prompt_edit.setFixedHeight(70)
        main.addWidget(self.neg_prompt_edit)

        # === Image Reference Strip (add / remove reference images) ===
        from ai_workflow.gemini_chat import ImageStrip
        self._ref_image_strip = ImageStrip(add_callback=self._add_ref_image)
        self._ref_image_strip.imagesChanged.connect(self._save_ref_images_to_node)
        main.addWidget(self._ref_image_strip)

        # === Regenerate Button ===
        self.regen_btn = QtWidgets.QPushButton("REGENERATE VIDEO")
        self.regen_btn.setObjectName("regenerateBtn")
        self.regen_btn.setMinimumHeight(42)
        self.regen_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.regen_btn.clicked.connect(self._regenerate)
        main.addWidget(self.regen_btn)

        # === Progress Bar ===
        self.pbar = QtWidgets.QProgressBar()
        self.pbar.setVisible(False)
        self.pbar.setTextVisible(True)
        self.pbar.setFixedHeight(12)
        self.pbar.setRange(0, 100)
        main.addWidget(self.pbar)

        # === Status ===
        self.status_label = QtWidgets.QLabel("Ready")
        self.status_label.setAlignment(QtCore.Qt.AlignCenter)
        self.status_label.setStyleSheet("color: #666; font-size: 11px;")
        main.addWidget(self.status_label)

    def _load_from_node(self):
        """Load settings from node knobs.
        - Populates read-only record section.
        - Pre-fills editable section with original values."""
        if not self.node:
            return
        try:
            # --- Read-only record section ---
            if "veo_model" in self.node.knobs():
                self._info_labels["model"].setText(self.node["veo_model"].value())
            if "veo_ratio" in self.node.knobs():
                self._info_labels["ratio"].setText(self.node["veo_ratio"].value())
            if "veo_resolution" in self.node.knobs():
                self._info_labels["resolution"].setText(self.node["veo_resolution"].value())
            if "veo_duration" in self.node.knobs():
                self._info_labels["duration"].setText(self.node["veo_duration"].value())
            if "veo_mode" in self.node.knobs():
                self._info_labels["mode"].setText(self.node["veo_mode"].value())

            if "veo_prompt" in self.node.knobs():
                self.prompt_display.setPlainText(self.node["veo_prompt"].value())
            if "veo_neg_prompt" in self.node.knobs():
                self.neg_prompt_display.setPlainText(self.node["veo_neg_prompt"].value())

            # Cached images info (using unified collection function)
            try:
                all_ref_paths = _collect_veo_input_image_paths(self.node)
                if all_ref_paths:
                    found = sum(1 for p in all_ref_paths if os.path.exists(p))
                    self.cached_info_label.setText(
                        "{} image(s) ({} available)".format(len(all_ref_paths), found))
                else:
                    self.cached_info_label.setText("Text-only generation")
            except Exception:
                self.cached_info_label.setText("")

            # Read node reference
            if "veo_read_node" in self.node.knobs():
                self.read_node_label.setText(self.node["veo_read_node"].value())

            # --- Pre-fill editable section with original values ---
            # Model
            if "veo_model" in self.node.knobs():
                model_val = self.node["veo_model"].value()
                model_map = {
                    "veo-3.1-fast-generate-preview": 0,
                    "veo-3.1-generate-preview": 1,
                    "Google VEO 3.1-Fast": 0,
                    "Google VEO 3.1": 1,
                }
                idx = model_map.get(model_val, 0)
                if 0 <= idx < self.model_combo.count():
                    self.model_combo.setCurrentIndex(idx)

            # Aspect ratio
            if "veo_ratio" in self.node.knobs():
                ratio_val = self.node["veo_ratio"].value()
                ratio_idx = self.ratio_combo.findText(ratio_val)
                if ratio_idx >= 0:
                    self.ratio_combo.setCurrentIndex(ratio_idx)

            # Resolution
            if "veo_resolution" in self.node.knobs():
                res_val = self.node["veo_resolution"].value()
                res_idx = self.res_combo.findText(res_val)
                if res_idx >= 0:
                    self.res_combo.setCurrentIndex(res_idx)

            # Duration
            if "veo_duration" in self.node.knobs():
                dur_val = self.node["veo_duration"].value()
                dur_map = {"4": 0, "6": 1, "8": 2}
                dur_idx = dur_map.get(dur_val, 2)
                if 0 <= dur_idx < self.dur_combo.count():
                    self.dur_combo.setCurrentIndex(dur_idx)

            # Mode
            if "veo_mode" in self.node.knobs():
                mode_val = self.node["veo_mode"].value()
                mode_map = {
                    VEO_MODE_TEXT: 0,
                    VEO_MODE_FIRST_FRAME: 1,
                    VEO_MODE_FRAMES: 2,
                    VEO_MODE_INGREDIENTS: 3,
                }
                mode_idx = mode_map.get(mode_val, 0)
                if 0 <= mode_idx < self.mode_combo.count():
                    self.mode_combo.setCurrentIndex(mode_idx)

            # Prompts (editable - pre-fill from record)
            if "veo_prompt" in self.node.knobs():
                self.prompt_edit.setText(self.node["veo_prompt"].value())
            if "veo_neg_prompt" in self.node.knobs():
                self.neg_prompt_edit.setText(self.node["veo_neg_prompt"].value())

            # Apply duration constraints based on loaded mode + resolution
            self._update_duration_constraints()

            # Load reference images into the ImageStrip (mirrors NanoBanana)
            try:
                print("[VEO Regen] >>> Loading input images from knob...")
                all_paths = _collect_veo_input_image_paths(self.node)
                found_count = sum(1 for p in all_paths if os.path.exists(p))

                print("[VEO Regen]     TOTAL {} paths ({} found on disk)".format(
                    len(all_paths), found_count))
                for vp in all_paths:
                    print("  [VEO Regen]       -> {} [{}]".format(
                        vp, "OK" if os.path.exists(vp) else "MISSING"))

                if all_paths:
                    for p in all_paths:
                        self._ref_image_strip.add_image(p)
                    print("[VEO Regen]     DONE - added {} images to strip".format(
                        len(all_paths)))
                else:
                    self._ref_image_strip.clear_images()
                    print("[VEO Regen]     No images stored")
            except Exception as ex:
                print("[VEO Regen]     ERROR loading images: {}".format(ex))
                import traceback
                traceback.print_exc()
                self._ref_image_strip.clear_images()

        except Exception as e:
            print("VEO: Error loading record settings: {}".format(e))

    # ----- Image reference strip callbacks -----

    def _add_ref_image(self):
        """Called when the '+' button on the ImageStrip is clicked."""
        fpath, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Add Reference Image", "",
            "Images (*.png *.jpg *.jpeg *.gif *.webp);;All Files (*)"
        )
        if fpath and os.path.isfile(fpath):
            self._ref_image_strip.add_image(fpath)
            self._save_ref_images_to_node()

    def _save_ref_images_to_node(self):
        """Persist the current ImageStrip paths back to the node's veo_input_images knob."""
        print("[VEO Regen] >>> _save_ref_images_to_node: strip has {} images".format(
            len(self._ref_image_strip.images) if hasattr(self, '_ref_image_strip') else 0))
        if not self.node or "veo_input_images" not in self.node.knobs():
            print("[VEO Regen]     SKIPPED - no node or no knob")
            return
        paths = self._ref_image_strip.images if hasattr(self, '_ref_image_strip') else []
        for i, p in enumerate(paths):
            print("  [VEO Regen]   images[{}]: '{}'".format(i, p))
        json_val = json.dumps(paths)
        self.node["veo_input_images"].setValue(json_val)

    def _update_duration_constraints(self):
        """Update duration combo based on current mode + resolution.

        API constraints (Google Veo 3.1 official docs):
          - Frames (first+last frame) mode: always 8s only (any resolution)
          - 1080p / 4k resolution: always 8s only (any mode)
          - Otherwise (Text/FirstFrame/Ingredients @ 720p): 4s, 6s, 8s
        """
        mode = self.mode_combo.currentData() or VEO_MODE_TEXT
        resolution = self.res_combo.currentText().lower()

        must_lock_8s = False
        if mode == VEO_MODE_FRAMES:
            must_lock_8s = True
        if resolution in ("1080p", "4k"):
            must_lock_8s = True

        if must_lock_8s:
            self.dur_combo.blockSignals(True)
            self.dur_combo.setCurrentIndex(2)   # index 2 = "8"
            self.dur_combo.blockSignals(False)
            self.dur_combo.setEnabled(False)
        else:
            self.dur_combo.setEnabled(True)

    def _regenerate(self):
        """Regenerate video using editable parameters and cached reference images."""
        if not self.settings.api_key:
            self.status_label.setStyleSheet("color: #ef4444; font-size: 11px;")
            self.status_label.setText("Please set API key in Settings")
            nuke.message("API key not set.\nPlease open AI Workflow > Setting in the toolbar.")
            return

        if self.current_worker and self.current_worker.is_running:
            worker = self.current_worker
            worker.stop()
            # Disconnect all signals to prevent callbacks into dead/stale objects
            try:
                worker.finished.disconnect()
                worker.error.disconnect()
                worker.status_update.disconnect()
                worker.progress_update.disconnect()
            except (RuntimeError, TypeError):
                pass
            # Cancel the status bar progress task
            if hasattr(self, '_status_task_id') and self._status_task_id:
                try:
                    from ai_workflow.status_bar import task_progress_manager
                    task_progress_manager.cancel_task(
                        self._status_task_id, "Cancelled by user")
                except Exception:
                    pass
                self._status_task_id = None
            # IMPORTANT: The QThread must stay alive (prevent GC) until its
            # OS thread exits, otherwise Qt crashes.  Keep it in the global
            # registry and poll with a QTimer until isRunning() returns False.
            worker_id = id(worker)
            _cleanup_timer = QtCore.QTimer()
            _cleanup_timer.setInterval(500)
            def _poll_thread_exit():
                if not worker.isRunning():
                    _cleanup_timer.stop()
                    _veo_active_workers.pop(worker_id, None)
            _cleanup_timer.timeout.connect(_poll_thread_exit)
            _cleanup_timer.start()
            _veo_active_workers.setdefault(worker_id, {})["_cleanup_timer"] = _cleanup_timer
            self.current_worker = None
            # Reset UI
            self.pbar.setVisible(False)
            self.pbar.setValue(0)
            self.status_label.setStyleSheet("color: #facc15; font-size: 11px;")
            self.status_label.setText("Generation cancelled")
            self._toggle_ui(False)
            return

        prompt = self.prompt_edit.toPlainText().strip()
        neg_prompt = self.neg_prompt_edit.toPlainText().strip()
        if not prompt:
            self.status_label.setText("Please enter a prompt")
            self.status_label.setStyleSheet("color: #ef4444; font-size: 11px;")
            return

        model_name = self.model_combo.currentText()
        ratio = self.ratio_combo.currentText()
        duration = self.dur_combo.currentData() or self.dur_combo.currentText()
        resolution = self.res_combo.currentText().lower()
        current_mode = self.mode_combo.currentData() or VEO_MODE_TEXT

        # Enforce API constraints before generation
        if resolution in ("1080p", "4k"):
            duration = "8"
        elif current_mode == VEO_MODE_FRAMES:
            duration = "8"

        # Collect reference images from the ImageStrip (UI source of truth)
        reference_image_paths = []
        if hasattr(self, '_ref_image_strip'):
            reference_image_paths = [p for p in self._ref_image_strip.images
                                     if p and os.path.exists(p)]
            # Sync strip state back to the node knob
            self._save_ref_images_to_node()
        else:
            # Fallback: use unified collection function
            reference_image_paths = [p for p in _collect_veo_input_image_paths(self.node)
                                     if p and os.path.exists(p)]

        # Find generator name
        gen_name = "VEO_Generate"
        if "veo_generator" in self.node.knobs():
            gen_name = self.node["veo_generator"].value() or gen_name

        output_dir = get_output_directory()

        self._toggle_ui(True)
        self.status_label.setStyleSheet("color: #facc15; font-size: 11px;")
        self.status_label.setText("Starting regeneration...")

        worker = VeoWorker(
            api_key=self.settings.api_key,
            prompt=prompt,
            reference_image_paths=reference_image_paths,
            model=model_name,
            aspect_ratio=ratio,
            duration=duration,
            resolution=resolution,
            mode=current_mode,
            negative_prompt=neg_prompt,
            temp_dir=output_dir,
            gen_name=gen_name,
        )
        self.current_worker = worker

        worker_id = id(worker)
        _veo_active_workers[worker_id] = {"worker": worker, "params": {}}

        widget_ref = self
        node_ref = self.node

        # --- Register task in global status bar progress manager ---
        try:
            from ai_workflow.status_bar import task_progress_manager
            status_task_id = task_progress_manager.add_task(
                node_ref.name() if node_ref else "VEO Regen", "video")
            self._status_task_id = status_task_id  # Save for stop/cancel access
            worker.status_update.connect(
                lambda s: task_progress_manager.update_status(status_task_id, s))
            worker.progress_update.connect(
                lambda v: task_progress_manager.update_status(status_task_id, progress=v))
        except Exception:
            status_task_id = None
            self._status_task_id = None

        def _on_finished(path, metadata):
            # Update global status bar
            if status_task_id:
                try:
                    from ai_workflow.status_bar import task_progress_manager as _tpm
                    _tpm.complete_task(status_task_id, "Done! Video: {}".format(
                        os.path.basename(path) if path else ""))
                except Exception:
                    pass

            try:
                _alive = _isValid(widget_ref)
                if _alive:
                    widget_ref._toggle_ui(False)
                    widget_ref.status_label.setStyleSheet("color: #3CB371; font-size: 11px;")
                    widget_ref.status_label.setText("Done! Video: {}".format(os.path.basename(path)))
            except Exception:
                pass

            if path and os.path.exists(path):
                def _update():
                    try:
                        cur_node = node_ref
                        updated = update_veo_viewer_read(cur_node, path)
                        if updated:
                            cur_node = updated
                            # --- Replacement Jutsu: rebuild Group for fresh thumbnail ---
                            rebuilt = _rebuild_veo_group_for_thumbnail(cur_node, path)
                            if rebuilt:
                                cur_node = rebuilt
                                # Update widget reference so future operations target the new node
                                try:
                                    if _isValid(widget_ref):
                                        widget_ref.node = rebuilt
                                except Exception:
                                    pass
                            else:
                                # Fallback: legacy thumbnail update
                                _update_veo_thumbnail(cur_node, path)
                            try:
                                nuke.connectViewer(0, cur_node)
                            except:
                                pass
                        else:
                            print("VEO: WARNING update_veo_viewer_read returned None for '{}'".format(
                                node_ref.name() if node_ref else "?"))
                    except Exception as e:
                        import traceback
                        print("VEO: ERROR updating VEO Viewer Read: {}".format(e))
                        traceback.print_exc()
                    finally:
                        _veo_active_workers.pop(worker_id, None)
                nuke.executeInMainThread(_update)
            else:
                _veo_active_workers.pop(worker_id, None)

        def _on_error(err):
            # Update global status bar
            if status_task_id:
                try:
                    from ai_workflow.status_bar import task_progress_manager as _tpm
                    _tpm.error_task(status_task_id, str(err)[:80])
                except Exception:
                    pass

            try:
                _alive = _isValid(widget_ref)
                if _alive:
                    widget_ref._toggle_ui(False)
                    widget_ref.status_label.setStyleSheet("color: #ef4444; font-size: 11px;")
                    widget_ref.status_label.setText("Error")
            except Exception:
                pass
            _veo_active_workers.pop(worker_id, None)
            nuke.executeInMainThread(nuke.message, args=("VEO Regeneration Error:\n{}".format(err),))

        worker.finished.connect(_on_finished)
        worker.error.connect(_on_error)
        worker.status_update.connect(
            lambda s: widget_ref.status_label.setText(s) if _isValid(widget_ref) else None)
        worker.progress_update.connect(
            lambda v: widget_ref.pbar.setValue(v) if _isValid(widget_ref) else None)
        worker.start()

    def _toggle_ui(self, is_running):
        if is_running:
            self.regen_btn.setText("STOP")
            self.regen_btn.setObjectName("stopBtn")
            self.regen_btn.setStyleSheet("")
            self.regen_btn.style().unpolish(self.regen_btn)
            self.regen_btn.style().polish(self.regen_btn)
            self.pbar.setValue(0)
            self.pbar.setVisible(True)
        else:
            self.regen_btn.setText("REGENERATE VIDEO")
            self.regen_btn.setObjectName("regenerateBtn")
            self.regen_btn.setStyleSheet("")
            self.regen_btn.style().unpolish(self.regen_btn)
            self.regen_btn.style().polish(self.regen_btn)
            self.current_worker = None
            self.pbar.setVisible(False)
            self.pbar.reset()


# ---------------------------------------------------------------------------
# Node Creation Functions
# ---------------------------------------------------------------------------
def _next_veo_name():
    """Return the next available name like 'veo1', 'veo2', etc."""
    used = set()
    for node in nuke.allNodes():
        n = node.name()
        if n == "veo":
            used.add(1)
        else:
            m = re.match(r"^veo(\d+)$", n, re.IGNORECASE)
            if m:
                used.add(int(m.group(1)))
    i = 1
    while i in used:
        i += 1
    return "veo{}".format(i)


def _create_veo_group_inputs(group_node, input_names):
    """Create Input nodes inside a VEO Group using NanoBanana's proven pattern.

    Pattern: REVERSE creation order + 'number' knob
    - Nuke: first-created Input = RIGHTmost on the DAG input strip.
    - We CREATE in REVERSE: rightmost name first -> leftmost last.
    - 'number' knob: higher value = more LEFT. Leftmost gets highest number.
    - Connection mapping: names[K] = node.input(count - 1 - K)

    Examples:
      Frames mode ["FirstFrame", "EndFrame"] (count=2):
        Created 1st: EndFrame   number=0  input(0)  RIGHT
        Created 2nd: FirstFrame number=1  input(1)  LEFT

      Ingredients mode ["img1", "img2", "img3"] (count=3):
        Created 1st: img3  number=0  input(0)  RIGHT
        Created 2nd: img2  number=1  input(1)
        Created 3rd: img1  number=2  input(2)  LEFT
    """
    count = len(input_names)
    group_node.begin()
    for i in range(count, 0, -1):
        inp = nuke.nodes.Input()
        label = input_names[i - 1]
        inp.setName(label)
        # number knob: leftmost = highest number
        inp["number"].setValue(count - i)
        # xpos: leftmost at 0, rightmost at (count-1)*200
        inp["xpos"].setValue((i - 1) * 200)
        inp["ypos"].setValue(0)
        print("[VEO DEBUG] _create_veo_inputs: '{}' num={} created_#{}"
              .format(inp.name(), count - i, count - i + 1))

    out = nuke.nodes.Output()
    out["xpos"].setValue(0)
    out["ypos"].setValue(200)
    group_node.end()

    # Debug verify
    for idx in range(count):
        print("[VEO DEBUG] _create_veo_inputs: verify input({}) -> {}"
              .format(idx, group_node.input(idx).name() if group_node.input(idx) else "None"))


def create_veo_node():
    """
    Create a VEO node.
    Auto-detect mode based on the number of selected nodes (any type, not just Read):
      - 0 nodes selected -> FirstFrame mode (1 input, default)
      - 1 node selected  -> FirstFrame mode (1 input)
      - 2 nodes selected -> Frames mode (2 inputs: first + last frame)
      - 3+ nodes selected -> Ingredients mode (3 inputs)

    nuke.selectedNodes() returns nodes in reverse selection order (last selected first),
    so we reverse the list to get the natural selection order:
      first selected = FirstFrame, second selected = EndFrame, etc.
    """
    # nuke.selectedNodes() returns in reverse selection order, so reverse it
    sel = list(reversed(nuke.selectedNodes()))
    input_nodes = sel  # use all selected nodes, not just Read

    # Determine mode based on selected node count
    node_count = len(input_nodes)
    if node_count == 0:
        auto_mode = VEO_MODE_FIRST_FRAME  # Default to FirstFrame
    elif node_count == 1:
        auto_mode = VEO_MODE_FIRST_FRAME
    elif node_count == 2:
        auto_mode = VEO_MODE_FRAMES
    else:
        auto_mode = VEO_MODE_INGREDIENTS
        input_nodes = input_nodes[:3]  # max 3 inputs

    # Map mode to mode combo index for saving state
    _MODE_TO_INDEX = {
        VEO_MODE_TEXT: 0,
        VEO_MODE_FIRST_FRAME: 1,
        VEO_MODE_FRAMES: 2,
        VEO_MODE_INGREDIENTS: 3,
    }

    # Input names per mode
    _INPUT_NAMES = {
        VEO_MODE_TEXT: [],
        VEO_MODE_FIRST_FRAME: ["FirstFrame"],
        VEO_MODE_FRAMES: ["FirstFrame", "EndFrame"],
        VEO_MODE_INGREDIENTS: ["img1", "img2", "img3"],
    }

    needed_inputs = VEO_MODE_INPUT_COUNTS.get(auto_mode, 0)
    input_names = _INPUT_NAMES.get(auto_mode, [])

    # Position: below first selected node, or center of DAG
    ref_node = input_nodes[0] if input_nodes else None

    # Create the main VEO Group node
    group_node = nuke.nodes.Group()
    group_node.setName(_next_veo_name())
    group_node["tile_color"].setValue(0x4169E1FF)  # Royal Blue

    if ref_node:
        sx = int(ref_node["xpos"].value())
        sy = int(ref_node["ypos"].value())
        group_node["xpos"].setValue(sx)
        group_node["ypos"].setValue(sy + 100)
    else:
        try:
            center = nuke.center()
            x, y = int(center[0]), int(center[1])
        except Exception:
            x, y = 0, 0
        group_node["xpos"].setValue(x)
        group_node["ypos"].setValue(y)

    # Build internal structure using NanoBanana pattern (reverse creation + number knob)
    _create_veo_group_inputs(group_node, input_names[:needed_inputs])

    # Connect selected nodes using NanoBanana mapping:
    #   input_names[K] = node.input(needed_inputs - 1 - K)
    #   input_nodes[0] -> leftmost port (FirstFrame / img1)
    for k, src_node in enumerate(input_nodes[:needed_inputs]):
        port_idx = needed_inputs - 1 - k
        print("[VEO DEBUG] setInput(port={}, node='{}') for '{}'"
              .format(port_idx, src_node.name(), input_names[k]))
        group_node.setInput(port_idx, src_node)

    # Add custom VEO tab
    tab = nuke.Tab_Knob("veo_tab", "VEO")
    group_node.addKnob(tab)

    custom_knob = nuke.PyCustom_Knob(
        "veo_ui", "",
        "ai_workflow.veo.VeoKnobWidget()"
    )
    custom_knob.setFlag(nuke.STARTLINE)
    group_node.addKnob(custom_knob)

    # Pre-save mode state so the UI widget restores the correct mode
    mode_idx = _MODE_TO_INDEX.get(auto_mode, 0)
    mode_knob = nuke.Int_Knob("veo_s_mode", "s_mode")
    mode_knob.setVisible(False)
    group_node.addKnob(mode_knob)
    group_node["veo_s_mode"].setValue(mode_idx)

    # Log mode selection
    mode_display = {
        VEO_MODE_TEXT: "Text",
        VEO_MODE_FIRST_FRAME: "FirstFrame",
        VEO_MODE_FRAMES: "Frames",
        VEO_MODE_INGREDIENTS: "Ingredients",
    }
    print("VEO: Created '{}' with auto-detected mode: {} (selected {} nodes)".format(
        group_node.name(), mode_display.get(auto_mode, auto_mode), node_count))

    return group_node


# ---------------------------------------------------------------------------
# Knob Widget Wrappers (for PyCustom_Knob)
# ---------------------------------------------------------------------------
class VeoKnobWidget(QtWidgets.QWidget):
    """Wrapper for VEO_Generate node."""

    def __init__(self):
        super(VeoKnobWidget, self).__init__()
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        # Capture the node at construction time (PyCustom_Knob evaluates
        # its expression in the context of the owning node).
        try:
            node = nuke.thisNode()
        except Exception:
            node = None
        print("[VEO] KnobWidget __init__: node = {}".format(
            node.name() if node else "None"))
        self.panel = VeoWidget(node=node, parent=self)
        layout.addWidget(self.panel)

    def makeUI(self):
        return self

    def updateValue(self):
        """Called by Nuke on every knob change while the panel is visible.
        We use this opportunity to save state because Nuke may destroy
        the widget without triggering hideEvent / closeEvent."""
        try:
            self.panel._save_all_state_to_node()
        except Exception:
            pass


class VeoRecordKnobWidget(QtWidgets.QWidget):
    """Wrapper for VEO video record node."""

    def __init__(self, node=None):
        super(VeoRecordKnobWidget, self).__init__()
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        if node is None:
            try:
                node = nuke.thisNode()
            except:
                node = None

        self.panel = VeoRecordWidget(node, self)
        layout.addWidget(self.panel)

    def makeUI(self):
        return self

    def updateValue(self):
        pass


class VeoViewerRegenWidget(QtWidgets.QWidget):
    """Wrapper for VEO Viewer node's Regenerate tab (PyCustom_Knob).

    Re-uses VeoRecordWidget which provides:
      - Top: read-only record of original generation parameters
      - Bottom: editable parameters + REGENERATE button
    For a standalone (manually created) viewer the record section will
    simply be empty until a generation populates the knobs.
    """

    def __init__(self):
        super(VeoViewerRegenWidget, self).__init__()
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        try:
            self.node = nuke.thisNode()
        except Exception:
            self.node = None

        self.panel = VeoRecordWidget(self.node, parent=self)
        layout.addWidget(self.panel)

    def makeUI(self):
        return self

    def updateValue(self):
        try:
            if hasattr(self, 'panel') and hasattr(self.panel, '_save_state_to_node'):
                self.panel._save_state_to_node()
        except Exception:
            pass
