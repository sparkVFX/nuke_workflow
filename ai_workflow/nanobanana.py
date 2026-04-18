"""
NanoBanana Image Generation Node for Nuke.
Creates a node in Node Graph with a PySide/Qt custom UI embedded
in the Properties panel via PyCustom_Knob.

Based on Google Gemini Image Generation API:
- Supports up to 14 input images (Gemini 3.1 Flash limit)
- Input images are rendered to temp directory before sending
- Supports text prompts + reference images

Workflow:
- NanoBanana_Generate: Main generation node with inputs
- NanoBanana_Prompt: Prompt history node that can regenerate images
"""

# ---------------------------------------------------------------------------
# Shared imports from ai_workflow.core
# ---------------------------------------------------------------------------
from ai_workflow.core.pyside_compat import QtWidgets, QtCore, QtGui, _isValid
from ai_workflow.core.ui_components import DropDownComboBox, SHARED_DARK_STYLE
from ai_workflow.core.model_catalog import (
    NB_MODEL_OPTIONS,
    NB_RATIO_OPTIONS,
    NB_RESOLUTION_OPTIONS,
    fill_combo_from_options,
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
    get_internal_read as _get_internal_read_nb,
    next_node_name as _next_node_name,
    rebuild_group_for_thumbnail as _rebuild_group_for_thumbnail,
    update_node_thumbnail as _update_node_thumbnail,
)
from ai_workflow.core.worker_base import (
    BaseWorker, register_active_worker, unregister_active_worker,
)

# Backward-compatible re-exports from nb_diagnostics
# (diagnostic functions extracted for maintainability)
from ai_workflow.nb_diagnostics import (  # noqa: F401
    diagnose_visual_refresh_v3,
    diagnose_visual_refresh_v4,
    diagnose_visual_refresh_v5,
    diagnose_visual_refresh,
    test_thumbnail_refresh,
    restore_nb_thumbnails,
)

import nuke
import nukescripts
import os
import sys
import json
import tempfile
import time
import random
import base64
import datetime
import re


# Backward-compatible aliases
NANOBANANA_STYLE = SHARED_DARK_STYLE

# ---------------------------------------------------------------------------
# Constants are now defined in nb_nodes.py and re-imported below.
# ---------------------------------------------------------------------------

# Use the shared worker registry from core
from ai_workflow.core.worker_base import _active_workers  # noqa: F811

# Backward-compatible re-exports from nb_nodes
# (node creation functions extracted for maintainability)
from ai_workflow.nb_nodes import (  # noqa: F401
    _is_generator_node,
    get_nanobanana_node,
    _add_send_to_studio_knob,
    create_nb_player_node,
    create_prompt_node,
    update_prompt_read_node,
    _create_group_inputs,
    create_nanobanana_node,
    _nanobanana_input_changed,
    _SEND_TO_STUDIO_SCRIPT,
    MAX_INPUT_IMAGES,
    MODEL_MAX_INPUTS,
)


# NOTE: NanoBananaSettings is now imported from core.settings as AppSettings.
# The alias `NanoBananaSettings = AppSettings` is already done at the top.
# To access the singleton: `app_settings` (from core) or `NanoBananaSettings()`.

# Legacy class kept for source-level backward compatibility — delegates to core.
# (This block intentionally left minimal; actual logic lives in core.settings.)


# ---------------------------------------------------------------------------
# Settings Dialog
# ---------------------------------------------------------------------------
class NanoBananaSettingsDialog(QtWidgets.QDialog):
    """Settings dialog for API key and temp directory configuration."""
    
    def __init__(self, parent=None):
        super(NanoBananaSettingsDialog, self).__init__(parent)
        self.setWindowTitle("NanoBanana Settings")
        self.setMinimumWidth(450)
        self.setStyleSheet(NANOBANANA_STYLE)
        self.settings = NanoBananaSettings()
        self._build_ui()
    
    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)
        
        # === API Key Section ===
        api_group = QtWidgets.QGroupBox("API Key")
        api_layout = QtWidgets.QVBoxLayout(api_group)
        
        api_label = QtWidgets.QLabel("Google Gemini API Key:")
        api_label.setStyleSheet("color: #aaa; font-size: 11px;")
        api_layout.addWidget(api_label)
        
        api_row = QtWidgets.QHBoxLayout()
        self.api_key_input = QtWidgets.QLineEdit()
        self.api_key_input.setPlaceholderText("Enter your Gemini API key here...")
        self.api_key_input.setEchoMode(QtWidgets.QLineEdit.Password)
        self.api_key_input.setText(self.settings.api_key)
        api_row.addWidget(self.api_key_input)

        self.test_api_btn = QtWidgets.QPushButton("Test")
        self.test_api_btn.setObjectName("secondaryBtn")
        self.test_api_btn.setFixedWidth(60)
        self.test_api_btn.clicked.connect(self._test_api_key)
        api_row.addWidget(self.test_api_btn)

        api_layout.addLayout(api_row)

        # Status label for test result
        self.api_test_label = QtWidgets.QLabel()
        self.api_test_label.setStyleSheet("color: #666; font-size: 10px;")
        self.api_test_label.setWordWrap(True)
        api_layout.addWidget(self.api_test_label)
        
        # Show/Hide API key checkbox
        self.show_api_chk = QtWidgets.QCheckBox("Show API Key")
        self.show_api_chk.toggled.connect(self._toggle_api_visibility)
        api_layout.addWidget(self.show_api_chk)
        
        api_help = QtWidgets.QLabel(
            '<a href="https://aistudio.google.com/app/apikey" style="color: #60a5fa;">Get API Key from Google AI Studio</a>'
        )
        api_help.setOpenExternalLinks(True)
        api_help.setStyleSheet("font-size: 10px;")
        api_layout.addWidget(api_help)
        
        layout.addWidget(api_group)
        
        # === Temp Directory Section ===
        temp_group = QtWidgets.QGroupBox("Temporary Directory")
        temp_layout = QtWidgets.QVBoxLayout(temp_group)
        
        temp_label = QtWidgets.QLabel("Directory for rendered input images:")
        temp_label.setStyleSheet("color: #aaa; font-size: 11px;")
        temp_layout.addWidget(temp_label)
        
        temp_row = QtWidgets.QHBoxLayout()
        self.temp_dir_input = QtWidgets.QLineEdit()
        self.temp_dir_input.setPlaceholderText("Leave empty for default (system temp)")
        self.temp_dir_input.setText(self.settings._data.get("temp_directory", ""))
        temp_row.addWidget(self.temp_dir_input)
        
        browse_btn = QtWidgets.QPushButton("Browse...")
        browse_btn.setObjectName("secondaryBtn")
        browse_btn.clicked.connect(self._browse_temp_dir)
        temp_row.addWidget(browse_btn)
        
        temp_layout.addLayout(temp_row)
        
        # Show current effective path
        self.effective_path_label = QtWidgets.QLabel()
        self._update_effective_path()
        self.effective_path_label.setStyleSheet("color: #666; font-size: 10px;")
        self.effective_path_label.setWordWrap(True)
        temp_layout.addWidget(self.effective_path_label)
        
        self.temp_dir_input.textChanged.connect(self._update_effective_path)
        
        layout.addWidget(temp_group)
        
        # === Project Cache Root Section ===
        cache_group = QtWidgets.QGroupBox("Project Cache (per-script isolation)")
        cache_layout = QtWidgets.QVBoxLayout(cache_group)
        
        cache_label = QtWidgets.QLabel(
            "Root folder for per-project caches:\n"
            "Each .nk script gets its own sub-folder for input/output/logs.")
        cache_label.setStyleSheet("color: #aaa; font-size: 11px;")
        cache_layout.addWidget(cache_label)
        
        cache_row = QtWidgets.QHBoxLayout()
        self.cache_root_input = QtWidgets.QLineEdit()
        self.cache_root_input.setPlaceholderText(
            "Leave empty for default (system temp/nanobanana_projects)")
        self.cache_root_input.setText(self.settings._data.get("project_cache_root", ""))
        cache_row.addWidget(self.cache_root_input)
        
        browse_cache_btn = QtWidgets.QPushButton("Browse...")
        browse_cache_btn.setObjectName("secondaryBtn")
        browse_cache_btn.clicked.connect(self._browse_cache_root)
        cache_row.addWidget(browse_cache_btn)
        
        cache_layout.addLayout(cache_row)
        
        # Show current effective project path
        self.effective_proj_label = QtWidgets.QLabel()
        self._update_effective_proj_path()
        self.effective_proj_label.setStyleSheet("color: #666; font-size: 10px;")
        self.effective_proj_label.setWordWrap(True)
        cache_layout.addWidget(self.effective_proj_label)
        
        self.cache_root_input.textChanged.connect(self._update_effective_proj_path)
        
        layout.addWidget(cache_group)
        
        # === ProRes Transcode Section ===
        prores_group = QtWidgets.QGroupBox("ProRes Transcode")
        prores_layout = QtWidgets.QVBoxLayout(prores_group)
        
        prores_label = QtWidgets.QLabel("Codec for output video (used when writing ProRes):")
        prores_label.setStyleSheet("color: #aaa; font-size: 11px;")
        prores_label.setWordWrap(True)
        prores_layout.addWidget(prores_label)
        
        self.prores_combo = DropDownComboBox()
        _prores_options = [
            ("ProRes 422 HQ", "ProRes 422 HQ", "~184 Mbps | 100% | 1:8 | 极低损失"),
            ("ProRes 422", "ProRes 422", "~122 Mbps | 66% | 1:12 | 低损失"),
            ("ProRes 422 LT", "ProRes 422 LT", "~85 Mbps | 45% | 1:17 | 中等损失"),
            ("ProRes 422 Proxy", "ProRes 422 Proxy", "~38 Mbps | 20% | 1:38 | 高损失(预览用)"),
        ]
        for display_name, value, desc in _prores_options:
            self.prores_combo.addItem("{} — {}".format(display_name, desc))
            # Store the actual codec name as userData
            self.prores_combo.setItemData(self.prores_combo.count() - 1, value)
        # Set current selection from saved settings
        current_codec = self.settings._data.get("prores_codec", "ProRes 422 HQ")
        idx = self.prores_combo.findData(current_codec)
        if idx >= 0:
            self.prores_combo.setCurrentIndex(idx)
        else:
            self.prores_combo.setCurrentIndex(0)
        prores_layout.addWidget(self.prores_combo)
        
        layout.addWidget(prores_group)
        
        # === Buttons ===
        btn_layout = QtWidgets.QHBoxLayout()
        btn_layout.addStretch()
        
        cancel_btn = QtWidgets.QPushButton("Cancel")
        cancel_btn.setObjectName("secondaryBtn")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        
        save_btn = QtWidgets.QPushButton("Save Settings")
        save_btn.setObjectName("generateBtn")
        save_btn.clicked.connect(self._save_settings)
        btn_layout.addWidget(save_btn)
        
        layout.addLayout(btn_layout)
    
    def _toggle_api_visibility(self, show):
        if show:
            self.api_key_input.setEchoMode(QtWidgets.QLineEdit.Normal)
        else:
            self.api_key_input.setEchoMode(QtWidgets.QLineEdit.Password)

    def _test_api_key(self):
        """Test if the current API key is valid by calling the Gemini models endpoint."""
        from google import genai
        api_key = self.api_key_input.text().strip()

        if not api_key:
            self.api_test_label.setText("Please enter an API key first.")
            self.api_test_label.setStyleSheet("color: #f87171; font-size: 10px;")
            return

        self.test_api_btn.setEnabled(False)
        self.test_api_btn.setText("Testing...")
        self.api_test_label.setText("Validating API key...")
        self.api_test_label.setStyleSheet("color: #fbbf24; font-size: 10px;")
        QtWidgets.QApplication.processEvents()

        try:
            client = genai.Client(api_key=api_key)
            # List available models to validate the key (lightweight call)
            models = client.models.list()
            model_count = len(list(models))
            self.api_test_label.setText(
                "API Key is valid! Found {} available model(s).".format(model_count))
            self.api_test_label.setStyleSheet("color: #4ade80; font-size: 10px;")
        except Exception as e:
            err_msg = str(e)
            if "401" in err_msg or "API_KEY" in err_msg or "permission" in err_msg.lower():
                display = "Invalid API Key. Please check and try again."
            elif "connection" in err_msg.lower() or "network" in err_msg.lower():
                display = "Network error. Please check your internet connection."
            else:
                display = "Error: {}".format(err_msg[:100])
            self.api_test_label.setText(display)
            self.api_test_label.setStyleSheet("color: #f87171; font-size: 10px;")
        finally:
            self.test_api_btn.setEnabled(True)
            self.test_api_btn.setText("Test")
    
    def _browse_temp_dir(self):
        dir_path = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Select Temporary Directory",
            self.temp_dir_input.text() or tempfile.gettempdir()
        )
        if dir_path:
            self.temp_dir_input.setText(dir_path)
    
    def _browse_cache_root(self):
        dir_path = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Select Project Cache Root",
            self.cache_root_input.text() or tempfile.gettempdir()
        )
        if dir_path:
            self.cache_root_input.setText(dir_path)
    
    def _update_effective_path(self):
        custom = self.temp_dir_input.text().strip()
        if custom:
            effective = custom
        else:
            effective = os.path.join(tempfile.gettempdir(), DEFAULT_TEMP_DIR_NAME)
        self.effective_path_label.setText("Effective path: {}".format(effective))
    
    def _update_effective_proj_path(self):
        custom = self.cache_root_input.text().strip()
        try:
            script_name = get_script_name()
        except Exception:
            script_name = UNSAVED_PROJECT_DIR
        if custom:
            root = custom
        else:
            root = os.path.join(tempfile.gettempdir(), DEFAULT_PROJECT_CACHE_NAME)
        proj_path = os.path.join(root, script_name)
        self.effective_proj_label.setText(
            "Current project cache: {}  (script='{}')".format(proj_path, script_name))
    
    def _save_settings(self):
        self.settings.api_key = self.api_key_input.text().strip()
        self.settings.temp_directory = self.temp_dir_input.text().strip()
        self.settings.project_cache_root = self.cache_root_input.text().strip()
        # Save selected ProRes codec
        idx = self.prores.currentIndex()
        if idx >= 0:
            codec = self.prores.itemData(idx)
            if codec:
                self.settings.prores_codec = codec
        self.accept()




# ---------------------------------------------------------------------------
# Project Cache Migration System
# ---------------------------------------------------------------------------
def _update_node_knob_paths(old_prefix, new_prefix):
    """After migrating cache files, update absolute paths in all NB node knobs.

    Scans all Group nodes with is_nb_player knob and replaces old_prefix
    with new_prefix in nb_file, nb_output_path, and nb_input_images JSON.
    """
    updated_count = 0
    for node in nuke.allNodes("Group"):
        if "is_nb_player" not in node.knobs():
            continue
        changed = False
        # Update nb_file
        if "nb_file" in node.knobs():
            old_path = node["nb_file"].value()
            if old_path and old_prefix in old_path:
                new_path = old_path.replace(old_prefix, new_prefix, 1)
                node["nb_file"].setValue(new_path)
                changed = True
        # Update nb_output_path
        if "nb_output_path" in node.knobs():
            old_path = node["nb_output_path"].value()
            if old_path and old_prefix in old_path:
                new_path = old_path.replace(old_prefix, new_prefix, 1)
                node["nb_output_path"].setValue(new_path)
                changed = True
        # Update nb_input_images JSON array
        if "nb_input_images" in node.knobs():
            raw = node["nb_input_images"].value()
            if raw and raw.strip():
                try:
                    paths = json.loads(raw)
                    new_paths = []
                    for p in paths:
                        if p and old_prefix in p:
                            new_paths.append(p.replace(old_prefix, new_prefix, 1))
                            changed = True
                        else:
                            new_paths.append(p)
                    if changed:
                        node["nb_input_images"].setValue(json.dumps(new_paths))
                except (json.JSONDecodeError, TypeError):
                    pass  # skip malformed JSON
        if changed:
            updated_count += 1
    return updated_count


def migrate_project_cache(old_script_name, new_script_name):
    """Migrate cache from one project directory to another.

    Called when user saves an untitled script (old="_unsaved_") or does Save As.
    Moves input/output/logs subdirectories and updates all node knob paths.

    Args:
        old_script_name: Source script name (e.g. "_unsaved_" or "OldProject")
        new_script_name: Target script name (e.g. "MyComp_v01")

    Returns:
        Number of files moved, or -1 on error.
    """
    settings = NanoBananaSettings()
    root = settings.project_cache_root

    src_dir = os.path.join(root, old_script_name)
    dst_dir = os.path.join(root, new_script_name)

    # Nothing to do if source doesn't exist
    if not os.path.isdir(src_dir):
        print("[NB Migrate] Source dir '{}' does not exist — nothing to move".format(src_dir))
        return 0

    # If source == destination (re-save same name), skip
    if src_dir == dst_dir:
        return 0

    try:
        import shutil

        moved_files = 0
        subdirs = ["input", "output", "logs"]
        history_file = "history.json"

        # Move each subdirectory that exists
        for subdir in subdirs:
            src_sub = os.path.join(src_dir, subdir)
            if os.path.isdir(src_sub):
                dst_sub = os.path.join(dst_dir, subdir)
                os.makedirs(dst_sub, exist_ok=True)
                # Move individual files (avoid shutil.move which may fail cross-drive)
                for fname in os.listdir(src_sub):
                    src_f = os.path.join(src_sub, fname)
                    dst_f = os.path.join(dst_sub, fname)
                    if not os.path.exists(dst_f):
                        shutil.move(src_f, dst_f)
                        moved_files += 1
                # Remove empty source sub
                try:
                    os.rmdir(src_sub)
                except OSError:
                    pass  # not empty or other error, ignore

        # Move history file
        src_hist = os.path.join(src_dir, history_file)
        if os.path.isfile(src_hist):
            os.makedirs(dst_dir, exist_ok=True)
            dst_hist = os.path.join(dst_dir, history_file)
            if not os.path.exists(dst_hist):
                shutil.move(src_hist, dst_hist)
                moved_files += 1

        # Update node knobs
        old_prefix = src_dir.replace("\\", "/")
        new_prefix = dst_dir.replace("\\", "/")
        nodes_updated = _update_node_knob_paths(old_prefix, new_prefix)

        # Clean up empty source dir
        try:
            os.rmdir(src_dir)
        except OSError:
            pass

        print("[NB Migrate] {} files moved from '{}' to '{}', {} nodes updated".format(
            moved_files, old_script_name, new_script_name, nodes_updated))
        return moved_files

    except Exception as e:
        print("[NB Migrate] ERROR migrating cache: {}".format(e))
        import traceback
        traceback.print_exc()
        return -1


def _on_nuke_script_save():
    """Callback registered via nuke.addOnScriptSave().

    Detects when the script name changes (untitled -> saved, or Save As)
    and triggers cache migration.
    """
    try:
        current_name = get_script_name()

        # Get previously stored name (if any)
        prev_name = getattr(_on_nuke_script_save, "_last_script_name", None)

        if prev_name is None:
            # First save callback — store and wait for next change
            _on_nuke_script_save._last_script_name = current_name
            if current_name != UNSAVED_PROJECT_DIR:
                print("[NB Save] Script saved as: '{}'".format(current_name))
            return

        if prev_name == current_name:
            return  # Same name, normal re-save — nothing to do

        # Name changed! This means Save As or first-save-after-untitled.
        print("[NB Save] Script renamed: '{}' -> '{}'".format(prev_name, current_name))

        result = migrate_project_cache(prev_name, current_name)
        if result >= 0:
            _on_nuke_script_save._last_script_name = current_name

    except Exception as e:
        print("[NB Save] Error in onScriptSave callback: {}".format(e))


# Module-level flag to track whether we've registered the save callback
_save_callback_registered = False


def ensure_save_callback_registered():
    """Register the onScriptSave callback exactly once."""
    global _save_callback_registered
    if not _save_callback_registered:
        try:
            nuke.addOnScriptSave(_on_nuke_script_save, args=(), kwargs={},
                                 nodeClass="Root")
            _save_callback_registered = True
            print("[NB] onScriptSave callback registered")
        except Exception as e:
            print("[NB] Warning: could not register onScriptSave callback: {}".format(e))


# ---------------------------------------------------------------------------
# Per-Project History (replaces global prompt_history in config)
# ---------------------------------------------------------------------------
class ProjectHistory(object):
    """Manages per-project prompt history stored as {project_dir}/history.json.

    Each .nk script gets its own history file, so Project A's prompts
    never appear in Project B's dropdown.
    """

    @staticmethod
    def _get_file():
        return os.path.join(get_project_directory(), "history.json")

    @staticmethod
    def load(key="prompt_history"):
        """Load history list for *key* from the current project's history.json."""
        f = ProjectHistory._get_file()
        if os.path.exists(f):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                    return data.get(key, [])
            except Exception:
                pass
        # Fallback: try loading from global config (migration path)
        settings = NanoBananaSettings()
        return getattr(settings, key) if hasattr(settings, key) else []

    @staticmethod
    def save(key, value):
        """Save history list for *key* to the current project's history.json."""
        f = ProjectHistory._get_file()
        data = {}
        if os.path.exists(f):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception:
                pass
        data[key] = value[:20]  # cap at 20 entries
        try:
            proj_dir = os.path.dirname(f)
            if not os.path.exists(proj_dir):
                os.makedirs(proj_dir)
            with open(f, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
        except Exception as e:
            print("[NB History] Save error: {}".format(e))


def get_project_prompt_history():
    """Get prompt history for current project."""
    return ProjectHistory.load("prompt_history")


def set_project_prompt_history(value):
    """Set prompt history for current project."""
    ProjectHistory.save("prompt_history", value)


def get_project_veo_history():
    """Get VEO prompt history for current project."""
    return ProjectHistory.load("veo_prompt_history")


def set_project_veo_history(value):
    """Set VEO prompt history for current project."""
    ProjectHistory.save("veo_prompt_history", value)







# ---------------------------------------------------------------------------
# The Main Generate Widget
# ---------------------------------------------------------------------------
class NanoBananaWidget(QtWidgets.QWidget):
    """Custom Qt widget embedded inside the NanoBanana_Generate node."""

    def __init__(self, node=None, parent=None):
        super(NanoBananaWidget, self).__init__(parent)
        # Cache a reference to the owning node so save/restore always
        # target the correct NanoBanana_Generate even when another node
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

        # === Model ===
        self.model_combo = DropDownComboBox()
        fill_combo_from_options(self.model_combo, NB_MODEL_OPTIONS)
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        self.model_combo.currentIndexChanged.connect(lambda _: self._save_all_state_to_node())
        main.addWidget(self.model_combo)

        # === Ratio + Resolution ===
        row2 = QtWidgets.QHBoxLayout()
        row2.setSpacing(6)

        self.ratio_combo = DropDownComboBox()
        fill_combo_from_options(self.ratio_combo, NB_RATIO_OPTIONS)
        self.ratio_combo.currentIndexChanged.connect(lambda _: self._save_all_state_to_node())

        self.res_combo = DropDownComboBox()
        fill_combo_from_options(self.res_combo, NB_RESOLUTION_OPTIONS)
        self.res_combo.currentIndexChanged.connect(lambda _: self._save_all_state_to_node())

        row2.addWidget(self.ratio_combo)
        row2.addWidget(self.res_combo)
        main.addLayout(row2)

        # === Seed ===
        row3 = QtWidgets.QHBoxLayout()
        row3.setSpacing(6)

        seed_label = QtWidgets.QLabel("Seed:")
        self.seed_input = QtWidgets.QLineEdit()
        self.seed_input.setPlaceholderText("Random Seed")
        self.seed_input.setValidator(QtGui.QIntValidator())
        self.seed_input.setEnabled(False)
        self.seed_input.textChanged.connect(lambda _: self._save_all_state_to_node())

        self.seed_random_chk = QtWidgets.QCheckBox("Random")
        self.seed_random_chk.setChecked(True)
        self.seed_random_chk.toggled.connect(lambda c: self.seed_input.setEnabled(not c))
        self.seed_random_chk.toggled.connect(lambda _: self._save_all_state_to_node())

        row3.addWidget(seed_label)
        row3.addWidget(self.seed_input, 1)
        row3.addWidget(self.seed_random_chk)
        main.addLayout(row3)

        # === History ===
        row4 = QtWidgets.QHBoxLayout()
        row4.setSpacing(4)

        self.history_combo = DropDownComboBox()
        self.history_combo.addItem("Select from History...")
        for h in get_project_prompt_history():
            display = h[:40] + "..." if len(h) > 40 else h
            self.history_combo.addItem(display, h)
        self.history_combo.currentIndexChanged.connect(self._on_history_select)

        hist_clear_btn = QtWidgets.QPushButton("x")
        hist_clear_btn.setObjectName("secondaryBtn")
        hist_clear_btn.setFixedWidth(22)
        hist_clear_btn.clicked.connect(self._clear_history)

        row4.addWidget(self.history_combo, 1)
        row4.addWidget(hist_clear_btn)
        main.addLayout(row4)

        # === Prompt ===
        self.prompt_edit = QtWidgets.QTextEdit()
        self.prompt_edit.setPlaceholderText(
            "Enter your creative prompt here...\n\n"
            "Use img1, img2, img3... to reference input images.\n"
            "e.g. Replace the red character in img1 with the person from img2, "
            "keep the pose from img1."
        )
        self.prompt_edit.setMinimumHeight(100)
        self.prompt_edit.textChanged.connect(self._save_all_state_to_node)
        main.addWidget(self.prompt_edit)

        # === Negative Prompt ===
        self.neg_prompt_edit = QtWidgets.QTextEdit()
        self.neg_prompt_edit.setPlaceholderText("Negative prompt (optional)...")
        self.neg_prompt_edit.setFixedHeight(70)
        main.addWidget(self.neg_prompt_edit)

        # === Input Info ===
        inputs_group = QtWidgets.QGroupBox("Input Images")
        inputs_layout = QtWidgets.QVBoxLayout(inputs_group)
        self.inputs_info_label = QtWidgets.QLabel("Connect images to inputs")
        self.inputs_info_label.setStyleSheet("color: #888; font-size: 11px;")
        inputs_layout.addWidget(self.inputs_info_label)
        main.addWidget(inputs_group)

        # === Generate Button ===
        self.gen_btn = QtWidgets.QPushButton("GENERATE IMAGE")
        self.gen_btn.setObjectName("generateBtn")
        self.gen_btn.setMinimumHeight(42)
        self.gen_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.gen_btn.clicked.connect(self._start_generate)
        main.addWidget(self.gen_btn)

        # === Progress Bar ===
        self.pbar = QtWidgets.QProgressBar()
        self.pbar.setVisible(False)
        self.pbar.setTextVisible(False)
        self.pbar.setFixedHeight(6)
        main.addWidget(self.pbar)

        # === Status ===
        self.status_label = QtWidgets.QLabel("Ready")
        self.status_label.setAlignment(QtCore.Qt.AlignCenter)
        self.status_label.setStyleSheet("color: #666; font-size: 11px;")
        main.addWidget(self.status_label)

    def _on_history_select(self, index):
        if index <= 0:
            return
        full_text = self.history_combo.itemData(index)
        if not full_text:
            return
        self.prompt_edit.setText(full_text)
        # Move selected item to top of history
        self._add_to_history(full_text)
        # Reset combo to "Select from History..." after selecting
        self.history_combo.blockSignals(True)
        self.history_combo.setCurrentIndex(0)
        self.history_combo.blockSignals(False)

    def _add_to_history(self, prompt):
        if not prompt:
            return
        history = get_project_prompt_history()
        # Remove duplicate if exists (will be re-inserted at top)
        if prompt in history:
            history.remove(prompt)
        history.insert(0, prompt)
        if len(history) > 20:
            history = history[:20]
        set_project_prompt_history(history)

        self._refresh_history_combo(history)

    def _clear_history(self):
        set_project_prompt_history([])
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
    # Ensure a hidden knob on the owning node (one per parameter)
    # ------------------------------------------------------------------
    def _ensure_int_knob(self, node, name, label=""):
        if name not in node.knobs():
            k = nuke.Int_Knob(name, label)
            k.setVisible(False)
            node.addKnob(k)

    def _ensure_bool_knob(self, node, name, label=""):
        if name not in node.knobs():
            k = nuke.Boolean_Knob(name, label)
            k.setVisible(False)
            node.addKnob(k)

    def _ensure_text_knob(self, node, name, label=""):
        if name not in node.knobs():
            k = nuke.Multiline_Eval_String_Knob(name, label)
            k.setVisible(False)
            node.addKnob(k)

    # ------------------------------------------------------------------
    # Save / Restore ALL UI state using individual hidden knobs
    # (String_Knob has a ~256 char limit, so we avoid JSON-in-one-knob)
    # ------------------------------------------------------------------
    def _save_all_state_to_node(self):
        """Persist every user-visible widget value into hidden knobs on the
        owning node so it survives widget destruction / recreation."""
        node = self._node          # use cached ref directly (fastest)
        if node is None:
            return
        try:
            _ = node.name()        # validate the node is still alive
        except Exception:
            return
        try:
            self._ensure_int_knob(node, "nb_s_model", "s_model")
            self._ensure_int_knob(node, "nb_s_ratio", "s_ratio")
            self._ensure_int_knob(node, "nb_s_res", "s_res")
            self._ensure_bool_knob(node, "nb_s_seed_rnd", "s_seed_rnd")
            self._ensure_text_knob(node, "nb_s_seed_val", "s_seed_val")
            self._ensure_text_knob(node, "nb_s_prompt", "s_prompt")
            self._ensure_text_knob(node, "nb_s_neg", "s_neg")

            node["nb_s_model"].setValue(self.model_combo.currentIndex())
            node["nb_s_ratio"].setValue(self.ratio_combo.currentIndex())
            node["nb_s_res"].setValue(self.res_combo.currentIndex())
            node["nb_s_seed_rnd"].setValue(self.seed_random_chk.isChecked())
            node["nb_s_seed_val"].setValue(self.seed_input.text())
            node["nb_s_prompt"].setValue(self.prompt_edit.toPlainText())
            node["nb_s_neg"].setValue(self.neg_prompt_edit.toPlainText())
        except Exception as e:
            print("[NanoBanana] _save_all_state_to_node error: {}".format(e))

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
            # If none of the state knobs exist yet, nothing to restore
            if "nb_s_model" not in node.knobs():
                print("[NanoBanana] No saved state found on node '{}'".format(node.name()))
                return

            print("[NanoBanana] Restoring state from node '{}'".format(node.name()))

            # Block signals to avoid triggering saves during restore
            widgets = [self.model_combo, self.ratio_combo, self.res_combo,
                       self.seed_random_chk, self.seed_input,
                       self.prompt_edit, self.neg_prompt_edit]
            for w in widgets:
                w.blockSignals(True)

            # Model
            if "nb_s_model" in node.knobs():
                idx = int(node["nb_s_model"].value())
                if 0 <= idx < self.model_combo.count():
                    self.model_combo.setCurrentIndex(idx)

            # Ratio
            if "nb_s_ratio" in node.knobs():
                idx = int(node["nb_s_ratio"].value())
                if 0 <= idx < self.ratio_combo.count():
                    self.ratio_combo.setCurrentIndex(idx)

            # Resolution
            if "nb_s_res" in node.knobs():
                idx = int(node["nb_s_res"].value())
                if 0 <= idx < self.res_combo.count():
                    self.res_combo.setCurrentIndex(idx)

            # Seed
            if "nb_s_seed_rnd" in node.knobs():
                is_random = bool(node["nb_s_seed_rnd"].value())
                self.seed_random_chk.setChecked(is_random)
                self.seed_input.setEnabled(not is_random)
            if "nb_s_seed_val" in node.knobs():
                seed_val = node["nb_s_seed_val"].value()
                if seed_val:
                    self.seed_input.setText(seed_val)

            # Prompts
            if "nb_s_prompt" in node.knobs():
                prompt = node["nb_s_prompt"].value()
                if prompt:
                    self.prompt_edit.setText(prompt)
            if "nb_s_neg" in node.knobs():
                neg = node["nb_s_neg"].value()
                if neg:
                    self.neg_prompt_edit.setText(neg)

            for w in widgets:
                w.blockSignals(False)

            print("[NanoBanana] State restored successfully")

        except Exception as e:
            print("[NanoBanana] _restore_all_state_from_node error: {}".format(e))

    # ------------------------------------------------------------------
    # Lifecycle hooks – guarantee save before widget is destroyed / hidden
    # ------------------------------------------------------------------
    def hideEvent(self, event):
        self._save_all_state_to_node()
        super(NanoBananaWidget, self).hideEvent(event)

    def closeEvent(self, event):
        self._save_all_state_to_node()
        super(NanoBananaWidget, self).closeEvent(event)

    def event(self, ev):
        """Catch DeferredDelete (Nuke's PyCustom_Knob doesn't always fire
        hideEvent / closeEvent before destroying the widget)."""
        if ev.type() == QtCore.QEvent.DeferredDelete:
            self._save_all_state_to_node()
        return super(NanoBananaWidget, self).event(ev)

    def _start_generate(self):
        if not self.settings.api_key:
            self.status_label.setStyleSheet("color: #ef4444; font-size: 11px;")
            self.status_label.setText("Please set API key in Settings")
            nuke.message("API key not set.\nPlease open CompMind > Setting in the toolbar.")
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

        model = self.model_combo.currentData()
        ratio = self.ratio_combo.currentText()
        resolution = self.res_combo.currentData()

        if self.seed_random_chk.isChecked():
            seed = random.randint(0, 2147483647)  # Max value for Nuke Int_Knob
        else:
            try:
                seed = int(self.seed_input.text())
                seed = min(seed, 2147483647)  # Clamp to valid range
            except ValueError:
                seed = random.randint(0, 2147483647)

        self.status_label.setStyleSheet("color: #facc15; font-size: 11px;")
        self.status_label.setText("Collecting inputs...")
        self._toggle_stop_ui(True)

        node = self._get_owner_node()
        input_dir = get_input_directory()
        images_info = []
        
        if node:
            try:
                images_info = collect_input_images(node, input_dir)
                connected = [img for img in images_info if img["connected"]]
                self.inputs_info_label.setText("{} images connected".format(len(connected)))
            except Exception as e:
                print("Warning: Could not collect inputs: {}".format(str(e)))

        self.status_label.setText("Generating (Seed: {})...".format(seed))

        # Store generation params for creating Prompt node later
        gen_params = {
            "prompt": prompt,
            "neg_prompt": neg_prompt,
            "model": model,
            "ratio": ratio,
            "resolution": resolution,
            "seed": seed,
            "images_info": images_info,
            "generator_node": node
        }
        self._gen_params = gen_params

        output_dir = get_output_directory()
        worker = NanoBananaWorker(
            model, prompt, neg_prompt, ratio, resolution, seed, 
            images_info, output_dir, self.settings.api_key,
            gen_name=node.name() if node else "nanobanana"
        )
        self.current_worker = worker

        # Register worker in module-level dict to prevent GC when widget dies
        worker_id = id(worker)
        _active_workers[worker_id] = {"worker": worker, "params": gen_params}

        # Capture references for closures so they don't depend on self
        widget_ref = self

        # --- Register task in global status bar progress manager ---
        from ai_workflow.status_bar import task_progress_manager
        status_task_id = task_progress_manager.add_task(
            node.name() if node else "NanoBanana", "image")

        # ---- Direct callbacks (called from Worker.run() in background thread) ----
        # These are NOT Qt signal slots – they are plain Python function calls made
        # directly by the worker thread.  They are immune to Qt signal disconnection
        # that happens when Nuke destroys the PyCustom_Knob widget.

        def _direct_on_finished(path, metadata):
            """Direct callback from worker thread – always fires."""
            # Update global status bar
            s = metadata.get("seed", "N/A")
            task_progress_manager.complete_task(
                status_task_id, "Done! Seed: {}".format(s))

            # UI updates (safe to skip if widget is gone)
            def _update_ui():
                try:
                    if _isValid(widget_ref):
                        widget_ref._toggle_stop_ui(False)
                        widget_ref.status_label.setStyleSheet("color: #3CB371; font-size: 11px;")
                        widget_ref.status_label.setText("Done! Seed: {}".format(s))
                except Exception:
                    pass
            nuke.executeInMainThread(_update_ui)

            print("NanoBanana: Generation finished. Output path: {}".format(path))
            print("NanoBanana: Path exists: {}".format(os.path.exists(path) if path else False))

            if path and os.path.exists(path):
                params = gen_params

                def _create_nodes():
                    try:
                        gen_node = params["generator_node"]
                        gen_x = int(gen_node["xpos"].value())
                        gen_y = int(gen_node["ypos"].value())

                        # Find existing players connected to this generator
                        existing_players = []
                        for n in nuke.allNodes("Group"):
                            if "is_nb_player" in n.knobs() and n["is_nb_player"].value():
                                inp = n.input(0)
                                if inp and (inp.name() == gen_node.name()):
                                    existing_players.append(n)

                        player_num = len(existing_players) + 1
                        if existing_players:
                            last_p = max(existing_players, key=lambda nn: nn["ypos"].value())
                            px = int(last_p["xpos"].value()) + 200
                            py = int(last_p["ypos"].value())
                        else:
                            px = gen_x + 300
                            py = gen_y + 50

                        player_name = "Nano_Viewer{}".format(player_num)
                        _gen_imgs = gen_params.get("images_info", [])
                        print("[NB Main] create_nb_player_node with {} input_images".format(len(_gen_imgs)))
                        player_node, read_node = create_nb_player_node(
                            image_path=path,
                            name=player_name,
                            xpos=px,
                            ypos=py,
                            prompt=params["prompt"],
                            neg_prompt=params["neg_prompt"],
                            model=params["model"],
                            ratio=params["ratio"],
                            resolution=params["resolution"],
                            seed=params["seed"],
                            input_images=_gen_imgs,
                            gen_name=gen_node.name()
                        )
                        player_node.setInput(0, gen_node)

                        print("NanoBanana: Created NB Player '{}' with regeneration UI".format(player_name))
                        if read_node:
                            try:
                                nuke.connectViewer(0, read_node)
                            except Exception as e:
                                print("NanoBanana: Could not connect viewer: {}".format(e))
                    except Exception as e:
                        import traceback
                        print("NanoBanana: ERROR in _create_nodes: {}".format(e))
                        traceback.print_exc()
                    finally:
                        _active_workers.pop(worker_id, None)
                nuke.executeInMainThread(_create_nodes)
            else:
                print("NanoBanana: ERROR - No valid output path")
                _active_workers.pop(worker_id, None)
                nuke.executeInMainThread(nuke.message, args=("Generation completed but no image was created.\nPath: {}".format(path),))

        def _direct_on_error(err):
            """Direct callback from worker thread – always fires."""
            # Update global status bar
            task_progress_manager.error_task(status_task_id, str(err)[:80])

            def _update_ui():
                try:
                    if _isValid(widget_ref):
                        widget_ref._toggle_stop_ui(False)
                        widget_ref.status_label.setStyleSheet("color: #ef4444; font-size: 11px;")
                        widget_ref.status_label.setText("Error")
                except Exception:
                    pass
            nuke.executeInMainThread(_update_ui)
            _active_workers.pop(worker_id, None)
            nuke.executeInMainThread(nuke.message, args=("Generation Error:\n{}".format(err),))

        # Assign direct callbacks to worker (called from run() regardless of signal state)
        worker._on_finished_cb = _direct_on_finished
        worker._on_error_cb = _direct_on_error

        # Signal connections kept for UI updates while widget is alive
        worker.status_update.connect(
            lambda s: widget_ref.status_label.setText(s) if _isValid(widget_ref) else None)
        # Also update global status bar from status_update signal
        worker.status_update.connect(
            lambda s: task_progress_manager.update_status(status_task_id, s))
        worker.start()

    def _on_model_changed(self, index):
        """When model selection changes, adjust the max input count on the node.

        If reducing, delete ALL Inputs and recreate in reverse order.
        Mapping: imgK = input (max_inputs - K)
        """
        model_id = self.model_combo.currentData()
        max_inputs = MODEL_MAX_INPUTS.get(model_id, MAX_INPUT_IMAGES)

        node = self._get_owner_node()
        if not node or "nb_input_count" not in node.knobs():
            return

        # Store the model's max limit
        if "nb_max_inputs" not in node.knobs():
            k = nuke.Int_Knob("nb_max_inputs", "Max Inputs")
            k.setVisible(False)
            node.addKnob(k)
        node["nb_max_inputs"].setValue(max_inputs)

        current_count = int(node["nb_input_count"].value())

        if current_count > max_inputs:
            global _expanding_inputs
            _expanding_inputs = True
            try:
                print("[NanoBanana] _onModel: reduce {} -> {}"
                      .format(current_count, max_inputs))

                # Save: imgK = input (current_count - K)
                saved = {}
                for k in range(1, max_inputs + 1):
                    old_idx = current_count - k
                    conn = node.input(old_idx)
                    if conn is not None:
                        saved[k] = conn
                        print("[NanoBanana]   save: img{} <- '{}'"
                              .format(k, conn.name()))

                # Delete all
                node.begin()
                for inp in list(nuke.allNodes("Input")):
                    nuke.delete(inp)
                node.end()

                # Recreate reverse + number knob
                node.begin()
                for i in range(max_inputs, 0, -1):
                    inp = nuke.nodes.Input()
                    inp.setName("img{}".format(i))
                    inp["number"].setValue(max_inputs - i)  # imgK -> (count-K)
                    inp["xpos"].setValue((i - 1) * 100)
                    inp["ypos"].setValue(0)
                    print("[NanoBanana]   create: '{}' num={} #{}"
                          .format(inp.name(), max_inputs - i,
                                  max_inputs - i + 1))
                node.end()

                # Restore: imgK = input (max_inputs - K)
                set_indices = set()
                for k, conn_node in saved.items():
                    new_idx = max_inputs - k
                    node.setInput(new_idx, conn_node)
                    set_indices.add(new_idx)

                # Clear any auto-filled indices (Nuke setInput fills 0..N-1)
                for i in range(max_inputs):
                    if i not in set_indices:
                        node.setInput(i, None)

                node["nb_input_count"].setValue(max_inputs)
            finally:
                _expanding_inputs = False

    def _toggle_stop_ui(self, is_running):
        if is_running:
            self.gen_btn.setText("STOP")
            self.gen_btn.setObjectName("stopBtn")
            self.gen_btn.setStyleSheet("")  # clear inline style
            self.gen_btn.style().unpolish(self.gen_btn)
            self.gen_btn.style().polish(self.gen_btn)
            self.pbar.setRange(0, 0)
            self.pbar.setVisible(True)
        else:
            self.gen_btn.setText("GENERATE IMAGE")
            self.gen_btn.setObjectName("generateBtn")
            self.gen_btn.setStyleSheet("")  # clear inline style
            self.gen_btn.style().unpolish(self.gen_btn)
            self.gen_btn.style().polish(self.gen_btn)
            self.current_worker = None
            self.pbar.setVisible(False)
            self.pbar.reset()


# ---------------------------------------------------------------------------
# Prompt Node Regenerate Widget (Read-only record + editable regeneration UI)
# ---------------------------------------------------------------------------
class NanoBananaPromptWidget(QtWidgets.QWidget):
    """Widget for Prompt record nodes.
    Top section: read-only record of the original generation parameters.
    Bottom section: editable parameters (same as NanoBanana_Generate) for regeneration."""

    def __init__(self, node, parent=None):
        print("[NB Regen] >>> NanoBananaPromptWidget.__init__ node='{}'".format(
            node.name() if node else "None"))
        super(NanoBananaPromptWidget, self).__init__(parent)
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

        header_label = QtWidgets.QLabel("📦 Generation Record (Read Only)")
        header_label.setStyleSheet("color: #8b5cf6; font-weight: bold; font-size: 12px; background: transparent;")
        header_label.setAlignment(QtCore.Qt.AlignCenter)
        record_layout.addWidget(header_label)

        info_style = "color: #ccc; font-size: 11px; background: transparent;"
        label_style = "color: #888; font-size: 10px; font-weight: bold; background: transparent;"
        value_style = "color: #ccc; font-size: 11px; background: transparent; padding: 0px;"

        # Horizontal layout: Model | Ratio | Resolution | Seed side by side
        info_row = QtWidgets.QHBoxLayout()
        info_row.setSpacing(8)

        self._info_labels = {}
        fields = [
            ("Model", "model"),
            ("Ratio", "ratio"),
            ("Resolution", "resolution"),
            ("Seed", "seed"),
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
        prompt_header = QtWidgets.QLabel("Prompt:")
        prompt_header.setStyleSheet(label_style)
        record_layout.addWidget(prompt_header)

        self.prompt_display = QtWidgets.QPlainTextEdit()
        self.prompt_display.setReadOnly(True)
        self.prompt_display.setMaximumHeight(80)
        self.prompt_display.setStyleSheet("background: #2a2a2a; border: 1px solid #444; color: #ccc; font-size: 11px;")
        record_layout.addWidget(self.prompt_display)

        # Read-only negative prompt display
        neg_header = QtWidgets.QLabel("Negative:")
        neg_header.setStyleSheet(label_style)
        record_layout.addWidget(neg_header)

        self.neg_prompt_display = QtWidgets.QPlainTextEdit()
        self.neg_prompt_display.setReadOnly(True)
        self.neg_prompt_display.setMaximumHeight(50)
        self.neg_prompt_display.setStyleSheet("background: #2a2a2a; border: 1px solid #444; color: #ccc; font-size: 11px;")
        record_layout.addWidget(self.neg_prompt_display)

        # Hidden cached images info (kept for data but not displayed)
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

        # === Model ===
        self.model_combo = DropDownComboBox()
        fill_combo_from_options(self.model_combo, NB_MODEL_OPTIONS)
        main.addWidget(self.model_combo)

        # === Ratio + Resolution ===
        row2 = QtWidgets.QHBoxLayout()
        row2.setSpacing(6)

        self.ratio_combo = DropDownComboBox()
        fill_combo_from_options(self.ratio_combo, NB_RATIO_OPTIONS)

        self.res_combo = DropDownComboBox()
        fill_combo_from_options(self.res_combo, NB_RESOLUTION_OPTIONS)

        row2.addWidget(self.ratio_combo)
        row2.addWidget(self.res_combo)
        main.addLayout(row2)

        # === Seed ===
        row3 = QtWidgets.QHBoxLayout()
        row3.setSpacing(6)

        seed_label = QtWidgets.QLabel("Seed:")
        self.seed_input = QtWidgets.QLineEdit()
        self.seed_input.setPlaceholderText("Random Seed")
        self.seed_input.setValidator(QtGui.QIntValidator())
        self.seed_input.setEnabled(False)

        self.seed_random_chk = QtWidgets.QCheckBox("Random")
        self.seed_random_chk.setChecked(True)
        self.seed_random_chk.toggled.connect(lambda c: self.seed_input.setEnabled(not c))

        row3.addWidget(seed_label)
        row3.addWidget(self.seed_input, 1)
        row3.addWidget(self.seed_random_chk)
        main.addLayout(row3)

        # === Editable Prompt ===
        self.prompt_edit = QtWidgets.QTextEdit()
        self.prompt_edit.setPlaceholderText("Edit prompt and regenerate...")
        self.prompt_edit.setMinimumHeight(100)
        main.addWidget(self.prompt_edit)

        # === Editable Negative Prompt ===
        self.neg_prompt_edit = QtWidgets.QTextEdit()
        self.neg_prompt_edit.setPlaceholderText("Negative prompt (optional)...")
        self.neg_prompt_edit.setFixedHeight(70)
        main.addWidget(self.neg_prompt_edit)

        # === Image Reference Strip ===
        from ai_workflow.gemini_chat import ImageStrip, _ThumbCard
        self._ref_image_strip = ImageStrip(add_callback=self._add_ref_image)
        # Connect imagesChanged so any strip modification saves to node knob
        self._ref_image_strip.imagesChanged.connect(self._save_ref_images_to_node)
        main.addWidget(self._ref_image_strip)

        # === Regenerate Button ===
        self.regen_btn = QtWidgets.QPushButton("REGENERATE IMAGE")
        self.regen_btn.setObjectName("regenerateBtn")
        self.regen_btn.setMinimumHeight(42)
        self.regen_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.regen_btn.clicked.connect(self._regenerate)
        main.addWidget(self.regen_btn)

        # === Progress Bar ===
        self.pbar = QtWidgets.QProgressBar()
        self.pbar.setVisible(False)
        self.pbar.setTextVisible(False)
        self.pbar.setFixedHeight(6)
        main.addWidget(self.pbar)

        # === Status ===
        self.status_label = QtWidgets.QLabel("Ready")
        self.status_label.setAlignment(QtCore.Qt.AlignCenter)
        self.status_label.setStyleSheet("color: #666; font-size: 11px;")
        main.addWidget(self.status_label)

def _collect_input_images_for_round(gen_name):
    """Find cached input images for a specific generation round.

    When banana generates, upstream images are rendered to get_input_directory()
    with filenames:  {GenName}_input_img{K}_frame{K}.png
    This function finds all files matching this pattern — exactly the images
    used in THIS round, not other rounds' history.

    Args:
        gen_name: The NanoBanana_Generate node name (e.g. "NanoBanana_Generate1")

    Returns:
        List of sorted file path strings.
    """
    paths = []
    try:
        input_dir = get_input_directory()
        if not os.path.isdir(input_dir):
            return paths

        prefix = "{}_input_".format(gen_name)
        extensions = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
        for fname in sorted(os.listdir(input_dir)):
            if fname.startswith(prefix):
                ext = os.path.splitext(fname)[1].lower()
                if ext in extensions:
                    fpath = os.path.join(input_dir, fname).replace("\\", "/")
                    paths.append(fpath)

        print("[NB InputScan] Found {} image(s) for gen='{}' in input dir".format(
            len(paths), gen_name))
        for p in paths:
            exists = os.path.exists(p)
            print("  [NB InputScan]   -> {} [{}]".format(p, "OK" if exists else "MISSING"))

    except Exception as e:
        print("[NB InputScan] Error scanning input dir for '{}': {}".format(gen_name, e))

    return paths


def _find_generator_for_player(player_node):
    """Find which NanoBanana_Generate node fed into this Player/Prompt node.

    Walks upstream to find the generator. Also checks nb_gen_name knob
    (stored during creation) as fast path.
    """
    if not player_node:
        return ""

    # Fast path: check stored generator name
    if "nb_gen_name" in player_node.knobs():
        stored = player_node["nb_gen_name"].value() or ""
        if stored:
            return stored

    # Slow path: walk upstream looking for NanoBanana_Generate
    try:
        visited = set()
        queue = [player_node]
        while queue:
            cur = queue.pop(0)
            name = cur.name() if hasattr(cur, "name") else "?"
            if name in visited:
                continue
            visited.add(name)

            if hasattr(cur, "name") and isinstance(cur.name(), str) and \
               _is_generator_node(cur):
                return cur.name()

            max_inputs = getattr(cur, "inputs", lambda: 0)()
            for i in range(max_inputs):
                inp = cur.input(i)
                if inp:
                    queue.append(inp)
    except Exception as e:
        print("[NB InputScan] Error walking upstream: {}".format(e))

    return ""


def _collect_input_image_paths(node):
    """Main entry point: collect images for this generation round.

    Priority 1 (primary): Read nb_input_images JSON knob.
      - Contains user's last-saved image list (including manual edits).
      - Reliable on file re-open because Nuke serializes knob values.

    Priority 2 (fallback): Scan input cache dir via generator name.
      - Used when JSON knob is empty/missing (legacy nodes).
      - Matches {GenName}_input_img{K}_frame{K}.png pattern.
    """
    # Step 1: PRIMARY — try JSON knob first (user-edited image list)
    if node and "nb_input_images" in node.knobs():
        try:
            raw = node["nb_input_images"].value()
            print("[NB InputScan] Step 1: knob exists, raw length={} chars".format(len(raw) if raw else 0))
            if raw and raw.strip():
                paths = [p for p in json.loads(raw) if p]
                if paths:
                    found_count = sum(1 for p in paths if os.path.exists(p))
                    print("[NB InputScan] Primary (JSON): {} image(s), {} on disk".format(
                        len(paths), found_count))
                    return paths
                else:
                    print("[NB InputScan] Step 1: JSON parsed but empty list")
            else:
                print("[NB InputScan] Step 1: knob value is blank/empty")
        except Exception as e:
            print("[NB InputScan] JSON knob parse error: {}".format(e))
    else:
        print("[NB InputScan] Step 1: knob not found on node")

    # Step 2: FALLBACK — scan input cache directory by generator name
    gen_name = _find_generator_for_player(node)
    if gen_name:
        paths = _collect_input_images_for_round(gen_name)
        if paths:
            return paths

    print("[NB InputScan] No images found for '{}'".format(node.name() if node else "?"))
    return []




    def _load_from_node(self):
        """Load settings from node knobs.
        - Populates read-only record section from node knobs.
        - Pre-fills editable section with the same values for convenience."""
        if not self.node:
            return

        try:
            # --- Read-only record section ---
            model = self.node["nb_model"].value() if "nb_model" in self.node.knobs() else ""
            self._info_labels["model"].setText(model)

            ratio = self.node["nb_ratio"].value() if "nb_ratio" in self.node.knobs() else ""
            self._info_labels["ratio"].setText(ratio)

            resolution = self.node["nb_resolution"].value() if "nb_resolution" in self.node.knobs() else ""
            self._info_labels["resolution"].setText(resolution)

            seed = self.node["nb_seed"].value() if "nb_seed" in self.node.knobs() else 0
            self._info_labels["seed"].setText(str(int(seed)))

            prompt = self.node["nb_prompt"].value() if "nb_prompt" in self.node.knobs() else ""
            self.prompt_display.setPlainText(prompt)

            neg_prompt = self.node["nb_neg_prompt"].value() if "nb_neg_prompt" in self.node.knobs() else ""
            self.neg_prompt_display.setPlainText(neg_prompt)

            # Cached images info
            input_images_json = ""
            if "nb_input_images" in self.node.knobs():
                input_images_json = self.node["nb_input_images"].value()

            # Read node reference
            if "nb_read_node" in self.node.knobs():
                self.read_node_label.setText(self.node["nb_read_node"].value())

            # --- Pre-fill editable section with original values ---
            # Model
            model_map = {
                "gemini-3.1-flash-image-preview": 0,
                "gemini-3-pro-image-preview": 1,
                "gemini-2.5-flash-image": 2,
                "gemini-2.0-flash-exp-image-generation": 3,
                "imagen-3.0-generate-002": 4,
            }
            model_idx = model_map.get(model, 0)
            if 0 <= model_idx < self.model_combo.count():
                self.model_combo.setCurrentIndex(model_idx)

            # Ratio
            ratio_idx = self.ratio_combo.findText(ratio)
            if ratio_idx >= 0:
                self.ratio_combo.setCurrentIndex(ratio_idx)

            # Resolution
            res_idx = self.res_combo.findText(resolution)
            if res_idx >= 0:
                self.res_combo.setCurrentIndex(res_idx)

            # Seed
            self.seed_input.setText(str(int(seed)))
            self.seed_random_chk.setChecked(False)  # Default to using the recorded seed

            # Prompt
            self.prompt_edit.setText(prompt)
            self.neg_prompt_edit.setText(neg_prompt)

            # Load reference images from nb_input_images JSON knob.
            # Uses Multiline_Eval_String_Knob — no length limit, stores exactly
            # which images this generation round used.
            try:
                print("[NB Regen] >>> Loading input images from knob...")
                all_paths = _collect_input_image_paths(self.node)
                found_count = sum(1 for p in all_paths if os.path.exists(p))

                print("[NB Regen]     TOTAL {} paths ({} found on disk)".format(
                    len(all_paths), found_count))
                for vp in all_paths:
                    print("  [NB Regen]       -> {} [{}]".format(vp, "OK" if os.path.exists(vp) else "MISSING"))

                if all_paths:
                    self.cached_info_label.setText(
                        "{} image(s) ({} available)".format(len(all_paths), found_count))
                    for p in all_paths:
                        self._ref_image_strip.add_image(p)
                    print("[NB Regen]     DONE - added {} images to strip".format(len(all_paths)))
                else:
                    self.cached_info_label.setText("Text-only generation")
                    self._ref_image_strip.clear_images()
                    print("[NB Regen]     No images stored")
            except Exception as ex:
                print("[NB Regen]     ERROR loading images: {}".format(ex))
                import traceback
                traceback.print_exc()
                self._ref_image_strip.clear_images()

        except Exception as e:
            print("NanoBanana: Error loading node settings: {}".format(str(e)))

    def _add_ref_image(self):
        """Open file dialog to add a reference image."""
        fpath, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Add Reference Image", "",
            "Images (*.png *.jpg *.jpeg *.gif *.webp);;All Files (*)"
        )
        if fpath and os.path.isfile(fpath):
            self._ref_image_strip.add_image(fpath)
            self._save_ref_images_to_node()

    def _remove_ref_image_callback(self, path):
        """Called when a thumbnail's remove button is clicked."""
        self._ref_image_strip.remove_image(path) if hasattr(self._ref_image_strip, 'remove_image') else None
        if path in [img.get("path") for img in getattr(self, '_cached_ref_imgs', [])]:
            self._save_ref_images_to_node()

    def _save_ref_images_to_node(self):
        """Save current reference images list to node knob."""
        if not self.node or "nb_input_images" not in self.node.knobs():
            return
        paths = self._ref_image_strip.images
        self.node["nb_input_images"].setValue(json.dumps(paths))

    def _regenerate(self):
        """Regenerate using the editable parameters and cached input images."""
        if not self.settings.api_key:
            self.status_label.setStyleSheet("color: #ef4444; font-size: 11px;")
            self.status_label.setText("Please set API key in Settings")
            nuke.message("API key not set.\nPlease open CompMind > Setting in the toolbar.")
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

        model = self.model_combo.currentData()
        ratio = self.ratio_combo.currentText()
        resolution = self.res_combo.currentData()

        if self.seed_random_chk.isChecked():
            seed = random.randint(0, 2147483647)
        else:
            try:
                seed = int(self.seed_input.text())
                seed = min(seed, 2147483647)
            except ValueError:
                seed = random.randint(0, 2147483647)

        # Collect input images: prefer strip images, fall back to cached
        images_info = []
        strip_images = self._ref_image_strip.images if hasattr(self, '_ref_image_strip') else []
        source_images = strip_images if strip_images else []
        # Also merge with cached images not already in strip
        if "nb_input_images" in self.node.knobs():
            try:
                cached_paths = json.loads(self.node["nb_input_images"].value())
                for p in cached_paths:
                    if p and os.path.exists(p) and p not in source_images:
                        source_images.append(p)
            except:
                pass
        for idx, p in enumerate(source_images):
            images_info.append({
                "index": idx,
                "name": "img{}".format(idx + 1),
                "path": p,
                "connected": True,
                "node_name": "user_ref" if idx < len(strip_images) else "cached"
            })

        self.status_label.setStyleSheet("color: #facc15; font-size: 11px;")
        self.status_label.setText("Regenerating (Seed: {})...".format(seed))
        self._toggle_ui(True)

        output_dir = get_output_directory()

        # Find generator name (stored during node creation)
        gen_name = "nanobanana"
        if "nb_gen_name" in self.node.knobs():
            gen_name = self.node["nb_gen_name"].value() or gen_name

        worker = NanoBananaWorker(
            model, prompt, neg_prompt, ratio, resolution, seed,
            images_info, output_dir, self.settings.api_key,
            gen_name=gen_name
        )
        self.current_worker = worker

        worker_id = id(worker)
        _active_workers[worker_id] = {"worker": worker, "params": {}}

        widget_ref = self
        node_ref = self.node

        def _on_finished(path, metadata):
            try:
                _alive = _isValid(widget_ref)
                if _alive:
                    widget_ref._toggle_ui(False)
                    s = metadata.get("seed", "N/A")
                    widget_ref.status_label.setStyleSheet("color: #3CB371; font-size: 11px;")
                    widget_ref.status_label.setText("Done! Seed: {}".format(s))
            except Exception:
                pass

            if path and os.path.exists(path):
                def _update():
                    try:
                        read_node = update_prompt_read_node(node_ref, path)
                        if read_node:
                            try:
                                nuke.connectViewer(0, read_node)
                            except:
                                pass
                    except Exception as e:
                        print("NanoBanana: ERROR updating Read node: {}".format(e))
                    finally:
                        _active_workers.pop(worker_id, None)
                nuke.executeInMainThread(_update)
            else:
                _active_workers.pop(worker_id, None)

        def _on_error(err):
            try:
                _alive = _isValid(widget_ref)
                if _alive:
                    widget_ref._toggle_ui(False)
                    widget_ref.status_label.setStyleSheet("color: #ef4444; font-size: 11px;")
                    widget_ref.status_label.setText("Error")
            except Exception:
                pass
            _active_workers.pop(worker_id, None)
            nuke.executeInMainThread(nuke.message, args=("Regeneration Error:\n{}".format(err),))

        worker.finished.connect(_on_finished)
        worker.error.connect(_on_error)
        worker.status_update.connect(
            lambda s: widget_ref.status_label.setText(s) if _isValid(widget_ref) else None)
        worker.start()

    def _toggle_ui(self, is_running):
        if is_running:
            self.regen_btn.setText("STOP")
            self.regen_btn.setObjectName("stopBtn")
            self.regen_btn.setStyleSheet("")
            self.regen_btn.style().unpolish(self.regen_btn)
            self.regen_btn.style().polish(self.regen_btn)
            self.pbar.setRange(0, 0)
            self.pbar.setVisible(True)
        else:
            self.regen_btn.setText("REGENERATE IMAGE")
            self.regen_btn.setObjectName("regenerateBtn")
            self.regen_btn.setStyleSheet("")
            self.regen_btn.style().unpolish(self.regen_btn)
            self.regen_btn.style().polish(self.regen_btn)
            self.current_worker = None
            self.pbar.setVisible(False)
            self.pbar.reset()


# ---------------------------------------------------------------------------
# Generation Worker Thread
# ---------------------------------------------------------------------------
class NanoBananaWorker(QtCore.QThread):
    finished = QtCore.Signal(str, dict)
    error = QtCore.Signal(str)
    status_update = QtCore.Signal(str)

    def __init__(self, model_name, prompt, neg_prompt, aspect_ratio, resolution, seed, 
                 images_info=None, temp_dir=None, api_key=None, gen_name="nanobanana",
                 on_finished_callback=None, on_error_callback=None):
        super(NanoBananaWorker, self).__init__()
        self.model_name = model_name
        self.prompt = prompt
        self.neg_prompt = neg_prompt
        self.aspect_ratio = aspect_ratio
        self.resolution = resolution
        self.seed = seed
        self.images_info = images_info or []
        self.temp_dir = temp_dir
        self.api_key = api_key
        self.gen_name = gen_name
        self.is_running = True
        # Direct callbacks that bypass Qt signal system – immune to widget destruction
        self._on_finished_cb = on_finished_callback
        self._on_error_cb = on_error_callback

    def stop(self):
        self.is_running = False

    def run(self):
        try:
            self.status_update.emit("Preparing request...")
            
            connected_images = [img for img in self.images_info if img.get("connected") and img.get("path")]
            
            parts = []
            
            # Build prompt with image references
            # Each image is labeled as [Image 1], [Image 2], etc. so user can reference them
            if connected_images:
                # Add image reference guide at the beginning
                image_labels = []
                for idx, img_info in enumerate(connected_images):
                    label = "Image {} (Input {})".format(idx + 1, img_info.get("name", "img{}".format(idx + 1)))
                    image_labels.append(label)
                
                # Insert each image with its label: text label -> image data -> text label -> image data ...
                for idx, img_info in enumerate(connected_images):
                    if img_info["path"] and os.path.exists(img_info["path"]):
                        # Add text label before each image
                        parts.append({"text": "[img{}]".format(idx + 1)})
                        base64_data = image_to_base64(img_info["path"])
                        if base64_data:
                            parts.append({
                                "inlineData": {
                                    "mimeType": get_mime_type(img_info["path"]),
                                    "data": base64_data
                                }
                            })
            
            # Add the user prompt after all images
            full_prompt = self.prompt
            if self.neg_prompt:
                full_prompt += "\n\nAvoid: " + self.neg_prompt
            
            # If there are images, add a hint about image referencing
            if connected_images:
                full_prompt = ("You have {} reference image(s) labeled [img1] through [img{}]. "
                              "The user refers to them as img1, img2, img3, etc.\n\n"
                              "User request: {}").format(
                    len(connected_images), len(connected_images), full_prompt
                )
            
            parts.append({"text": full_prompt})
            
            contents = [{"parts": parts}]
            generation_config = {"responseModalities": ["TEXT", "IMAGE"]}

            # Add imageConfig for aspect ratio and resolution
            image_config = {}
            if self.aspect_ratio and self.aspect_ratio != "Auto":
                image_config["aspectRatio"] = self.aspect_ratio
            if self.resolution:
                image_config["imageSize"] = self.resolution
            if image_config:
                generation_config["imageConfig"] = image_config
            
            if not self.is_running:
                # User cancelled before API call – invoke error callback to clean up
                if self._on_error_cb:
                    self._on_error_cb("Generation cancelled by user")
                return
            
            self.status_update.emit("Calling API ({} images)...".format(len(connected_images)))

            # Save the full request payload as JSON for debugging
            log_path = ""
            try:
                # Build a loggable version of contents (without base64 image data)
                log_parts = []
                for p in parts:
                    if "text" in p:
                        log_parts.append({"text": p["text"]})
                    elif "inlineData" in p:
                        log_parts.append({
                            "inlineData": {
                                "mimeType": p["inlineData"].get("mimeType", ""),
                                "data": "<base64 {} bytes>".format(
                                    len(p["inlineData"].get("data", "")))
                            }
                        })

                request_log = {
                    "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "model": self.model_name,
                    "prompt": self.prompt,
                    "negative_prompt": self.neg_prompt,
                    "seed": self.seed,
                    "aspect_ratio": self.aspect_ratio,
                    "resolution": self.resolution,
                    "connected_image_count": len(connected_images),
                    "connected_image_paths": [img.get("path", "") for img in connected_images],
                    "generation_config": generation_config,
                    "contents_preview": [{"parts": log_parts}],
                }

                log_filename = "banana_request_{}.json".format(
                    datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
                # Use logs directory (project-aware)
                try:
                    logs_dir = get_logs_directory()
                except Exception:
                    logs_dir = self.temp_dir
                log_path = os.path.join(logs_dir, log_filename)
                with open(log_path, "w", encoding="utf-8") as f:
                    json.dump(request_log, f, indent=2, ensure_ascii=False)
                print("NanoBanana: Request payload saved to {}".format(log_path))
                print("NanoBanana: Request payload:\n{}".format(
                    json.dumps(request_log, indent=2, ensure_ascii=False)))
            except Exception as log_err:
                print("NanoBanana: Failed to save request log: {}".format(log_err))

            success, result = call_gemini_api(
                self.api_key, self.model_name, contents, generation_config
            )
            
            if not self.is_running:
                # User cancelled after API call – invoke error callback to clean up
                if self._on_error_cb:
                    self._on_error_cb("Generation cancelled by user")
                return
            
            if not success:
                err_msg = "API Error: {}".format(result)
                self.error.emit(err_msg)
                if self._on_error_cb:
                    self._on_error_cb(err_msg)
                return
            
            self.status_update.emit("Processing response...")
            
            output_path, extract_error = extract_image_from_response(result, self.temp_dir, self.gen_name)
            
            if extract_error:
                err_msg = "Failed to extract image: {}".format(extract_error)
                self.error.emit(err_msg)
                if self._on_error_cb:
                    self._on_error_cb(err_msg)
                return
            
            metadata = {
                "model": self.model_name,
                "prompt": self.prompt,
                "seed": self.seed,
                "ratio": self.aspect_ratio,
            }
            
            self.status_update.emit("Image generated!")
            self.finished.emit(output_path, metadata)
            # Direct callback – works even if widget (signal receiver) is destroyed
            if self._on_finished_cb:
                self._on_finished_cb(output_path, metadata)
            
        except Exception as e:
            err_msg = "Error: {}".format(str(e))
            self.error.emit(err_msg)
            if self._on_error_cb:
                self._on_error_cb(err_msg)


# ---------------------------------------------------------------------------
# Node Creation Functions
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Knob Widget Wrappers (for PyCustom_Knob)
# ---------------------------------------------------------------------------

class NanoBananaPlayerRegenWidget(QtWidgets.QWidget):
    """Widget for NB Player node's Regenerate tab.
    
    Shows the generation record (read-only) at top,
    and editable parameters + REGENERATE button below.
    Replaces the old Prompt node's functionality.
    """

    def __init__(self):
        super(NanoBananaPlayerRegenWidget, self).__init__()
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        try:
            self.node = nuke.thisNode()
        except Exception:
            self.node = None

        self.panel = _NanoBananaPlayerRegenPanel(self.node, parent=self)
        layout.addWidget(self.panel)

    def makeUI(self):
        return self

    def updateValue(self):
        print("[NB Player2] >>> updateValue() called by Nuke")
        try:
            if hasattr(self, 'panel'):
                self.panel._save_state_to_node()
        except Exception:
            pass


class _NanoBananaPlayerRegenPanel(QtWidgets.QWidget):
    """The actual panel content for Player regeneration UI."""

    def __init__(self, node=None, parent=None):
        print("[NB Player2] >>> _NanoBananaPlayerRegenPanel.__init__ node='{}'".format(
            node.name() if node else "None"))
        super(_NanoBananaPlayerRegenPanel, self).__init__(parent)
        self.node = node
        self.setObjectName("nbPlayerRegenRoot")
        self.setStyleSheet(NANOBANANA_STYLE)
        self.setMinimumWidth(380)
        font = self.font()
        font.setStyleStrategy(QtGui.QFont.NoSubpixelAntialias)
        self.setFont(font)

        self.settings = NanoBananaSettings()
        self.current_worker = None
        self._build_ui()

        if node:
            self._load_from_node(node)

    def _build_ui(self):
        main = QtWidgets.QVBoxLayout(self)
        main.setSpacing(6)
        main.setContentsMargins(8, 8, 8, 8)

        # --- Read-only Generation Record ---
        record_frame = QtWidgets.QFrame()
        record_frame.setStyleSheet(
            "QFrame { background: #1a1a1a; border: 1px solid #444; border-radius: 4px; }")
        rec_layout = QtWidgets.QVBoxLayout(record_frame)
        rec_layout.setSpacing(4)
        rec_layout.setContentsMargins(8, 8, 8, 8)

        header = QtWidgets.QLabel("Regenerate (edit params below)")
        header.setStyleSheet(
            "color: #facc15; font-weight: bold; font-size: 12px; background: transparent;")
        header.setAlignment(QtCore.Qt.AlignCenter)
        rec_layout.addWidget(header)

        # Model | Ratio | Resolution | Seed info row
        label_style = "color: #888; font-size: 10px; font-weight: bold; background: transparent;"
        value_style = "color: #ccc; font-size: 11px; background: transparent; padding: 0px;"
        info_row = QtWidgets.QHBoxLayout()
        info_row.setSpacing(8)

        self.info_labels = {}
        fields = [("Model", "model"), ("Ratio", "ratio"),
                  ("Resolution", "resolution"), ("Seed", "seed")]
        for idx, (display_name, key) in enumerate(fields):
            col = QtWidgets.QVBoxLayout()
            col.setSpacing(1)
            lbl = QtWidgets.QLabel(display_name)
            lbl.setStyleSheet(label_style)
            lbl.setAlignment(QtCore.Qt.AlignCenter)
            val = QtWidgets.QLabel("")
            val.setStyleSheet(value_style)
            val.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            col.addWidget(lbl)
            col.addWidget(val)
            info_row.addLayout(col)
            self.info_labels[key] = val

            if idx < len(fields) - 1:
                sep = QtWidgets.QFrame()
                sep.setFrameShape(QtWidgets.QFrame.VLine)
                sep.setStyleSheet("color: #444;")
                info_row.addWidget(sep)

        rec_layout.addLayout(info_row)

        # Prompt (read-only display)
        prompt_hdr = QtWidgets.QLabel("Prompt:")
        prompt_hdr.setStyleSheet(label_style)
        rec_layout.addWidget(prompt_hdr)
        self.prompt_display = QtWidgets.QPlainTextEdit()
        self.prompt_display.setReadOnly(True)
        self.prompt_display.setMaximumHeight(80)
        self.prompt_display.setStyleSheet(
            "background: #2a2a2a; border: 1px solid #444; color: #ccc; font-size: 11px;")
        rec_layout.addWidget(self.prompt_display)

        neg_hdr = QtWidgets.QLabel("Negative:")
        neg_hdr.setStyleSheet(label_style)
        rec_layout.addWidget(neg_hdr)
        self.neg_prompt_display = QtWidgets.QPlainTextEdit()
        self.neg_prompt_display.setReadOnly(True)
        self.neg_prompt_display.setMaximumHeight(50)
        self.neg_prompt_display.setStyleSheet(
            "background: #2a2a2a; border: 1px solid #444; color: #ccc; font-size: 11px;")
        rec_layout.addWidget(self.neg_prompt_display)

        main.addWidget(record_frame)

        # --- Editable Parameters Section ---

        # Model combo
        self.model_combo = DropDownComboBox()
        fill_combo_from_options(self.model_combo, NB_MODEL_OPTIONS)
        main.addWidget(self.model_combo)

        row2 = QtWidgets.QHBoxLayout()
        row2.setSpacing(6)
        self.ratio_combo = DropDownComboBox()
        fill_combo_from_options(self.ratio_combo, NB_RATIO_OPTIONS)
        self.res_combo = DropDownComboBox()
        fill_combo_from_options(self.res_combo, NB_RESOLUTION_OPTIONS)
        row2.addWidget(self.ratio_combo)
        row2.addWidget(self.res_combo)
        main.addLayout(row2)

        # Seed
        row3 = QtWidgets.QHBoxLayout()
        row3.setSpacing(6)
        seed_lbl = QtWidgets.QLabel("Seed:")
        seed_lbl.setStyleSheet("color: #aaa; font-size: 11px;")
        self.seed_input = QtWidgets.QLineEdit()
        self.seed_input.setPlaceholderText("Random")
        self.seed_input.setValidator(QtGui.QIntValidator())
        self.seed_input.setEnabled(False)
        self.random_chk = QtWidgets.QCheckBox("Random")
        self.random_chk.setChecked(True)
        self.random_chk.toggled.connect(lambda c: self.seed_input.setEnabled(not c))
        row3.addWidget(seed_lbl)
        row3.addWidget(self.seed_input, 1)
        row3.addWidget(self.random_chk)
        main.addLayout(row3)

        # Editable prompt
        self.prompt_edit = QtWidgets.QTextEdit()
        self.prompt_edit.setPlaceholderText("Edit prompt to regenerate...")
        self.prompt_edit.setMinimumHeight(80)
        main.addWidget(self.prompt_edit)

        self.neg_edit = QtWidgets.QTextEdit()
        self.neg_edit.setPlaceholderText("Negative prompt (optional)...")
        self.neg_edit.setFixedHeight(70)
        main.addWidget(self.neg_edit)

        # Image Reference Strip
        from ai_workflow.gemini_chat import ImageStrip, _ThumbCard
        self._ref_image_strip = ImageStrip(add_callback=self._add_ref_image)
        # Connect imagesChanged so any strip modification saves to node knob
        self._ref_image_strip.imagesChanged.connect(self._save_ref_images_to_node)
        main.addWidget(self._ref_image_strip)

        # REGENERATE BUTTON
        self.regen_btn = QtWidgets.QPushButton("REGENERATE IMAGE")

        # REGENERATE BUTTON
        self.regen_btn = QtWidgets.QPushButton("REGENERATE IMAGE")
        self.regen_btn.setObjectName("regenerateBtn")
        self.regen_btn.setMinimumHeight(42)
        self.regen_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.regen_btn.clicked.connect(self._on_regenerate)
        main.addWidget(self.regen_btn)

        # Progress bar
        self.pbar = QtWidgets.QProgressBar()
        self.pbar.setVisible(False)
        self.pbar.setTextVisible(False)
        self.pbar.setFixedHeight(6)
        main.addWidget(self.pbar)

        # Status label
        self.status_label = QtWidgets.QLabel("")
        self.status_label.setStyleSheet("color: #888; font-size: 11px; background: transparent;")
        main.addWidget(self.status_label)

    def _load_from_node(self, node):
        """Load stored parameters from the Player node's hidden knobs into the UI."""
        if not node:
            return

        for key, knob_name, cast_fn in [
            ("model", "nb_model", str), ("ratio", "nb_ratio", str),
            ("resolution", "nb_resolution", str), ("seed", "nb_seed", int),
        ]:
            if knob_name in node.knobs():
                val = node[knob_name].value()
                try:
                    val = cast_fn(val)
                    self.info_labels[key].setText(str(val))
                except:
                    pass

        for knob_key, display_widget in [("nb_prompt", self.prompt_display),
                                         ("nb_neg_prompt", self.neg_prompt_display)]:
            if knob_key in node.knobs():
                display_widget.setPlainText(node[knob_key].value() or "")

        if "nb_model" in node.knobs():
            model_val = node["nb_model"].value()
            for i in range(self.model_combo.count()):
                if self.model_combo.itemData(i) == model_val:
                    self.model_combo.setCurrentIndex(i)
                    break

        if "nb_ratio" in node.knobs():
            ratio_val = node["nb_ratio"].value()
            ratio_idx = self.ratio_combo.findText(ratio_val)
            if ratio_idx >= 0:
                self.ratio_combo.setCurrentIndex(ratio_idx)

        if "nb_resolution" in node.knobs():
            res_val = node["nb_resolution"].value()
            res_idx = self.res_combo.findText(res_val)
            if res_idx >= 0:
                self.res_combo.setCurrentIndex(res_idx)

        if "nb_seed" in node.knobs():
            s = int(node["nb_seed"].value())
            if s <= 0:
                self.random_chk.setChecked(True)
            else:
                self.random_chk.setChecked(False)
                self.seed_input.setText(str(s))

        if "nb_prompt" in node.knobs():
            self.prompt_edit.setPlainText(node["nb_prompt"].value() or "")
        if "nb_neg_prompt" in node.knobs():
            self.neg_edit.setPlainText(node["nb_neg_prompt"].value() or "")
        # Load cached reference images from upstream DAG connections instead
        # of JSON-serialized knobs.  This is 100% reliable.
        # Show raw knob value for debugging
        raw_knob = ""
        if "nb_input_images" in node.knobs():
            try: raw_knob = node["nb_input_images"].value()
            except: pass
        print("[NB Player2]   Raw nb_input_images knob value ({} chars): '{}'".format(
            len(raw_knob), raw_knob[:200] if raw_knob else ""))
        try:
            print("[NB Player2] >>> Scanning upstream for input images from '{}'...".format(
                node.name() if node else "None"))
            all_paths = _collect_input_image_paths(node)
            found = sum(1 for p in all_paths if os.path.exists(p))
            print("[NB Player2]     TOTAL {} paths ({} found on disk)".format(len(all_paths), found))

            for vp in all_paths:
                print("  [NB Player2]       -> {} [{}]".format(vp, "OK" if os.path.exists(vp) else "MISSING"))
                self._ref_image_strip.add_image(vp)
            if all_paths:
                print("[NB Player2]     DONE - {} images added to strip".format(len(all_paths)))
            else:
                self._ref_image_strip.clear_images()
                print("[NB Player2]     No images stored")
        except Exception:
            self._ref_image_strip.clear_images()

    def _add_ref_image(self):
        fpath, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Add Reference Image", "",
            "Images (*.png *.jpg *.jpeg *.gif *.webp);;All Files (*)"
        )
        if fpath and os.path.isfile(fpath):
            self._ref_image_strip.add_image(fpath)
            self._save_ref_images_to_node()

    def _save_ref_images_to_node(self):
        print("[NB Player2] >>> _save_ref_images_to_node: strip has {} images".format(
            len(self._ref_image_strip.images) if hasattr(self, '_ref_image_strip') else 0))
        for i, p in enumerate(self._ref_image_strip.images):
            print("  [NB Player2]   images[{}]: '{}'".format(i, p))
        if not self.node or "nb_input_images" not in self.node.knobs():
            print("[NB Player2]     SKIPPED - no node or no knob")
            return
        paths = self._ref_image_strip.images
        json_val = json.dumps(paths)
        self.node["nb_input_images"].setValue(json_val)
        # Verify write-back
        verify = ""
        try: verify = self.node["nb_input_images"].value()
        except: pass
        print("[NB Player2]     SAVED {} paths ({} chars), verified={} chars".format(
            len(paths), len(json_val), len(verify)))

    def _remove_ref_image_callback(self, path):
        """Called when a thumbnail's remove button is clicked."""
        if hasattr(self._ref_image_strip, 'remove_image'):
            self._ref_image_strip.remove_image(path)
        self._save_ref_images_to_node()

    def _save_state_to_node(self):
        """Save current editable values back to the node's hidden knobs."""
        print("[NB Player2] >>> _save_state_to_node called")
        if not self.node:
            print("[NB Player2]     SKIP - no node")
            return
        try:
            if "nb_model" in self.node.knobs():
                self.node["nb_model"].setValue(str(self.model_combo.currentData()))
            if "nb_ratio" in self.node.knobs():
                self.node["nb_ratio"].setValue(self.ratio_combo.currentText())
            if "nb_resolution" in self.node.knobs():
                self.node["nb_resolution"].setValue(self.res_combo.currentText())
            if "nb_seed" in self.node.knobs():
                if self.random_chk.isChecked():
                    self.node["nb_seed"].setValue(0)
                else:
                    try:
                        self.node["nb_seed"].setValue(int(self.seed_input.text()) or 0)
                    except ValueError:
                        self.node["nb_seed"].setValue(0)
            if "nb_prompt" in self.node.knobs():
                self.node["nb_prompt"].setValue(self.prompt_edit.toPlainText())
            if "nb_neg_prompt" in self.node.knobs():
                self.node["nb_neg_prompt"].setValue(self.neg_edit.toPlainText())
            # NOTE: Do NOT save nb_input_images here!
            # Images are saved independently via _save_ref_images_to_node()
            # which is called only on explicit add/remove actions.
            # Saving here risks overwriting persisted paths with an empty list
            # if updateValue() fires before _load_from_node() completes.
        except Exception as e:
            print("[NB Player Regen] Error saving state: {}".format(e))

    def _toggle_ui(self, is_running):
        if is_running:
            self.regen_btn.setText("STOP")
            self.regen_btn.setObjectName("stopBtn")
            self.regen_btn.setStyleSheet("")
            self.regen_btn.style().unpolish(self.regen_btn)
            self.regen_btn.style().polish(self.regen_btn)
            self.pbar.setRange(0, 0)
            self.pbar.setVisible(True)
        else:
            self.regen_btn.setText("REGENERATE IMAGE")
            self.regen_btn.setObjectName("regenerateBtn")
            self.regen_btn.setStyleSheet("")
            self.regen_btn.style().unpolish(self.regen_btn)
            self.regen_btn.style().polish(self.regen_btn)
            self.current_worker = None
            self.pbar.setVisible(False)
            self.pbar.reset()

    def _on_regenerate(self):
        """Handle regenerate button click - read params from Player node and re-generate."""
        print("[NB Player2] ===== _on_regenerate START =====")
        if not self.node:
            nuke.message("No associated node.")
            return

        if not self.settings.api_key:
            nuke.message("API key not set.\nPlease open CompMind > Setting in the toolbar.")
            return

        # Log current state BEFORE saving
        strip_imgs_before = self._ref_image_strip.images if hasattr(self, '_ref_image_strip') else []
        knob_val_before = ""
        if "nb_input_images" in self.node.knobs():
            try: knob_val_before = self.node["nb_input_images"].value()
            except: pass
        print("[NB Player2]   Before save: strip has {} images, knob has {} chars".format(
            len(strip_imgs_before), len(knob_val_before)))
        for i, p in enumerate(strip_imgs_before): print("     strip[{}]: {}".format(i, p))

        self._save_state_to_node()

        # Log state AFTER saving
        knob_val_after = ""
        if "nb_input_images" in self.node.knobs():
            try: knob_val_after = self.node["nb_input_images"].value()
            except: pass
        print("[NB Player2]   After save:  knob has {} chars".format(len(knob_val_after)))

        model = ""
        ratio = "auto"
        resolution = "1K"
        seed = 0
        prompt_text = ""
        neg_text = ""

        for knob_key, target_var in [
            ("nb_model", None), ("nb_ratio", None), ("nb_resolution", None),
            ("nb_seed", None), ("nb_prompt", None), ("nb_neg_prompt", None),
        ]:
            if knob_key in self.node.knobs():
                val = self.node[knob_key].value()
                if knob_key == "nb_model":
                    model = val
                elif knob_key == "nb_ratio":
                    ratio = val or "auto"
                elif knob_key == "nb_resolution":
                    resolution = val or "1K"
                elif knob_key == "nb_seed":
                    seed = int(val) or 0
                elif knob_key == "nb_prompt":
                    prompt_text = val or ""
                elif knob_key == "nb_neg_prompt":
                    neg_text = val or ""

        if not model:
            nuke.message("No model set on this player node.")
            return

        output_dir = get_output_directory()
        if not os.path.isdir(output_dir):
            output_dir = os.path.join(os.path.expanduser("~"), ".nuke", "AI_Output")

        if self.random_chk.isChecked() or seed <= 0:
            import random as _rnd
            seed = _rnd.randint(1, 999999999)

        self.status_label.setStyleSheet("color: #facc15; font-size: 11px;")
        self.status_label.setText("Generating...")
        self._toggle_ui(True)

        # Use stored generator name for consistent file naming
        gen_name = "NB_Regen_{}".format(int(time.time()))
        if "nb_gen_name" in self.node.knobs():
            gen_name = self.node["nb_gen_name"].value() or gen_name

        # Collect reference images from strip (same order as displayed)
        images_info = []
        if hasattr(self, '_ref_image_strip'):
            for idx, p in enumerate(self._ref_image_strip.images):
                if p and os.path.exists(p):
                    images_info.append({
                        "index": idx,
                        "name": "img{}".format(idx + 1),
                        "path": p,
                        "connected": True,
                        "node_name": "user_ref",
                    })

        print("[NB Player2] _on_regenerate: using {} image(s) for regeneration".format(len(images_info)))

        worker = NanoBananaWorker(
            model, prompt_text, neg_text, ratio, resolution, seed,
            images_info, output_dir, self.settings.api_key,
            gen_name=gen_name
        )
        self.current_worker = worker
        widget_ref = self
        node_ref = self.node
        # Mutable container so nested closures can update the reference
        # after Replacement Jutsu rebuilds the node (avoids nonlocal).
        _refs = {"node": node_ref}

        # --- Register in global status bar progress manager ---
        try:
            from ai_workflow.status_bar import task_progress_manager
            status_tid = task_progress_manager.add_task(node_ref.name(), "image")
            worker.status_update.connect(
                lambda s: task_progress_manager.update_status(status_tid, s))
        except Exception:
            status_tid = None

        worker_id = id(worker)
        _active_workers[worker_id] = {"worker": worker, "params": {}}

        # ---- Direct callbacks (called from Worker.run() — immune to Qt signal loss) ----
        def _direct_on_finished(path, metadata):
            """Called from worker thread when generation succeeds."""
            # Complete global status bar task
            if status_tid:
                try:
                    from ai_workflow.status_bar import task_progress_manager as _tpm
                    _tpm.complete_task(status_tid, "Image generated!")
                except Exception:
                    pass

            s = metadata.get("seed", "N/A")

            def _update_ui():
                cur_node = _refs["node"]
                try:
                    widget_ref._toggle_ui(False)
                    widget_ref.status_label.setStyleSheet("color: #3CB371; font-size: 11px;")
                    widget_ref.status_label.setText("Done! Seed: {}".format(s))
                    widget_ref.info_labels["seed"].setText(str(s))
                except Exception:
                    pass

                if path and os.path.exists(path):
                    try:
                        internal_read = _get_internal_read_nb(cur_node)
                        if internal_read:
                            internal_read["file"].fromUserText(path)
                            cur_node["nb_file"].setValue(path.replace("\\", "/"))
                            cur_node["nb_output_path"].setValue(path.replace("\\", "/"))
                            new_seed = metadata.get("seed", s)
                            widget_ref.info_labels["seed"].setText(str(new_seed))
                            cur_node["nb_seed"].setValue(int(new_seed) if new_seed else 0)
                            # --- Replacement Jutsu: rebuild Group for fresh thumbnail ---
                            rebuilt = _rebuild_group_for_thumbnail(cur_node, path)
                            if rebuilt:
                                # Update references — old cur_node is now deleted
                                _refs["node"] = rebuilt
                                widget_ref.node = rebuilt
                                cur_node = rebuilt
                                # Re-fetch InternalRead from the new node
                                internal_read = _get_internal_read_nb(rebuilt)
                            else:
                                # Fallback: legacy thumbnail update
                                _update_node_thumbnail(cur_node, path)
                            if internal_read:
                                nuke.connectViewer(0, internal_read)
                    except Exception as e:
                        print("[NB Player Regen] ERROR updating: {}".format(e))

                # CRITICAL: re-save image list AFTER all knob modifications.
                # The setValue/fromUserText calls above trigger Nuke's updateValue()
                # which may interfere with nb_input_images. Re-saving here ensures
                # the user's current image list (including manual additions/deletions)
                # persists across save/reload.
                try:
                    widget_ref._save_ref_images_to_node()
                except Exception as e:
                    print("[NB Player Regen] WARNING: failed to re-save images: {}".format(e))

            nuke.executeInMainThread(_update_ui)
            _active_workers.pop(worker_id, None)

        def _direct_on_error(err):
            """Called from worker thread on error."""
            if status_tid:
                try:
                    from ai_workflow.status_bar import task_progress_manager as _tpm
                    _tpm.error_task(status_tid, str(err)[:80])
                except Exception:
                    pass

            def _update_ui():
                try:
                    widget_ref._toggle_ui(False)
                    widget_ref.status_label.setStyleSheet("color: #ef4444; font-size: 11px;")
                    widget_ref.status_label.setText("Error")
                except Exception:
                    pass
            nuke.executeInMainThread(_update_ui)

            _active_workers.pop(worker_id, None)
            nuke.executeInMainThread(nuke.message,
                                     args=("Regeneration Error:\n{}".format(err),))

        # Assign direct callbacks to worker (called from run() regardless of signal state)
        worker._on_finished_cb = _direct_on_finished
        worker._on_error_cb = _direct_on_error

        worker.start()


class NanoBananaKnobWidget(QtWidgets.QWidget):
    """Wrapper for NanoBanana_Generate node."""

    def __init__(self):
        super(NanoBananaKnobWidget, self).__init__()
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        # Capture the node at construction time (PyCustom_Knob evaluates
        # its expression in the context of the owning node).
        try:
            node = nuke.thisNode()
        except Exception:
            node = None
        print("[NanoBanana] KnobWidget __init__: node = {}".format(
            node.name() if node else "None"))
        self.panel = NanoBananaWidget(node=node, parent=self)
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


class NanoBananaPromptKnobWidget(QtWidgets.QWidget):
    """Wrapper for NanoBanana_Prompt node."""

    def __init__(self, node=None):
        super(NanoBananaPromptKnobWidget, self).__init__()
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        # Get the node if not provided
        if node is None:
            try:
                node = nuke.thisNode()
            except:
                node = None
        
        self.panel = NanoBananaPromptWidget(node, self)
        layout.addWidget(self.panel)

    def makeUI(self):
        return self

    def updateValue(self):
        pass
