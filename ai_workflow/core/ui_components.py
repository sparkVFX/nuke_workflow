"""
Shared UI components: DropDownComboBox, dark theme style sheets.
"""

from ai_workflow.core.pyside_compat import QtWidgets, QtCore


# ---------------------------------------------------------------------------
# DropDownComboBox — QComboBox that shows popup below widget
# ---------------------------------------------------------------------------
class DropDownComboBox(QtWidgets.QComboBox):
    """QComboBox that always shows popup below the widget (not covering it)."""

    def showPopup(self):
        super(DropDownComboBox, self).showPopup()
        popup = self.view().window()
        pos = self.mapToGlobal(QtCore.QPoint(0, self.height()))
        popup.move(pos)


# ---------------------------------------------------------------------------
# Shared Dark Theme Style Sheet (yellow accents — used by NanoBanana & VEO)
# ---------------------------------------------------------------------------
SHARED_DARK_STYLE = """
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
