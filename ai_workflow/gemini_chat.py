"""
Gemini Dialogue Panel for Nuke.
Opens a floating panel (not a node) with:
- Session management (New / history dropdown)
- Chat display (assistant left, user right)
- Image attachment (Select file / Paste clipboard)
- Model selection
- Text input + Send button

Uses the same shared API key from NanoBananaSettings.
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
import uuid
import base64
import datetime
import threading
import traceback

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CHAT_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
]

SESSIONS_DIR_NAME = "gemini_chat_sessions"

# ---------------------------------------------------------------------------
# Style
# ---------------------------------------------------------------------------
GEMINI_CHAT_STYLE = """
QWidget#geminiChatRoot {
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
    selection-background-color: #4f87f7;
    selection-color: #ffffff;
}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {
    border: 1px solid #4f87f7;
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
    selection-background-color: #4f87f7;
    selection-color: #ffffff;
}
QPushButton#sendBtn {
    background-color: #4f87f7;
    color: #ffffff;
    border: none;
    border-radius: 4px;
    padding: 10px 20px;
    font-weight: bold;
    font-size: 13px;
}
QPushButton#sendBtn:hover {
    background-color: #6a9fff;
}
QPushButton#sendBtn:pressed {
    background-color: #3a6fd8;
}
QPushButton#sendBtn:disabled {
    background-color: #555555;
    color: #888888;
}
QPushButton#actionBtn {
    background-color: #404040;
    color: #e0e0e0;
    border: 1px solid #555555;
    padding: 5px 10px;
    font-size: 11px;
    border-radius: 3px;
}
QPushButton#actionBtn:hover {
    background-color: #505050;
    border-color: #777777;
}
QPushButton#newDialogueBtn {
    background-color: #2d8a4e;
    color: #ffffff;
    border: none;
    border-radius: 4px;
    padding: 5px 12px;
    font-weight: bold;
    font-size: 11px;
}
QPushButton#newDialogueBtn:hover {
    background-color: #38a169;
}
QPushButton#deleteBtn {
    background-color: #ef4444;
    color: #ffffff;
    border: none;
    border-radius: 3px;
    padding: 5px 8px;
    font-size: 11px;
}
QPushButton#deleteBtn:hover {
    background-color: #dc2626;
}
QPushButton#copyBtn {
    background-color: transparent;
    color: #999999;
    border: 1px solid #555555;
    border-radius: 3px;
    padding: 2px 8px;
    font-size: 10px;
}
QPushButton#copyBtn:hover {
    background-color: #404040;
    color: #ffffff;
    border-color: #777777;
}
QScrollArea {
    border: none;
    background-color: #1a1a1a;
}
QScrollBar:vertical {
    background-color: #1a1a1a;
    width: 8px;
    border-radius: 4px;
}
QScrollBar::handle:vertical {
    background-color: #444444;
    border-radius: 4px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background-color: #555555;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}
"""


# ---------------------------------------------------------------------------
# Session Manager  — persists chat history to disk
# ---------------------------------------------------------------------------
class SessionManager:
    """Manages chat sessions stored as JSON files."""

    def __init__(self):
        self._sessions_dir = os.path.join(
            os.path.expanduser("~"), ".nuke", SESSIONS_DIR_NAME
        )
        if not os.path.isdir(self._sessions_dir):
            os.makedirs(self._sessions_dir, exist_ok=True)

    # --- list / load / save / delete -------------------------------------------

    def list_sessions(self):
        """Return list of (session_id, title, modified_time) sorted newest first."""
        sessions = []
        for fname in os.listdir(self._sessions_dir):
            if fname.endswith(".json"):
                fpath = os.path.join(self._sessions_dir, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    sessions.append((
                        data.get("id", fname[:-5]),
                        data.get("title", "Untitled"),
                        os.path.getmtime(fpath),
                    ))
                except Exception:
                    pass
        sessions.sort(key=lambda x: x[2], reverse=True)
        return sessions

    def load_session(self, session_id):
        fpath = os.path.join(self._sessions_dir, "{}.json".format(session_id))
        if os.path.isfile(fpath):
            with open(fpath, "r", encoding="utf-8") as f:
                return json.load(f)
        return None

    def save_session(self, session_data):
        sid = session_data.get("id", str(uuid.uuid4()))
        session_data["id"] = sid
        fpath = os.path.join(self._sessions_dir, "{}.json".format(sid))
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(session_data, f, ensure_ascii=False, indent=2)
        return sid

    def delete_session(self, session_id):
        fpath = os.path.join(self._sessions_dir, "{}.json".format(session_id))
        if os.path.isfile(fpath):
            os.remove(fpath)

    def new_session(self):
        sid = str(uuid.uuid4())
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        data = {
            "id": sid,
            "title": "Chat {}".format(ts),
            "model": CHAT_MODELS[0],
            "messages": [],
        }
        self.save_session(data)
        return data


# ---------------------------------------------------------------------------
# Chat Bubble Widget
# ---------------------------------------------------------------------------
class ChatBubble(QtWidgets.QFrame):
    """A single chat message bubble."""

    def __init__(self, role, text, images=None, parent=None):
        super(ChatBubble, self).__init__(parent)
        self.role = role
        is_user = (role == "user")

        self.setStyleSheet(
            "QFrame {{ background-color: {}; border-radius: 8px; padding: 8px; }}".format(
                "#2a3a5c" if is_user else "#333333"
            )
        )

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(4)

        # Role label
        role_label = QtWidgets.QLabel("You" if is_user else "Gemini")
        role_label.setStyleSheet(
            "color: {}; font-size: 10px; font-weight: bold;".format(
                "#6a9fff" if is_user else "#66bb6a"
            )
        )
        layout.addWidget(role_label)

        # Show attached images (thumbnails)
        if images:
            img_row = QtWidgets.QHBoxLayout()
            img_row.setSpacing(4)
            for img_path in images:
                if os.path.isfile(img_path):
                    thumb = QtWidgets.QLabel()
                    pix = QtGui.QPixmap(img_path)
                    if not pix.isNull():
                        pix = pix.scaled(80, 80, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
                        thumb.setPixmap(pix)
                        thumb.setFixedSize(pix.width(), pix.height())
                        img_row.addWidget(thumb)
            img_row.addStretch()
            layout.addLayout(img_row)

        # Message text
        self.msg_label = QtWidgets.QLabel(text)
        self.msg_label.setWordWrap(True)
        self.msg_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.msg_label.setStyleSheet("color: #eeeeee; font-size: 12px; background: transparent;")
        layout.addWidget(self.msg_label)

        # Copy button (only for model/assistant replies)
        if not is_user:
            copy_row = QtWidgets.QHBoxLayout()
            copy_row.setContentsMargins(0, 2, 0, 0)
            copy_row.addStretch()
            self._copy_btn = QtWidgets.QPushButton("📋 Copy")
            self._copy_btn.setObjectName("copyBtn")
            self._copy_btn.setToolTip("Copy this response to clipboard")
            self._copy_btn.setCursor(QtCore.Qt.PointingHandCursor)
            self._copy_btn.clicked.connect(self._copy_text)
            copy_row.addWidget(self._copy_btn)
            layout.addLayout(copy_row)

    def _copy_text(self):
        """Copy the message text to clipboard."""
        text = self.msg_label.text()
        clipboard = QtWidgets.QApplication.clipboard()
        clipboard.setText(text)
        # Brief visual feedback
        if hasattr(self, "_copy_btn") and _isValid(self._copy_btn):
            self._copy_btn.setText("✓ Copied")
            QtCore.QTimer.singleShot(1500, lambda: (
                self._copy_btn.setText("📋 Copy") if _isValid(self._copy_btn) else None
            ))

    def set_text(self, text):
        """Update the displayed message text (used for streaming)."""
        self.msg_label.setText(text)


# ---------------------------------------------------------------------------
# Image Thumbnail Strip
# ---------------------------------------------------------------------------
class ImageStrip(QtWidgets.QWidget):
    """Shows thumbnails of attached images with remove buttons."""

    imagesChanged = QtCore.Signal()

    def __init__(self, parent=None):
        super(ImageStrip, self).__init__(parent)
        self._images = []  # list of file paths
        self._layout = QtWidgets.QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(4)
        self._layout.addStretch()
        self.setFixedHeight(0)  # hidden initially

    @property
    def images(self):
        return list(self._images)

    def add_image(self, path):
        if path and os.path.isfile(path) and path not in self._images:
            self._images.append(path)
            self._rebuild()
            self.imagesChanged.emit()

    def clear_images(self):
        self._images.clear()
        self._rebuild()
        self.imagesChanged.emit()

    def _rebuild(self):
        # Clear layout
        while self._layout.count():
            item = self._layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        for img_path in self._images:
            frame = QtWidgets.QFrame()
            frame.setStyleSheet("QFrame { background: #333; border-radius: 4px; }")
            fl = QtWidgets.QVBoxLayout(frame)
            fl.setContentsMargins(2, 2, 2, 2)
            fl.setSpacing(1)

            thumb = QtWidgets.QLabel()
            pix = QtGui.QPixmap(img_path)
            if not pix.isNull():
                pix = pix.scaled(60, 60, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
                thumb.setPixmap(pix)
            thumb.setFixedSize(60, 60)
            thumb.setAlignment(QtCore.Qt.AlignCenter)
            fl.addWidget(thumb)

            remove_btn = QtWidgets.QPushButton("✕")
            remove_btn.setFixedSize(18, 18)
            remove_btn.setStyleSheet(
                "QPushButton { background: #ef4444; color: white; border: none; "
                "border-radius: 9px; font-size: 10px; font-weight: bold; }"
                "QPushButton:hover { background: #dc2626; }"
            )
            remove_btn.clicked.connect(lambda checked=False, p=img_path: self._remove(p))
            fl.addWidget(remove_btn, alignment=QtCore.Qt.AlignCenter)

            self._layout.insertWidget(self._layout.count() - 1, frame)

        self._layout.addStretch()
        h = 90 if self._images else 0
        self.setFixedHeight(h)

    def _remove(self, path):
        if path in self._images:
            self._images.remove(path)
            self._rebuild()
            self.imagesChanged.emit()


# ---------------------------------------------------------------------------
# Main Chat Panel
# ---------------------------------------------------------------------------
class GeminiChatPanel(QtWidgets.QWidget):
    """The main Gemini Dialogue panel."""

    def __init__(self, parent=None):
        super(GeminiChatPanel, self).__init__(parent)
        self.setObjectName("geminiChatRoot")
        self.setStyleSheet(GEMINI_CHAT_STYLE)

        self._session_mgr = SessionManager()
        self._current_session = None  # dict
        self._is_sending = False

        self._build_ui()
        self._refresh_session_list()

        # Auto-load last session or create new
        sessions = self._session_mgr.list_sessions()
        if sessions:
            self._load_session(sessions[0][0])
        else:
            self._new_dialogue()

    # ---- Build UI ----------------------------------------------------------

    def _build_ui(self):
        root_layout = QtWidgets.QVBoxLayout(self)
        root_layout.setContentsMargins(8, 8, 8, 8)
        root_layout.setSpacing(6)

        # ---- Top bar: session management ----
        top_bar = QtWidgets.QHBoxLayout()
        top_bar.setSpacing(6)

        new_btn = QtWidgets.QPushButton("New Dialogue")
        new_btn.setObjectName("newDialogueBtn")
        new_btn.clicked.connect(self._new_dialogue)
        top_bar.addWidget(new_btn)

        self._session_combo = QtWidgets.QComboBox()
        self._session_combo.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self._session_combo.currentIndexChanged.connect(self._on_session_selected)
        top_bar.addWidget(self._session_combo)

        del_btn = QtWidgets.QPushButton("🗑")
        del_btn.setObjectName("deleteBtn")
        del_btn.setFixedWidth(32)
        del_btn.setToolTip("Delete current session")
        del_btn.clicked.connect(self._delete_current_session)
        top_bar.addWidget(del_btn)

        root_layout.addLayout(top_bar)

        # ---- Chat area ----
        self._scroll_area = QtWidgets.QScrollArea()
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)

        self._chat_container = QtWidgets.QWidget()
        self._chat_layout = QtWidgets.QVBoxLayout(self._chat_container)
        self._chat_layout.setContentsMargins(4, 4, 4, 4)
        self._chat_layout.setSpacing(8)
        self._chat_layout.addStretch()

        self._scroll_area.setWidget(self._chat_container)
        root_layout.addWidget(self._scroll_area, 1)

        # ---- Input section ----
        input_section = QtWidgets.QVBoxLayout()
        input_section.setSpacing(4)

        # Image attach row
        img_btn_row = QtWidgets.QHBoxLayout()
        img_btn_row.setSpacing(6)

        input_label = QtWidgets.QLabel("Input")
        input_label.setStyleSheet("color: #aaa; font-size: 11px; font-weight: bold;")
        img_btn_row.addWidget(input_label)

        select_btn = QtWidgets.QPushButton("Select")
        select_btn.setObjectName("actionBtn")
        select_btn.setToolTip("Select image file from disk")
        select_btn.clicked.connect(self._select_image)
        img_btn_row.addWidget(select_btn)

        paste_btn = QtWidgets.QPushButton("Paste")
        paste_btn.setObjectName("actionBtn")
        paste_btn.setToolTip("Paste image from clipboard")
        paste_btn.clicked.connect(self._paste_image)
        img_btn_row.addWidget(paste_btn)

        img_btn_row.addStretch()
        input_section.addLayout(img_btn_row)

        # Image strip (thumbnails)
        self._image_strip = ImageStrip()
        input_section.addWidget(self._image_strip)

        # Model selection
        model_row = QtWidgets.QHBoxLayout()
        model_row.setSpacing(6)
        model_label = QtWidgets.QLabel("Model")
        model_label.setStyleSheet("color: #aaa; font-size: 11px;")
        model_label.setFixedWidth(40)
        model_row.addWidget(model_label)

        self._model_combo = QtWidgets.QComboBox()
        for m in CHAT_MODELS:
            self._model_combo.addItem(m)
        self._model_combo.currentIndexChanged.connect(self._on_model_changed)
        model_row.addWidget(self._model_combo)
        input_section.addLayout(model_row)

        # Text input
        self._text_input = QtWidgets.QPlainTextEdit()
        self._text_input.setPlaceholderText("Type your message here...")
        self._text_input.setFixedHeight(80)
        self._text_input.installEventFilter(self)
        input_section.addWidget(self._text_input)

        # Send button
        self._send_btn = QtWidgets.QPushButton("Send")
        self._send_btn.setObjectName("sendBtn")
        self._send_btn.clicked.connect(self._send_message)
        input_section.addWidget(self._send_btn)

        # Status label
        self._status_label = QtWidgets.QLabel("")
        self._status_label.setStyleSheet("color: #888; font-size: 10px;")
        self._status_label.setWordWrap(True)
        input_section.addWidget(self._status_label)

        root_layout.addLayout(input_section)

    # ---- Event filter (Ctrl+Enter to send) ---------------------------------

    def eventFilter(self, obj, event):
        if obj is self._text_input and event.type() == QtCore.QEvent.KeyPress:
            if event.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
                if event.modifiers() & QtCore.Qt.ControlModifier:
                    self._send_message()
                    return True
        return super(GeminiChatPanel, self).eventFilter(obj, event)

    # ---- Session management ------------------------------------------------

    def _refresh_session_list(self):
        self._session_combo.blockSignals(True)
        self._session_combo.clear()
        sessions = self._session_mgr.list_sessions()
        for sid, title, _ in sessions:
            self._session_combo.addItem(title, sid)
        self._session_combo.blockSignals(False)

    def _new_dialogue(self):
        data = self._session_mgr.new_session()
        self._current_session = data
        self._refresh_session_list()
        # Select newly created session
        idx = self._session_combo.findData(data["id"])
        if idx >= 0:
            self._session_combo.blockSignals(True)
            self._session_combo.setCurrentIndex(idx)
            self._session_combo.blockSignals(False)
        self._rebuild_chat_display()
        self._sync_model_combo()
        self._image_strip.clear_images()
        self._text_input.clear()
        self._status_label.setText("")

    def _on_session_selected(self, idx):
        sid = self._session_combo.itemData(idx)
        if sid:
            self._load_session(sid)

    def _load_session(self, session_id):
        data = self._session_mgr.load_session(session_id)
        if data is None:
            return
        self._current_session = data
        idx = self._session_combo.findData(session_id)
        if idx >= 0:
            self._session_combo.blockSignals(True)
            self._session_combo.setCurrentIndex(idx)
            self._session_combo.blockSignals(False)
        self._rebuild_chat_display()
        self._sync_model_combo()
        self._image_strip.clear_images()
        self._status_label.setText("")

    def _delete_current_session(self):
        if self._current_session is None:
            return
        sid = self._current_session["id"]
        self._session_mgr.delete_session(sid)
        self._current_session = None
        self._refresh_session_list()
        sessions = self._session_mgr.list_sessions()
        if sessions:
            self._load_session(sessions[0][0])
        else:
            self._new_dialogue()

    def _sync_model_combo(self):
        if self._current_session:
            model = self._current_session.get("model", CHAT_MODELS[0])
            idx = self._model_combo.findText(model)
            if idx >= 0:
                self._model_combo.blockSignals(True)
                self._model_combo.setCurrentIndex(idx)
                self._model_combo.blockSignals(False)

    def _on_model_changed(self, idx):
        if self._current_session:
            self._current_session["model"] = self._model_combo.currentText()
            self._session_mgr.save_session(self._current_session)

    # ---- Chat display ------------------------------------------------------

    def _rebuild_chat_display(self):
        """Rebuild chat bubbles from current session."""
        # Remove all existing bubbles
        while self._chat_layout.count() > 1:
            item = self._chat_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        if not self._current_session:
            return

        for msg in self._current_session.get("messages", []):
            bubble = ChatBubble(
                msg["role"],
                msg["text"],
                images=msg.get("images"),
            )
            # Insert before the stretch at the end
            self._chat_layout.insertWidget(self._chat_layout.count() - 1, bubble)

        # Scroll to bottom
        QtCore.QTimer.singleShot(50, self._scroll_to_bottom)

    def _add_bubble(self, role, text, images=None):
        bubble = ChatBubble(role, text, images=images)
        self._chat_layout.insertWidget(self._chat_layout.count() - 1, bubble)
        QtCore.QTimer.singleShot(50, self._scroll_to_bottom)
        return bubble

    def _scroll_to_bottom(self):
        vbar = self._scroll_area.verticalScrollBar()
        vbar.setValue(vbar.maximum())

    # ---- Image attachment --------------------------------------------------

    def _select_image(self):
        fpath, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Select Image",
            "",
            "Images (*.png *.jpg *.jpeg *.bmp *.gif *.webp);;All Files (*)",
        )
        if fpath:
            self._image_strip.add_image(fpath)

    def _paste_image(self):
        """Paste image from clipboard, save to temp and add."""
        clipboard = QtWidgets.QApplication.clipboard()
        mime = clipboard.mimeData()
        if mime and mime.hasImage():
            image = clipboard.image()
            if not image.isNull():
                # Save to temp
                from ai_workflow.nanobanana import NanoBananaSettings
                settings = NanoBananaSettings()
                temp_dir = settings.temp_directory
                fname = "clipboard_{}.png".format(
                    datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                )
                fpath = os.path.join(temp_dir, fname)
                image.save(fpath, "PNG")
                self._image_strip.add_image(fpath)
                self._status_label.setText("Pasted image from clipboard")
                return
        self._status_label.setText("No image found in clipboard")

    # ---- Send message ------------------------------------------------------

    def _send_message(self):
        if self._is_sending:
            return
        if self._current_session is None:
            self._new_dialogue()

        text = self._text_input.toPlainText().strip()
        attached_images = self._image_strip.images

        if not text and not attached_images:
            return

        # Add user message to session
        user_msg = {"role": "user", "text": text, "images": list(attached_images)}
        self._current_session["messages"].append(user_msg)

        # Auto-update session title from first message
        if len(self._current_session["messages"]) == 1 and text:
            title = text[:40] + ("..." if len(text) > 40 else "")
            self._current_session["title"] = title

        self._session_mgr.save_session(self._current_session)
        self._refresh_session_list()
        # Re-select current session in combo
        idx = self._session_combo.findData(self._current_session["id"])
        if idx >= 0:
            self._session_combo.blockSignals(True)
            self._session_combo.setCurrentIndex(idx)
            self._session_combo.blockSignals(False)

        # Display user bubble
        self._add_bubble("user", text, images=attached_images)

        # Clear input
        self._text_input.clear()
        self._image_strip.clear_images()

        # Show loading
        self._is_sending = True
        self._send_btn.setEnabled(False)
        self._send_btn.setText("Sending...")
        self._status_label.setText("Waiting for Gemini response...")

        # Create an empty assistant bubble for streaming
        self._streaming_bubble = self._add_bubble("model", "▌")
        self._streaming_text = ""

        # Run API call in background thread
        session_data = json.loads(json.dumps(self._current_session))  # deep copy
        threading.Thread(
            target=self._call_gemini_stream_thread,
            args=(session_data,),
            daemon=True,
        ).start()

    def _call_gemini_stream_thread(self, session_data):
        """Runs in background thread — calls Gemini API with streaming."""
        try:
            from google import genai
            from google.genai import types
            from ai_workflow.nanobanana import NanoBananaSettings

            settings = NanoBananaSettings()
            api_key = settings.api_key
            if not api_key:
                self._stream_finish("Error: No API key configured. Please set it in Settings.", error=True)
                return

            client = genai.Client(api_key=api_key)
            model = session_data.get("model", CHAT_MODELS[0])

            # Build contents list from conversation history
            contents = []
            for msg in session_data.get("messages", []):
                parts = []
                # Add images
                for img_path in msg.get("images", []):
                    if os.path.isfile(img_path):
                        try:
                            with open(img_path, "rb") as f:
                                img_data = f.read()
                            # Determine mime type
                            ext = os.path.splitext(img_path)[1].lower()
                            mime_map = {
                                ".png": "image/png",
                                ".jpg": "image/jpeg",
                                ".jpeg": "image/jpeg",
                                ".gif": "image/gif",
                                ".webp": "image/webp",
                                ".bmp": "image/bmp",
                            }
                            mime = mime_map.get(ext, "image/png")
                            parts.append(types.Part.from_bytes(data=img_data, mime_type=mime))
                        except Exception:
                            pass
                # Add text
                if msg.get("text"):
                    parts.append(types.Part.from_text(text=msg["text"]))

                if parts:
                    role = "user" if msg["role"] == "user" else "model"
                    contents.append(types.Content(role=role, parts=parts))

            # Stream API call
            full_text = ""
            for chunk in client.models.generate_content_stream(
                model=model,
                contents=contents,
            ):
                if chunk.text:
                    full_text += chunk.text
                    self._stream_update_chunk(full_text)

            if not full_text:
                full_text = "(No response)"

            self._stream_finish(full_text)

        except Exception as e:
            tb = traceback.format_exc()
            print("[GeminiChat] Error: {}".format(tb))
            self._stream_finish("Error: {}".format(str(e)), error=True)

    def _stream_update_chunk(self, accumulated_text):
        """Thread-safe: update the streaming bubble with accumulated text so far."""
        def _update():
            if not _isValid(self):
                return
            if hasattr(self, "_streaming_bubble") and self._streaming_bubble and _isValid(self._streaming_bubble):
                self._streaming_bubble.set_text(accumulated_text + " ▌")
                self._scroll_to_bottom()
        try:
            nuke.executeInMainThreadWithResult(_update)
        except Exception:
            pass

    def _stream_finish(self, final_text, error=False):
        """Thread-safe: finalize the streaming bubble and save to session."""
        def _update():
            if not _isValid(self):
                return

            # Update the bubble with final text (remove cursor)
            if hasattr(self, "_streaming_bubble") and self._streaming_bubble and _isValid(self._streaming_bubble):
                self._streaming_bubble.set_text(final_text)

            # Save assistant message to session
            if self._current_session is not None:
                assistant_msg = {"role": "model", "text": final_text, "images": []}
                self._current_session["messages"].append(assistant_msg)
                self._session_mgr.save_session(self._current_session)

            self._streaming_bubble = None
            self._streaming_text = ""
            self._is_sending = False
            self._send_btn.setEnabled(True)
            self._send_btn.setText("Send")
            self._status_label.setText(
                "Error occurred" if error else "Response received"
            )
            self._scroll_to_bottom()

        try:
            nuke.executeInMainThreadWithResult(_update)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Nuke Panel Registration — embeds into Nuke's panel system as a tab
# ---------------------------------------------------------------------------

# Store a reference so we can locate it later
_gemini_panel_instance = None


def _create_gemini_panel_widget():
    """Factory function called by Nuke's panel system to create the widget."""
    global _gemini_panel_instance
    _gemini_panel_instance = GeminiChatPanel()
    return _gemini_panel_instance


def register_gemini_panel():
    """Register GeminiChatPanel as a Nuke panel so it can be added as a tab.
    Call this once at startup (e.g. from menu.py or init.py).
    """
    try:
        nukescripts.panels.registerWidgetAsPanel(
            "ai_workflow.gemini_chat._create_gemini_panel_widget",
            "Generate Dialogue Gemini",
            "ai_workflow.GeminiChatPanel",
        )
    except Exception as e:
        print("[GeminiChat] Failed to register panel: {}".format(e))


def open_gemini_chat_panel():
    """Open the Gemini Dialogue panel as a tab inside an existing Nuke pane.
    It will appear next to Properties / Scene Graph etc., not as a floating window.
    """
    panel_id = "ai_workflow.GeminiChatPanel"

    # Create a PythonPanel instance via registerWidgetAsPanel with create=True
    panel = nukescripts.panels.registerWidgetAsPanel(
        "ai_workflow.gemini_chat._create_gemini_panel_widget",
        "Generate Dialogue Gemini",
        panel_id,
        True,  # create=True returns a PythonPanel we can dock
    )

    if panel:
        # Find a suitable pane to dock into (try Properties pane first)
        target_pane = None
        for pane_name in ("Properties.1", "Viewer.1", "DAG.1"):
            target_pane = nuke.getPaneFor(pane_name)
            if target_pane:
                break

        if target_pane:
            panel.addToPane(target_pane)
        else:
            # No pane found — show as floating (addToPane with no args)
            panel.addToPane()
