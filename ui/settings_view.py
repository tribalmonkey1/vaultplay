"""
ui/settings_view.py — Settings screen for VaultPlay

Six sections navigable via left nav:
  NAS Connection / Paths / API Keys / Wine+Lutris / Appearance / Scan & Cache
All settings save immediately on change.
"""


# ── AppImage path fix ─────────────────────────────────────────────────────────
import sys as _sys, os as _os
_appdir = _os.environ.get("APPDIR", "")
if _appdir:
    _bin = _os.path.join(_appdir, "usr", "bin")
    if _bin not in _sys.path:
        _sys.path.insert(0, _bin)
_here = _os.path.dirname(_os.path.abspath(__file__))
if _here not in _sys.path:
    _sys.path.insert(0, _here)
# Also add parent (for ui/ subpackage files to find top-level modules)
_parent = _os.path.dirname(_here)
if _parent not in _sys.path:
    _sys.path.insert(0, _parent)
# ─────────────────────────────────────────────────────────────────────────────

import logging
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QLineEdit, QComboBox, QStackedWidget, QScrollArea,
    QFileDialog, QSizePolicy, QCheckBox
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from PyQt6.QtGui import QFont, QColor

import json
import db
import protondb as protondb_mod
import scanner
from ui.style import COLORS, accent_button_style

log = logging.getLogger(__name__)


# ── Small reusable widgets ────────────────────────────────────────────────────

class SettingsRow(QWidget):
    def __init__(self, label: str, description: str = "", parent=None):
        super().__init__(parent)
        # Single outer vertical layout: [content row] + [separator]
        outer = QVBoxLayout(self)
        outer.setSpacing(0)
        outer.setContentsMargins(0, 0, 0, 0)

        # Content row
        content = QWidget()
        content.setStyleSheet("background: transparent;")
        row_layout = QHBoxLayout(content)
        row_layout.setContentsMargins(0, 12, 0, 12)
        row_layout.setSpacing(16)

        info = QVBoxLayout()
        info.setSpacing(2)
        info.setContentsMargins(0, 0, 0, 0)

        lbl = QLabel(label)
        lbl.setFont(QFont("DM Sans", 13, QFont.Weight.Medium))
        lbl.setStyleSheet(f"color: {COLORS['text']}; background: transparent;")
        info.addWidget(lbl)

        if description:
            desc = QLabel(description)
            desc.setFont(QFont("DM Sans", 10))
            desc.setStyleSheet(
                f"color: {COLORS['text_muted']}; background: transparent;")
            desc.setWordWrap(True)
            info.addWidget(desc)

        row_layout.addLayout(info, 1)

        self.control_layout = QHBoxLayout()
        self.control_layout.setContentsMargins(0, 0, 0, 0)
        self.control_layout.setSpacing(6)
        row_layout.addLayout(self.control_layout)

        outer.addWidget(content)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet("background: rgba(255,255,255,0.04); border: none;")
        outer.addWidget(sep)

    def add_control(self, widget: QWidget):
        self.control_layout.addWidget(widget)


class SettingsToggle(QWidget):
    changed = pyqtSignal(bool)

    def __init__(self, checked: bool = False, parent=None):
        super().__init__(parent)
        self.setFixedSize(44, 24)
        self._checked = checked
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update()

    def _update(self):
        # Background is drawn in paintEvent for proper antialiasing
        self.setStyleSheet("")
        self.update()

    def paintEvent(self, event):
        from PyQt6.QtGui import QPainter, QBrush, QPen
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Draw track background
        track_color = QColor(COLORS["accent"]) if self._checked else QColor("#3a3f52")
        p.setBrush(QBrush(track_color))
        p.setPen(QPen(QColor("#555a70"), 1))
        p.drawRoundedRect(0, 0, 44, 24, 12, 12)
        # Draw knob
        knob_color = QColor("#ffffff") if self._checked else QColor("#aaaaaa")
        p.setBrush(QBrush(knob_color))
        p.setPen(Qt.PenStyle.NoPen)
        x = 23 if self._checked else 3
        p.drawEllipse(x, 3, 18, 18)
        p.end()

    def mousePressEvent(self, event):
        self._checked = not self._checked
        self._update()
        self.changed.emit(self._checked)

    @property
    def checked(self) -> bool:
        return self._checked

    def set_checked(self, val: bool):
        self._checked = val
        self._update()


class PathEdit(QWidget):
    changed = pyqtSignal(str)

    def __init__(self, value: str = "", placeholder: str = "", parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.edit = QLineEdit(value)
        self.edit.setPlaceholderText(placeholder)
        self.edit.setFont(QFont("DM Mono", 10))
        self.edit.setStyleSheet(f"color: {COLORS['accent2']}; min-width: 220px;")
        self.edit.textChanged.connect(self.changed)
        layout.addWidget(self.edit)

        browse = QPushButton("…")
        browse.setFixedSize(30, 30)
        browse.setFont(QFont("DM Sans", 12))
        browse.setToolTip("Browse…")
        browse.clicked.connect(self._browse)
        layout.addWidget(browse)

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "Select Folder",
                                                self.edit.text())
        if path:
            self.edit.setText(path)

    @property
    def text(self) -> str:
        return self.edit.text()


class SectionHeader(QWidget):
    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 4)
        layout.setSpacing(0)

        lbl = QLabel(text.upper())
        lbl.setFont(QFont("DM Mono", 9))
        lbl.setStyleSheet(f"color: {COLORS['text_muted']}; letter-spacing: 2px;")
        layout.addWidget(lbl)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"background: {COLORS['border']}; border: none; max-height: 1px;")
        layout.addWidget(sep)


class ActionButton(QPushButton):
    def __init__(self, text: str, danger: bool = False, parent=None):
        super().__init__(text, parent)
        self.setFont(QFont("DM Sans", 11))
        color = COLORS["danger"] if danger else COLORS["text_dim"]
        border = "rgba(248,113,113,0.3)" if danger else COLORS["border"]
        hover_bg = "rgba(248,113,113,0.08)" if danger else COLORS["surface3"]
        self.setStyleSheet(f"""
            QPushButton {{
                background: {COLORS['surface2']};
                border: 1px solid {border};
                border-radius: 6px;
                color: {color};
                padding: 6px 14px;
            }}
            QPushButton:hover {{
                background: {hover_bg};
                color: {'#fca5a5' if danger else COLORS['text']};
            }}
        """)



# ── Multi-path list widget ────────────────────────────────────────────────────

class InstallPathList(QWidget):
    """
    Editable list of install paths.
    Shows each path as a row with a remove button.
    Has an Add button at the bottom with a browse dialog.
    """
    paths_changed = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)

        # Rows container
        self.rows_widget = QWidget()
        self.rows_widget.setStyleSheet("background: transparent;")
        self.rows_layout = QVBoxLayout(self.rows_widget)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(6)
        self._layout.addWidget(self.rows_widget)

        # Add button
        add_btn = QPushButton("📁  Add Install Path")
        add_btn.setFont(QFont("DM Sans", 11))
        add_btn.setStyleSheet(f"""
            QPushButton {{
                background: {COLORS['surface2']};
                border: 1px dashed rgba(255,255,255,0.15);
                border-radius: 8px;
                color: {COLORS['text_muted']};
                padding: 8px 16px;
                text-align: left;
            }}
            QPushButton:hover {{
                border-color: {COLORS['accent']};
                color: {COLORS['accent']};
            }}
        """)
        add_btn.clicked.connect(self._add_path)
        self._layout.addWidget(add_btn)

        self.refresh()

    def refresh(self):
        """Reload paths from DB and rebuild rows."""
        while self.rows_layout.count():
            item = self.rows_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for path in db.get_install_paths():
            self._add_row(path)

    def _add_row(self, path: str):
        is_first = self.rows_layout.count() == 0
        paths    = db.get_install_paths()
        is_last  = (len(paths) > 0 and path == paths[-1])

        row = QWidget()
        row.setStyleSheet(f"""
            QWidget {{
                background: {COLORS['surface2']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
            }}
        """)
        row_l = QHBoxLayout(row)
        row_l.setContentsMargins(12, 8, 8, 8)
        row_l.setSpacing(6)

        # Default star
        star = QLabel("★" if is_first else "  ")
        star.setFont(QFont("DM Sans", 12))
        star.setFixedWidth(18)
        star.setStyleSheet(
            f"color: {COLORS['accent']}; background: transparent; border: none;"
            if is_first else
            "color: transparent; background: transparent; border: none;"
        )
        star.setToolTip("Default path" if is_first else "")
        row_l.addWidget(star)

        path_lbl = QLabel(path)
        path_lbl.setFont(QFont("DM Mono", 10))
        path_lbl.setStyleSheet(
            f"color: {COLORS['accent2']}; background: transparent; border: none;")
        row_l.addWidget(path_lbl, 1)

        def _icon_btn(label, tip, fg, hover_fg, hover_border):
            b = QPushButton(label)
            b.setFixedHeight(28)
            b.setMinimumWidth(36)
            b.setToolTip(tip)
            b.setStyleSheet(f"""
                QPushButton {{
                    background: {COLORS['surface3']};
                    border: 1px solid {COLORS['border']};
                    border-radius: 4px;
                    color: {fg};
                    font-size: 13px;
                    padding: 0 6px;
                }}
                QPushButton:hover {{
                    background: {COLORS['surface']};
                    color: {hover_fg};
                    border-color: {hover_border};
                }}
            """)
            return b

        if not is_first:
            up = _icon_btn("↑ Up", "Move up (higher priority)",
                           COLORS['text_muted'], COLORS['text'], "rgba(255,255,255,0.25)")
            up.clicked.connect(lambda _, p=path: self._move_up(p))
            row_l.addWidget(up)

        if not is_last:
            dn = _icon_btn("↓ Down", "Move down (lower priority)",
                           COLORS['text_muted'], COLORS['text'], "rgba(255,255,255,0.25)")
            dn.clicked.connect(lambda _, p=path: self._move_down(p))
            row_l.addWidget(dn)

        rm = _icon_btn("✕ Remove", "Remove this path",
                       COLORS['danger'], "#fca5a5", "#f87171")
        rm.clicked.connect(lambda _, p=path: self._remove_path(p))
        row_l.addWidget(rm)

        self.rows_layout.addWidget(row)

    def _add_path(self):
        from PyQt6.QtWidgets import QFileDialog
        start = db.get_install_paths()[0] if db.get_install_paths() else str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Select Install Path", start)
        if chosen:
            paths = db.get_install_paths()
            if chosen not in paths:
                paths.append(chosen)
                db.set_install_paths(paths)
                self.refresh()
                self.paths_changed.emit(paths)

    def _remove_path(self, path: str):
        paths = db.get_install_paths()
        if path in paths:
            paths.remove(path)
            if not paths:
                paths = [str(Path.home() / "Games")]
            db.set_install_paths(paths)
            self.refresh()
            self.paths_changed.emit(paths)

    def _move_up(self, path: str):
        paths = db.get_install_paths()
        idx = paths.index(path) if path in paths else -1
        if idx > 0:
            paths[idx - 1], paths[idx] = paths[idx], paths[idx - 1]
            db.set_install_paths(paths)
            self.refresh()
            self.paths_changed.emit(paths)

    def _move_down(self, path: str):
        paths = db.get_install_paths()
        idx = paths.index(path) if path in paths else -1
        if 0 <= idx < len(paths) - 1:
            paths[idx], paths[idx + 1] = paths[idx + 1], paths[idx]
            db.set_install_paths(paths)
            self.refresh()
            self.paths_changed.emit(paths)


# ── Settings View ─────────────────────────────────────────────────────────────

def _safe_get(row, key, default=None):
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


class SettingsView(QWidget):
    back_requested    = pyqtSignal()
    nas_path_changed  = pyqtSignal(str)   # new NAS path → clear DB + rescan
    rescan_requested  = pyqtSignal(str)   # same path → rescan only, no clear
    reload_requested  = pyqtSignal()      # just refresh library display, no scan

    def __init__(self, parent=None):
        super().__init__(parent)
        self._widgets = {}   # key → widget for reading values

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Back bar
        back_bar = QWidget()
        back_bar.setFixedHeight(44)
        back_bar.setStyleSheet(f"background: {COLORS['surface']}; border-bottom: 1px solid {COLORS['border']};")
        back_layout = QHBoxLayout(back_bar)
        back_layout.setContentsMargins(20, 0, 20, 0)
        back_btn = QPushButton("← Back to Library")
        back_btn.setFont(QFont("DM Sans", 11))
        back_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; border: none; color: {COLORS['text_muted']}; padding: 0; }}
            QPushButton:hover {{ color: {COLORS['text']}; }}
        """)
        back_btn.clicked.connect(self.back_requested)
        back_layout.addWidget(back_btn)
        back_layout.addStretch()
        root.addWidget(back_bar)

        # Main two-column layout
        main = QHBoxLayout()
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)
        root.addLayout(main, 1)

        # Left nav
        self.nav = QWidget()
        self.nav.setFixedWidth(200)
        self.nav.setStyleSheet(f"background: {COLORS['surface']}; border-right: 1px solid {COLORS['border']};")
        nav_layout = QVBoxLayout(self.nav)
        nav_layout.setContentsMargins(0, 20, 0, 20)
        nav_layout.setSpacing(0)

        self._nav_items = []
        self._pages_stack = QStackedWidget()

        nav_defs = [
            ("🗄  NAS Connection", self._build_nas_page),
            ("📁  Paths",          self._build_paths_page),
            ("🗂  Categories",     self._build_categories_page),
            ("🔑  API Keys",       self._build_api_page),
            ("🍷  Wine / Lutris",  self._build_wine_page),
            ("🎨  Appearance",     self._build_appearance_page),
            ("🔄  Scan & Cache",   self._build_scan_page),
            ("🔔  Version Tracking", self._build_version_tracking_page),
            ("ℹ  About",           self._build_about_page),
        ]

        for i, (label, builder) in enumerate(nav_defs):
            btn = QPushButton(label)
            btn.setFont(QFont("DM Sans", 11))
            btn.setCheckable(False)
            btn.setProperty("page_idx", i)
            btn.clicked.connect(lambda _, idx=i: self._show_page(idx))
            btn.setStyleSheet(self._nav_style(False))
            self._nav_items.append(btn)
            nav_layout.addWidget(btn)
            page = builder()
            self._pages_stack.addWidget(page)

        nav_layout.addStretch()
        main.addWidget(self.nav)
        main.addWidget(self._pages_stack, 1)

        self._current_page = 0
        self._show_page(0)

    def load_settings(self):
        """Reload all widget values from DB."""
        s = db.get_all_settings()
        for key, widget in self._widgets.items():
            val = s.get(key, "")
            if isinstance(widget, QLineEdit):
                widget.setText(val)
            elif isinstance(widget, SettingsToggle):
                widget.set_checked(val == "true")
            elif isinstance(widget, QComboBox):
                idx = widget.findData(val)
                if idx >= 0:
                    widget.setCurrentIndex(idx)

        # Refresh NAS path edit (stored as instance var, not in _widgets)
        if hasattr(self, "nas_path_edit"):
            self.nas_path_edit.setText(s.get("nas_path", ""))

        # Refresh API key edits
        if hasattr(self, "_api_key_edits"):
            for key, edit in self._api_key_edits.items():
                edit.setText(s.get(key, ""))

        # Refresh install path list
        if hasattr(self, "path_list_widget"):
            self.path_list_widget.refresh()

        # Refresh category list
        if hasattr(self, "cat_list_layout"):
            self._refresh_category_list()

        # Refresh last scan result label
        if hasattr(self, "_last_scan_row"):
            result = db.get_setting("last_scan_result", "Never scanned")
            self._last_scan_row.findChild(QLabel, "scan_desc").setText(result) if False else None
            # Update via the stored reference
            if hasattr(self, "_last_scan_desc_lbl"):
                self._last_scan_desc_lbl.setText(
                    db.get_setting("last_scan_result", "Never scanned")
                )

        # Refresh ProtonDB status label
        self._refresh_pdb_status_label()

        # Refresh redistributables status
        self._refresh_steamcmd_status()
        self._refresh_redist_stats()

        # Refresh version tracking status
        self._refresh_version_status_label()
        if hasattr(self, "_ver_sites_container"):
            self._refresh_version_sites()

    def _show_page(self, idx: int):
        self._current_page = idx
        self._pages_stack.setCurrentIndex(idx)
        for i, btn in enumerate(self._nav_items):
            btn.setStyleSheet(self._nav_style(i == idx))

    def _nav_style(self, active: bool) -> str:
        if active:
            return f"""
                QPushButton {{
                    background: rgba(232,199,106,0.05);
                    border: none;
                    border-left: 2px solid {COLORS['accent']};
                    color: {COLORS['accent']};
                    padding: 10px 20px;
                    text-align: left;
                    font-weight: 500;
                }}
            """
        return f"""
            QPushButton {{
                background: transparent;
                border: none;
                border-left: 2px solid transparent;
                color: {COLORS['text_muted']};
                padding: 10px 20px;
                text-align: left;
            }}
            QPushButton:hover {{
                background: {COLORS['surface2']};
                color: {COLORS['text']};
            }}
        """

    def _scrollable_page(self, title: str) -> tuple[QScrollArea, QVBoxLayout]:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        container = QWidget()
        container.setStyleSheet(f"background: {COLORS['bg']};")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(48, 36, 48, 48)
        layout.setSpacing(0)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        title_lbl = QLabel(title)
        title_lbl.setFont(QFont("Rajdhani", 22, QFont.Weight.Bold))
        title_lbl.setStyleSheet(f"color: {COLORS['text']};")
        layout.addWidget(title_lbl)
        layout.addSpacing(28)

        scroll.setWidget(container)
        return scroll, layout

    def _make_setting_row(self, layout, key: str, label: str, desc: str,
                          widget: QWidget):
        row = SettingsRow(label, desc)
        row.add_control(widget)
        layout.addWidget(row)
        self._widgets[key] = widget

    def _save(self, key: str, value: str):
        db.set_setting(key, value)

    # ── NAS Connection ────────────────────────────────────────────────────────

    def _build_nas_page(self) -> QScrollArea:
        scroll, layout = self._scrollable_page("NAS Connection")

        layout.addWidget(SectionHeader("Connection"))

        # NAS path with Apply button and confirmation label
        nas_current = db.get_setting("nas_path", "")
        self.nas_path_edit = QLineEdit(nas_current)
        self.nas_path_edit.setFont(QFont("DM Mono", 10))
        self.nas_path_edit.setStyleSheet(f"color: {COLORS['accent2']}; min-width: 240px;")
        self.nas_path_edit.setPlaceholderText("/mnt/Games")
        nas_edit = self.nas_path_edit  # local alias for closures below

        nas_apply_btn = QPushButton("Apply")
        nas_apply_btn.setFont(QFont("DM Sans", 11, QFont.Weight.Medium))
        nas_apply_btn.setStyleSheet(f"""
            QPushButton {{
                background: {COLORS['accent']};
                border: none; border-radius: 6px;
                color: #000; padding: 6px 16px; font-weight: 600;
            }}
            QPushButton:hover {{ background: #f0d47a; }}
        """)

        nas_confirm = QLabel("")
        nas_confirm.setFont(QFont("DM Sans", 10))
        nas_confirm.setStyleSheet("color: #4ade80; background: transparent;")

        def _apply_nas_path():
            import os
            v = nas_edit.text().strip()
            if not v:
                nas_confirm.setText("⚠ Path is empty — not saved")
                nas_confirm.setStyleSheet(f"color: {COLORS['danger']}; background: transparent;")
                return
            if v == "/":
                nas_confirm.setText("⚠ Cannot use root (/) as NAS path")
                nas_confirm.setStyleSheet(f"color: {COLORS['danger']}; background: transparent;")
                return
            # Test connection immediately
            if os.path.exists(v) and os.path.isdir(v):
                nas_confirm.setText(f"✓ Connected: {v}")
                nas_confirm.setStyleSheet("color: #4ade80; background: transparent;")
            else:
                nas_confirm.setText(f"⚠ Path not found: {v} — saved anyway")
                nas_confirm.setStyleSheet(f"color: {COLORS['accent']}; background: transparent;")
            old_path = db.get_setting("nas_path", "")
            self._save("nas_path", v)
            if v != old_path:
                self.nas_path_changed.emit(v)
            # Show Scan Now prompt
            scan_prompt.setVisible(True)

        nas_apply_btn.clicked.connect(_apply_nas_path)

        nas_row = SettingsRow("NAS Games Path",
            "Path to your games folder on the NAS (e.g. /mnt/GoldenNAS/Games). "
            "Click Apply to save — the library will rescan automatically.")
        nas_row.add_control(nas_edit)
        nas_row.add_control(nas_apply_btn)
        layout.addWidget(nas_row)
        layout.addWidget(nas_confirm)

        scan_prompt = QWidget()
        scan_prompt.setVisible(False)
        scan_prompt.setStyleSheet("background: transparent;")
        sp_l = QHBoxLayout(scan_prompt)
        sp_l.setContentsMargins(0, 4, 0, 0)
        sp_l.setSpacing(10)
        sp_lbl = QLabel("Library not yet scanned for this path.")
        sp_lbl.setFont(QFont("DM Sans", 11))
        sp_lbl.setStyleSheet(f"color: {COLORS['text_muted']};")
        sp_l.addWidget(sp_lbl)
        scan_now_btn = QPushButton("Scan Now")
        scan_now_btn.setFont(QFont("DM Sans", 11, QFont.Weight.Medium))
        scan_now_btn.setStyleSheet(f"""
            QPushButton {{
                background: {COLORS['accent']}; border: none;
                border-radius: 6px; color: #000;
                padding: 5px 16px; font-weight: 600;
            }}
            QPushButton:hover {{ background: #f0d47a; }}
        """)
        def _scan_now():
            nas = nas_edit.text().strip()
            if nas and nas != "/":
                self.rescan_requested.emit(nas)
                scan_prompt.setVisible(False)
                nas_confirm.setText("✓ Scan started…")
        scan_now_btn.clicked.connect(_scan_now)
        sp_l.addWidget(scan_now_btn)
        sp_l.addStretch()
        layout.addWidget(scan_prompt)

        conn_combo = QComboBox()
        conn_combo.setFixedWidth(160)
        for label, val in [("SMB Share", "smb"), ("NFS Share", "nfs"), ("Local Mount", "local")]:
            conn_combo.addItem(label, val)
        idx = conn_combo.findData(db.get_setting("nas_connection_type", "smb"))
        conn_combo.setCurrentIndex(max(0, idx))
        conn_combo.currentIndexChanged.connect(
            lambda: self._save("nas_connection_type", conn_combo.currentData())
        )
        self._make_setting_row(layout, "nas_connection_type", "Connection Type",
                               "How the NAS is accessed on this machine", conn_combo)

        mount_toggle = SettingsToggle(db.get_setting("nas_auto_mount", "false") == "true")
        mount_toggle.changed.connect(lambda v: self._save("nas_auto_mount", "true" if v else "false"))
        self._make_setting_row(layout, "nas_auto_mount", "Auto-Mount on Launch",
                               "Mount the NAS share automatically when the app starts", mount_toggle)

        layout.addSpacing(24)
        layout.addWidget(SectionHeader("Connection Status"))

        status_row = SettingsRow("Status", "Last scan result")
        self.nas_status_label = QLabel("Not tested")
        self.nas_status_label.setFont(QFont("DM Mono", 10))
        self.nas_status_label.setStyleSheet(f"color: {COLORS['text_muted']};")
        test_btn = ActionButton("Test Connection")
        test_btn.clicked.connect(lambda: self._test_nas_connection(nas_edit.text()))
        status_row.add_control(self.nas_status_label)
        status_row.add_control(test_btn)
        layout.addWidget(status_row)

        # Apply NAS path change on focus lost / enter
        def _apply_nas():
            path = nas_edit.text().strip()
            if path:
                self.nas_path_changed.emit(path)
        nas_edit.editingFinished.connect(_apply_nas)

        layout.addStretch()
        return scroll

    def _test_nas_connection(self, path: str):
        import os
        if not path:
            self.nas_status_label.setText("No path configured")
            self.nas_status_label.setStyleSheet(f"color: {COLORS['text_muted']};")
            return
        ok = os.path.exists(path) and os.path.isdir(path)
        if ok:
            try:
                entries = len([e for e in os.scandir(path)])
                self.nas_status_label.setText(f"● Connected · {entries} entries")
                self.nas_status_label.setStyleSheet(f"color: {COLORS['installed']};")
            except Exception as e:
                self.nas_status_label.setText(f"Error: {e}")
                self.nas_status_label.setStyleSheet(f"color: {COLORS['danger']};")
        else:
            self.nas_status_label.setText("○ Not reachable")
            self.nas_status_label.setStyleSheet(f"color: {COLORS['danger']};")

    # ── Paths ─────────────────────────────────────────────────────────────────

    def _build_paths_page(self) -> QScrollArea:
        scroll, layout = self._scrollable_page("Paths")

        layout.addWidget(SectionHeader("Game Install Paths"))

        path_desc = QLabel(
            "These paths appear as options in the install dialog when installing "
            "portable games. Add as many as you like (e.g. different drives or "
            "partitions). The first path is the default."
        )
        path_desc.setFont(QFont("DM Sans", 11))
        path_desc.setStyleSheet(f"color: {COLORS['text_muted']}; padding-bottom: 8px;")
        path_desc.setWordWrap(True)
        layout.addWidget(path_desc)

        # The path list widget
        self.path_list_widget = InstallPathList()
        self.path_list_widget.paths_changed.connect(
            lambda paths: db.set_install_paths(paths)
        )
        layout.addWidget(self.path_list_widget)

        layout.addSpacing(24)
        layout.addWidget(SectionHeader("Other Paths"))

        for key, label, desc in [
            ("tmp_path", "Temp / Extraction Path",
             "Where archives are extracted before install. Cleaned up automatically if enabled."),
        ]:
            pe = PathEdit(db.get_setting(key, ""), label)
            pe.changed.connect(lambda v, k=key: self._save(k, v))
            self._make_setting_row(layout, key, label, desc, pe)

        cleanup_toggle = SettingsToggle(
            db.get_setting("auto_cleanup_tmp", "true") == "true")
        cleanup_toggle.changed.connect(
            lambda v: self._save("auto_cleanup_tmp", "true" if v else "false"))
        self._make_setting_row(layout, "auto_cleanup_tmp",
                               "Auto-cleanup Temp Files",
                               "Delete extracted archives after a successful install",
                               cleanup_toggle)

        layout.addSpacing(24)
        layout.addWidget(SectionHeader("Wine"))

        wine_prefix_info = QLabel(
            "Wine prefixes are always created at: ~/.local/share/wineprefixes/<prefix_name>"
        )
        wine_prefix_info.setFont(QFont("DM Mono", 10))
        wine_prefix_info.setStyleSheet(
            f"color: {COLORS['accent2']}; padding: 8px 0;")
        layout.addWidget(wine_prefix_info)

        layout.addStretch()
        return scroll

    # ── Categories ───────────────────────────────────────────────────────────

    def _build_categories_page(self) -> QScrollArea:
        scroll, layout = self._scrollable_page("Categories")

        desc = QLabel(
            "Each subfolder inside your NAS Games path becomes a category filter "
            "in the sidebar. You can rename categories for display purposes, or "
            "blacklist them to hide them from the library and skip them during scanning."
        )
        desc.setFont(QFont("DM Sans", 11))
        desc.setStyleSheet(f"color: {COLORS['text_muted']}; padding-bottom: 12px;")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        layout.addWidget(SectionHeader("Category List"))

        self.cat_list_container = QWidget()
        self.cat_list_container.setStyleSheet("background: transparent;")
        self.cat_list_layout = QVBoxLayout(self.cat_list_container)
        self.cat_list_layout.setContentsMargins(0, 0, 0, 0)
        self.cat_list_layout.setSpacing(6)
        layout.addWidget(self.cat_list_container)

        self._refresh_category_list()

        layout.addWidget(QLabel(""))  # spacer
        note = QLabel("Rescan your library after making changes here.")
        note.setFont(QFont("DM Sans", 10))
        note.setStyleSheet(f"color: {COLORS['text_muted']};")
        layout.addWidget(note)

        layout.addStretch()
        return scroll

    def _refresh_category_list(self):
        while self.cat_list_layout.count():
            item = self.cat_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        cats = db.get_all_categories()
        if not cats:
            empty = QLabel("No categories found yet. Run a scan first.")
            empty.setFont(QFont("DM Sans", 11))
            empty.setStyleSheet(f"color: {COLORS['text_muted']};")
            self.cat_list_layout.addWidget(empty)
            return

        active = [c for c in cats if not c["blacklisted"]]
        blisted = [c for c in cats if c["blacklisted"]]

        if active:
            lbl = QLabel("ACTIVE")
            lbl.setFont(QFont("DM Mono", 8))
            lbl.setStyleSheet(f"color: {COLORS['text_muted']}; letter-spacing: 2px; padding: 4px 0 2px 0;")
            self.cat_list_layout.addWidget(lbl)
            for cat in active:
                self.cat_list_layout.addWidget(self._make_category_row(cat))

        if blisted:
            lbl2 = QLabel("BLACKLISTED — skipped during scan, hidden from sidebar")
            lbl2.setFont(QFont("DM Mono", 8))
            lbl2.setStyleSheet(f"color: {COLORS['danger']}; letter-spacing: 1px; padding: 8px 0 2px 0;")
            self.cat_list_layout.addWidget(lbl2)
            for cat in blisted:
                self.cat_list_layout.addWidget(self._make_category_row(cat))

    def _make_category_row(self, cat) -> QWidget:
        is_blacklisted = bool(cat["blacklisted"])
        folder = cat["folder_name"]
        display = cat["display_name"]

        row = QWidget()
        row.setStyleSheet(f"""
            QWidget {{
                background: {'rgba(248,113,113,0.06)' if is_blacklisted else COLORS['surface2']};
                border: 1px solid {'rgba(248,113,113,0.3)' if is_blacklisted else COLORS['border']};
                border-radius: 8px;
            }}
        """)
        row_l = QHBoxLayout(row)
        row_l.setContentsMargins(12, 8, 8, 8)
        row_l.setSpacing(8)

        # Folder name (fixed)
        folder_lbl = QLabel(folder)
        folder_lbl.setFont(QFont("DM Mono", 9))
        folder_lbl.setFixedWidth(140)
        folder_lbl.setStyleSheet(
            f"color: {COLORS['text_muted']}; background: transparent; border: none;")
        row_l.addWidget(folder_lbl)

        # Editable display name
        name_edit = QLineEdit(display)
        name_edit.setFont(QFont("DM Sans", 11))
        name_edit.setStyleSheet(f"color: {COLORS['text']}; min-width: 160px;")
        name_edit.setPlaceholderText("Display name…")
        name_edit.editingFinished.connect(
            lambda f=folder, e=name_edit: db.rename_category(f, e.text().strip() or f)
        )
        row_l.addWidget(name_edit, 1)

        # Blacklist toggle button
        bl_btn = QPushButton("🚫 Blacklisted" if is_blacklisted else "Blacklist")
        bl_btn.setFont(QFont("DM Sans", 10))
        bl_btn.setCheckable(False)
        if is_blacklisted:
            bl_btn.setStyleSheet(f"""
                QPushButton {{
                    background: rgba(248,113,113,0.15);
                    border: 1px solid rgba(248,113,113,0.4);
                    border-radius: 6px; color: {COLORS['danger']};
                    padding: 4px 10px;
                }}
                QPushButton:hover {{
                    background: rgba(248,113,113,0.08);
                }}
            """)
        else:
            bl_btn.setStyleSheet(f"""
                QPushButton {{
                    background: {COLORS['surface3']};
                    border: 1px solid {COLORS['border']};
                    border-radius: 6px; color: {COLORS['text_muted']};
                    padding: 4px 10px;
                }}
                QPushButton:hover {{
                    color: {COLORS['danger']};
                    border-color: rgba(248,113,113,0.4);
                }}
            """)
        bl_btn.clicked.connect(
            lambda _, f=folder, bl=is_blacklisted: self._toggle_blacklist(f, not bl)
        )
        row_l.addWidget(bl_btn)

        return row

    def _toggle_blacklist(self, folder_name: str, blacklist: bool):
        db.set_category_blacklisted(folder_name, blacklist)
        self._refresh_category_list()
        # Tell main window to reload library display (no scan, no DB clear)
        self.reload_requested.emit()

    # ── API Keys ──────────────────────────────────────────────────────────────

    def _build_api_page(self) -> QScrollArea:
        scroll, layout = self._scrollable_page("API Keys")

        layout.addWidget(SectionHeader("SteamGridDB"))
        def _test_sgdb(key):
            import requests as _req
            try:
                r = _req.get("https://www.steamgriddb.com/api/v2/search/autocomplete/test",
                             headers={"Authorization": f"Bearer {key}"}, timeout=8)
                if r.status_code == 200:
                    return True, "Valid key — SteamGridDB connected"
                elif r.status_code == 401:
                    return False, "Invalid key (401 Unauthorized)"
                else:
                    return False, f"Unexpected response: {r.status_code}"
            except Exception as e:
                return False, f"Connection error: {e}"

        self._api_key_row(layout, "sgdb_api_key",
                          "API Key",
                          "Used for cover art, hero banners, logos and icons.  steamgriddb.com → Profile → API",
                          test_fn=_test_sgdb)
        art_combo = QComboBox()
        for label, val in [("Alternate / Fan Art", "alternate"),
                            ("Official", "official"),
                            ("Any", "")]:
            art_combo.addItem(label, val)
        idx = art_combo.findData(db.get_setting("sgdb_art_style", "alternate"))
        art_combo.setCurrentIndex(max(0, idx))
        art_combo.currentIndexChanged.connect(
            lambda: self._save("sgdb_art_style", art_combo.currentData())
        )
        self._make_setting_row(layout, "sgdb_art_style",
                               "Preferred Art Style",
                               "Which style of cover art to prefer when multiple are available",
                               art_combo)

        layout.addSpacing(24)
        layout.addWidget(SectionHeader("IGDB  (optional — enables descriptions, dates, genres)"))
        self._api_key_row(layout, "igdb_client_id",
                          "Client ID",
                          "From dev.twitch.tv — register your app for free")
        self._api_key_row(layout, "igdb_client_secret",
                          "Client Secret",
                          "Required alongside Client ID", secret=True)

        layout.addStretch()
        return scroll

    def _api_key_row(self, layout, key, label, desc, secret=False, test_fn=None):
        row_widget = QWidget()
        row_widget.setStyleSheet("background: transparent;")
        row_l = QVBoxLayout(row_widget)
        row_l.setContentsMargins(0, 0, 0, 0)
        row_l.setSpacing(4)

        input_row = QHBoxLayout()
        input_row.setContentsMargins(0, 0, 0, 0)
        input_row.setSpacing(8)

        edit = QLineEdit(db.get_setting(key, ""))
        edit.setFont(QFont("DM Mono", 10))
        edit.setStyleSheet(f"color: {COLORS['accent2']}; min-width: 200px;")
        edit.setPlaceholderText("Not configured")
        if secret:
            edit.setEchoMode(QLineEdit.EchoMode.Password)
        input_row.addWidget(edit, 1)

        # Store so load_settings() can update this field
        if not hasattr(self, "_api_key_edits"):
            self._api_key_edits = {}
        self._api_key_edits[key] = edit

        apply_btn = QPushButton("Apply")
        apply_btn.setFont(QFont("DM Sans", 10, QFont.Weight.Medium))
        apply_btn.setStyleSheet(f"""
            QPushButton {{
                background: {COLORS['accent']}; border: none;
                border-radius: 5px; color: #000;
                padding: 5px 12px; font-weight: 600;
            }}
            QPushButton:hover {{ background: #f0d47a; }}
        """)

        status_lbl = QLabel("")
        status_lbl.setFont(QFont("DM Sans", 10))
        status_lbl.setStyleSheet("color: transparent; background: transparent;")

        def _apply_key():
            v = edit.text().strip()
            self._save(key, v)
            if test_fn and v:
                status_lbl.setText("Testing…")
                status_lbl.setStyleSheet(f"color: {COLORS['text_muted']}; background: transparent;")
                ok, msg = test_fn(v)
                if ok:
                    status_lbl.setText(f"✓ {msg}")
                    status_lbl.setStyleSheet("color: #4ade80; background: transparent;")
                else:
                    status_lbl.setText(f"✗ {msg}")
                    status_lbl.setStyleSheet(f"color: {COLORS['danger']}; background: transparent;")
            elif v:
                status_lbl.setText("✓ Saved")
                status_lbl.setStyleSheet("color: #4ade80; background: transparent;")
            else:
                status_lbl.setText("Cleared")
                status_lbl.setStyleSheet(f"color: {COLORS['text_muted']}; background: transparent;")

        apply_btn.clicked.connect(_apply_key)
        input_row.addWidget(apply_btn)
        row_l.addLayout(input_row)
        row_l.addWidget(status_lbl)

        self._make_setting_row(layout, key, label, desc, row_widget)

    # ── Wine ─────────────────────────────────────────────────────────────────

    def _build_wine_page(self) -> QScrollArea:
        scroll, layout = self._scrollable_page("Wine")

        # Explanation
        explain = QLabel(
            "VaultPlay installs games using Wine prefixes. Each game gets its own "
            "isolated prefix at ~/.local/share/wineprefixes/<name>. The prefix is "
            "created with wineboot and redists installed via winetricks."
        )
        explain.setFont(QFont("DM Sans", 11))
        explain.setStyleSheet(f"color: {COLORS['text_muted']}; padding-bottom: 8px;")
        explain.setWordWrap(True)
        layout.addWidget(explain)

        layout.addWidget(SectionHeader("Defaults"))

        prefix_combo = QComboBox()
        for label, val in [("New prefix per game (recommended)", "per_game"),
                            ("Use default ~/.wine (shared)", "default")]:
            prefix_combo.addItem(label, val)
        idx = prefix_combo.findData(db.get_setting("default_prefix_mode", "per_game"))
        prefix_combo.setCurrentIndex(max(0, idx))
        prefix_combo.currentIndexChanged.connect(
            lambda: self._save("default_prefix_mode", prefix_combo.currentData())
        )
        self._make_setting_row(layout, "default_prefix_mode",
                               "Default Wine Prefix Mode",
                               "Per-game prefixes keep games isolated from each other.",
                               prefix_combo)

        layout.addSpacing(24)
        layout.addWidget(SectionHeader("Installed Proton / Wine Versions"))

        ver_note = QLabel(
            "Detected from ~/.local/share/lutris/runners/wine/, "
            "~/.steam/root/compatibilitytools.d/, and Steam's compatibilitytools.d. "
            "Install more versions using ProtonUp-Qt (recommended) or Lutris."
        )
        ver_note.setFont(QFont("DM Sans", 11))
        ver_note.setStyleSheet(f"color: {COLORS['text_muted']}; padding-bottom: 8px;")
        ver_note.setWordWrap(True)
        layout.addWidget(ver_note)

        # Show detected versions
        self.wine_versions_list = QWidget()
        self.wine_versions_list.setStyleSheet("background: transparent;")
        self._wine_ver_layout = QVBoxLayout(self.wine_versions_list)
        self._wine_ver_layout.setContentsMargins(0, 0, 0, 0)
        self._wine_ver_layout.setSpacing(4)
        self._refresh_wine_versions()
        layout.addWidget(self.wine_versions_list)

        refresh_ver_row = SettingsRow("", "")
        refresh_ver_btn = ActionButton("Refresh Detected Versions")
        refresh_ver_btn.clicked.connect(lambda: self._refresh_wine_versions())
        refresh_ver_row.add_control(refresh_ver_btn)
        layout.addWidget(refresh_ver_row)

        # Default version selector
        layout.addSpacing(16)
        self.default_wine_combo = QComboBox()
        self.default_wine_combo.setMinimumWidth(280)
        self._populate_wine_combo()
        self.default_wine_combo.currentIndexChanged.connect(
            lambda: self._save("default_proton_version",
                               self.default_wine_combo.currentData() or "")
        )
        self._make_setting_row(layout, "default_proton_version",
                               "Default Version (no ProtonDB data)",
                               "Used when ProtonDB has no recommendation for a game.",
                               self.default_wine_combo)

        layout.addSpacing(24)
        layout.addWidget(SectionHeader("Redistributables"))

        detect_toggle = SettingsToggle(
            db.get_setting("auto_detect_redists", "true") == "true")
        detect_toggle.changed.connect(
            lambda v: self._save("auto_detect_redists", "true" if v else "false"))
        self._make_setting_row(layout, "auto_detect_redists",
                               "Auto-detect Required Redists via SteamDB",
                               "Query Steam API for depot-based redistributable requirements "
                               "when installing a game that has a Steam App ID.",
                               detect_toggle)

        layout.addSpacing(24)
        layout.addWidget(SectionHeader("ProtonDB"))

        protondb_explain = QLabel(
            "ProtonDB data is fetched live from protondb.com for each game that has a "
            "Steam App ID. Per-game community report counts are stored locally and shown "
            "in the install dialog so you can see which Proton version the community "
            "recommends and make your own choice. No API key required."
        )
        protondb_explain.setFont(QFont("DM Sans", 11))
        protondb_explain.setStyleSheet(f"color: {COLORS['text_muted']}; padding-bottom: 8px;")
        protondb_explain.setWordWrap(True)
        layout.addWidget(protondb_explain)

        protondb_toggle = SettingsToggle(
            db.get_setting("protondb_auto_fetch", "true") == "true")
        protondb_toggle.changed.connect(
            lambda v: self._save("protondb_auto_fetch", "true" if v else "false"))
        self._make_setting_row(layout, "protondb_auto_fetch",
                               "Auto-fetch ProtonDB Compatibility",
                               "Fetch tier and per-version report counts after metadata "
                               "is retrieved. Runs in the background. No API key required.",
                               protondb_toggle)

        # Last fetched status
        pdb_status_row = SettingsRow(
            "Last Fetched",
            "When ProtonDB data was last refreshed for your library.")
        self.pdb_last_fetched_lbl = QLabel("")
        self.pdb_last_fetched_lbl.setFont(QFont("DM Mono", 9))
        self.pdb_last_fetched_lbl.setStyleSheet(f"color: {COLORS['text_muted']};")
        self._refresh_pdb_status_label()
        pdb_status_row.add_control(self.pdb_last_fetched_lbl)
        layout.addWidget(pdb_status_row)

        refresh_pdb_row = SettingsRow(
            "Refresh ProtonDB Data",
            "Re-fetches tier and per-version report counts for all games with a "
            "Steam App ID. Clears existing ProtonDB data first so everything is "
            "pulled fresh from protondb.com.")
        self.pdb_refresh_btn = ActionButton("Refresh Now")
        self.pdb_refresh_btn.clicked.connect(self._refresh_all_protondb)
        refresh_pdb_row.add_control(self.pdb_refresh_btn)
        layout.addWidget(refresh_pdb_row)

        self.pdb_status_lbl = QLabel("")
        self.pdb_status_lbl.setFont(QFont("DM Mono", 9))
        self.pdb_status_lbl.setStyleSheet(f"color: {COLORS['text_muted']};")
        layout.addWidget(self.pdb_status_lbl)

        # ── Redistributables ──────────────────────────────────────────────────
        layout.addSpacing(24)
        layout.addWidget(SectionHeader("Redistributables"))

        redist_explain = QLabel(
            "VaultPlay builds a per-game redistributable list from SteamCMD depot "
            "data and stores it in redists.json in your config folder. This file "
            "can be exported and imported to share data with another machine that "
            "doesn't have SteamCMD installed."
        )
        redist_explain.setFont(QFont("DM Sans", 11))
        redist_explain.setStyleSheet(f"color: {COLORS['text_muted']}; padding-bottom: 8px;")
        redist_explain.setWordWrap(True)
        layout.addWidget(redist_explain)

        # SteamCMD status
        steamcmd_row = SettingsRow("SteamCMD", "Required for fetching redistributable data")
        self.steamcmd_status_lbl = QLabel("")
        self.steamcmd_status_lbl.setFont(QFont("DM Mono", 9))
        self._refresh_steamcmd_status()
        steamcmd_row.add_control(self.steamcmd_status_lbl)
        layout.addWidget(steamcmd_row)

        # File stats
        redist_stats_row = SettingsRow("redists.json", "Current redistributable data file")
        self.redist_stats_lbl = QLabel("")
        self.redist_stats_lbl.setFont(QFont("DM Mono", 9))
        self.redist_stats_lbl.setStyleSheet(f"color: {COLORS['text_muted']};")
        self._refresh_redist_stats()
        redist_stats_row.add_control(self.redist_stats_lbl)
        layout.addWidget(redist_stats_row)

        # Refresh missing
        refresh_missing_row = SettingsRow(
            "Fetch Missing Data",
            "Runs SteamCMD for all PC games not yet in redists.json. "
            "Skips games already processed.")
        self.redist_missing_btn = ActionButton("Fetch Missing")
        self.redist_missing_btn.clicked.connect(self._refresh_redists_missing)
        refresh_missing_row.add_control(self.redist_missing_btn)
        layout.addWidget(refresh_missing_row)

        # Refresh all
        refresh_all_row = SettingsRow(
            "Refresh All Data",
            "Re-runs SteamCMD for every PC game with a Steam App ID. "
            "Overwrites existing entries with fresh data.")
        self.redist_all_btn = ActionButton("Refresh All")
        self.redist_all_btn.clicked.connect(self._refresh_redists_all)
        refresh_all_row.add_control(self.redist_all_btn)
        layout.addWidget(refresh_all_row)

        self.redist_status_lbl = QLabel("")
        self.redist_status_lbl.setFont(QFont("DM Mono", 9))
        self.redist_status_lbl.setStyleSheet(f"color: {COLORS['text_muted']};")
        layout.addWidget(self.redist_status_lbl)

        # Export / Import
        layout.addSpacing(8)
        export_row = SettingsRow(
            "Export redists.json",
            "Save a copy to share with another machine.")
        export_btn = ActionButton("Export…")
        export_btn.clicked.connect(self._export_redists)
        export_row.add_control(export_btn)
        layout.addWidget(export_row)

        import_row = SettingsRow(
            "Import redists.json",
            "Load data from another machine. Imported entries overwrite local ones.")
        import_btn = ActionButton("Import…")
        import_btn.clicked.connect(self._import_redists)
        import_row.add_control(import_btn)
        layout.addWidget(import_row)

        self.redist_import_lbl = QLabel("")
        self.redist_import_lbl.setFont(QFont("DM Mono", 9))
        self.redist_import_lbl.setStyleSheet(f"color: {COLORS['text_muted']};")
        layout.addWidget(self.redist_import_lbl)

        layout.addStretch()
        return scroll

    def _refresh_wine_versions(self):
        import protondb as protondb_mod
        while self._wine_ver_layout.count():
            item = self._wine_ver_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        versions = protondb_mod.get_versions_for_ui()
        if not versions:
            lbl = QLabel("No Proton/Wine versions found. Install via ProtonUp-Qt.")
            lbl.setFont(QFont("DM Sans", 11))
            lbl.setStyleSheet(f"color: {COLORS['danger']};")
            self._wine_ver_layout.addWidget(lbl)
        else:
            for label, value in versions:
                row = QWidget()
                row.setStyleSheet(
                    f"background: {COLORS['surface2']}; border: 1px solid {COLORS['border']};"
                    f" border-radius: 6px;")
                rl = QHBoxLayout(row)
                rl.setContentsMargins(12, 6, 12, 6)
                name_lbl = QLabel(label)
                name_lbl.setFont(QFont("DM Mono", 10))
                name_lbl.setStyleSheet(
                    f"color: {COLORS['accent2']}; background: transparent; border: none;")
                rl.addWidget(name_lbl, 1)
                self._wine_ver_layout.addWidget(row)
        # Also refresh the combo
        self._populate_wine_combo()

    def _populate_wine_combo(self):
        import protondb as protondb_mod
        if not hasattr(self, "default_wine_combo"):
            return
        # Block signals so repopulating doesn't fire currentIndexChanged and
        # overwrite the saved setting with whatever item lands at index 0.
        self.default_wine_combo.blockSignals(True)
        self.default_wine_combo.clear()
        versions = protondb_mod.get_versions_for_ui()
        current = db.get_setting("default_proton_version", "")
        if not versions:
            self.default_wine_combo.addItem("No versions found", "")
        else:
            for label, value in versions:
                self.default_wine_combo.addItem(label, value)
            idx = self.default_wine_combo.findData(current)
            if idx >= 0:
                self.default_wine_combo.setCurrentIndex(idx)
        self.default_wine_combo.blockSignals(False)

    def _refresh_pdb_status_label(self):
        """Update the last-fetched label from DB."""
        if not hasattr(self, "pdb_last_fetched_lbl"):
            return
        try:
            # Find most recent protondb_fetched_at across all metadata rows
            with db.get_connection() as conn:
                row = conn.execute(
                    "SELECT MAX(protondb_fetched_at) AS last_at, "
                    "COUNT(CASE WHEN protondb_tier IS NOT NULL THEN 1 END) AS with_data, "
                    "COUNT(CASE WHEN steam_app_id IS NOT NULL THEN 1 END) AS eligible "
                    "FROM metadata"
                ).fetchone()
            if row and row["last_at"]:
                last_at = row["last_at"][:16].replace("T", " ")
                with_data = row["with_data"] or 0
                eligible  = row["eligible"] or 0
                self.pdb_last_fetched_lbl.setText(
                    f"{last_at}  ·  {with_data}/{eligible} games have data")
                self.pdb_last_fetched_lbl.setStyleSheet("color: #4ade80;")
            else:
                self.pdb_last_fetched_lbl.setText("Never fetched")
                self.pdb_last_fetched_lbl.setStyleSheet(
                    f"color: {COLORS['text_muted']};")
        except Exception:
            self.pdb_last_fetched_lbl.setText("—")
            self.pdb_last_fetched_lbl.setStyleSheet(
                f"color: {COLORS['text_muted']};")

    def _refresh_all_protondb(self):
        import protondb as protondb_mod
        from PyQt6.QtCore import QThread, pyqtSignal as _Signal

        class _Worker(QThread):
            status = _Signal(str)
            done   = _Signal(int)   # games_updated count

            def run(self):
                try:
                    import time as _time

                    # Clear existing ProtonDB data so everything is fetched fresh.
                    # protondb_internal_id is intentionally preserved — old hashes
                    # remain valid and save a counts.json fetch per game.
                    db.reset_protondb_data()

                    games    = db.get_all_games()
                    eligible = [g for g in games if _safe_get(g, "steam_app_id")]
                    total    = len(eligible)
                    count    = 0

                    self.status.emit(
                        f"Fetching ProtonDB data for {total} games…")

                    # Fetch counts.json once for the whole batch
                    counts = protondb_mod.fetch_counts()
                    if not counts:
                        self.status.emit(
                            "⚠ Could not fetch ProtonDB counts.json — "
                            "will use cached hashes only")

                    for g in eligible:
                        result = protondb_mod.fetch_and_store(g["id"], counts=counts)
                        if result:
                            count += 1
                        self.status.emit(
                            f"Fetching… {count}/{total} — "
                            f"{_safe_get(g, 'display_name') or ''}")
                        _time.sleep(0.05)

                    self.done.emit(count)
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).error(
                        "ProtonDB refresh failed: %s", e)
                    self.status.emit(f"✗ Error: {e}")
                    self.done.emit(0)

        self.pdb_refresh_btn.setEnabled(False)
        self.pdb_refresh_btn.setText("Refreshing…")
        self.pdb_status_lbl.setText("Starting…")
        self.pdb_status_lbl.setStyleSheet(f"color: {COLORS['text_muted']};")

        self._pdb_worker = _Worker()
        self._pdb_worker.status.connect(self.pdb_status_lbl.setText)
        self._pdb_worker.done.connect(self._on_pdb_refresh_done)
        self._pdb_worker.start()

    def _on_pdb_refresh_done(self, count: int):
        self.pdb_status_lbl.setText(
            f"✓ Done — {count} game(s) updated")
        self.pdb_status_lbl.setStyleSheet("color: #4ade80;")
        self.pdb_refresh_btn.setText("Refresh Now")
        self.pdb_refresh_btn.setEnabled(True)
        self._refresh_pdb_status_label()

    # ── Redistributables helpers ──────────────────────────────────────────────

    def _refresh_steamcmd_status(self):
        if not hasattr(self, "steamcmd_status_lbl"):
            return
        import redists as redists_mod
        if redists_mod.is_steamcmd_available():
            import shutil
            path = shutil.which("steamcmd")
            self.steamcmd_status_lbl.setText("● Detected at " + str(path))
            self.steamcmd_status_lbl.setStyleSheet("color: #4ade80;")
        else:
            self.steamcmd_status_lbl.setText(
                "○ Not installed  (sudo pacman -S steamcmd)")
            self.steamcmd_status_lbl.setStyleSheet(
                f"color: {COLORS['text_muted']};")

    def _refresh_redist_stats(self):
        if not hasattr(self, "redist_stats_lbl"):
            return
        try:
            import redists as redists_mod
            stats = redists_mod.get_stats()
            if not stats["exists"]:
                self.redist_stats_lbl.setText("No file yet")
                self.redist_stats_lbl.setStyleSheet(
                    f"color: {COLORS['text_muted']};")
            else:
                size_kb = stats["file_size"] / 1024
                text = (str(stats["total"]) + " entries  ·  " +
                        str(stats["with_data"]) + " with data  ·  " +
                        str(stats["missing"]) + " missing  ·  " +
                        str(round(size_kb, 1)) + " KB")
                self.redist_stats_lbl.setText(text)
                self.redist_stats_lbl.setStyleSheet(
                    "color: #4ade80;" if stats["missing"] == 0
                    else f"color: {COLORS['text_muted']};")
        except Exception as e:
            self.redist_stats_lbl.setText("Error reading stats")
            self.redist_stats_lbl.setStyleSheet(
                f"color: {COLORS['danger']};")

    def _refresh_redists_missing(self):
        import redists as redists_mod
        if not redists_mod.is_steamcmd_available():
            self.redist_status_lbl.setText(
                "✗ SteamCMD not installed — cannot fetch data")
            self.redist_status_lbl.setStyleSheet(
                f"color: {COLORS['danger']};")
            return
        stats = redists_mod.get_stats()
        if stats["missing"] == 0:
            self.redist_status_lbl.setText(
                "✓ All games already have data — nothing to fetch")
            self.redist_status_lbl.setStyleSheet("color: #4ade80;")
            return
        est_secs = redists_mod.estimate_refresh_seconds(stats["missing"])
        est_mins = round(est_secs / 60, 1)
        self._start_redist_refresh(missing_only=True, estimated_mins=est_mins)

    def _refresh_redists_all(self):
        import redists as redists_mod
        if not redists_mod.is_steamcmd_available():
            self.redist_status_lbl.setText(
                "✗ SteamCMD not installed — cannot fetch data")
            self.redist_status_lbl.setStyleSheet(
                f"color: {COLORS['danger']};")
            return
        stats = redists_mod.get_stats()
        total = stats["total"] + stats["missing"]
        est_secs = redists_mod.estimate_refresh_seconds(total)
        est_mins = round(est_secs / 60, 1)
        self._start_redist_refresh(missing_only=False, estimated_mins=est_mins)

    def _start_redist_refresh(self, missing_only: bool, estimated_mins: float):
        from PyQt6.QtWidgets import QMessageBox
        import redists as redists_mod

        action = "fetch missing" if missing_only else "refresh all"
        reply = QMessageBox.question(
            self,
            "Fetch Redistributable Data",
            "VaultPlay will run SteamCMD to " + action + " redistributable "
            "data for your library.\n\n"
            "Estimated time: ~" + str(estimated_mins) + " minutes\n\n"
            "The app will remain usable while this runs in the background.",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Ok
        )
        if reply != QMessageBox.StandardButton.Ok:
            return

        from PyQt6.QtCore import QThread
        from PyQt6.QtCore import pyqtSignal as _Signal

        class _Worker(QThread):
            status = _Signal(str)
            done   = _Signal(int)

            def __init__(self, missing_only):
                super().__init__()
                self._missing_only = missing_only

            def run(self):
                import redists as _redists
                fn = _redists.refresh_missing if self._missing_only \
                     else _redists.refresh_all
                count = fn(
                    progress_cb=lambda cur, tot, name:
                        self.status.emit(
                            "Fetching… " + str(cur) + "/" + str(tot) +
                            " — " + name)
                )
                self.done.emit(count)

        self.redist_missing_btn.setEnabled(False)
        self.redist_all_btn.setEnabled(False)
        self.redist_status_lbl.setText("Starting SteamCMD…")
        self.redist_status_lbl.setStyleSheet(
            f"color: {COLORS['text_muted']};")

        self._redist_worker = _Worker(missing_only)
        self._redist_worker.status.connect(self.redist_status_lbl.setText)
        self._redist_worker.done.connect(self._on_redist_refresh_done)
        self._redist_worker.start()

    def _on_redist_refresh_done(self, count: int):
        self.redist_status_lbl.setText(
            "✓ Done — " + str(count) + " game(s) processed")
        self.redist_status_lbl.setStyleSheet("color: #4ade80;")
        self.redist_missing_btn.setEnabled(True)
        self.redist_all_btn.setEnabled(True)
        self._refresh_redist_stats()

    def _export_redists(self):
        from PyQt6.QtWidgets import QFileDialog
        import redists as redists_mod
        if not redists_mod.get_redists_path().exists():
            self.redist_import_lbl.setText(
                "✗ No redists.json to export — run a fetch first")
            self.redist_import_lbl.setStyleSheet(
                f"color: {COLORS['danger']};")
            return
        dest, _ = QFileDialog.getSaveFileName(
            self,
            "Export redists.json",
            str(Path.home() / "redists.json"),
            "JSON files (*.json)"
        )
        if not dest:
            return
        ok = redists_mod.export_redists(Path(dest))
        if ok:
            self.redist_import_lbl.setText("✓ Exported to " + dest)
            self.redist_import_lbl.setStyleSheet("color: #4ade80;")
        else:
            self.redist_import_lbl.setText("✗ Export failed")
            self.redist_import_lbl.setStyleSheet(
                f"color: {COLORS['danger']};")

    def _import_redists(self):
        from PyQt6.QtWidgets import QFileDialog
        import redists as redists_mod
        src, _ = QFileDialog.getOpenFileName(
            self,
            "Import redists.json",
            str(Path.home()),
            "JSON files (*.json)"
        )
        if not src:
            return
        added, updated = redists_mod.import_redists(Path(src))
        self.redist_import_lbl.setText(
            "✓ Import complete — " + str(added) + " added, " +
            str(updated) + " updated")
        self.redist_import_lbl.setStyleSheet("color: #4ade80;")
        self._refresh_redist_stats()

    # ── Appearance    # ── Appearance ────────────────────────────────────────────────────────────

    def _build_appearance_page(self) -> QScrollArea:
        scroll, layout = self._scrollable_page("Appearance")
        layout.addWidget(SectionHeader("Theme"))

        theme_combo = QComboBox()
        for label, val in [("Dark", "dark"), ("Light (coming soon)", "light")]:
            theme_combo.addItem(label, val)
        self._make_setting_row(layout, "theme", "Color Theme",
                               "App color scheme", theme_combo)

        # Accent swatches
        accent_row = SettingsRow("Accent Color", "Primary highlight color")
        swatches_widget = QWidget()
        swatches_widget.setStyleSheet("background: transparent; border: none;")
        sw_layout = QHBoxLayout(swatches_widget)
        sw_layout.setContentsMargins(0, 0, 0, 0)
        sw_layout.setSpacing(8)

        current_accent = db.get_setting("accent_color", "#e8c76a")
        for color in ["#e8c76a", "#5b8dee", "#e85b8d", "#5be89c", "#e8795b", "#b55be8"]:
            swatch = QPushButton()
            swatch.setFixedSize(24, 24)
            selected_style = "border: 2px solid white; transform: scale(1.1);" if color == current_accent else ""
            swatch.setStyleSheet(f"""
                QPushButton {{
                    background: {color};
                    border-radius: 12px;
                    border: 2px solid {'white' if color == current_accent else 'transparent'};
                }}
                QPushButton:hover {{ border: 2px solid rgba(255,255,255,0.6); }}
            """)
            swatch.clicked.connect(lambda _, c=color: self._pick_accent(c))
            sw_layout.addWidget(swatch)
            self._widgets[f"swatch_{color}"] = swatch

        accent_row.add_control(swatches_widget)
        layout.addWidget(accent_row)

        layout.addSpacing(24)
        layout.addWidget(SectionHeader("Library"))

        size_combo = QComboBox()
        for label, val in [("Small", "small"), ("Medium", "medium"), ("Large", "large")]:
            size_combo.addItem(label, val)
        idx = size_combo.findData(db.get_setting("tile_size", "medium"))
        size_combo.setCurrentIndex(max(0, idx))
        size_combo.currentIndexChanged.connect(
            lambda: self._save("tile_size", size_combo.currentData())
        )
        self._make_setting_row(layout, "tile_size", "Tile Size",
                               "How large game tiles appear in the library grid",
                               size_combo)

        filesize_toggle = SettingsToggle(db.get_setting("show_filesize_on_tile", "false") == "true")
        filesize_toggle.changed.connect(lambda v: self._save("show_filesize_on_tile", "true" if v else "false"))
        self._make_setting_row(layout, "show_filesize_on_tile",
                               "Show File Size on Tile",
                               "Display NAS archive size below title on each tile",
                               filesize_toggle)

        layout.addStretch()
        return scroll

    def _pick_accent(self, color: str):
        self._save("accent_color", color)
        # Update swatch borders
        for c in ["#e8c76a", "#5b8dee", "#e85b8d", "#5be89c", "#e8795b", "#b55be8"]:
            w = self._widgets.get(f"swatch_{c}")
            if w:
                border = "white" if c == color else "transparent"
                w.setStyleSheet(f"""
                    QPushButton {{
                        background: {c};
                        border-radius: 12px;
                        border: 2px solid {border};
                    }}
                    QPushButton:hover {{ border: 2px solid rgba(255,255,255,0.6); }}
                """)

    # ── Scan & Cache ──────────────────────────────────────────────────────────

    def _build_scan_page(self) -> QScrollArea:
        scroll, layout = self._scrollable_page("Scan & Cache")
        layout.addWidget(SectionHeader("Library Scanning"))

        scan_toggle = SettingsToggle(db.get_setting("scan_on_launch", "true") == "true")
        scan_toggle.changed.connect(lambda v: self._save("scan_on_launch", "true" if v else "false"))
        self._make_setting_row(layout, "scan_on_launch",
                               "Scan on App Launch",
                               "Automatically check for new games when the app opens",
                               scan_toggle)

        interval_combo = QComboBox()
        for label, val in [("Every 15 min", "15"), ("Every 30 min", "30"),
                            ("Every hour", "60"), ("Never", "0")]:
            interval_combo.addItem(label, val)
        idx = interval_combo.findData(db.get_setting("scan_interval_minutes", "30"))
        interval_combo.setCurrentIndex(max(0, idx))
        interval_combo.currentIndexChanged.connect(
            lambda: self._save("scan_interval_minutes", interval_combo.currentData())
        )
        self._make_setting_row(layout, "scan_interval_minutes",
                               "Background Scan Interval",
                               "How often to check for new games while the app is running",
                               interval_combo)

        layout.addSpacing(24)
        layout.addWidget(SectionHeader("Library Filters"))

        recent_combo = QComboBox()
        for label, val in [("7 days",  "7"),
                            ("14 days", "14"),
                            ("30 days", "30"),
                            ("60 days", "60"),
                            ("90 days", "90")]:
            recent_combo.addItem(label, val)
        idx = recent_combo.findData(db.get_setting("recently_added_days", "14"))
        recent_combo.setCurrentIndex(max(0, idx))
        recent_combo.currentIndexChanged.connect(
            lambda: self._save("recently_added_days", recent_combo.currentData())
        )
        self._make_setting_row(layout, "recently_added_days",
                               "\"Recently Added\" Window",
                               "How far back the Recently Added sidebar filter looks",
                               recent_combo)

        scan_now_row = SettingsRow("Last Scan", "")
        scan_now_btn = ActionButton("Scan Now")
        scan_now_btn.clicked.connect(lambda: self._trigger_scan_now(scan_now_row))
        scan_now_row.add_control(scan_now_btn)
        layout.addWidget(scan_now_row)

        self._last_scan_desc_lbl = QLabel(
            db.get_setting("last_scan_result", "Never scanned")
        )
        self._last_scan_desc_lbl.setFont(QFont("DM Sans", 10))
        self._last_scan_desc_lbl.setStyleSheet(f"color: {COLORS['text_muted']}; padding: 0 0 8px 0;")
        layout.addWidget(self._last_scan_desc_lbl)

        layout.addSpacing(24)
        layout.addWidget(SectionHeader("Metadata (Cover Art & Game Info)"))

        meta_explain = QLabel(
            "Metadata (cover art, descriptions, release dates) is fetched automatically "
            "after each scan — but only if a SteamGridDB API key is configured in API Keys. "
            "It runs in the background and does not block the UI. You can also trigger it "
            "manually below for any games that are still missing art."
        )
        meta_explain.setFont(QFont("DM Sans", 11))
        meta_explain.setStyleSheet(f"color: {COLORS['text_muted']}; padding-bottom: 4px;")
        meta_explain.setWordWrap(True)
        layout.addWidget(meta_explain)

        self.meta_status_lbl = QLabel("")
        self.meta_status_lbl.setFont(QFont("DM Mono", 9))
        self.meta_status_lbl.setStyleSheet(f"color: {COLORS['text_muted']};")

        meta_row = SettingsRow(
            "Fetch Missing Metadata",
            "Runs SteamGridDB (and IGDB if configured) for all games without art/info."
        )
        self.fetch_meta_btn = ActionButton("Fetch Now")
        self.fetch_meta_btn.clicked.connect(self._trigger_metadata_fetch)
        meta_row.add_control(self.fetch_meta_btn)
        layout.addWidget(meta_row)
        layout.addWidget(self.meta_status_lbl)

        layout.addSpacing(24)
        layout.addWidget(SectionHeader("Metadata Cache"))

        cache_path_edit = PathEdit(db.get_setting("cache_path",
                                                   str(db.CONFIG_DIR / "cache")))
        cache_path_edit.changed.connect(lambda v: self._save("cache_path", v))
        self._make_setting_row(layout, "cache_path", "Cache Location",
                               "Where artwork and metadata are stored locally",
                               cache_path_edit)

        size_bytes = db.get_cache_size_bytes()
        cache_size_row = SettingsRow("Cache Size",
                                     scanner.format_size(size_bytes) + " currently cached")
        clear_btn = ActionButton("Clear & Re-fetch", danger=True)
        clear_btn.clicked.connect(self._clear_cache)
        cache_size_row.add_control(clear_btn)
        layout.addWidget(cache_size_row)

        layout.addSpacing(24)
        layout.addWidget(SectionHeader("Database"))

        db_row = SettingsRow(
            "Clear Entire Database",
            "Wipes all games, metadata, categories, art cache, and settings. "
            "Use this if you need a completely fresh start. The app will restart "
            "its configuration from scratch."
        )
        db_clear_btn = ActionButton("Clear Database", danger=True)
        db_clear_btn.clicked.connect(self._clear_database)
        db_row.add_control(db_clear_btn)
        layout.addWidget(db_row)

        layout.addStretch()
        return scroll

    def _trigger_scan_now(self, row: SettingsRow):
        nas_path = db.get_setting("nas_path", "")
        if nas_path and nas_path.strip() not in ("", "/"):
            # Rescan only — do NOT clear the DB (preserves blacklist etc.)
            self.rescan_requested.emit(nas_path)

    def _clear_cache(self):
        db.clear_metadata_cache()

    def _trigger_metadata_fetch(self):
        sgdb_key = db.get_setting("sgdb_api_key", "")
        if not sgdb_key:
            self.meta_status_lbl.setText(
                "⚠ No SteamGridDB API key configured — go to API Keys first")
            self.meta_status_lbl.setStyleSheet(f"color: {COLORS['danger']};")
            return
        self.fetch_meta_btn.setEnabled(False)
        self.fetch_meta_btn.setText("Fetching…")
        self.meta_status_lbl.setText("Starting metadata fetch…")
        self.meta_status_lbl.setStyleSheet(f"color: {COLORS['text_muted']};")
        self.rescan_requested.emit("__metadata__")

    def _clear_database(self):
        from PyQt6.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            self,
            "Clear Entire Database",
            "This will wipe ALL games, metadata, categories, art, and settings. Are you sure? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel
        )
        if reply == QMessageBox.StandardButton.Yes:
            db.clear_database()
            # Reload settings UI to reflect cleared state
            self.load_settings()
            if hasattr(self, "cat_list_layout"):
                self._refresh_category_list()

    # ── Version Tracking ──────────────────────────────────────────────────────

    def _build_version_tracking_page(self) -> QScrollArea:
        scroll, layout = self._scrollable_page("Version Tracking")

        # ── Auto-check settings ───────────────────────────────────────────────
        layout.addWidget(SectionHeader("Automatic Checking"))

        auto_toggle = SettingsToggle(
            db.get_setting("version_check_auto", "true") == "true")
        auto_toggle.changed.connect(
            lambda v: self._save("version_check_auto", "true" if v else "false"))
        self._make_setting_row(
            layout, "version_check_auto",
            "Auto-check for Updates",
            "Periodically recheck all tracked pages for new version numbers",
            auto_toggle)

        interval_combo = QComboBox()
        for lbl, val in [("Every 12 hours", "12"), ("Every 24 hours", "24"),
                          ("Every 48 hours", "48"), ("Every 7 days", "168")]:
            interval_combo.addItem(lbl, val)
        idx = interval_combo.findData(
            db.get_setting("version_check_interval_hours", "24"))
        interval_combo.setCurrentIndex(max(0, idx))
        interval_combo.currentIndexChanged.connect(
            lambda: self._save("version_check_interval_hours",
                               interval_combo.currentData()))
        self._make_setting_row(
            layout, "version_check_interval_hours",
            "Check Interval",
            "How often to recheck all tracked pages in the background",
            interval_combo)

        # ── Status + manual trigger ───────────────────────────────────────────
        layout.addSpacing(16)
        layout.addWidget(SectionHeader("Status"))

        status_row = SettingsRow("Last Check", "When all trackers were last rechecked")
        self.ver_last_run_lbl = QLabel("")
        self.ver_last_run_lbl.setFont(QFont("DM Mono", 9))
        self._refresh_version_status_label()
        status_row.add_control(self.ver_last_run_lbl)
        layout.addWidget(status_row)

        check_all_row = SettingsRow(
            "Check All Now",
            "Recheck every tracked page immediately")
        self.ver_check_all_btn = ActionButton("Check All Now")
        self.ver_check_all_btn.clicked.connect(self._on_check_all_now)
        check_all_row.add_control(self.ver_check_all_btn)
        layout.addWidget(check_all_row)

        self.ver_status_lbl = QLabel("")
        self.ver_status_lbl.setFont(QFont("DM Mono", 9))
        self.ver_status_lbl.setStyleSheet(f"color: {COLORS['text_muted']};")
        layout.addWidget(self.ver_status_lbl)

        # ── Site management ───────────────────────────────────────────────────
        layout.addSpacing(16)
        layout.addWidget(SectionHeader("Tracked Sites"))

        site_desc = QLabel(
            "Sites listed here are used to look up version numbers. "
            "Formula sites (marked ★) automatically check all new games "
            "using a slugified URL pattern. Manual sites are game-specific "
            "and managed via the right-click menu on any game tile."
        )
        site_desc.setFont(QFont("DM Sans", 11))
        site_desc.setStyleSheet(
            f"color: {COLORS['text_muted']}; padding-bottom: 8px;")
        site_desc.setWordWrap(True)
        layout.addWidget(site_desc)

        # Sites list container — rebuilt by _refresh_version_sites()
        self._ver_sites_container = QWidget()
        self._ver_sites_container.setStyleSheet("background: transparent;")
        self._ver_sites_layout = QVBoxLayout(self._ver_sites_container)
        self._ver_sites_layout.setContentsMargins(0, 0, 0, 0)
        self._ver_sites_layout.setSpacing(8)
        layout.addWidget(self._ver_sites_container)

        self._refresh_version_sites()

        layout.addSpacing(8)
        layout.addWidget(SectionHeader("Add Formula Site"))

        formula_desc = QLabel(
            "A formula site constructs a URL for each game using its slugified "
            "name. Provide a base URL, a path prefix, and a suffix. "
            "The full URL will be: base_url + prefix + slug + suffix.\n"
            "Example: base='https://pcgamingwiki.com' prefix='/wiki/' suffix=''"
        )
        formula_desc.setFont(QFont("DM Sans", 10))
        formula_desc.setStyleSheet(
            f"color: {COLORS['text_muted']}; padding-bottom: 6px;")
        formula_desc.setWordWrap(True)
        layout.addWidget(formula_desc)

        # Add formula site form
        form_widget = QWidget()
        form_widget.setStyleSheet(f"""
            QWidget {{
                background: {COLORS['surface2']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
            }}
        """)
        form_l = QVBoxLayout(form_widget)
        form_l.setContentsMargins(14, 12, 14, 12)
        form_l.setSpacing(8)

        for field_name, placeholder, attr in [
            ("Label",    "PCGamingWiki",                 "_ver_new_label"),
            ("Base URL", "https://pcgamingwiki.com",     "_ver_new_base"),
            ("Prefix",   "/wiki/",                       "_ver_new_prefix"),
            ("Suffix",   "(leave blank if not needed)",  "_ver_new_suffix"),
        ]:
            row_l = QHBoxLayout()
            row_l.setSpacing(10)
            row_l.setContentsMargins(0, 0, 0, 0)
            name_lbl = QLabel(field_name)
            name_lbl.setFont(QFont("DM Mono", 9))
            name_lbl.setFixedWidth(68)
            name_lbl.setStyleSheet(
                f"color: {COLORS['text_muted']}; background: transparent; border: none;")
            edit = QLineEdit()
            edit.setFont(QFont("DM Mono", 10))
            edit.setPlaceholderText(placeholder)
            edit.setStyleSheet(f"""
                QLineEdit {{
                    background: {COLORS['surface3']};
                    border: 1px solid {COLORS['border']};
                    border-radius: 5px;
                    color: {COLORS['accent2']};
                    padding: 4px 8px;
                }}
                QLineEdit:focus {{ border-color: rgba(255,255,255,0.2); }}
            """)
            setattr(self, attr, edit)
            row_l.addWidget(name_lbl)
            row_l.addWidget(edit, 1)
            form_l.addLayout(row_l)

        add_site_row = QHBoxLayout()
        add_site_row.setContentsMargins(0, 4, 0, 0)
        add_site_row.setSpacing(10)
        add_site_row.addStretch()

        self._ver_add_result_lbl = QLabel("")
        self._ver_add_result_lbl.setFont(QFont("DM Mono", 9))
        self._ver_add_result_lbl.setStyleSheet(
            f"color: {COLORS['text_muted']}; background: transparent; border: none;")
        add_site_row.addWidget(self._ver_add_result_lbl)

        add_site_btn = ActionButton("Add Formula Site")
        add_site_btn.clicked.connect(self._on_add_formula_site)
        add_site_row.addWidget(add_site_btn)
        form_l.addLayout(add_site_row)
        layout.addWidget(form_widget)

        layout.addStretch()
        return scroll

    def _refresh_version_sites(self):
        """Rebuild the site list widget from DB."""
        while self._ver_sites_layout.count():
            item = self._ver_sites_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        sites = db.get_version_sites()
        if not sites:
            empty = QLabel("No sites configured yet.")
            empty.setFont(QFont("DM Sans", 11))
            empty.setStyleSheet(f"color: {COLORS['text_muted']};")
            self._ver_sites_layout.addWidget(empty)
            return

        for site in sites:
            self._ver_sites_layout.addWidget(self._make_site_row(site))

    def _make_site_row(self, site) -> QWidget:
        """Build one site management row."""
        is_formula = bool(site["auto_track_new_games"])
        n_trackers = db.count_trackers_for_site(site["id"])

        row = QWidget()
        row.setStyleSheet(f"""
            QWidget {{
                background: {COLORS['surface2']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
            }}
        """)
        row_l = QVBoxLayout(row)
        row_l.setContentsMargins(14, 10, 14, 10)
        row_l.setSpacing(6)

        # Top: label (editable) + formula badge + tracker count
        top = QHBoxLayout()
        top.setSpacing(8)
        top.setContentsMargins(0, 0, 0, 0)

        label_edit = QLineEdit(site["label"])
        label_edit.setFont(QFont("DM Sans", 11, QFont.Weight.Medium))
        label_edit.setStyleSheet(f"""
            QLineEdit {{
                background: transparent;
                border: none;
                border-bottom: 1px solid {COLORS['border']};
                color: {COLORS['text']};
                padding: 2px 0;
            }}
            QLineEdit:focus {{
                border-bottom-color: rgba(255,255,255,0.3);
            }}
        """)
        label_edit.setPlaceholderText("Site label…")
        top.addWidget(label_edit, 1)

        if is_formula:
            badge = QLabel("★ formula")
            badge.setFont(QFont("DM Mono", 8))
            badge.setStyleSheet(f"""
                color: {COLORS['accent']};
                background: rgba(232,199,106,0.10);
                border: 1px solid rgba(232,199,106,0.3);
                border-radius: 4px;
                padding: 1px 6px;
            """)
            top.addWidget(badge)

        count_lbl = QLabel(f"{n_trackers} tracker{'s' if n_trackers != 1 else ''}")
        count_lbl.setFont(QFont("DM Mono", 9))
        count_lbl.setStyleSheet(
            f"color: {COLORS['text_muted']}; background: transparent; border: none;")
        top.addWidget(count_lbl)
        row_l.addLayout(top)

        # Base URL (read-only display)
        url_lbl = QLabel(site["base_url"] + (site["suffix"] or ""))
        url_lbl.setFont(QFont("DM Mono", 9))
        url_lbl.setStyleSheet(
            f"color: {COLORS['accent2']}; background: transparent; border: none;")
        url_lbl.setWordWrap(False)
        row_l.addWidget(url_lbl)

        # Buttons row
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.setContentsMargins(0, 0, 0, 0)

        save_btn = ActionButton("Save Label")
        save_btn.clicked.connect(
            lambda _, sid=site["id"], e=label_edit:
            self._on_save_site_label(sid, e.text().strip()))
        btn_row.addWidget(save_btn)

        if is_formula:
            backfill_btn = ActionButton("Backfill Library…")
            backfill_btn.clicked.connect(
                lambda _, sid=site["id"], slbl=site["label"]:
                self._on_backfill_site(sid, slbl))
            btn_row.addWidget(backfill_btn)

        btn_row.addStretch()

        delete_btn = ActionButton("Delete", danger=True)
        delete_btn.clicked.connect(
            lambda _, sid=site["id"], slbl=site["label"], n=n_trackers:
            self._on_delete_site(sid, slbl, n))
        btn_row.addWidget(delete_btn)

        row_l.addLayout(btn_row)
        return row

    def _refresh_version_status_label(self):
        """Update the last-checked timestamp label from DB."""
        if not hasattr(self, "ver_last_run_lbl"):
            return
        import datetime
        last_run = db.get_setting("version_check_last_run_at", "")
        if not last_run:
            self.ver_last_run_lbl.setText("Never")
            self.ver_last_run_lbl.setStyleSheet(
                f"color: {COLORS['text_muted']}; font-family: 'DM Mono'; font-size: 9px;")
            return
        try:
            dt = datetime.datetime.fromisoformat(
                last_run.replace("T", " ")[:19])
            delta = (datetime.datetime.utcnow() - dt).days
            if delta == 0:
                label = "Today"
                color = "#4ade80"
            elif delta == 1:
                label = "Yesterday"
                color = COLORS["text_muted"]
            else:
                label = f"{delta} days ago"
                color = COLORS["text_muted"]
            self.ver_last_run_lbl.setText(label)
            self.ver_last_run_lbl.setStyleSheet(
                f"color: {color}; font-family: 'DM Mono'; font-size: 9px;")
        except (ValueError, TypeError):
            self.ver_last_run_lbl.setText("—")

    def _on_check_all_now(self):
        """Trigger Check All Now via main window's version check entry point."""
        try:
            main_win = self.window()
            if hasattr(main_win, "_start_version_check"):
                self.ver_check_all_btn.setEnabled(False)
                self.ver_check_all_btn.setText("Checking…")
                self.ver_status_lbl.setText("Starting version check…")
                self.ver_status_lbl.setStyleSheet(
                    f"color: {COLORS['text_muted']};")
                main_win._start_version_check(manual=True)
            else:
                self.ver_status_lbl.setText(
                    "⚠ Could not reach main window — restart the app.")
        except Exception as e:
            self.ver_status_lbl.setText(f"✗ Error: {e}")

    def _on_version_check_done(self, found: int, checked: int, errors: int):
        """
        Called by MainWindow._on_version_worker_done() when a check completes.
        Updates the settings page status without requiring a full reload.
        """
        self.ver_check_all_btn.setEnabled(True)
        self.ver_check_all_btn.setText("Check All Now")
        msg = f"✓ Done — {found} updated, {checked} checked"
        if errors:
            msg += f", {errors} error(s)"
        self.ver_status_lbl.setText(msg)
        self.ver_status_lbl.setStyleSheet("color: #4ade80;")
        self._refresh_version_status_label()

    def _on_add_formula_site(self):
        """Create a new formula site from the add-site form fields."""
        label  = self._ver_new_label.text().strip()
        base   = self._ver_new_base.text().strip().rstrip("/")
        prefix = self._ver_new_prefix.text().strip()
        suffix = self._ver_new_suffix.text().strip()

        if not base:
            self._set_add_result("✗ Base URL is required", "error")
            return

        import version_check as _vc
        err = _vc.validate_url(base)
        if err:
            self._set_add_result(f"✗ {err}", "error")
            return

        # The prefix becomes part of the path template.
        # Stored in version_sites.suffix as the path prefix so that
        # build_url(base_url, "/" + slug, suffix=prefix_suffix) works.
        # We store it as: base_url = host, suffix = prefix (goes before slug).
        # Actually align with how db.get_or_create works:
        #   base_url = the site root (host only)
        #   auto-track workers build: base_url + "/" + slug + suffix
        # So for PCGamingWiki with prefix "/wiki/" the suffix should be "/wiki/"
        # and workers prepend it: base_url + suffix + slug.
        # Redefine: store prefix as part of base_url so it's baked in.
        # e.g. base="https://pcgamingwiki.com" prefix="/wiki/" →
        #   stored base_url = "https://pcgamingwiki.com/wiki" (no trailing slash)
        #   workers build: base_url + "/" + slug + suffix

        # Combine base + prefix into base_url (strip trailing slashes)
        prefix_clean = prefix.lstrip("/").rstrip("/")
        if prefix_clean:
            stored_base = base + "/" + prefix_clean
        else:
            stored_base = base

        site = db.get_or_create_version_site_by_base_url(
            base_url   = stored_base,
            label      = label or _vc._base_url_to_label(base),
            suffix     = suffix,
            auto_track = True,
        )

        # Clear form
        for attr in ("_ver_new_label", "_ver_new_base",
                     "_ver_new_prefix", "_ver_new_suffix"):
            getattr(self, attr).clear()

        self._set_add_result(f"✓ Site '{site['label']}' added", "good")
        self._refresh_version_sites()

    def _set_add_result(self, text: str, style: str):
        colors = {"good": "#4ade80", "error": COLORS["danger"],
                  "muted": COLORS["text_muted"]}
        self._ver_add_result_lbl.setText(text)
        self._ver_add_result_lbl.setStyleSheet(
            f"color: {colors.get(style, COLORS['text_muted'])};"
            " background: transparent; border: none;")

    def _on_save_site_label(self, site_id: int, new_label: str):
        if not new_label:
            return
        db.update_version_site(site_id, label=new_label)
        self._refresh_version_sites()

    def _on_delete_site(self, site_id: int, label: str, n_trackers: int):
        from PyQt6.QtWidgets import QMessageBox
        msg = QMessageBox(self)
        msg.setWindowTitle("Delete Site")
        msg.setText(f"Delete site '{label}'?")
        if n_trackers > 0:
            msg.setInformativeText(
                f"This will also delete {n_trackers} tracker "
                f"{'rows' if n_trackers != 1 else 'row'} and all stored "
                f"version data for this site. This cannot be undone.")
        else:
            msg.setInformativeText("This site has no trackers.")
        msg.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        msg.setDefaultButton(QMessageBox.StandardButton.Cancel)
        msg.setStyleSheet(
            f"QMessageBox {{ background: {COLORS['surface']}; "
            f"color: {COLORS['text']}; }}")
        if msg.exec() == QMessageBox.StandardButton.Yes:
            db.delete_version_site(site_id)
            self._refresh_version_sites()

    def _on_backfill_site(self, site_id: int, label: str):
        """Start a backfill pass via main window."""
        from PyQt6.QtWidgets import QMessageBox
        import version_checker as vc_mod
        candidates = db.get_backfill_candidates_for_site(site_id)
        n = len(candidates)
        if n == 0:
            QMessageBox.information(
                self, "Backfill",
                f"No new candidates found for '{label}'.\n"
                "All library games have either been checked or confirmed absent.")
            return
        est_secs = vc_mod.estimate_check_seconds(n)
        est_mins = round(est_secs / 60, 1)
        reply = QMessageBox.question(
            self, "Backfill Library",
            f"Check {n} game{'s' if n != 1 else ''} against '{label}'?\n\n"
            f"Estimated time: ~{est_mins} minutes\n"
            "Games already tracked or confirmed absent will be skipped.",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Ok)
        if reply != QMessageBox.StandardButton.Ok:
            return
        try:
            main_win = self.window()
            if hasattr(main_win, "start_version_backfill"):
                main_win.start_version_backfill(site_id)
                self.ver_status_lbl.setText(
                    f"Backfill started for '{label}'…")
                self.ver_status_lbl.setStyleSheet(
                    f"color: {COLORS['text_muted']};")
            else:
                self.ver_status_lbl.setText(
                    "⚠ Could not reach main window — restart the app.")
        except Exception as e:
            self.ver_status_lbl.setText(f"✗ Error: {e}")

    # ── About / Version ──────────────────────────────────────────────────────
    # Version is hardcoded here — not stored in the DB.
    # To bump the version, change this constant only.
    APP_VERSION = "0.3.0-dev"

    def _build_about_page(self) -> QScrollArea:
        scroll, layout = self._scrollable_page("About VaultPlay")

        version = self.APP_VERSION

        ver_box = QWidget()
        ver_box.setStyleSheet(
            f"background: {COLORS['surface2']}; border: 1px solid {COLORS['border']};"
            f" border-radius: 10px;")
        ver_l = QVBoxLayout(ver_box)
        ver_l.setContentsMargins(20, 16, 20, 16)
        ver_l.setSpacing(6)

        ver_title = QLabel(f"VaultPlay  v{version}")
        ver_title.setFont(QFont("Rajdhani", 18, QFont.Weight.Bold))
        ver_title.setStyleSheet(f"color: {COLORS['accent']};")
        ver_l.addWidget(ver_title)

        status_lbl = QLabel("Pre-release — In active development")
        status_lbl.setFont(QFont("DM Mono", 10))
        status_lbl.setStyleSheet(f"color: {COLORS['text_muted']};")
        ver_l.addWidget(status_lbl)

        layout.addWidget(ver_box)
        layout.addSpacing(24)

        layout.addWidget(SectionHeader("What's new in 0.2.0-dev"))

        changelog = [
            # ── Major features ────────────────────────────────────────────────
            ("ProtonDB Compatibility — Live Community Data",
             "Each game's install dialog now shows which Proton version the community "
             "actually uses, pulled live from ProtonDB reports. The top-voted version "
             "is pre-selected. Counts are shown next to each version (e.g. 'Proton 9.0 "
             "· 12 of 30 most recent reports') so you can make your own judgement."),
            ("Proton Version Download — Install Without Leaving VaultPlay",
             "If the recommended Proton version isn't installed, it now appears in the "
             "dropdown marked '(not installed)'. Selecting it shows a Download button. "
             "GE-Proton versions download and install directly in-app with a progress "
             "bar. Official Steam Proton versions open Steam's install dialog with one "
             "click. Begin Install stays locked until the selected version is ready."),
            ("Redistributable Auto-Detection",
             "The install dialog auto-detects required redistributables for games that "
             "have a Steam App ID, using Steam's public API. Known depot IDs and package "
             "name patterns are mapped to winetricks verbs and pre-checked for you. "
             "Baseline redists (vcrun2019, vcrun2022, d3dcompiler_47) are always included."),
            ("NAS Category Scanning",
             "Each subfolder inside your Games path becomes a category filter in the "
             "sidebar (PC, PS4, Switch, etc.). Recursive detection up to 3 levels deep "
             "handles nested folder structures. Categories can be renamed or blacklisted "
             "in Settings → Categories to hide them from the library entirely."),
            ("Install Type Auto-Detection",
             "Games are automatically tagged as Installer, Portable, or ISO by peeking "
             "inside archives. Multi-part RAR, 7z, and ZIP are all supported. The detected "
             "type can be overridden in the install dialog."),
            ("ISO Support",
             "ISO games are mounted via udisksctl (falls back to fuseiso — no sudo "
             "needed), the installer is run through Wine, then the ISO is unmounted and "
             "cleaned up automatically."),
            ("Multi-Path Install Locations",
             "Settings → Paths lets you add multiple game install destinations (different "
             "drives, partitions, etc.). The first is starred as default. Each install "
             "lets you pick from the list."),
            ("Game Name Cleaning",
             "Raw NAS folder names are cleaned for display and metadata search — strips "
             "scene group tags (CODEX, PLAZA, ElAmigos, EMPRESS, etc.), GOG IDs, version "
             "strings, MULTiN suffixes, and repack labels across multiple passes."),
            ("First-Run Setup Wizard",
             "Fresh installs show a one-time wizard to configure your NAS path, "
             "SteamGridDB API key, and default install location before the first scan."),
            # ── Minor improvements ────────────────────────────────────────────
            ("ProtonDB Refresh",
             "Settings → Wine → Refresh Now re-fetches compatibility data for all games. "
             "Previously fetched hashes are reused where valid to save network calls."),
            ("API Key Validation",
             "SteamGridDB and IGDB keys each have an Apply button that tests the key "
             "live and shows a ✓ / ✗ result inline."),
            ("NAS Connection Testing",
             "NAS path Apply button tests the connection immediately and shows a 'Scan "
             "Now' prompt. Sidebar connection status updates without waiting for a scan."),
            ("Category Blacklist",
             "Blacklisted categories are skipped entirely during scanning and hidden from "
             "the sidebar and All Games count. The blacklist flag survives rescans."),
            # ── Bug fixes & stability ──────────────────────────────────────────
            ("Stability — Large Libraries",
             "File descriptor limit raised to 65536 at startup. Image loader thread pool "
             "capped at 4. Metadata fetch rate-limited to 0.4s per game. Fixes crashes "
             "on libraries with 600+ games."),
            ("Stability — SQLite",
             "WAL journal mode, 30s timeout, 30s busy timeout on all connections. DB path "
             "computed from environment variables set at startup so every thread uses the "
             "same absolute path regardless of working directory."),
            ("Stability — AppImage",
             "All modules have a sys.path guard that reads $APPDIR at import time. "
             "Eliminates ModuleNotFoundError when running as an AppImage."),
            ("Automatic DB Migration",
             "New columns are added to existing databases on launch without data loss. "
             "No manual database actions needed when updating."),
        ]

        for title, desc in changelog:
            entry = QWidget()
            entry.setStyleSheet("background: transparent;")
            el = QVBoxLayout(entry)
            el.setContentsMargins(0, 8, 0, 8)
            el.setSpacing(3)
            tl = QLabel(f"• {title}")
            tl.setFont(QFont("DM Sans", 12, QFont.Weight.Medium))
            tl.setStyleSheet(f"color: {COLORS['text']};")
            el.addWidget(tl)
            dl = QLabel(desc)
            dl.setFont(QFont("DM Sans", 10))
            dl.setStyleSheet(f"color: {COLORS['text_muted']}; padding-left: 12px;")
            dl.setWordWrap(True)
            el.addWidget(dl)
            sep = QFrame()
            sep.setFrameShape(QFrame.Shape.HLine)
            sep.setFixedHeight(1)
            sep.setStyleSheet(f"background: {COLORS['border']}; border: none;")
            el.addWidget(sep)
            layout.addWidget(entry)

        layout.addStretch()
        return scroll
