"""
Seedance Video Generation Node for Nuke.
Creates a node in Node Graph for AI video generation using Volcengine Ark API (Seedance).

Features:
- Three modes: Text (no inputs), Image (1 input: first frame), Frames (2 inputs: first+last frame)
- Model selection: Seedance 2.0 / 1.5 Pro / 1.0 Pro / 1.0 Pro Fast
- Resolution: 480p / 720p / 1080p
- Duration: 4 / 5 / 8 / 10 / 15 seconds or Auto(-1)
- Aspect ratio: 16:9, 4:3, 1:1, 3:4, 9:16, 21:9, adaptive
- Prompt history (saves last 10)
- Negative prompt support
- Async video generation with polling via OpenAI-compatible API
- Video record nodes for regeneration

Mode input mapping:
  Text: No inputs (pure text-to-video)
  Image: 1 input (FirstFrame reference image)
  Frames: 2 inputs (FirstFrame + EndFrame)
"""

# ---------------------------------------------------------------------------
# Shared imports from ai_workflow.core
# ---------------------------------------------------------------------------
from ai_workflow.core.pyside_compat import QtWidgets, QtCore, QtGui, _isValid
from ai_workflow.core.ui_components import DropDownComboBox, SHARED_DARK_STYLE
from ai_workflow.core.model_catalog import (
    SEEDANCE_MODEL_OPTIONS,
    SEEDANCE_RATIO_OPTIONS,
    SEEDANCE_RESOLUTION_OPTIONS,
    SEEDANCE_DURATION_OPTIONS,
    SEEDANCE_MODE_OPTIONS,
    fill_combo_from_options,
)
from ai_workflow.core.history_store import (
    get_history,
    set_history,
    push_history_item,
)
from ai_workflow.core.settings import (
    AppSettings as NanoBananaSettings,
    app_settings,
    CONFIG_FILE_NAME, DEFAULT_TEMP_DIR_NAME,
    DEFAULT_PROJECT_CACHE_NAME, UNSAVED_PROJECT_DIR,
)
from ai_workflow.core.directories import (
    get_script_name, get_project_directory, get_temp_directory,
    get_input_directory, get_output_directory, get_logs_directory,
)
from ai_workflow.core.rendering import (
    render_input_to_file_silent, collect_input_images, collect_input_image_paths,
)
from ai_workflow.core.api_helpers import (
    image_to_base64, get_mime_type,
)
from ai_workflow.core.nuke_utils import (
    get_internal_read as _get_internal_read_core,
    next_node_name,
    rebuild_group_for_thumbnail,
    update_node_thumbnail as _update_node_thumbnail_core,
    restore_thumbnails,
)
from ai_workflow.core.worker_base import (
    BaseWorker, register_active_worker, unregister_active_worker,
    _active_workers,
)

# Backward-compatible re-exports from seedance_nodes:
from ai_workflow.seedance_nodes import (  # noqa: F401
    _SEND_TO_STUDIO_SCRIPT,
    _add_send_to_studio_knob,
    create_seedance_viewer_node,
    _get_internal_seedance_read as _get_internal_read,
    _rebuild_seedance_group_for_thumbnail,
    _update_seedance_thumbnail,
    _next_seedance_viewer_name,
    create_seedance_viewer_standalone,
    update_seedance_viewer_read,
    _find_seedance_generator,
    _collect_seedance_input_image_paths,
    _next_seedance_name,
    _create_seedance_group_inputs,
    create_seedance_node,
    seedance_omni_add_input,
    seedance_omni_remove_input,
    # Constants re-exported for backward compatibility
    SEEDANCE_MODE_TEXT, SEEDANCE_MODE_IMAGE, SEEDANCE_MODE_FRAMES,
    SEEDANCE_MODE_OMNI_REF, SEEDANCE_MODE_VIDEO_EXTEND, SEEDANCE_MODE_AUDIO_DRIVE,
    SEEDANCE_MODE_INPUT_COUNTS,
)

import nuke
import nukescripts
import os
import json
import tempfile
import time
import datetime
import re
import base64
import subprocess
import sys

# HTTP requests for Ark API
try:
    import urllib.request
    import urllib.error
    HAS_URllib = True
except ImportError:
    HAS_URllib = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SEEDANCE_MAX_INPUTS = 2

SEEDANCE_STYLE = SHARED_DARK_STYLE

# Seedance-specific worker registry (separate from NB/VEO workers)
_seedance_active_workers = {}

# Default Ark API endpoint (OpenAI-compatible)
ARK_API_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"


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

    script_dir = os.path.dirname(os.path.abspath(__file__))
    local_ffmpeg = os.path.join(script_dir, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if os.path.isfile(local_ffmpeg):
        return local_ffmpeg

    try:
        nuke_dir = os.path.dirname(nuke.EXE_PATH)
        nuke_ffmpeg = os.path.join(nuke_dir, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if os.path.isfile(nuke_ffmpeg):
            return nuke_ffmpeg
    except Exception:
        pass

    found = shutil.which("ffmpeg")
    if found:
        return found

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
        codec: Optional str - ProRes profile name.

    Returns:
        mov_path (str) on success, or mp4_path unchanged if ffmpeg is unavailable.
    """
    _PROFILE_MAP = {
        "ProRes 422 HQ":   ("3", "yuva444p10le"),
        "ProRes 422":      ("2", "yuva444p10le"),
        "ProRes 422 LT":   ("1", "yuv422p10le"),
        "ProRes 422 Proxy":("0", "yuv422p10le"),
    }

    if not codec:
        try:
            codec = NanoBananaSettings().prores_codec
        except Exception:
            codec = "ProRes 422 HQ"

    profile_val, pix_fmt = _PROFILE_MAP.get(codec, _PROFILE_MAP["ProRes 422 HQ"])

    print("[Seedance Transcode] Converting '{}' to ProRes MOV | codec={} | profile={}".format(
        mp4_path, codec, profile_val))

    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        print("Seedance: ffmpeg not found - keeping original MP4.")
        if status_callback:
            status_callback("Warning: ffmpeg not found, using MP4")
        return mp4_path

    mov_path = os.path.splitext(mp4_path)[0] + ".mov"

    cmd = [
        ffmpeg,
        "-y",
        "-i", mp4_path,
        "-c:v", "prores_ks",
        "-profile:v", profile_val,
        "-pix_fmt", pix_fmt,
        "-c:a", "pcm_s16le",
        mov_path,
    ]

    if status_callback:
        status_callback("Converting to ProRes MOV...")

    try:
        print("Seedance: Running ffmpeg: {}".format(" ".join(cmd)))
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode == 0 and os.path.exists(mov_path):
            print("Seedance: ProRes conversion OK -> {}".format(mov_path))
            return mov_path
        else:
            print("Seedance: ffmpeg failed (code {}): {}".format(
                result.returncode, result.stderr[:500]))
            if status_callback:
                status_callback("Warning: ProRes conversion failed, using MP4")
            return mp4_path
    except subprocess.TimeoutExpired:
        print("Seedance: ffmpeg timed out after 300s")
        if status_callback:
            status_callback("Warning: ffmpeg timed out, using MP4")
        return mp4_path
    except Exception as e:
        print("Seedance: ffmpeg error: {}".format(e))
        if status_callback:
            status_callback("Warning: ffmpeg error, using MP4")
        return mp4_path


def _load_image_base64(image_path):
    """Load an image file and return base64-encoded string with mime type."""
    ext = os.path.splitext(image_path)[1].lower()
    mime_map = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
    }
    mime_type = mime_map.get(ext, "image/png")
    with open(image_path, "rb") as f:
        raw_bytes = f.read()
    b64 = base64.b64encode(raw_bytes).decode("utf-8")
    return b64, mime_type


def _call_ark_api(api_key, endpoint, payload, base_url=None):
    """Call the Volcengine Ark API (OpenAI-compatible).

    Args:
        api_key: The API key for authentication.
        endpoint: API endpoint path (e.g., '/videos/generations').
        payload: JSON-serializable dict for request body.
        base_url: Optional override for API base URL.

    Returns:
        Parsed JSON response dict.
    """
    url = (base_url or ARK_API_BASE_URL).rstrip("/") + endpoint

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer {}".format(api_key),
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise Exception("Ark API HTTP {}: {}".format(e.code, body[:500]))
    except urllib.error.URLError as e:
        raise Exception("Ark API connection failed: {}".format(str(e)))


def _poll_task_result(api_key, task_id, base_url=None, max_polls=120, poll_interval=5):
    """Poll a Seedance async task until completion.

    Args:
        api_key: API key.
        task_id: Task ID from generation response.
        base_url: Optional API base URL override.
        max_polls: Maximum number of polls.
        poll_interval: Seconds between polls.

    Returns:
        Dict with task status and result URL.
    """
    url = (base_url or ARK_API_BASE_URL).rstrip("/")
    endpoint = "/videos/generations/{}?with_result_url=true".format(task_id)

    for i in range(max_polls):
        time.sleep(poll_interval)

        req = urllib.request.Request(
            url + endpoint,
            headers={
                "Authorization": "Bearer {}".format(api_key),
            },
            method="GET",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8")
                result = json.loads(body)
        except urllib.error.URLError as e:
            print("Seedance: Poll error (attempt {}/{}): {}".format(i + 1, max_polls, e))
            continue

        status = result.get("status", "")
        print("Seedance: Poll {} status={}".format(i + 1, status))

        if status in ("completed", "succeed", "success"):
            return result
        elif status in ("failed", "error"):
            err_msg = result.get("status_message", result.get("message", "Unknown error"))
            raise Exception("Task failed: {}".format(err_msg))

    raise Exception("Task polling timed out after {}s".format(max_polls * poll_interval))


def _download_file(url, dest_path):
    """Download a file from URL to local path."""
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            with open(dest_path, "wb") as f:
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    f.write(chunk)
    except Exception as e:
        raise Exception("Download failed: {}".format(e))


# ---------------------------------------------------------------------------
# Seedance Generation Worker Thread (uses Volcengine Ark OpenAI-compatible API)
# ---------------------------------------------------------------------------


class SeedanceWorker(QtCore.QThread):
    finished = QtCore.Signal(str, dict)   # video_path, metadata
    error = QtCore.Signal(str)
    status_update = QtCore.Signal(str)
    progress_update = QtCore.Signal(int)  # percentage 0-100

    def __init__(self, api_key, prompt,
                 reference_image_paths=None,
                 model="doubao-seedance-2-0-260128",
                 aspect_ratio="16:9", duration=5,
                 resolution="720p",
                 mode="text",
                 negative_prompt="",
                 # Omni Reference: multi-modal references (images/videos/audio)
                 omni_references=None,
                 # Video Extend / Audio Drive: single file path
                 media_path=None,
                 temp_dir=None, gen_name="Seedance_Generate"):
        super(SeedanceWorker, self).__init__()
        self.api_key = api_key
        self.prompt = prompt
        self.negative_prompt = negative_prompt
        self.reference_image_paths = reference_image_paths or []
        self.omni_references = omni_references or {}   # {"image1": path, "video1": path, ...}
        self.media_path = media_path                  # for video_extend / audio_drive
        self.model = model
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
            # 1. Load reference images as base64
            self.status_update.emit("Preparing inputs...")
            ref_images_data = []
            for rp in self.reference_image_paths:
                if rp and os.path.exists(rp):
                    b64, mime = _load_image_base64(rp)
                    ref_images_data.append({
                        "type": "image_url",
                        "image_url": {"url": "data:{};base64,{}".format(mime, b64)},
                    })

            if not self.is_running:
                return

            # 2. Build generation payload for Ark API (/videos/generations)
            payload = {
                "model": self.model,
                "prompt": self.prompt,
                "aspect_ratio": self.aspect_ratio,
                "resolution": self.resolution,
            }

            # Duration: handle Auto(-1) by omitting or passing -1
            if isinstance(self.duration, int) and self.duration > 0:
                payload["duration"] = self.duration
            elif isinstance(self.duration, str) and self.duration != "-1":
                try:
                    payload["duration"] = int(self.duration)
                except (ValueError, TypeError):
                    pass  # Auto mode - let server decide

            # Negative prompt
            if self.negative_prompt:
                payload["negative_prompt"] = self.negative_prompt

            # Mode and reference images
            if self.mode == "text":
                pass  # Pure text-to-video, no image references needed

            elif self.mode == "image" and ref_images_data:
                payload["image"] = ref_images_data[0]

            elif self.mode == "frames" and ref_images_data:
                payload["image"] = ref_images_data[0]
                if len(ref_images_data) > 1:
                    payload["end_image"] = ref_images_data[1]

            elif self.mode == "omni_reference":
                # Omni Reference: build references dict with up to 9 images, 3 videos, 3 audio
                refs = {}
                # Image references (image1 ~ image9)
                for key, path in sorted(self.omni_references.items()):
                    if key.startswith("image") and os.path.exists(path):
                        b64, mime = _load_image_base64(path)
                        refs[key] = {"type": "image_url",
                                      "image_url": {"url": "data:{};base64,{}".format(mime, b64)}}
                    elif key.startswith("video") and os.path.exists(path):
                        # Video reference - pass as file URL or base64 if small
                        refs[key] = {"type": "video", "url": path}
                    elif key.startswith("audio") and os.path.exists(path):
                        # Audio reference
                        refs[key] = {"type": "audio", "url": path}
                if refs:
                    payload["references"] = refs
                payload["task_type"] = "omni_reference"

            elif self.mode == "video_extend":
                # Video extend: input video to continue from
                if self.media_path and os.path.exists(self.media_path):
                    payload["video"] = {"url": self.media_path}
                payload["task_type"] = "video_extend"

            elif self.mode == "audio_drive":
                # Audio-driven generation
                if self.media_path and os.path.exists(self.media_path):
                    payload["audio"] = {"url": self.media_path}
                payload["task_type"] = "audio_drive"

            # Resolve API base URL from settings
            base_url = None
            try:
                settings = NanoBananaSettings()
                if hasattr(settings, 'seedance_api_base_url') and settings.seedance_api_base_url:
                    base_url = settings.seedance_api_base_url
            except Exception:
                pass

            # Save request log
            try:
                request_log = {
                    "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "model": self.model,
                    "prompt": self.prompt,
                    "negative_prompt": self.negative_prompt,
                    "mode": self.mode,
                    "aspect_ratio": self.aspect_ratio,
                    "duration": self.duration,
                    "resolution": self.resolution,
                    "reference_image_count": len(ref_images_data),
                    "reference_image_paths": self.reference_image_paths,
                }
                log_filename = "seedance_request_{}.json".format(
                    datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
                log_path = os.path.join(self.temp_dir, log_filename)
                with open(log_path, "w", encoding="utf-8") as f:
                    json.dump(request_log, f, indent=2, ensure_ascii=False)
                print("Seedance API: Request payload saved to {}".format(log_path))
                print("Seedance API: Request payload:\n{}".format(
                    json.dumps({k: v for k, v in request.items() if k != "reference_image_paths"},
                               indent=2, ensure_ascii=False)))
            except Exception as log_err:
                print("Seedance API: Failed to save request log: {}".format(log_err))

            # 3. Call Ark API
            self.status_update.emit("Starting video generation ({})...".format(self.mode))
            print("Seedance API: Calling /videos/generations model={} mode={} resolution={} duration={}".format(
                self.model, self.mode, self.resolution, self.duration))

            response = _call_ark_api(
                self.api_key,
                "/videos/generations",
                payload,
                base_url=base_url,
            )

            if not self.is_running:
                return

            # Parse response - extract task ID or direct video URL
            task_id = response.get("id") or response.get("task_id")
            video_url = None

            # Check for immediate result
            if "video_result" in response:
                vr = response["video_result"]
                video_url = vr.get("video_url") or vr.get("url") or vr.get("result_video_url")
            if "data" in response and isinstance(response["data"], list):
                for item in response["data"]:
                    if isinstance(item, dict):
                        video_url = (item.get("video", {}) or {}).get("url") or item.get("url") or item.get("video_url")
                        if video_url:
                            break
                        task_id = task_id or item.get("id") or item.get("task_id")

            # 4. Poll for async task if needed
            if not video_url and task_id:
                self.status_update.emit("Generating video (this may take a few minutes)...")
                self.progress_update.emit(20)

                poll_result = _poll_task_result(
                    self.api_key, task_id, base_url=base_url,
                    max_polls=180, poll_interval=5,  # up to 15 minutes
                )
                if not self.is_running:
                    return

                # Extract video URL from poll result
                if "video_result" in poll_result:
                    vr = poll_result["video_result"]
                    video_url = vr.get("video_url") or vr.get("url") or vr.get("result_video_url")
                if "data" in poll_result and isinstance(poll_result["data"], list):
                    for item in poll_result["data"]:
                        if isinstance(item, dict):
                            video_url = (item.get("video", {}) or {}).get("url") or item.get("url") or item.get("video_url")
                            if video_url:
                                break
                if not video_url:
                    video_url = poll_result.get("result_url") or poll_result.get("video_url")

            if not video_url:
                self.error.emit("No video generated. Response:\n{}".format(
                    json.dumps(response, indent=2, ensure_ascii=False)[:500]))
                return

            # 5. Download video
            self.status_update.emit("Downloading video...")
            self.progress_update.emit(90)

            frame_num = 1
            while True:
                filename = "{}_frame{}.mp4".format(self.gen_name, frame_num)
                output_path = os.path.join(self.temp_dir, filename)
                if not os.path.exists(output_path):
                    break
                frame_num += 1

            _download_file(video_url, output_path)
            print("Seedance API: Video downloaded to {}".format(output_path))

            if not os.path.exists(output_path) or os.path.getsize(output_path) < 1024:
                self.error.emit("Video download failed or file is empty")
                return

            # Convert MP4 -> ProRes MOV for Nuke compatibility
            self.status_update.emit("Converting to ProRes MOV...")
            self.progress_update.emit(96)
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
                "ref_image_count": len(ref_images_data),
            }

            self.finished.emit(final_path, metadata)

        except Exception as e:
            import traceback
            traceback.print_exc()
            self.error.emit("Error: {}".format(str(e)))


# ---------------------------------------------------------------------------
# Seedance Main Widget (embedded in Seedance_Generate node)
# ---------------------------------------------------------------------------
class SeedanceWidget(QtWidgets.QWidget):
    """Custom Qt widget embedded inside the Seedance_Generate node."""

    def __init__(self, node=None, parent=None):
        super(SeedanceWidget, self).__init__(parent)
        if node is None:
            try:
                node = nuke.thisNode()
            except Exception:
                node = None
        self._node = node

        self.setObjectName("nanoBananaRoot")
        self.setStyleSheet(SEEDANCE_STYLE)
        self.setMinimumWidth(380)
        font = self.font()
        font.setStyleStrategy(QtGui.QFont.NoSubpixelAntialias)
        self.setFont(font)
        self.settings = NanoBananaSettings()
        self.current_worker = None
        self._build_ui()
        self._restore_all_state_from_node()

        # Poll the Seedance group's input state so the "+ Add / - Remove / N/9"
        # UI in Omni Reference mode reflects DAG changes made by the user
        # (connecting or disconnecting pipes) without any explicit callback.
        # Lightweight: reads node.inputs() + node.input(0) and only updates
        # when the cached signature changes.
        self._omni_last_sig = None
        self._omni_poll_timer = QtCore.QTimer(self)
        self._omni_poll_timer.setInterval(500)
        self._omni_poll_timer.timeout.connect(self._poll_omni_port_state)
        self._omni_poll_timer.start()

    def _build_ui(self):
        main = QtWidgets.QVBoxLayout(self)
        main.setSpacing(8)
        main.setContentsMargins(8, 8, 8, 8)

        # === Row 1: Model / Aspect ratio / Resolution / Duration ===
        config_row = QtWidgets.QHBoxLayout()
        config_row.setSpacing(12)

        # Model selection
        model_group = QtWidgets.QVBoxLayout()
        model_group.setSpacing(2)
        model_label = QtWidgets.QLabel("Model:")
        model_label.setStyleSheet("color: #aaa; font-size: 11px;")
        model_group.addWidget(model_label)
        self.model_combo = DropDownComboBox()
        fill_combo_from_options(self.model_combo, SEEDANCE_MODEL_OPTIONS)
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
        fill_combo_from_options(self.ratio_combo, SEEDANCE_RATIO_OPTIONS)
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
        fill_combo_from_options(self.res_combo, SEEDANCE_RESOLUTION_OPTIONS)
        self.res_combo.setCurrentIndex(1)  # default 720p
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
        fill_combo_from_options(self.dur_combo, SEEDANCE_DURATION_OPTIONS)
        self.dur_combo.setCurrentIndex(1)  # default 5
        self.dur_combo.currentIndexChanged.connect(lambda _: self._save_all_state_to_node())
        dur_group.addWidget(self.dur_combo)
        config_row.addLayout(dur_group, 1)

        main.addLayout(config_row)

        # === Row 2: Mode (dropdown) ===
        mode_row = QtWidgets.QHBoxLayout()
        mode_row.setSpacing(10)

        mode_label = QtWidgets.QLabel("Mode:")
        mode_label.setStyleSheet("font-weight: bold;")
        mode_row.addWidget(mode_label)

        self.mode_combo = DropDownComboBox()
        fill_combo_from_options(self.mode_combo, SEEDANCE_MODE_OPTIONS)
        self.mode_combo.setCurrentIndex(1)  # Default to Image (first frame)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self.mode_combo.currentIndexChanged.connect(lambda _: self._save_all_state_to_node())
        mode_row.addWidget(self.mode_combo, 1)

        mode_row.addStretch()
        main.addLayout(mode_row)

        # === Row 3: History + Prompt mode ===
        row_hist = QtWidgets.QHBoxLayout()
        row_hist.setSpacing(6)

        self.history_combo = DropDownComboBox()
        self.history_combo.addItem("Select from History...")
        for h in get_history("seedance_prompt_history", scope="project", limit=10):
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
             "Describe the main subject, e.g.: a golden retriever puppy",           False),
            ("Action:",       "action",
             "Describe action, e.g.: running through a field of wildflowers",      False),
            ("Style:",        "style",
             "Visual style, e.g.: cinematic, realistic, anime style",               False),
            ("Camera:",       "camera",
             "(Optional) Camera position & movement, e.g.: low angle tracking",     True),
            ("Composition:",  "composition",
             "(Optional) Composition, e.g.: rule of thirds, centered",              True),
            ("Lens Effects:", "lens",
             "(Optional) Focus & lens effects, e.g.: shallow DOF, bokeh",          True),
            ("Mood:",         "mood",
             "(Optional) Atmosphere & mood, e.g.: warm golden hour lighting",       True),
        ]
        self._std_field_keys = []
        self._std_field_widgets = {}
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
            setattr(self, "_std_{}".format(key), field)

        self._std_prompt_container.setVisible(False)
        main.addWidget(self._std_prompt_container)

        # === Negative Prompt ===
        self._neg_prompt_label = QtWidgets.QLabel("Negative Prompt:")
        self._neg_prompt_label.setStyleSheet(
            "color: #ccc; font-size: 12px; font-weight: bold;")
        main.addWidget(self._neg_prompt_label)

        self.neg_prompt_edit = QtWidgets.QTextEdit()
        self.neg_prompt_edit.setPlaceholderText("(Optional) Content to exclude, e.g.: blurry, distorted, low quality")
        self.neg_prompt_edit.setMinimumHeight(80)
        self.neg_prompt_edit.textChanged.connect(self._save_all_state_to_node)
        main.addWidget(self.neg_prompt_edit)

        # === Omni Reference Panel (visible only in omni_reference mode) ===
        self._omni_container = QtWidgets.QWidget()
        omni_layout = QtWidgets.QVBoxLayout(self._omni_container)
        omni_layout.setSpacing(6)
        omni_layout.setContentsMargins(0, 0, 0, 0)

        omni_title = QtWidgets.QLabel("Omni Reference - Connect up to 9 image inputs + optional video/audio")
        omni_title.setStyleSheet("color: #ff9800; font-size: 12px; font-weight: bold;")
        omni_layout.addWidget(omni_title)

        # Image input hint (images come from Group Input nodes, like VEO Ingredients)
        img_hint = QtWidgets.QLabel(
            "Images: Connect upstream Read/Writer nodes to the inputs on the left.\n"
            "Use @image1~@image9 in your prompt to reference each input.\n"
            "Add or remove input ports with the buttons below."
        )
        img_hint.setStyleSheet(
            "color: #4fc3f7; font-size: 11px; background: #1a2332;"
            " border: 1px dashed #4fc3f7; border-radius: 4px; padding: 6px;"
        )
        img_hint.setWordWrap(True)
        omni_layout.addWidget(img_hint)

        # --- Image input port count controls (+ / -) ---
        ports_row = QtWidgets.QHBoxLayout()
        ports_row.setContentsMargins(0, 2, 0, 2)
        ports_row.setSpacing(6)

        ports_label = QtWidgets.QLabel("Image inputs:")
        ports_label.setStyleSheet("color: #ccc; font-size: 12px; font-weight: bold;")
        ports_row.addWidget(ports_label)

        self._omni_port_count_lbl = QtWidgets.QLabel("1 / 9")
        self._omni_port_count_lbl.setStyleSheet(
            "color: #ff9800; font-size: 12px; font-weight: bold;"
            " padding: 2px 8px; background: #2a2a2a; border-radius: 3px;"
        )
        self._omni_port_count_lbl.setMinimumWidth(52)
        self._omni_port_count_lbl.setAlignment(QtCore.Qt.AlignCenter)
        ports_row.addWidget(self._omni_port_count_lbl)

        self._omni_add_btn = QtWidgets.QPushButton("+ Add")
        self._omni_add_btn.setObjectName("secondaryBtn")
        self._omni_add_btn.setToolTip(
            "Add one more image input port (img2..img9).\n"
            "A new port appears on the left edge of the node, ready to be connected."
        )
        self._omni_add_btn.clicked.connect(self._on_omni_add_input)
        ports_row.addWidget(self._omni_add_btn)

        self._omni_remove_btn = QtWidgets.QPushButton("- Remove Last")
        self._omni_remove_btn.setObjectName("secondaryBtn")
        self._omni_remove_btn.setToolTip(
            "Remove the last (highest-numbered) image input port.\n"
            "The port must be unconnected; disconnect it first if needed."
        )
        self._omni_remove_btn.clicked.connect(self._on_omni_remove_input)
        ports_row.addWidget(self._omni_remove_btn)

        ports_row.addStretch()
        omni_layout.addLayout(ports_row)

        # Video references (video1-video3) - still use file browse (not Input)
        vid_group = QtWidgets.QWidget()
        vid_layout = QtWidgets.QHBoxLayout(vid_group)
        vid_layout.setContentsMargins(0, 2, 0, 0)
        vid_label = QtWidgets.QLabel("Video refs:")
        vid_label.setStyleSheet("color: #aaa; font-size: 11px;")
        vid_layout.addWidget(vid_label)
        self._omni_video_edits = []
        for i in range(1, 4):
            edit = QtWidgets.QLineEdit()
            edit.setPlaceholderText("@video{}".format(i))
            edit.setStyleSheet(
                "background: #2a2a2a; border: 1px solid #444; border-radius: 3px;"
                " color: #ddd; padding: 3px 5px; font-size: 11px;"
            )
            edit.textChanged.connect(self._save_all_state_to_node)
            # Refresh preview rows whenever the path changes.
            edit.textChanged.connect(self._refresh_omni_previews)
            btn = QtWidgets.QPushButton("...")
            btn.setFixedWidth(28)
            btn.setObjectName("secondaryBtn")
            btn.setToolTip("Browse for video file (@video{})".format(i))
            # Use *args so the handler is tolerant of any signal signature
            # (Qt's clicked may emit with or without a bool depending on
            # bindings / checkable state).
            btn.clicked.connect(
                lambda *args, idx=i: self._browse_omni_file(idx, "video")
            )
            if i > 1:
                sep = QtWidgets.QLabel(",")
                sep.setStyleSheet("color: #555;")
                vid_layout.addWidget(sep)
            vid_layout.addWidget(edit, 1)
            vid_layout.addWidget(btn)
            self._omni_video_edits.append(edit)
        omni_layout.addWidget(vid_group)

        # Preview list for video refs (shown only for non-empty paths).
        self._omni_video_preview_container = QtWidgets.QWidget()
        vpl = QtWidgets.QVBoxLayout(self._omni_video_preview_container)
        vpl.setContentsMargins(14, 0, 0, 2)
        vpl.setSpacing(2)
        self._omni_video_preview_rows = []
        for i in range(1, 4):
            row = self._build_omni_preview_row(idx=i, ref_type="video")
            vpl.addWidget(row["widget"])
            self._omni_video_preview_rows.append(row)
        omni_layout.addWidget(self._omni_video_preview_container)

        # Audio references (audio1-audio3) - still use file browse
        aud_group = QtWidgets.QWidget()
        aud_layout = QtWidgets.QHBoxLayout(aud_group)
        aud_layout.setContentsMargins(0, 2, 0, 0)
        aud_label = QtWidgets.QLabel("Audio refs:")
        aud_label.setStyleSheet("color: #aaa; font-size: 11px;")
        aud_layout.addWidget(aud_label)
        self._omni_audio_edits = []
        for i in range(1, 4):
            edit = QtWidgets.QLineEdit()
            edit.setPlaceholderText("@audio{}".format(i))
            edit.setStyleSheet(
                "background: #2a2a2a; border: 1px solid #444; border-radius: 3px;"
                " color: #ddd; padding: 3px 5px; font-size: 11px;"
            )
            edit.textChanged.connect(self._save_all_state_to_node)
            edit.textChanged.connect(self._refresh_omni_previews)
            btn = QtWidgets.QPushButton("...")
            btn.setFixedWidth(28)
            btn.setObjectName("secondaryBtn")
            btn.setToolTip("Browse for audio file (@audio{})".format(i))
            btn.clicked.connect(
                lambda *args, idx=i: self._browse_omni_file(idx, "audio")
            )
            if i > 1:
                sep = QtWidgets.QLabel(",")
                sep.setStyleSheet("color: #555;")
                aud_layout.addWidget(sep)
            aud_layout.addWidget(edit, 1)
            aud_layout.addWidget(btn)
            self._omni_audio_edits.append(edit)
        omni_layout.addWidget(aud_group)

        # Preview list for audio refs (shown only for non-empty paths).
        self._omni_audio_preview_container = QtWidgets.QWidget()
        apl = QtWidgets.QVBoxLayout(self._omni_audio_preview_container)
        apl.setContentsMargins(14, 0, 0, 2)
        apl.setSpacing(2)
        self._omni_audio_preview_rows = []
        for i in range(1, 4):
            row = self._build_omni_preview_row(idx=i, ref_type="audio")
            apl.addWidget(row["widget"])
            self._omni_audio_preview_rows.append(row)
        omni_layout.addWidget(self._omni_audio_preview_container)

        # Prime the preview rows (all empty initially -> all hidden).
        self._refresh_omni_previews()

        self._omni_container.setVisible(False)
        main.addWidget(self._omni_container)

        # === Media path line (for video_extend / audio_drive modes) ===
        self._media_path_container = QtWidgets.QWidget()
        media_layout = QtWidgets.QHBoxLayout(self._media_path_container)
        media_layout.setContentsMargins(0, 0, 0, 0)
        media_layout.setSpacing(6)
        self._media_path_label = QtWidgets.QLabel("Media file:")
        self._media_path_label.setStyleSheet("color: #ccc; font-size: 12px; font-weight: bold;")
        self._media_path_edit = QtWidgets.QLineEdit()
        self._media_path_edit.setPlaceholderText("Path to video/audio file for extend or drive mode...")
        self._media_path_edit.setStyleSheet(
            "background: #2a2a2a; border: 1px solid #444; border-radius: 3px;"
            " color: #ddd; padding: 4px 6px; font-size: 12px;"
        )
        self._media_path_edit.textChanged.connect(self._save_all_state_to_node)
        media_btn = QtWidgets.QPushButton("Browse...")
        media_btn.setObjectName("secondaryBtn")
        media_btn.clicked.connect(self._browse_media_path)
        media_layout.addWidget(self._media_path_label)
        media_layout.addWidget(self._media_path_edit, 1)
        media_layout.addWidget(media_btn)
        self._media_path_container.setVisible(False)
        main.addWidget(self._media_path_container)

        # === Mode info label ===
        self._mode_info_lbl = QtWidgets.QLabel("")
        self._mode_info_lbl.setWordWrap(True)
        self._mode_info_lbl.setStyleSheet("color: #888; font-size: 11px; font-style: italic;")
        self._mode_info_lbl.setVisible(False)
        main.addWidget(self._mode_info_lbl)

        # === Generate Button ===
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

    # --- Prompt mode switching ---

    def _on_prompt_mode_changed(self, index):
        is_standard = (index == 1)
        self.prompt_edit.setVisible(not is_standard)
        self._std_prompt_container.setVisible(is_standard)
        self._neg_prompt_label.setVisible(is_standard)
        self._save_all_state_to_node()

    def _get_assembled_prompt(self):
        if self.prompt_mode_combo.currentIndex() == 0:
            return self.prompt_edit.toPlainText().strip()
        else:
            parts = []
            for key in self._std_field_keys:
                val = self._std_field_widgets[key].text().strip()
                if val:
                    parts.append(val)
            return ", ".join(parts)

    # --- Mode switching ---
    def _on_mode_changed(self, index):
        mode = self.mode_combo.currentData() or SEEDANCE_MODE_TEXT
        print("[Seedance DEBUG] _on_mode_changed: index={} mode={}".format(index, mode))
        self._update_node_inputs(mode)
        self._toggle_mode_panels(mode)


    def _toggle_mode_panels(self, mode):
        """Show/hide UI panels based on selected mode."""
        is_omni = (mode == SEEDANCE_MODE_OMNI_REF)
        is_media = (mode in (SEEDANCE_MODE_VIDEO_EXTEND, SEEDANCE_MODE_AUDIO_DRIVE))

        self._omni_container.setVisible(is_omni)
        self._media_path_container.setVisible(is_media)
        self._mode_info_lbl.setVisible(is_omni or is_media)

        if mode == SEEDANCE_MODE_OMNI_REF:
            self._mode_info_lbl.setText(
                "Omni Ref: Connect up to 9 image inputs on node left edge. Use @image1~@image9 in prompt."
            )
            # Refresh the port count label for the current node.
            self._refresh_omni_port_count()
        elif mode == SEEDANCE_MODE_VIDEO_EXTEND:
            self._mode_info_lbl.setText(
                "Video Extend: Provide a video file to continue generating from its last frame."
            )
        elif mode == SEEDANCE_MODE_AUDIO_DRIVE:
            self._mode_info_lbl.setText(
                "Audio Drive: Generate video driven by audio rhythm / speech sync."
            )
        else:
            self._mode_info_lbl.setVisible(False)

    # --- Omni Reference: manual port management ---

    def _poll_omni_port_state(self):
        """Poll the owning Seedance node's port state and refresh the panel
        when something changes on the DAG (port added/removed, pipe
        connected/disconnected by the user).

        This drives live updates for the "N / 9" label and "- Remove Last"
        enable-state without relying on Nuke's knobChanged (which is flaky
        for PyCustom_Knob-hosted widgets across panel rebuilds).
        """
        # Only relevant in Omni Reference mode; skip otherwise to keep cheap.
        try:
            if self._get_current_mode() != SEEDANCE_MODE_OMNI_REF:
                return
        except Exception:
            return

        node = self._get_owner_node()
        if not node:
            return
        try:
            count = int(node.inputs())
            # Signature: (count, tuple of bools is-connected per port).
            conn_flags = tuple(
                (node.input(i) is not None) for i in range(count)
            )
            sig = (count, conn_flags)
        except Exception:
            return

        if sig == self._omni_last_sig:
            return
        self._omni_last_sig = sig
        self._refresh_omni_port_count()



    def _refresh_omni_port_count(self):
        """Update the 'N / 9' label and enable/disable +/- buttons.

        Port mapping reminder (see seedance_nodes._rebuild_seedance_omni_inputs):
            imgN (rightmost, last-added) -> outer port 0
            img1 (leftmost)              -> outer port (count-1)

        "Remove Last" targets imgN, so its enable-state depends on input(0).
        """
        node = self._get_owner_node()
        if not node or not hasattr(self, "_omni_port_count_lbl"):
            return
        current = int(node.inputs())
        self._omni_port_count_lbl.setText("{} / 9".format(current))
        if hasattr(self, "_omni_add_btn"):
            self._omni_add_btn.setEnabled(current < 9)
        if hasattr(self, "_omni_remove_btn"):
            # imgN (the port we'd remove) lives at outer index 0.
            imgN_connected = (current > 0 and node.input(0) is not None)
            can_remove = (current > 1 and not imgN_connected)
            self._omni_remove_btn.setEnabled(can_remove)
            if current > 1 and imgN_connected:
                self._omni_remove_btn.setToolTip(
                    "img{} is currently connected.\n"
                    "Disconnect img{} (rightmost port on the node) first, "
                    "then this button will activate.".format(current, current)
                )
            else:
                self._omni_remove_btn.setToolTip(
                    "Remove the last (highest-numbered) image input port.\n"
                    "The port must be unconnected; disconnect it first if needed."
                )

    def _on_omni_add_input(self):
        """Handle '+ Add' button: add one more img input port."""
        node = self._get_owner_node()
        if not node:
            return
        if self._get_current_mode() != SEEDANCE_MODE_OMNI_REF:
            return
        new_count = seedance_omni_add_input(node)
        if new_count is not None:
            print("[Seedance] UI: added port, total = {}".format(new_count))
        self._refresh_omni_port_count()

    def _on_omni_remove_input(self):
        """Handle '- Remove Last' button: remove last img input port."""
        node = self._get_owner_node()
        if not node:
            return
        if self._get_current_mode() != SEEDANCE_MODE_OMNI_REF:
            return
        new_count = seedance_omni_remove_input(node)
        if new_count is not None:
            print("[Seedance] UI: removed port, total = {}".format(new_count))
        self._refresh_omni_port_count()

    def _browse_media_path(self):
        """Browse for video/audio file."""
        print("[Seedance] _browse_media_path clicked")
        mode = self._get_current_mode()
        if mode == SEEDANCE_MODE_VIDEO_EXTEND:
            caption = "Select Video File"
            ffilter = "Video Files (*.mp4 *.mov *.avi *.mkv);;All Files (*)"
        elif mode == SEEDANCE_MODE_AUDIO_DRIVE:
            caption = "Select Audio File"
            ffilter = "Audio Files (*.wav *.mp3 *.aac *.m4a *.flac);;All Files (*)"
        else:
            caption = "Select Media File"
            ffilter = "All Files (*)"

        try:
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, caption, "", ffilter
            )
        except Exception as e:
            print("[Seedance] QFileDialog failed: {}".format(e))
            # Fallback to Nuke's native file picker if Qt dialog misbehaves.
            try:
                path = nuke.getFilename(caption) or ""
            except Exception as e2:
                print("[Seedance] nuke.getFilename also failed: {}".format(e2))
                return
        if path:
            self._media_path_edit.setText(path)

    def _browse_omni_file(self, idx, ref_type):
        """Browse for an Omni Reference file (video/audio only; images use Input nodes)."""
        print("[Seedance] _browse_omni_file clicked: idx={} type={}".format(idx, ref_type))
        if ref_type == "video":
            caption = "Select Video @{}".format(idx)
            ffilter = "Video Files (*.mp4 *.mov *.avi *.mkv);;All Files (*)"
        else:  # audio
            caption = "Select Audio @{}".format(idx)
            ffilter = "Audio Files (*.wav *.mp3 *.aac *.m4a *.flac);;All Files (*)"

        try:
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, caption, "", ffilter
            )
        except Exception as e:
            print("[Seedance] QFileDialog failed: {}".format(e))
            # Fallback to Nuke's native picker — it never fails inside Nuke.
            try:
                path = nuke.getFilename(caption) or ""
            except Exception as e2:
                print("[Seedance] nuke.getFilename also failed: {}".format(e2))
                return

        if not path:
            print("[Seedance] user cancelled file dialog")
            return
        if ref_type == "video":
            edits = getattr(self, "_omni_video_edits", [])
            if 0 < idx <= len(edits):
                edits[idx - 1].setText(path)
                print("[Seedance] set @video{} = {}".format(idx, path))
        elif ref_type == "audio":
            edits = getattr(self, "_omni_audio_edits", [])
            if 0 < idx <= len(edits):
                edits[idx - 1].setText(path)
                print("[Seedance] set @audio{} = {}".format(idx, path))

    # ------------------------------------------------------------------ #
    # Omni reference preview rows                                         #
    # ------------------------------------------------------------------ #
    def _build_omni_preview_row(self, idx, ref_type):
        """Build one preview row: [icon] name [Open] [X]. Hidden when empty."""
        icon = "V" if ref_type == "video" else "A"
        w = QtWidgets.QWidget()
        hl = QtWidgets.QHBoxLayout(w)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(6)

        tag = QtWidgets.QLabel("[{} @{}{}]".format(icon, ref_type, idx))
        tag.setStyleSheet(
            "color: #888; font-size: 10px; font-family: Consolas,monospace;"
        )

        name_lbl = QtWidgets.QLabel("")
        name_lbl.setStyleSheet("color: #cfd8dc; font-size: 11px;")
        name_lbl.setTextInteractionFlags(
            QtCore.Qt.TextSelectableByMouse
        )
        name_lbl.setToolTip("")
        # Elide long names so the row never overflows.
        try:
            name_lbl.setSizePolicy(
                QtWidgets.QSizePolicy.Ignored,
                QtWidgets.QSizePolicy.Preferred,
            )
        except Exception:
            pass

        open_btn = QtWidgets.QPushButton("Open")
        open_btn.setFixedHeight(20)
        open_btn.setObjectName("secondaryBtn")
        open_btn.setToolTip("Open this file with the system's default app")
        open_btn.clicked.connect(
            lambda *args, i=idx, t=ref_type: self._open_omni_ref(i, t)
        )

        clear_btn = QtWidgets.QPushButton("x")
        clear_btn.setFixedSize(20, 20)
        clear_btn.setObjectName("secondaryBtn")
        clear_btn.setToolTip("Remove this reference")
        clear_btn.clicked.connect(
            lambda *args, i=idx, t=ref_type: self._clear_omni_ref(i, t)
        )

        hl.addWidget(tag)
        hl.addWidget(name_lbl, 1)
        hl.addWidget(open_btn)
        hl.addWidget(clear_btn)
        w.setVisible(False)
        return {"widget": w, "name": name_lbl, "open": open_btn}

    def _refresh_omni_previews(self, *_args):
        """Sync preview rows with the current @video/@audio edit paths."""
        def _apply(edits, rows):
            for i, edit in enumerate(edits):
                if i >= len(rows):
                    break
                row = rows[i]
                path = (edit.text() or "").strip()
                if not path:
                    row["widget"].setVisible(False)
                    row["name"].setText("")
                    row["name"].setToolTip("")
                    continue
                base = os.path.basename(path) or path
                exists = os.path.exists(path)
                color = "#cfd8dc" if exists else "#e57373"
                suffix = "" if exists else "  (missing)"
                row["name"].setStyleSheet(
                    "color: {}; font-size: 11px;".format(color)
                )
                row["name"].setText(base + suffix)
                row["name"].setToolTip(path)
                row["open"].setEnabled(exists)
                row["widget"].setVisible(True)

        _apply(
            getattr(self, "_omni_video_edits", []),
            getattr(self, "_omni_video_preview_rows", []),
        )
        _apply(
            getattr(self, "_omni_audio_edits", []),
            getattr(self, "_omni_audio_preview_rows", []),
        )

    def _open_omni_ref(self, idx, ref_type):
        """Open the referenced file with the OS default application."""
        edits = (
            getattr(self, "_omni_video_edits", [])
            if ref_type == "video"
            else getattr(self, "_omni_audio_edits", [])
        )
        if not (0 < idx <= len(edits)):
            return
        path = (edits[idx - 1].text() or "").strip()
        if not path:
            return
        if not os.path.exists(path):
            print("[Seedance] file not found: {}".format(path))
            nuke.message("File not found:\n{}".format(path))
            return
        self._open_path_externally(path)

    def _clear_omni_ref(self, idx, ref_type):
        """Clear a given @video/@audio slot."""
        edits = (
            getattr(self, "_omni_video_edits", [])
            if ref_type == "video"
            else getattr(self, "_omni_audio_edits", [])
        )
        if 0 < idx <= len(edits):
            edits[idx - 1].setText("")

    def _open_path_externally(self, path):
        """Open *path* using the platform's default handler."""
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
            print("[Seedance] opened {}".format(path))
        except Exception as e:
            print("[Seedance] open failed: {}".format(e))
            # Last-ditch fallback via Qt (works when a desktop service is registered).
            try:
                from PySide2.QtCore import QUrl
                from PySide2.QtGui import QDesktopServices
                QDesktopServices.openUrl(QUrl.fromLocalFile(path))
            except Exception as e2:
                print("[Seedance] QDesktopServices fallback failed: {}".format(e2))
                nuke.message("Could not open file:\n{}".format(path))

    def _get_current_mode(self):
        return self.mode_combo.currentData() or SEEDANCE_MODE_TEXT

    def _update_node_inputs(self, mode):
        """Dynamically update the Seedance node's internal Input count."""
        node = self._get_owner_node()
        if not node:
            return

        needed = SEEDANCE_MODE_INPUT_COUNTS.get(mode, 0)

        _INPUT_NAMES = {
            SEEDANCE_MODE_TEXT: [],
            SEEDANCE_MODE_IMAGE: ["FirstFrame"],
            SEEDANCE_MODE_FRAMES: ["FirstFrame", "EndFrame"],
            SEEDANCE_MODE_OMNI_REF: ["img1", "img2", "img3", "img4", "img5", "img6", "img7", "img8", "img9"],
            SEEDANCE_MODE_VIDEO_EXTEND: ["VideoIn"],
            SEEDANCE_MODE_AUDIO_DRIVE: ["AudioIn"],
        }
        names = _INPUT_NAMES.get(mode, [])

        # Omni Reference uses MANUAL port management via "+ Add" / "- Remove"
        # buttons on the panel. Don't create all 9 ports upfront (they're
        # visually cramped and hard to target).
        #
        # When entering omni_reference mode from a DIFFERENT mode, initialize
        # to (connected + 1) or 1 ports — just enough for the user to start
        # connecting. If we're already in omni mode (re-entering, e.g. on
        # restore), preserve whatever port count the user already set.
        is_omni_layout = (mode == SEEDANCE_MODE_OMNI_REF)
        if is_omni_layout:
            # Detect existing img* ports inside the group.
            node.begin()
            try:
                inner_inputs = list(nuke.allNodes("Input"))
            finally:
                node.end()
            has_img_ports = any(
                re.match(r"^img\d+$", n.name()) for n in inner_inputs
            )
            if has_img_ports:
                # Already in omni layout — keep the user's chosen port count.
                needed = len([n for n in inner_inputs if re.match(r"^img\d+$", n.name())])
                needed = max(1, min(needed, 9))
            else:
                # Entering omni from a DIFFERENT mode (text / image / frames / ...).
                # Preferred UX: one port per existing live connection (so img1
                # inherits the previous FirstFrame, img2 inherits EndFrame, etc.)
                # — and NOT one extra empty port. The user explicitly adds more
                # via "+ Add". Fall back to 1 when nothing is connected yet.
                existing_connected = sum(
                    1 for i in range(node.inputs()) if node.input(i) is not None
                )
                needed = max(1, min(existing_connected, 9)) if existing_connected > 0 else 1
            names = names[:needed]

        # Use VEO-style fixed spacing=200 for ALL modes. Smaller xpos gaps (e.g.
        # 40px) confuse Nuke's external port ordering on Group nodes.
        spacing = 200
        center_offset = 0  # VEO layout: leftmost at xpos=0, rightmost at (count-1)*spacing


        def _debug_dump(stage):
            try:
                owner = self._get_owner_node()
                print("[Seedance DEBUG] {}: mode={} needed={} owner={}".format(
                    stage, mode, needed, owner.name() if owner else "None"))
                if owner:
                    max_inputs = max(owner.inputs(), needed)
                    for idx in range(max_inputs):
                        conn = owner.input(idx)
                        print("[Seedance DEBUG]   outer input({}) <- {}".format(
                            idx, conn.name() if conn else "None"))
                for idx, inp_node in enumerate(sorted(nuke.allNodes("Input"), key=lambda n: int(n["xpos"].value()))):
                    print("[Seedance DEBUG]   inner #{} name={} number={} xpos={}".format(
                        idx,
                        inp_node.name(),
                        int(inp_node["number"].value()) if "number" in inp_node.knobs() else -1,
                        int(inp_node["xpos"].value())
                    ))
            except Exception as dbg_e:
                print("[Seedance DEBUG] {} dump failed: {}".format(stage, dbg_e))

        node.begin()
        existing_inputs = [n for n in nuke.allNodes("Input")]
        current_count = len(existing_inputs)
        existing_names = [n.name() for n in sorted(existing_inputs, key=lambda n: int(n["xpos"].value()))]
        need_rebuild = (needed != current_count)
        if not need_rebuild and needed > 0 and existing_names != names:
            need_rebuild = True

        print("[Seedance DEBUG] _update_node_inputs: mode={} needed={} current={} need_rebuild={} is_omni_layout={} names={} existing_names={}".format(
            mode, needed, current_count, need_rebuild, is_omni_layout, names, existing_names))
        _debug_dump("before rebuild")

        if need_rebuild and needed > 0:
            saved = {}
            # When switching INTO omni layout from another mode (image/frames/...),
            # the old label names (FirstFrame, EndFrame, ...) won't match the new
            # img1/img2/... labels, so a label-based saved/restore drops every
            # connection. Track outer-port -> upstream separately so we can
            # remap by logical position (leftmost old conn -> img1, etc.).
            saved_by_old_port = {}
            if current_count > 0:
                old_names = sorted(existing_inputs, key=lambda n: int(n["xpos"].value()))
                for k, inp_node in enumerate(old_names):
                    if "number" in inp_node.knobs():
                        old_port = int(inp_node["number"].value())
                    else:
                        old_port = current_count - 1 - k
                    conn = node.input(old_port) if 0 <= old_port < current_count else None
                    if conn is not None:
                        logical_name = inp_node.name()
                        saved[logical_name] = conn
                        # xpos-order index 0 == leftmost inner Input == img1 slot
                        # after the rebuild, so we key by that logical position.
                        saved_by_old_port[k] = conn
                        print("[Seedance DEBUG]   save {} <- {} (old_port={} logical_idx={})".format(
                            logical_name, conn.name(), old_port, k))

            for inp in list(nuke.allNodes("Input")):
                print("[Seedance DEBUG]   delete inner input {}".format(inp.name()))
                nuke.delete(inp)

            # Detach every external connection BEFORE leaving begin()/creating
            # new Inputs. If we don't, Nuke keeps a "ghost" port on the node's
            # outer input strip that survives the Input-node rebuild and shows
            # up as an unlabeled stub arrow in the DAG (same root cause as the
            # bug fixed in _rebuild_seedance_omni_inputs).
            node.end()
            try:
                prior_outer = int(node.inputs())
                for i in range(prior_outer):
                    try:
                        node.setInput(i, None)
                    except Exception:
                        pass
            finally:
                node.begin()

            # VEO-style rebuild: reverse creation order + number knob + xpos=(i-1)*200.
            # This is IDENTICAL to VEO Ingredients, which is the proven correct layout.
            for i in range(needed, 0, -1):
                inp = nuke.nodes.Input()
                label = names[i - 1]
                xpos = int(round((i - 1) * spacing - center_offset))
                inp.setName(label)
                inp["number"].setValue(needed - i)
                inp["xpos"].setValue(xpos)
                inp["ypos"].setValue(0)
                print("[Seedance DEBUG]   create {} number={} xpos={}".format(
                    label, needed - i, xpos))
            node.end()

            set_indices = set()
            # First pass: same-label restore (omni->omni, image->image, ...).
            for k, label in enumerate(names):
                if label in saved:
                    new_port = needed - 1 - k
                    group_ref = self._get_owner_node()
                    if group_ref:
                        group_ref.setInput(new_port, saved[label])
                        set_indices.add(new_port)
                        print("[Seedance DEBUG]   restore {} -> input({}) from {} (by label)".format(
                            label, new_port, saved[label].name()))

            # Second pass: cross-mode positional remap. For any new label (img1,
            # img2, ...) whose name wasn't in `saved`, fall back to the old
            # connection at the same logical (leftmost-first) index. This is
            # what makes "Image mode FirstFrame -> img1" when switching into
            # Omni Reference.
            for k, label in enumerate(names):
                new_port = needed - 1 - k
                if new_port in set_indices:
                    continue
                if k in saved_by_old_port:
                    group_ref = self._get_owner_node()
                    if group_ref:
                        group_ref.setInput(new_port, saved_by_old_port[k])
                        set_indices.add(new_port)
                        print("[Seedance DEBUG]   restore {} -> input({}) from {} (by position, cross-mode)".format(
                            label, new_port, saved_by_old_port[k].name()))

            for i in range(needed):
                if i not in set_indices:
                    group_ref = self._get_owner_node()
                    if group_ref:
                        group_ref.setInput(i, None)
                        print("[Seedance DEBUG]   clear input({})".format(i))

            node.begin()
            _debug_dump("after rebuild")
            node.end()
            return

        elif needed == 0 and current_count > 0:
            for inp in list(nuke.allNodes("Input")):
                print("[Seedance DEBUG]   delete inner input {}".format(inp.name()))
                nuke.delete(inp)
            _debug_dump("after clear")
            node.end()
            return

        _debug_dump("no rebuild")
        node.end()


    # --- History ---
    def _on_history_select(self, index):
        if index <= 0:
            return
        full_text = self.history_combo.itemData(index)
        if not full_text:
            return
        self.prompt_edit.setText(full_text)
        self._add_to_history(full_text)
        self.history_combo.blockSignals(True)
        self.history_combo.setCurrentIndex(0)
        self.history_combo.blockSignals(False)

    def _add_to_history(self, prompt):
        if not prompt:
            return
        push_history_item("seedance_prompt_history", prompt, scope="project", limit=10)
        self._refresh_history_combo(get_history("seedance_prompt_history", scope="project", limit=10))

    def _clear_history(self):
        set_history("seedance_prompt_history", [], scope="project", limit=10)
        self._refresh_history_combo([])

    def _refresh_history_combo(self, history):
        self.history_combo.blockSignals(True)
        self.history_combo.clear()
        self.history_combo.addItem("Select from History...")
        for h in history:
            display = h[:40] + "..." if len(h) > 40 else h
            self.history_combo.addItem(display, h)
        self.history_combo.blockSignals(False)

    def _get_owner_node(self):
        try:
            if self._node is not None and self._node.name():
                return self._node
        except Exception:
            pass
        try:
            return nuke.thisNode()
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Ensure hidden knobs on the owning node
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
        node = self._node
        if node is None:
            return
        try:
            _ = node.name()
        except Exception:
            return
        try:
            self._ensure_int_knob(node, "sd_s_model", "s_model")
            self._ensure_int_knob(node, "sd_s_ratio", "s_ratio")
            self._ensure_int_knob(node, "sd_s_res", "s_res")
            self._ensure_int_knob(node, "sd_s_dur", "s_dur")
            self._ensure_int_knob(node, "sd_s_mode", "s_mode")
            self._ensure_int_knob(node, "sd_s_pm", "s_pm")
            self._ensure_text_knob(node, "sd_s_prompt", "s_prompt")
            self._ensure_text_knob(node, "sd_s_neg", "s_neg")
            self._ensure_text_knob(node, "sd_s_stdfields", "s_stdfields")
            self._ensure_text_knob(node, "sd_s_omni_videos", "s_omni_videos")
            self._ensure_text_knob(node, "sd_s_omni_audio", "s_omni_audio")
            self._ensure_text_knob(node, "sd_s_media_path", "s_media_path")

            node["sd_s_model"].setValue(self.model_combo.currentIndex())
            node["sd_s_ratio"].setValue(self.ratio_combo.currentIndex())
            node["sd_s_res"].setValue(self.res_combo.currentIndex())
            node["sd_s_dur"].setValue(self.dur_combo.currentIndex())
            node["sd_s_mode"].setValue(self.mode_combo.currentIndex())
            node["sd_s_pm"].setValue(self.prompt_mode_combo.currentIndex())
            node["sd_s_prompt"].setValue(self.prompt_edit.toPlainText())
            node["sd_s_neg"].setValue(self.neg_prompt_edit.toPlainText())

            std_vals = "|".join(
                self._std_field_widgets[k].text() for k in self._std_field_keys
            )
            node["sd_s_stdfields"].setValue(std_vals)

            # Save omni video/audio paths (images come from Input nodes, no need to save)
            if hasattr(self, "_omni_video_edits"):
                omni_vid_vals = "|".join(e.text() for e in self._omni_video_edits)
                node["sd_s_omni_videos"].setValue(omni_vid_vals)
            if hasattr(self, "_omni_audio_edits"):
                omni_aud_vals = "|".join(e.text() for e in self._omni_audio_edits)
                node["sd_s_omni_audio"].setValue(omni_aud_vals)

            # Save media path
            node["sd_s_media_path"].setValue(self._media_path_edit.text())
        except Exception as e:
            print("[Seedance] _save_all_state_to_node error: {}".format(e))

    def _restore_all_state_from_node(self):
        node = self._node
        if node is None:
            return
        try:
            _ = node.name()
        except Exception:
            return
        try:
            if "sd_s_model" not in node.knobs() and "sd_s_mode" not in node.knobs():
                print("[Seedance] No saved state found on node '{}'".format(node.name()))
                return

            print("[Seedance] Restoring state from node '{}'".format(node.name()))

            widgets = [self.model_combo, self.ratio_combo, self.res_combo,
                       self.dur_combo, self.mode_combo, self.prompt_mode_combo,
                       self.prompt_edit, self.neg_prompt_edit]
            std_widgets = list(self._std_field_widgets.values())
            for w in widgets + std_widgets:
                w.blockSignals(True)

            if "sd_s_model" in node.knobs():
                idx = int(node["sd_s_model"].value())
                if 0 <= idx < self.model_combo.count():
                    self.model_combo.setCurrentIndex(idx)
            if "sd_s_ratio" in node.knobs():
                idx = int(node["sd_s_ratio"].value())
                if 0 <= idx < self.ratio_combo.count():
                    self.ratio_combo.setCurrentIndex(idx)
            if "sd_s_res" in node.knobs():
                idx = int(node["sd_s_res"].value())
                if 0 <= idx < self.res_combo.count():
                    self.res_combo.setCurrentIndex(idx)
            if "sd_s_dur" in node.knobs():
                idx = int(node["sd_s_dur"].value())
                if 0 <= idx < self.dur_combo.count():
                    self.dur_combo.setCurrentIndex(idx)
            if "sd_s_mode" in node.knobs():
                idx = int(node["sd_s_mode"].value())
                print("[Seedance DEBUG] restore mode index={}".format(idx))
                if 0 <= idx < self.mode_combo.count():
                    self.mode_combo.setCurrentIndex(idx)
            if "sd_s_pm" in node.knobs():
                idx = int(node["sd_s_pm"].value())
                if 0 <= idx < self.prompt_mode_combo.count():
                    self.prompt_mode_combo.setCurrentIndex(idx)
            if "sd_s_prompt" in node.knobs():
                prompt = node["sd_s_prompt"].value()
                if prompt:
                    self.prompt_edit.setText(prompt)
            if "sd_s_neg" in node.knobs():
                neg = node["sd_s_neg"].value()
                if neg:
                    self.neg_prompt_edit.setText(neg)
            if "sd_s_stdfields" in node.knobs():
                raw = node["sd_s_stdfields"].value()
                if raw:
                    vals = raw.split("|")
                    for i, key in enumerate(self._std_field_keys):
                        if i < len(vals):
                            self._std_field_widgets[key].setText(vals[i])

            # Restore Omni Reference video/audio paths (images from Input nodes)
            if "sd_s_omni_videos" in node.knobs() and hasattr(self, "_omni_video_edits"):
                raw = node["sd_s_omni_videos"].value()
                if raw:
                    vals = raw.split("|")
                    for i, edit in enumerate(self._omni_video_edits):
                        if i < len(vals) and vals[i]:
                            edit.setText(vals[i])
            if "sd_s_omni_audio" in node.knobs() and hasattr(self, "_omni_audio_edits"):
                raw = node["sd_s_omni_audio"].value()
                if raw:
                    vals = raw.split("|")
                    for i, edit in enumerate(self._omni_audio_edits):
                        if i < len(vals) and vals[i]:
                            edit.setText(vals[i])

            # Restore media path
            if "sd_s_media_path" in node.knobs():
                path = node["sd_s_media_path"].value()
                if path:
                    self._media_path_edit.setText(path)

            for w in widgets + std_widgets:
                w.blockSignals(False)

            mode = self.mode_combo.currentData() or SEEDANCE_MODE_TEXT
            self._update_node_inputs(mode)
            self._toggle_mode_panels(mode)

            pm_idx = self.prompt_mode_combo.currentIndex()
            self._on_prompt_mode_changed(pm_idx)

            print("[Seedance] State restored successfully")

        except Exception as e:
            print("[Seedance] _restore_all_state_from_node error: {}".format(e))

    # ------------------------------------------------------------------
    # Lifecycle hooks
    # ------------------------------------------------------------------
    def hideEvent(self, event):
        self._save_all_state_to_node()
        super(SeedanceWidget, self).hideEvent(event)

    def closeEvent(self, event):
        self._save_all_state_to_node()
        super(SeedanceWidget, self).closeEvent(event)

    def event(self, ev):
        if ev.type() == QtCore.QEvent.DeferredDelete:
            self._save_all_state_to_node()
        return super(SeedanceWidget, self).event(ev)

    # ------------------------------------------------------------------
    # Generate
    # ------------------------------------------------------------------
    def _start_generate(self):
        if not self.settings.api_key:
            self.status_label.setStyleSheet("color: #ef4444; font-size: 11px;")
            self.status_label.setText("Please set API key in Settings")
            nuke.message("API key not set.\nPlease open AI Workflow > Setting in the toolbar.")
            return

        if self.current_worker and self.current_worker.is_running:
            worker = self.current_worker
            worker.stop()
            try:
                worker.finished.disconnect()
                worker.error.disconnect()
                worker.status_update.disconnect()
                worker.progress_update.disconnect()
            except (RuntimeError, TypeError):
                pass
            if hasattr(self, '_status_task_id') and self._status_task_id:
                try:
                    from ai_workflow.status_bar import task_progress_manager
                    task_progress_manager.cancel_task(
                        self._status_task_id, "Cancelled by user")
                except Exception:
                    pass
                self._status_task_id = None
            worker_id = id(worker)
            _cleanup_timer = QtCore.QTimer()
            _cleanup_timer.setInterval(500)
            def _poll_thread_exit():
                if not worker.isRunning():
                    _cleanup_timer.stop()
                    _seedance_active_workers.pop(worker_id, None)
            _cleanup_timer.timeout.connect(_poll_thread_exit)
            _cleanup_timer.start()
            _seedance_active_workers.setdefault(worker_id, {})["_cleanup_timer"] = _cleanup_timer
            self.current_worker = None
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
            self.status_label.setText("Seedance_Generate node not found")
            return

        input_dir = get_input_directory()
        output_dir = get_output_directory()
        gen_name = node.name()
        current_mode = self._get_current_mode()

        self.status_label.setStyleSheet("color: #facc15; font-size: 11px;")
        self.status_label.setText("Collecting inputs...")

        reference_image_paths = []
        input_count = SEEDANCE_MODE_INPUT_COUNTS.get(current_mode, 0)

        _INPUT_NAMES = {
            SEEDANCE_MODE_TEXT: [],
            SEEDANCE_MODE_IMAGE: ["FirstFrame"],
            SEEDANCE_MODE_FRAMES: ["FirstFrame", "EndFrame"],
            SEEDANCE_MODE_OMNI_REF: ["img1", "img2", "img3", "img4", "img5", "img6", "img7", "img8", "img9"],
            SEEDANCE_MODE_VIDEO_EXTEND: ["VideoIn"],
            SEEDANCE_MODE_AUDIO_DRIVE: ["AudioIn"],
        }
        names = _INPUT_NAMES.get(current_mode, [])

        # For omni_reference: use the node's ACTUAL port count (dynamically grown)
        # rather than the theoretical max of 9. This matches how many img ports
        # the user currently sees and may have connected.
        if current_mode == SEEDANCE_MODE_OMNI_REF:
            input_count = node.inputs()
            names = names[:input_count]

        # Collect image inputs from node Input ports (like VEO Ingredients)
        # For omni_reference, collect all connected image inputs as references
        omni_image_count = input_count if current_mode == SEEDANCE_MODE_OMNI_REF else input_count
        effective_count = min(input_count, len(names)) if names else 0

        for k in range(effective_count):
            port_idx = effective_count - 1 - k
            label = names[k] if k < len(names) else "input{}".format(k + 1)
            inp_ref = node.input(port_idx)
            if inp_ref:
                frame_idx = k + 1
                path = os.path.join(input_dir, "{}_{}_frame{}.png".format(
                    gen_name, label.replace("/", "_"), frame_idx))
                if render_input_to_file_silent(inp_ref, path, nuke.frame()):
                    if current_mode == SEEDANCE_MODE_VIDEO_EXTEND:
                        # Video extend: use as video path directly (not image)
                        pass
                    else:
                        reference_image_paths.append(path)
                else:
                    nuke.message("Error: Failed to render {}.".format(label))
                    self.status_label.setText("Error: {} render failed".format(label))
                    self._toggle_stop_ui(False)
                    return

        # Collect Omni Reference paths
        omni_references = {}
        if current_mode == SEEDANCE_MODE_OMNI_REF:
            # Images come from Input ports (already rendered into reference_image_paths)
            for i, img_path in enumerate(reference_image_paths, 1):
                omni_references["image{}".format(i)] = img_path
            # Video/audio from file browse fields
            for i, edit in enumerate(getattr(self, "_omni_video_edits", []), 1):
                p = edit.text().strip()
                if p and os.path.exists(p):
                    omni_references["video{}".format(i)] = p
            for i, edit in enumerate(getattr(self, "_omni_audio_edits", []), 1):
                p = edit.text().strip()
                if p and os.path.exists(p):
                    omni_references["audio{}".format(i)] = p

        # Collect media path for video_extend / audio_drive
        media_path = None
        if current_mode in (SEEDANCE_MODE_VIDEO_EXTEND, SEEDANCE_MODE_AUDIO_DRIVE):
            media_path = self._media_path_edit.text().strip()
            if not media_path or not os.path.exists(media_path):
                nuke.message("Error: Please specify a valid {} file.".format(
                    "video" if current_mode == SEEDANCE_MODE_VIDEO_EXTEND else "audio"))
                self.status_label.setText("Error: Media file required")
                self._toggle_stop_ui(False)
                return

        model_name = self.model_combo.currentText()
        ratio = self.ratio_combo.currentText()
        duration = self.dur_combo.currentData() or self.dur_combo.currentText()
        resolution = self.res_combo.currentText().lower()

        self._toggle_stop_ui(True)
        self.status_label.setText("Starting generation...")

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

        worker = SeedanceWorker(
            api_key=self.settings.api_key,
            prompt=prompt,
            reference_image_paths=reference_image_paths,
            model=model_name,
            aspect_ratio=ratio,
            duration=duration,
            resolution=resolution,
            mode=current_mode,
            negative_prompt=neg_prompt,
            omni_references=omni_references if current_mode == SEEDANCE_MODE_OMNI_REF else None,
            media_path=media_path,
            temp_dir=output_dir,
            gen_name=gen_name,
        )
        self.current_worker = worker

        worker_id = id(worker)
        _seedance_active_workers[worker_id] = {"worker": worker, "params": gen_params}

        widget_ref = self

        try:
            from ai_workflow.status_bar import task_progress_manager
            status_task_id = task_progress_manager.add_task(
                node.name() if node else "Seedance", "video")
            self._status_task_id = status_task_id
            worker.status_update.connect(
                lambda s: task_progress_manager.update_status(status_task_id, s))
            worker.progress_update.connect(
                lambda v: task_progress_manager.update_status(status_task_id, progress=v))
        except Exception:
            status_task_id = None
            self._status_task_id = None

        def _on_finished(path, metadata):
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
                        viewer_node, internal_read = create_seedance_viewer_node(
                            params["generator_node"],
                            params["prompt"],
                            params["ratio"],
                            params["duration"],
                            path,
                            reference_image_paths=params.get("reference_image_paths"),
                            model=params.get("model", "doubao-seedance-2-0-260128"),
                            resolution=params.get("resolution", "720p"),
                            mode=params.get("mode", SEEDANCE_MODE_TEXT),
                            negative_prompt=params.get("negative_prompt", ""),
                        )
                        if viewer_node:
                            try:
                                nuke.connectViewer(0, viewer_node)
                            except:
                                pass
                    except Exception as e:
                        import traceback
                        print("Seedance: ERROR in _create_nodes: {}".format(e))
                        traceback.print_exc()
                    finally:
                        _seedance_active_workers.pop(worker_id, None)
                nuke.executeInMainThread(_create_nodes)
            else:
                _seedance_active_workers.pop(worker_id, None)
                nuke.executeInMainThread(
                    nuke.message,
                    args=("Video generation completed but no file was created.\nPath: {}".format(path),)
                )

        def _on_error(err):
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
            _seedance_active_workers.pop(worker_id, None)
            nuke.executeInMainThread(nuke.message, args=("Seedance Error:\n{}".format(err),))

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
            self.gen_btn.setStyleSheet("")
            self.gen_btn.style().unpolish(self.gen_btn)
            self.gen_btn.style().polish(self.gen_btn)
            self.pbar.setValue(0)
            self.pbar.setVisible(True)
        else:
            self.gen_btn.setText("GENERATE VIDEO")
            self.gen_btn.setObjectName("generateBtn")
            self.gen_btn.setStyleSheet("")
            self.gen_btn.style().unpolish(self.gen_btn)
            self.gen_btn.style().polish(self.gen_btn)
            self.current_worker = None
            self.pbar.setVisible(False)
            self.pbar.reset()


# ---------------------------------------------------------------------------
# Seedance Record Widget (read-only record + editable regeneration UI)
# ---------------------------------------------------------------------------
class SeedanceRecordWidget(QtWidgets.QWidget):
    """Widget for Seedance video record nodes."""

    def __init__(self, node, parent=None):
        super(SeedanceRecordWidget, self).__init__(parent)
        self.node = node
        self.setObjectName("nanoBananaRoot")
        self.setStyleSheet(SEEDANCE_STYLE)
        self.setMinimumWidth(380)
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

        header = QtWidgets.QLabel("Seedance Video Record (Read Only)")
        header.setStyleSheet("color: #FF6347; font-weight: bold; font-size: 12px; background: transparent;")
        header.setAlignment(QtCore.Qt.AlignCenter)
        record_layout.addWidget(header)

        info_style = "color: #ccc; font-size: 11px; background: transparent;"
        label_style = "color: #888; font-size: 10px; font-weight: bold; background: transparent;"
        value_style = "color: #ccc; font-size: 11px; background: transparent; padding: 0px;"

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
            if col_idx < len(fields) - 1:
                sep = QtWidgets.QFrame()
                sep.setFrameShape(QtWidgets.QFrame.VLine)
                sep.setStyleSheet("color: #444;")
                info_row.addWidget(sep)

        record_layout.addLayout(info_row)

        prompt_label = QtWidgets.QLabel("Prompt:")
        prompt_label.setStyleSheet(label_style)
        record_layout.addWidget(prompt_label)
        self.prompt_display = QtWidgets.QPlainTextEdit()
        self.prompt_display.setReadOnly(True)
        self.prompt_display.setMaximumHeight(80)
        self.prompt_display.setStyleSheet("background: #2a2a2a; border: 1px solid #444; color: #ccc; font-size: 11px;")
        record_layout.addWidget(self.prompt_display)

        neg_label = QtWidgets.QLabel("Negative Prompt:")
        neg_label.setStyleSheet(label_style)
        record_layout.addWidget(neg_label)
        self.neg_prompt_display = QtWidgets.QPlainTextEdit()
        self.neg_prompt_display.setReadOnly(True)
        self.neg_prompt_display.setMaximumHeight(50)
        self.neg_prompt_display.setStyleSheet("background: #2a2a2a; border: 1px solid #444; color: #ccc; font-size: 11px;")
        record_layout.addWidget(self.neg_prompt_display)

        self.cached_info_label = QtWidgets.QLabel("")
        self.cached_info_label.setVisible(False)
        record_layout.addWidget(self.cached_info_label)

        self.read_node_label = QtWidgets.QLabel("")
        self.read_node_label.setVisible(False)
        record_layout.addWidget(self.read_node_label)

        main.addWidget(record_frame)

        # ================================================================
        # Divider + Regenerate Header
        # ================================================================
        divider_line = QtWidgets.QFrame()
        divider_line.setFrameShape(QtWidgets.QFrame.HLine)
        divider_line.setStyleSheet("color: #555;")
        main.addWidget(divider_line)

        regen_header = QtWidgets.QLabel("Regenerate (edit params below)")
        regen_header.setStyleSheet("color: #facc15; font-weight: bold; font-size: 12px;")
        regen_header.setAlignment(QtCore.Qt.AlignCenter)
        main.addWidget(regen_header)

        # ================================================================
        # BOTTOM SECTION: Editable parameters for regeneration
        # ================================================================

        config_row = QtWidgets.QHBoxLayout()
        config_row.setSpacing(12)

        model_group = QtWidgets.QVBoxLayout()
        model_group.setSpacing(2)
        model_lbl = QtWidgets.QLabel("Model:")
        model_lbl.setStyleSheet("color: #aaa; font-size: 11px;")
        model_group.addWidget(model_lbl)
        self.model_combo = DropDownComboBox()
        fill_combo_from_options(self.model_combo, SEEDANCE_MODEL_OPTIONS)
        model_group.addWidget(self.model_combo)
        config_row.addLayout(model_group, 2)

        ratio_group = QtWidgets.QVBoxLayout()
        ratio_group.setSpacing(2)
        ratio_lbl = QtWidgets.QLabel("Aspect ratio:")
        ratio_lbl.setStyleSheet("color: #aaa; font-size: 11px;")
        ratio_group.addWidget(ratio_lbl)
        self.ratio_combo = DropDownComboBox()
        fill_combo_from_options(self.ratio_combo, SEEDANCE_RATIO_OPTIONS)
        ratio_group.addWidget(self.ratio_combo)
        config_row.addLayout(ratio_group, 1)

        res_group = QtWidgets.QVBoxLayout()
        res_group.setSpacing(2)
        res_lbl = QtWidgets.QLabel("Resolution:")
        res_lbl.setStyleSheet("color: #aaa; font-size: 11px;")
        res_group.addWidget(res_lbl)
        self.res_combo = DropDownComboBox()
        fill_combo_from_options(self.res_combo, SEEDANCE_RESOLUTION_OPTIONS)
        self.res_combo.setCurrentIndex(1)
        res_group.addWidget(self.res_combo)
        config_row.addLayout(res_group, 1)

        dur_group = QtWidgets.QVBoxLayout()
        dur_group.setSpacing(2)
        dur_lbl = QtWidgets.QLabel("Duration:")
        dur_lbl.setStyleSheet("color: #aaa; font-size: 11px;")
        dur_group.addWidget(dur_lbl)
        self.dur_combo = DropDownComboBox()
        fill_combo_from_options(self.dur_combo, SEEDANCE_DURATION_OPTIONS)
        self.dur_combo.setCurrentIndex(1)
        dur_group.addWidget(self.dur_combo)
        config_row.addLayout(dur_group, 1)

        main.addLayout(config_row)

        # Mode
        mode_row = QtWidgets.QHBoxLayout()
        mode_row.setSpacing(10)
        mode_label = QtWidgets.QLabel("Mode:")
        mode_label.setStyleSheet("font-weight: bold;")
        mode_row.addWidget(mode_label)
        self.mode_combo = DropDownComboBox()
        fill_combo_from_options(self.mode_combo, SEEDANCE_MODE_OPTIONS)
        self.mode_combo.setCurrentIndex(1)
        mode_row.addWidget(self.mode_combo, 1)
        mode_row.addStretch()
        main.addLayout(mode_row)

        # Editable Prompt
        self.prompt_edit = QtWidgets.QTextEdit()
        self.prompt_edit.setPlaceholderText("Edit prompt and regenerate...")
        self.prompt_edit.setMinimumHeight(120)
        main.addWidget(self.prompt_edit)

        # Editable Negative Prompt
        self.neg_prompt_edit = QtWidgets.QTextEdit()
        self.neg_prompt_edit.setPlaceholderText("Negative prompt (optional)...")
        self.neg_prompt_edit.setFixedHeight(70)
        main.addWidget(self.neg_prompt_edit)

        # Image Reference Strip
        from ai_workflow.gemini_chat import ImageStrip
        self._ref_image_strip = ImageStrip(add_callback=self._add_ref_image)
        self._ref_image_strip.imagesChanged.connect(self._save_ref_images_to_node)
        main.addWidget(self._ref_image_strip)

        # Regenerate Button
        self.regen_btn = QtWidgets.QPushButton("REGENERATE VIDEO")
        self.regen_btn.setObjectName("regenerateBtn")
        self.regen_btn.setMinimumHeight(42)
        self.regen_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.regen_btn.clicked.connect(self._regenerate)
        main.addWidget(self.regen_btn)

        # Progress Bar
        self.pbar = QtWidgets.QProgressBar()
        self.pbar.setVisible(False)
        self.pbar.setTextVisible(True)
        self.pbar.setFixedHeight(12)
        self.pbar.setRange(0, 100)
        main.addWidget(self.pbar)

        # Status
        self.status_label = QtWidgets.QLabel("Ready")
        self.status_label.setAlignment(QtCore.Qt.AlignCenter)
        self.status_label.setStyleSheet("color: #666; font-size: 11px;")
        main.addWidget(self.status_label)

    def _load_from_node(self):
        if not self.node:
            return
        try:
            if "sd_model" in self.node.knobs():
                self._info_labels["model"].setText(self.node["sd_model"].value())
            if "sd_ratio" in self.node.knobs():
                self._info_labels["ratio"].setText(self.node["sd_ratio"].value())
            if "sd_resolution" in self.node.knobs():
                self._info_labels["resolution"].setText(self.node["sd_resolution"].value())
            if "sd_duration" in self.node.knobs():
                self._info_labels["duration"].setText(self.node["sd_duration"].value())
            if "sd_mode" in self.node.knobs():
                self._info_labels["mode"].setText(self.node["sd_mode"].value())

            if "sd_prompt" in self.node.knobs():
                self.prompt_display.setPlainText(self.node["sd_prompt"].value())
            if "sd_neg_prompt" in self.node.knobs():
                self.neg_prompt_display.setPlainText(self.node["sd_neg_prompt"].value())

            try:
                all_ref_paths = _collect_seedance_input_image_paths(self.node)
                if all_ref_paths:
                    found = sum(1 for p in all_ref_paths if os.path.exists(p))
                    self.cached_info_label.setText(
                        "{} image(s) ({} available)".format(len(all_ref_paths), found))
                else:
                    self.cached_info_label.setText("Text-only generation")
            except Exception:
                self.cached_info_label.setText("")

            if "sd_read_node" in self.node.knobs():
                self.read_node_label.setText(self.node["sd_read_node"].value())

            # --- Pre-fill editable section ---
            if "sd_model" in self.node.knobs():
                model_val = self.node["sd_model"].value()
                idx = self.model_combo.findText(model_val.split("(")[0].strip()) if "(" in model_val else self.model_combo.findText(model_val)
                if idx < 0:
                    idx = 0
                if 0 <= idx < self.model_combo.count():
                    self.model_combo.setCurrentIndex(idx)

            if "sd_ratio" in self.node.knobs():
                ratio_val = self.node["sd_ratio"].value()
                ratio_idx = self.ratio_combo.findText(ratio_val)
                if ratio_idx >= 0:
                    self.ratio_combo.setCurrentIndex(ratio_idx)

            if "sd_resolution" in self.node.knobs():
                res_val = self.node["sd_resolution"].value()
                res_idx = self.res_combo.findText(res_val)
                if res_idx >= 0:
                    self.res_combo.setCurrentIndex(res_idx)

            if "sd_duration" in self.node.knobs():
                dur_val = self.node["sd_duration"].value()
                dur_map = {"4": 0, "5": 1, "8": 2, "10": 3, "15": 4, "Auto(-1)": 5}
                dur_idx = dur_map.get(str(dur_val), 1)
                if 0 <= dur_idx < self.dur_combo.count():
                    self.dur_combo.setCurrentIndex(dur_idx)

            if "sd_mode" in self.node.knobs():
                mode_val = self.node["sd_mode"].value()
                mode_map = {
                    SEEDANCE_MODE_TEXT: 0,
                    SEEDANCE_MODE_IMAGE: 1,
                    SEEDANCE_MODE_FRAMES: 2,
                }
                mode_idx = mode_map.get(mode_val, 0)
                if 0 <= mode_idx < self.mode_combo.count():
                    self.mode_combo.setCurrentIndex(mode_idx)

            if "sd_prompt" in self.node.knobs():
                self.prompt_edit.setText(self.node["sd_prompt"].value())
            if "sd_neg_prompt" in self.node.knobs():
                self.neg_prompt_edit.setText(self.node["sd_neg_prompt"].value())

            # Load reference images into ImageStrip
            try:
                all_paths = _collect_seedance_input_image_paths(self.node)
                if all_paths:
                    for p in all_paths:
                        self._ref_image_strip.add_image(p)
                else:
                    self._ref_image_strip.clear_images()
            except Exception as ex:
                print("[Seedance Regen] ERROR loading images: {}".format(ex))
                self._ref_image_strip.clear_images()

        except Exception as e:
            print("Seedance: Error loading record settings: {}".format(e))

    def _add_ref_image(self):
        fpath, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Add Reference Image", "",
            "Images (*.png *.jpg *.jpeg *.gif *.webp);;All Files (*)"
        )
        if fpath and os.path.isfile(fpath):
            self._ref_image_strip.add_image(fpath)
            self._save_ref_images_to_node()

    def _save_ref_images_to_node(self):
        if not self.node or "sd_input_images" not in self.node.knobs():
            return
        paths = self._ref_image_strip.images if hasattr(self, '_ref_image_strip') else []
        json_val = json.dumps(paths)
        self.node["sd_input_images"].setValue(json_val)

    def _regenerate(self):
        if not self.settings.api_key:
            self.status_label.setStyleSheet("color: #ef4444; font-size: 11px;")
            self.status_label.setText("Please set API key in Settings")
            nuke.message("API key not set.\nPlease open AI Workflow > Setting in the toolbar.")
            return

        if self.current_worker and self.current_worker.is_running:
            worker = self.current_worker
            worker.stop()
            try:
                worker.finished.disconnect()
                worker.error.disconnect()
                worker.status_update.disconnect()
                worker.progress_update.disconnect()
            except (RuntimeError, TypeError):
                pass
            if hasattr(self, '_status_task_id') and self._status_task_id:
                try:
                    from ai_workflow.status_bar import task_progress_manager
                    task_progress_manager.cancel_task(
                        self._status_task_id, "Cancelled by user")
                except Exception:
                    pass
                self._status_task_id = None
            worker_id = id(worker)
            _cleanup_timer = QtCore.QTimer()
            _cleanup_timer.setInterval(500)
            def _poll_thread_exit():
                if not worker.isRunning():
                    _cleanup_timer.stop()
                    _seedance_active_workers.pop(worker_id, None)
            _cleanup_timer.timeout.connect(_poll_thread_exit)
            _cleanup_timer.start()
            _seedance_active_workers.setdefault(worker_id, {})["_cleanup_timer"] = _cleanup_timer
            self.current_worker = None
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
        current_mode = self.mode_combo.currentData() or SEEDANCE_MODE_TEXT

        reference_image_paths = []
        if hasattr(self, '_ref_image_strip'):
            reference_image_paths = [p for p in self._ref_image_strip.images
                                     if p and os.path.exists(p)]
            self._save_ref_images_to_node()
        else:
            reference_image_paths = [p for p in _collect_seedance_input_image_paths(self.node)
                                     if p and os.path.exists(p)]

        gen_name = "Seedance_Generate"
        if "sd_generator" in self.node.knobs():
            gen_name = self.node["sd_generator"].value() or gen_name

        output_dir = get_output_directory()

        self._toggle_ui(True)
        self.status_label.setStyleSheet("color: #facc15; font-size: 11px;")
        self.status_label.setText("Starting regeneration...")

        worker = SeedanceWorker(
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
        _seedance_active_workers[worker_id] = {"worker": worker, "params": {}}

        widget_ref = self
        node_ref = self.node

        try:
            from ai_workflow.status_bar import task_progress_manager
            status_task_id = task_progress_manager.add_task(
                node_ref.name() if node_ref else "Seedance Regen", "video")
            self._status_task_id = status_task_id
            worker.status_update.connect(
                lambda s: task_progress_manager.update_status(status_task_id, s))
            worker.progress_update.connect(
                lambda v: task_progress_manager.update_status(status_task_id, progress=v))
        except Exception:
            status_task_id = None
            self._status_task_id = None

        def _on_finished(path, metadata):
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
                        _regen_duration = metadata.get("duration") if metadata else None
                        cur_node = node_ref
                        if _regen_duration and "sd_duration" in cur_node.knobs():
                            cur_node["sd_duration"].setValue(str(_regen_duration))
                        updated = update_seedance_viewer_read(cur_node, path, duration=_regen_duration)
                        if updated:
                            cur_node = updated
                            rebuilt = _rebuild_seedance_group_for_thumbnail(cur_node, path, duration=_regen_duration)
                            if rebuilt:
                                cur_node = rebuilt
                                try:
                                    if _isValid(widget_ref):
                                        widget_ref.node = rebuilt
                                except Exception:
                                    pass
                            else:
                                _update_seedance_thumbnail(cur_node, path)
                            try:
                                nuke.connectViewer(0, cur_node)
                            except:
                                pass
                    except Exception as e:
                        import traceback
                        print("Seedance: ERROR updating Viewer Read: {}".format(e))
                        traceback.print_exc()
                    finally:
                        _seedance_active_workers.pop(worker_id, None)
                nuke.executeInMainThread(_update)
            else:
                _seedance_active_workers.pop(worker_id, None)

        def _on_error(err):
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
            _seedance_active_workers.pop(worker_id, None)
            nuke.executeInMainThread(nuke.message, args=("Seedance Regeneration Error:\n{}".format(err),))

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
# Knob Widget Wrappers (for PyCustom_Knob)
# ---------------------------------------------------------------------------
class SeedanceKnobWidget(QtWidgets.QWidget):
    """Wrapper for Seedance_Generate node."""

    def __init__(self):
        super(SeedanceKnobWidget, self).__init__()
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        try:
            node = nuke.thisNode()
        except Exception:
            node = None
        print("[Seedance] KnobWidget __init__: node = {}".format(
            node.name() if node else "None"))
        self.panel = SeedanceWidget(node=node, parent=self)
        layout.addWidget(self.panel)

    def makeUI(self):
        return self

    def updateValue(self):
        try:
            self.panel._save_all_state_to_node()
        except Exception:
            pass


class SeedanceRecordKnobWidget(QtWidgets.QWidget):
    """Wrapper for Seedance video record node."""

    def __init__(self, node=None):
        super(SeedanceRecordKnobWidget, self).__init__()
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        if node is None:
            try:
                node = nuke.thisNode()
            except:
                node = None

        self.panel = SeedanceRecordWidget(node, self)
        layout.addWidget(self.panel)

    def makeUI(self):
        return self

    def updateValue(self):
        pass


class SeedanceViewerRegenWidget(QtWidgets.QWidget):
    """Wrapper for Seedance Viewer node's Regenerate tab (PyCustom_Knob)."""

    def __init__(self):
        super(SeedanceViewerRegenWidget, self).__init__()
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        try:
            self.node = nuke.thisNode()
        except Exception:
            self.node = None

        self.panel = SeedanceRecordWidget(self.node, parent=self)
        layout.addWidget(self.panel)

    def makeUI(self):
        return self

    def updateValue(self):
        try:
            if hasattr(self, 'panel') and hasattr(self.panel, '_save_all_state_to_node'):
                self.panel._save_all_state_to_node()
        except Exception:
            pass
