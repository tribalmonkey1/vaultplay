"""
ui/setup_wizard.py — First-run setup wizard for VaultPlay

Shown only once on fresh install. Collects:
  - NAS Games Path
  - SteamGridDB API key (optional but recommended)
  - Default install path

Saves directly to settings DB. After completion, sets first_run_complete=true.
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

import os
from pathlib import Path
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QWidget, QFrame, QFileDialog, QStackedWidget,
    QScrollArea
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont

import db
from ui.style import COLORS, accent_button_style


class SetupWizard(QDialog):
    setup_complete = pyqtSignal(str)   # emits the NAS path

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Welcome to VaultPlay")
        self.setModal(True)
        self.setMinimumSize(680, 660)
        self.resize(720, 700)
        self.setStyleSheet(f"QDialog {{ background: {COLORS['surface']}; }}")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header
        hdr = QWidget()
        hdr.setStyleSheet(f"background: {COLORS['bg']};")
        hdr_l = QVBoxLayout(hdr)
        hdr_l.setContentsMargins(48, 40, 48, 28)
        hdr_l.setSpacing(10)

        logo = QLabel("⬡  VAULTPLAY")
        logo.setFont(QFont("Rajdhani", 28, QFont.Weight.Bold))
        logo.setStyleSheet(f"color: {COLORS['accent']}; letter-spacing: 3px;")
        hdr_l.addWidget(logo)

        subtitle = QLabel("Let's get you set up in under a minute.")
        subtitle.setFont(QFont("DM Sans", 14))
        subtitle.setStyleSheet(f"color: {COLORS['text_muted']};")
        hdr_l.addWidget(subtitle)
        root.addWidget(hdr)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background: {COLORS['border']}; border: none;")
        root.addWidget(sep)

        # Body — in a scroll area so content never clips regardless of dialog height
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        body = QWidget()
        body.setStyleSheet(f"background: {COLORS['surface']};")
        body_l = QVBoxLayout(body)
        body_l.setContentsMargins(48, 32, 48, 24)
        body_l.setSpacing(20)

        # ── NAS Path ──────────────────────────────────────────────────────────
        body_l.addWidget(self._section("1.  Where are your games stored on the NAS?",
            "Enter the path to your games folder (e.g. /mnt/GoldenNAS/Games). "
            "This folder's subfolders will become your library categories."))

        nas_row = QHBoxLayout()
        self.nas_edit = QLineEdit(db.get_setting("nas_path", ""))
        self.nas_edit.setFont(QFont("DM Mono", 11))
        self.nas_edit.setPlaceholderText("/mnt/Games")
        self.nas_edit.setMinimumHeight(38)
        self.nas_edit.setStyleSheet(f"color: {COLORS['accent2']}; padding: 8px 14px;")
        nas_row.addWidget(self.nas_edit, 1)

        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_nas)
        nas_row.addWidget(browse_btn)
        body_l.addLayout(nas_row)

        self.nas_status = QLabel("")
        self.nas_status.setFont(QFont("DM Sans", 10))
        self.nas_status.setStyleSheet("color: transparent;")
        body_l.addWidget(self.nas_status)

        # ── SteamGridDB ───────────────────────────────────────────────────────
        body_l.addWidget(self._section("2.  SteamGridDB API Key  (for cover art)",
            "Free at steamgriddb.com → Profile → API. Leave blank to skip art for now."))

        self.sgdb_edit = QLineEdit(db.get_setting("sgdb_api_key", ""))
        self.sgdb_edit.setFont(QFont("DM Mono", 11))
        self.sgdb_edit.setPlaceholderText("Paste your API key here (optional)")
        self.sgdb_edit.setMinimumHeight(38)
        self.sgdb_edit.setStyleSheet(f"color: {COLORS['accent2']}; padding: 8px 14px;")
        body_l.addWidget(self.sgdb_edit)

        # ── Install path ──────────────────────────────────────────────────────
        body_l.addWidget(self._section("3.  Where should games be installed?",
            "Default location on this machine for installing portable games."))

        install_row = QHBoxLayout()
        self.install_edit = QLineEdit(
            db.get_setting("install_path", str(Path.home() / "Games"))
        )
        self.install_edit.setFont(QFont("DM Mono", 11))
        self.install_edit.setMinimumHeight(38)
        self.install_edit.setStyleSheet(f"color: {COLORS['accent2']}; padding: 8px 14px;")
        install_row.addWidget(self.install_edit, 1)

        browse_install = QPushButton("Browse…")
        browse_install.clicked.connect(self._browse_install)
        install_row.addWidget(browse_install)
        body_l.addLayout(install_row)

        body_l.addStretch()
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        # Footer
        footer = QWidget()
        footer.setStyleSheet(
            f"background: {COLORS['surface']}; border-top: 1px solid {COLORS['border']};")
        foot_l = QHBoxLayout(footer)
        foot_l.setContentsMargins(48, 18, 48, 18)
        foot_l.setSpacing(12)

        skip_btn = QPushButton("Skip for now")
        skip_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; border: none;
                color: {COLORS['text_muted']}; padding: 0;
            }}
            QPushButton:hover {{ color: {COLORS['text']}; }}
        """)
        skip_btn.clicked.connect(self._skip)
        foot_l.addWidget(skip_btn)
        foot_l.addStretch()

        self.finish_btn = QPushButton("Save and Start Scan  →")
        self.finish_btn.setFont(QFont("Rajdhani", 14, QFont.Weight.Bold))
        self.finish_btn.setStyleSheet(accent_button_style())
        self.finish_btn.clicked.connect(self._finish)
        foot_l.addWidget(self.finish_btn)
        root.addWidget(footer)

    def _section(self, title: str, desc: str) -> QWidget:
        w = QWidget()
        w.setStyleSheet("background: transparent;")
        l = QVBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 4)
        l.setSpacing(5)
        t = QLabel(title)
        t.setFont(QFont("DM Sans", 13, QFont.Weight.Medium))
        t.setStyleSheet(f"color: {COLORS['text']};")
        l.addWidget(t)
        d = QLabel(desc)
        d.setFont(QFont("DM Sans", 11))
        d.setStyleSheet(f"color: {COLORS['text_muted']};")
        d.setWordWrap(True)
        l.addWidget(d)
        return w

    def _browse_nas(self):
        p = QFileDialog.getExistingDirectory(self, "Select NAS Games Folder",
                                             self.nas_edit.text() or "/mnt")
        if p:
            self.nas_edit.setText(p)

    def _browse_install(self):
        p = QFileDialog.getExistingDirectory(self, "Select Install Folder",
                                             self.install_edit.text())
        if p:
            self.install_edit.setText(p)

    def _validate_nas(self) -> bool:
        path = self.nas_edit.text().strip()
        if not path or path == "/":
            self.nas_status.setText("⚠  Please enter a valid NAS path")
            self.nas_status.setStyleSheet(f"color: {COLORS['danger']};")
            return False
        if not os.path.exists(path):
            self.nas_status.setText(f"⚠  Path not found: {path}")
            self.nas_status.setStyleSheet(f"color: {COLORS['danger']};")
            return False
        self.nas_status.setText(f"✓  Connected: {path}")
        self.nas_status.setStyleSheet("color: #4ade80;")
        return True

    def _save(self):
        nas = self.nas_edit.text().strip()
        sgdb = self.sgdb_edit.text().strip()
        install = self.install_edit.text().strip()
        if nas and nas != "/":
            db.set_setting("nas_path", nas)
        if sgdb:
            db.set_setting("sgdb_api_key", sgdb)
        if install:
            db.set_setting("install_path", install)
            import json
            db.set_install_paths([install])
        db.set_setting("first_run_complete", "true")

    def _finish(self):
        if not self._validate_nas():
            return
        self._save()
        nas = self.nas_edit.text().strip()
        self.accept()
        self.setup_complete.emit(nas)

    def _skip(self):
        self._save()
        db.set_setting("first_run_complete", "true")
        self.reject()
