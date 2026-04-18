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

# ---------------------------------------------------------------------------
# Shared imports from ai_workflow.core
# ---------------------------------------------------------------------------
from ai_workflow.core.pyside_compat import QtWidgets, QtCore, QtGui, _isValid
from ai_workflow.core.ui_components import DropDownComboBox, SHARED_DARK_STYLE
from ai_workflow.core.model_catalog import (
    VEO_MODEL_OPTIONS,
    VEO_RATIO_OPTIONS,
    VEO_RESOLUTION_OPTIONS,
    VEO_DURATION_OPTIONS,
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
    image_to_base64, get_mime_type, call_gemini_api, extract_image_from_response,
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

# Backward-compatible re-exports from veo_nodes
# (node creation functions extracted for maintainability)
from ai_workflow.veo_nodes import (  # noqa: F401
    _SEND_TO_STUDIO_SCRIPT,
    _VEO_PLAYER_SEND_SCRIPT,
    _add_send_to_studio_knob,
    create_veo_player_node,
    _get_internal_read,
    _rebuild_veo_group_for_thumbnail,
    _update_veo_thumbnail,
    _next_veo_viewer_name,
    create_veo_viewer_node,
    create_veo_viewer_standalone,
    update_veo_viewer_read,
    _find_veo_generator,
    _collect_veo_input_images_for_round,
    _collect_veo_input_image_paths,
    _next_veo_name,
    _create_veo_group_inputs,
    create_veo_node,
    # Constants re-exported for backward compatibility
    VEO_MODELS, VEO_MODEL_DEFAULT,
    VEO_MODE_TEXT, VEO_MODE_FIRST_FRAME, VEO_MODE_FRAMES, VEO_MODE_INGREDIENTS,
    VEO_MODE_INPUT_COUNTS,
)

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

# ---------------------------------------------------------------------------
# Constants are now defined in veo_nodes.py and re-imported below.
# ---------------------------------------------------------------------------

# Max inputs needed (for node creation)
VEO_MAX_INPUTS = 3

# Backward-compatible aliases
VEO_STYLE = SHARED_DARK_STYLE
NANOBANANA_STYLE = SHARED_DARK_STYLE  # legacy compat

# VEO-specific worker registry (separate from NB workers)
_veo_active_workers = {}


# NOTE: DropDownComboBox is now imported from core.ui_components
# NOTE: NanoBananaSettings is now imported from core.settings as AppSettings



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
        self.model = resolve_video_model_id(model)
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

            # 2. Build generation payload via adapter registry
            adapter = get_video_adapter(self.model)
            if not adapter:
                self.error.emit("No video adapter registered for model: {}".format(self.model))
                return

            generate_kwargs, config_kwargs, mode_str, dur_seconds = adapter.build_generate_kwargs(
                prompt=self.prompt,
                mode=self.mode,
                ref_images=ref_images,
                aspect_ratio=self.aspect_ratio,
                duration=self.duration,
                resolution=self.resolution,
                types_module=types,
            )

            has_refs = len(ref_images) > 0


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


# ---------------------------------------------------------------------------
# VEO Player Group Node (wraps Read node with exposed knobs + Send to Studio)
# ---------------------------------------------------------------------------

# Send to Studio script for VEO Player Group — reads file from internal Read node





# ---------------------------------------------------------------------------
# VEO Viewer Node (unified: Read playback + record + regeneration in one node)
# Mirrors NanoBanana's Nano Viewer pattern.
# ---------------------------------------------------------------------------




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
        fill_combo_from_options(self.model_combo, VEO_MODEL_OPTIONS)
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
        fill_combo_from_options(self.ratio_combo, VEO_RATIO_OPTIONS)
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
        fill_combo_from_options(self.res_combo, VEO_RESOLUTION_OPTIONS)
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
        fill_combo_from_options(self.dur_combo, VEO_DURATION_OPTIONS)
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
        for h in get_history("veo_prompt_history", scope="project", limit=10):
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
        push_history_item("veo_prompt_history", prompt, scope="project", limit=10)
        self._refresh_history_combo(get_history("veo_prompt_history", scope="project", limit=10))

    def _clear_history(self):
        set_history("veo_prompt_history", [], scope="project", limit=10)
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
        fill_combo_from_options(self.model_combo, VEO_MODEL_OPTIONS)
        model_group.addWidget(self.model_combo)
        config_row.addLayout(model_group, 2)

        # Aspect ratio
        ratio_group = QtWidgets.QVBoxLayout()
        ratio_group.setSpacing(2)
        ratio_lbl = QtWidgets.QLabel("Aspect ratio:")
        ratio_lbl.setStyleSheet("color: #aaa; font-size: 11px;")
        ratio_group.addWidget(ratio_lbl)
        self.ratio_combo = DropDownComboBox()
        fill_combo_from_options(self.ratio_combo, VEO_RATIO_OPTIONS)
        ratio_group.addWidget(self.ratio_combo)
        config_row.addLayout(ratio_group, 1)

        # Resolution
        res_group = QtWidgets.QVBoxLayout()
        res_group.setSpacing(2)
        res_lbl = QtWidgets.QLabel("Resolution:")
        res_lbl.setStyleSheet("color: #aaa; font-size: 11px;")
        res_group.addWidget(res_lbl)
        self.res_combo = DropDownComboBox()
        fill_combo_from_options(self.res_combo, VEO_RESOLUTION_OPTIONS)
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
        fill_combo_from_options(self.dur_combo, VEO_DURATION_OPTIONS)
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
                        # Extract duration from metadata for frame range fallback
                        _regen_duration = metadata.get("duration") if metadata else None
                        cur_node = node_ref
                        # Update veo_duration knob so fallback calculations use the new value
                        if _regen_duration and "veo_duration" in cur_node.knobs():
                            cur_node["veo_duration"].setValue(str(_regen_duration))
                        updated = update_veo_viewer_read(cur_node, path, duration=_regen_duration)
                        if updated:
                            cur_node = updated
                            # --- Replacement Jutsu: rebuild Group for fresh thumbnail ---
                            rebuilt = _rebuild_veo_group_for_thumbnail(cur_node, path, duration=_regen_duration)
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
