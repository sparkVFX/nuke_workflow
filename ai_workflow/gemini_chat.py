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
import logging

# ---------------------------------------------------------------------------
# Debug logger for layout / sizing issues
# ---------------------------------------------------------------------------
_log = logging.getLogger("GeminiChat.DEBUG")
_log.setLevel(logging.DEBUG)
if not _log.handlers:
    _handler = logging.StreamHandler()
    _handler.setLevel(logging.DEBUG)
    _handler.setFormatter(logging.Formatter(
        "[%(name)s] %(levelname)s  %(message)s"
    ))
    _log.addHandler(_handler)

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

# Gemini API supported file types and their MIME types
SUPPORTED_MIME_MAP = {
    # Images
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    # PDF
    ".pdf": "application/pdf",
    # Microsoft Office
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    # Plain text / rich text
    ".txt": "text/plain",
    ".rtf": "application/rtf",
    # Structured data
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
}
SUPPORTED_EXTENSIONS = set(SUPPORTED_MIME_MAP.keys())

# Formats that can be sent inline via Part.from_bytes()
# (images, plain text, csv/tsv are small and supported inline)
INLINE_MIME_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".txt", ".csv", ".tsv", ".rtf",
}
# All other supported formats (pdf, office docs) must be uploaded via client.files.upload()

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
# Custom Copy Icon — painted with QPainter (no emoji dependency)
# ---------------------------------------------------------------------------
class _CopyIconWidget(QtWidgets.QWidget):
    """A small widget that paints a 'two overlapping rectangles' copy icon."""

    def __init__(self, size=16, color="#888888", parent=None):
        super(_CopyIconWidget, self).__init__(parent)
        self._color = QtGui.QColor(color)
        self.setFixedSize(size, size)

    def set_color(self, color_str):
        self._color = QtGui.QColor(color_str)
        self.update()

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        pen = QtGui.QPen(self._color, 1.2)
        p.setPen(pen)
        p.setBrush(QtCore.Qt.NoBrush)
        w = self.width()
        h = self.height()
        # Back rectangle (offset top-left)
        m = int(w * 0.15)
        rw = int(w * 0.6)
        rh = int(h * 0.65)
        p.drawRoundedRect(m, m, rw, rh, 2, 2)
        # Front rectangle (offset bottom-right)
        x2 = w - m - rw
        y2 = h - m - rh
        p.drawRoundedRect(x2, y2, rw, rh, 2, 2)
        p.end()


# ---------------------------------------------------------------------------
# Chat Bubble Widget
# ---------------------------------------------------------------------------
class ChatBubble(QtWidgets.QFrame):
    """A single chat message bubble."""

    _MAX_USER_LINES = 3  # User bubble collapsed line limit

    def __init__(self, role, text, images=None, parent=None):
        super(ChatBubble, self).__init__(parent)
        self.role = role
        self._full_text = text
        self._is_collapsed = True
        is_user = (role == "user")

        if is_user:
            # ---- User message: compact bar, right-aligned, max 3 lines ----
            self.setStyleSheet(
                "QFrame { background-color: #2a2a2a; border: none; "
                "border-radius: 16px; padding: 0px; }"
            )
            # Shrink-to-content but cap at a reasonable max
            self.setSizePolicy(QtWidgets.QSizePolicy.Preferred,
                               QtWidgets.QSizePolicy.Preferred)
            _log.debug("[ChatBubble.__init__] user bubble created, text length=%d, "
                       "sizePolicy=Preferred/Preferred", len(text))

            layout = QtWidgets.QHBoxLayout(self)
            layout.setContentsMargins(12, 6, 12, 6)
            layout.setSpacing(6)

            # Right side: text + optional expand toggle (vertical)
            right_col = QtWidgets.QVBoxLayout()
            right_col.setContentsMargins(0, 0, 0, 0)
            right_col.setSpacing(2)

            # Show attached images (thumbnails) inline
            if images:
                img_row = QtWidgets.QHBoxLayout()
                img_row.setSpacing(4)
                for img_path in images:
                    if os.path.isfile(img_path):
                        thumb = QtWidgets.QLabel()
                        pix = QtGui.QPixmap(img_path)
                        if not pix.isNull():
                            pix = pix.scaled(32, 32, QtCore.Qt.KeepAspectRatio,
                                             QtCore.Qt.SmoothTransformation)
                            thumb.setPixmap(pix)
                            thumb.setFixedSize(pix.width(), pix.height())
                            thumb.setStyleSheet("background: transparent;")
                            img_row.addWidget(thumb)
                img_row.addStretch()
                right_col.addLayout(img_row)

            # Message text label
            self.msg_label = QtWidgets.QLabel("")
            self.msg_label.setWordWrap(True)
            self.msg_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            self.msg_label.setStyleSheet(
                "color: #eeeeee; font-size: 12px; background: transparent;"
            )
            right_col.addWidget(self.msg_label)

            # Expand / collapse toggle (▼ / ▲)
            self._toggle_btn = QtWidgets.QPushButton("▼")
            self._toggle_btn.setFixedHeight(16)
            self._toggle_btn.setCursor(QtCore.Qt.PointingHandCursor)
            self._toggle_btn.setStyleSheet(
                "QPushButton { background: transparent; border: none; "
                "color: #888888; font-size: 10px; padding: 0px; }"
                "QPushButton:hover { color: #ffffff; }"
            )
            self._toggle_btn.clicked.connect(self._toggle_expand)
            self._toggle_btn.setVisible(False)
            right_col.addWidget(self._toggle_btn, 0, QtCore.Qt.AlignHCenter)

            layout.addLayout(right_col)

            # Apply collapsed text
            self._apply_collapsed_text(text)

        else:
            # ---- Gemini message: original card style ----
            self.setStyleSheet(
                "QFrame { background-color: #333333; border-radius: 8px; padding: 8px; }"
            )

            layout = QtWidgets.QVBoxLayout(self)
            layout.setContentsMargins(10, 6, 10, 6)
            layout.setSpacing(4)

            # Role label
            role_label = QtWidgets.QLabel("Gemini")
            role_label.setStyleSheet(
                "color: #66bb6a; font-size: 10px; font-weight: bold;"
            )
            layout.addWidget(role_label)

            # Message text
            self.msg_label = QtWidgets.QLabel(text)
            self.msg_label.setWordWrap(True)
            self.msg_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            self.msg_label.setStyleSheet("color: #eeeeee; font-size: 12px; background: transparent;")
            layout.addWidget(self.msg_label)

            # Copy button
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

    # ---- User bubble: collapse / expand helpers ----------------------------

    def _needs_collapse(self, text):
        """Return True if *text* exceeds _MAX_USER_LINES lines."""
        lines = text.split("\n")
        if len(lines) > self._MAX_USER_LINES:
            _log.debug("[_needs_collapse] raw line count %d > %d => True",
                       len(lines), self._MAX_USER_LINES)
            return True
        # Also check if word-wrap causes more than 3 visual lines
        fm = QtGui.QFontMetrics(self.msg_label.font())
        # Prefer msg_label's maximumWidth (set in _insert_bubble_widget),
        # fall back to self.maximumWidth(), then default 500.
        max_w = self.msg_label.maximumWidth()
        _log.debug("[_needs_collapse] msg_label.maximumWidth()=%d, "
                   "self.maximumWidth()=%d", max_w, self.maximumWidth())
        if max_w >= 16777215:  # Qt default QWIDGETSIZE_MAX — means no constraint
            max_w = self.maximumWidth()
        if max_w >= 16777215:
            max_w = 500
        total_lines = 0
        for line in lines:
            if not line:
                total_lines += 1
            else:
                line_w = fm.horizontalAdvance(line) if hasattr(fm, 'horizontalAdvance') else fm.width(line)
                total_lines += max(1, int((line_w + max_w - 1) / max_w))
        _log.debug("[_needs_collapse] computed visual lines=%d, max_w_for_calc=%d => %s",
                   total_lines, max_w, total_lines > self._MAX_USER_LINES)
        return total_lines > self._MAX_USER_LINES

    def _collapsed_text(self, text):
        """Return the first _MAX_USER_LINES logical lines, trimmed."""
        lines = text.split("\n")
        kept = lines[:self._MAX_USER_LINES]
        result = "\n".join(kept)
        if len(result) > 200:
            result = result[:200]
        return result + " …"

    def _apply_collapsed_text(self, text):
        """Set label text, show/hide toggle button."""
        self._full_text = text
        if self.role != "user":
            self.msg_label.setText(text)
            return
        needs = self._needs_collapse(text)
        _log.debug("[_apply_collapsed_text] needs_collapse=%s, is_collapsed=%s",
                   needs, self._is_collapsed)
        if needs:
            if self._is_collapsed:
                self.msg_label.setText(self._collapsed_text(text))
                self._toggle_btn.setText("▼")
                self._toggle_btn.setToolTip("Expand")
            else:
                self.msg_label.setText(text)
                self._toggle_btn.setText("▲")
                self._toggle_btn.setToolTip("Collapse")
            self._toggle_btn.setVisible(True)
        else:
            self.msg_label.setText(text)
            self._toggle_btn.setVisible(False)
            self._is_collapsed = True

    def _toggle_expand(self):
        self._is_collapsed = not self._is_collapsed
        self._apply_collapsed_text(self._full_text)

    def _copy_text(self):
        """Copy the full message text to clipboard."""
        text = self._full_text if hasattr(self, "_full_text") else self.msg_label.text()
        clipboard = QtWidgets.QApplication.clipboard()
        clipboard.setText(text)
        # Brief visual feedback
        if self.role == "user" and hasattr(self, "_copy_icon") and _isValid(self._copy_icon):
            self._copy_icon.set_color("#66bb6a")  # green flash
            QtCore.QTimer.singleShot(1500, lambda: (
                self._copy_icon.set_color("#888888") if _isValid(self._copy_icon) else None
            ))
        elif hasattr(self, "_copy_btn") and _isValid(self._copy_btn):
            self._copy_btn.setText("✓ Copied")
            QtCore.QTimer.singleShot(1500, lambda: (
                self._copy_btn.setText("📋 Copy") if _isValid(self._copy_btn) else None
            ))

    def set_text(self, text):
        """Update the displayed message text (used for streaming)."""
        if self.role == "user" and hasattr(self, "_toggle_btn"):
            self._apply_collapsed_text(text)
        else:
            self._full_text = text
            self.msg_label.setText(text)

    def mousePressEvent(self, event):
        """When user clicks on the bubble, give focus to the parent scroll area
        so that mouse-wheel scrolling works immediately."""
        parent = self.parent()
        while parent is not None:
            if isinstance(parent, QtWidgets.QScrollArea):
                parent.setFocus(QtCore.Qt.MouseFocusReason)
                break
            parent = parent.parent()
        super(ChatBubble, self).mousePressEvent(event)


# ---------------------------------------------------------------------------
# Image Thumbnail Strip
# ---------------------------------------------------------------------------

class _ImageStripWheelGrabber(QtCore.QObject):
    """Application-level event filter that intercepts Wheel events *before*
    Nuke's own handlers consume them.  It checks whether the mouse cursor
    is inside any registered ImageStrip widget and, if so, forwards the
    event to that strip's ``_handle_wheel`` method."""

    _instance = None          # singleton
    _strips = []              # registered ImageStrip widgets (weak-ish list)

    @classmethod
    def ensure_installed(cls):
        """Install the global filter exactly once per QApplication."""
        if cls._instance is None:
            app = QtWidgets.QApplication.instance()
            if app is not None:
                cls._instance = cls(app)
                app.installEventFilter(cls._instance)
                print("[NB ImageStrip] Global wheel grabber installed")
        return cls._instance

    @classmethod
    def register(cls, strip):
        inst = cls.ensure_installed()
        if strip not in cls._strips:
            cls._strips.append(strip)

    @classmethod
    def unregister(cls, strip):
        if strip in cls._strips:
            cls._strips.remove(strip)

    def eventFilter(self, obj, event):
        if event.type() == QtCore.QEvent.Wheel:
            # Check every registered strip
            for strip in list(self._strips):
                try:
                    # globalPos → is cursor inside the strip's scroll area?
                    gp = event.globalPos()
                    scroll = strip._scroll
                    local = scroll.mapFromGlobal(gp)
                    if scroll.rect().contains(local):
                        strip._handle_wheel(event)
                        event.accept()
                        return True          # consumed – Nuke won't see it
                except Exception:
                    pass
        return False


class _ThumbCard(QtWidgets.QFrame):
    """Single thumbnail card: image + filename label.
    A '✕' remove button at top-right corner appears only on mouse hover.
    Double-click opens the image in system default viewer."""

    removeClicked = QtCore.Signal(str)  # emits image path

    def __init__(self, img_path, parent=None):
        print("=" * 60)
        print("[NB ImageStrip] _ThumbCard.__init__ CALLED with path: '{}'".format(img_path))
        super(_ThumbCard, self).__init__(parent)
        self._img_path = img_path
        self.setFixedSize(64, 64)
        self.setStyleSheet("QFrame { background: #333; border-radius: 4px; }")

        # Use a layout so that sizeHint works correctly inside QScrollArea
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setAlignment(QtCore.Qt.AlignCenter)

        # -- thumbnail --
        self._thumb = QtWidgets.QLabel()
        _file_exists = bool(img_path and os.path.isfile(img_path))
        print("[NB ImageStrip] _ThumbCard: file_exists={}, path='{}'".format(_file_exists, img_path))
        if _file_exists:
            pix = QtGui.QPixmap(img_path)
            print("[NB ImageStrip] _ThumbCard loading: {} | null={}".format(img_path, pix.isNull()))
            if not pix.isNull():
                pix = pix.scaled(60, 60, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
                self._thumb.setPixmap(pix)
                print("[NB ImageStrip] _ThumbCard: OK - pixmap set ({}x{})".format(pix.width(), pix.height()))
            else:
                # File exists but cannot be decoded as an image
                self._thumb.setText("⚠")
                self._thumb.setStyleSheet(
                    "color: #f59e0b; font-size: 24px; background: #2a2a2a; border-radius: 4px;")
                print("[NB ImageStrip] _ThumbCard: WARN - file exists but not a valid image")
        else:
            # File is missing — show a broken-link placeholder so the user knows
            self._thumb.setText("🖼")
            self._thumb.setStyleSheet(
                "color: #666; font-size: 20px; background: #2a2a2a; border-radius: 4px;")
            self._thumb.setToolTip("Image file not found:\n{}".format(img_path))
            print("[NB ImageStrip] _ThumbCard: MISSING - showing placeholder for '{}'".format(img_path))
        self._thumb.setFixedSize(60, 60)
        self._thumb.setAlignment(QtCore.Qt.AlignCenter)
        layout.addWidget(self._thumb)

        # -- remove button (overlay, TOP-RIGHT corner, hidden by default) --
        self._remove_btn = QtWidgets.QPushButton("✕", self)
        btn_size = 18
        self._remove_btn.setFixedSize(btn_size, btn_size)
        self._remove_btn.setStyleSheet(
            "QPushButton { background: rgba(239,68,68,200); color: white; border: none; "
            "border-radius: %dpx; font-size: 10px; font-weight: bold; }"
            "QPushButton:hover { background: rgba(220,38,38,240); }" % (btn_size // 2)
        )
        # Position at top-right corner with small margin
        margin = 2
        self._remove_btn.move(64 - btn_size - margin, margin)
        self._remove_btn.hide()
        self._remove_btn.clicked.connect(lambda: self.removeClicked.emit(self._img_path))

    def enterEvent(self, event):
        self._remove_btn.show()
        super(_ThumbCard, self).enterEvent(event)

    def leaveEvent(self, event):
        self._remove_btn.hide()
        super(_ThumbCard, self).leaveEvent(event)

    # ---- Left-click drag-to-reorder support ----
    def mousePressEvent(self, event):
        """Start a left-drag if user presses left button on the card."""
        if event.button() == QtCore.Qt.LeftButton and not self._remove_btn.underMouse():
            self._drag_start_pos = event.pos()
            self._is_dragging = False
        else:
            super(_ThumbCard, self).mousePressEvent(event)

    def mouseMoveEvent(self, event):
        """If moved far enough from press position, begin card drag-reorder."""
        if hasattr(self, '_drag_start_pos') and self._drag_start_pos is not None:
            if (event.pos() - self._drag_start_pos).manhattanLength() > 12:
                self._is_dragging = True
                # Notify parent ImageStrip that we want to start dragging
                strip = self._find_parent_strip()
                if strip is not None:
                    strip._start_card_drag(self)
                return
        super(_ThumbCard, self).mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        """End drag on left release."""
        if hasattr(self, '_is_dragging') and self._is_dragging:
            strip = self._find_parent_strip()
            if strip is not None:
                strip._end_card_drag(self)
            self._is_dragging = False
            self._drag_start_pos = None
            return
        self._drag_start_pos = None
        super(_ThumbCard, self).mouseReleaseEvent(event)

    def _find_parent_strip(self):
        """Walk up parent chain to find the owning ImageStrip widget."""
        w = self.parent() if hasattr(self, 'parent') and callable(self.parent) else None
        while w is not None:
            if isinstance(w, ImageStrip):
                return w
            w = w.parent() if hasattr(w, 'parent') and callable(w.parent) else None
        return None

    def mouseDoubleClickEvent(self, event):
        """Open image with system default viewer on double-click."""
        import subprocess
        import sys
        if self._img_path and os.path.isfile(self._img_path):
            try:
                if sys.platform == "win32":
                    os.startfile(self._img_path)
                else:
                    opener = "open" if sys.platform == "darwin" else "xdg-open"
                    subprocess.Popen([opener, self._img_path])
            except Exception as e:
                print("[NB ImageStrip] Failed to open image: {}".format(e))
        else:
            # File is missing — show a friendly message
            QtWidgets.QMessageBox.warning(
                self, "Image Not Found",
                "The image file could not be found:\n\n{}\n\n"
                "It may have been moved or deleted. You can remove this "
                "thumbnail using the ✕ button.".format(self._img_path))
        super(_ThumbCard, self).mouseDoubleClickEvent(event)

    def wheelEvent(self, event):
        """Forward wheel events to the parent ImageStrip so scrolling
        works even when the cursor is directly on a thumbnail."""
        strip = self.parent()
        # Walk up to find the ImageStrip (parent chain: card -> _inner -> viewport -> scroll -> ImageStrip)
        while strip is not None and not isinstance(strip, ImageStrip):
            strip = strip.parent() if hasattr(strip, 'parent') and callable(strip.parent) else None
        if strip is not None:
            strip._handle_wheel(event)
            event.accept()
        else:
            super(_ThumbCard, self).wheelEvent(event)


class ImageStrip(QtWidgets.QWidget):
    """Shows thumbnails of attached images inside a horizontally scrollable
    area, with a '+' add-local-file button always pinned at the right end."""

    imagesChanged = QtCore.Signal()

    def __init__(self, add_callback=None, parent=None):
        super(ImageStrip, self).__init__(parent)
        self._images = []  # list of file paths
        self._add_callback = add_callback  # called when "+" is clicked

        # Outer layout: scroll area (takes remaining space) + pinned "+" button
        outer = QtWidgets.QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        # --- Scroll area for thumbnails ---
        self._scroll = QtWidgets.QScrollArea()
        self._scroll.setWidgetResizable(False)  # inner widget keeps its own width
        self._scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self._scroll.setFixedHeight(72)
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
        )

        # Inner container widget + layout for the thumbnail cards
        self._inner = QtWidgets.QWidget()
        self._inner.setStyleSheet("background: transparent;")
        self._layout = QtWidgets.QHBoxLayout(self._inner)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(4)
        self._layout.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        self._scroll.setWidget(self._inner)

        outer.addWidget(self._scroll, 1)

        # --- Pinned "+" button always visible at the right ---
        self._add_btn = QtWidgets.QPushButton("+")
        self._add_btn.setFixedSize(40, 40)
        self._add_btn.setToolTip("Add local file (images & documents)")
        self._add_btn.setStyleSheet(
            "QPushButton { background: #333; color: #e74c3c; border: 2px solid #555; "
            "border-radius: 6px; font-size: 22px; font-weight: bold; }"
            "QPushButton:hover { background: #444; border-color: #e74c3c; }"
        )
        if self._add_callback:
            self._add_btn.clicked.connect(self._add_callback)
        outer.addWidget(self._add_btn, alignment=QtCore.Qt.AlignVCenter)

        # Enable mouse-wheel horizontal scrolling
        self._scroll.installEventFilter(self)
        self._scroll.viewport().installEventFilter(self)
        self._inner.installEventFilter(self)
        self._mid_drag = False       # middle-button drag state
        self._mid_drag_start_x = 0   # mouse X at drag start
        self._mid_drag_start_val = 0  # scrollbar value at drag start

        # ---- Card reorder-drag state ----
        self._drag_card = None           # _ThumbCard currently being dragged (or None)
        self._drag_offset_x = 0          # horizontal offset from mouse to card left edge
        self._drag_orig_index = -1       # original index of dragged card in _images
        self._drag_placeholder = None    # placeholder widget inserted at drag position
        self._drop_indicator = None      # semi-transparent overlay showing drop target position

        # Register with the app-level wheel grabber so scrolling works
        # even when Nuke intercepts wheel events at a higher level.
        _ImageStripWheelGrabber.register(self)

        # Build initial state
        self._rebuild()

    def _is_scroll_child(self, obj):
        """Return True if *obj* belongs to the scroll area hierarchy."""
        if obj is self._scroll or obj is self._scroll.viewport() or obj is self._inner:
            return True
        # Walk up the parent chain to see if it's a child of _inner
        w = obj
        while w is not None:
            if w is self._inner:
                return True
            w = w.parent() if hasattr(w, 'parent') and callable(w.parent) else None
        return False

    def hideEvent(self, event):
        """Unregister from global wheel grabber when hidden/closed."""
        _ImageStripWheelGrabber.unregister(self)
        super(ImageStrip, self).hideEvent(event)

    def showEvent(self, event):
        """Re-register when shown again."""
        _ImageStripWheelGrabber.register(self)
        super(ImageStrip, self).showEvent(event)

    # -- event filter: wheel scroll + middle-button drag --
    def eventFilter(self, obj, event):
        etype = event.type()
        viewport = self._scroll.viewport()

        # DEBUG: log mouse events on scroll children
        _debug_types = {QtCore.QEvent.Wheel, QtCore.QEvent.MouseButtonPress,
                        QtCore.QEvent.MouseButtonRelease}
        if etype in _debug_types and self._is_scroll_child(obj):
            print("[NB ImageStrip] eventFilter: type={} obj={} btn={}".format(
                etype, type(obj).__name__,
                event.button() if hasattr(event, 'button') else 'N/A'))

        # ---- Mouse wheel → horizontal scroll ----
        if etype == QtCore.QEvent.Wheel and self._is_scroll_child(obj):
            delta = event.angleDelta().y()
            if delta == 0:
                delta = event.angleDelta().x()
            sb = self._scroll.horizontalScrollBar()
            old_val = sb.value()
            sb.setValue(old_val - delta)
            print("[NB ImageStrip] wheel: delta={} sb={}->{}/{}  obj={}".format(
                delta, old_val, sb.value(), sb.maximum(), type(obj).__name__))
            return True

        # ---- Middle-button press → start drag ----
        if etype == QtCore.QEvent.MouseButtonPress and self._is_scroll_child(obj):
            if event.button() == QtCore.Qt.MiddleButton:
                self._mid_drag = True
                self._mid_drag_start_x = event.globalX()
                self._mid_drag_start_val = self._scroll.horizontalScrollBar().value()
                viewport.setCursor(QtCore.Qt.ClosedHandCursor)
                print("[NB ImageStrip] mid-drag start at x={}".format(self._mid_drag_start_x))
                return True

        # ---- Middle-button move → drag scroll ----
        if etype == QtCore.QEvent.MouseMove:
            if self._mid_drag:
                dx = event.globalX() - self._mid_drag_start_x
                sb = self._scroll.horizontalScrollBar()
                sb.setValue(self._mid_drag_start_val - dx)
                return True
            # ---- Card reorder-drag: track mouse position ----
            elif self._drag_card is not None:
                self._update_card_drag()
                return True

        # ---- Middle-button release → end drag ----
        if etype == QtCore.QEvent.MouseButtonRelease and self._mid_drag:
            if event.button() == QtCore.Qt.MiddleButton:
                self._mid_drag = False
                viewport.setCursor(QtCore.Qt.ArrowCursor)
                print("[NB ImageStrip] mid-drag end")
                return True

        # ---- Left-button release during card drag → finish reorder ----
        if etype == QtCore.QEvent.MouseButtonRelease and self._drag_card is not None:
            if event.button() == QtCore.Qt.LeftButton:
                self._end_card_drag(self._drag_card)
                return True

        return super(ImageStrip, self).eventFilter(obj, event)

    # -- direct wheel handler (called from eventFilter and child wheelEvent) --
    def _handle_wheel(self, event):
        """Scroll the image strip horizontally in response to a wheel event."""
        delta = event.angleDelta().y()
        if delta == 0:
            delta = event.angleDelta().x()
        sb = self._scroll.horizontalScrollBar()
        old_val = sb.value()
        sb.setValue(old_val - delta)
        print("[NB ImageStrip] wheel: delta={} sb={}->{}/{}".format(
            delta, old_val, sb.value(), sb.maximum()))

    def wheelEvent(self, event):
        """Catch wheel events that reach the ImageStrip widget itself."""
        self._handle_wheel(event)
        event.accept()

    @property
    def images(self):
        return list(self._images)

    def add_image(self, path):
        print("[NB ImageStrip] >>> add_image called: path='{}'".format(path))
        if path and path not in self._images:
            self._images.append(path)
            print("[NB ImageStrip] add_image: ADDED '{}' | exists={} | total={}".format(
                path, os.path.isfile(path) if path else False, len(self._images)))
            self._rebuild()
            self.imagesChanged.emit()
        else:
            reason = "empty/None" if not path else "duplicate"
            print("[NB ImageStrip] add_image SKIPPED '{}': {}".format(path, reason))

    def clear_images(self):
        self._images.clear()
        self._rebuild()
        self.imagesChanged.emit()

    def _rebuild(self):
        print("[NB ImageStrip] >>> _rebuild called: {} images in _images list".format(len(self._images)))
        for i, p in enumerate(self._images):
            print("[NB ImageStrip]   _images[{}]: '{}' (exists={})".format(
                i, p, os.path.exists(p) if p else False))

        # Clear layout
        while self._layout.count():
            item = self._layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        # Add all thumbnail cards (no overflow label needed — scroll handles it)
        for idx, img_path in enumerate(self._images):
            print("[NB ImageStrip]   Creating card[{}] for '{}'".format(idx, img_path))
            card = _ThumbCard(img_path)
            card.removeClicked.connect(self._remove)
            # Install eventFilter so middle-drag / wheel works even when
            # the cursor is directly over a thumbnail card.
            card.installEventFilter(self)
            card._thumb.installEventFilter(self)
            self._layout.addWidget(card)

        # Manually calculate the inner widget width so QScrollArea can scroll
        # Each card is 64px wide, spacing is 4px between cards
        n = len(self._images)
        if n > 0:
            inner_w = n * 64 + (n - 1) * 4  # cards + spacing between them
        else:
            inner_w = 0
        print("[NB ImageStrip] _rebuild: calculated inner_w={} for {} cards".format(inner_w, n))
        self._inner.setFixedSize(inner_w, 72)

        # Scroll to the rightmost position so newest images are visible
        QtCore.QTimer.singleShot(0, lambda: self._scroll.horizontalScrollBar().setValue(
            self._scroll.horizontalScrollBar().maximum()))

        # Set overall strip height
        self.setFixedHeight(72 if self._images else 48)

    def _remove(self, path):
        if path in self._images:
            self._images.remove(path)
            self._rebuild()
            self.imagesChanged.emit()

    # ---- Card drag-to-reorder methods (called by _ThumbCard) ----
    def _start_card_drag(self, card):
        """Begin dragging a thumbnail card for reordering."""
        if self._drag_card is not None:
            return  # already dragging

        # Find the index of this card in _images
        try:
            idx = self._images.index(card._img_path)
        except ValueError:
            return

        self._drag_card = card
        self._drag_orig_index = idx

        # Calculate offset from mouse to card left edge (in inner widget coords)
        card_global_pos = card.mapToGlobal(QtCore.QPoint(0, 0))
        cursor_pos = QtGui.QCursor.pos()
        self._drag_offset_x = cursor_pos.x() - card_global_pos.x()

        # Hide the remove button during drag
        card._remove_btn.hide()

        # Make the dragged card semi-transparent with subtle border (no heavy visual)
        card.setWindowFlags(card.windowFlags() | QtCore.Qt.WindowStaysOnTopHint)
        card.setStyleSheet(
            "QFrame { background: rgba(60, 60, 60, 200); border: 1px solid #888888; "
            "border-radius: 4px; }")
        card.raise_()
        card.show()
        card.grabMouse()

        # Insert a transparent placeholder at the original position (invisible to user)
        placeholder = QtWidgets.QFrame()
        placeholder.setFixedSize(64, 64)
        placeholder.setStyleSheet("QFrame { background: transparent; border: none; }")
        self._layout.insertWidget(idx, placeholder)
        self._drag_placeholder = placeholder
        self._drag_current_index = idx

        print("[NB ImageStrip] Drag started: card '{}' from index {}".format(card._img_path, idx))

    def _update_card_drag(self):
        """Update drag position during mouse move. Called via event filter on _inner."""
        if self._drag_card is None:
            return

        # Move the floating card with the cursor
        cursor = QtGui.QCursor.pos()
        new_x = cursor.x() - self._drag_offset_x
        # Map to inner widget coordinates
        inner_global = self._inner.mapToGlobal(QtCore.QPoint(0, 0))
        local_x = new_x - inner_global.x() + self._scroll.horizontalScrollBar().value()
        local_y = 4  # small top margin within the strip

        self._drag_card.move(local_x, local_y)

        # Determine which position we're hovering over for drop indicator
        card_center_x = local_x + 32  # center of the 64px card
        target_index = self._calculate_drop_index(card_center_x)

        if target_index != self._drag_current_index:
            self._move_placeholder(target_index)
            self._drag_current_index = target_index

    def _calculate_drop_index(self, drop_x):
        """Calculate which index a card at horizontal position *drop_x* would occupy."""
        n = len(self._images)
        if n <= 1:
            return 0
        card_w = 64
        spacing = 4
        for i in range(n):
            left = i * (card_w + spacing)
            right = left + card_w
            mid = left + card_w / 2.0
            if drop_x < mid:
                return i
        return n - 1

    def _move_placeholder(self, new_index):
        """Move the placeholder widget to *new_index* in the layout."""
        if self._drag_placeholder is None:
            return
        old_index = self._layout.indexOf(self._drag_placeholder)
        if old_index == -1 or old_index == new_index:
            return
        self._layout.removeWidget(self._drag_placeholder)
        self._layout.insertWidget(new_index, self._drag_placeholder)

    def _end_card_drag(self, card):
        """Finish drag: reorder _images list, cleanup visual state."""
        if self._drag_card is None or self._drag_card != card:
            return

        final_index = self._drag_current_index
        orig_index = self._drag_orig_index

        print("[NB ImageStrip] Drag ended: {} -> {}".format(orig_index, final_index))

        # Release mouse grab & restore card appearance
        try:
            card.releaseMouse()
        except Exception:
            pass
        card.setWindowFlags(card.windowFlags() & ~QtCore.Qt.WindowStaysOnTopHint)
        card.setParent(self._inner)  # re-parent back into scroll area
        card.setStyleSheet("")
        card.show()

        # Remove placeholder
        if self._drag_placeholder is not None:
            self._layout.removeWidget(self._drag_placeholder)
            self._drag_placeholder.deleteLater()
            self._drag_placeholder = None

        # Reorder the _images list if index actually changed
        if orig_index != final_index:
            item = self._images.pop(orig_index)
            self._images.insert(final_index, item)
            print("[NB ImageStrip] Images reordered: moved '{}' to index {}".format(item, final_index))

        # Reset state
        self._drag_card = None
        self._drag_offset_x = 0
        self._drag_orig_index = -1
        self._drag_current_index = -1

        # Rebuild the entire strip (clean up any visual artifacts)
        self._rebuild()

        # Emit change signal so parent saves updated order
        self.imagesChanged.emit()

    def _cancel_card_drag(self):
        """Cancel an in-progress drag without applying changes."""
        if self._drag_card is None:
            return
        card = self._drag_card

        try:
            card.releaseMouse()
        except Exception:
            pass
        card.setWindowFlags(card.windowFlags() & ~QtCore.Qt.WindowStaysOnTopHint)
        card.setParent(self._inner)
        card.setStyleSheet("")
        card.show()

        if self._drag_placeholder is not None:
            self._layout.removeWidget(self._drag_placeholder)
            self._drag_placeholder.deleteLater()
            self._drag_placeholder = None

        self._drag_card = None
        self._rebuild()





# ---------------------------------------------------------------------------
# Scroll Area that grabs focus on click / wheel so middle-mouse scrolling
# works regardless of which child widget the cursor is over.
# ---------------------------------------------------------------------------
class _WheelScrollArea(QtWidgets.QScrollArea):
    """QScrollArea that intercepts mouse-click and wheel events from ALL
    child widgets so that scrolling works no matter where the cursor is."""

    def __init__(self, parent=None):
        super(_WheelScrollArea, self).__init__(parent)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self._handling_wheel = False  # prevent re-entrant wheel handling

    # -- public: call after adding new children (e.g. chat bubbles) ----------
    def install_filters(self):
        """(Re-)install the event filter on the inner widget and every
        descendant.  Call this after adding / rebuilding chat bubbles."""
        inner = self.widget()
        if inner and _isValid(inner):
            inner.installEventFilter(self)
            for child in inner.findChildren(QtWidgets.QWidget):
                child.installEventFilter(self)

    # -- Override setWidget to auto-install filters --------------------------
    def setWidget(self, widget):
        super(_WheelScrollArea, self).setWidget(widget)
        self.install_filters()

    # -- Intercept child events ----------------------------------------------
    def eventFilter(self, obj, event):
        etype = event.type()
        if etype == QtCore.QEvent.MouseButtonPress:
            self.setFocus(QtCore.Qt.MouseFocusReason)
        elif etype == QtCore.QEvent.Wheel:
            if not self._handling_wheel:
                self._handling_wheel = True
                self.setFocus(QtCore.Qt.MouseFocusReason)
                self._do_wheel(event)
                self._handling_wheel = False
            return True  # always consume to stop further propagation
        return super(_WheelScrollArea, self).eventFilter(obj, event)

    # -- Own events ----------------------------------------------------------
    def mousePressEvent(self, event):
        self.setFocus(QtCore.Qt.MouseFocusReason)
        super(_WheelScrollArea, self).mousePressEvent(event)

    def wheelEvent(self, event):
        if not self._handling_wheel:
            self._handling_wheel = True
            self.setFocus(QtCore.Qt.MouseFocusReason)
            super(_WheelScrollArea, self).wheelEvent(event)
            self._handling_wheel = False
        else:
            event.accept()  # already handled, just consume

    # -- Manual wheel scroll -------------------------------------------------
    def _do_wheel(self, event):
        vbar = self.verticalScrollBar()
        if not vbar:
            return
        try:
            delta = event.angleDelta().y()
        except AttributeError:
            delta = event.delta()
        # Scroll 3 lines per notch (120 units = 1 notch), smooth feel
        pixels_per_line = 20
        notches = delta / 120.0
        scroll_amount = int(notches * pixels_per_line * 3)
        vbar.setValue(vbar.value() - scroll_amount)


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
        self._scroll_area = _WheelScrollArea()
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self._scroll_area.setFocusPolicy(QtCore.Qt.StrongFocus)

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

        # Row 1: Input label + Select(Nuke node) + Paste + stretch + Model combo
        toolbar_row = QtWidgets.QHBoxLayout()
        toolbar_row.setSpacing(4)

        input_label = QtWidgets.QLabel("Input")
        input_label.setStyleSheet("color: #aaa; font-size: 11px; font-weight: bold;")
        toolbar_row.addWidget(input_label)

        select_btn = QtWidgets.QPushButton("Select")
        select_btn.setObjectName("actionBtn")
        select_btn.setToolTip("Select image from Nuke Node Graph node")
        select_btn.clicked.connect(self._grab_nuke_node)
        toolbar_row.addWidget(select_btn)

        paste_btn = QtWidgets.QPushButton("Paste")
        paste_btn.setObjectName("actionBtn")
        paste_btn.setToolTip("Paste image from clipboard")
        paste_btn.clicked.connect(self._paste_image)
        toolbar_row.addWidget(paste_btn)

        toolbar_row.addStretch()

        self._model_combo = QtWidgets.QComboBox()
        self._model_combo.setMinimumWidth(140)
        for m in CHAT_MODELS:
            self._model_combo.addItem(m)
        self._model_combo.currentIndexChanged.connect(self._on_model_changed)
        toolbar_row.addWidget(self._model_combo)

        input_section.addLayout(toolbar_row)

        # Row 2: Image strip (thumbnails + "+" add button at the end, same row as Select/Paste)
        self._image_strip = ImageStrip(add_callback=self._select_image)
        input_section.addWidget(self._image_strip)

        # Row 3: Text input
        self._text_input = QtWidgets.QPlainTextEdit()
        self._text_input.setPlaceholderText("Please enter the question...")
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

    # ---- Event filter (Enter to send, Ctrl+Enter to newline) ----------------

    def eventFilter(self, obj, event):
        if obj is self._text_input and event.type() == QtCore.QEvent.KeyPress:
            if event.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
                if event.modifiers() & QtCore.Qt.ControlModifier:
                    # Ctrl+Enter => insert newline
                    self._text_input.insertPlainText("\n")
                    return True
                else:
                    # Enter => send message
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
            self._insert_bubble_widget(bubble, msg["role"])

        # Scroll to bottom
        QtCore.QTimer.singleShot(50, self._scroll_to_bottom)
        # Re-install event filters so new bubbles respond to click/wheel
        QtCore.QTimer.singleShot(100, self._scroll_area.install_filters)

    def _add_bubble(self, role, text, images=None):
        _log.debug("[_add_bubble] role=%s, text_len=%d, images=%s",
                   role, len(text), bool(images))
        bubble = ChatBubble(role, text, images=images)
        self._insert_bubble_widget(bubble, role)
        QtCore.QTimer.singleShot(50, self._scroll_to_bottom)
        # Install event filters on the new bubble and its children
        QtCore.QTimer.singleShot(100, self._scroll_area.install_filters)
        return bubble

    def _insert_bubble_widget(self, bubble, role):
        """Insert a bubble into the chat layout.
        User bubbles are right-aligned with a computed fixed width so the
        text fills the available space instead of collapsing to minimum."""
        if role == "user":
            # Calculate usable width from the visible chat area
            area_w = self._scroll_area.viewport().width()
            scroll_w = self._scroll_area.width()
            max_bubble_w = max(300, int(area_w * 0.85))

            # Padding inside bubble: contentsMargins(12,6,12,6) + copy-btn(22) + spacing(6) = 52
            inner_pad = 52
            label_max_w = max(200, max_bubble_w - inner_pad)

            if hasattr(bubble, 'msg_label'):
                lbl = bubble.msg_label
                fm = QtGui.QFontMetrics(lbl.font())
                text = lbl.text()

                # Compute the natural (no-wrap) width of each line,
                # then pick the widest one.
                raw_lines = text.split("\n")
                max_text_w = 0
                for raw_line in raw_lines:
                    w = fm.horizontalAdvance(raw_line) if hasattr(fm, 'horizontalAdvance') else fm.width(raw_line)
                    max_text_w = max(max_text_w, w)

                # The bubble width should be:
                #  - at least 80 px (for very short text)
                #  - at most label_max_w (85% of viewport minus padding)
                #  - ideally = natural text width (so short msgs stay compact)
                ideal_label_w = min(max_text_w + 4, label_max_w)  # +4 for rounding
                ideal_bubble_w = ideal_label_w + inner_pad

                # Clamp
                ideal_bubble_w = max(80, min(ideal_bubble_w, max_bubble_w))

                # Set the label's maximumWidth so word-wrap uses it
                lbl.setMaximumWidth(ideal_label_w)
                # Fix the bubble to the computed width
                bubble.setFixedWidth(ideal_bubble_w)

                # Re-evaluate collapsed text now that label has correct width
                if hasattr(bubble, '_apply_collapsed_text') and hasattr(bubble, '_full_text'):
                    bubble._apply_collapsed_text(bubble._full_text)

                _log.debug("[_insert_bubble_widget] role=user | "
                           "scroll_area.width()=%d | viewport.width()=%d | "
                           "max_bubble_w=%d | label_max_w=%d | "
                           "max_text_w=%d | ideal_label_w=%d | ideal_bubble_w=%d",
                           scroll_w, area_w, max_bubble_w, label_max_w,
                           max_text_w, ideal_label_w, ideal_bubble_w)

            # Use wrapper + layout with AlignRight to position the fixed-width bubble
            wrapper = QtWidgets.QWidget()
            wrapper.setStyleSheet("background: transparent;")
            h = QtWidgets.QHBoxLayout(wrapper)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(6)

            # Copy button outside the bubble, centered vertically
            copy_icon = _CopyIconWidget(size=16, color="#888888")
            copy_btn = QtWidgets.QPushButton()
            copy_btn.setFixedSize(22, 22)
            copy_btn.setToolTip("Copy to clipboard")
            copy_btn.setCursor(QtCore.Qt.PointingHandCursor)
            copy_btn.setStyleSheet(
                "QPushButton { background: transparent; border: none; padding: 0px; }"
            )
            btn_layout = QtWidgets.QHBoxLayout(copy_btn)
            btn_layout.setContentsMargins(3, 3, 3, 3)
            btn_layout.addWidget(copy_icon)
            copy_btn.clicked.connect(bubble._copy_text)
            # Store references on bubble for feedback
            bubble._copy_icon = copy_icon
            bubble._copy_btn = copy_btn

            h.addStretch()
            h.addWidget(copy_btn, 0, QtCore.Qt.AlignVCenter)
            h.addWidget(bubble, 0, QtCore.Qt.AlignRight)
            self._chat_layout.insertWidget(self._chat_layout.count() - 1, wrapper)

            # Log post-layout sizes after a short delay
            def _log_post_layout():
                if _isValid(bubble) and _isValid(wrapper):
                    _log.debug("[_insert_bubble_widget] POST-LAYOUT: "
                               "bubble.width()=%d | bubble.height()=%d | "
                               "wrapper.width()=%d | "
                               "msg_label.width()=%d | msg_label.height()=%d | "
                               "msg_label.maximumWidth()=%d",
                               bubble.width(), bubble.height(),
                               wrapper.width(),
                               bubble.msg_label.width() if hasattr(bubble, 'msg_label') else -1,
                               bubble.msg_label.height() if hasattr(bubble, 'msg_label') else -1,
                               bubble.msg_label.maximumWidth() if hasattr(bubble, 'msg_label') else -1)
            QtCore.QTimer.singleShot(200, _log_post_layout)
        else:
            _log.debug("[_insert_bubble_widget] role=model/assistant, no width cap")
            self._chat_layout.insertWidget(self._chat_layout.count() - 1, bubble)

    def _scroll_to_bottom(self):
        vbar = self._scroll_area.verticalScrollBar()
        vbar.setValue(vbar.maximum())

    # ---- Image attachment --------------------------------------------------

    def _select_image(self):
        fpath, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Select File",
            "",
            "Supported Files (*.png *.jpg *.jpeg *.gif *.webp *.pdf *.doc *.docx *.ppt *.pptx *.xls *.xlsx *.txt *.rtf *.csv *.tsv);;"
            "Images (*.png *.jpg *.jpeg *.gif *.webp);;"
            "Documents (*.pdf *.doc *.docx *.ppt *.pptx *.txt *.rtf);;"
            "Spreadsheets (*.csv *.tsv *.xls *.xlsx);;"
            "All Files (*)",
        )
        if fpath:
            ext = os.path.splitext(fpath)[1].lower()
            if ext not in SUPPORTED_EXTENSIONS:
                QtWidgets.QMessageBox.warning(
                    self,
                    "Unsupported Format",
                    "The file format '{}' is not supported by Gemini.\n\n"
                    "Supported formats:\n"
                    "  Images: png, jpg, jpeg, gif, webp\n"
                    "  Documents: pdf, doc, docx, ppt, pptx, txt, rtf\n"
                    "  Spreadsheets: csv, tsv, xls, xlsx".format(ext or "(none)"),
                )
                return
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

    def _grab_nuke_node(self):
        """Grab the file path from the currently selected Read / Write /
        image-based node in Nuke's Node Graph and add it as an attachment."""
        try:
            import nuke
        except ImportError:
            self._status_label.setText("Nuke module not available")
            return

        sel = nuke.selectedNodes()
        if not sel:
            self._status_label.setText("No node selected in Node Graph")
            return

        added = 0
        for node in sel:
            # Try common knob names that hold file paths
            for knob_name in ("file", "filename", "proxy"):
                k = node.knob(knob_name)
                if k:
                    fpath = k.evaluate() if hasattr(k, 'evaluate') else k.value()
                    if fpath and os.path.isfile(fpath):
                        self._image_strip.add_image(fpath)
                        added += 1
                        break  # one path per node

        if added:
            self._status_label.setText("Added {} image(s) from Node Graph".format(added))
        else:
            self._status_label.setText("Selected node(s) have no valid file path")

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
        uploaded_files = []  # track uploaded file handles for cleanup
        client = None
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
                # Add files (images / documents)
                for img_path in msg.get("images", []):
                    if os.path.isfile(img_path):
                        try:
                            ext = os.path.splitext(img_path)[1].lower()
                            mime = SUPPORTED_MIME_MAP.get(ext)
                            if not mime:
                                continue  # skip unsupported formats

                            if ext in INLINE_MIME_EXTENSIONS:
                                # Images / plain text / csv can be sent inline
                                with open(img_path, "rb") as f:
                                    img_data = f.read()
                                parts.append(types.Part.from_bytes(data=img_data, mime_type=mime))
                            else:
                                # PDF / Office docs must be uploaded first
                                uploaded = client.files.upload(
                                    file=img_path,
                                    config=types.UploadFileConfig(mime_type=mime),
                                )
                                uploaded_files.append(uploaded)
                                parts.append(types.Part.from_uri(
                                    file_uri=uploaded.uri,
                                    mime_type=mime,
                                ))
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
        finally:
            # Clean up uploaded files from Gemini File API
            if client and uploaded_files:
                for uf in uploaded_files:
                    try:
                        client.files.delete(name=uf.name)
                    except Exception:
                        pass

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
