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
    background-color: #1e1e1e;
    color: #e0e0e0;
    font-family: 'Segoe UI', 'Arial', sans-serif;
    font-size: 13px;
}
QLabel {
    color: #e0e0e0;
    background: transparent;
}
QLineEdit, QTextEdit, QPlainTextEdit {
    background-color: #2a2a2a;
    border: none;
    border-radius: 6px;
    padding: 8px;
    color: #e0e0e0;
    selection-background-color: #4f87f7;
    selection-color: #ffffff;
    font-size: 13px;
}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {
    border: none;
}
QComboBox {
    background-color: #2a2a2a;
    border: 1px solid #3a3a3a;
    border-radius: 6px;
    padding: 6px 10px;
    color: #e0e0e0;
    font-size: 13px;
    min-height: 20px;
}
QComboBox::drop-down {
    border: none;
    width: 24px;
}
QComboBox::down-arrow {
    width: 10px;
    height: 10px;
}
QComboBox QAbstractItemView {
    background-color: #2a2a2a;
    selection-background-color: #3a3a3a;
    selection-color: #ffffff;
    outline: none;
    border: 1px solid #3a3a3a;
}
QPushButton#sendBtn {
    background-color: #3a3a3a;
    color: #e0e0e0;
    border: 1px solid #444444;
    border-radius: 6px;
    padding: 10px 20px;
    font-weight: bold;
    font-size: 14px;
}
QPushButton#sendBtn:hover {
    background-color: #444444;
    border-color: #555555;
}
QPushButton#sendBtn:pressed {
    background-color: #333333;
}
QPushButton#sendBtn:disabled {
    background-color: #2a2a2a;
    color: #666666;
    border-color: #333333;
}
QPushButton#actionBtn {
    background-color: #3a3a3a;
    color: #c0c0c0;
    border: 1px solid #484848;
    padding: 6px 14px;
    font-size: 12px;
    border-radius: 6px;
}
QPushButton#actionBtn:hover {
    background-color: #444444;
    border-color: #606060;
    color: #e0e0e0;
}
QPushButton#newDialogueBtn {
    background-color: transparent;
    color: #b0b0b0;
    border: 1px solid #484848;
    border-radius: 6px;
    padding: 4px 12px;
    font-size: 18px;
    font-weight: normal;
    min-width: 28px;
    max-width: 28px;
}
QPushButton#newDialogueBtn:hover {
    background-color: #2a2a2a;
    color: #e0e0e0;
    border-color: #666666;
}
QPushButton#deleteBtn {
    background-color: #c0392b;
    color: #ffffff;
    border: none;
    border-radius: 50%;
    padding: 2px;
    font-size: 12px;
    min-width: 22px;
    max-width: 22px;
    min-height: 22px;
    max-height: 22px;
}
QPushButton#deleteBtn:hover {
    background-color: #e74c3c;
}
QPushButton#copyBtn {
    background-color: transparent;
    color: #888888;
    border: 1px solid #444444;
    border-radius: 4px;
    padding: 2px 10px;
    font-size: 11px;
}
QPushButton#copyBtn:hover {
    background-color: #333333;
    color: #cccccc;
    border-color: #666666;
}
QScrollArea {
    border: none;
    background-color: #1e1e1e;
}
QScrollBar:vertical {
    background-color: #1e1e1e;
    width: 8px;
    border-radius: 4px;
}
QScrollBar::handle:vertical {
    background-color: #444444;
    border-radius: 4px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background-color: #585858;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}
/* ---- Input section label style ---- */
QLabel#inputLabel {
    color: #d4a017;
    font-size: 13px;
    font-weight: bold;
}
/* ---- Session dropdown button (custom) ---- */
QPushButton#sessionDropdown {
    background-color: #2a2a2a;
    color: #e0e0e0;
    border: 1px solid #3a3a3a;
    border-radius: 6px;
    padding: 6px 12px;
    font-size: 13px;
    text-align: left;
}
QPushButton#sessionDropdown:hover {
    background-color: #333333;
    border-color: #555555;
}
/* ---- Chat area role labels ---- */
QLabel#roleLabel {
    color: #e0e0e0;
    font-size: 14px;
    font-weight: bold;
}
QLabel#thinkingLabel {
    color: #888888;
    font-size: 12px;
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

    _MAX_USER_LINES = 2  # User bubble collapsed line limit

    def __init__(self, role, text, images=None, parent=None):
        super(ChatBubble, self).__init__(parent)
        self.role = role
        self._full_text = text
        self._is_collapsed = True
        is_user = (role == "user")

        if is_user:
            # ---- User message: compact bar, right-aligned, max 3 lines ----
            self.setStyleSheet(
                "QFrame { background-color: #2d2d2d; border: none; "
                "border-radius: 12px; padding: 0px; }"
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
            self.msg_label.setFocusPolicy(QtCore.Qt.NoFocus)
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
            # ---- Gemini message: clean flat style matching Google Gemini UI ----
            self.setStyleSheet(
                "QFrame { background-color: transparent; border: none; }"
            )

            layout = QtWidgets.QVBoxLayout(self)
            layout.setContentsMargins(8, 4, 8, 4)
            layout.setSpacing(6)

            # Top row: copy icon (left) + "Gemini:" label
            top_row = QtWidgets.QHBoxLayout()
            top_row.setSpacing(8)

            # Left: copy icon button
            copy_icon_btn = QtWidgets.QPushButton()
            copy_icon_btn.setFixedSize(24, 24)
            copy_icon_btn.setToolTip("Copy to clipboard")
            copy_icon_btn.setCursor(QtCore.Qt.PointingHandCursor)
            copy_icon_btn.setStyleSheet(
                "QPushButton { background: transparent; border: none; padding: 0px; }"
                "QPushButton:hover { background: #333333; border-radius: 12px; }"
            )
            btn_icon_layout = QtWidgets.QHBoxLayout(copy_icon_btn)
            btn_icon_layout.setContentsMargins(4, 4, 4, 4)
            _copy_icon = _CopyIconWidget(size=16, color="#888888")
            btn_icon_layout.addWidget(_copy_icon)
            copy_icon_btn.clicked.connect(self._copy_text)
            self._copy_icon_widget = _copy_icon
            top_row.addWidget(copy_icon_btn, 0, QtCore.Qt.AlignTop)

            # Right column: label + thinking toggle + text
            right_col = QtWidgets.QVBoxLayout()
            right_col.setSpacing(4)
            right_col.setContentsMargins(0, 0, 0, 0)

            # "Gemini:" label
            role_label = QtWidgets.QLabel("Gemini:")
            role_label.setObjectName("roleLabel")
            right_col.addWidget(role_label)

            # "显示思路" dropdown toggle — clickable row to collapse/expand reply
            self._thinking_collapsed = False
            self._full_response_text = text

            # Clickable container for thinking toggle
            think_container = QtWidgets.QWidget()
            think_container.setCursor(QtCore.Qt.PointingHandCursor)
            think_container.setStyleSheet(
                "QWidget { background: transparent; }"
                "QWidget:hover { background: rgba(255,255,255,0.04); border-radius: 3px; }"
            )
            think_row = QtWidgets.QHBoxLayout(think_container)
            think_row.setContentsMargins(2, 1, 6, 1)
            think_row.setSpacing(4)
            think_label = QtWidgets.QLabel("显示思路")
            think_label.setObjectName("thinkingLabel")
            think_row.addWidget(think_label)
            self._think_arrow = QtWidgets.QLabel("▼")
            self._think_arrow.setStyleSheet("color: #888888; font-size: 10px;")
            think_row.addWidget(self._think_arrow)
            think_row.addStretch()
            right_col.addWidget(think_container)

            # Connect mouse click event on think_container
            think_container.mousePressEvent = lambda e: self._toggle_collapse_reply()

            # Message text
            self.msg_label = QtWidgets.QLabel(text)
            self.msg_label.setWordWrap(True)
            # Prevent focus so no caret/cursor can appear
            self.msg_label.setFocusPolicy(QtCore.Qt.NoFocus)
            # Do NOT enable TextSelectableByMouse — it causes an ugly
            # text-cursor (caret) to appear when the label gains focus.
            # Copy is handled by the dedicated copy icon button instead.
            self.msg_label.setTextInteractionFlags(QtCore.Qt.NoTextInteraction)
            self.msg_label.setStyleSheet(
                "color: #d0d0d0; font-size: 13px; background: transparent;"
                "line-height: 1.5; padding: 2px 0px;"
            )
            right_col.addWidget(self.msg_label)

            top_row.addLayout(right_col, 1)
            layout.addLayout(top_row)

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
        """Return text truncated to fit within ~_MAX_USER_LINES visual lines,
        ending with '...' if content was cut."""
        fm = QtGui.QFontMetrics(self.msg_label.font())
        max_w = self.msg_label.maximumWidth()
        if max_w >= 16777215:
            max_w = self.maximumWidth() - 36  # subtract inner padding
        if max_w < 50:
            max_w = 300

        suffix = "..."
        lines = text.split("\n")
        result_lines = []
        total_chars = 0
        for line in lines:
            # Check if adding this line exceeds visual line limit
            if not line:
                result_lines.append("")
                total_chars += 1
                continue
            # How many visual lines does this line take?
            line_w = fm.horizontalAdvance(line) if hasattr(fm, 'horizontalAdvance') else fm.width(line)
            vis_lines = max(1, int((line_w + max_w - 1) / max_w)) if line else 1

            # Count current total visual lines so far
            current_vis = sum(
                max(1, int(((fm.horizontalAdvance(l) if hasattr(fm, 'horizontalAdvance') else fm.width(l)) + max_w - 1) / max_w)) if l else 1
                for l in result_lines
            )
            if current_vis + vis_lines > self._MAX_USER_LINES:
                # This line would exceed — truncate it to fit remaining space
                remaining = self._MAX_USER_LINES - current_vis
                avail_chars = int(max_w * remaining / (fm.averageCharWidth() or 8))
                truncated = line[:max(avail_chars - len(suffix), 10)] + suffix
                result_lines.append(truncated)
                break
            else:
                result_lines.append(line)

        result = "\n".join(result_lines)
        return result

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
                collapsed = self._collapsed_text(text)
                # Always append ... suffix when collapsed
                if not collapsed.endswith("..."):
                    collapsed = collapsed + "..."
                self.msg_label.setText(collapsed)
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

    def _toggle_collapse_reply(self):
        """Toggle collapse/expand for Gemini reply (model role) bubble."""
        if not hasattr(self, '_thinking_collapsed'):
            return
        self._thinking_collapsed = not self._thinking_collapsed
        if self._thinking_collapsed:
            # Collapse: hide reply text, show preview + change arrow to ▲
            self.msg_label.setVisible(False)
            self._think_arrow.setText("▲")
            # Show a brief preview (first 80 chars)
            preview = self._full_response_text[:80] + ("..." if len(self._full_response_text) > 80 else "")
            if not hasattr(self, '_collapse_preview'):
                self._collapse_preview = QtWidgets.QLabel(preview)
                self._collapse_preview.setWordWrap(True)
                self._collapse_preview.setStyleSheet(
                    "color: #999999; font-size: 12px; background: transparent; "
                    "padding: 2px 0px; font-style: italic;"
                )
                # Insert preview label into the layout right after think_container
                parent_layout = self.layout().itemAt(0).layout()  # top_row -> layout
                inner_col = None  # find the right_col ( QVBoxLayout inside top_row )
                for i in range(parent_layout.count()):
                    item = parent_layout.itemAt(i)
                    lay = item.layout()
                    if lay and isinstance(lay, QtWidgets.QVBoxLayout):
                        inner_col = lay
                        break
                if inner_col:
                    inner_col.insertWidget(2, self._collapse_preview)
            else:
                self._collapse_preview.setText(preview)
                self._collapse_preview.show()
        else:
            # Expand: show full text, hide preview, change arrow to ▼
            self.msg_label.setVisible(True)
            self._think_arrow.setText("▼")
            if hasattr(self, '_collapse_preview') and self._collapse_preview is not None:
                self._collapse_preview.hide()

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
        elif self.role != "user" and hasattr(self, "_copy_icon_widget") and _isValid(self._copy_icon_widget):
            # Gemini bubble copy icon feedback
            self._copy_icon_widget.set_color("#66bb6a")
            QtCore.QTimer.singleShot(1500, lambda: (
                self._copy_icon_widget.set_color("#888888") if _isValid(self._copy_icon_widget) else None
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
        super(_ThumbCard, self).__init__(parent)
        self._img_path = img_path
        # Fixed size to fit inside toolbar (toolbar 56px -> ~48px usable)
        self.setFixedSize(42, 42)
        self.setStyleSheet(
            "QFrame { background: #333333; border-radius: 6px; border: 1px solid #444444; }"
        )

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setAlignment(QtCore.Qt.AlignCenter)

        thumb_size = 34
        self._thumb = QtWidgets.QLabel()
        _file_exists = bool(img_path and os.path.isfile(img_path))
        if _file_exists:
            pix = QtGui.QPixmap(img_path)
            if not pix.isNull():
                pix = pix.scaled(thumb_size, thumb_size,
                    QtCore.Qt.KeepAspectRatioByExpanding,
                    QtCore.Qt.SmoothTransformation)
                if pix.width() > thumb_size or pix.height() > thumb_size:
                    cx = (pix.width() - thumb_size) // 2
                    cy = (pix.height() - thumb_size) // 2
                    pix = pix.copy(cx, cy, thumb_size, thumb_size)
                self._thumb.setPixmap(pix)
            else:
                self._thumb.setText("!")
                self._thumb.setStyleSheet("color:#f59e0b;font-size:18px;background:transparent;")
        else:
            self._thumb.setText("?")
            self._thumb.setStyleSheet("color:#666;font-size:16px;background:transparent;")
            self._thumb.setToolTip("Image file not found:\n{}".format(img_path))

        self._thumb.setFixedSize(thumb_size, thumb_size)
        self._thumb.setAlignment(QtCore.Qt.AlignCenter)
        self._thumb.setScaledContents(False)
        layout.addWidget(self._thumb)

        btn_size = 16
        self._remove_btn = QtWidgets.QPushButton("\u00d7", self)
        self._remove_btn.setFixedSize(btn_size, btn_size)
        br = btn_size // 2
        # Use QPalette instead of stylesheet (Nuke Qt can't parse rgba stylesheets)
        _btn_palette = QtGui.QPalette()
        _btn_palette.setColor(QtGui.QPalette.Button, QtGui.QColor(239, 68, 68, 200))
        _btn_palette.setColor(QtGui.QPalette.ButtonText, QtCore.Qt.white)
        self._remove_btn.setPalette(_btn_palette)
        self._remove_btn.setStyleSheet(
            "QPushButton{{border:none;border-radius:{}px;font-size:9px;font-weight:bold;}}".format(br))
        margin = 1
        self._remove_btn.move(42 - btn_size - margin, margin)
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
        self._scroll.setFixedHeight(42)
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
        self._add_btn.setFixedSize(34, 34)
        self._add_btn.setToolTip("Add local file (images & documents)")
        self._add_btn.setStyleSheet(
            "QPushButton { background: #2a2a2a; color: #888888; border: 1px solid #444444; "
            "border-radius: 6px; font-size: 20px; font-weight: bold; }"
            "QPushButton:hover { background: #333333; color: #bbbbbb; border-color: #666666; }"
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

        # Fix our own height — never grow beyond this
        self.setFixedHeight(48)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)

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
        # Each card is 42px wide, spacing is 4px between cards
        n = len(self._images)
        if n > 0:
            inner_w = n * 42 + (n - 1) * 4  # cards + spacing between them
        else:
            inner_w = 0
        print("[NB ImageStrip] _rebuild: calculated inner_w={} for {} cards".format(inner_w, n))
        self._inner.setFixedSize(inner_w, 42)

        # Scroll to the rightmost position so newest images are visible
        QtCore.QTimer.singleShot(0, lambda: self._scroll.horizontalScrollBar().setValue(
            self._scroll.horizontalScrollBar().maximum()))

        # Set overall strip height (fixed, matches toolbar)
        self.setFixedHeight(42)

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


class _DeleteButtonDelegate(QtWidgets.QStyledItemDelegate):
    """Custom delegate that paints a (x) delete button on the right of each list item."""

    def __init__(self, parent=None):
        super(_DeleteButtonDelegate, self).__init__(parent)
        self._hover_row = -1

    def sizeHint(self, option, index):
        size = super(_DeleteButtonDelegate, self).sizeHint(option, index)
        return QtCore.QSize(size.width(), max(size.height(), 36))

    def paint(self, painter, option, index):
        # Draw background (selected/hover)
        if option.state & QtWidgets.QStyle.State_Selected:
            painter.fillRect(option.rect, QtGui.QColor(64, 64, 64))
        elif option.state & QtWidgets.QStyle.State_MouseOver:
            painter.fillRect(option.rect, QtGui.QColor(56, 56, 56))
        else:
            painter.fillRect(option.rect, QtGui.QColor(42, 42, 42))

        # Draw text with padding
        text = index.data(QtCore.Qt.DisplayRole)
        text_rect = option.rect.adjusted(10, 0, -34, 0)  # left pad 10, right pad 34 for button
        painter.setPen(QtGui.QColor(224, 224, 224))
        font = painter.font()
        font.setPixelSize(13)
        painter.setFont(font)
        flags = QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft | QtCore.Qt.TextSingleLine
        painter.drawText(text_rect, flags, str(text))

        # Draw "x" delete button on the right
        btn_w = 24
        btn_h = 24
        btn_x = option.rect.right() - btn_w - 6
        btn_y = option_rect_center = option.rect.center().y() - btn_h // 2
        btn_rect = QtCore.QRect(btn_x, btn_y, btn_w, btn_h)

        row = index.row()
        is_hover = (row == self._hover_row)

        # Button circle background
        if is_hover:
            painter.setBrush(QtGui.QColor(192, 57, 43))  # red when hover over button area
        else:
            painter.setBrush(QtGui.QColor(60, 60, 60))
        painter.setPen(QtCore.Qt.NoPen)
        painter.drawEllipse(btn_rect)

        # "×" symbol
        painter.setPen(QtGui.QColor(255, 255, 255))
        font = painter.font()
        font.setPixelSize(14)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(btn_rect, QtCore.Qt.AlignCenter, "\u00d7")

        # Draw separator line below item (except last one)
        list_w = self.parent()
        if list_w and row < list_w.count() - 1:
            sep_y = option.rect.bottom() - 1
            painter.setPen(QtGui.QColor(51, 51, 51))  # #333333
            painter.drawLine(option.rect.left() + 8, sep_y, option.rect.right() - 8, sep_y)

    def set_hover_row(self, row):
        self._hover_row = row


class _SessionDropdown(QtWidgets.QPushButton):
    """Custom dropdown widget for session selection with per-item delete support.

    Replaces QComboBox so each session entry has a remove (x) button,
    and the popup always aligns to the bottom edge of the trigger button.
    """

    currentIndexChanged = QtCore.Signal(int)

    def __init__(self, parent=None):
        super(_SessionDropdown, self).__init__(parent)
        self._items = []  # list of (title, userData)
        self._current_idx = -1
        self._signals_blocked = False
        self._popup = None
        self._list_widget = None
        self._delegate = None

        # Style like a combobox
        self.setText("Select Session")
        self.setObjectName("sessionDropdown")
        self.setCursor(QtCore.Qt.PointingHandCursor)

        self.clicked.connect(self._show_popup)

    # ---- QComboBox-compatible API -------------------------------------------

    def blockSignals(self, b):
        self._signals_blocked = b
        return super(_SessionDropdown, self).blockSignals(b)

    def addItem(self, text, userData=None):
        self._items.append((text, userData))
        if self._current_idx < 0:
            self._current_idx = 0
            self.setText(text)

    def clear(self):
        self._items = []
        self._current_idx = -1
        self.setText("Select Session")

    def itemData(self, idx):
        if 0 <= idx < len(self._items):
            return self._items[idx][1]
        return None

    def findData(self, data):
        for i, (_, ud) in enumerate(self._items):
            if ud == data:
                return i
        return -1

    def setCurrentIndex(self, idx):
        if 0 <= idx < len(self._items):
            self._current_idx = idx
            self.setText(self._items[idx][0])

    def currentIndex(self):
        return self._current_idx

    # ---- Popup ---------------------------------------------------------------

    def _show_popup(self):
        if not self._items:
            return

        # Create popup on first use
        if self._popup is None:
            self._popup = QtWidgets.QFrame(self, QtCore.Qt.Popup | QtCore.Qt.FramelessWindowHint)
            self._popup.setAttribute(QtCore.Qt.WA_TranslucentBackground)

            layout = QtWidgets.QVBoxLayout(self._popup)
            layout.setContentsMargins(4, 4, 4, 0)
            layout.setSpacing(2)

            self._list_widget = QtWidgets.QListWidget()
            self._list_widget.setStyleSheet(
                "QListWidget { background-color: #2a2a2a; border: 1px solid #3a3a3a; "
                "border-radius: 6px; outline: none; padding-bottom: 0px; }"
                "QScrollBar:vertical { background: transparent; width: 8px; }"
                "QScrollBar::handle:vertical { background: #555555; border-radius: 4px; min-height: 20px; }"
                "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
                "QListWidget::item { border: none; padding: 0px; margin: 0px; border-radius: 4px; }"
            )
            self._list_widget.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
            self._list_widget.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
            # Force zero bottom padding on viewport
            self._list_widget.viewport().setContentsMargins(0, 0, 0, 0)
            # NO itemClicked here — all click handling done via eventFilter

            # Custom delegate with delete button
            self._delegate = _DeleteButtonDelegate(self._list_widget)
            self._list_widget.setItemDelegate(self._delegate)

            # Track mouse movement for hover effect on delete buttons
            self._list_widget.viewport().setMouseTracking(True)
            self._list_widget.viewport().installEventFilter(self)

            layout.addWidget(self._list_widget)

        # Populate / refresh items
        self._populate_list()

        # Position: anchor bottom-left of button to top-left of popup
        btn_rect = self.geometry()
        pos = self.mapToGlobal(QtCore.QPoint(0, btn_rect.height()))
        screen = QtWidgets.QApplication.primaryScreen().availableGeometry()

        # Dynamic height based on item count
        n_items = len(self._items)
        ITEM_H = 38
        if n_items > 7:
            popup_h = 300  # cap at ~7 items with scrollbar
        else:
            popup_h = n_items * ITEM_H + 2

        # If not enough room below, flip upward
        if pos.y() + popup_h > screen.bottom():
            pos = self.mapToGlobal(QtCore.QPoint(0, 0)) - QtCore.QPoint(0, popup_h)

        # Dynamic width: measure text to find optimal size
        fm = QtGui.QFontMetrics(self.font())
        max_text_w = max(fm.horizontalAdvance(t[0]) for t in self._items) if self._items else 0
        # Account for delete button area (36px right side) + padding (~60px)
        dynamic_w = max_text_w + 96  # padding left(12) + text + spacing + delete_btn(36) + padding_right(12) + margin
        popup_w = max(btn_rect.width(), min(dynamic_w, 400))  # between btn_width and 400px cap

        self._popup.move(pos)
        self._popup.setFixedSize(popup_w, popup_h)
        self._popup.show()
        self._popup.setFocus()

    def eventFilter(self, obj, event):
        """Track hover & handle all clicks: text area = select, right side = delete."""
        vp = getattr(self._list_widget, 'viewport', lambda: None)()
        if obj != vp:
            return super(_SessionDropdown, self).eventFilter(obj, event)

        if event.type() == QtCore.QEvent.MouseMove:
            pos = event.pos()
            item = self._list_widget.itemAt(pos)
            if item:
                self._delegate.set_hover_row(self._list_widget.row(item))
            else:
                self._delegate.set_hover_row(-1)
            vp.update()
            return False

        if event.type() == QtCore.QEvent.MouseButtonRelease and event.button() == QtCore.Qt.LeftButton:
            pos = event.pos()
            item = self._list_widget.itemAt(pos)
            if item:
                rect = self._list_widget.visualItemRect(item)
                # Delete button area: rightmost 36px
                btn_area = QtCore.QRect(rect.right() - 36, rect.y(), 36, rect.height())
                if btn_area.contains(pos):
                    # Clicked delete button
                    row = self._list_widget.row(item)
                    self._delete_item(row)
                    return True
                else:
                    # Clicked text → select session + close popup
                    row = self._list_widget.row(item)
                    if row >= 0 and row != self._current_idx:
                        self._current_idx = row
                        self.setText(self._items[row][0])
                        if not self._signals_blocked:
                            self.currentIndexChanged.emit(row)
                    self._popup.hide()
                    return True

        return super(_SessionDropdown, self).eventFilter(obj, event)

    def _populate_list(self):
        self._list_widget.clear()
        for i, (title, _) in enumerate(self._items):
            item = QtWidgets.QListWidgetItem(title)
            item.setData(QtCore.Qt.UserRole, i)
            # Dynamic height only; width is handled by popup setFixedSize
            item.setSizeHint(QtCore.QSize(-1, 38))
            self._list_widget.addItem(item)

    def _on_item_clicked(self, item):
        """Called by itemClicked signal — only switches session."""
        row = self._list_widget.row(item)
        if row >= 0 and row != self._current_idx:
            self._current_idx = row
            self.setText(self._items[row][0])
            if not self._signals_blocked:
                self.currentIndexChanged.emit(row)
        self._popup.hide()

    def _delete_item(self, idx):
        """Remove a session item by index."""
        if idx < 0 or idx >= len(self._items):
            return
        sid = self._items[idx][1]
        del self._items[idx]

        if len(self._items) == 0:
            self._current_idx = -1
            self.setText("Select Session")
        elif self._current_idx >= len(self._items):
            self._current_idx = len(self._items) - 1
            self.setText(self._items[self._current_idx][0])

        self._populate_list()
        self._popup.hide()

        # Notify parent panel to delete this session from storage
        parent = self.parent()
        while parent is not None:
            if hasattr(parent, '_session_deleted'):
                parent._session_deleted(sid)
                break
            parent = parent.parent()



class _ModelComboBox(QtWidgets.QComboBox):
    """QComboBox with dropdown list always aligned to bottom edge."""

    def showPopup(self):
        super(_ModelComboBox, self).showPopup()
        # Delay repositioning until after the popup is fully displayed
        QtCore.QTimer.singleShot(0, self._align_popup_to_bottom)

    def _align_popup_to_bottom(self):
        """Move popup so its top edge aligns with this widget's bottom edge."""
        # In PySide2, find the actual popup container window
        # Try multiple strategies to locate the dropdown list container
        popup = None
        view = self.view()
        if view:
            # Walk up from the list view to find the popup container
            w = view.parentWidget()
            while w is not None and w != self:
                # Check if this looks like the popup (top-level or frameless)
                if w.windowFlags() & QtCore.Qt.Popup or (
                    hasattr(w, 'isWindow') and w.isWindow()):
                    popup = w
                    break
                w = w.parentWidget()

        if not popup:
            return
        btn_rect = self.geometry()
        pos = self.mapToGlobal(QtCore.QPoint(0, btn_rect.height()))
        popup.move(pos)


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
        self._cancel_requested = False       # ESC cancellation flag (checked by stream thread)
        self._status_task_id = None          # global status bar task ID

        self._build_ui()
        self._setup_esc_shortcut()
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

        # ---- Top bar: + button + session dropdown ----
        top_bar = QtWidgets.QHBoxLayout()
        top_bar.setSpacing(8)

        # "+" new dialogue button (compact, icon-style)
        new_btn = QtWidgets.QPushButton("+")
        new_btn.setObjectName("newDialogueBtn")
        new_btn.setToolTip("New Dialogue")
        new_btn.clicked.connect(self._new_dialogue)
        top_bar.addWidget(new_btn)

        # Session selector (custom dropdown with delete support)
        self._session_combo = _SessionDropdown()
        self._session_combo.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self._session_combo.currentIndexChanged.connect(self._on_session_selected)
        top_bar.addWidget(self._session_combo)

        root_layout.addLayout(top_bar)

        # ---- Chat area ----
        self._scroll_area = _WheelScrollArea()
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self._scroll_area.setFocusPolicy(QtCore.Qt.StrongFocus)

        self._chat_container = QtWidgets.QWidget()
        self._chat_container.setStyleSheet("QWidget { background-color: #222222; }")
        self._chat_layout = QtWidgets.QVBoxLayout(self._chat_container)
        self._chat_layout.setContentsMargins(10, 8, 10, 15)
        self._chat_layout.setSpacing(10)
        self._chat_layout.addStretch()

        # Chat scroll area — dark background matching target UI
        self._scroll_area.setStyleSheet(
            "QScrollArea { background-color: #222222; border: none; }"
        )

        self._scroll_area.setWidget(self._chat_container)
        root_layout.addWidget(self._scroll_area, 1)

        # ---- Input section ----
        input_section = QtWidgets.QVBoxLayout()
        input_section.setSpacing(6)
        input_section.setContentsMargins(0, 4, 0, 0)

        # Row 1: "Input" label only
        input_label = QtWidgets.QLabel("Input")
        input_label.setObjectName("inputLabel")
        input_section.addWidget(input_label)

        # Row 2: Select + Paste + image strip + Model combo (inside a bordered container)
        toolbar_frame = QtWidgets.QFrame()
        toolbar_frame.setObjectName("inputToolbarFrame")
        toolbar_frame.setFixedHeight(56)
        toolbar_frame.setStyleSheet(
            "QFrame#inputToolbarFrame { background-color: #252525; border: 1px solid #555555; "
            "border-radius: 8px; }"
        )
        toolbar_row = QtWidgets.QHBoxLayout(toolbar_frame)
        toolbar_row.setContentsMargins(6, 4, 6, 4)
        toolbar_row.setSpacing(6)
        toolbar_row.setAlignment(QtCore.Qt.AlignVCenter)

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

        # Image strip (thumbnails + "+" add button)
        self._image_strip = ImageStrip(add_callback=self._select_image)
        toolbar_row.addWidget(self._image_strip, 1)

        # Model selector on the right
        self._model_combo = _ModelComboBox()
        self._model_combo.setFixedWidth(155)
        self._model_combo.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Preferred)
        for m in CHAT_MODELS:
            self._model_combo.addItem(m)
        self._model_combo.currentIndexChanged.connect(self._on_model_changed)
        toolbar_row.addWidget(self._model_combo)

        input_section.addWidget(toolbar_frame)

        # Row 2: Text input
        self._text_input = QtWidgets.QPlainTextEdit()
        self._text_input.setPlaceholderText("Please enter the question...")
        self._text_input.setFixedHeight(70)
        self._text_input.setStyleSheet(
            "QPlainTextEdit { background-color: #2a2a2a; border: none; "
            "border-radius: 6px; padding: 8px; color: #e0e0e0; font-size: 13px; }"
        )
        self._text_input.installEventFilter(self)
        input_section.addWidget(self._text_input)

        # Send button (gray style matching target)
        self._send_btn = QtWidgets.QPushButton("Send")
        self._send_btn.setObjectName("sendBtn")
        self._send_btn.clicked.connect(self._send_message)
        input_section.addWidget(self._send_btn)

        # Status label
        self._status_label = QtWidgets.QLabel("")
        self._status_label.setStyleSheet("color: #666666; font-size: 11px;")
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

    # ---- ESC shortcut to cancel streaming -----------------------------------

    def _setup_esc_shortcut(self):
        """Install a global ESC QShortcut on the panel so users can cancel
        an in-flight Gemini request at any time."""
        from ai_workflow.status_bar import task_progress_manager
        esc = QtWidgets.QShortcut(
            QtGui.QKeySequence(QtCore.Qt.Key_Escape), self
        )
        esc.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
        esc.activated.connect(self._cancel_streaming)

    def _cancel_streaming(self):
        """Called by ESC or programmatically. Sets cancellation flag and
        updates both the panel status label and the global status bar."""
        if not self._is_sending:
            return

        self._cancel_requested = True
        self._status_label.setText("Cancelling... (pressing ESC)")
        self._status_label.setStyleSheet("color: #ef4444; font-size: 11px;")

        # Update global status bar to show cancelling state
        if self._status_task_id:
            try:
                from ai_workflow.status_bar import task_progress_manager as _tpm
                _tpm.update_status(
                    self._status_task_id, "Cancelling...", progress=-1
                )
            except Exception:
                pass

    def keyPressEvent(self, event):
        """Handle ESC key press for cancellation (fallback when text input
        does not have focus)."""
        if event.key() == QtCore.Qt.Key_Escape and self._is_sending:
            self._cancel_streaming()
            event.accept()
            return
        super(GeminiChatPanel, self).keyPressEvent(event)

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

    def _session_deleted(self, session_id):
        """Callback from _SessionDropdown when user clicks (x) on a session item."""
        # Delete from storage
        self._session_mgr.delete_session(session_id)

        # If we deleted the current active session, switch
        if self._current_session and self._current_session.get("id") == session_id:
            self._current_session = None
            self._refresh_session_list()
            sessions = self._session_mgr.list_sessions()
            if sessions:
                self._load_session(sessions[0][0])
            else:
                self._new_dialogue()
        else:
            # Just refresh dropdown list
            self._refresh_session_list()

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
        text fills the available space instead of collapsing to minimum.

        Width strategy — all user bubbles use FIXED width to prevent
        Qt's size-hint shrinking from causing runaway line-wrapping:
        ── Set label max_width → evaluate collapse → decide mode:
        ├── Long/medium text (needs_collapse): setFixedWidth(~447) — wide bubble, 2 lines + ...
        └── Truly short text (single line): setFixedWidth(natural) — compact but stable
        """
        if role == "user":
            # Calculate usable width from the visible chat area
            area_w = self._scroll_area.viewport().width()
            # Max bubble width: ~80% of chat area for wider user bubbles
            max_bubble_w = max(200, int(area_w * 0.80))

            # Padding inside user bubble: contentsMargins(12,6,12,6) + spacing(6) ≈ 36
            inner_pad_user = 36
            label_max_w = max(80, max_bubble_w - inner_pad_user)

            if hasattr(bubble, 'msg_label'):
                lbl = bubble.msg_label

                # Step 1: Set label's maximumWidth FIRST so word-wrap works correctly
                lbl.setMaximumWidth(label_max_w)

                # Step 2: Re-evaluate collapsed text NOW (before deciding width strategy)
                if hasattr(bubble, '_apply_collapsed_text') and hasattr(bubble, '_full_text'):
                    bubble._apply_collapsed_text(bubble._full_text)

                # Step 3: Decide width strategy based on whether text needs collapsing
                needs_collapse = (
                    bubble._is_collapsed and
                    hasattr(bubble, '_full_text') and
                    bubble._needs_collapse(bubble._full_text)
                )

                if needs_collapse:
                    # Long text (collapsed): force WIDE fixed-width bubble so it stays
                    # wide & shows exactly 2 lines + ... with ▼ expand icon
                    bubble.setFixedWidth(min(max_bubble_w, 447))
                    lbl.setMinimumWidth(int(label_max_w * 0.5))
                else:
                    # Short text (no collapse): still use FIXED width (not Maximum!)
                    # to prevent Qt from shrinking the bubble and causing extra wrapping.
                    # Size it to natural text width + padding.
                    fm = QtGui.QFontMetrics(lbl.font())
                    full_text = bubble._full_text if hasattr(bubble, '_full_text') else ''
                    raw_lines = full_text.split('\n')
                    nat_w = 0
                    for rl in raw_lines:
                        w = fm.horizontalAdvance(rl) if hasattr(fm, 'horizontalAdvance') else fm.width(rl)
                        nat_w = max(nat_w, w)
                    ideal_bubble_w = min(max(nat_w + inner_pad_user + 8, 70), max_bubble_w)
                    bubble.setFixedWidth(int(ideal_bubble_w))

                _log.debug("[_insert_bubble_widget] role=user | "
                           "area_w=%d | max_bubble_w=%d | label_max_w=%d | "
                           "needs_collapse=%s | is_collapsed=%s",
                           area_w, max_bubble_w, label_max_w,
                           needs_collapse, bubble._is_collapsed)

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
        self._cancel_requested = False
        self._send_btn.setEnabled(False)
        self._send_btn.setText("Sending...")
        self._status_label.setText("Waiting for Gemini response...")

        # Register in global status bar (like NanoBanana / VEO do)
        try:
            from ai_workflow.status_bar import task_progress_manager
            self._status_task_id = task_progress_manager.add_task(
                "Gemini Chat", "image"
            )
            task_progress_manager.update_status(
                self._status_task_id, "Talking to Gemini...", progress=-1
            )
        except Exception:
            self._status_task_id = None

        # Create an empty assistant bubble for streaming (no blinking cursor)
        self._streaming_bubble = self._add_bubble("model", "")
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
                # Check if user pressed ESC to cancel
                if self._cancel_requested:
                    self._stream_finish(full_text or "(Cancelled)", error=True)
                    return
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
                self._streaming_bubble.set_text(accumulated_text)
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

            # Determine if this was a user cancellation
            was_cancelled = self._cancel_requested

            # Save assistant message to session (unless cancelled with no content)
            if self._current_session is not None and not (was_cancelled and not final_text.strip()):
                assistant_msg = {"role": "model", "text": final_text, "images": []}
                self._current_session["messages"].append(assistant_msg)
                self._session_mgr.save_session(self._current_session)

            # Clean up streaming state
            self._streaming_bubble = None
            self._streaming_text = ""
            self._is_sending = False
            self._cancel_requested = False

            # Reset send button
            self._send_btn.setEnabled(True)
            self._send_btn.setText("Send")

            # Update panel status label + global status bar
            if was_cancelled:
                self._status_label.setText("Cancelled by user")
                self._status_label.setStyleSheet("color: #ef4444; font-size: 11px;")
            elif error:
                self._status_label.setText("Error occurred")
                self._status_label.setStyleSheet("color: #ef4444; font-size: 11px;")
            else:
                self._status_label.setText("Response received")
                self._status_label.setStyleSheet("color: #666666; font-size: 11px;")

            # Complete / cancel global status bar task
            if self._status_task_id:
                try:
                    from ai_workflow.status_bar import task_progress_manager as _tpm
                    if was_cancelled:
                        _tpm.cancel_task(
                            self._status_task_id,
                            "Cancelled by user"
                        )
                    elif error:
                        _tpm.error_task(
                            self._status_task_id, final_text[:80]
                        )
                    else:
                        _tpm.complete_task(
                            self._status_task_id, "Done!"
                        )
                except Exception:
                    pass
                self._status_task_id = None

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
