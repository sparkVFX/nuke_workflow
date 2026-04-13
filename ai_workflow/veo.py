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

            # Force 8s per API requirement:
            #   - Ingredients mode (reference_images) -> must be 8s
            #   - Resolution 1080p/4k + Ingredients mode -> must be 8s
            #   - Frames / FirstFrame mode: user can freely choose 4/6/8s
            if has_refs and self.mode not in ("Frames", "FirstFrame"):
                dur_seconds = 8
            if self.resolution and self.resolution.lower() in ("1080p", "4k"):
                if self.mode not in ("Frames", "FirstFrame"):
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

    The Group exposes all Read-tab knobs via Link_Knobs so the user sees
    exactly the same parameters as a native Read node, plus a Send to Studio
    button at the bottom.

    Args:
        video_path: Optional path to a video file to load.
        name: Optional node name.
        xpos, ypos: Optional position.

    Returns:
        (group_node, internal_read_node) tuple.
    """
    # --- Create the Group shell ---
    group = nuke.nodes.Group()
    if name:
        group.setName(name)
    else:
        group.setName("VEO_Player1")
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
    # Instead of Link_Knob (which doesn't render UI for many knob types),
    # we add a custom "Open Read Properties" button and a direct knob callback
    # approach.  The simplest reliable method in Nuke is to use the Group's
    # built-in "publish" mechanism via TCL, but since that is fragile across
    # versions we use a pragmatic approach: expose the most important knobs
    # as real knobs with TCL expression links, and for MOV options use a
    # dedicated callback.

    # Tab: Read
    tab_read = nuke.Tab_Knob("read_tab", "Read")
    group.addKnob(tab_read)

    # Helper: get full TCL path for internal read knob
    read_full = read_node.fullName()

    # --- file knob (special: File_Knob) ---
    file_knob = nuke.File_Knob("veo_file", "file")
    if video_path:
        file_knob.setValue(video_path.replace("\\", "/"))
    group.addKnob(file_knob)

    # --- format ---
    try:
        link_format = nuke.Link_Knob("format")
        link_format.makeLink(read_full, "format")
        link_format.setLabel("format")
        group.addKnob(link_format)
    except Exception:
        pass

    # --- frame range knobs ---
    # first and last on the same line
    if "first" in read_node.knobs():
        try:
            link = nuke.Link_Knob("first")
            link.makeLink(read_full, "first")
            link.setLabel("first")
            link.setFlag(nuke.STARTLINE)
            group.addKnob(link)
        except Exception:
            pass
    if "last" in read_node.knobs():
        try:
            link = nuke.Link_Knob("last")
            link.makeLink(read_full, "last")
            link.setLabel("last")
            link.clearFlag(nuke.STARTLINE)
            group.addKnob(link)
        except Exception:
            pass

    # frame_mode and frame on the same line
    if "frame_mode" in read_node.knobs():
        try:
            link = nuke.Link_Knob("frame_mode")
            link.makeLink(read_full, "frame_mode")
            link.setLabel("frame mode")
            link.setFlag(nuke.STARTLINE)
            group.addKnob(link)
        except Exception:
            pass
    if "frame" in read_node.knobs():
        try:
            link = nuke.Link_Knob("frame")
            link.makeLink(read_full, "frame")
            link.setLabel("frame")
            link.clearFlag(nuke.STARTLINE)
            group.addKnob(link)
        except Exception:
            pass

    # origfirst and origlast on the same line
    if "origfirst" in read_node.knobs():
        try:
            link = nuke.Link_Knob("origfirst")
            link.makeLink(read_full, "origfirst")
            link.setLabel("origfirst")
            link.setFlag(nuke.STARTLINE)
            group.addKnob(link)
        except Exception:
            pass
    if "origlast" in read_node.knobs():
        try:
            link = nuke.Link_Knob("origlast")
            link.makeLink(read_full, "origlast")
            link.setLabel("origlast")
            link.clearFlag(nuke.STARTLINE)
            group.addKnob(link)
        except Exception:
            pass

    # --- on_error ---
    if "on_error" in read_node.knobs():
        try:
            link = nuke.Link_Knob("on_error")
            link.makeLink(read_full, "on_error")
            link.setLabel("missing frames")
            link.setFlag(nuke.STARTLINE)
            group.addKnob(link)
        except Exception:
            pass

    # --- colorspace (Input Transform), premultiplied, raw, auto_alpha on the same line ---
    if "colorspace" in read_node.knobs():
        try:
            link = nuke.Link_Knob("colorspace")
            link.makeLink(read_full, "colorspace")
            link.setLabel(read_node["colorspace"].label() or "colorspace")
            link.setFlag(nuke.STARTLINE)
            group.addKnob(link)
        except Exception:
            pass
    for kname in ["premultiplied", "raw", "auto_alpha"]:
        if kname in read_node.knobs():
            try:
                link = nuke.Link_Knob(kname)
                link.makeLink(read_full, kname)
                link.setLabel(read_node[kname].label() or kname)
                link.clearFlag(nuke.STARTLINE)
                group.addKnob(link)
            except Exception:
                pass

    # --- MOV Options section ---
    # These knobs are dynamically added by Nuke when a MOV file is loaded.
    mov_knob_names = [
        "ycbcr_matrix", "mov_data_range",
        "first_track_only", "metadata", "noprefix", "match_key_format",
        "mov64_decode_codec", "mov_decode_codec",
        "video_codec_knob",
    ]

    mov_divider_added = False
    for kname in mov_knob_names:
        if kname in read_node.knobs():
            if not mov_divider_added:
                mov_div = nuke.Text_Knob("mov_divider", "MOV Options")
                group.addKnob(mov_div)
                mov_divider_added = True
            try:
                link = nuke.Link_Knob(kname)
                link.makeLink(read_full, kname)
                link.setLabel(read_node[kname].label() or kname)
                group.addKnob(link)
            except Exception:
                pass

    # If MOV was loaded, also show the video codec as a text display
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
    open_read_script = "n = nuke.thisNode()\nn.begin()\nr = nuke.toNode('InternalRead')\nn.end()\nif r: nuke.show(r)"
    open_btn = nuke.PyScript_Knob("open_read_props", "Open Full Read Properties", open_read_script)
    open_btn.setFlag(nuke.STARTLINE)
    group.addKnob(open_btn)

    # --- knobChanged callback to sync veo_file → internal Read's file ---
    kc_script = (
        "n = nuke.thisNode()\n"
        "k = nuke.thisKnob()\n"
        "if k.name() == 'veo_file':\n"
        "    n.begin()\n"
        "    r = nuke.toNode('InternalRead')\n"
        "    n.end()\n"
        "    if r:\n"
        "        r['file'].fromUserText(k.value())\n"
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
    return group, read_node


def _get_internal_read(player_group):
    """Get the internal Read node from a VEO Player Group."""
    if player_group is None:
        return None
    try:
        player_group.begin()
        read_node = nuke.toNode("InternalRead")
        player_group.end()
        return read_node
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Video Record Node Creation
# ---------------------------------------------------------------------------
def create_video_record_node(generator_node, prompt, aspect_ratio, duration,
                             output_video_path,
                             reference_image_paths=None,
                             model="Google VEO 3.1-Fast",
                             resolution="720P",
                             mode="Text",
                             negative_prompt=""):
    """
    Create a video record (视频记录) node linked to the VEO generator.
    """
    gen_x = generator_node["xpos"].value()
    gen_y = generator_node["ypos"].value()
    gen_name = generator_node.name()

    # Find existing video record nodes for THIS generator
    existing_records = []
    for node in nuke.allNodes("Group"):
        if "veo_record_tab" in node.knobs():
            if "veo_generator" in node.knobs():
                if node["veo_generator"].value() == gen_name:
                    existing_records.append(node)

    # Calculate position
    if existing_records:
        last_record = max(existing_records, key=lambda n: n["ypos"].value())
        rec_x = last_record["xpos"].value()
        rec_y = last_record["ypos"].value() + 150
        connect_to = last_record
    else:
        rec_x = gen_x
        rec_y = gen_y + 150
        connect_to = generator_node

    # --- Create a Dot node between generator/previous-record and this record ---
    dot_node = nuke.nodes.Dot()
    dot_x = int(rec_x) + 34  # Dot is small, offset to center under parent
    dot_y = int(connect_to["ypos"].value()) + 80 if not existing_records else int(rec_y) - 50
    if not existing_records:
        dot_y = int(gen_y) + 100
    dot_node["xpos"].setValue(dot_x)
    dot_node["ypos"].setValue(dot_y)
    dot_node.setInput(0, connect_to)

    # Create record node
    record_num = len(existing_records) + 1
    record_node = nuke.nodes.Group()
    record_node.setName("sp{}".format(record_num))
    record_node["tile_color"].setValue(0x4169E1FF)  # Blue
    record_node["xpos"].setValue(int(rec_x))
    record_node["ypos"].setValue(int(rec_y))

    prompt_short = prompt[:40] + "..." if len(prompt) > 40 else prompt
    record_node["label"].setValue("视频记录sp{}".format(record_num))

    # Internal structure
    record_node.begin()
    inp = nuke.nodes.Input(name="Input")
    out = nuke.nodes.Output(name="Output")
    out.setInput(0, inp)
    record_node.end()

    # Record node connects to the Dot
    record_node.setInput(0, dot_node)

    # Add custom tab
    tab = nuke.Tab_Knob("veo_record_tab", "VEO Record")
    record_node.addKnob(tab)

    gen_knob = nuke.String_Knob("veo_generator", "Generator")
    gen_knob.setValue(gen_name)
    gen_knob.setFlag(nuke.INVISIBLE)
    record_node.addKnob(gen_knob)

    prompt_knob = nuke.Multiline_Eval_String_Knob("veo_prompt", "Prompt")
    prompt_knob.setValue(prompt)
    prompt_knob.setFlag(nuke.INVISIBLE)
    record_node.addKnob(prompt_knob)

    ratio_knob = nuke.String_Knob("veo_ratio", "Aspect Ratio")
    ratio_knob.setValue(aspect_ratio)
    ratio_knob.setFlag(nuke.INVISIBLE)
    record_node.addKnob(ratio_knob)

    dur_knob = nuke.String_Knob("veo_duration", "Duration")
    dur_knob.setValue(duration if duration else "8")
    dur_knob.setFlag(nuke.INVISIBLE)
    record_node.addKnob(dur_knob)

    model_knob = nuke.String_Knob("veo_model", "Model")
    model_knob.setValue(model)
    model_knob.setFlag(nuke.INVISIBLE)
    record_node.addKnob(model_knob)

    res_knob = nuke.String_Knob("veo_resolution", "Resolution")
    res_knob.setValue(resolution)
    res_knob.setFlag(nuke.INVISIBLE)
    record_node.addKnob(res_knob)

    mode_knob = nuke.String_Knob("veo_mode", "Mode")
    mode_knob.setValue(mode)
    mode_knob.setFlag(nuke.INVISIBLE)
    record_node.addKnob(mode_knob)

    neg_prompt_knob = nuke.Multiline_Eval_String_Knob("veo_neg_prompt", "Negative Prompt")
    neg_prompt_knob.setValue(negative_prompt)
    neg_prompt_knob.setFlag(nuke.INVISIBLE)
    record_node.addKnob(neg_prompt_knob)

    # Output video path (hidden)
    output_knob = nuke.File_Knob("veo_output_path", "Output Video")
    output_knob.setValue(output_video_path.replace("\\", "/") if output_video_path else "")
    output_knob.setFlag(nuke.INVISIBLE)
    record_node.addKnob(output_knob)

    # Store input image paths (hidden)
    input_data = {
        "reference_images": reference_image_paths or [],
    }
    inputs_knob = nuke.String_Knob("veo_input_images", "Input Images (JSON)")
    inputs_knob.setValue(json.dumps(input_data))
    inputs_knob.setFlag(nuke.INVISIBLE)
    record_node.addKnob(inputs_knob)

    # Add PyCustom_Knob for regenerate UI
    divider = nuke.Text_Knob("divider1", "")
    record_node.addKnob(divider)

    custom_knob = nuke.PyCustom_Knob(
        "veo_record_ui", "",
        "ai_workflow.veo.VeoRecordKnobWidget(nuke.thisNode())"
    )
    custom_knob.setFlag(nuke.STARTLINE)
    record_node.addKnob(custom_knob)

    # Create VEO Player node (Group wrapping Read) for output video
    read_node = None
    player_node = None
    if output_video_path and os.path.exists(output_video_path):
        player_node, read_node = create_veo_player_node(
            video_path=output_video_path,
            name="生成图像sp{}".format(record_num),
            xpos=rec_x + 200,
            ypos=rec_y,
        )

        read_ref_knob = nuke.String_Knob("veo_read_node", "Read Node")
        read_ref_knob.setValue(player_node.name())
        read_ref_knob.setFlag(nuke.INVISIBLE)
        record_node.addKnob(read_ref_knob)

        # Player's input 0 connects to record node (SP → Player direction)
        player_node.setInput(0, record_node)

        print("VEO: Created VEO Player '{}' for video: {}".format(
            player_node.name(), output_video_path))
    else:
        read_ref_knob = nuke.String_Knob("veo_read_node", "Read Node")
        read_ref_knob.setValue("")
        read_ref_knob.setFlag(nuke.INVISIBLE)
        record_node.addKnob(read_ref_knob)

    return record_node, player_node


def update_record_read_node(record_node, new_video_path):
    """Update the VEO Player (or legacy Read) node associated with a video record node.
    If the node doesn't exist, create a new VEO Player."""
    player_node = None
    player_node_name = ""

    if "veo_read_node" in record_node.knobs():
        player_node_name = record_node["veo_read_node"].value()
        if player_node_name:
            player_node = nuke.toNode(player_node_name)

    if player_node:
        # Check if it's a VEO Player Group or a legacy Read node
        internal_read = _get_internal_read(player_node)
        if internal_read:
            # It's a VEO Player Group — update the internal Read
            internal_read["file"].fromUserText(new_video_path)
            # Also sync the Group's veo_file knob
            if "veo_file" in player_node.knobs():
                player_node["veo_file"].setValue(new_video_path.replace("\\", "/"))
        elif player_node.Class() == "Read":
            # Legacy Read node — update directly
            player_node["file"].fromUserText(new_video_path)
        record_node["veo_output_path"].setValue(new_video_path.replace("\\", "/"))
        return player_node

    # Node not found — create a new VEO Player
    rec_x = int(record_node["xpos"].value())
    rec_y = int(record_node["ypos"].value())

    player_node, read_node = create_veo_player_node(
        video_path=new_video_path,
        name="regenerated_video",
        xpos=rec_x + 200,
        ypos=rec_y,
    )

    # Store new node reference
    if "veo_read_node" in record_node.knobs():
        record_node["veo_read_node"].setValue(player_node.name())
    else:
        read_ref_knob = nuke.String_Knob("veo_read_node", "Read Node")
        read_ref_knob.setValue(player_node.name())
        record_node.addKnob(read_ref_knob)

    if "veo_output_path" in record_node.knobs():
        record_node["veo_output_path"].setValue(new_video_path.replace("\\", "/"))

    # Player's input 0 connects to record node (SP → Player direction)
    player_node.setInput(0, record_node)

    print("VEO: Created new VEO Player '{}' for regenerated video: {}".format(
        player_node.name(), new_video_path))
    return player_node


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
        self.mode_combo.addItem("Text（文本）", VEO_MODE_TEXT)
        self.mode_combo.addItem("FirstFrame（首帧）", VEO_MODE_FIRST_FRAME)
        self.mode_combo.addItem("Frames（首尾帧）", VEO_MODE_FRAMES)
        self.mode_combo.addItem("Ingredients（多图参考）", VEO_MODE_INGREDIENTS)
        self.mode_combo.setCurrentIndex(0)
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
        self.prompt_mode_combo.currentIndexChanged.connect(lambda _: self._save_all_state_to_node())
        row_hist.addWidget(self.prompt_mode_combo)

        main.addLayout(row_hist)

        # === Prompt ===
        self.prompt_edit = QtWidgets.QTextEdit()
        self.prompt_edit.setPlaceholderText("Enter your creative prompt here...")
        self.prompt_edit.setMinimumHeight(120)
        self.prompt_edit.textChanged.connect(self._save_all_state_to_node)
        main.addWidget(self.prompt_edit)

        # === Negative Prompt ===
        self.neg_prompt_edit = QtWidgets.QTextEdit()
        self.neg_prompt_edit.setPlaceholderText("Negative Prompt (Optional)...")
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

    # --- Mode switching ---
    def _on_mode_combo_changed(self, index):
        mode = self.mode_combo.currentData() or VEO_MODE_TEXT
        self._update_node_inputs(mode)
        self._update_duration_for_mode(mode)

    def _update_duration_for_mode(self, mode):
        """Lock duration to 8s for Frames / Ingredients modes (API requirement)."""
        if mode in (VEO_MODE_FRAMES, VEO_MODE_INGREDIENTS):
            self.dur_combo.blockSignals(True)
            self.dur_combo.setCurrentIndex(2)   # index 2 = "8"
            self.dur_combo.blockSignals(False)
            self.dur_combo.setEnabled(False)
        else:
            self.dur_combo.setEnabled(True)

    def _get_current_mode(self):
        return self.mode_combo.currentData() or VEO_MODE_TEXT

    def _update_node_inputs(self, mode):
        """Dynamically update the VEO_Generate node's internal Input count
        and rename the Input nodes according to mode:
          FirstFrame:   A1 -> FirstFrame
          Frames:       A1 -> FirstFrame, A2 -> EndFrame
          Ingredients:  A1 -> img1, A2 -> img2, A3 -> img3
        """
        node = self._get_owner_node()
        if not node:
            return

        needed = VEO_MODE_INPUT_COUNTS.get(mode, 0)

        # Name mapping per mode
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

        if current_count < needed:
            # Add missing inputs
            for i in range(current_count, needed):
                inp = nuke.nodes.Input()
                label = names[i] if i < len(names) else "A{}".format(i + 1)
                inp.setName(label)
                inp["xpos"].setValue(i * 200)
                inp["ypos"].setValue(0)
        elif current_count > needed:
            # Remove extra inputs (remove from highest index)
            existing_inputs.sort(key=lambda n: n.name(), reverse=True)
            for i in range(current_count - needed):
                nuke.delete(existing_inputs[i])

        # Rename remaining inputs to match the current mode.
        # Sort by xpos (layout order) to get a stable index order.
        remaining_inputs = sorted(
            [n for n in nuke.allNodes("Input")],
            key=lambda n: int(n["xpos"].value())
        )
        # First pass: give temporary names to avoid collision
        for i, inp_node in enumerate(remaining_inputs):
            try:
                inp_node.setName("_veo_tmp_{}".format(i))
            except Exception:
                pass
        # Second pass: assign final names
        for i, inp_node in enumerate(remaining_inputs):
            if i < len(names):
                try:
                    inp_node.setName(names[i])
                except Exception:
                    pass

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

            node["veo_s_model"].setValue(self.model_combo.currentIndex())
            node["veo_s_ratio"].setValue(self.ratio_combo.currentIndex())
            node["veo_s_res"].setValue(self.res_combo.currentIndex())
            node["veo_s_dur"].setValue(self.dur_combo.currentIndex())
            node["veo_s_mode"].setValue(self.mode_combo.currentIndex())
            node["veo_s_pm"].setValue(self.prompt_mode_combo.currentIndex())
            node["veo_s_prompt"].setValue(self.prompt_edit.toPlainText())
            node["veo_s_neg"].setValue(self.neg_prompt_edit.toPlainText())
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
            for w in widgets:
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

            for w in widgets:
                w.blockSignals(False)

            # After restoring mode, update the node inputs accordingly
            mode = self.mode_combo.currentData() or VEO_MODE_TEXT
            self._update_node_inputs(mode)
            self._update_duration_for_mode(mode)

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
            self.current_worker.stop()
            self.status_label.setText("Stopped")
            self._toggle_stop_ui(False)
            return

        prompt = self.prompt_edit.toPlainText().strip()
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
        for idx in range(input_count):
            inp_ref = node.input(idx)
            if inp_ref:
                ref_label = "input{}".format(idx + 1)
                # Render from current timeline frame, but name files as frame1/frame2/frame3
                frame_idx = idx + 1
                path = os.path.join(input_dir, "{}_{}_frame{}.png".format(gen_name, ref_label, frame_idx))
                if render_input_to_file_silent(inp_ref, path, nuke.frame()):
                    reference_image_paths.append(path)
                else:
                    nuke.message("Error: Failed to render input A{}.".format(idx + 1))
                    self.status_label.setText("Error: A{} render failed".format(idx + 1))
                    self._toggle_stop_ui(False)
                    return

        model_name = self.model_combo.currentText()
        ratio = self.ratio_combo.currentText()
        duration = self.dur_combo.currentData() or self.dur_combo.currentText()
        resolution = self.res_combo.currentText()

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

        def _on_finished(path, metadata):
            """Called when generation finishes. Works even if widget is destroyed."""
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
                        record_node, read_node = create_video_record_node(
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
                        if read_node:
                            try:
                                nuke.connectViewer(0, read_node)
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
        self.mode_combo.addItem("Text（文本）", VEO_MODE_TEXT)
        self.mode_combo.addItem("FirstFrame（首帧）", VEO_MODE_FIRST_FRAME)
        self.mode_combo.addItem("Frames（首尾帧）", VEO_MODE_FRAMES)
        self.mode_combo.addItem("Ingredients（多图参考）", VEO_MODE_INGREDIENTS)
        self.mode_combo.setCurrentIndex(0)
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
        self.neg_prompt_edit.setPlaceholderText("Negative Prompt (Optional)...")
        self.neg_prompt_edit.setMinimumHeight(80)
        main.addWidget(self.neg_prompt_edit)

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

            # Cached images info
            if "veo_input_images" in self.node.knobs():
                try:
                    data = json.loads(self.node["veo_input_images"].value())
                    ref_list = data.get("reference_images", [])
                    cached_refs = sum(1 for r in ref_list if r and os.path.exists(r))
                    if cached_refs > 0:
                        self.cached_info_label.setText("Cached refs: {}".format(cached_refs))
                    else:
                        self.cached_info_label.setText("Text-only generation")
                except:
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

        except Exception as e:
            print("VEO: Error loading record settings: {}".format(e))

    def _regenerate(self):
        """Regenerate video using editable parameters and cached reference images."""
        if not self.settings.api_key:
            self.status_label.setStyleSheet("color: #ef4444; font-size: 11px;")
            self.status_label.setText("Please set API key in Settings")
            nuke.message("API key not set.\nPlease open AI Workflow > Setting in the toolbar.")
            return

        if self.current_worker and self.current_worker.is_running:
            self.current_worker.stop()
            self.status_label.setText("Stopped")
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
        resolution = self.res_combo.currentText()
        current_mode = self.mode_combo.currentData() or VEO_MODE_TEXT

        # Collect cached reference images from node
        reference_image_paths = []
        if "veo_input_images" in self.node.knobs():
            try:
                data = json.loads(self.node["veo_input_images"].value())
                ref_list = data.get("reference_images", [])
                reference_image_paths = [r for r in ref_list if r and os.path.exists(r)]
            except:
                pass

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

        def _on_finished(path, metadata):
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
                        read_node = update_record_read_node(node_ref, path)
                        if read_node:
                            try:
                                nuke.connectViewer(0, read_node)
                            except:
                                pass
                    except Exception as e:
                        print("VEO: ERROR updating Read node: {}".format(e))
                    finally:
                        _veo_active_workers.pop(worker_id, None)
                nuke.executeInMainThread(_update)
            else:
                _veo_active_workers.pop(worker_id, None)

        def _on_error(err):
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
def create_veo_node():
    """
    Create a VEO_Generate node.
    Auto-detect mode based on the number of selected nodes (any type, not just Read):
      - 0 nodes selected -> Text mode (no inputs)
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
        auto_mode = VEO_MODE_TEXT
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
    group_node.setName("VEO_Generate")
    group_node["tile_color"].setValue(0x4169E1FF)  # Royal Blue
    group_node["label"].setValue("VEO Video Generation")

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

    # Build internal structure
    group_node.begin()

    # Create Input nodes based on mode
    for i in range(needed_inputs):
        inp = nuke.nodes.Input()
        label = input_names[i] if i < len(input_names) else "A{}".format(i + 1)
        inp.setName(label)
        inp["xpos"].setValue(i * 200)
        inp["ypos"].setValue(0)

    out = nuke.nodes.Output()
    out["xpos"].setValue(0)
    out["ypos"].setValue(200)

    group_node.end()

    # Connect selected nodes as inputs (first selected -> input 0 = FirstFrame, etc.)
    for i, node in enumerate(input_nodes[:needed_inputs]):
        group_node.setInput(i, node)

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
        VEO_MODE_TEXT: "Text（文本）",
        VEO_MODE_FIRST_FRAME: "FirstFrame（首帧）",
        VEO_MODE_FRAMES: "Frames（首尾帧）",
        VEO_MODE_INGREDIENTS: "Ingredients（多图参考）",
    }
    print("VEO: Created VEO_Generate with auto-detected mode: {} (selected {} nodes)".format(
        mode_display.get(auto_mode, auto_mode), node_count))

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
