"""
ui/style.py — Centralized stylesheet and color tokens for VaultPlay
"""

COLORS = {
    "bg":          "#0d0f14",
    "surface":     "#13161e",
    "surface2":    "#1a1e2a",
    "surface3":    "#222636",
    "border":      "rgba(255,255,255,0.07)",
    "accent":      "#e8c76a",
    "accent2":     "#5b8dee",
    "text":        "#e8eaf0",
    "text_muted":  "#6b7280",
    "text_dim":    "#9ca3af",
    "installed":   "#4ade80",
    "danger":      "#f87171",
}

STYLESHEET = f"""
* {{
    font-family: "DM Sans", "Segoe UI", sans-serif;
    color: {COLORS['text']};
}}

QMainWindow, QWidget {{
    background: {COLORS['bg']};
}}

QScrollArea, QScrollArea > QWidget > QWidget {{
    background: transparent;
    border: none;
}}

QScrollBar:vertical {{
    background: transparent;
    width: 6px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {COLORS['surface3']};
    border-radius: 3px;
    min-height: 30px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 6px;
}}
QScrollBar::handle:horizontal {{
    background: {COLORS['surface3']};
    border-radius: 3px;
}}

QLineEdit {{
    background: {COLORS['surface']};
    border: 1px solid {COLORS['border']};
    border-radius: 8px;
    padding: 7px 12px;
    color: {COLORS['text']};
    font-size: 13px;
}}
QLineEdit:focus {{
    border-color: rgba(255,255,255,0.18);
}}
QLineEdit::placeholder {{
    color: {COLORS['text_muted']};
}}

QComboBox {{
    background: {COLORS['surface2']};
    border: 1px solid {COLORS['border']};
    border-radius: 6px;
    padding: 7px 12px;
    color: {COLORS['text']};
    font-size: 13px;
}}
QComboBox:hover {{
    border-color: rgba(255,255,255,0.15);
}}
QComboBox::drop-down {{
    border: none;
    width: 20px;
}}
QComboBox QAbstractItemView {{
    background: {COLORS['surface2']};
    border: 1px solid {COLORS['border']};
    selection-background-color: {COLORS['surface3']};
    color: {COLORS['text']};
    padding: 4px;
}}

QPushButton {{
    background: {COLORS['surface2']};
    border: 1px solid {COLORS['border']};
    border-radius: 8px;
    padding: 8px 18px;
    color: {COLORS['text_dim']};
    font-size: 13px;
}}
QPushButton:hover {{
    background: {COLORS['surface3']};
    color: {COLORS['text']};
    border-color: rgba(255,255,255,0.15);
}}
QPushButton:pressed {{
    background: {COLORS['bg']};
}}

QCheckBox {{
    spacing: 8px;
    color: {COLORS['text_dim']};
    font-size: 13px;
}}
QCheckBox::indicator {{
    width: 15px;
    height: 15px;
    border-radius: 3px;
    border: 1px solid rgba(255,255,255,0.2);
    background: transparent;
}}
QCheckBox::indicator:checked {{
    background: {COLORS['accent']};
    border-color: {COLORS['accent']};
    image: url(none);
}}

QLabel {{
    background: transparent;
    border: none;
}}

QToolTip {{
    background: {COLORS['surface2']};
    color: {COLORS['text']};
    border: 1px solid {COLORS['border']};
    padding: 4px 8px;
    border-radius: 4px;
}}
"""


def accent_button_style(danger=False) -> str:
    color = COLORS["danger"] if danger else COLORS["accent"]
    text  = "#000000" if not danger else "#ffffff"
    hover = "#f0d47a" if not danger else "#fca5a5"
    return f"""
        QPushButton {{
            background: {color};
            border: none;
            border-radius: 8px;
            padding: 10px 24px;
            color: {text};
            font-family: Rajdhani, sans-serif;
            font-weight: 700;
            font-size: 14px;
            letter-spacing: 1.5px;
        }}
        QPushButton:hover {{
            background: {hover};
        }}
        QPushButton:disabled {{
            background: {COLORS['surface3']};
            color: {COLORS['text_muted']};
        }}
    """


def card_style() -> str:
    return f"""
        QFrame {{
            background: {COLORS['surface']};
            border: 1px solid {COLORS['border']};
            border-radius: 12px;
        }}
    """
