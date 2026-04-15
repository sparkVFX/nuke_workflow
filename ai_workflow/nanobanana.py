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
import sys
import json
import tempfile
import time
import random
import base64
import datetime
import re


class _DropDownComboBox(QtWidgets.QComboBox):
    """QComboBox that always shows popup below the widget (not covering it)."""
    def showPopup(self):
        super(_DropDownComboBox, self).showPopup()
        popup = self.view().window()
        pos = self.mapToGlobal(QtCore.QPoint(0, self.height()))
        popup.move(pos)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MAX_INPUT_IMAGES = 14  # Absolute max (Gemini 3.1 Flash limit)
DEFAULT_TEMP_DIR_NAME = "nanobanana_temp"

# Per-model max input image limits
MODEL_MAX_INPUTS = {
    "gemini-3.1-flash-image-preview": 14,
    "gemini-3-pro-image-preview": 14,
    "gemini-2.5-flash-image": 3,
    "gemini-2.0-flash-exp-image-generation": 1,
    "imagen-3.0-generate-002": 1,
}
CONFIG_FILE_NAME = "nanobanana_config.json"
DEFAULT_PROJECT_CACHE_NAME = "nanobanana_projects"
UNSAVED_PROJECT_DIR = "_unsaved_"

# ---------------------------------------------------------------------------
# Module-level registry for active workers.
# Prevents garbage collection when the Widget is destroyed mid-generation.
# key = worker id, value = {"worker": NanoBananaWorker, "params": dict}
# ---------------------------------------------------------------------------
_active_workers = {}

# ---------------------------------------------------------------------------
# Style Sheet (dark theme with yellow accents)
# ---------------------------------------------------------------------------
NANOBANANA_STYLE = """
QWidget#nanoBananaRoot {
    background-color: #222222;
    color: #eeeeee;
    font-family: 'Segoe UI', 'Arial', sans-serif;
    font-size: 12px;
}
QLabel {
    color: #eeeeee;
    background: transparent;
}
QLineEdit, QTextEdit, QPlainTextEdit {
    background-color: #1a1a1a;
    border: 1px solid #333333;
    border-radius: 4px;
    padding: 6px;
    color: #ffffff;
    selection-background-color: #facc15;
    selection-color: #000000;
}
QLineEdit:focus, QTextEdit:focus {
    border: 1px solid #facc15;
}
QComboBox {
    background-color: #333333;
    border: 1px solid #444444;
    border-radius: 4px;
    padding: 5px;
    color: #ffffff;
}
QComboBox::drop-down {
    border: none;
    width: 20px;
}
QComboBox QAbstractItemView {
    background-color: #2a2a2a;
    selection-background-color: #facc15;
    selection-color: #000000;
}
QPushButton#generateBtn {
    background-color: #facc15;
    color: #121212;
    border: none;
    border-radius: 4px;
    padding: 10px 15px;
    font-weight: bold;
    font-size: 13px;
}
QPushButton#generateBtn:hover {
    background-color: #fde047;
}
QPushButton#generateBtn:pressed {
    background-color: #ca8a04;
}
QPushButton#regenerateBtn {
    background-color: #8b5cf6;
    color: #ffffff;
    border: none;
    border-radius: 4px;
    padding: 8px 12px;
    font-weight: bold;
    font-size: 12px;
}
QPushButton#regenerateBtn:hover {
    background-color: #a78bfa;
}
QPushButton#stopBtn {
    background-color: #ef4444;
    color: #ffffff;
    border: none;
    border-radius: 4px;
    padding: 10px 15px;
    font-weight: bold;
    font-size: 13px;
}
QPushButton#stopBtn:hover {
    background-color: #dc2626;
}
QPushButton#secondaryBtn {
    background-color: #404040;
    color: #e0e0e0;
    border: 1px solid #555555;
    padding: 4px 8px;
    font-size: 11px;
    font-weight: normal;
    border-radius: 3px;
}
QPushButton#secondaryBtn:hover {
    background-color: #505050;
    border-color: #777777;
}
QPushButton#testBtn {
    background-color: #3b82f6;
    color: #ffffff;
    border: none;
    border-radius: 4px;
    padding: 8px 12px;
    font-weight: bold;
    font-size: 12px;
}
QPushButton#testBtn:hover {
    background-color: #60a5fa;
}
QPushButton#settingsBtn {
    background-color: #555555;
    color: #ffffff;
    border: none;
    border-radius: 4px;
    padding: 6px 10px;
    font-size: 11px;
}
QPushButton#settingsBtn:hover {
    background-color: #666666;
}
QCheckBox {
    color: #eeeeee;
    spacing: 5px;
    background: transparent;
}
QCheckBox::indicator {
    width: 14px;
    height: 14px;
    border: 1px solid #555;
    border-radius: 3px;
    background-color: #333;
}
QCheckBox::indicator:checked {
    background-color: #facc15;
    border-color: #facc15;
}
QProgressBar {
    border: 1px solid #444;
    border-radius: 4px;
    text-align: center;
    background-color: #1a1a1a;
    color: #fff;
    font-size: 10px;
    height: 6px;
}
QProgressBar::chunk {
    background-color: #facc15;
    border-radius: 3px;
}
QGroupBox {
    border: 1px solid #444;
    border-radius: 4px;
    margin-top: 8px;
    padding-top: 8px;
    color: #aaa;
    font-size: 11px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 5px;
    color: #facc15;
}
"""


# ---------------------------------------------------------------------------
# Custom ComboBox (popup always below the widget)
# ---------------------------------------------------------------------------
class DropDownComboBox(QtWidgets.QComboBox):
    """QComboBox that always shows popup below the widget (not covering it)."""

    def showPopup(self):
        super(DropDownComboBox, self).showPopup()
        popup = self.view().window()
        # Move popup to just below the combo box
        pos = self.mapToGlobal(QtCore.QPoint(0, self.height()))
        popup.move(pos)


# ---------------------------------------------------------------------------
# Global Settings Manager
# ---------------------------------------------------------------------------
class NanoBananaSettings:
    """Manages NanoBanana settings (API key, temp directory, etc.)"""
    
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(NanoBananaSettings, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        
        self.config_file = os.path.join(os.path.expanduser("~"), ".nuke", CONFIG_FILE_NAME)
        self._data = self._load()
    
    def _load(self):
        """Load settings from config file."""
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print("NanoBanana: Error loading config: {}".format(e))
        return {}
    
    def _save(self):
        """Save settings to config file."""
        try:
            config_dir = os.path.dirname(self.config_file)
            if not os.path.exists(config_dir):
                os.makedirs(config_dir)
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=4)
        except Exception as e:
            print("NanoBanana: Error saving config: {}".format(e))
    
    @property
    def api_key(self):
        return self._data.get("api_key", "")
    
    @api_key.setter
    def api_key(self, value):
        self._data["api_key"] = value
        self._save()
    
    @property
    def temp_directory(self):
        custom_dir = self._data.get("temp_directory", "").strip()
        if custom_dir:
            # User specified a custom path – create it if it doesn't exist yet
            if not os.path.exists(custom_dir):
                try:
                    os.makedirs(custom_dir)
                except OSError:
                    pass  # fall through to default if creation fails
            if os.path.isdir(custom_dir):
                return custom_dir
        # Default to system temp
        default_dir = os.path.join(tempfile.gettempdir(), DEFAULT_TEMP_DIR_NAME)
        if not os.path.exists(default_dir):
            os.makedirs(default_dir)
        return default_dir
    
    @temp_directory.setter
    def temp_directory(self, value):
        self._data["temp_directory"] = value
        self._save()
    
    @property
    def prompt_history(self):
        return self._data.get("prompt_history", [])
    
    @prompt_history.setter
    def prompt_history(self, value):
        self._data["prompt_history"] = value
        self._save()

    @property
    def veo_prompt_history(self):
        return self._data.get("veo_prompt_history", [])

    @veo_prompt_history.setter
    def veo_prompt_history(self, value):
        self._data["veo_prompt_history"] = value
        self._save()

    @property
    def prores_codec(self):
        return self._data.get("prores_codec", "ProRes 422 HQ")

    @prores_codec.setter
    def prores_codec(self, value):
        self._data["prores_codec"] = value
        self._save()

    @property
    def project_cache_root(self):
        custom_dir = self._data.get("project_cache_root", "").strip()
        if custom_dir:
            if not os.path.exists(custom_dir):
                try:
                    os.makedirs(custom_dir)
                except OSError:
                    pass
            if os.path.isdir(custom_dir):
                return custom_dir
        # Default: next to the old temp directory
        old_temp = self.temp_directory
        # Use parent of temp dir as root, or system temp
        default_root = os.path.join(tempfile.gettempdir(), DEFAULT_PROJECT_CACHE_NAME)
        if not os.path.exists(default_root):
            os.makedirs(default_root)
        return default_root

    @project_cache_root.setter
    def project_cache_root(self, value):
        self._data["project_cache_root"] = value
        self._save()


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
        
        self.prores_combo = _DropDownComboBox()
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
# Project-Aware Directory System
# ---------------------------------------------------------------------------
def get_script_name():
    """Get the current Nuke script's base name (without path or extension).

    Returns UNSAVED_PROJECT_DIR (e.g. "_unsaved_") for untitled scripts.
    This is the key function that isolates caches per .nk file.
    """
    try:
        root_name = nuke.root().name()
        if root_name in ("", "root", "untitled", "Untitled"):
            return UNSAVED_PROJECT_DIR
        # "E:/projects/my_comp.nk" -> "my_comp"
        basename = os.path.basename(root_name)
        return os.path.splitext(basename)[0] or UNSAVED_PROJECT_DIR
    except Exception:
        return UNSAVED_PROJECT_DIR


def get_project_directory():
    """Get the cache directory for the CURRENT Nuke script/project.

    Returns a per-script directory like:
      {project_cache_root}/{script_name}/
    where script_name is the .nk file name (or "_unsaved_" if not saved yet).

    Each project gets its own input/, output/, logs/ subdirectories.
    """
    settings = NanoBananaSettings()
    root = settings.project_cache_root
    script_name = get_script_name()
    proj_dir = os.path.join(root, script_name)
    if not os.path.exists(proj_dir):
        os.makedirs(proj_dir)
    return proj_dir


def get_temp_directory():
    """Get the temporary (project-aware) directory for storing images.

    DEPRECATED: Prefer get_project_directory() for new code.
    Kept for backward compatibility — now delegates to get_project_directory().
    """
    return get_project_directory()


def get_input_directory():
    """Get the input subdirectory inside project directory."""
    input_dir = os.path.join(get_project_directory(), "input")
    if not os.path.exists(input_dir):
        os.makedirs(input_dir)
    return input_dir


def get_output_directory():
    """Get the output subdirectory inside project directory."""
    output_dir = os.path.join(get_project_directory(), "output")
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    return output_dir


def get_logs_directory():
    """Get the logs subdirectory inside project directory."""
    logs_dir = os.path.join(get_project_directory(), "logs")
    if not os.path.exists(logs_dir):
        os.makedirs(logs_dir)
    return logs_dir


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


def _is_generator_node(node):
    """Return True if *node* is a NanoBanana Generate node (not a Player/Prompt)."""
    name = node.name()
    # Match "NanoBanana", "NanoBanana1", "NanoBanana2", ... AND legacy "NanoBanana_Generate*"
    # But NOT "Nano_Viewer*" (player nodes)
    if name.startswith("NanoBanana_Generate"):
        return True
    if name == "NanoBanana" or re.match(r"^NanoBanana\d+$", name):
        return True
    return False

def _next_node_name(prefix):
    """Return the next available name like 'Prefix1', 'Prefix2', etc.
    Checks all existing nodes and picks the lowest unused number."""
    import re
    used = set()
    for node in nuke.allNodes():
        n = node.name()
        if n == prefix:
            used.add(1)
        else:
            m = re.match(r"^{}(\d+)$".format(re.escape(prefix)), n)
            if m:
                used.add(int(m.group(1)))
    # Find the lowest unused positive integer
    i = 1
    while i in used:
        i += 1
    return "{}{}".format(prefix, i)

def get_nanobanana_node():
    """Try to find the NanoBanana_Generate node."""
    # First check if there's a selected node
    selected = nuke.selectedNodes()
    for node in selected:
        if _is_generator_node(node):
            return node
    
    # Search all nodes for NanoBanana_Generate
    for node in nuke.allNodes():
        if _is_generator_node(node):
            return node
    return None


def render_input_to_file_silent(input_node, output_path, frame=None):
    """
    Render a node's output to a file silently (without creating visible Write node).
    """
    if frame is None:
        frame = nuke.frame()
    
    if input_node is None:
        return False
    
    try:
        # Create a temporary Write node
        write = nuke.nodes.Write()
        write.setInput(0, input_node)
        write["file"].setValue(output_path.replace("\\", "/"))
        write["file_type"].setValue("png")
        write["channels"].setValue("rgb")

        # Hide from DAG
        write["xpos"].setValue(-10000)
        write["ypos"].setValue(-10000)
        
        # Execute the write silently
        nuke.execute(write, frame, frame)
        
        # Delete the temporary Write node immediately
        nuke.delete(write)
        
        return os.path.exists(output_path)
    except Exception as e:
        print("NanoBanana: Error rendering: {}".format(str(e)))
        try:
            if write:
                nuke.delete(write)
        except:
            pass
        return False


def collect_input_images(node, temp_dir):
    """Collect connected input images from the NanoBanana node.

    DAG layout:       img1(LEFT) ... imgN(RIGHT)
    Input index map:  imgK -> input (num_inputs - K)
                      img1 -> highest idx (leftmost in DAG)
                      imgN  -> input 0 (rightmost in DAG)

    We iterate by img number (1..N) for consistent API ordering.
    """
    inputs_info = []
    render_frame = nuke.frame()
    gen_name = node.name()
    
    num_inputs = node.inputs()
    
    print("[NanoBanana] collect: '{}' has {} inputs".format(gen_name, num_inputs))
    
    for k in range(1, num_inputs + 1):
        # imgK -> input (num_inputs - K)
        input_idx = num_inputs - k
        input_node = node.input(input_idx)
        input_name = "img{}".format(k)
        
        info = {
            "index": k - 1,  # 0-based for API
            "name": input_name,
            "path": None,
            "connected": input_node is not None,
            "node_name": input_node.name() if input_node else None
        }
        
        print("[NanoBanana]   img{} = input({}) <- {}"
              .format(k, input_idx, input_node.name() if input_node else "None"))
        
        if input_node is not None:
            filename = "{}_input_{}_frame{}.png".format(gen_name, input_name, k)
            output_path = os.path.join(temp_dir, filename)
            
            if render_input_to_file_silent(input_node, output_path, render_frame):
                info["path"] = output_path
        
        inputs_info.append(info)
    
    return inputs_info


def image_to_base64(image_path):
    """Convert an image file to base64 string."""
    if not image_path or not os.path.exists(image_path):
        return None
    
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def get_mime_type(image_path):
    """Get MIME type based on file extension."""
    ext = os.path.splitext(image_path)[1].lower()
    mime_types = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp"
    }
    return mime_types.get(ext, "image/png")


# ---------------------------------------------------------------------------
# Gemini API Helper Functions
# ---------------------------------------------------------------------------
def call_gemini_api(api_key, model, contents, generation_config):
    """Call Gemini API to generate image."""
    try:
        if sys.version_info[0] >= 3:
            import urllib.request as urllib_request
            import urllib.error as urllib_error
        else:
            import urllib2 as urllib_request
            urllib_error = urllib_request
        
        url = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent?key={}".format(
            model, api_key
        )
        
        request_body = {
            "contents": contents,
            "generationConfig": generation_config
        }
        
        json_data = json.dumps(request_body).encode("utf-8")
        
        req = urllib_request.Request(url, data=json_data)
        req.add_header("Content-Type", "application/json")
        
        response = urllib_request.urlopen(req, timeout=120)
        response_data = response.read().decode("utf-8")
        result = json.loads(response_data)
        
        return True, result
        
    except Exception as e:
        error_msg = str(e)
        if hasattr(e, 'read'):
            try:
                error_body = e.read().decode("utf-8")
                error_json = json.loads(error_body)
                if "error" in error_json:
                    error_msg = error_json["error"].get("message", error_msg)
            except:
                pass
        return False, error_msg


def extract_image_from_response(response, output_dir, gen_name="nanobanana"):
    """Extract generated image from Gemini API response.
    
    Args:
        response: Gemini API response dict
        output_dir: Directory to save the image
        gen_name: Generator node name, used as filename prefix
    """
    try:
        candidates = response.get("candidates", [])
        if not candidates:
            return None, "No candidates in response"
        
        content = candidates[0].get("content", {})
        parts = content.get("parts", [])
        
        for part in parts:
            if "inlineData" in part:
                inline_data = part["inlineData"]
                mime_type = inline_data.get("mimeType", "image/png")
                data = inline_data.get("data", "")
                
                if data:
                    ext_map = {
                        "image/png": ".png",
                        "image/jpeg": ".jpg",
                        "image/webp": ".webp",
                        "image/gif": ".gif"
                    }
                    ext = ext_map.get(mime_type, ".png")
                    
                    # Find next available frame number for this generator
                    frame_num = 1
                    while True:
                        filename = "{}_frame{}{}".format(gen_name, frame_num, ext)
                        output_path = os.path.join(output_dir, filename)
                        if not os.path.exists(output_path):
                            break
                        frame_num += 1
                    
                    image_data = base64.b64decode(data)
                    with open(output_path, "wb") as f:
                        f.write(image_data)
                    
                    return output_path, None
            
            if "text" in part:
                text = part.get("text", "")
                if text:
                    print("NanoBanana API text response: {}".format(text[:500]))
        
        return None, "No image data in response"
        
    except Exception as e:
        return None, "Error extracting image: {}".format(str(e))



# ---------------------------------------------------------------------------
# Send to Studio helper
# ---------------------------------------------------------------------------
_SEND_TO_STUDIO_SCRIPT = """
import nuke, socket, json, struct

node = nuke.thisNode()
file_path = ""
if "nb_file" in node.knobs():
    file_path = node["nb_file"].value()
elif "file" in node.knobs():
    file_path = node["file"].value()
if not file_path:
    nuke.message("No file path set on this node.")
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


def _get_internal_read_nb(group_node):
    """Get the internal Read node from a NanoBanana Player Group."""
    if group_node is None or group_node.Class() != "Group":
        return None
    try:
        group_node.begin()
        r = nuke.toNode("InternalRead")
        group_node.end()
        return r
    except Exception:
        return None


def _rebuild_group_for_thumbnail(node, image_path=None):
    """'Replacement Jutsu' — rebuild the Group node to force thumbnail refresh.

    Nuke's Group-node postage-stamp cache is bound to the C++ node instance
    and cannot be flushed via any public Python / Tcl API (30+ techniques
    tested in V1-V4 diagnostics).  The only reliable way to make the DAG
    show a new thumbnail is to **replace the node with an identical copy**.

    Strategy:
      1. Save all upstream / downstream connections
      2. nuke.nodeCopy  → clipboard  (serialises the Group + its internals)
      3. Delete old node
      4. nuke.nodePaste → new node   (fresh C++ instance → fresh thumbnail)
      5. Restore all connections & ensure the new node keeps the same name

    If *image_path* is given the InternalRead is pointed to that file
    **before** the copy so the pasted clone already has the right image.

    Returns the **new** Group node (the old reference is now invalid).
    Returns *None* on failure (caller should fall back to legacy approach).
    """
    if not node or node.Class() != "Group":
        return None
    if "is_nb_player" not in node.knobs():
        return None  # safety — only operate on NB Player nodes

    _tag = "[NB Rebuild]"

    try:
        node_name = node.name()
        print("{} START for '{}'".format(_tag, node_name))

        # Wrap in Undo group so the whole operation can be reverted if needed
        nuke.Undo.begin("NB Rebuild Thumbnail")

        # --- 0. Set image_path on InternalRead BEFORE copy ---
        if image_path and os.path.isfile(image_path):
            ir = _get_internal_read_nb(node)
            if ir:
                node.begin()
                ir["file"].fromUserText(image_path)
                node.end()
                # Also sync Group-level nb_file knob
                if "nb_file" in node.knobs():
                    node["nb_file"].setValue(image_path.replace("\\", "/"))
                if "nb_output_path" in node.knobs():
                    node["nb_output_path"].setValue(image_path.replace("\\", "/"))

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
        # First deselect all
        for n in nuke.allNodes():
            n.setSelected(False)
        node.setSelected(True)

        nuke.nodeCopy("%clipboard%")
        print("{}   nodeCopy OK".format(_tag))

        # --- 3. Delete old node ---
        nuke.delete(node)
        print("{}   deleted old '{}'".format(_tag, node_name))

        # --- 4. Paste from clipboard ---
        # Deselect all first
        for n in nuke.allNodes():
            n.setSelected(False)

        nuke.nodePaste("%clipboard%")
        print("{}   nodePaste OK".format(_tag))

        # The pasted node(s) are selected — find our new Group
        new_node = None
        for n in nuke.selectedNodes():
            if n.Class() == "Group" and "is_nb_player" in n.knobs():
                new_node = n
                break

        if not new_node:
            print("{} ERROR: Could not find pasted node!".format(_tag))
            return None

        # --- 5. Restore name ---
        # nodePaste may have appended a number if name conflicts
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

        # Deselect
        new_node.setSelected(False)

        print("{} DONE — new node '{}' created".format(_tag, new_node.name()))
        nuke.Undo.end()
        return new_node

    except Exception as e:
        import traceback
        print("{} FATAL ERROR: {}\n{}".format(_tag, e, traceback.format_exc()))
        # Try to cancel/end the undo group so user can Ctrl+Z to recover
        try:
            nuke.Undo.cancel()
        except Exception:
            try:
                nuke.Undo.end()
            except Exception:
                pass
        return None


def _update_node_thumbnail(node, image_path=None):
    """Enable the native Nuke postage-stamp preview on the Group node.

    This uses the same rendering mechanism as Read nodes — Nuke renders
    the node's output at the current frame and displays it as a thumbnail
    directly on the node in the DAG.  No external thumbnail file needed.

    If *image_path* is provided (and valid), we also make sure the internal
    Read node has that file loaded so there is something to render.
    After enabling postage_stamp, we force a redraw so the thumbnail
    updates immediately (like Read nodes do).
    """
    if not node:
        print("[NB] Thumbnail ERROR: node is None")
        return
    _tag = "[NB] Thumbnail '{}'".format(node.name())

    print("{}: === _update_node_thumbnail START ===".format(_tag))
    print("{}:   image_path = {}".format(_tag, repr(image_path)))

    # If image_path supplied, ensure InternalRead is loaded with it
    if image_path and os.path.isfile(image_path):
        try:
            internal_read = _get_internal_read_nb(node)
            if internal_read:
                # Log BEFORE loading
                old_file = ""
                try:
                    old_file = internal_read["file"].value()
                except:
                    pass
                print("{}:   InternalRead current file: {}".format(_tag, repr(old_file)))
                
                node.begin()
                internal_read["file"].fromUserText(image_path)
                new_file = internal_read["file"].value()
                node.end()
                print("{}:   InternalRead loaded NEW file: {}".format(_tag, repr(new_file)))
            else:
                print("{}:   WARNING: no InternalRead found!".format(_tag))
        except Exception as e:
            import traceback
            print("{}:   InternalRead load ERROR: {}\n{}".format(_tag, e, traceback.format_exc()))
    elif image_path:
        print("{}:   WARNING: image_path provided but file DOES NOT EXIST: {}".format(_tag, repr(image_path)))

    # Enable postage_stamp knob — same approach as Read nodes
    if "postage_stamp" in node.knobs():
        try:
            ps_val = node["postage_stamp"].value()
            print("{}:   postage_stamp current value: {}".format(_tag, ps_val))
            node["postage_stamp"].setValue(True)
            print("{}:   postage_stamp set to True".format(_tag))
        except Exception as e:
            print("{}:   postage_stamp failed: {}".format(_tag, e))
    else:
        print("{}:   no postage_stamp knob (Class={})".format(_tag, node.Class()))
        return

    # Force Nuke to actually compute the Group output pixels.
    try:
        r, g, b = 0.0, 0.0, 0.0
        r = node.sample("red", 0, 0)
        g = node.sample("green", 0, 0)
        b = node.sample("blue", 0, 0)
        print("{}:   sample() OK: pixel(0,0) = ({:.3f}, {:.3f}, {:.3f})".format(_tag, r, g, b))
    except Exception as e:
        print("{}:   sample() FAILED: {}".format(_tag, e))

    # Check nb_file knob matches
    if "nb_file" in node.knobs():
        nb_f = node["nb_file"].value()
        print("{}:   nb_file knob = {}".format(_tag, repr(nb_f)))

    # --- Try multiple DAG view refresh methods ---
    methods_tried = []

    # Method 1: nuke.modified()
    try:
        nuke.modified()
        methods_tried.append("nuke.modified()")
    except Exception as e:
        print("{}:   nuke.modified() failed: {}".format(_tag, e))

    # Method 2: Toggle postage_stamp off then on (forces Nuke to re-render)
    try:
        node["postage_stamp"].setValue(False)
        node["postage_stamp"].setValue(True)
        methods_tried.append("toggle_postage_stamp")
        print("{}:   toggled postage_stamp off/on".format(_tag))
    except Exception as e:
        print("{}:   toggle postage_stamp failed: {}".format(_tag, e))

    # Method 3: Find Nuke's actual DAG view (QGraphicsView) and force refresh
    try:
        def _deferred_refresh():
            app = QtWidgets.QApplication.instance()
            dag_views = []
            scene_items = []
            if app:
                for tw in app.topLevelWidgets():
                    # Nuke DAG = QGraphicsView containing node items
                    for gv in tw.findChildren(QtWidgets.QGraphicsView):
                        cn = gv.metaObject().className()
                        dag_views.append("{}:{}".format(cn, id(gv)))
                        # Method A: Invalidate entire scene
                        scene = gv.scene()
                        if scene:
                            scene.update()
                            scene.invalidate(scene.sceneRect(), QtWidgets.QGraphicsScene.AllLayers)
                            scene_items.append("scene_invalidated")
                        # Method B: Repaint viewport
                        vp = gv.viewport()
                        if vp:
                            vp.repaint()
                            scene_items.append("viewport_repainted")
                        # Method C: Trigger full redraw
                        gv.updateGeometry()
                        scene_items.append("geometry_updated")

                # Also try selecting/deselecting the specific node in DAG
                try:
                    node.setSelected(False)
                    node.setSelected(True)
                    node.setSelected(False)
                    scene_items.append("node_select_toggle")
                except Exception as e2:
                    scene_items.append("select_err:{}".format(e2))

            print("[NB] Deferred: found {} QGraphicsView(s): [{}]".format(
                len(dag_views), ", ".join(dag_views) if dag_views else "NONE"))
            print("[NB] Deferred: actions: [{}]".format(", ".join(scene_items) if scene_items else "NONE"))

        QtCore.QTimer.singleShot(300, _deferred_refresh)
        methods_tried.append("QGraphicsView_scene_invalidate")
    except Exception as e:
        print("{}:   QTimer deferred refresh failed: {}".format(_tag, e))

    # Method 4: Force node re-evaluation by touching internal Read knobs
    try:
        node.begin()
        read_node = _get_internal_read_nb(node)
        if read_node:
            # Touch proxy_format or any knob value to invalidate cache
            for knob_name in ["proxy_format", "colorspace"]:
                if knob_name in read_node.knobs():
                    try:
                        val = str(read_node[knob_name].value())
                        read_node[knob_name].setValue(val)
                        methods_tried.append("touch_{}_knob".format(knob_name))
                        print("{}:   touched {} knob".format(_tag, knob_name))
                        break
                    except Exception:
                        pass
        node.end()
    except Exception as e:
        print("{}:   touch_knob failed: {}".format(_tag, e))

    # Method 5: Use Nuke's internal redraw mechanism via nuke.executeInMainThread
    try:
        def _nuke_redraw():
            # Try multiple Nuke-specific cache invalidation techniques
            results = []
            # 5a: Double-toggle postage_stamp
            try:
                node["postage_stamp"].setValue(False)
                node["postage_stamp"].setValue(True)
                results.append("toggle_ps")
            except Exception as e2:
                results.append("toggle_err")

            # 5b: Use nuke.tcl to call internal Nuke command if available
            try:
                import nuke
                # Force Nuke to re-evaluate this node's output
                node.begin()
                read_node = _get_internal_read_nb(node)
                if read_node:
                    # Re-read the file by setting to empty then back
                    cur_file = str(read_node["file"].value())
                    read_node["file"].fromUserText("")
                    read_node["file"].fromUserText(cur_file)
                    results.append("reread_file")
                node.end()
            except Exception as e3:
                results.append("reread_err:{}".format(str(e3)[:30]))

            print("[NB] Method5 actions: [{}]".format(", ".join(results)))
        nuke.executeInMainThread(_nuke_redraw)
        methods_tried.append("executeInMainThread_multi")
    except Exception as e:
        print("{}:   executeInMainThread failed: {}".format(_tag, e))

    # Method 6: Use QTimer to do a delayed full re-read of InternalRead
    try:
        _node_ref = node
        _img_path = image_path
        def _delayed_reread():
            try:
                _node_ref.begin()
                rd = _get_internal_read_nb(_node_ref)
                if rd:
                    cur = rd["file"].value()
                    rd["file"].fromUserText("")
                    if _img_path and os.path.isfile(_img_path):
                        rd["file"].fromUserText(_img_path)
                    else:
                        rd["file"].fromUserText(cur)
                    _node_ref["postage_stamp"].setValue(False)
                    _node_ref["postage_stamp"].setValue(True)
                    print("[NB] Method6: delayed reread + ps toggle done")
                _node_ref.end()
            except Exception as e6:
                print("[NB] Method6 error: {}".format(e6))
        QtCore.QTimer.singleShot(500, _delayed_reread)
        methods_tried.append("QTimer_500ms_delayed_reread")
    except Exception as e:
        print("{}:   Method6 setup failed: {}".format(_tag, e))

    print("{}: === _update_node_thumbnail END (methods tried: {}) ===\n".format(
        _tag, ", ".join(methods_tried)))


def diagnose_visual_refresh_v3(node_name=None, image_path=None):
    """V3 Diagnostic: New approaches based on V2 findings.
    
    Key finding from V2: Visual refresh TECHNIQUES WORK (old→black transition).
    Problem: Re-rendered thumbnail shows BLACK = wrong data at render time.
    Root cause hypothesis: Some techniques clear file mid-operation, 
    OR Nuke's postage_stamp renderer picks up stale/cleared state.
    
    V3 Strategy:
      A) Control test: Can a PLAIN Read node update its thumbnail live?
      B) Safe refresh: NEVER clear file, only set new value + trigger
      C) Execution-based: Force proper render via correct API
      D) Hash/dirty flag: Find Nuke's internal invalidation mechanism
      E) Qt event injection: Simulate real user interaction
    """
    import nuke
    import time as _time

    print("=" * 70)
    print("[V3] diagnose_visual_refresh_v3 START")
    print("=" * 70)

    # --- Find target ---
    if not node_name:
        found = None
        for nd in nuke.allNodes("Group"):
            if "is_nb_player" in nd.knobs() and nd["is_nb_player"].value():
                found = nd; break
        if not found:
            for nd in nuke.allNodes("Read"): found = nd; break
        if not found:
            print("[V3] ERROR: No node found!"); return
        node_name = found.name()

    node = nuke.toNode(node_name)
    if not node:
        print("[V3] ERROR: '{}' not found!".format(node_name)); return

    is_group = (node.Class() == "Group")
    print("[V3] Target: '{}' (Class={})".format(node_name, node.Class()))

    # --- Get image_path ---
    if not image_path:
        if "nb_file" in node.knobs(): image_path = node["nb_file"].value()
        elif "file" in node.knobs(): image_path = node["file"].value()
    if not os.path.isfile(image_path):
        print("[V3] WARNING: File missing: {}".format(repr(image_path)))

    internal_read = _get_internal_read_nb(node) if is_group else None
    read_target = internal_read or node

    def _px(n):
        try: return (n.sample("red",0,0), n.sample("green",0,0), n.sample("blue",0,0))
        except: return None

    def _vtry(name, func):
        try:
            func()
            print("[V3]   {:40s} OK".format(name))
            return True
        except Exception as e:
            print("[V3]   {:40s} ERR: {}".format(name, str(e)[:60]))
            return False

    app = QtWidgets.QApplication.instance()

    # =====================================================================
    # PART A: CONTROL TEST — Does a plain Read node update thumbnail?
    # =====================================================================
    print("\n" + "-" * 70)
    print("[V3] PART A: Control Test (plain Read node)")
    print("-" * 70)

    test_read = None
    try:
        # Create temp Read node next to our node
        ox = node["xpos"].value() + 200 if "xpos" in node.knobs() else 0
        oy = node["ypos"].value() if "ypos" in node.knobs() else 0
        test_read = nuke.nodes.Read(file=image_path, xpos=ox, ypos=oy)
        test_read["postage_stamp"].setValue(True)

        px_before = _px(test_read)
        if px_before:
            print("[V3]   Created Read '{}', px={:.4f},{:.4f},{:.4f}".format(
                test_read.name(), *px_before))

            # Now change its file to a DIFFERENT image
            print("[V3]   Changing Read file...")
            test_read["file"].fromUserText(image_path)  # Same file first (ensure loaded)
            px_after = _px(test_read)
            if px_after:
                print("[V3]   After set, px={:.4f},{:.4f},{:.4f}".format(*px_after))

            # Check if DAG shows updated thumbnail for this Read
            print("[V3] >>> Look at the TEMP Read node '{}' in DAG:".format(test_read.name()))
            print("[V3]     Does it show the correct image thumbnail?")
            print("[V3]     (This tests if Nuke updates Read node thumbnails at all)")

            _time.sleep(1)
    except Exception as e:
        print("[V3]   Control test error: {}".format(e))

    # =====================================================================
    # PART B: SAFE REFRESH — never clear file, just direct set + triggers
    # =====================================================================
    print("\n" + "-" * 70)
    print("[V3] PART B: Safe Refresh (NEVER clear file)")
    print("-" * 70)

    # First ensure clean state
    print("[V3] Setting file (safe, no clear)...")
    if is_group: node.begin()
    read_target["file"].fromUserText(image_path)
    if is_group: node.end()
    base_px = _px(node)
    if base_px:
        print("[V3]   Base px={:.4f},{:.4f},{:.4f}".format(*base_px))

    # B1: Simple setValue (not fromUserText) on postage_stamp
    _vtry("B1.ps_setValue_True",
          lambda: node["postage_stamp"].setValue(True))

    # B2: Sample to force compute, then toggle ps
    def _b2():
        _px(node)  # Force compute
        node["postage_stamp"].setValue(False)
        _time.sleep(0.05)
        node["postage_stamp"].setValue(True)
    _vtry("B2.sample+ps_toggle_50ms", _b2)

    # B3: Process ALL pending Qt events multiple times
    def _b3():
        for _ in range(5):
            QtWidgets.QApplication.processEvents(
                QtCore.QEventLoop.AllEvents, 50)
            _time.sleep(0.02)
    _vtry("B3.processEvents_x5", _b3)

    # B4: Touch "tile_color" knob (changes node appearance → forces redraw)
    def _b4():
        if "tile_color" in node.knobs():
            old = node["tile_color"].value()
            node["tile_color"].setValue(old)
    _vtry("B4.touch_tile_color", _b4)

    # B5: Touch "note_font" or "note_font_size"
    def _b5():
        for kn in ["note_font", "postage_stamp", "selected", "gl_renderer"]:
            if kn in node.knobs():
                try:
                    v = node[kn].value()
                    node[kn].setValue(v)
                    break
                except: pass
    _vtry("B5.touch_render_knob", _b5)

    # B6: Use begin/complete wrapped operation (atomic from Nuke's POV)
    def _b6():
        node.begin()
        rd = internal_read or node
        rd["file"].fromUserText(image_path)  # Set directly, no clear
        node["postage_stamp"].setValue(True)
        node.end()
        nuke.modified()
    _vtry("B6.atomic_begin_end_ps", _b6)

    # =====================================================================
    # PART C: EXECUTION-BASED APPROACHES
    # =====================================================================
    print("\n" + "-" * 70)
    print("[V3] PART C: Execution / Render-based")
    print("-" * 70)

    # C1: Use nuke.render() or proper execute for non-Write nodes
    def _c1():
        # Try rendering just the required range
        import nuke as _nk
        f = int(_nk.frame())
        # Method: use execute on the internal Read (which is executable)
        if internal_read:
            if is_group: node.begin()
            try:
                _nk.execute(internal_read, f, f)
            except Exception as ex:
                print("[V3]     exec IR err: {}".format(ex))
            if is_group: node.end()
    _vtry("C1.exec_InternalRead_only", _c1)

    # C2: Use render with a temporary Write inside the Group
    def _c2():
        if not is_group: return
        node.begin()
        try:
            # Create temp Write connected after InternalRead, render 1 frame, delete
            tmp_write = nuke.nodes.Write(file="C:/temp/nb_tmp_####.exr",
                                         name="__tmp_nb_write__")
            tmp_write.setInput(0, internal_read)
            try:
                nuke.execute(tmp_write, 1, 1)
            except Exception as ex:
                print("[V3]     exec Write err: {}".format(ex))
            nuke.delete(tmp_write)
        except Exception as e:
            print("[V3]     setup err: {}".format(e))
        node.end()
    _vtry("C2.temp_Write_inside_Group", _c2)

    # C3: nuke.executeInMainThreadWithCallback (if available)
    def _c3():
        def _cb(result):
            print("[V3]     mainThreadCb done: {}".format(result))
        
        def _work():
            node["postage_stamp"].setValue(False)
            node["postage_stamp"].setValue(True)
            return "ps_toggled"

        try:
            nuke.executeInMainThreadWithCallback(_cb, _work)
        except AttributeError:
            # Fallback to regular executeInMainThread
            nuke.executeInMainThread(_work)
    _vtry("C3.mainThread_with_callback", _c3)

    # =====================================================================
    # PART D: HASH / DIRTY FLAG APPROACHES
    # =====================================================================
    print("\n" + "-" * 70)
    print("[V3] Part D: Dirty Flag / Hash approaches")
    print("-" * 70)

    # D1: Check available internal methods
    print("[V3]   Node methods containing 'hash','dirty','valid','refresh','update':")
    interesting_methods = []
    for attr_name in dir(node):
        low = attr_name.lower()
        if any(k in low for k in ["hash", "dirty", "valid", "refresh", "update",
                                   "rebuild", "recalc", "invalidate", "render"]):
            interesting_methods.append(attr_name)
    if interesting_methods:
        print("[V3]     Found: [{}]".format(", ".join(interesting_methods)))
    else:
        print("[V3]     None found")

    # D2: Try calling any promising ones
    for m in ["forceValidate", "validate", "markDirty", "setDirty"]:
        if hasattr(node, m):
            def _call_m(method=m):
                getattr(node, method)()
            _vtry("D2.node.{}()".format(m), _call_m)

    # D3: Try Nuke's internal "update" command variants  
    def _d3():
        import nuke as _n
        nodeFullName = node.name()
        for cmd in ["idletasks", "update idletasks"]:
            try:
                _n.tcl(cmd)
                break
            except: pass
    _vtry("D3.tcl_idletasks_variants", _d3)

    # =====================================================================
    # PART E: QT EVENT INJECTION
    # =====================================================================
    print("\n" + "-" * 70)
    print("[V3] PART E: Qt Event Injection")
    print("-" * 70)

    # E1: Send mouse press+release on the node's position in DAG
    def _e1():
        for tw in app.topLevelWidgets():
            for gv in tw.findChildren(QtWidgets.QGraphicsView):
                s = gv.scene()
                if not s: continue
                # Map node position to scene coords
                nx = node["xpos"].value() if "xpos" in node.knobs() else 0
                ny = node["ypos"].value() if "ypos" in node.knobs() else 0
                scene_pos = QtCore.QPointF(nx * 10, ny * 10)  # Approximate mapping
                global_pos = gv.mapToGlobal(gv.mapFromScene(scene_pos))

                # Send mouse events
                press = QtGui.QMouseEvent(
                    QtCore.QEvent.MouseButtonPress, gv.mapFromScene(scene_pos),
                    global_pos, QtCore.Qt.LeftButton,
                    QtCore.Qt.LeftButton, QtCore.Qt.NoModifier)
                release = QtGui.QMouseEvent(
                    QtCore.QEvent.MouseButtonRelease, gv.mapFromScene(scene_pos),
                    global_pos, QtCore.Qt.LeftButton,
                    QtCore.Qt.LeftButton, QtCore.Qt.NoModifier)

                QtWidgets.QApplication.sendEvent(gv.viewport(), press)
                _time.sleep(0.02)
                QtWidgets.QApplication.sendEvent(gv.viewport(), release)
                print("[V3]     Injected click at ({}, {})".format(nx, ny))
                break
    _vtry("E1.mouse_click_on_node", _e1)

    # E2: Double-click on node (opens properties, forces refresh)
    def _e2():
        for tw in app.topLevelWidgets():
            for gv in tw.findChildren(QtWidgets.QGraphicsView):
                s = gv.scene()
                if not s: continue
                nx = node["xpos"].value() if "xpos" in node.knobs() else 0
                ny = node["ypos"].value() if "ypos" in node.knobs() else 0
                scene_pos = QtCore.QPointF(nx * 10, ny * 10)
                global_pos = gv.mapToGlobal(gv.mapFromScene(scene_pos))

                dclick = QtGui.QMouseEvent(
                    QtCore.QEvent.MouseButtonDblClick, gv.mapFromScene(scene_pos),
                    global_pos, QtCore.Qt.LeftButton,
                    QtCore.Qt.LeftButton, QtCore.Qt.NoModifier)
                QtWidgets.QApplication.sendEvent(gv.viewport(), dclick)
                print("[V3]     Injected double-click")
                # Close property panel quickly
                _time.sleep(0.1)
                break
    _vtry("E2.dblclick_node", _e2)

    # E3: Focus change trick
    def _e3():
        for tw in app.topLevelWidgets():
            for gv in tw.findChildren(QtWidgets.QGraphicsView):
                gv.setFocus()
                gv.viewport().setFocus()
                gv.clearFocus()
                node.setSelected(True)
                break
    _vtry("E3.focus_cycle", _e3)

    # =====================================================================
    # CLEANUP & SUMMARY
    # =====================================================================
    print("\n" + "=" * 70)
    print("[V3] CLEANUP: Removing temp Read node...")
    if test_read:
        try:
            nuke.delete(test_read)
            print("[V3]   Deleted '{}'".format(test_read.name()))
        except Exception as e:
            print("[V3]   Delete err: {}".format(e))

    final_px = _px(node)
    if final_px:
        print("[V3] Final px={:.4f},{:.4f},{:.4f}".format(*final_px))

    print("\n[V3] === END === Watch DAG: Did ANYTHING update the thumbnail?")
    print("=" * 70)


def diagnose_visual_refresh_v4(node_name=None, image_path=None):
    """V4: Use Read node's built-in 'reload' knob + nuke.updateUI() + nuke.show().
    
    Key discoveries from web search:
      1. Read nodes have a 'reload' knob button: node['reload'].execute()
         This forces Nuke to re-read the file from disk AND refresh the thumbnail.
      2. nuke.updateUI() forces a full UI refresh cycle.
      3. nuke.show(node) forces node properties refresh.
    """
    import nuke
    import time as _time

    print("=" * 70)
    print("[V4] diagnose_visual_refresh_v4 START")
    print("=" * 70)

    # --- Find target ---
    if not node_name:
        found = None
        for nd in nuke.allNodes("Group"):
            if "is_nb_player" in nd.knobs() and nd["is_nb_player"].value():
                found = nd; break
        if not found:
            print("[V4] ERROR: No node found!"); return
        node_name = found.name()

    node = nuke.toNode(node_name)
    if not node:
        print("[V4] ERROR: '{}' not found!".format(node_name)); return

    is_group = (node.Class() == "Group")
    internal_read = _get_internal_read_nb(node) if is_group else None
    read_target = internal_read or node

    print("[V4] Target: '{}' Class={}, InternalRead={}".format(
        node_name, node.Class(), internal_read.name() if internal_read else "None"))

    # --- Get image_path ---
    if not image_path:
        if "nb_file" in node.knobs(): image_path = node["nb_file"].value()
        elif "file" in node.knobs(): image_path = node["file"].value()
    print("[V4] image_path = {}".format(repr(image_path)))

    def _px(n):
        try: return (n.sample("red",0,0), n.sample("green",0,0), n.sample("blue",0,0))
        except: return None

    px_before = _px(node)
    if px_before:
        print("[V4] BEFORE pixel = {:.4f},{:.4f},{:.4f}".format(*px_before))

    # =====================================================================
    # Step 1: Set file path on InternalRead
    # =====================================================================
    print("\n[V4] --- Step 1: Set file on InternalRead ---")
    if is_group: node.begin()
    read_target["file"].fromUserText(image_path)
    if is_group: node.end()
    print("[V4]   file set OK")

    # =====================================================================
    # Step 2: List ALL knobs on InternalRead (looking for 'reload')
    # =====================================================================
    print("\n[V4] --- Step 2: InternalRead knobs ---")
    ir_knobs = sorted(read_target.knobs().keys())
    print("[V4]   Total knobs: {}".format(len(ir_knobs)))
    # Print button-type and interesting knobs
    interesting = ["reload", "localize", "update", "refresh", "read", 
                   "postage_stamp", "file", "proxy", "cacheLocal",
                   "on_error", "format"]
    for kname in ir_knobs:
        if any(i in kname.lower() for i in interesting):
            k = read_target[kname]
            ktype = type(k).__name__
            print("[V4]   {} ({})".format(kname, ktype))

    # =====================================================================
    # Step 3: Try reload knob on InternalRead
    # =====================================================================
    print("\n[V4] --- Step 3: InternalRead['reload'].execute() ---")
    if "reload" in read_target.knobs():
        try:
            if is_group: node.begin()
            read_target["reload"].execute()
            if is_group: node.end()
            print("[V4]   reload.execute() OK!")
        except Exception as e:
            print("[V4]   reload.execute() FAIL: {}".format(e))
            if is_group:
                try: node.end()
                except: pass
    else:
        print("[V4]   NO 'reload' knob on InternalRead!")
        print("[V4]   Trying fromScript/setValue alternatives...")
        # Try knobChanged approach
        try:
            if is_group: node.begin()
            cur = read_target["file"].value()
            read_target["file"].fromUserText("")
            read_target["file"].fromUserText(cur)
            if is_group: node.end()
            print("[V4]   file clear+reload done")
        except Exception as e:
            print("[V4]   file reload FAIL: {}".format(e))

    # =====================================================================
    # Step 4: Also list Group-level knobs (looking for reload/update)
    # =====================================================================
    print("\n[V4] --- Step 4: Group-level knobs ---")
    grp_knobs = sorted(node.knobs().keys())
    for kname in grp_knobs:
        if any(i in kname.lower() for i in interesting):
            k = node[kname]
            ktype = type(k).__name__
            print("[V4]   {} ({})".format(kname, ktype))

    # =====================================================================
    # Step 5: Try nuke.updateUI()
    # =====================================================================
    print("\n[V4] --- Step 5: nuke.updateUI() ---")
    try:
        nuke.updateUI()
        print("[V4]   nuke.updateUI() OK")
    except Exception as e:
        print("[V4]   nuke.updateUI() FAIL: {}".format(e))

    # =====================================================================
    # Step 6: Try nuke.show(node)
    # =====================================================================
    print("\n[V4] --- Step 6: nuke.show(node) ---")
    try:
        nuke.show(node)
        print("[V4]   nuke.show() OK")
    except Exception as e:
        print("[V4]   nuke.show() FAIL: {}".format(e))

    # =====================================================================
    # Step 7: Toggle ps + nuke.updateUI combo
    # =====================================================================
    print("\n[V4] --- Step 7: ps toggle + updateUI combo ---")
    try:
        node["postage_stamp"].setValue(False)
        nuke.updateUI()
        QtWidgets.QApplication.processEvents()
        _time.sleep(0.1)
        node["postage_stamp"].setValue(True)
        nuke.updateUI()
        QtWidgets.QApplication.processEvents()
        print("[V4]   combo OK")
    except Exception as e:
        print("[V4]   combo FAIL: {}".format(e))

    # =====================================================================
    # Step 8: setFlag(0) approach
    # =====================================================================
    print("\n[V4] --- Step 8: postage_stamp.setFlag(0) ---")
    try:
        node["postage_stamp"].setFlag(0)
        print("[V4]   setFlag(0) OK")
    except Exception as e:
        print("[V4]   setFlag(0) FAIL: {}".format(e))

    # =====================================================================
    # Step 9: Root node force refresh
    # =====================================================================
    print("\n[V4] --- Step 9: Root setModified + frame jog ---")
    try:
        nuke.root().setModified(True)
        cur_f = nuke.frame()
        nuke.frame(cur_f)  # Jump to same frame, triggering re-evaluate
        print("[V4]   root.setModified + frame() OK")
    except Exception as e:
        print("[V4]   root refresh FAIL: {}".format(e))

    # =====================================================================
    # Step 10: CONTROL — Create plain Read node + see if it shows thumbnail
    # =====================================================================
    print("\n[V4] --- Step 10: Control — Create plain Read + check ---")
    test_read = None
    try:
        test_read = nuke.nodes.Read(file=image_path)
        test_read["postage_stamp"].setValue(True)
        nuke.updateUI()
        QtWidgets.QApplication.processEvents()
        _time.sleep(0.5)
        
        rpx = _px(test_read)
        print("[V4]   Created '{}', px={}".format(
            test_read.name(),
            "{:.4f},{:.4f},{:.4f}".format(*rpx) if rpx else "N/A"))
        print("[V4]   Does this Read node show a thumbnail in DAG?")
        print("[V4]   (If NOT, then postage_stamp is globally disabled in Preferences!)")
        
        # Try reload on this Read too
        if "reload" in test_read.knobs():
            test_read["reload"].execute()
            print("[V4]   Read reload.execute() OK")
    except Exception as e:
        print("[V4]   Control test FAIL: {}".format(e))

    # =====================================================================
    # Step 11: Check global postage_stamp preferences
    # =====================================================================
    print("\n[V4] --- Step 11: Check Nuke Preferences for postage stamps ---")
    try:
        root = nuke.root()
        prefs = nuke.toNode("preferences")
        if prefs:
            pref_knobs = sorted(prefs.knobs().keys())
            ps_prefs = [k for k in pref_knobs if "postage" in k.lower() or "stamp" in k.lower()]
            print("[V4]   Postage-stamp related prefs: {}".format(ps_prefs))
            for pk in ps_prefs:
                print("[V4]     {} = {}".format(pk, prefs[pk].value()))
        else:
            print("[V4]   'preferences' node not found")
    except Exception as e:
        print("[V4]   Prefs check FAIL: {}".format(e))

    px_after = _px(node)
    if px_after:
        print("\n[V4] AFTER pixel = {:.4f},{:.4f},{:.4f} (changed={})".format(
            *px_after, px_before != px_after))

    print("\n[V4] >>> CRITICAL QUESTION: Does the CONTROL Read8 show a thumbnail?")
    print("[V4]     If NO → Nuke postage_stamp is globally OFF / broken")
    print("[V4]     If YES → Group nodes need different treatment")
    print("=" * 70)


def diagnose_visual_refresh_v5(node_name=None, image_path=None):
    """V5 Diagnostic: 'Replacement Jutsu' — delete + rebuild the Group node.

    After 30+ failed refresh techniques (V1-V4), this approach sidesteps the
    stale-cache problem entirely by creating a brand-new C++ node instance
    via nuke.nodeCopy / nuke.nodePaste.

    Call from Nuke Script Editor:
        import importlib, ai_workflow.nanobanana as nb
        importlib.reload(nb)
        nb.diagnose_visual_refresh_v5("Nano_Viewer9",
            "E:/BaiduNetdiskDownload/nuke_workflow/temp/NanoBanana_Generate_frame5.jpg")
    """
    import nuke
    import time as _time

    print("=" * 70)
    print("[V5] diagnose_visual_refresh_v5 START  (Replacement Jutsu)")
    print("=" * 70)

    # --- Find target ---
    if not node_name:
        found = None
        for nd in nuke.allNodes("Group"):
            if "is_nb_player" in nd.knobs() and nd["is_nb_player"].value():
                found = nd; break
        if not found:
            print("[V5] ERROR: No NB Player node found!"); return
        node_name = found.name()

    node = nuke.toNode(node_name)
    if not node:
        print("[V5] ERROR: '{}' not found!".format(node_name)); return

    print("[V5] Target: '{}' (Class={})".format(node_name, node.Class()))

    # --- Get image_path ---
    if not image_path:
        if "nb_file" in node.knobs():
            image_path = node["nb_file"].value()
        elif "file" in node.knobs():
            image_path = node["file"].value()
    print("[V5] image_path = {}".format(repr(image_path)))

    if image_path and not os.path.isfile(image_path):
        print("[V5] WARNING: File missing: {}".format(repr(image_path)))

    # --- Sample BEFORE ---
    def _px(n):
        try: return (n.sample("red",0,0), n.sample("green",0,0), n.sample("blue",0,0))
        except: return None

    px_before = _px(node)
    if px_before:
        print("[V5] BEFORE pixel = {:.4f},{:.4f},{:.4f}".format(*px_before))

    # --- Record old state for comparison ---
    old_xpos = int(node["xpos"].value())
    old_ypos = int(node["ypos"].value())
    old_inputs = []
    for i in range(node.inputs()):
        inp = node.input(i)
        old_inputs.append(inp.name() if inp else None)
    old_deps = []
    for dep in node.dependent(nuke.INPUTS | nuke.HIDDEN_INPUTS):
        for i in range(dep.inputs()):
            if dep.input(i) == node:
                old_deps.append((dep.name(), i))

    print("[V5] Old pos: ({}, {})".format(old_xpos, old_ypos))
    print("[V5] Old inputs: {}".format(old_inputs))
    print("[V5] Old downstream: {}".format(old_deps))

    # =====================================================================
    # THE REPLACEMENT JUTSU
    # =====================================================================
    print("\n[V5] --- Executing Replacement Jutsu ---")
    new_node = _rebuild_group_for_thumbnail(node, image_path)

    if not new_node:
        print("[V5] FAILED! _rebuild_group_for_thumbnail returned None")
        print("[V5] Falling back to legacy _update_node_thumbnail...")
        # The old node may be deleted; try to find by name
        fallback = nuke.toNode(node_name)
        if fallback:
            _update_node_thumbnail(fallback, image_path)
        print("=" * 70)
        return

    # =====================================================================
    # Verify the new node
    # =====================================================================
    print("\n[V5] --- Verification ---")
    print("[V5] New node: '{}' (Class={})".format(new_node.name(), new_node.Class()))
    print("[V5] New pos: ({}, {})".format(
        int(new_node["xpos"].value()), int(new_node["ypos"].value())))

    # Check connections restored
    new_inputs = []
    for i in range(new_node.inputs()):
        inp = new_node.input(i)
        new_inputs.append(inp.name() if inp else None)
    print("[V5] New inputs: {}".format(new_inputs))

    new_deps = []
    for dep in new_node.dependent(nuke.INPUTS | nuke.HIDDEN_INPUTS):
        for i in range(dep.inputs()):
            if dep.input(i) == new_node:
                new_deps.append((dep.name(), i))
    print("[V5] New downstream: {}".format(new_deps))

    # Check InternalRead has correct file
    ir = _get_internal_read_nb(new_node)
    if ir:
        ir_file = ir["file"].value()
        print("[V5] InternalRead file: {}".format(repr(ir_file)))
    else:
        print("[V5] WARNING: No InternalRead in new node!")

    # Check pixel data
    px_after = _px(new_node)
    if px_after:
        print("[V5] AFTER pixel = {:.4f},{:.4f},{:.4f}".format(*px_after))

    # Check postage_stamp
    if "postage_stamp" in new_node.knobs():
        print("[V5] postage_stamp = {}".format(new_node["postage_stamp"].value()))

    # Check nb_file
    if "nb_file" in new_node.knobs():
        print("[V5] nb_file = {}".format(repr(new_node["nb_file"].value())))

    print("\n[V5] >>> CHECK THE DAG NOW!")
    print("[V5]     Does '{}' show the CORRECT thumbnail?".format(new_node.name()))
    print("[V5]     (The node was deleted and recreated — a fresh C++ instance)")
    print("=" * 70)

    return new_node


def diagnose_visual_refresh(node_name=None, image_path=None):
    """V2 Diagnostic: Focus on DAG VISUAL refresh only.
    
    Data layer is CONFIRMED working (Group pixels update correctly after file change).
    The remaining problem: DAG view shows STALE thumbnail despite correct pixels.
    
    Tests visual-only refresh techniques on the QGraphicsView / NodeItem level.
    """
    import nuke

    print("=" * 70)
    print("[VIS] diagnose_visual_refresh START")
    print("=" * 70)

    # --- Find target ---
    if not node_name:
        found = None
        for nd in nuke.allNodes("Group"):
            if "is_nb_player" in nd.knobs() and nd["is_nb_player"].value():
                found = nd; break
        if not found:
            for nd in nuke.allNodes("Read"): found = nd; break
        if not found:
            print("[VIS] ERROR: No node found!"); return
        node_name = found.name()

    node = nuke.toNode(node_name)
    if not node:
        print("[VIS] ERROR: '{}' not found!".format(node_name)); return

    is_group = (node.Class() == "Group")
    print("[VIS] Target: '{}' (Class={})".format(node_name, node.Class()))

    # --- Get image_path ---
    if not image_path:
        if "nb_file" in node.knobs(): image_path = node["nb_file"].value()
        elif "file" in node.knobs(): image_path = node["file"].value()
    if not os.path.isfile(image_path):
        print("[VIS] WARNING: File missing: {}".format(repr(image_path)))

    internal_read = _get_internal_read_nb(node) if is_group else None
    read_target = internal_read or node

    def _safe_sample(n):
        try: return (n.sample("red",0,0), n.sample("green",0,0), n.sample("blue",0,0))
        except: return None

    # =====================================================================
    # PHASE 0: Restore to known-good state
    # =====================================================================
    print("\n[VIS] PHASE 0: Restoring file...")
    try:
        if is_group: node.begin()
        read_target["file"].fromUserText(image_path)
        if is_group: node.end()
        px0 = _safe_sample(node)
        if px0: print("[VIS]   pixel(0,0) = {:.4f},{:.4f},{:.4f}".format(*px0))
    except Exception as e:
        print("[VIS]   FAIL: {}".format(e)); return

    # Ensure ps is ON
    if "postage_stamp" in node.knobs():
        node["postage_stamp"].setValue(True)

    # =====================================================================
    # PHASE 1: Enumerate all DAG-related widgets in detail
    # =====================================================================
    print("\n[VIS] PHASE 1: Enumerating DAG widgets...")
    app = QtWidgets.QApplication.instance()
    dag_info = {"qgv": [], "scene": [], "viewport": []}

    if app:
        for tw in app.topLevelWidgets():
            for gv in tw.findChildren(QtWidgets.QGraphicsView):
                cn = gv.metaObject().className()
                gv_id = "{}@{}".format(cn, id(gv))
                dag_info["qgv"].append(gv_id)

                scene = gv.scene()
                if scene:
                    n_items = len(scene.items())
                    dag_info["scene"].append("{} has {} items".format(gv_id, n_items))

                    # Look for items containing our node name
                    for item in scene.items():
                        try:
                            # Try to get item text/data
                            item_data = str(type(item).__name__)
                            if hasattr(item, 'toolTip'):
                                tt = item.toolTip()
                                if node_name in str(tt):
                                    dag_info["viewport"].append(
                                        "FOUND NodeItem: {} tooltip={}".format(item_data, tt[:60]))
                        except:
                            pass

                vp = gv.viewport()
                if vp:
                    dag_info["viewport"].append("viewport={}@{}".format(
                        type(vp).__name__, id(vp)))

    for cat, items in dag_info.items():
        print("[VIS]   {}: [{}]".format(cat, "; ".join(items) if items else "NONE"))

    # =====================================================================
    # PHASE 2: Visual refresh techniques (each followed by user check)
    # =====================================================================
    print("\n[VIS] PHASE 2: Testing VISUAL refresh techniques...")
    print("[VIS] Watch the DAG view after each technique!\n")

    results = []

    def _vtry(name, func):
        """Try one visual refresh technique."""
        try:
            func()
            results.append((name, "OK"))
            print("[VIS]   {:35s} OK".format(name))
        except Exception as e:
            results.append((name, "ERR:{}".format(str(e)[:40])))
            print("[VIS]   {:35s} ERR: {}".format(name, str(e)[:50]))

    # --- Technique 1: Full QGraphicsScene invalidate + repaint ---
    def _t1_full_invalidate():
        for tw in app.topLevelWidgets():
            for gv in tw.findChildren(QtWidgets.QGraphicsView):
                s = gv.scene()
                if s:
                    # Invalidate ALL layers
                    s.invalidate(s.sceneRect(), QtWidgets.QGraphicsScene.AllLayers)
                    s.update(s.sceneRect())
                gv.viewport().repaint()
                gv.repaint()
                gv.viewport().update()
    _vtry("T1.full_scene_invalidate+repaint", _t1_full_invalidate)

    # --- Technique 2: Toggle postage_stamp off→on (classic approach) ---
    def _t2_toggle_ps():
        node["postage_stamp"].setValue(False)
        node.processEvents() if hasattr(node, "processEvents") else None
        import time; time.sleep(0.05)
        node["postage_stamp"].setValue(True)
    _vtry("T2.toggle_ps_with_delay", _t2_toggle_ps)

    # --- Technique 3: Select/deselect to trigger node redraw ---
    def _t3_select_cycle():
        was_sel = node.isSelected()
        node.setSelected(False)
        QtWidgets.QApplication.processEvents()
        node.setSelected(True)
        QtWidgets.QApplication.processEvents()
        node.setSelected(was_sel)
    _vtry("T3.select_deselect_cycle", _t3_select_cycle)

    # --- Technique 4: Force DAG viewport full redraw via painter ---
    def _t4_viewport_redraw():
        for tw in app.topLevelWidgets():
            for gv in tw.findChildren(QtWidgets.QGraphicsView):
                vp = gv.viewport()
                # Force paint event
                p = QtGui.QPainter(vp)
                p.end()
                vp.update(vp.rect())
                # Also trigger resize event (forces full redraw)
                geo = gv.geometry()
                gv.setGeometry(geo.x(), geo.y(), geo.width()+1, geo.height())
                gv.setGeometry(geo)
    _vtry("T4.viewport_paint+resize_trick", _t4_viewport_redraw)

    # --- Technique 5: nuke.modified() + nuke.tcl(idletasks) ---
    def _t5_nuke_refresh():
        nuke.modified()
        __import__("nuke").tcl("idletasks")
        __import__("nuke").tcl("update idletasks")
        QtWidgets.QApplication.processEvents()
        QtWidgets.QApplication.sendPostedEvents(None, 0)
    _vtry("T5.nuke_modified+idletasks", _t5_nuke_refresh)

    # --- Technique 6: Find NodeItem in scene and call update() directly ---
    def _t6_nodeitem_update():
        for tw in app.topLevelWidgets():
            for gv in tw.findChildren(QtWidgets.QGraphicsView):
                s = gv.scene()
                if s:
                    updated = 0
                    for item in s.items():
                        item_str = str(type(item))
                        # NodeItem classes in Nuke DAG
                        if any(k in item_str for k in ["Node", "Item"]):
                            item.update()
                            updated += 1
                    if updated > 0:
                        print("[VIS]     Updated {} items in scene".format(updated))
    _vtry("T6.nodeitem_direct_update", _t6_nodeitem_update)

    # --- Technique 7: Change node position slightly (forces DAG relayout) ---
    def _t7_nudge_position():
        if "xpos" in node.knobs() and "ypos" in node.knobs():
            old_x = node["xpos"].value()
            old_y = node["ypos"].value()
            node["xpos"].setValue(old_x)
            node["ypos"].setValue(old_y)
    _vtry("T7.nudge_xpos_ypos_knob", _t7_nudge_position)

    # --- Technique 8: Re-read file + ps toggle in main thread ---
    def _t8_mainthread_reread():
        def _do_it():
            if is_group: node.begin()
            rd = internal_read or node
            cur = rd["file"].value()
            rd["file"].fromUserText("")
            rd["file"].fromUserText(cur)
            if is_group: node.end()
            node["postage_stamp"].setValue(False)
            node["postage_stamp"].setValue(True)
        nuke.executeInMainThread(_do_it)
    _vtry("T8.mainThread_reread+ps", _t8_mainthread_reread)

    # --- Technique 9: QTimer delayed cascade (ps toggle → invalidate → select) ---
    def _t9_delayed_cascade():
        _n = node
        def _step1():
            try:
                _n["postage_stamp"].setValue(False)
                _n["postage_stamp"].setValue(True)
            except: pass
        def _step2():
            for tw in app.topLevelWidgets():
                for gv in tw.findChildren(QtWidgets.QGraphicsView):
                    s = gv.scene()
                    if s: s.invalidate(s.sceneRect(), QtWidgets.QGraphicsScene.AllLayers)
        def _step3():
            _n.setSelected(False)
            _n.setSelected(True)
        QtCore.QTimer.singleShot(100, _step1)
        QtCore.QTimer.singleShot(300, _step2)
        QtCore.QTimer.singleShot(500, _step3)
    _vtry("T9.cascade_100_300_500ms", _t9_delayed_cascade)

    # --- Technique 10: Touch knob_value_changed callback trigger ---
    def _t10_touch_label():
        if "label" in node.knobs():
            old = node["label"].value()
            node["label"].setValue(old)
    _vtry("T10.touch_label_knob", _t10_touch_label)

    # --- Technique 11: Hide then show node from DAG ---
    def _t11_hide_show():
        if "hide_input" in node.knobs():
            node["hide_input"].setValue(True)
            QtWidgets.QApplication.processEvents()
            node["hide_input"].setValue(False)
        else:
            # Use opacity knob if available
            if "opacity" in node.knobs():
                node["opacity"].setValue(0.0)
                QtWidgets.QApplication.processEvents()
                node["opacity"].setValue(1.0)
    _vtry("T11.hide_show_toggle", _t11_hide_show)

    # =====================================================================
    # Summary
    # =====================================================================
    print("\n" + "=" * 70)
    print("[VIS] SUMMARY:")
    ok_count = sum(1 for _, s in results if s == "OK")
    err_count = len(results) - ok_count
    print("[VIS]   OK: {}  |  ERR: {}".format(ok_count, err_count))
    for name, status in results:
        print("[VIS]   {:35s} {}".format(name, status))

    final_px = _safe_sample(node)
    if final_px:
        print("\n[VIS] Final pixel(0,0) = {:.4f},{:.4f},{:.4f}".format(*final_px))
    
    print("\n[VIS] >>> Check DAG NOW: Did ANY technique update the thumbnail? <<<")
    print("=" * 70)


def test_thumbnail_refresh(node_name=None, image_path=None):
    """Deep diagnostic: WHY does Group node output NOT update after InternalRead file change?

    Tests at 4 layers:
      Layer 1 - InternalRead: does IT see new pixels after file change?
      Layer 2 - Group output: when does it pick up InternalRead changes?
      Layer 3 - Cache invalidation: which techniques force Group recompute?
      Layer 4 - postage_stamp render: does it use stale or fresh pixels?

    Call from Nuke Python Console:
        import ai_workflow.nanobanana as nb
        nb.diagnose_thumbnail_cache("Nano_Viewer9", "E:/path/to/different_image.jpg")
    """
    import nuke

    print("=" * 70)
    print("[DIAG] diagnose_thumbnail_cache START")
    print("=" * 70)

    # --- Find target ---
    if not node_name:
        found = None
        for nd in nuke.allNodes("Group"):
            if "is_nb_player" in nd.knobs() and nd["is_nb_player"].value():
                found = nd; break
        if not found:
            for nd in nuke.allNodes("Read"):
                found = nd; break
        if not found:
            print("[DIAG] ERROR: No node found!"); return
        node_name = found.name()

    node = nuke.toNode(node_name)
    if not node:
        print("[DIAG] ERROR: Node '{}' not found!".format(node_name)); return

    is_group = (node.Class() == "Group")
    print("[DIAG] Target: '{}' (Class={})".format(node_name, node.Class()))

    # --- Get image_path ---
    if not image_path:
        if "nb_file" in node.knobs(): image_path = node["nb_file"].value()
        elif "file" in node.knobs(): image_path = node["file"].value()
    if not os.path.isfile(image_path):
        print("[DIAG] WARNING: File missing: {}".format(repr(image_path)))

    # --- Helper: safe sample ---
    def _sample(nd, label=""):
        try:
            return (nd.sample("red",0,0), nd.sample("green",0,0), nd.sample("blue",0,0))
        except Exception as e:
            print("[DIAG]   {} sample FAIL: {}".format(label, e))
            return None

    # =====================================================================
    # LAYER 1: InternalRead state BEFORE any change
    # =====================================================================
    print("\n" + "-" * 70)
    print("[DIAG] ====== LAYER 1: Initial State ======")
    print("-" * 70)

    internal_read = _get_internal_read_nb(node) if is_group else None
    read_target = internal_read or node

    ir_file = ""
    ir_px = None
    group_px = _sample(node, "Group-BEFORE")

    if internal_read or node.Class() == "Read":
        try:
            ir_file = read_target["file"].value()
            print("[DIAG]   Read file = {}".format(repr(ir_file)))
            ir_px = _sample(read_target, "Read-BEFORE")
            if ir_px:
                print("[DIAG]   Read pixel(0,0) = {:.4f},{:.4f},{:.4f}".format(*ir_px))
        except Exception as e:
            print("[DIAG]   Read access error: {}".format(e))

    if group_px:
        print("[DIAG]   Group pixel(0,0) = {:.4f},{:.4f},{:.4f}".format(*group_px))

    ps_state = False
    if "postage_stamp" in node.knobs():
        ps_state = node["postage_stamp"].value()
        print("[DIAG]   postage_stamp = {}".format(ps_state))

    # Check Group internals
    if is_group:
        try:
            child_names = [c.name() for c in node.nodes()]
            print("[DIAG]   Group children: [{}]".format(", ".join(child_names)))
        except Exception as e:
            print("[DIAG]   Cannot list children: {}".format(e))

        # Check if InternalRead is actually connected to Output
        try:
            if internal_read:
                out_nodes = [n for n in node.nodes() if n.Class() == "Output"]
                if out_nodes:
                    out = out_nodes[0]
                    inp = out.input(0)
                    inp_name = inp.name() if inp else "None"
                    connected_to_output = (inp == internal_read)
                    print("[DIAG]   Output[0] connected to: '{}' (is_InternalRead={})".format(
                        inp_name, connected_to_output))
                    if not connected_to_output:
                        print("[DIAG]   *** WARNING: InternalRead NOT connected to Output! ***")
        except Exception as e:
            print("[DIAG]   Output check error: {}".format(e))

    # =====================================================================
    # LAYER 2: Change file and IMMEDIATELY check both levels
    # =====================================================================
    print("\n" + "-" * 70)
    print("[DIAG] ====== LAYER 2: File Change & Immediate Re-check ======")
    print("-" * 70)

    print("\n[DIAG] >>> Changing Read file to: {}".format(repr(image_path)))
    try:
        if is_group:
            node.begin()
        read_target["file"].fromUserText(image_path)
        new_ir_file = read_target["file"].value()
        if is_group:
            node.end()
        print("[DIAG] >>> Read file NOW = {}".format(repr(new_ir_file)))
    except Exception as e:
        print("[DIAG] >>> File change FAILED: {}".format(e))

    # Sample Read immediately
    new_ir_px = _sample(read_target, "Read-AFTER-file-change")
    if new_ir_px:
        match_ir = (ir_px == new_ir_px)
        print("[DIAG] >>> Read pixel(0,0) = {:.4f},{:.4f},{:.4f} (changed={})".format(
            *new_ir_px, not match_ir))

    # Sample Group immediately
    new_group_px = _sample(node, "Group-AFTER-file-change")
    if new_group_px:
        match_gp = (group_px == new_group_px)
        print("[DIAG] >>> Group pixel(0,0)= {:.4f},{:.4f},{:.4f} (changed={})".format(
            *new_group_px, not match_gp))

    if new_ir_px and new_group_px:
        same_as_read = (new_group_px == new_ir_px)
        print("[DIAG] >>> Group==Read pixels? {}".format(same_as_read))
        if not same_as_read:
            print("[DIAG] *** Group still showing OLD cached output! ***")

    # =====================================================================
    # LAYER 3: Try cache invalidation techniques ONE BY ONE
    # =====================================================================
    print("\n" + "-" * 70)
    print("[DIAG] ====== LAYER 3: Cache Invalidation Techniques ======")
    print("-" * 70)

    layer3_results = []

    def _try_technique(name, func):
        """Try one technique and report whether Group pixels changed."""
        before = _sample(node, "before_{}".format(name))
        try:
            func()
        except Exception as e:
            layer3_results.append((name, "ERROR: {}".format(str(e)[:40]), None))
            return
        after = _sample(node, "after_{}".format(name))
        changed = (before != after) if (before and after) else None
        status = "CHANGED" if changed else ("SAME" if (changed is False) else "CANT_SAMPLE")
        layer3_results.append((name, status, after))
        print("[DIAG]   {:30s} : {} pixel(0,0)={}".format(
            name, status,
            "{:.4f},{:.4f},{:.4f}".format(*after) if after else "N/A"))

    # Tech 3a: nuke.execute() on the Group - force full re-render of this frame
    _try_technique("3a.nuke_execute",
         lambda: nuke.execute(node, nuke.frame(), nuke.frame()))

    # Tech 3b: Force InternalRead re-read with knob value cycle
    _try_technique("3b.reread_internal",
         lambda: (_for_node_begin(read_target, is_group),
                  read_target["file"].fromUserText(""),
                  read_target["file"].fromUserText(image_path),
                  _for_node_end(is_group)))

    # Tech 3b-alt: Use setValue on file knob instead of fromUserText
    _try_technique("3b_alt.setValue",
         lambda: (_for_node_begin(read_target, is_group),
                  read_target["file"].setValue(image_path),
                  _for_node_end(is_group)))

    # Tech 3c: Touch the Group's proxy_format knob
    _try_technique("3c.touch_proxy_fmt",
         lambda: (node.begin(),
                  node["proxy_format"].setValue(node["proxy_format"].value()) if "proxy_format" in node.knobs() else None,
                  node.end()))

    # Tech 3d: Delete and recreate InternalRead's cache via knob clear/set
    _try_technique("3d.clearFile_setFile",
         lambda: (_for_node_begin(read_target, is_group),
                  read_target["file"].setValue(""),
                  __import__("nuke").tcl("update {}".format(node.name())),
                  read_target["file"].setValue(image_path),
                  __import__("nuke").tcl("update {}".format(node.name())),
                  _for_node_end(is_group)))

    # Tech 3e: nuke.tcl("node_mark_dirty ..." ) equivalent
    _try_technique("3e.tcl_update",
         lambda: __import__("nuke").tcl("update {}".format(node.name())))

    # Tech 3f: Force execution on InternalRead first, then check Group
    def _exec_internal_then_check():
        if is_group: node.begin()
        try:
            nuke.execute(read_target, nuke.frame(), nuke.frame())
        except: pass
        if is_group: node.end()
    _try_technique("3f.exec_internal",
         _exec_internal_then_check)

    # Tech 3g: Set frame to different then back
    cur_frame = nuke.frame()
    def _frame_jiggle():
        nuke.setFrame(int(cur_frame + 100))
        nuke.setFrame(int(cur_frame))
    _try_technique("3g.frame_jiggle", _frame_jiggle)

    # Tech 3h: Disable/enable InternalRead
    def _disable_enable_read():
        if is_group: node.begin()
        read_target["disable"].setValue(True)
        read_target["disable"].setValue(False)
        if is_group: node.end()
    _try_technique("3h.disable_enable_read",
         _disable_enable_read if "disable" in read_target.knobs()
         else lambda: None)

    # Tech 3i: Use begin/end wrapped file set + tcl update
    def _begin_end_tcl():
        node.begin()
        rd = _get_internal_read_nb(node)
        if rd:
            rd["file"].setValue("")
            import nuke as _n; _n.tcl("update " + node.name())
            rd["file"].setValue(image_path)
            _n.tcl("update " + node.name())
        node.end()
    _try_technique("3i.begin_end_tcl", _begin_end_tcl)

    # =====================================================================
    # LAYER 4: After finding best technique, apply to postage_stamp
    # =====================================================================
    print("\n" + "-" * 70)
    print("[DIAG] ====== LAYER 4: Summary & Best Approach ======")
    print("-" * 70)

    successes = [(name, px) for name, status, px in layer3_results if status == "CHANGED"]
    print("[DIAG] Techniques that WORKED (Group pixels updated):")
    if successes:
        for name, px in successes:
            print("[DIAG]   +++ {} -> pixel={:.4f},{:.4f},{:.4f}".format(name, *px))
    else:
        print("[DIAG]   *** NONE! All techniques failed to update Group output ***")

    print("\n[DIAG] Techniques that did NOTHING:")
    for name, status, px in layer3_results:
        if status != "CHANGED":
            print("[DIAG]   --- {} ({})".format(name, status))

    # Final: Try the winning approach + postage_stamp toggle
    if successes:
        best_name = successes[0][0]
        print("\n[DIAG] >>> Applying BEST technique '{}' + postage_stamp toggle...".format(best_name))

        # Re-apply file change (in case we're testing with same file)
        if is_group: node.begin()
        read_target["file"].fromUserText(image_path)
        if is_group: node.end()

        # Run best technique again
        for name, _, _ in layer3_results:
            if name == best_name: break
        # We already ran them all, so just do final ps toggle
        node["postage_stamp"].setValue(False)
        node["postage_stamp"].setValue(True)

        final_px = _sample(node, "FINAL")
        if final_px:
            print("[DIAG] >>> FINAL pixel(0,0) = {:.4f},{:.4f},{:.4f}".format(*final_px))
            if final_px != group_px:
                print("[DIAG] >>> SUCCESS! Thumbnail should now be updated!")
            else:
                print("[DIAG] >>> Still same as initial...")

    print("\n" + "=" * 70)
    print("[DIAG] diagnose_thumbnail_cache END")
    print("=" * 70)



# Note: _for_node_begin / _for_node_end are no-ops because the caller
# (each _try lambda) already manages node.begin()/node.end() context.


def test_thumbnail_refresh(node_name=None, image_path=None):
    """Test function: update a Nano Viewer thumbnail and try all refresh methods.

    Call from Nuke's Python Console:
        # Example 1: Use existing Nano_Viewer7 with a new image
        import ai_workflow.nanobanana as nb
        nb.test_thumbnail_refresh("Nano_Viewer7", "E:/path/to/new_image.jpg")

        # Example 2: Auto-find first NB Player node and refresh current image
        nb.test_thumbnail_refresh()

        # Example 3: Test on a Read node
        n = nuke.nodes.Read(file="E:/path/to/image.jpg")
        nb.test_thumbnail_refresh(n.name())
    """
    import nuke

    print("=" * 60)
    print("[TEST] test_thumbnail_refresh START")
    print("[TEST]   node_name = {}".format(repr(node_name)))
    print("[TEST]   image_path = {}".format(repr(image_path)))

    # --- Find the target node ---
    if not node_name:
        found = None
        for nd in nuke.allNodes("Group"):
            if "is_nb_player" in nd.knobs() and nd["is_nb_player"].value():
                found = nd
                break
        if not found:
            reads = [n for n in nuke.allNodes("Read")]
            if reads:
                found = reads[0]
        if not found:
            print("[TEST] ERROR: No NB Player or Read node found!")
            return
        node_name = found.name()
        print("[TEST]   Auto-selected: '{}'".format(node_name))

    node = nuke.toNode(node_name)
    if not node:
        print("[TEST] ERROR: Node '{}' not found!".format(node_name))
        return

    print("[TEST]   Node '{}' (Class={})".format(node.name(), node.Class()))

    # --- Determine image path ---
    if not image_path:
        if "nb_file" in node.knobs():
            image_path = node["nb_file"].value()
        elif "file" in node.knobs():
            image_path = node["file"].value()
        else:
            print("[TEST] ERROR: No file knob!")
            return
        if not os.path.isfile(image_path):
            print("[TEST] WARNING: File doesn't exist: {}".format(repr(image_path)))

    internal_read = _get_internal_read_nb(node) if node.Class() == "Group" else None

    # --- Sample BEFORE change ---
    old_px = (0, 0, 0)
    try:
        old_px = (node.sample("red", 0, 0), node.sample("green", 0, 0), node.sample("blue", 0, 0))
    except:
        pass
    print("[TEST] BEFORE pixel(0,0) = {:.4f},{:.4f},{:.4f}".format(*old_px))

    # --- Update file path ---
    print("\n[TEST] Updating to: {}".format(image_path))
    if internal_read:
        node.begin()
        internal_read["file"].fromUserText(image_path)
        node.end()
    elif "file" in node.knobs():
        node["file"].fromUserText(image_path)

    # --- Sample AFTER change ---
    new_px = (0, 0, 0)
    try:
        new_px = (node.sample("red", 0, 0), node.sample("green", 0, 0), node.sample("blue", 0, 0))
    except:
        pass
    print("[TEST] AFTER  pixel(0,0) = {:.4f},{:.4f},{:.4f} (changed={})".format(
        *new_px, old_px != new_px))

    # Ensure postage_stamp is ON
    if "postage_stamp" in node.knobs():
        node["postage_stamp"].setValue(True)

    # --- Try each refresh method individually ---
    results = {}

    # A: toggle postage_stamp
    try:
        node["postage_stamp"].setValue(False); node["postage_stamp"].setValue(True)
        results["A.toggle_ps"] = "OK"
    except Exception as e: results["A.toggle_ps"] = str(e)[:40]

    # B: sample force
    try:
        node.sample("red", 0, 0)
        results["B.sample"] = "OK"
    except Exception as e: results["B.sample"] = str(e)[:40]

    # C: nuke.modified
    try:
        nuke.modified(); results["C.modified"] = "OK"
    except Exception as e: results["C.modified"] = str(e)[:40]

    # D: QGraphicsView scene invalidate
    try:
        app = QtWidgets.QApplication.instance(); n_gv = 0
        for tw in app.topLevelWidgets():
            for gv in tw.findChildren(QtWidgets.QGraphicsView):
                s = gv.scene()
                if s: s.invalidate(s.sceneRect(), QtWidgets.QGraphicsScene.AllLayers)
                vp = gv.viewport()
                if vp: vp.repaint()
                n_gv += 1
        results["D.QGView({}v)".format(n_gv)] = "OK"
    except Exception as e: results["D.QGView"] = str(e)[:40]

    # E: select toggle
    try:
        s = node.isSelected(); node.setSelected(not s); node.setSelected(s)
        results["E.select_toggle"] = "OK"
    except Exception as e: results["E.select_toggle"] = str(e)[:40]

    # F: re-read file (clear + reload)
    try:
        rd = internal_read or node
        cur = rd["file"].value() if "file" in rd.knobs() else ""
        rd["file"].fromUserText(""); rd["file"].fromUserText(cur)
        results["F.reread"] = "OK"
    except Exception as e: results["F.reread"] = str(e)[:40]

    # G: delayed re-read + ps toggle at 800ms
    _nr = node; _ip = image_path; _ir = _get_internal_read_nb(_nr) if _nr.Class()=="Group" else None
    def _delayed_G():
        try:
            if _nr.Class()=="Group": _nr.begin()
            rd = _ir or _nr
            c = rd["file"].value() if "file" in rd.knobs() else ""
            rd["file"].fromUserText(""); rd["file"].fromUserText(c)
            _nr["postage_stamp"].setValue(False); _nr["postage_stamp"].setValue(True)
            if _nr.Class()=="Group": _nr.end()
            print("[TEST] Method G (800ms): done")
        except Exception as eg: print("[TEST] Method G fail: {}".format(eg))
    QtCore.QTimer.singleShot(800, _delayed_G)
    results["G.delayed_800ms"] = "SCHEDULED"

    # Print summary table
    print("\n[TEST] === REFRESH RESULTS ===")
    for m, r in sorted(results.items()):
        print("  {:20s} : {}".format(m, r))
    print("\n[TEST] Check DAG view NOW - did thumbnail update?")
    print("[TEST] Wait ~1s for Method G (delayed)")
    print("=" * 60)


def restore_nb_thumbnails():
    """Restore postage-stamp previews for all NB Player nodes in the current script.

    Called on script load so that existing NB Player nodes display their
    thumbnail in the Node Graph (like Read nodes).
    Uses Replacement Jutsu to force fresh thumbnail rendering.
    """
    restored = 0
    # Collect nodes first to avoid modifying allNodes() during iteration
    nb_players = [n for n in nuke.allNodes("Group")
                  if "is_nb_player" in n.knobs() and n["is_nb_player"].value()]
    for node in nb_players:
        img = None
        if "nb_file" in node.knobs():
            img = node["nb_file"].value()
        rebuilt = _rebuild_group_for_thumbnail(node, img)
        if not rebuilt:
            # Fallback to legacy (best-effort)
            _update_node_thumbnail(node, img)
        restored += 1
    if restored:
        print("[NB] restore: rebuilt {} node(s) for fresh thumbnails".format(restored))


def create_nb_player_node(image_path=None, name=None, xpos=None, ypos=None,
                         prompt="", neg_prompt="", model="",
                         ratio="auto", resolution="1K", seed=0,
                         input_images=None, gen_name=""):
    """
    Create a NanoBanana Player Group node wrapping a Read node.
    Similar to VEO Player but for single images (no frame range knobs).
    
    Now includes generation parameters and regeneration capability,
    replacing the old Prompt node pattern.

    Returns:
        tuple: (group_node, internal_read_node)
    """
    # Wrap entire node creation as a single undo unit so that
    # subsequent rename-undo cannot partially break internal structure
    nuke.Undo.begin("Create Nano Viewer")
    try:
        _default_name = _next_node_name("Nano_Viewer")
        group = nuke.nodes.Group(name=(name or _default_name))

        if xpos is not None:
            group["xpos"].setValue(int(xpos))
        if ypos is not None:
            group["ypos"].setValue(int(ypos))

        # Green colour (same as VEO Player)
        group["tile_color"].setValue(0x2E2E2EFF)
        # No label — keep the node name clean in the DAG

        # --- Build internals: Read → Output ---
        group.begin()
        read_node = nuke.nodes.Read(name="InternalRead")
        out_node = nuke.nodes.Output(name="Output")
        out_node.setInput(0, read_node)
        group.end()

        # Load the image file AFTER group.end() so knobs are fully populated
        if image_path and os.path.exists(image_path):
            group.begin()
            read_node["file"].fromUserText(image_path)
            group.end()

        # --- Expose Read-tab knobs on the Group panel ---
        # Use REAL knobs (NOT Link_Knob) so they survive rename-undo.
        # Link_Knob stores hardcoded TCL paths like "NodeName.InternalRead.format"
        # which break after undo-rename. Real knobs store actual values,
        # and a knobChanged callback keeps them synced via name lookup.
        tab_read = nuke.Tab_Knob("read_tab", "Read")
        group.addKnob(tab_read)

        # --- file knob ---
        file_knob = nuke.File_Knob("nb_file", "file")
        if image_path:
            file_knob.setValue(image_path.replace("\\", "/"))
        group.addKnob(file_knob)

        # Track which knobs need syncing between Group panel <-> internal Read
        _read_sync_knobs = []

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
            print("[NB Player] format.value type={} repr={}".format(type(_fv).__name__, repr(_fv)))
            if hasattr(_fv, 'width') and _fv.width() > 0:
                fmt_current = '%dx%d' % (_fv.width(), _fv.height())
                print("[NB Player] format from width/height: {}".format(fmt_current))
            elif hasattr(_fv, 'name') and _fv.name():
                fmt_current = _fv.name()
                print("[NB Player] format from name: {}".format(fmt_current))
            else:
                print("[NB Player] format fallback: str={}".format(str(_fv)))
        except Exception as e:
            print("[NB Player] ERR format read: {}".format(e))
        # Fallback: if format is a preset name (not WxH), try PIL for actual image size
        _looks_like_preset = (
            fmt_current != "---"
            and ('x' not in fmt_current or not fmt_current.split('x')[0].strip().isdigit())
        )
        if _looks_like_preset:
            print("[NB Player] format '{}' looks like preset, trying PIL...".format(fmt_current))
            _w, _h = 0, 0
            try:
                import PIL.Image as _PIL
                _img = _PIL.open(image_path) if (image_path and os.path.exists(image_path)) else None
                if _img:
                    _w, _h = _img.size
                    _img.close()
                    print("[NB Player] PIL size: {}x{}".format(_w, _h))
            except Exception as e:
                print("[NB Player] ERR PIL: {}".format(e))
            if _w > 0 and _h > 0:
                fmt_current = '%dx%d' % (_w, _h)
                print("[NB Player] format final (PIL): {}".format(fmt_current))
            else:
                print("[NB Player] WARN PIL failed, keeping preset '{}'".format(fmt_current))
        # Ensure fmt_current is in the dropdown (images may have non-preset sizes like 1024x1024)
        if fmt_current and fmt_current not in fmt_values:
            fmt_values.append(fmt_current)
            print("[NB Player] added '{}' to format dropdown".format(fmt_current))
        format_knob = nuke.Enumeration_Knob("nb_format", "format", fmt_values)
        print("[NB Player] setting nb_format='{}'".format(fmt_current))
        format_knob.setValue(fmt_current)
        group.addKnob(format_knob)
        _read_sync_knobs.append(("nb_format", "format"))

        # --- colorspace (Input Transform), premultiplied, raw, auto_alpha ---
        # Use real knobs, NOT Link_Knob. colorspace uses Enumeration_Knob for dropdown.

        if "colorspace" in read_node.knobs():
            cs_label = read_node["colorspace"].label() or "colorspace"
            # Build dropdown from Read node's own colorspace enum values
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
            cs_knob = nuke.Enumeration_Knob("nb_colorspace", cs_label, cs_values)
            cs_knob.setValue(current_cs)
            cs_knob.setFlag(nuke.STARTLINE)
            group.addKnob(cs_knob)
            _read_sync_knobs.append(("nb_colorspace", "colorspace"))

        for kname in ["premultiplied", "raw", "auto_alpha"]:
            if kname in read_node.knobs():
                klabel = read_node[kname].label() or kname
                real_knob = nuke.Boolean_Knob("nb_" + kname, klabel)
                real_knob.setValue(int(read_node[kname].value()))
                real_knob.clearFlag(nuke.STARTLINE)
                group.addKnob(real_knob)
                _read_sync_knobs.append(("nb_" + kname, kname))

        # --- Button to open internal Read node's full properties ---
        open_read_script = (
            "n = nuke.thisNode()\n"
            "n.begin()\n"
            "r = nuke.toNode('InternalRead')\n"
            "n.end()\n"
            "if r:\n"
            "    nuke.show(r)\n"
        )
        open_btn = nuke.PyScript_Knob(
            "open_read_props", "Open Full Read Properties", open_read_script
        )
        open_btn.setFlag(nuke.STARTLINE)
        group.addKnob(open_btn)

        # --- knobChanged callback: sync ALL exposed knobs <-> internal Read ---
        # Uses name-based lookup (nuke.toNode) so it survives rename-undo.
        # DEBUG: logs to Nuke Script Editor console
        _sync_pairs_str = repr(_read_sync_knobs).replace("'", '"')
        kc_script = (
            "import nuke\n"
            "n = nuke.thisNode()\n"
            "k = nuke.thisKnob()\n"
            "kn = k.name()\n"
            "n.begin()\n"
            "r = nuke.toNode('InternalRead')\n"
            "n.end()\n"
            "if not r:\n"
            "    pass\n"
            "# NOTE: no 'return' - Nuke runs knobChanged as standalone script, not a function\n"
            "# File changed: load + pull fresh values from Read\n"
            "if kn == 'nb_file' and r:\n"
            "    # IMPORTANT: fromUserText must run inside group context\n"
            "    n.begin()\n"
            "    r['file'].fromUserText(k.value())\n"
            "    n.end()\n"
            "    import json as _json_mod, os as _os_mod\n"
            "    try:\n"
            "        _fv = r['format'].value()\n"
            "        _fn = ''\n"
            "        if hasattr(_fv, 'width') and _fv.width() > 0:\n"
            "            _fn = '%dx%d' % (_fv.width(), _fv.height())\n"
            "        elif isinstance(_fv, str) and _fv:\n"
            "            _fn = _fv\n"
            "        # Fallback: if format looks like a preset name (not WxH), use PIL\n"
            "        if _fn and ('x' not in _fn or not _fn.split('x')[0].strip().isdigit()):\n"
            "            try:\n"
            "                from PIL import Image as _PILImage\n"
            "                _fp = k.value().strip()\n"
            "                if _fp and _os_mod.path.isfile(_fp):\n"
            "                    _pil_img = _PILImage.open(_fp)\n"
            "                    _w, _h = _pil_img.size\n"
            "                    _pil_img.close()\n"
            "                    if _w > 0 and _h > 0:\n"
            "                        _fn = '%dx%d' % (_w, _h)\n"
            "            except Exception:\n"
            "                pass\n"
            "        if _fn:\n"
            "            _cur = list(n['nb_format'].values())\n"
            "            if _fn not in _cur:\n"
            "                n['nb_format'].setValues(_cur + [_fn])\n"
            "            n['nb_format'].setValue(_fn)\n"
            "    except Exception:\n"
            "        pass\n"
            "    try:\n"
            "        _cv = str(r['colorspace'].value())\n"
            "        n['nb_colorspace'].setValue(_cv)\n"
            "    except Exception:\n"
            "        pass\n"
            "    # Force postage stamp refresh after file change\n"
            "    if 'postage_stamp' in n.knobs():\n"
            "        n['postage_stamp'].setValue(True)\n"
            "    # Force pixel computation so postage stamp has data to render\n"
            "    try:\n"
            "        n.sample('red', 0, 0)\n"
            "    except Exception:\n"
            "        pass\n"
            "# Sync Group->Read for all other exposed knobs\n"
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
            "        except Exception:\n"
            "            pass\n"
        )
        group["knobChanged"].setValue(kc_script)
        # --- Divider + Send to Studio button ---
        studio_divider = nuke.Text_Knob("studio_divider", "")
        group.addKnob(studio_divider)

        studio_btn = nuke.PyScript_Knob("send_to_studio", "Send To Sequence", _SEND_TO_STUDIO_SCRIPT)
        studio_btn.setFlag(nuke.STARTLINE)
        group.addKnob(studio_btn)

        # ============================================================
        # Generation parameters + Regenerate Tab (replaces old Prompt node)
        # ============================================================
        tab_gen = nuke.Tab_Knob("gen_tab", "Regenerate")
        group.addKnob(tab_gen)

        # Store generation settings as hidden knobs (read by PyCustom widget)
        model_knob = nuke.String_Knob("nb_model", "Model")
        model_knob.setValue(model or "")
        model_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(model_knob)

        ratio_knob = nuke.String_Knob("nb_ratio", "Ratio")
        ratio_knob.setValue(ratio or "")
        ratio_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(ratio_knob)

        res_knob = nuke.String_Knob("nb_resolution", "Resolution")
        res_knob.setValue(resolution or "")
        res_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(res_knob)

        seed_val = int(seed) if seed else 0
        seed_clamped = min(seed_val, 2147483647)
        seed_knob = nuke.Int_Knob("nb_seed", "Seed")
        seed_knob.setValue(seed_clamped)
        seed_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(seed_knob)

        prompt_knob = nuke.Multiline_Eval_String_Knob("nb_prompt", "Prompt")
        prompt_knob.setValue(prompt)
        prompt_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(prompt_knob)

        neg_knob = nuke.Multiline_Eval_String_Knob("nb_neg_prompt", "Negative Prompt")
        neg_knob.setValue(neg_prompt)
        neg_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(neg_knob)

        output_path_knob = nuke.File_Knob("nb_output_path", "Output Path")
        output_path_knob.setValue((image_path or "").replace("\\", "/"))
        output_path_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(output_path_knob)

        # Store input reference images as JSON (for Regenerate panel ImageStrip)
        input_img_paths = []
        if input_images:
            for img in (input_images if isinstance(input_images, list) else []):
                if isinstance(img, dict):
                    p = img.get("path", "")
                else:
                    p = str(img)
                if p:
                    input_img_paths.append(p)
        print("[NB Player] nb_input_images: storing {} paths".format(len(input_img_paths)))
        for _ip in input_img_paths:
            print("  [NB Player]   -> {}".format(_ip))
        # Use Multiline_Eval_String_Knob instead of String_Knob to avoid
        # the ~256 char length limit that truncates long JSON arrays of
        # file paths (especially temp-dir paths on Windows).
        # CRITICAL: setValue MUST be called AFTER addKnob, otherwise Nuke
        # resets the value when addKnob is called!
        inputs_json_knob = nuke.Multiline_Eval_String_Knob("nb_input_images", "Input Images")
        inputs_json_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(inputs_json_knob)
        inputs_json_knob.setValue(json.dumps(input_img_paths))

        # Store generator node name so we can find cached input images later
        if gen_name:
            gen_name_knob = nuke.String_Knob("nb_gen_name", "Generator Name")
            gen_name_knob.setFlag(nuke.INVISIBLE)
            group.addKnob(gen_name_knob)
            gen_name_knob.setValue(gen_name)

        # PyCustom_Knob for the regenerate UI widget
        regen_custom = nuke.PyCustom_Knob(
            "nanobanana_regen_ui",
            "",
            "ai_workflow.nanobanana.NanoBananaPlayerRegenWidget()"
        )
        regen_custom.setFlag(nuke.STARTLINE)
        group.addKnob(regen_custom)

        # --- Hidden marker knob ---
        marker = nuke.Boolean_Knob("is_nb_player", "")
        marker.setValue(True)
        marker.setFlag(nuke.INVISIBLE)
        group.addKnob(marker)

        # Set colorspace to sRGB for generated images
        try:
            read_node["colorspace"].setValue("sRGB")
        except Exception:
            pass

        # --- Set thumbnail icon on the Group node (like Read's postage stamp) ---
        _update_node_thumbnail(group, image_path)

        return group, read_node
    finally:
        nuke.Undo.end()


# ---------------------------------------------------------------------------
# Prompt Node Creation and Management
# ---------------------------------------------------------------------------
def create_prompt_node(generator_node, prompt, neg_prompt, model, ratio, resolution, seed, 
                       output_image_path, images_info=None):
    """
    Create a NanoBanana_Prompt node that records the generation settings
    and allows regeneration.
    
    Args:
        generator_node: The NanoBanana_Generate node
        prompt: The prompt text
        neg_prompt: Negative prompt
        model: Model ID
        ratio: Aspect ratio
        resolution: Resolution
        seed: Seed used
        output_image_path: Path to the generated image
        images_info: List of input image info dicts
    
    Returns:
        tuple: (prompt_node, read_node)
    """
    # Get position of generator node
    gen_x = generator_node["xpos"].value()
    gen_y = generator_node["ypos"].value()
    gen_name = generator_node.name()
    
    # Find existing prompt nodes that belong to THIS generator
    # Walk the chain starting from generator_node's dependent nodes
    existing_prompts = []
    
    def find_prompt_chain(start_node):
        """Recursively find all Prompt nodes connected downstream from start_node."""
        for node in nuke.allNodes("Group"):
            if node.name().startswith("Prompt") and "nanobanana_prompt_tab" in node.knobs():
                # Check if this prompt's input 0 connects back to start_node
                inp = node.input(0)
                if inp and (inp.name() == start_node.name()):
                    existing_prompts.append(node)
                    find_prompt_chain(node)
    
    find_prompt_chain(generator_node)
    
    # Also check for prompts that store this generator's name
    # (for prompts that might have been reconnected)
    for node in nuke.allNodes("Group"):
        if node.name().startswith("Prompt") and "nanobanana_prompt_tab" in node.knobs():
            if "nb_gen_name" in node.knobs():
                if node["nb_gen_name"].value() == gen_name:
                    if node not in existing_prompts:
                        existing_prompts.append(node)
    
    # Calculate position for new prompt node (below generator or last prompt)
    if existing_prompts:
        # Find the last prompt node (furthest down)
        last_prompt = max(existing_prompts, key=lambda n: n["ypos"].value())
        prompt_x = last_prompt["xpos"].value()
        prompt_y = last_prompt["ypos"].value() + 150
        # Connect to last prompt's output
        connect_to = last_prompt
    else:
        prompt_x = gen_x
        prompt_y = gen_y + 150
        connect_to = generator_node

    # --- Create a Dot node between generator/previous-prompt and this prompt ---
    dot_node = nuke.nodes.Dot()
    dot_x = int(prompt_x) + 34  # Dot is small, offset to center under parent
    if not existing_prompts:
        dot_y = int(gen_y) + 100
    else:
        dot_y = int(prompt_y) - 50
    dot_node["xpos"].setValue(dot_x)
    dot_node["ypos"].setValue(dot_y)
    dot_node.setInput(0, connect_to)

    # Create Prompt node (Group with custom UI)
    prompt_node = nuke.nodes.Group()
    prompt_num = len(existing_prompts) + 1
    prompt_node.setName("Prompt{}".format(prompt_num))
    prompt_node["tile_color"].setValue(0x8B5CF6FF)  # Purple color
    prompt_node["xpos"].setValue(int(prompt_x))
    prompt_node["ypos"].setValue(int(prompt_y))
    
    # Leave label empty – user requested no prompt text in label
    prompt_node["label"].setValue("")
    
    # Build internal structure
    prompt_node.begin()
    inp = nuke.nodes.Input(name="Input")
    out = nuke.nodes.Output(name="Output")
    out.setInput(0, inp)
    prompt_node.end()
    
    # Prompt node connects to the Dot
    prompt_node.setInput(0, dot_node)
    
    # Add custom tab with stored settings
    tab = nuke.Tab_Knob("nanobanana_prompt_tab", "NanoBanana Prompt")
    prompt_node.addKnob(tab)
    
    # Store generator node name (for finding cached input images on reload)
    # Unified name across all node types: nb_gen_name
    if generator_node:
        gk = nuke.String_Knob("nb_gen_name", "Generator Name")
        gk.setFlag(nuke.INVISIBLE)
        prompt_node.addKnob(gk)
        gk.setValue(generator_node.name())

    # Store generation settings in knobs (all hidden - shown in custom widget)
    model_knob = nuke.String_Knob("nb_model", "Model")
    model_knob.setValue(model)
    model_knob.setFlag(nuke.INVISIBLE)
    prompt_node.addKnob(model_knob)
    
    ratio_knob = nuke.String_Knob("nb_ratio", "Ratio")
    ratio_knob.setValue(ratio)
    ratio_knob.setFlag(nuke.INVISIBLE)
    prompt_node.addKnob(ratio_knob)
    
    res_knob = nuke.String_Knob("nb_resolution", "Resolution")
    res_knob.setValue(resolution)
    res_knob.setFlag(nuke.INVISIBLE)
    prompt_node.addKnob(res_knob)
    
    # Seed - clamp to valid range for Int_Knob (max 2147483647)
    seed_knob = nuke.Int_Knob("nb_seed", "Seed")
    seed_clamped = min(int(seed), 2147483647)
    seed_knob.setValue(seed_clamped)
    seed_knob.setFlag(nuke.INVISIBLE)
    prompt_node.addKnob(seed_knob)
    
    # Prompt text (hidden - shown in custom widget)
    prompt_knob = nuke.Multiline_Eval_String_Knob("nb_prompt", "Prompt")
    prompt_knob.setValue(prompt)
    prompt_knob.setFlag(nuke.INVISIBLE)
    prompt_node.addKnob(prompt_knob)
    
    # Negative prompt (hidden - shown in custom widget)
    neg_knob = nuke.Multiline_Eval_String_Knob("nb_neg_prompt", "Negative")
    neg_knob.setValue(neg_prompt)
    neg_knob.setFlag(nuke.INVISIBLE)
    prompt_node.addKnob(neg_knob)
    
    # Output image path (hidden)
    output_knob = nuke.File_Knob("nb_output_path", "Output")
    output_knob.setValue(output_image_path.replace("\\", "/"))
    output_knob.setFlag(nuke.INVISIBLE)
    prompt_node.addKnob(output_knob)
    
    # Store input image paths as JSON
    input_paths = []
    if images_info:
        for img in images_info:
            if img.get("connected") and img.get("path"):
                input_paths.append(img["path"])
    
    print("NanoBanana: Saving {} input image paths to Prompt node".format(len(input_paths)))
    for p in input_paths:
        print("  - {}".format(p))
    
    # CRITICAL: setValue MUST be called AFTER addKnob, otherwise Nuke
    # resets the value when addKnob is called!
    inputs_json_knob = nuke.Multiline_Eval_String_Knob("nb_input_images", "Input Images (JSON)")
    inputs_json_knob.setFlag(nuke.INVISIBLE)
    prompt_node.addKnob(inputs_json_knob)
    inputs_json_knob.setValue(json.dumps(input_paths))

    # Store generator node name so we can find cached input images later
    if generator_node:
        gen_name_knob = nuke.String_Knob("nb_gen_name", "Generator Name")
        gen_name_knob.setFlag(nuke.INVISIBLE)
        prompt_node.addKnob(gen_name_knob)
        gen_name_knob.setValue(generator_node.name())
    # Add PyCustom_Knob for regenerate UI
    divider = nuke.Text_Knob("divider1", "")
    prompt_node.addKnob(divider)
    
    custom_knob = nuke.PyCustom_Knob(
        "nanobanana_prompt_ui",
        "",
        "ai_workflow.nanobanana.NanoBananaPromptKnobWidget(nuke.thisNode())"
    )
    custom_knob.setFlag(nuke.STARTLINE)
    prompt_node.addKnob(custom_knob)
    
    # Create NB Player node (Group wrapping Read) for output image
    read_node = None
    player_node = None
    if output_image_path and os.path.exists(output_image_path):
        print("[NB Prompt] create_nb_player_node with {} images_info".format(len(images_info or [])))
        player_node, read_node = create_nb_player_node(
            image_path=output_image_path,
            name="生成图像Prompt{}".format(prompt_num),
            xpos=prompt_x + 200,
            ypos=prompt_y,
            input_images=images_info,
        )
        
        # Store reference to Player node in Prompt node
        read_ref_knob = nuke.String_Knob("nb_read_node", "Read Node")
        read_ref_knob.setValue(player_node.name())
        read_ref_knob.setFlag(nuke.INVISIBLE)
        prompt_node.addKnob(read_ref_knob)
        
        # Player's input 0 connects to prompt node (Prompt → Player direction)
        player_node.setInput(0, prompt_node)
        
        print("NanoBanana: Created NB Player '{}' for output: {}".format(player_node.name(), output_image_path))
    else:
        print("NanoBanana: WARNING - No output image to create Player node. Path: {}".format(output_image_path))
        # Still add the read node reference knob (empty)
        read_ref_knob = nuke.String_Knob("nb_read_node", "Read Node")
        read_ref_knob.setValue("")
        read_ref_knob.setFlag(nuke.INVISIBLE)
        prompt_node.addKnob(read_ref_knob)
    
    return prompt_node, player_node


def update_prompt_read_node(prompt_node, new_image_path):
    """Update the NB Player (or legacy Read) node associated with a Prompt node.
    If the node doesn't exist, create a new NB Player."""
    if "nb_read_node" not in prompt_node.knobs():
        return None

    player_node_name = prompt_node["nb_read_node"].value()
    player_node = nuke.toNode(player_node_name) if player_node_name else None

    if player_node:
        # Check if it's an NB Player Group or a legacy Read node
        internal_read = _get_internal_read_nb(player_node)
        if internal_read:
            # It's an NB Player Group — update the internal Read
            internal_read["file"].fromUserText(new_image_path)
            # Also sync the Group's nb_file knob
            if "nb_file" in player_node.knobs():
                player_node["nb_file"].setValue(new_image_path.replace("\\", "/"))
        elif player_node.Class() == "Read":
            # Legacy Read node — update directly
            player_node["file"].fromUserText(new_image_path)
        # Update stored path
        if "nb_output_path" in prompt_node.knobs():
            prompt_node["nb_output_path"].setValue(new_image_path.replace("\\", "/"))
        # Update the node thumbnail — use Replacement Jutsu for Group nodes
        if player_node.Class() == "Group" and "is_nb_player" in player_node.knobs():
            rebuilt = _rebuild_group_for_thumbnail(player_node, new_image_path)
            if rebuilt:
                player_node = rebuilt
            else:
                _update_node_thumbnail(player_node, new_image_path)
        else:
            _update_node_thumbnail(player_node, new_image_path)
        return player_node

    # Node not found — create a new NB Player
    prompt_x = int(prompt_node["xpos"].value())
    prompt_y = int(prompt_node["ypos"].value())

    player_node, read_node = create_nb_player_node(
        image_path=new_image_path,
        name="regenerated_image",
        xpos=prompt_x + 200,
        ypos=prompt_y,
    )

    # Store new node reference
    if "nb_read_node" in prompt_node.knobs():
        prompt_node["nb_read_node"].setValue(player_node.name())
    else:
        read_ref_knob = nuke.String_Knob("nb_read_node", "Read Node")
        read_ref_knob.setValue(player_node.name())
        prompt_node.addKnob(read_ref_knob)

    if "nb_output_path" in prompt_node.knobs():
        prompt_node["nb_output_path"].setValue(new_image_path.replace("\\", "/"))

    # Player's input 0 connects to prompt node (Prompt → Player direction)
    player_node.setInput(0, prompt_node)

    print("NanoBanana: Created new NB Player '{}' for regenerated image: {}".format(
        player_node.name(), new_image_path))
    return player_node


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
        # Image generation models with multi-image support
        self.model_combo.addItem("Gemini 3.1 Flash - Nano Banana 2", "gemini-3.1-flash-image-preview")
        self.model_combo.addItem("Gemini 3 Pro - Nano Banana Pro", "gemini-3-pro-image-preview")
        self.model_combo.addItem("Gemini 2.5 Flash - Nano Banana", "gemini-2.5-flash-image")
        # Additional models
        self.model_combo.addItem("Gemini 2.0 Flash Exp (Image Gen)", "gemini-2.0-flash-exp-image-generation")
        self.model_combo.addItem("Imagen 3.0 Generate", "imagen-3.0-generate-002")
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        self.model_combo.currentIndexChanged.connect(lambda _: self._save_all_state_to_node())
        main.addWidget(self.model_combo)

        # === Ratio + Resolution ===
        row2 = QtWidgets.QHBoxLayout()
        row2.setSpacing(6)

        self.ratio_combo = DropDownComboBox()
        self.ratio_combo.addItems(["Auto", "1:1", "16:9", "9:16", "4:3", "3:4", "21:9", "4:5"])
        self.ratio_combo.currentIndexChanged.connect(lambda _: self._save_all_state_to_node())

        self.res_combo = DropDownComboBox()
        self.res_combo.addItem("1K", "1K")
        self.res_combo.addItem("2K", "2K")
        self.res_combo.addItem("4K", "4K")
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
        self.model_combo.addItem("Gemini 3.1 Flash - Nano Banana 2", "gemini-3.1-flash-image-preview")
        self.model_combo.addItem("Gemini 3 Pro - Nano Banana Pro", "gemini-3-pro-image-preview")
        self.model_combo.addItem("Gemini 2.5 Flash - Nano Banana", "gemini-2.5-flash-image")
        self.model_combo.addItem("Gemini 2.0 Flash Exp (Image Gen)", "gemini-2.0-flash-exp-image-generation")
        self.model_combo.addItem("Imagen 3.0 Generate", "imagen-3.0-generate-002")
        main.addWidget(self.model_combo)

        # === Ratio + Resolution ===
        row2 = QtWidgets.QHBoxLayout()
        row2.setSpacing(6)

        self.ratio_combo = DropDownComboBox()
        self.ratio_combo.addItems(["Auto", "1:1", "16:9", "9:16", "4:3", "3:4", "21:9", "4:5"])

        self.res_combo = DropDownComboBox()
        self.res_combo.addItem("1K", "1K")
        self.res_combo.addItem("2K", "2K")
        self.res_combo.addItem("4K", "4K")

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
def _create_group_inputs(group_node, count):
    """Create Input nodes inside a Group.

    TWO mechanisms work together for user's desired layout:

      DAG display:  img1(LEFT)  img2  ...  imgN(RIGHT)

    1) CREATION ORDER controls DAG visual position:
       Nuke: first-created Input = RIGHTmost, each new one goes LEFT.
       So we CREATE in REVERSE order (imgN first -> right, img1 last -> left).

    2) 'number' KNOB controls input index (connection):
       Nuke: higher number = MORE LEFT in DAG.
       We want img1 on the LEFT, so img1 gets the HIGHEST number.
       Mapping:  imgK -> number = (count - K)

    Summary table (for count=3):
      Name   | Created   | number | input idx | DAG pos
      ------ | --------- | ------ | --------- | --------
      img3   | 1st (→right) | 0     | 0         | RIGHTMOST
      img2   | 2nd         | 1     | 1         | middle
      img1   | 3rd (→left) | 2     | 2         | LEFTMOST
    """
    group_node.begin()
    # Reverse creation order: imgN first (rightmost), img1 last (leftmost)
    for i in range(count, 0, -1):
        inp = nuke.nodes.Input()
        inp.setName("img{}".format(i))
        # imgK gets number (count-K): img1=highest=leftmost, imgN=0=rightmost
        inp["number"].setValue(count - i)
        # xpos: img1 at left (0), imgN at right ((count-1)*100)
        inp["xpos"].setValue((i - 1) * 100)
        inp["ypos"].setValue(0)
        print("[NanoBanana] _create: '{}' number={} created_#{}"
              .format(inp.name(), int(inp["number"].value()),
                      count - i + 1))
    
    out = nuke.nodes.Output()
    out["xpos"].setValue(0)
    out["ypos"].setValue(200)
    group_node.end()
    
    # Debug verify
    for idx in range(count):
        print("[NanoBanana] _create: verify input({}) -> {}"
              .format(idx, group_node.input(idx)))


def create_nanobanana_node():
    """Create a NanoBanana_Generate node with multiple inputs using Group.

    - No nodes selected  -> start with 1 input  (img1 only)
    - 1+ nodes selected  -> start with 1 input (img1), auto-connect selected node to img1

    Auto-expands up to MAX_INPUT_IMAGES when all inputs are connected.

    DAG layout:       img1(LEFT)  img2  ...  imgN(RIGHT)
    Input mapping:     imgK = input index (count - K)
                       img1 = input(count-1) [leftmost, highest number]
    """
    sel = nuke.selectedNodes()
    sel_node = sel[0] if sel else None

    # Always start with 1 input; if node selected, connect it to img1 after creation
    initial_inputs = 1

    group_node = nuke.nodes.Group(name=_next_node_name("NanoBanana"))
    group_node["tile_color"].setValue(0x2E2E2EFF)

    # Position below selected node, or at DAG viewport center
    if sel_node:
        sx = int(sel_node["xpos"].value())
        sy = int(sel_node["ypos"].value())
        group_node["xpos"].setValue(sx)
        group_node["ypos"].setValue(sy + 100)
        print("[NanoBanana] create: positioned under '{}'".format(sel_node.name()))
    else:
        try:
            center = nuke.center()
            x, y = int(center[0]), int(center[1])
        except Exception:
            x, y = 0, 0
        group_node["xpos"].setValue(x)
        group_node["ypos"].setValue(y)
        print("[NanoBanana] create: positioned at DAG viewport center ({}, {})"
              .format(x, y))

    # Build internal Input / Output nodes (reverse creation + explicit number)
    _create_group_inputs(group_node, initial_inputs)

    # Auto-connect selected node to img1 (input index = count - 1 = 0 for count=1)
    if sel_node:
        img1_idx = initial_inputs - 1  # img1 maps to input(0) when count=1
        group_node.setInput(img1_idx, sel_node)
        print("[NanoBanana] create: connected '{}' -> img1 (input{})"
              .format(sel_node.name(), img1_idx))

    # Add our custom NanoBanana tab FIRST (so no "User" tab is auto-created)
    tab = nuke.Tab_Knob("nanobanana_tab", "NanoBanana")
    group_node.addKnob(tab)

    custom_knob = nuke.PyCustom_Knob(
        "nanobanana_ui", "",
        "ai_workflow.nanobanana.NanoBananaKnobWidget()"
    )
    custom_knob.setFlag(nuke.STARTLINE)
    group_node.addKnob(custom_knob)

    # Track current input count for auto-expansion (hidden, under NanoBanana tab)
    count_knob = nuke.Int_Knob("nb_input_count", "Input Count")
    count_knob.setValue(initial_inputs)
    count_knob.setVisible(False)
    group_node.addKnob(count_knob)

    # Store per-model max inputs (default model is first in combo: gemini-3.1-flash)
    max_knob = nuke.Int_Knob("nb_max_inputs", "Max Inputs")
    max_knob.setValue(MODEL_MAX_INPUTS.get("gemini-3.1-flash-image-preview", MAX_INPUT_IMAGES))
    max_knob.setVisible(False)
    group_node.addKnob(max_knob)

    return group_node


_expanding_inputs = False  # Guard against recursive knobChanged callbacks


def _nanobanana_input_changed():
    """Callback: auto-expand Group inputs when all current inputs are connected.

    Target DAG:  img1(LEFT)  ...  imgN(RIGHT)
    Mapping:     imgK = input index (new_count - K)

    Steps:
      1. Save connections: imgK -> node.input(current_count - K)
      2. Delete ALL Input nodes
      3. Recreate in reverse order with number knobs:
           img(N+1) first (rightmost), ..., img1 last (leftmost)
           each gets number = new_count - K
      4. Restore connections via new mapping
    """
    global _expanding_inputs
    if _expanding_inputs:
        return

    node = nuke.thisNode()
    if not _is_generator_node(node):
        return
    if node.Class() != "Group":
        return
    if "nb_input_count" not in node.knobs():
        return
    
    # Use per-model max if available, otherwise global max
    if "nb_max_inputs" in node.knobs():
        max_allowed = int(node["nb_max_inputs"].value())
    else:
        max_allowed = MAX_INPUT_IMAGES

    current_count = int(node["nb_input_count"].value())
    if current_count >= max_allowed:
        return
    
    # Debug log
    print("[NanoBanana] _input_changed: '{}' count={}/max={}"
          .format(node.name(), current_count, max_allowed))
    for i in range(current_count):
        conn = node.input(i)
        print("[NanoBanana]   input({}) <- {}"
              .format(i, conn.name() if conn else "None"))

    # Check if all connected
    all_connected = True
    for i in range(current_count):
        if node.input(i) is None:
            all_connected = False
            break
    
    if all_connected:
        _expanding_inputs = True
        try:
            new_count = current_count + 1
            print("[NanoBanana]   EXPAND {} -> {}".format(current_count, new_count))

            # --- Step 1: Save connections ---
            # Old mapping: imgK = input (current_count - K)
            saved = {}
            for k in range(1, current_count + 1):
                old_idx = current_count - k
                conn = node.input(old_idx)
                if conn is not None:
                    saved[k] = conn
                    print("[NanoBanana]   save: img{} <- '{}'"
                          .format(k, conn.name()))

            # --- Step 2: Delete all Inputs ---
            node.begin()
            del_names = [n.name() for n in nuke.allNodes("Input")]
            for inp in list(nuke.allNodes("Input")):
                nuke.delete(inp)
            node.end()
            print("[NanoBanana]   delete: {}".format(del_names))

            # --- Step 3: Recreate reverse order + number knob ---
            # imgK -> number = (new_count - K): img1=highest=leftmost
            node.begin()
            for i in range(new_count, 0, -1):
                inp = nuke.nodes.Input()
                inp.setName("img{}".format(i))
                inp["number"].setValue(new_count - i)
                inp["xpos"].setValue((i - 1) * 100)
                inp["ypos"].setValue(0)
                print("[NanoBanana]   create: '{}' num={} #{}"
                      .format(inp.name(), int(inp["number"].value()),
                              new_count - i + 1))
            node.end()

            # --- Step 4: Restore ---
            # New mapping: imgK = input (new_count - K)
            # Track which indices we intentionally set
            set_indices = set()
            for k, conn_node in saved.items():
                new_idx = new_count - k
                node.setInput(new_idx, conn_node)
                set_indices.add(new_idx)
                print("[NanoBanana]   restore: img{} <- input {} ('{}')"
                      .format(k, new_idx, conn_node.name()))

            # --- Step 5: Clear auto-filled inputs ---
            # Nuke's setInput(N, node) auto-fills indices 0..N-1 with same node.
            # Clear any index that we didn't explicitly set above.
            for i in range(new_count):
                if i not in set_indices:
                    print("[NanoBanana]   clear auto-fill: input({})".format(i))
                    node.setInput(i, None)

            node["nb_input_count"].setValue(new_count)

            # Verify
            print("[NanoBanana]   AFTER ({} inputs):".format(new_count))
            for i in range(new_count):
                c = node.input(i)
                print("[NanoBanana]     input({}) <- {}".format(
                    i, c.name() if c else "None"))
            node.begin()
            for inp in nuke.allNodes("Input"):
                print("[NanoBanana]     internal '{}' number={}"
                      .format(inp.name(), int(inp["number"].value())))
            node.end()
        finally:
            _expanding_inputs = False






# Register the callback for auto-expanding inputs
nuke.addKnobChanged(_nanobanana_input_changed, nodeClass="Group")


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
        self.model_combo.addItem("Gemini 3.1 Flash", "gemini-3.1-flash-image-preview")
        self.model_combo.addItem("Gemini 3 Pro", "gemini-3-pro-image-preview")
        self.model_combo.addItem("Gemini 2.5 Flash", "gemini-2.5-flash-image")
        self.model_combo.addItem("Imagen 3.0", "imagen-3.0-generate-002")
        main.addWidget(self.model_combo)

        row2 = QtWidgets.QHBoxLayout()
        row2.setSpacing(6)
        self.ratio_combo = DropDownComboBox()
        self.ratio_combo.addItems(["Auto", "1:1", "16:9", "9:16", "4:3", "3:4"])
        self.res_combo = DropDownComboBox()
        self.res_combo.addItem("1K", "1K")
        self.res_combo.addItem("2K", "2K")
        self.res_combo.addItem("4K", "4K")
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
