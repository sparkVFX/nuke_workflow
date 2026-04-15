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
    border: 2px solid #facc15;
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
"""


# ---------------------------------------------------------------------------
# Media card widget
# ---------------------------------------------------------------------------

class MediaCard(QtWidgets.QFrame):
    """Single media item card showing thumbnail + metadata.

    Clicking selects the corresponding node in the DAG.
    """

    clicked = QtCore.Signal(str)  # node_name

    def __init__(self, node_name, media_type, file_path, parent=None):
        super(MediaCard, self).__init__(parent)
        self.setObjectName("mediaCard")
        self.node_name = node_name
        self.media_type = media_type  # "image" or "video"
        self.file_path = file_path or ""
        self.setFixedSize(220, 260)
        self.setCursor(QtCore.Qt.PointingHandCursor)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Thumbnail area (label with pixmap)
        self.thumb_label = QtWidgets.QLabel()
        self.thumb_label.setAlignment(QtCore.Qt.AlignCenter)
        self.thumb_label.setMinimumSize(204, 140)
        self.thumb_label.setMaximumSize(204, 140)
        self.thumb_label.setStyleSheet(
            "background-color: #1a1a1a; border-radius: 4px; border: 1px solid #2a2a2a;")
        layout.addWidget(self.thumb_label)

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
        _display_name = node_name
        if len(_display_name) > 18:
            _display_name = _display_name[:17] + "\u2026"
        name_label.setText(_display_name)
        top_row.addWidget(name_label)
        layout.addLayout(top_row)

        # File / info line
        info_label = QtWidgets.QLabel()
        info_label.setObjectName("cardInfo")
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
        layout.addWidget(info_label)

        layout.addStretch()

        # Load thumbnail
        self._load_thumbnail()

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
                scaled = pix.scaled(
                    204, 140,
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
                    scaled = pix.scaled(
                        204, 140,
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
        pm = QtGui.QPixmap(204, 140)
        pm.fill(QtGui.QColor("#1a1a1a"))
        painter = QtGui.QPainter(pm)
        pen = QtGui.QPen(QtGui.QColor("#444444"))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawRect(2, 2, 200, 136)
        icon_color = QtGui.QColor("#555555") if self.media_type == "video" else QtGui.QColor("#3b82f6")
        painter.setPen(QtGui.QPen(icon_color))
        font = painter.font()
        font.setPixelSize(32)
        painter.setFont(font)
        icon = "\u25B6" if self.media_type == "video" else "\u25A0"
        rect = painter.fontMetrics().boundingRect(icon)
        x = (204 - rect.width()) // 2
        y = (140 - rect.height()) // 2
        painter.drawText(x, y + rect.height(), icon)
        painter.end()
        self.thumb_label.setPixmap(pm)

    def mousePressEvent(self, event):
        super(MediaCard, self).mousePressEvent(event)
        if event.button() == QtCore.Qt.LeftButton:
            self.clicked.emit(self.node_name)


# ---------------------------------------------------------------------------
# Main panel widget
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
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        main = QtWidgets.QVBoxLayout(self)
        main.setContentsMargins(12, 12, 12, 12)
        main.setSpacing(10)

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
        """Scan all nodes and rebuild the card grid."""
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

        col = 0
        row = 0
        available_width = self.scroll_area.viewport().width()
        card_w = 220  # MediaCard setFixedSize(220, 260)
        spacing = self.card_layout.spacing()
        unit = card_w + spacing  # one column unit width
        max_cols = max(1, available_width // unit)

        pending_video_cards = []
        for entry in items:
            card = MediaCard(
                node_name=entry["name"],
                media_type=entry["type"],
                file_path=entry["file"],
            )
            card.clicked.connect(self._on_card_clicked)
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

        # Force scroll_content geometry to sync with current viewport (shrink only)
        vp_w = self.scroll_area.viewport().width()
        if self.scroll_content.width() > vp_w:
            self.scroll_content.setGeometry(0, 0, vp_w, self.scroll_content.height())

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
