"""
Media Browser — Collect & display all Nano Viewer / VEO Viewer nodes as cards.
Click a card to select & zoom to that node in the Nuke DAG.
"""

from __future__ import division

import os
import subprocess
import tempfile
import time
import threading
import nuke
import nukescripts

from ai_workflow.core.pyside_compat import QtWidgets, QtCore, QtGui, _isValid
from ai_workflow.core.model_catalog import (
    NB_MODEL_OPTIONS,
    NB_RATIO_OPTIONS,
    NB_RESOLUTION_OPTIONS,
    VEO_MODEL_OPTIONS,
    VEO_RATIO_OPTIONS,
    VEO_RESOLUTION_OPTIONS,
    VEO_DURATION_OPTIONS,
    VEO_MODE_OPTIONS,
    fill_combo_from_options,
)



# ---------------------------------------------------------------------------
# Style (consistent with NanoBanana / VEO dark theme)
# ---------------------------------------------------------------------------

MEDIA_BROWSER_STYLE = """
QFrame#mediaBrowserRoot {
    background-color: #1e1e1e;
}
QLabel#titleLabel {
    color: #facc15;
    font-size: 16px;
    font-weight: bold;
    background: transparent;
}
QLabel#subtitleLabel {
    color: #888888;
    font-size: 11px;
    background: transparent;
}
QPushButton#refreshBtn {
    background-color: #333333;
    border: 1px solid #444444;
    border-radius: 4px;
    padding: 6px 12px;
    color: #eeeeee;
    font-weight: bold;
    font-size: 12px;
}
QPushButton#refreshBtn:hover {
    background-color: #444444;
    border-color: #facc15;
}
QPushButton#refreshBtn:pressed {
    background-color: #222222;
}
QLineEdit#searchEdit {
    background-color: #1a1a1a;
    border: 1px solid #333333;
    border-radius: 4px;
    padding: 5px 10px;
    color: #ffffff;
    selection-background-color: #facc15;
    selection-color: #000000;
}
QLineEdit#searchEdit:focus {
    border-color: #facc15;
}
QScrollArea {
    background-color: #1e1e1e;
    border: none;
}
/* Card style */
QFrame#mediaCard {
    background-color: #252525;
    border: 1px solid #333333;
    border-radius: 8px;
}
QFrame#mediaCard:hover {
    border-color: #666666;
}
QFrame#mediaCard[selected=\"true\"] {
    border: 2px solid #3b82f6;
}
QLabel#cardName {
    color: #ffffff;
    font-size: 13px;
    font-weight: bold;
    background: transparent;
    padding: 2px 0;
}
QLabel#cardTypeVideo {
    color: #ef4444;
    font-size: 10px;
    font-weight: bold;
    background: transparent;
    text-transform: uppercase;
}
QLabel#cardTypeImage {
    color: #3b82f6;
    font-size: 10px;
    font-weight: bold;
    background: transparent;
    text-transform: uppercase;
}
QLabel#cardInfo {
    color: #999999;
    font-size: 11px;
    background: transparent;
}
QLabel#cardPrompt {
    color: #aaaaaa;
    font-size: 11px;
    background: transparent;
}
QLabel#emptyLabel {
    color: #555555;
    font-size: 14px;
    background: transparent;
}
QLabel#countLabel {
    color: #777777;
    font-size: 11px;
    background: transparent;
}
/* Detail Panel Styles */
QFrame#detailRoot {
    background-color: #1e1e1e;
}
QPushButton#backBtn {
    background-color: transparent;
    border: 1px solid #444444;
    border-radius: 4px;
    padding: 6px 14px;
    color: #cccccc;
    font-size: 12px;
}
QPushButton#backBtn:hover {
    background-color: #333333;
    border-color: #facc15;
    color: #facc15;
}
QLabel#previewLabel {
    background-color: #0a0a0a;
    border: 1px solid #333333;
    border-radius: 8px;
}
QLabel#detailTitle {
    color: #facc15;
    font-size: 16px;
    font-weight: bold;
    background: transparent;
}
QFrame#recordFrame {
    background-color: #1a1a1a;
    border: 1px solid #444444;
    border-radius: 6px;
}
QTextEdit#promptEdit, QTextEdit#negPromptEdit {
    background-color: #1a1a1a;
    border: 1px solid #333333;
    border-radius: 4px;
    color: #eeeeee;
    padding: 6px;
    selection-background-color: #facc15;
    selection-color: #000000;
}
QLineEdit#seedInput {
    background-color: #1a1a1a;
    border: 1px solid #333333;
    border-radius: 4px;
    padding: 4px 8px;
    color: #eeeeee;
}
QComboBox#paramCombo {
    background-color: #2a2a2a;
    border: 1px solid #444444;
    border-radius: 4px;
    padding: 4px 8px;
    color: #eeeeee;
}
QComboBox#paramCombo::drop-down { border: none; width: 20px; }
QComboBox#paramCombo QAbstractItemView {
    background-color: #2a2a2a;
    color: #eeeeee;
    selection-background-color: #facc15;
    selection-color: #000000;
}
QCheckBox#randomChk {
    color: #aaaaaa;
    spacing: 5px;
    background: transparent;
}
QPushButton#regenBtn {
    background-color: #8b5cf6;
    color: #ffffff;
    border: none;
    border-radius: 4px;
    padding: 10px 16px;
    font-weight: bold;
    font-size: 13px;
}
QPushButton#regenBtn:hover {
    background-color: #a78bfa;
}
QPushButton#stopBtnDetail {
    background-color: #ef4444;
    color: #ffffff;
    border: none;
    border-radius: 4px;
    padding: 10px 16px;
    font-weight: bold;
    font-size: 13px;
}
"""


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _extract_video_thumb_pixmap(video_path, target_w, target_h, temp_tag):
    """Extract first frame with ffmpeg and return scaled QPixmap (or None)."""
    try:
        from ai_workflow.veo import _find_ffmpeg
        ffmpeg = _find_ffmpeg()
    except Exception:
        ffmpeg = None

    if not ffmpeg or not video_path or not os.path.isfile(video_path):
        return None

    out_path = os.path.join(
        tempfile.gettempdir(),
        "_mb_{}_{}.png".format(temp_tag, int(time.time())))

    cmd = [
        ffmpeg,
        "-y",
        "-i", video_path,
        "-vframes", "1",
        "-q:v", "2",
        out_path,
    ]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode != 0 or not os.path.isfile(out_path):
            return None

        pix = QtGui.QPixmap(out_path)
        if pix.isNull():
            return None

        return pix.scaled(
            target_w,
            target_h,
            QtCore.Qt.KeepAspectRatio,
            QtCore.Qt.SmoothTransformation,
        )
    except Exception:
        return None
    finally:
        try:
            if os.path.isfile(out_path):
                os.remove(out_path)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Media card widget
# ---------------------------------------------------------------------------


class MediaCard(QtWidgets.QFrame):
    """Single media item card showing thumbnail + metadata.

    Clicking selects the corresponding node in the DAG.
    Card size is set dynamically via resize_card() to fit responsive grid.
    """

    clicked = QtCore.Signal(str)  # node_name
    doubleClicked = QtCore.Signal(str, str)  # node_name, media_type

    # Base design dimensions (original fixed size)
    BASE_W = 220
    BASE_H = 260
    THUMB_BASE_W = 204
    THUMB_BASE_H = 140

    def __init__(self, node_name, media_type, file_path, parent=None):
        super(MediaCard, self).__init__(parent)
        self.setObjectName("mediaCard")
        self.node_name = node_name
        self.media_type = media_type  # "image" or "video"
        self.file_path = file_path or ""
        self.setCursor(QtCore.Qt.PointingHandCursor)

        self._main_layout = QtWidgets.QVBoxLayout(self)
        self._main_layout.setContentsMargins(8, 8, 8, 8)
        self._main_layout.setSpacing(6)

        # Thumbnail area (label with pixmap)
        self.thumb_label = QtWidgets.QLabel()
        self.thumb_label.setAlignment(QtCore.Qt.AlignCenter)
        self.thumb_label.setStyleSheet(
            "background-color: #1a1a1a; border-radius: 4px; border: 1px solid #2a2a2a;")
        self._main_layout.addWidget(self.thumb_label)

        # Type badge + Name row
        top_row = QtWidgets.QHBoxLayout()
        top_row.setSpacing(6)
        type_label = QtWidgets.QLabel()
        type_label.setObjectName("cardTypeVideo" if media_type == "video" else "cardTypeImage")
        icon_text = "\u25B6 VIDEO" if media_type == "video" else "\u25A0 IMAGE"
        type_label.setText(icon_text)
        top_row.addWidget(type_label)
        top_row.addStretch()

        name_label = QtWidgets.QLabel()
        name_label.setObjectName("cardName")
        self._name_label = name_label
        _display_name = node_name
        if len(_display_name) > 18:
            _display_name = _display_name[:17] + "\u2026"
        name_label.setText(_display_name)
        top_row.addWidget(name_label)
        self._main_layout.addLayout(top_row)

        # File / info line
        info_label = QtWidgets.QLabel()
        info_label.setObjectName("cardInfo")
        self._info_label = info_label
        _info = ""
        if file_path:
            _fname = os.path.basename(file_path)
            if len(_fname) > 30:
                _fname = _fname[:27] + "..."
            _info = _fname
            try:
                fsize = os.path.getsize(file_path)
                if fsize > 1048576:
                    _info += " ({:.1f} MB)".format(fsize / 1048576)
                elif fsize > 1024:
                    _info += " ({:.0f} KB)".format(fsize / 1024)
                else:
                    _info += " ({:.0f} B)".format(fsize)
            except Exception:
                pass
        info_label.setText(_info)
        info_label.setToolTip(file_path or "")
        self._main_layout.addWidget(info_label)

        self._main_layout.addStretch()

        # Set default base size (will be overridden by resize_card during layout)
        self.resize_card(self.BASE_W, self.BASE_H)

        # Load thumbnail
        self._load_thumbnail()

    def resize_card(self, card_w, card_h):
        """Dynamically resize card and its internal elements."""
        scale = card_w / self.BASE_W  # scaling ratio relative to base

        self.setFixedSize(int(card_w), int(card_h))

        thumb_w = max(10, int(self.THUMB_BASE_W * scale))
        thumb_h = max(10, int(self.THUMB_BASE_H * scale))
        self.thumb_label.setFixedSize(thumb_w, thumb_h)

        # Re-scale existing pixmap if present
        if self.thumb_label.pixmap() and not self.thumb_label.pixmap().isNull():
            pix = self.thumb_label.pixmap()
            scaled = pix.scaled(
                thumb_w, thumb_h,
                QtCore.Qt.KeepAspectRatio,
                QtCore.Qt.SmoothTransformation)
            self.thumb_label.setPixmap(scaled)

    def _load_thumbnail(self):
        """Try to load a preview image for this media."""
        if not self.file_path or not os.path.isfile(self.file_path):
            # No file — show placeholder
            self._show_placeholder()
            return

        ext = os.path.splitext(self.file_path)[1].lower()
        if ext in (".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".tga"):
            # Image file — load directly
            pix = QtGui.QPixmap(self.file_path)
            if not pix.isNull():
                tw = self.thumb_label.width()
                th = self.thumb_label.height()
                scaled = pix.scaled(
                    tw, th,
                    QtCore.Qt.KeepAspectRatio,
                    QtCore.Qt.SmoothTransformation)
                self.thumb_label.setPixmap(scaled)
                return
        elif ext in (".mov", ".mp4", ".avi"):
            # Video — show placeholder first, then render thumbnail lazily
            self._show_placeholder()
            self._thumb_pending = True
            return

        self._show_placeholder()

    def _extract_video_frame(self):
        """Extract first frame of video via ffmpeg (no Nuke render, no progress bar)."""
        try:
            ffmpeg = None
            try:
                from ai_workflow.veo import _find_ffmpeg
                ffmpeg = _find_ffmpeg()
            except Exception:
                pass

            if not ffmpeg:
                self._show_placeholder()
                return

            out_path = os.path.join(
                tempfile.gettempdir(),
                "_mb_thumb_{}_{}.png".format(
                    self.node_name.replace("/", "_"), int(time.time())))

            cmd = [
                ffmpeg,
                "-y",
                "-i", self.file_path,
                "-vframes", "1",
                "-q:v", "2",
                out_path,
            ]

            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )

            if result.returncode == 0 and os.path.isfile(out_path):
                pix = QtGui.QPixmap(out_path)
                if not pix.isNull():
                    tw = self.thumb_label.width()
                    th = self.thumb_label.height()
                    scaled = pix.scaled(
                        tw, th,
                        QtCore.Qt.KeepAspectRatio,
                        QtCore.Qt.SmoothTransformation)
                    self.thumb_label.setPixmap(scaled)
                try:
                    os.remove(out_path)
                except Exception:
                    pass
                return
        except Exception:
            pass
        self._show_placeholder()

    def _show_placeholder(self):
        """Show placeholder when no image is available."""
        tw = max(10, self.thumb_label.width())
        th = max(10, self.thumb_label.height())
        pm = QtGui.QPixmap(tw, th)
        pm.fill(QtGui.QColor("#1a1a1a"))
        painter = QtGui.QPainter(pm)
        pen = QtGui.QPen(QtGui.QColor("#444444"))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawRect(2, 2, tw - 4, th - 4)
        icon_color = QtGui.QColor("#555555") if self.media_type == "video" else QtGui.QColor("#3b82f6")
        painter.setPen(QtGui.QPen(icon_color))
        font = painter.font()
        font.setPixelSize(max(12, int(32 * (tw / self.THUMB_BASE_W))))
        painter.setFont(font)
        icon = "\u25B6" if self.media_type == "video" else "\u25A0"
        rect = painter.fontMetrics().boundingRect(icon)
        x = (tw - rect.width()) // 2
        y = (th - rect.height()) // 2
        painter.drawText(x, y + rect.height(), icon)
        painter.end()
        self.thumb_label.setPixmap(pm)

    def mousePressEvent(self, event):
        super(MediaCard, self).mousePressEvent(event)
        if event.button() == QtCore.Qt.LeftButton:
            self.clicked.emit(self.node_name)

    def mouseDoubleClickEvent(self, event):
        super(MediaCard, self).mouseDoubleClickEvent(event)
        if event.button() == QtCore.Qt.LeftButton:
            self.doubleClicked.emit(self.node_name, self.media_type)

    def set_selected(self, selected):
        """Set or clear the visual selected state (blue border via CSS property selector)."""
        self.setProperty("selected", "true" if selected else "false")
        # Re-apply stylesheet so the dynamic property takes effect
        self.style().unpolish(self)
        self.style().polish(self)




# ---------------------------------------------------------------------------
# Detail Panel — shown on card double-click (regeneration UI)
# ---------------------------------------------------------------------------

class MediaDetailPanel(QtWidgets.QWidget):
    """Full detail view for a media node: preview + editable params + regenerate.

    Two modes:
      - IMAGE mode: reuses NanoBananaWorker for image regeneration
      - VIDEO mode: reuses VeoWorker for video regeneration
    """

    def __init__(self, parent=None):
        super(MediaDetailPanel, self).__init__(parent)
        self.setObjectName("detailRoot")
        self.setStyleSheet(MEDIA_BROWSER_STYLE)
        self.node = None
        self.media_type = ""   # "image" or "video"
        self.current_worker = None

        # Settings / API key
        self._settings = None
        self._api_key = ""

        # Thumb refresh callback set by parent panel
        self._on_thumb_refresh_callback = None

        self._build_ui()

    # ------------------------------------------------------------------
    # Public entry point: load a node into this panel
    # ------------------------------------------------------------------
    def load_node(self, node_name, media_type):
        """Load node data and build appropriate UI mode."""
        node = nuke.toNode(node_name)
        if not node:
            return
        self.node = node
        self.media_type = media_type

        # Load settings & API key
        try:
            from ai_workflow.nanobanana import NanoBananaSettings
            self._settings = NanoBananaSettings()
            self._api_key = self._settings.api_key or ""
        except Exception:
            self._api_key = ""

        # Update title
        self._title_label.setText("{}  [{}]".format(node_name,
                                                     "IMAGE" if media_type == "image" else "VIDEO"))

        # Load preview image/video frame
        self._load_preview()

        # Build parameter UI based on type
        self._clear_param_area()
        if media_type == "image":
            self._build_image_params()
            self._load_nb_params_from_node()
        else:
            self._build_video_params()
            self._load_veo_params_from_node()

    def set_thumb_refresh_callback(self, cb):
        """Set callback(parent_panel) to trigger grid thumbnail refresh."""
        self._on_thumb_refresh_callback = cb

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        main = QtWidgets.QVBoxLayout(self)
        main.setContentsMargins(12, 10, 12, 10)
        main.setSpacing(8)

        # --- Header row: Back button + title ---
        header = QtWidgets.QHBoxLayout()
        back_btn = QtWidgets.QPushButton("\u2190  Back")
        back_btn.setObjectName("backBtn")
        back_btn.setCursor(QtCore.Qt.PointingHandCursor)
        back_btn.clicked.connect(self._on_back)
        header.addWidget(back_btn)

        self._title_label = QtWidgets.QLabel("")
        self._title_label.setObjectName("detailTitle")
        header.addWidget(self._title_label, 1)
        main.addLayout(header)

        # --- Preview area (scrollable large preview) ---
        preview_scroll = QtWidgets.QScrollArea()
        preview_scroll.setWidgetResizable(True)
        preview_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        preview_scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        preview_scroll.setMinimumHeight(200)

        self._preview_label = QtWidgets.QLabel()
        self._preview_label.setObjectName("previewLabel")
        self._preview_label.setAlignment(QtCore.Qt.AlignCenter)
        self._preview_label.setMinimumSize(300, 180)
        preview_scroll.setWidget(self._preview_label)
        main.addWidget(preview_scroll, stretch=1)

        # --- Parameter area (dynamic content) ---
        self._param_widget = QtWidgets.QWidget()
        self._param_layout = QtWidgets.QVBoxLayout(self._param_widget)
        self._param_layout.setContentsMargins(0, 0, 0, 0)
        self._param_layout.setSpacing(6)
        main.addWidget(self._param_widget)

    def _clear_param_area(self):
        """Remove all widgets from param area."""
        while self._param_layout.count():
            item = self._param_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    # ------------------------------------------------------------------
    # Preview loading
    # ------------------------------------------------------------------
    def _load_preview(self):
        """Load a large preview of the current node's file."""
        if not self.node:
            return
        file_path = ""
        knob_name = "nb_file" if self.media_type == "image" else "veo_file"
        if knob_name in self.node.knobs():
            file_path = self.node[knob_name].value()
        if not file_path or not os.path.isfile(file_path):
            self._show_preview_placeholder()
            return

        pw = max(300, self._preview_label.width())
        ph = max(180, int(pw * 0.6))

        if self.media_type == "video":
            self._extract_video_preview(file_path, pw, ph)
        else:
            try:
                pix = QtGui.QPixmap(file_path)
                if not pix.isNull():
                    scaled = pix.scaled(pw, ph, QtCore.Qt.KeepAspectRatio,
                                        QtCore.Qt.SmoothTransformation)
                    self._preview_label.setPixmap(scaled)
                else:
                    self._show_preview_placeholder()
            except Exception:
                self._show_preview_placeholder()

    def _show_preview_placeholder(self):
        pw = max(300, self._preview_label.width())
        ph = max(180, int(pw * 0.6))
        pm = QtGui.QPixmap(pw, ph)
        pm.fill(QtGui.QColor("#0a0a0a"))
        painter = QtGui.QPainter(pm)
        painter.setPen(QtGui.QPen(QtGui.QColor("#333333")))
        painter.drawRect(2, 2, pw - 4, ph - 4)
        font = painter.font()
        font.setPixelSize(24)
        painter.setFont(font)
        text = "No Preview"
        rect = painter.fontMetrics().boundingRect(text)
        x = (pw - rect.width()) // 2
        y = (ph - rect.height()) // 2
        painter.drawText(x, y + rect.height(), text)
        painter.end()
        self._preview_label.setPixmap(pm)

    def _extract_video_preview(self, video_path, target_w, target_h):
        """Extract first frame of video for detail preview."""
        tag = "detail_{}".format((self.node.name() if self.node else "unknown").replace("/", "_"))
        pix = _extract_video_thumb_pixmap(video_path, target_w, target_h, tag)
        if pix is not None:
            self._preview_label.setPixmap(pix)
            return
        self._show_preview_placeholder()


    # ------------------------------------------------------------------
    # IMAGE mode (NanoBanana) parameters
    # ------------------------------------------------------------------
    def _build_image_params(self):
        """Build NB-style parameter controls for image regeneration."""
        pl = self._param_layout

        # Record frame (read-only info)
        record = QtWidgets.QFrame()
        record.setObjectName("recordFrame")
        rec_lay = QtWidgets.QVBoxLayout(record)
        rec_lay.setSpacing(4)
        rec_lay.setContentsMargins(8, 8, 8, 8)

        hdr = QtWidgets.QLabel("Regenerate Image")
        hdr.setStyleSheet(
            "color: #facc15; font-weight: bold; font-size: 13px; background: transparent;")
        hdr.setAlignment(QtCore.Qt.AlignCenter)
        rec_lay.addWidget(hdr)

        info_row = QtWidgets.QHBoxLayout()
        info_row.setSpacing(8)
        label_css = "color: #888; font-size: 10px; font-weight: bold; background: transparent;"
        val_css = "color: #ccc; font-size: 11px; background: transparent;"
        self.nb_info_labels = {}
        for idx, (display, key) in enumerate([("Model", "model"), ("Ratio", "ratio"),
                                              ("Resolution", "res"), ("Seed", "seed")]):
            col = QtWidgets.QVBoxLayout()
            col.setSpacing(1)
            lbl = QtWidgets.QLabel(display)
            lbl.setStyleSheet(label_css)
            lbl.setAlignment(QtCore.Qt.AlignCenter)
            val = QtWidgets.QLabel("")
            val.setStyleSheet(val_css)
            val.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            col.addWidget(lbl)
            col.addWidget(val)
            info_row.addLayout(col)
            self.nb_info_labels[key] = val
            if idx < 3:
                sep = QtWidgets.QFrame()
                sep.setFrameShape(QtWidgets.QFrame.VLine)
                sep.setStyleSheet("color: #444;")
                info_row.addWidget(sep)
        rec_lay.addLayout(info_row)
        pl.addWidget(record)

        # Model combo
        self.nb_model_combo = QtWidgets.QComboBox()
        self.nb_model_combo.setObjectName("paramCombo")
        self.nb_model_combo.addItem("Gemini 3.1 Flash", "gemini-3.1-flash-image-preview")
        self.nb_model_combo.addItem("Gemini 3 Pro", "gemini-3-pro-image-preview")
        self.nb_model_combo.addItem("Gemini 2.5 Flash", "gemini-2.5-flash-image")
        self.nb_model_combo.addItem("Imagen 3.0", "imagen-3.0-generate-002")
        pl.addWidget(self.nb_model_combo)

        # Ratio + Resolution
        row2 = QtWidgets.QHBoxLayout()
        row2.setSpacing(6)
        self.nb_ratio_combo = QtWidgets.QComboBox()
        self.nb_ratio_combo.setObjectName("paramCombo")
        self.nb_ratio_combo.addItems(["Auto", "1:1", "16:9", "9:16", "4:3", "3:4"])
        self.nb_res_combo = QtWidgets.QComboBox()
        self.nb_res_combo.setObjectName("paramCombo")
        self.nb_res_combo.addItem("1K", "1K")
        self.nb_res_combo.addItem("2K", "2K")
        self.nb_res_combo.addItem("4K", "4K")
        row2.addWidget(self.nb_ratio_combo)
        row2.addWidget(self.nb_res_combo)
        pl.addLayout(row2)

        # Seed
        row3 = QtWidgets.QHBoxLayout()
        row3.setSpacing(6)
        seed_lbl = QtWidgets.QLabel("Seed:")
        seed_lbl.setStyleSheet("color: #aaa; font-size: 11px; background: transparent;")
        self.nb_seed_input = QtWidgets.QLineEdit()
        self.nb_seed_input.setObjectName("seedInput")
        self.nb_seed_input.setPlaceholderText("Random")
        self.nb_seed_input.setValidator(QtGui.QIntValidator())
        self.nb_seed_input.setEnabled(False)
        self.nb_random_chk = QtWidgets.QCheckBox("Random")
        self.nb_random_chk.setObjectName("randomChk")
        self.nb_random_chk.setChecked(True)
        self.nb_random_chk.toggled.connect(lambda c: self.nb_seed_input.setEnabled(not c))
        row3.addWidget(seed_lbl)
        row3.addWidget(self.nb_seed_input, 1)
        row3.addWidget(self.nb_random_chk)
        pl.addLayout(row3)

        # Prompt
        self.nb_prompt_edit = QtWidgets.QTextEdit()
        self.nb_prompt_edit.setObjectName("promptEdit")
        self.nb_prompt_edit.setPlaceholderText("Describe the image you want...")
        self.nb_prompt_edit.setMinimumHeight(80)
        pl.addWidget(self.nb_prompt_edit)

        # Negative prompt
        self.nb_neg_edit = QtWidgets.QTextEdit()
        self.nb_neg_edit.setObjectName("negPromptEdit")
        self.nb_neg_edit.setPlaceholderText("Negative prompt (optional)...")
        self.nb_neg_edit.setFixedHeight(60)
        pl.addWidget(self.nb_neg_edit)

        # Reference image strip
        try:
            from ai_workflow.gemini_chat import ImageStrip
            self.nb_ref_strip = ImageStrip(add_callback=self._add_nb_ref_image)
            self.nb_strip_container = QtWidgets.QWidget()
            strip_lay = QtWidgets.QVBoxLayout(self.nb_strip_container)
            strip_lay.setContentsMargins(0, 0, 0, 0)
            ref_lbl = QtWidgets.QLabel("Reference Images:")
            ref_lbl.setStyleSheet("color: #888; font-size: 11px; background: transparent;")
            strip_lay.addWidget(ref_lbl)
            strip_lay.addWidget(self.nb_ref_strip)
            pl.addWidget(self.nb_strip_container)
        except Exception:
            self.nb_ref_strip = None
            self.nb_strip_container = None

        # Regenerate button
        self.regen_btn = QtWidgets.QPushButton("REGENERATE IMAGE")
        self.regen_btn.setObjectName("regenBtn")
        self.regen_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.regen_btn.setMinimumHeight(40)
        self.regen_btn.clicked.connect(self._on_regenerate_image)
        pl.addWidget(self.regen_btn)

        # Progress bar + status
        self.pbar = QtWidgets.QProgressBar()
        self.pbar.setVisible(False)
        self.pbar.setTextVisible(False)
        self.pbar.setFixedHeight(6)
        pl.addWidget(self.pbar)

        self.status_lbl = QtWidgets.QLabel("")
        self.status_lbl.setStyleSheet("color: #888; font-size: 11px; background: transparent;")
        pl.addWidget(self.status_lbl)

    def _load_nb_params_from_node(self):
        """Read nb_* knobs from node into UI."""
        if not self.node:
            return
        n = self.node
        for key, knob in [("model", "nb_model"), ("ratio", "nb_ratio"),
                          ("res", "nb_resolution"), ("seed", "nb_seed")]:
            if knob in n.knobs():
                val = n[knob].value()
                if hasattr(self, 'nb_info_labels') and key in self.nb_info_labels:
                    self.nb_info_labels[key].setText(str(val))
        # Model combo
        if "nb_model" in n.knobs():
            mv = str(n["nb_model"].value())
            for i in range(self.nb_model_combo.count()):
                if self.nb_model_combo.itemData(i) == mv:
                    self.nb_model_combo.setCurrentIndex(i)
                    break
        # Ratio
        if "nb_ratio" in n.knobs():
            rv = str(n["nb_ratio"].value())
            idx = self.nb_ratio_combo.findText(rv)
            if idx >= 0:
                self.nb_ratio_combo.setCurrentIndex(idx)
        # Resolution
        if "nb_resolution" in n.knobs():
            rsv = str(n["nb_resolution"].value())
            idx = self.nb_res_combo.findText(rsv)
            if idx >= 0:
                self.nb_res_combo.setCurrentIndex(idx)
        # Seed
        if "nb_seed" in n.knobs():
            sv = int(n["nb_seed"].value()) or 0
            if sv <= 0:
                self.nb_random_chk.setChecked(True)
            else:
                self.nb_random_chk.setChecked(False)
                self.nb_seed_input.setText(str(sv))
        # Prompt
        if "nb_prompt" in n.knobs():
            self.nb_prompt_edit.setPlainText(n["nb_prompt"].value() or "")
        # Neg prompt
        if "nb_neg_prompt" in n.knobs():
            self.nb_neg_edit.setPlainText(n["nb_neg_prompt"].value() or "")
        # Reference images
        if hasattr(self, 'nb_ref_strip') and self.nb_ref_strip:
            try:
                from ai_workflow.nanobanana import _collect_input_image_paths
                paths = _collect_input_image_paths(n)
                for p in paths:
                    if os.path.exists(p):
                        self.nb_ref_strip.add_image(p)
            except Exception:
                pass

    def _add_nb_ref_image(self):
        fpath, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Add Reference Image", "",
            "Images (*.png *.jpg *.jpeg *.gif *.webp);;All Files (*)")
        if fpath and os.path.isfile(fpath):
            self.nb_ref_strip.add_image(fpath)

    # ------------------------------------------------------------------
    # VIDEO mode (VEO) parameters
    # ------------------------------------------------------------------
    def _build_video_params(self):
        """Build VEO-style parameter controls for video regeneration."""
        pl = self._param_layout

        # Record frame
        record = QtWidgets.QFrame()
        record.setObjectName("recordFrame")
        rec_lay = QtWidgets.QVBoxLayout(record)
        rec_lay.setSpacing(4)
        rec_lay.setContentsMargins(8, 8, 8, 8)

        hdr = QtWidgets.QLabel("Regenerate Video")
        hdr.setStyleSheet(
            "color: #facc15; font-weight: bold; font-size: 13px; background: transparent;")
        hdr.setAlignment(QtCore.Qt.AlignCenter)
        rec_lay.addWidget(hdr)

        info_row = QtWidgets.QHBoxLayout()
        info_row.setSpacing(8)
        label_css = "color: #888; font-size: 10px; font-weight: bold; background: transparent;"
        val_css = "color: #ccc; font-size: 11px; background: transparent;"
        self.veo_info_labels = {}
        for idx, (display, key) in enumerate([("Model", "model"), ("Ratio", "ratio"),
                                              ("Duration", "dur"), ("Resolution", "res")]):
            col = QtWidgets.QVBoxLayout()
            col.setSpacing(1)
            lbl = QtWidgets.QLabel(display)
            lbl.setStyleSheet(label_css)
            lbl.setAlignment(QtCore.Qt.AlignCenter)
            val = QtWidgets.QLabel("")
            val.setStyleSheet(val_css)
            col.addWidget(lbl)
            col.addWidget(val)
            info_row.addLayout(col)
            self.veo_info_labels[key] = val
            if idx < 3:
                sep = QtWidgets.QFrame()
                sep.setFrameShape(QtWidgets.QFrame.VLine)
                sep.setStyleSheet("color: #444;")
                info_row.addWidget(sep)
        rec_lay.addLayout(info_row)
        pl.addWidget(record)

        # Mode combo
        mode_row = QtWidgets.QHBoxLayout()
        mode_row.setSpacing(6)
        mode_lbl = QtWidgets.QLabel("Mode:")
        mode_lbl.setStyleSheet("color: #aaa; font-size: 11px; background: transparent;")
        self.veo_mode_combo = QtWidgets.QComboBox()
        self.veo_mode_combo.setObjectName("paramCombo")
        veo_mode_labels = {
            "Text": "Text to Video",
            "FirstFrame": "Image to Video (First Frame)",
            "Frames": "Image to Video (Frames)",
            "Ingredients": "Reference Ingredients",
        }
        for mode_name, mode_value in VEO_MODE_OPTIONS:
            self.veo_mode_combo.addItem(veo_mode_labels.get(mode_value, mode_name), mode_value)
        mode_row.addWidget(mode_lbl)
        mode_row.addWidget(self.veo_mode_combo, 1)
        pl.addLayout(mode_row)

        # Model combo
        self.veo_model_combo = QtWidgets.QComboBox()
        self.veo_model_combo.setObjectName("paramCombo")
        for model_name, _model_id in VEO_MODEL_OPTIONS:
            # Viewer 节点里保存的是显示名，保持兼容
            self.veo_model_combo.addItem(model_name, model_name)
        pl.addWidget(self.veo_model_combo)

        # Ratio + Duration + Resolution
        row2 = QtWidgets.QHBoxLayout()
        row2.setSpacing(6)
        self.veo_ratio_combo = QtWidgets.QComboBox()
        self.veo_ratio_combo.setObjectName("paramCombo")
        fill_combo_from_options(self.veo_ratio_combo, VEO_RATIO_OPTIONS)
        self.veo_dur_combo = QtWidgets.QComboBox()
        self.veo_dur_combo.setObjectName("paramCombo")
        for duration_name, duration_val in VEO_DURATION_OPTIONS:
            self.veo_dur_combo.addItem("{}s".format(duration_name), duration_val)
        self.veo_dur_combo.setCurrentIndex(max(0, self.veo_dur_combo.findData("8")))
        self.veo_res_combo = QtWidgets.QComboBox()
        self.veo_res_combo.setObjectName("paramCombo")
        fill_combo_from_options(self.veo_res_combo, VEO_RESOLUTION_OPTIONS)
        row2.addWidget(self.veo_ratio_combo)
        row2.addWidget(self.veo_dur_combo)
        row2.addWidget(self.veo_res_combo)
        pl.addLayout(row2)


        # Prompt
        self.veo_prompt_edit = QtWidgets.QTextEdit()
        self.veo_prompt_edit.setObjectName("promptEdit")
        self.veo_prompt_edit.setPlaceholderText("Describe the video you want...")
        self.veo_prompt_edit.setMinimumHeight(70)
        pl.addWidget(self.veo_prompt_edit)

        # Negative prompt
        self.veo_neg_edit = QtWidgets.QTextEdit()
        self.veo_neg_edit.setObjectName("negPromptEdit")
        self.veo_neg_edit.setPlaceholderText("Negative prompt (optional)...")
        self.veo_neg_edit.setFixedHeight(50)
        pl.addWidget(self.veo_neg_edit)

        # Reference images
        try:
            from ai_workflow.gemini_chat import ImageStrip
            self.veo_ref_strip = ImageStrip(add_callback=self._add_veo_ref_image)
            self.veo_strip_container = QtWidgets.QWidget()
            strip_lay = QtWidgets.QVBoxLayout(self.veo_strip_container)
            strip_lay.setContentsMargins(0, 0, 0, 0)
            ref_lbl = QtWidgets.QLabel("Reference Images (FirstFrame/Frames/Ingredients):")
            ref_lbl.setStyleSheet("color: #888; font-size: 11px; background: transparent;")
            strip_lay.addWidget(ref_lbl)
            strip_lay.addWidget(self.veo_ref_strip)
            pl.addWidget(self.veo_strip_container)
        except Exception:
            self.veo_ref_strip = None
            self.veo_strip_container = None

        # Regenerate button
        self.regen_btn = QtWidgets.QPushButton("REGENERATE VIDEO")
        self.regen_btn.setObjectName("regenBtn")
        self.regen_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.regen_btn.setMinimumHeight(40)
        self.regen_btn.clicked.connect(self._on_regenerate_video)
        pl.addWidget(self.regen_btn)

        # Progress bar + status
        self.pbar = QtWidgets.QProgressBar()
        self.pbar.setVisible(False)
        self.pbar.setTextVisible(False)
        self.pbar.setFixedHeight(6)
        pl.addWidget(self.pbar)

        self.status_lbl = QtWidgets.QLabel("")
        self.status_lbl.setStyleSheet("color: #888; font-size: 11px; background: transparent;")
        pl.addWidget(self.status_lbl)

    def _load_veo_params_from_node(self):
        """Read veo_* knobs from node into UI."""
        if not self.node:
            return
        n = self.node
        veo_knob_map = {"model": "veo_model", "ratio": "veo_ratio",
                        "dur": "veo_duration", "res": "veo_resolution"}
        for ui_key, knob in veo_knob_map.items():
            if knob in n.knobs():
                val = n[knob].value()
                if hasattr(self, 'veo_info_labels') and ui_key in self.veo_info_labels:
                    self.veo_info_labels[ui_key].setText(str(val))

        # Model
        if "veo_model" in n.knobs():
            mv = str(n["veo_model"].value())
            idx = self.veo_model_combo.findText(mv)
            if idx >= 0:
                self.veo_model_combo.setCurrentIndex(idx)
        # Ratio
        if "veo_ratio" in n.knobs():
            rv = str(n["veo_ratio"].value())
            idx = self.veo_ratio_combo.findText(rv)
            if idx >= 0:
                self.veo_ratio_combo.setCurrentIndex(idx)
        # Duration
        if "veo_duration" in n.knobs():
            dv = str(n["veo_duration"].value()).replace("s", "")
            idx = self.veo_dur_combo.findData(dv)
            if idx >= 0:
                self.veo_dur_combo.setCurrentIndex(idx)
        # Resolution
        if "veo_resolution" in n.knobs():
            rsv = str(n["veo_resolution"].value())
            idx = self.veo_res_combo.findText(rsv)
            if idx >= 0:
                self.veo_res_combo.setCurrentIndex(idx)
        # Mode
        if "veo_mode" in n.knobs():
            mv = str(n["veo_mode"].value())
            idx = self.veo_mode_combo.findData(mv)
            if idx >= 0:
                self.veo_mode_combo.setCurrentIndex(idx)
        # Prompt
        if "veo_prompt" in n.knobs():
            self.veo_prompt_edit.setPlainText(n["veo_prompt"].value() or "")
        # Neg prompt
        if "veo_neg_prompt" in n.knobs():
            self.veo_neg_edit.setPlainText(n["veo_neg_prompt"].value() or "")
        # Ref images
        if hasattr(self, 'veo_ref_strip') and self.veo_ref_strip:
            try:
                from ai_workflow.nanobanana import _collect_input_image_paths
                paths = _collect_input_image_paths(n)
                for p in paths:
                    if os.path.exists(p):
                        self.veo_ref_strip.add_image(p)
            except Exception:
                pass

    def _add_veo_ref_image(self):
        fpath, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Add Reference Image", "",
            "Images (*.png *.jpg *.jpeg *.gif *.webp);;All Files (*)")
        if fpath and os.path.isfile(fpath):
            self.veo_ref_strip.add_image(fpath)

    # ------------------------------------------------------------------
    # Regeneration logic
    # ------------------------------------------------------------------
    def _toggle_generating(self, is_running):
        """Toggle UI state during generation."""
        if is_running:
            btn_text = "STOP IMAGE" if self.media_type == "image" else "STOP VIDEO"
            self.regen_btn.setText(btn_text)
            self.regen_btn.setObjectName("stopBtnDetail")
            self.regen_btn.setStyleSheet("")
            style = self.regen_btn.style()
            if style:
                style.unpolish(self.regen_btn)
                style.polish(self.regen_btn)
            self.pbar.setRange(0, 0)
            self.pbar.setVisible(True)
        else:
            btn_text = "REGENERATE IMAGE" if self.media_type == "image" else "REGENERATE VIDEO"
            self.regen_btn.setText(btn_text)
            self.regen_btn.setObjectName("regenBtn")
            self.regen_btn.setStyleSheet("")
            style = self.regen_btn.style()
            if style:
                style.unpolish(self.regen_btn)
                style.polish(self.regen_btn)
            self.current_worker = None
            self.pbar.setVisible(False)
            self.pbar.reset()

    def _save_nb_state_to_node(self):
        """Save current NB edit controls back to node knobs."""
        if not self.node:
            return
        try:
            n = self.node
            if "nb_model" in n.knobs():
                n["nb_model"].setValue(str(self.nb_model_combo.currentData()))
            if "nb_ratio" in n.knobs():
                n["nb_ratio"].setValue(self.nb_ratio_combo.currentText())
            if "nb_resolution" in n.knobs():
                n["nb_resolution"].setValue(self.nb_res_combo.currentText())
            if "nb_seed" in n.knobs():
                if self.nb_random_chk.isChecked():
                    n["nb_seed"].setValue(0)
                else:
                    try:
                        n["nb_seed"].setValue(int(self.nb_seed_input.text()) or 0)
                    except ValueError:
                        n["nb_seed"].setValue(0)
            if "nb_prompt" in n.knobs():
                n["nb_prompt"].setValue(self.nb_prompt_edit.toPlainText())
            if "nb_neg_prompt" in n.knobs():
                n["nb_neg_prompt"].setValue(self.nb_neg_edit.toPlainText())
        except Exception as e:
            print("[MediaDetail] Error saving NB state: {}".format(e))

    def _on_regenerate_image(self):
        """Start NanoBananaWorker for image regeneration."""
        if not self.node:
            nuke.message("No associated node.")
            return
        if not self._api_key:
            nuke.message("API key not set.\nPlease configure it in settings.")
            return

        self._save_nb_state_to_node()

        n = self.node
        model = ratio = resolution = prompt_text = neg_text = ""
        seed = 0

        for knob_key, target in [("nb_model", "model"), ("nb_ratio", "ratio"),
                                ("nb_resolution", "resolution"), ("nb_seed", "seed"),
                                ("nb_prompt", "prompt"), ("nb_neg_prompt", "neg")]:
            if knob_key in n.knobs():
                val = n[knob_key].value()
                if target == "model":
                    model = val or ""
                elif target == "ratio":
                    ratio = val or "auto"
                elif target == "resolution":
                    resolution = val or "1K"
                elif target == "seed":
                    seed = int(val) or 0
                elif target == "prompt":
                    prompt_text = val or ""
                elif target == "neg":
                    neg_text = val or ""

        if not model:
            nuke.message("No model set on this node.")
            return

        try:
            from ai_workflow.nanobanana import get_output_directory
            output_dir = get_output_directory()
        except Exception:
            output_dir = tempfile.gettempdir()

        if self.nb_random_chk.isChecked() or seed <= 0:
            import random as _rnd
            seed = _rnd.randint(1, 999999999)

        gen_name = "MB_Regen_{}".format(int(time.time()))
        if "nb_gen_name" in n.knobs():
            gen_name = n["nb_gen_name"].value() or gen_name

        # Collect reference images
        images_info = []
        if hasattr(self, 'nb_ref_strip') and self.nb_ref_strip:
            for idx, p in enumerate(self.nb_ref_strip.images):
                if p and os.path.exists(p):
                    images_info.append({
                        "index": idx, "name": "img{}".format(idx + 1),
                        "path": p, "connected": True, "node_name": "user_ref",
                    })

        self.status_lbl.setStyleSheet("color: #facc15; font-size: 11px;")
        self.status_lbl.setText("Generating image...")
        self._toggle_generating(True)

        try:
            from ai_workflow.nanobanana import NanoBananaWorker, _active_workers
        except ImportError as e:
            nuke.message("Cannot import NanoBananaWorker:\n{}".format(e))
            self._toggle_generating(False)
            return

        worker = NanoBananaWorker(
            model, prompt_text, neg_text, ratio, resolution, seed,
            images_info, output_dir, self._api_key,
            gen_name=gen_name
        )
        self.current_worker = worker
        worker_id = id(worker)
        _active_workers[worker_id] = {"worker": worker, "params": {}}

        widget_ref = self
        node_ref = self.node
        _refs = {"node": node_ref}

        def _direct_on_finished(path, metadata):
            s = metadata.get("seed", "N/A")

            def _update_ui():
                cur_node = _refs["node"]
                try:
                    widget_ref._toggle_generating(False)
                    widget_ref.status_lbl.setStyleSheet("color: #3CB371; font-size: 11px;")
                    widget_ref.status_lbl.setText("Done! Seed: {}".format(s))
                    if hasattr(widget_ref, 'nb_info_labels'):
                        widget_ref.nb_info_labels["seed"].setText(str(s))
                except Exception:
                    pass

                if path and os.path.exists(path):
                    try:
                        from ai_workflow.nanobanana import (
                            _get_internal_read_nb, _rebuild_group_for_thumbnail,
                            _update_node_thumbnail)
                        internal_read = _get_internal_read_nb(cur_node)
                        if internal_read:
                            internal_read["file"].fromUserText(path)
                            cur_node["nb_file"].setValue(path.replace("\\", "/"))
                            cur_node["nb_output_path"].setValue(path.replace("\\", "/"))
                            new_seed = metadata.get("seed", s)
                            if hasattr(widget_ref, 'nb_info_labels'):
                                widget_ref.nb_info_labels["seed"].setText(str(new_seed))
                            cur_node["nb_seed"].setValue(int(new_seed) if new_seed else 0)
                            rebuilt = _rebuild_group_for_thumbnail(cur_node, path)
                            if rebuilt:
                                _refs["node"] = rebuilt
                                widget_ref.node = rebuilt
                                cur_node = rebuilt
                                internal_read = _get_internal_read_nb(rebuilt)
                            else:
                                _update_node_thumbnail(cur_node, path)
                            if internal_read:
                                nuke.connectViewer(0, internal_read)
                            widget_ref._load_preview()
                            if widget_ref._on_thumb_refresh_callback:
                                widget_ref._on_thumb_refresh_callback()
                    except Exception as e:
                        print("[MediaDetail] Error updating after generation: {}".format(e))

            nuke.executeInMainThread(_update_ui)
            _active_workers.pop(worker_id, None)

        def _direct_on_error(err):
            def _update_ui():
                try:
                    widget_ref._toggle_generating(False)
                    widget_ref.status_lbl.setStyleSheet("color: #ef4444; font-size: 11px;")
                    widget_ref.status_lbl.setText("Error")
                except Exception:
                    pass
            nuke.executeInMainThread(_update_ui)
            _active_workers.pop(worker_id, None)
            nuke.executeInMainThread(nuke.message,
                                     args=("Regeneration Error:\n{}".format(err),))

        worker._on_finished_cb = _direct_on_finished
        worker._on_error_cb = _direct_on_error
        worker.start()

    def _on_regenerate_video(self):
        """Start VeoWorker for video regeneration."""
        if not self.node:
            nuke.message("No associated node.")
            return
        if not self._api_key:
            nuke.message("API key not set.\nPlease configure it in settings.")
            return

        # Save state to node
        try:
            n = self.node
            if "veo_model" in n.knobs():
                n["veo_model"].setValue(str(self.veo_model_combo.currentText()))
            if "veo_ratio" in n.knobs():
                n["veo_ratio"].setValue(self.veo_ratio_combo.currentText())
            if "veo_duration" in n.knobs():
                n["veo_duration"].setValue(self.veo_dur_combo.currentData())
            if "veo_resolution" in n.knobs():
                n["veo_resolution"].setValue(self.veo_res_combo.currentText())
            if "veo_mode" in n.knobs():
                n["veo_mode"].setValue(self.veo_mode_combo.currentData())
            if "veo_prompt" in n.knobs():
                n["veo_prompt"].setValue(self.veo_prompt_edit.toPlainText())
            if "veo_neg_prompt" in n.knobs():
                n["veo_neg_prompt"].setValue(self.veo_neg_edit.toPlainText())
        except Exception as e:
            print("[MediaDetail] Error saving VEO state: {}".format(e))

        n = self.node
        model = str(n["veo_model"].value()) if "veo_model" in n.knobs() else "Google VEO 3.1-Fast"
        prompt = str(n["veo_prompt"].value()) if "veo_prompt" in n.knobs() else ""
        neg_prompt = str(n["veo_neg_prompt"].value()) if "veo_neg_prompt" in n.knobs() else ""
        ratio = str(n["veo_ratio"].value()) if "veo_ratio" in n.knobs() else "16:9"
        duration = str(n["veo_duration"].value()).replace("s", "") if "veo_duration" in n.knobs() else "8"
        resolution = str(n["veo_resolution"].value()) if "veo_resolution" in n.knobs() else "720P"
        mode = str(n["veo_mode"].value()) if "veo_mode" in n.knobs() else "Text"

        ref_images = []
        if hasattr(self, 'veo_ref_strip') and self.veo_ref_strip:
            for p in self.veo_ref_strip.images:
                if p and os.path.exists(p):
                    ref_images.append(p)

        try:
            from ai_workflow.veo import get_output_directory as _get_veo_out
            output_dir = _get_veo_out()
        except Exception:
            output_dir = tempfile.gettempdir()

        self.status_lbl.setStyleSheet("color: #facc15; font-size: 11px;")
        self.status_lbl.setText("Generating video (may take several minutes)...")
        self._toggle_generating(True)

        try:
            from ai_workflow.veo import VeoWorker, _veo_active_workers
        except ImportError as e:
            nuke.message("Cannot import VeoWorker:\n{}".format(e))
            self._toggle_generating(False)
            return

        worker = VeoWorker(
            api_key=self._api_key,
            prompt=prompt,
            reference_image_paths=ref_images,
            model=model,
            aspect_ratio=ratio,
            duration=duration,
            resolution=resolution,
            mode=mode,
            negative_prompt=neg_prompt,
            temp_dir=output_dir,
            gen_name="MB_Veo_Regen_{}".format(int(time.time())),
        )
        self.current_worker = worker
        worker_id = id(worker)
        _veo_active_workers[worker_id] = {"worker": worker, "params": {}}

        widget_ref = self
        node_ref = self.node
        _refs = {"node": node_ref}

        def _veo_on_finished(video_path, metadata):
            def _update_ui():
                cur_node = _refs["node"]
                try:
                    widget_ref._toggle_generating(False)
                    widget_ref.status_lbl.setStyleSheet("color: #3CB371; font-size: 11px;")
                    widget_ref.status_lbl.setText("Video generated!")
                except Exception:
                    pass

                if video_path and os.path.exists(video_path):
                    try:
                        from ai_workflow.veo import (
                            _get_internal_read, _rebuild_veo_group_for_thumbnail)
                        ir = _get_internal_read(cur_node)
                        if ir:
                            ir["file"].fromUserText(video_path)
                            cur_node["veo_file"].setValue(video_path.replace("\\", "/"))
                            cur_node["veo_output_path"].setValue(video_path.replace("\\", "/"))
                            rebuilt = _rebuild_veo_group_for_thumbnail(
                                cur_node, video_path, duration)
                            if rebuilt:
                                _refs["node"] = rebuilt
                                widget_ref.node = rebuilt
                                cur_node = rebuilt
                            widget_ref._load_preview()
                            if widget_ref._on_thumb_refresh_callback:
                                widget_ref._on_thumb_refresh_callback()
                    except Exception as e:
                        print("[MediaDetail] Error updating VEO result: {}".format(e))

            nuke.executeInMainThread(_update_ui)
            _veo_active_workers.pop(worker_id, None)

        def _veo_on_error(err):
            def _update_ui():
                try:
                    widget_ref._toggle_generating(False)
                    widget_ref.status_lbl.setStyleSheet("color: #ef4444; font-size: 11px;")
                    widget_ref.status_lbl.setText("Error")
                except Exception:
                    pass
            nuke.executeInMainThread(_update_ui)
            _veo_active_workers.pop(worker_id, None)
            nuke.executeInMainThread(nuke.message,
                                     args=("Video Generation Error:\n{}".format(err),))

        worker._on_finished_cb = _veo_on_finished
        worker._on_error_cb = _veo_on_error
        worker.start()

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------
    def _on_back(self):
        if self.current_worker and getattr(self.current_worker, 'isRunning', lambda: False)():
            self.current_worker.stop()
        self.parentWidget()._back_to_grid()


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

class MediaBrowserPanel(QtWidgets.QWidget):
    """Panel that collects all NB Player / VEO Viewer nodes as clickable cards."""

    def __init__(self, parent=None):
        super(MediaBrowserPanel, self).__init__(parent)
        self.setStyleSheet(MEDIA_BROWSER_STYLE)
        self.setMinimumWidth(500)
        self.setMinimumHeight(400)
        self._cards = {}  # node_name -> MediaCard
        self._thumb_queue = []  # cards pending video thumbnail render
        self._thumb_timer = None
        self._resize_timer = None
        self._selected_node_name = ""  # track currently selected card for back-restore

        # Stacked layout: page 0 = grid, page 1 = detail panel
        self._stack = QtWidgets.QStackedLayout(self)
        self._build_ui_grid()   # builds into self._grid_page, adds to stack as index 0
        self._detail_panel = MediaDetailPanel()
        self._detail_panel.set_thumb_refresh_callback(self._refresh_single_thumbnail)
        self._stack.addWidget(self._detail_panel)  # index 1
        self._stack.setCurrentIndex(0)

        self.refresh()

    def _build_ui_grid(self):
        """Build the card grid page (page 0 of the stacked layout)."""
        self._grid_page = QtWidgets.QWidget()
        main = QtWidgets.QVBoxLayout(self._grid_page)
        main.setContentsMargins(12, 12, 12, 12)
        main.setSpacing(10)
        # IMPORTANT: must add to stack layout so it appears as index 0
        self._stack.insertWidget(0, self._grid_page)

        # --- Header ---
        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("\u25C6 Media Library")
        title.setObjectName("titleLabel")
        header.addWidget(title)
        header.addStretch()
        subtitle = QtWidgets.QLabel("Nano Viewer \u2022 VEO Viewer")
        subtitle.setObjectName("subtitleLabel")
        header.addWidget(subtitle)
        main.addLayout(header)

        # --- Search bar row ---
        search_row = QtWidgets.QHBoxLayout()
        search_edit = QtWidgets.QLineEdit()
        search_edit.setObjectName("searchEdit")
        search_edit.setPlaceholderText("Search by name or prompt...")
        search_edit.textChanged.connect(self._on_search_changed)
        search_row.addWidget(search_edit)
        refresh_btn = QtWidgets.QPushButton("Refresh")
        refresh_btn.setObjectName("refreshBtn")
        refresh_btn.setCursor(QtCore.Qt.PointingHandCursor)
        refresh_btn.clicked.connect(self.refresh)
        search_row.addWidget(refresh_btn)
        main.addLayout(search_row)

        # Count label
        count_label = QtWidgets.QLabel("")
        count_label.setObjectName("countLabel")
        main.addWidget(count_label)

        # Scroll area for cards
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.scroll_content = QtWidgets.QWidget()
        self.card_layout = QtWidgets.QGridLayout(self.scroll_content)
        self.card_layout.setSpacing(12)
        self.card_layout.setAlignment(QtCore.Qt.AlignTop)
        scroll.setWidget(self.scroll_content)
        main.addWidget(scroll, stretch=1)

        self.search_edit = search_edit
        self.count_label = count_label
        self.scroll_area = scroll

    def refresh(self):
        """Scan all nodes and rebuild the card grid with responsive sizing."""
        # Stop any pending thumbnail renders
        self._thumb_queue = []
        if self._thumb_timer is not None:
            self._thumb_timer.stop()
            self._thumb_timer = None

        # Clear existing cards
        while self.card_layout.count():
            item = self.card_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._cards.clear()

        items = self._collect_media_nodes()

        if not items:
            empty = QtWidgets.QLabel("No media viewers found.\nCreate Nano Viewer or VEO Viewer first.")
            empty.setObjectName("emptyLabel")
            empty.setAlignment(QtCore.Qt.AlignCenter)
            self.card_layout.addWidget(empty, 0, 0, QtCore.Qt.AlignCenter)
            self.count_label.setText("0 items")
            return

        # ---- Responsive grid calculation (AssetsManager-qt approach) ----
        spacing = self.card_layout.spacing()  # horizontal + vertical gap between cards
        margin_h = 16  # approximate horizontal margins of scroll_content (contentsMargins ~12*2)
        available_width = self.scroll_area.viewport().width()

        # Minimum card width — cards can shrink to this but not below
        MIN_CARD_W = 170
        # Base card dimensions (design reference)
        BASE_CARD_W = MediaCard.BASE_W   # 220
        BASE_CARD_H = MediaCard.BASE_H   # 260
        ASPECT = BASE_CARD_H / BASE_CARD_W  # height/width ratio

        # Step 1: determine column count from minimum cell size
        min_cell_w = MIN_CARD_W + spacing
        max_cols = max(1, int((available_width - margin_h) // min_cell_w))

        # Step 2: calculate exact cell width by evenly dividing usable area
        usable_w = available_width - margin_h
        cell_w = usable_w / max_cols          # float: each grid cell's width
        card_w = max(MIN_CARD_W, cell_w - spacing)  # actual card pixel width
        card_h = card_w * ASPECT              # maintain aspect ratio

        col = 0
        row = 0
        pending_video_cards = []
        for entry in items:
            card = MediaCard(
                node_name=entry["name"],
                media_type=entry["type"],
                file_path=entry["file"],
            )
            # Apply dynamic sizing BEFORE adding to layout
            card.resize_card(card_w, card_h)
            card.clicked.connect(self._on_card_clicked)
            card.doubleClicked.connect(self._on_card_double_clicked)
            self.card_layout.addWidget(card, row, col, QtCore.Qt.AlignCenter)
            self._cards[entry["name"]] = card
            # Collect video cards that need async thumbnail
            if getattr(card, '_thumb_pending', False):
                pending_video_cards.append(card)
            col += 1
            if col >= max_cols:
                col = 0
                row += 1

        # Make columns stretch evenly to fill full width
        for c in range(max_cols):
            self.card_layout.setColumnStretch(c, 1)

        video_count = sum(1 for i in items if i["type"] == "video")
        image_count = len(items) - video_count
        self.count_label.setText("{} total | {} videos  {} images".format(
            len(items), video_count, image_count))

        # Start async thumbnail rendering for video cards
        if pending_video_cards:
            self._thumb_queue = list(pending_video_cards)
            self._thumb_timer = QtCore.QTimer(self)
            self._thumb_timer.setSingleShot(True)
            self._thumb_timer.timeout.connect(self._render_next_thumb)
            self._thumb_timer.start(100)  # first thumb after 100ms

    def _collect_media_nodes(self):
        """Scan all Group nodes and collect NB Player / VEO Viewer entries."""
        results = []
        for node in nuke.allNodes("Group"):
            is_nb = "is_nb_player" in node.knobs() and node["is_nb_player"].value()
            is_veo = "is_veo_viewer" in node.knobs() and node["is_veo_viewer"].value()
            if not is_nb and not is_veo:
                continue

            media_type = "video" if is_veo else "image"
            file_path = ""

            if is_nb:
                if "nb_file" in node.knobs():
                    file_path = node["nb_file"].value()
            elif is_veo:
                if "veo_file" in node.knobs():
                    file_path = node["veo_file"].value()

            # Only include nodes that have an actual file loaded
            if not file_path:
                continue

            results.append({
                "name": node.name(),
                "type": media_type,
                "file": file_path,
                "node": node,
            })

        return results

    def _render_next_thumb(self):
        """Render thumbnail for next video card in the queue."""
        if not self._thumb_queue:
            self._thumb_timer = None
            return

        card = self._thumb_queue.pop(0)
        # Check card is still valid and needs thumbnail
        if not _isValid(card) or not getattr(card, '_thumb_pending', False):
            # Skip, move to next
            if self._thumb_queue:
                self._thumb_timer.start(50)
            else:
                self._thumb_timer = None
            return

        try:
            card._extract_video_frame()
        except Exception:
            pass
        card._thumb_pending = False

        # Schedule next card
        if self._thumb_queue:
            self._thumb_timer.start(100)  # 100ms between each render
        else:
            self._thumb_timer = None

    def _on_search_changed(self, text):
        """Filter cards based on search text."""
        query = text.strip().lower()
        visible_count = 0
        for name, card in self._cards.items():
            match = True
            if query:
                match = query in name.lower()
                if not match and card.file_path:
                    fname = os.path.basename(card.file_path).lower()
                    match = query in fname
            card.setVisible(match)
            if match:
                visible_count += 1
        self.count_label.setText("{} of {} shown".format(visible_count, len(self._cards)))

    def _on_card_clicked(self, node_name):
        """Select, zoom to, and connect clicked node to Viewer1."""
        self._set_selected_card(node_name)
        node = nuke.toNode(node_name)
        if node:
            # Select only this node, deselect others
            for n in nuke.allNodes():
                n.setSelected(False)
            node.setSelected(True)
            # Zoom to node in DAG view
            try:
                nuke.zoomToFitSelected()
            except Exception:
                pass
            # Connect node to Viewer1 (like pressing "1" in Nuke)
            try:
                viewer = nuke.toNode("Viewer1")
                if viewer:
                    viewer.setInput(0, node)
            except Exception:
                pass
            print("[Media Browser] Selected and viewed node '{}'".format(node_name))
        else:
            print("[Media Browser] WARNING: Node '{}' no longer exists".format(node_name))

    def _set_selected_card(self, node_name):
        """Update visual selection state on all cards."""
        for name, card in self._cards.items():
            card.set_selected(name == node_name)
        self._selected_node_name = node_name

    def _restore_selected_card(self):
        """After grid refresh, re-apply selection to the previously selected card."""
        if self._selected_node_name and self._selected_node_name in self._cards:
            self._cards[self._selected_node_name].set_selected(True)

    def _on_card_double_clicked(self, node_name, media_type):
        """Switch to detail panel for the double-clicked card."""
        self._set_selected_card(node_name)
        self._detail_panel.load_node(node_name, media_type)
        self._stack.setCurrentIndex(1)

    def _back_to_grid(self):
        """Return from detail panel to grid view."""
        self._stack.setCurrentIndex(0)
        # Trigger a refresh to update any changed thumbnails
        if self._cards:
            self.refresh()
            # Restore blue highlight on the previously selected card
            self._restore_selected_card()

    def _refresh_single_thumbnail(self):
        """Refresh a single card's thumbnail (called by detail panel after regen)."""
        # Re-scan and rebuild grid to pick up new file paths
        if self._stack.currentIndex() == 0:
            self.refresh()

    def resizeEvent(self, event):
        super(MediaBrowserPanel, self).resizeEvent(event)
        # Debounce re-layout to avoid excessive refreshes during drag
        if self._cards:
            if self._resize_timer is not None:
                self._resize_timer.stop()
            self._resize_timer = QtCore.QTimer(self)
            self._resize_timer.setSingleShot(True)
            self._resize_timer.timeout.connect(self._debounced_refresh)
            self._resize_timer.start(100)

    def _debounced_refresh(self):
        self._resize_timer = None
        if self._cards:
            self.refresh()

    def updateValue(self):
        """Called by Nuke knob system when panel is visible."""
        pass

    def makeUI(self):
        return self


# ---------------------------------------------------------------------------
# Registration helper (called by menu.py)
# ---------------------------------------------------------------------------

def _create_media_browser_widget():
    return MediaBrowserPanel()


def show_media_browser_panel():
    """Open the Media Library panel as a dockable tab inside an existing Nuke pane.
    It will appear next to Properties / Scene Graph etc., and can be dragged around.
    """
    panel_id = "ai_workflow.MediaBrowserPanel"

    # create=True returns a PythonPanel we can dock
    panel = nukescripts.panels.registerWidgetAsPanel(
        "ai_workflow.media_browser._create_media_browser_widget",
        "Media Library",
        panel_id,
        True,
    )

    if panel:
        # Find a suitable pane to dock into
        target_pane = None
        for pane_name in ("Properties.1", "Viewer.1", "DAG.1"):
            target_pane = nuke.getPaneFor(pane_name)
            if target_pane:
                break

        if target_pane:
            panel.addToPane(target_pane)
        else:
            panel.addToPane()
    return panel
