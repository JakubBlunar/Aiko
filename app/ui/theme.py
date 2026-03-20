"""Central palette and stylesheet for the assistant UI (light theme)."""
from __future__ import annotations

from PySide6.QtGui import QColor, QPalette

# Light-theme palette
WINDOW_BG = "#f1f5f9"
SURFACE_BG = "#ffffff"
INPUT_BG = "#ffffff"
TEXT_PRIMARY = "#0f172a"
TEXT_SECONDARY = "#475569"
BORDER = "#cbd5e1"
ACCENT = "#2563eb"
ACCENT_HOVER = "#1d4ed8"
DANGER = "#dc2626"
DANGER_HOVER = "#b91c1c"

# Chat bubbles (contrast-safe on light background)
BUBBLE_USER_BG = "#dbeafe"
BUBBLE_USER_TEXT = "#1e3a8a"
BUBBLE_USER_SPEAKER = "#1e40af"
BUBBLE_ASSISTANT_BG = "#dcfce7"
BUBBLE_ASSISTANT_TEXT = "#166534"
BUBBLE_ASSISTANT_SPEAKER = "#15803d"
BUBBLE_SYSTEM_BG = "#334155"
BUBBLE_SYSTEM_TEXT = "#f8fafc"
BUBBLE_SYSTEM_SPEAKER = "#e2e8f0"
BUBBLE_DEFAULT_BG = "#f1f5f9"
BUBBLE_DEFAULT_TEXT = "#0f172a"
BUBBLE_DEFAULT_SPEAKER = "#475569"

# Spacing (px)
CONTENT_MARGIN = 14
SPACING = 8
STATUS_STRIP_PADDING = 10

# Font
FONT_SIZE_BASE = 15
FONT_SIZE_SMALL = 12


def get_light_palette() -> QPalette:
    """Return a light-theme QPalette so all widgets use light colors (overrides OS dark mode)."""
    p = QPalette()
    window = QColor(WINDOW_BG)
    text = QColor(TEXT_PRIMARY)
    base = QColor(INPUT_BG)
    button = QColor(SURFACE_BG)
    p.setColor(QPalette.ColorRole.Window, window)
    p.setColor(QPalette.ColorRole.WindowText, text)
    p.setColor(QPalette.ColorRole.Base, base)
    p.setColor(QPalette.ColorRole.Text, text)
    p.setColor(QPalette.ColorRole.Button, button)
    p.setColor(QPalette.ColorRole.ButtonText, text)
    p.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(TEXT_SECONDARY))
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(TEXT_SECONDARY))
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(TEXT_SECONDARY))
    return p


def get_stylesheet() -> str:
    return f"""
QMainWindow, QDialog {{
    background-color: {WINDOW_BG};
}}
QWidget {{
    background-color: {WINDOW_BG};
    color: {TEXT_PRIMARY};
}}
QLabel {{
    background-color: transparent;
    color: {TEXT_PRIMARY};
    font-size: {FONT_SIZE_BASE}px;
}}
QTextEdit, QLineEdit, QPlainTextEdit, QComboBox {{
    background-color: {INPUT_BG};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 10px;
    font-size: {FONT_SIZE_BASE}px;
    selection-background-color: {ACCENT};
}}
QTextEdit:focus, QLineEdit:focus, QComboBox:focus {{
    border-color: {ACCENT};
}}
QComboBox::drop-down {{
    border: none;
    padding-right: 8px;
}}
QComboBox QAbstractItemView {{
    background-color: {INPUT_BG};
    color: {TEXT_PRIMARY};
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
    border: 1px solid {BORDER};
    outline: none;
}}
QComboBox QListView {{
    background-color: {INPUT_BG};
    color: {TEXT_PRIMARY};
}}
QMenu {{
    background-color: {INPUT_BG};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
}}
QMenu::item:selected {{
    background-color: {ACCENT};
    color: #ffffff;
}}
QPushButton {{
    background-color: {SURFACE_BG};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 8px 14px;
    font-size: {FONT_SIZE_BASE}px;
    min-width: 60px;
}}
QPushButton:hover {{
    background-color: #e2e8f0;
    border-color: #94a3b8;
}}
QPushButton:pressed {{
    background-color: #cbd5e1;
}}
QPushButton:disabled {{
    background-color: #f1f5f9;
    color: #94a3b8;
    border-color: #e2e8f0;
}}
QPushButton#sendButton {{
    background-color: {ACCENT};
    color: white;
    border-color: {ACCENT};
}}
QPushButton#sendButton:hover {{
    background-color: {ACCENT_HOVER};
    border-color: {ACCENT_HOVER};
}}
QPushButton#sendButton:pressed {{
    background-color: #1e40af;
}}
QPushButton#liveToggleButton {{
    background-color: {SURFACE_BG};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
}}
QPushButton#liveToggleButton:hover {{
    background-color: #e2e8f0;
}}
QPushButton#liveToggleButton[danger="true"] {{
    background-color: {DANGER};
    color: white;
    border-color: {DANGER};
}}
QPushButton#liveToggleButton[danger="true"]:hover {{
    background-color: {DANGER_HOVER};
    border-color: {DANGER_HOVER};
}}
QPushButton#clearChatButton, QPushButton#settingsButton {{
    background-color: {SURFACE_BG};
    color: {TEXT_SECONDARY};
}}
QGroupBox {{
    font-weight: bold;
    border: 1px solid {BORDER};
    border-radius: 6px;
    margin-top: 12px;
    padding-top: 10px;
    background-color: {WINDOW_BG};
}}
QGroupBox QWidget {{
    background-color: transparent;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    padding: 0 6px;
    background-color: {WINDOW_BG};
    color: {TEXT_PRIMARY};
}}
QTabWidget::pane {{
    border: 1px solid {BORDER};
    border-radius: 6px;
    top: -1px;
    background-color: {SURFACE_BG};
}}
QTabWidget QWidget {{
    background-color: transparent;
}}
QTabBar::tab {{
    background-color: #e2e8f0;
    color: {TEXT_SECONDARY};
    padding: 8px 16px;
    margin-right: 2px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
}}
QTabBar::tab:selected {{
    background-color: {SURFACE_BG};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-bottom: none;
}}
QProgressBar {{
    border: 1px solid {BORDER};
    border-radius: 4px;
    text-align: center;
    background-color: {SURFACE_BG};
}}
QProgressBar::chunk {{
    background-color: {ACCENT};
    border-radius: 3px;
}}
QScrollBar:vertical {{
    background-color: #f1f5f9;
    width: 12px;
    border-radius: 6px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background-color: #cbd5e1;
    border-radius: 6px;
    min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{
    background-color: #94a3b8;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QListWidget {{
    background-color: {INPUT_BG};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 4px;
    font-size: {FONT_SIZE_BASE}px;
    outline: none;
}}
QListWidget::item {{
    color: {TEXT_PRIMARY};
    padding: 4px 6px;
    border-radius: 4px;
}}
QListWidget::item:selected {{
    background-color: {ACCENT};
    color: #ffffff;
}}
QListWidget::item:hover:!selected {{
    background-color: #e2e8f0;
}}
QListWidget::item:alternate {{
    background-color: {WINDOW_BG};
}}
QSpinBox, QDoubleSpinBox {{
    background-color: {INPUT_BG};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 4px 8px;
    font-size: {FONT_SIZE_BASE}px;
}}
QSpinBox:focus, QDoubleSpinBox:focus {{
    border-color: {ACCENT};
}}
QSpinBox::up-button, QDoubleSpinBox::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    background-color: {SURFACE_BG};
    border: none;
    width: 16px;
}}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
    background-color: #e2e8f0;
}}
QCheckBox {{
    color: {TEXT_PRIMARY};
    font-size: {FONT_SIZE_BASE}px;
    spacing: 6px;
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {BORDER};
    border-radius: 3px;
    background-color: {INPUT_BG};
}}
QCheckBox::indicator:checked {{
    background-color: {ACCENT};
    border-color: {ACCENT};
}}
QCheckBox::indicator:hover {{
    border-color: {ACCENT};
}}
QMessageBox {{
    background-color: {WINDOW_BG};
}}
QMessageBox QLabel {{
    color: {TEXT_PRIMARY};
}}
QDialogButtonBox QPushButton {{
    min-width: 80px;
}}
#statusStrip {{
    background-color: #e2e8f0;
    color: {TEXT_SECONDARY};
    padding: {STATUS_STRIP_PADDING}px {CONTENT_MARGIN}px;
    font-size: {FONT_SIZE_SMALL}px;
    border-radius: 0 0 6px 6px;
    border-top: 1px solid {BORDER};
}}
QMainWindow > QWidget {{
    background-color: {WINDOW_BG};
}}
"""
