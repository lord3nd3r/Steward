"""Warm, quiet palette. Brass on ink, not a generic purple chat skin."""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

STYLESHEET = """
QMainWindow, QDialog, QWidget {
    background: #16130f;
    color: #f4efe6;
    font-size: 13px;
}
QMainWindow { background: #16130f; }
QWidget#sidebar { background: #1c1814; }
QWidget#chatPane, QWidget#activityPane { background: #16130f; }
QWidget#header {
    background: #16130f;
    border-bottom: 1px solid #3a332a;
}
QFrame#userBubble {
    background: #3d3122;
    border: 1px solid #5c4a32;
    border-radius: 12px;
}
QFrame#assistantBubble {
    background: #26211b;
    border: 1px solid #3a332a;
    border-radius: 12px;
}
QFrame#toolCard {
    background: #221e19;
    border: 1px solid #3a332a;
    border-radius: 10px;
}
QFrame#toolCardDenied {
    background: #2a1c19;
    border: 1px solid #6b3a32;
    border-radius: 10px;
}
QFrame#toolCardDanger {
    background: #2a1c19;
    border: 1px solid #a85a48;
    border-radius: 10px;
}
QLabel#wordmark {
    color: #f3e6d0;
    font-size: 22px;
}
QLabel#tagline, QLabel#muted, QLabel#persona {
    color: #b3a898;
}
QLabel#section {
    color: #8d8274;
    font-size: 11px;
    letter-spacing: 1px;
}
QLabel#chip {
    color: #1c140c;
    background: #e0a45a;
    border-radius: 8px;
    padding: 2px 8px;
    font-size: 11px;
}
QLabel#chipQuiet {
    color: #e7d7c0;
    background: #3a3128;
    border-radius: 8px;
    padding: 2px 8px;
    font-size: 11px;
}
QLabel#errorBanner {
    background: #4a2a24;
    color: #f6d2cb;
    border: 1px solid #8d3d32;
    border-radius: 8px;
    padding: 8px 10px;
}
QLabel#dangerBanner {
    background: #4a2a24;
    color: #f6d2cb;
    border-radius: 8px;
    padding: 8px 10px;
}
QListWidget {
    background: transparent;
    border: none;
    outline: none;
}
QListWidget::item {
    padding: 8px 10px;
    margin: 2px 8px;
    border-radius: 8px;
    border-left: 3px solid transparent;
}
QListWidget::item:selected {
    background: #3a2e1e;
    border-left: 3px solid #e0a45a;
    color: #f4efe6;
}
QListWidget::item:hover:!selected {
    background: #2a241c;
}
QTextBrowser {
    background: transparent;
    color: #f4efe6;
    border: none;
}
QPlainTextEdit, QLineEdit, QTextEdit, QComboBox, QSpinBox {
    background: #1c1814;
    color: #f4efe6;
    border: 1px solid #4a4036;
    border-radius: 8px;
    padding: 6px 8px;
    selection-background-color: #8a6430;
    selection-color: #fff8ee;
}
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background: #2a241e;
    color: #f4efe6;
    selection-background-color: #3a2e1e;
    border: 1px solid #4a4036;
}
QPushButton {
    background: #3a3128;
    color: #f4efe6;
    border: 1px solid #4d4338;
    border-radius: 8px;
    padding: 8px 14px;
}
QPushButton:hover { background: #4a3e32; }
QPushButton:disabled { color: #7d7468; background: #2a241c; }
QPushButton#primary {
    background: #e0a45a;
    color: #1c140c;
    border: none;
    font-weight: 600;
}
QPushButton#primary:hover { background: #e8b56e; }
QPushButton#danger {
    background: #8d3d32;
    color: #fff8f5;
    border: none;
}
QPushButton#danger:hover { background: #a34a3c; }
QPushButton#ghost {
    background: transparent;
    border: none;
    color: #e7d7c0;
    text-align: left;
    padding: 8px 12px;
}
QPushButton#ghost:hover { background: #2a241c; }
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 4px 2px;
}
QScrollBar::handle:vertical {
    background: #4a4036;
    border-radius: 4px;
    min-height: 24px;
}
QScrollBar::handle:vertical:hover { background: #6a5c4c; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal {
    background: transparent;
    height: 10px;
}
QScrollBar::handle:horizontal {
    background: #4a4036;
    border-radius: 4px;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QStatusBar { background: #1c1814; color: #b3a898; }
QMenuBar { background: #16130f; color: #f4efe6; padding: 2px 8px; }
QMenuBar::item { padding: 6px 12px; background: transparent; }
QMenuBar::item:selected { background: #3a2e1e; }
QMenu {
    background: #241f19;
    color: #f4efe6;
    border: 1px solid #4a4036;
    padding: 4px;
}
QMenu::item { padding: 6px 18px; }
QMenu::item:selected { background: #3a2e1e; }
QCheckBox { spacing: 8px; }
QSplitter::handle { background: #2a241c; }
QToolTip {
    background: #2a241e;
    color: #f4efe6;
    border: 1px solid #4a4036;
}
"""


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#16130f"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#f4efe6"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#1c1814"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#241f19"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#f4efe6"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#3a3128"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#f4efe6"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#e0a45a"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#1c140c"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#2a241e"))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor("#f4efe6"))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor("#8a7d6e"))
    app.setPalette(palette)
    app.setFont(QFont("Inter", 11))
    app.setStyleSheet(STYLESHEET)
