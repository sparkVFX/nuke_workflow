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
    
    def _update_effective_path(self):
        custom = self.temp_dir_input.text().strip()
        if custom:
            effective = custom
        else:
            effective = os.path.join(tempfile.gettempdir(), DEFAULT_TEMP_DIR_NAME)
        self.effective_path_label.setText("Effective path: {}".format(effective))
    
    def _save_settings(self):
        self.settings.api_key = self.api_key_input.text().strip()
        self.settings.temp_directory = self.temp_dir_input.text().strip()
        self.accept()


# ---------------------------------------------------------------------------
# Utility functions for handling input images
# ---------------------------------------------------------------------------
def get_temp_directory():
    """Get the temporary directory for storing rendered images."""
    settings = NanoBananaSettings()
    temp_dir = settings.temp_directory
    if not os.path.exists(temp_dir):
        os.makedirs(temp_dir)
    return temp_dir


def get_input_directory():
    """Get the input subdirectory inside temp directory."""
    input_dir = os.path.join(get_temp_directory(), "input")
    if not os.path.exists(input_dir):
        os.makedirs(input_dir)
    return input_dir


def get_output_directory():
    """Get the output subdirectory inside temp directory."""
    output_dir = os.path.join(get_temp_directory(), "output")
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    return output_dir


def get_nanobanana_node():
    """Try to find the NanoBanana_Generate node."""
    # First check if there's a selected node
    selected = nuke.selectedNodes()
    for node in selected:
        if node.name().startswith("NanoBanana_Generate"):
            return node
    
    # Search all nodes for NanoBanana_Generate
    for node in nuke.allNodes():
        if node.name().startswith("NanoBanana_Generate"):
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
        write["channels"].setValue("rgba")
        
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


def create_nb_player_node(image_path=None, name=None, xpos=None, ypos=None):
    """
    Create a NanoBanana Player Group node wrapping a Read node.
    Similar to VEO Player but for single images (no frame range knobs).
    
    Returns:
        tuple: (group_node, internal_read_node)
    """
    group = nuke.nodes.Group()

    if name:
        group.setName(name)
    else:
        group.setName("NB_Player1")
    if xpos is not None:
        group["xpos"].setValue(int(xpos))
    if ypos is not None:
        group["ypos"].setValue(int(ypos))

    # Green colour (same as VEO Player)
    group["tile_color"].setValue(0x00C878FF)
    group["label"].setValue("NB Player")

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
    # Tab: Read
    tab_read = nuke.Tab_Knob("read_tab", "Read")
    group.addKnob(tab_read)

    read_full = read_node.fullName()

    # --- file knob ---
    file_knob = nuke.File_Knob("nb_file", "file")
    if image_path:
        file_knob.setValue(image_path.replace("\\", "/"))
    group.addKnob(file_knob)

    # --- format ---
    try:
        link_format = nuke.Link_Knob("format")
        link_format.makeLink(read_full, "format")
        link_format.setLabel("format")
        group.addKnob(link_format)
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

    # --- Button to open internal Read node's full properties ---
    open_read_script = "n = nuke.thisNode()\nn.begin()\nr = nuke.toNode('InternalRead')\nn.end()\nif r: nuke.show(r)"
    open_btn = nuke.PyScript_Knob("open_read_props", "Open Full Read Properties", open_read_script)
    open_btn.setFlag(nuke.STARTLINE)
    group.addKnob(open_btn)

    # --- knobChanged callback to sync nb_file → internal Read's file ---
    kc_script = (
        "n = nuke.thisNode()\n"
        "k = nuke.thisKnob()\n"
        "if k.name() == 'nb_file':\n"
        "    n.begin()\n"
        "    r = nuke.toNode('InternalRead')\n"
        "    n.end()\n"
        "    if r:\n"
        "        r['file'].fromUserText(k.value())\n"
    )
    group["knobChanged"].setValue(kc_script)

    # --- Divider + Send to Studio button ---
    studio_divider = nuke.Text_Knob("studio_divider", "")
    group.addKnob(studio_divider)

    studio_btn = nuke.PyScript_Knob("send_to_studio", "Send to Studio", _SEND_TO_STUDIO_SCRIPT)
    studio_btn.setFlag(nuke.STARTLINE)
    group.addKnob(studio_btn)

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

    return group, read_node


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
            if "nb_generator" in node.knobs():
                if node["nb_generator"].value() == gen_name:
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
    
    # Store which generator this prompt belongs to (hidden - shown in custom widget)
    gen_knob = nuke.String_Knob("nb_generator", "Generator")
    gen_knob.setValue(gen_name)
    gen_knob.setFlag(nuke.INVISIBLE)
    prompt_node.addKnob(gen_knob)
    
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
    
    inputs_json_knob = nuke.String_Knob("nb_input_images", "Input Images (JSON)")
    inputs_json_knob.setValue(json.dumps(input_paths))
    inputs_json_knob.setFlag(nuke.INVISIBLE)
    prompt_node.addKnob(inputs_json_knob)
    
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
        player_node, read_node = create_nb_player_node(
            image_path=output_image_path,
            name="生成图像Prompt{}".format(prompt_num),
            xpos=prompt_x + 200,
            ypos=prompt_y,
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
        for h in self.settings.prompt_history:
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
        self.neg_prompt_edit.setPlaceholderText("Negative Prompt (Optional)...")
        self.neg_prompt_edit.setMinimumHeight(60)
        self.neg_prompt_edit.textChanged.connect(self._save_all_state_to_node)
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
        history = self.settings.prompt_history
        # Remove duplicate if exists (will be re-inserted at top)
        if prompt in history:
            history.remove(prompt)
        history.insert(0, prompt)
        if len(history) > 20:
            history = history[:20]
        self.settings.prompt_history = history

        self._refresh_history_combo(history)

    def _clear_history(self):
        self.settings.prompt_history = []
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

        # ---- Direct callbacks (called from Worker.run() in background thread) ----
        # These are NOT Qt signal slots – they are plain Python function calls made
        # directly by the worker thread.  They are immune to Qt signal disconnection
        # that happens when Nuke destroys the PyCustom_Knob widget.

        def _direct_on_finished(path, metadata):
            """Direct callback from worker thread – always fires."""
            # UI updates (safe to skip if widget is gone)
            def _update_ui():
                try:
                    if _isValid(widget_ref):
                        widget_ref._toggle_stop_ui(False)
                        s = metadata.get("seed", "N/A")
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
                        print("NanoBanana: Creating Prompt node with output: {}".format(path))
                        prompt_node, read_node = create_prompt_node(
                            params["generator_node"],
                            params["prompt"],
                            params["neg_prompt"],
                            params["model"],
                            params["ratio"],
                            params["resolution"],
                            params["seed"],
                            path,
                            params["images_info"]
                        )
                        if read_node:
                            try:
                                nuke.connectViewer(0, read_node)
                                print("NanoBanana: Connected viewer to Read node")
                            except Exception as e:
                                print("NanoBanana: Could not connect viewer: {}".format(e))
                        else:
                            print("NanoBanana: WARNING - No Read node was created")
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
        self.neg_prompt_edit.setPlaceholderText("Negative Prompt (Optional)...")
        self.neg_prompt_edit.setMinimumHeight(60)
        main.addWidget(self.neg_prompt_edit)

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

            try:
                if input_images_json and input_images_json.strip():
                    input_paths = json.loads(input_images_json)
                    valid_paths = [p for p in input_paths if p and os.path.exists(p)]
                    if valid_paths:
                        self.cached_info_label.setText("📷 {} cached input image(s)".format(len(valid_paths)))
                    else:
                        self.cached_info_label.setText("Text-only generation")
                else:
                    self.cached_info_label.setText("Text-only generation")
            except:
                self.cached_info_label.setText("")

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

        except Exception as e:
            print("NanoBanana: Error loading node settings: {}".format(str(e)))

    def _regenerate(self):
        """Regenerate using the editable parameters and cached input images."""
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

        # Collect cached input images from node
        images_info = []
        if "nb_input_images" in self.node.knobs():
            try:
                input_paths = json.loads(self.node["nb_input_images"].value())
                for idx, p in enumerate(input_paths):
                    if p and os.path.exists(p):
                        images_info.append({
                            "index": idx,
                            "name": "img{}".format(idx + 1),
                            "path": p,
                            "connected": True,
                            "node_name": "cached"
                        })
            except:
                pass

        self.status_label.setStyleSheet("color: #facc15; font-size: 11px;")
        self.status_label.setText("Regenerating (Seed: {})...".format(seed))
        self._toggle_ui(True)

        output_dir = get_output_directory()

        # Find generator name
        gen_name = "nanobanana"
        if "nb_generator" in self.node.knobs():
            gen_name = self.node["nb_generator"].value() or gen_name

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
                log_path = os.path.join(self.temp_dir, log_filename)
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

    group_node = nuke.nodes.Group()
    group_node.setName("NanoBanana_Generate")
    group_node["tile_color"].setValue(0x3CB371FF)  # Green

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
    if not node.name().startswith("NanoBanana_Generate"):
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
