"""
ui/exe_picker_dialog.py — Pick the game executable after an install

Shown (a) by InstallDialog when an installer added several candidate exes and
VaultPlay won't guess, and (b) by Cogwheel -> "Detect Executable…". Candidates
come from installer.find_new_prefix_exes() (exes added to the Wine prefix since
it was created), largest first. A manual Browse… row is always available.
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
_parent = _os.path.dirname(_here)
if _parent not in _sys.path:
    _sys.path.insert(0, _parent)
# ─────────────────────────────────────────────────────────────────────────────

from pathlib import Path
from typing import Optional

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QFrame, QWidget, QScrollArea, QButtonGroup, QRadioButton, QFileDialog
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont

import scanner
from ui.style import COLORS, accent_button_style

_LABEL_BASE = "background: transparent; border: none;"


class _ExeRow(QFrame):
    def __init__(self, group: QButtonGroup, path: Path, index: int, parent=None):
        super().__init__(parent)
        self.path = path
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(f"""
            QFrame {{
                background: {COLORS['surface2']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
            }}""")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(3)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.radio = QRadioButton()
        group.addButton(self.radio, index)
        top.addWidget(self.radio)
        try:
            size = scanner.format_size(path.stat().st_size)
        except OSError:
            size = ""
        name = QLabel(f"{path.name}" + (f"   ·   {size}" if size else ""))
        name.setFont(QFont("DM Sans", 11, QFont.Weight.Medium))
        name.setStyleSheet(f"color: {COLORS['text']}; {_LABEL_BASE}")
        top.addWidget(name, 1)
        lay.addLayout(top)

        loc = QLabel(str(path.parent))
        loc.setFont(QFont("DM Mono", 9))
        loc.setStyleSheet(f"color: {COLORS['text_muted']}; {_LABEL_BASE}")
        loc.setWordWrap(True)
        loc.setContentsMargins(24, 0, 0, 0)
        lay.addWidget(loc)

    def mousePressEvent(self, event):
        self.radio.setChecked(True)
        super().mousePressEvent(event)


class ExePickerDialog(QDialog):
    def __init__(self, candidates: list, start_dir: str = "",
                 intro: str = "", parent=None):
        super().__init__(parent)
        self._candidates = [Path(c) for c in candidates]
        self._start_dir = start_dir or str(Path.home())
        self._chosen: Optional[str] = None

        self.setWindowTitle("Select Game Executable")
        self.setModal(True)
        self.setMinimumWidth(560)
        self.setFixedWidth(600)
        self.setStyleSheet(f"QDialog {{ background: {COLORS['surface']}; }}")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        hdr = QWidget()
        hl = QVBoxLayout(hdr)
        hl.setContentsMargins(24, 20, 24, 12)
        hl.setSpacing(4)
        t = QLabel("Which file launches the game?")
        t.setFont(QFont("Rajdhani", 18, QFont.Weight.Bold))
        hl.addWidget(t)
        sub = QLabel(intro or (
            "These executables appeared in the game's Wine prefix after it "
            "was created, largest first. Pick the one that starts the game, "
            "or browse for it yourself."))
        sub.setFont(QFont("DM Sans", 11))
        sub.setStyleSheet(f"color: {COLORS['text_muted']};")
        sub.setWordWrap(True)
        hl.addWidget(sub)
        root.addWidget(hdr)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setMaximumHeight(380)
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(24, 8, 24, 16)
        bl.setSpacing(8)

        self._group = QButtonGroup(self)
        self._rows = []
        for i, p in enumerate(self._candidates):
            row = _ExeRow(self._group, p, i)
            self._rows.append(row)
            bl.addWidget(row)
        if self._rows:
            self._rows[0].radio.setChecked(True)

        manual = QFrame()
        manual.setStyleSheet(f"""
            QFrame {{
                background: {COLORS['surface2']};
                border: 1px dashed rgba(255,255,255,0.15);
                border-radius: 8px;
            }}""")
        ml = QVBoxLayout(manual)
        ml.setContentsMargins(12, 10, 12, 10)
        ml.setSpacing(6)
        self._manual_radio = QRadioButton(
            "None of these — choose a file" if self._rows else "Choose a file")
        self._manual_radio.setFont(QFont("DM Sans", 11))
        self._group.addButton(self._manual_radio, len(self._candidates))
        ml.addWidget(self._manual_radio)
        mrow = QHBoxLayout()
        self._manual_edit = QLineEdit()
        self._manual_edit.setFont(QFont("DM Mono", 10))
        self._manual_edit.setPlaceholderText("/path/to/game.exe")
        self._manual_edit.setStyleSheet(f"color: {COLORS['accent2']};")
        self._manual_edit.textChanged.connect(
            lambda t: t.strip() and self._manual_radio.setChecked(True))
        mrow.addWidget(self._manual_edit, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        mrow.addWidget(browse)
        ml.addLayout(mrow)
        bl.addWidget(manual)
        if not self._rows:
            self._manual_radio.setChecked(True)
        bl.addStretch()
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        footer = QWidget()
        footer.setStyleSheet(
            f"background: {COLORS['surface']}; border-top: 1px solid {COLORS['border']};")
        fl = QHBoxLayout(footer)
        fl.setContentsMargins(24, 14, 24, 14)
        skip = QPushButton("Skip for now")
        skip.setStyleSheet(f"""
            QPushButton {{ background: transparent; border: none;
                color: {COLORS['text_muted']}; padding: 8px 4px; }}
            QPushButton:hover {{ color: {COLORS['text']}; }}""")
        skip.clicked.connect(self.reject)
        fl.addWidget(skip)
        fl.addStretch()
        self.use_btn = QPushButton("Use This Executable →")
        self.use_btn.setFont(QFont("Rajdhani", 13, QFont.Weight.Bold))
        self.use_btn.setStyleSheet(accent_button_style())
        self.use_btn.clicked.connect(self._on_use)
        fl.addWidget(self.use_btn)
        root.addWidget(footer)

    def _browse(self):
        start = self._manual_edit.text().strip() or self._start_dir
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Game Executable", start, "Executables (*.exe);;All files (*)")
        if path:
            self._manual_edit.setText(path)
            self._manual_radio.setChecked(True)

    def _on_use(self):
        cid = self._group.checkedId()
        if cid == -1:
            return
        if cid == len(self._candidates):
            manual = self._manual_edit.text().strip()
            if not manual or not Path(manual).is_file():
                self._manual_edit.setStyleSheet(
                    f"color: {COLORS['danger']}; border: 1px solid {COLORS['danger']};")
                return
            self._chosen = manual
        else:
            self._chosen = str(self._candidates[cid])
        self.accept()

    def selected_path(self) -> Optional[str]:
        return self._chosen
