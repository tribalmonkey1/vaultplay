"""
ui/save_backup_dialog.py — Post-play save location confirmation dialog

Shown after a game closes when Save Backup (Flow 1, see save_backup.py)
has detected one or more candidate save folders — either from the
known-locations fast path or from diffing the Wine prefix against a
pre-launch snapshot. The user picks one candidate, or enters a path
manually. The dialog is modal with no auto-dismiss — per spec, the prompt
persists until the user explicitly responds.
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

import logging
from pathlib import Path

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QWidget, QScrollArea, QLineEdit, QButtonGroup,
    QRadioButton, QFileDialog
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont

from ui.style import COLORS, accent_button_style

log = logging.getLogger(__name__)


class _CandidateRow(QFrame):
    """One selectable candidate folder row, with a radio button."""

    def __init__(self, group: QButtonGroup, candidate: dict, index: int, parent=None):
        super().__init__(parent)
        self.candidate = candidate
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(f"""
            QFrame {{
                background: {COLORS['surface2']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
            }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.radio = QRadioButton()
        group.addButton(self.radio, index)
        top.addWidget(self.radio)

        path_lbl = QLabel(str(candidate["path"]))
        path_lbl.setFont(QFont("DM Mono", 10))
        path_lbl.setStyleSheet(
            f"color: {COLORS['accent2']}; background: transparent; border: none;")
        path_lbl.setWordWrap(True)
        top.addWidget(path_lbl, 1)
        layout.addLayout(top)

        n = candidate.get("file_count", 0)
        sample = candidate.get("sample_files", [])
        detail = f"{n} file{'s' if n != 1 else ''}"
        if sample:
            shown = ", ".join(sample[:3])
            detail += f"  ·  {shown}"
            if n > len(sample[:3]):
                detail += "…"
        detail_lbl = QLabel(detail)
        detail_lbl.setFont(QFont("DM Sans", 9))
        detail_lbl.setStyleSheet(
            f"color: {COLORS['text_muted']}; background: transparent; border: none;")
        detail_lbl.setContentsMargins(24, 0, 0, 0)
        layout.addWidget(detail_lbl)

    def mousePressEvent(self, event):
        self.radio.setChecked(True)
        super().mousePressEvent(event)


class SaveBackupDialog(QDialog):
    """
    Shown after a play session when candidate save folders were detected.
    exec() returns Accepted with chosen_path() set, or Rejected (skip —
    the caller leaves save_source_path unset so Flow 1 runs again the
    next time this game is played).
    """

    def __init__(self, game_title: str, candidates: list, parent=None):
        super().__init__(parent)
        self._candidates  = candidates
        self._chosen_path = None

        self.setWindowTitle(f"Back Up Save — {game_title}")
        self.setModal(True)
        self.setMinimumWidth(560)
        self.setFixedWidth(580)
        self.setStyleSheet(f"QDialog {{ background: {COLORS['surface']}; }}")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Header ────────────────────────────────────────────────────────────
        hdr = QWidget()
        hdr.setStyleSheet(f"background: {COLORS['surface']};")
        hdr_l = QVBoxLayout(hdr)
        hdr_l.setContentsMargins(24, 20, 24, 12)
        hdr_l.setSpacing(3)

        h_title = QLabel("Save Files Detected")
        h_title.setFont(QFont("Rajdhani", 18, QFont.Weight.Bold))
        hdr_l.addWidget(h_title)

        h_sub = QLabel(
            f"VaultPlay noticed changed files after playing {game_title}. "
            "Pick the folder that holds your save so it can be backed up "
            "and kept safe if this game's Wine prefix is ever deleted."
        )
        h_sub.setFont(QFont("DM Sans", 11))
        h_sub.setStyleSheet(f"color: {COLORS['text_muted']};")
        h_sub.setWordWrap(True)
        hdr_l.addWidget(h_sub)
        root.addWidget(hdr)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background: {COLORS['border']}; border: none;")
        root.addWidget(sep)

        # ── Scrollable candidate list ────────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMaximumHeight(360)

        body = QWidget()
        body.setStyleSheet(f"background: {COLORS['surface']};")
        body_l = QVBoxLayout(body)
        body_l.setContentsMargins(24, 16, 24, 16)
        body_l.setSpacing(8)

        self._group = QButtonGroup(self)
        self._rows: list[_CandidateRow] = []
        for i, cand in enumerate(candidates):
            row = _CandidateRow(self._group, cand, i)
            self._rows.append(row)
            body_l.addWidget(row)
        if self._rows:
            self._rows[0].radio.setChecked(True)

        # Manual entry — always available as a fallback
        manual_frame = QFrame()
        manual_frame.setStyleSheet(f"""
            QFrame {{
                background: {COLORS['surface2']};
                border: 1px dashed rgba(255,255,255,0.15);
                border-radius: 8px;
            }}
        """)
        manual_l = QVBoxLayout(manual_frame)
        manual_l.setContentsMargins(12, 10, 12, 10)
        manual_l.setSpacing(6)

        self._manual_radio = QRadioButton("None of these — enter manually")
        self._manual_radio.setFont(QFont("DM Sans", 11))
        self._group.addButton(self._manual_radio, len(candidates))
        manual_l.addWidget(self._manual_radio)

        manual_row = QHBoxLayout()
        manual_row.setSpacing(8)
        self._manual_edit = QLineEdit()
        self._manual_edit.setFont(QFont("DM Mono", 10))
        self._manual_edit.setPlaceholderText("/path/inside/wine/prefix/to/save/folder")
        self._manual_edit.setStyleSheet(f"color: {COLORS['accent2']};")
        self._manual_edit.textChanged.connect(self._on_manual_text_changed)
        manual_row.addWidget(self._manual_edit, 1)

        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_manual)
        manual_row.addWidget(browse_btn)
        manual_l.addLayout(manual_row)

        body_l.addWidget(manual_frame)
        body_l.addStretch()

        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        # ── Footer ────────────────────────────────────────────────────────────
        footer = QWidget()
        footer.setStyleSheet(
            f"background: {COLORS['surface']}; border-top: 1px solid {COLORS['border']};")
        foot_l = QHBoxLayout(footer)
        foot_l.setContentsMargins(24, 14, 24, 14)
        foot_l.setSpacing(10)

        skip_btn = QPushButton("Skip — ask me next time")
        skip_btn.setFont(QFont("DM Sans", 11))
        skip_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; border: none;
                color: {COLORS['text_muted']}; padding: 8px 4px;
            }}
            QPushButton:hover {{ color: {COLORS['text']}; }}
        """)
        skip_btn.clicked.connect(self.reject)
        foot_l.addWidget(skip_btn)
        foot_l.addStretch()

        self.confirm_btn = QPushButton("Back Up This Save →")
        self.confirm_btn.setFont(QFont("Rajdhani", 13, QFont.Weight.Bold))
        self.confirm_btn.setStyleSheet(accent_button_style())
        self.confirm_btn.clicked.connect(self._on_confirm)
        foot_l.addWidget(self.confirm_btn)

        root.addWidget(footer)

    def _on_manual_text_changed(self, text: str):
        if text.strip():
            self._manual_radio.setChecked(True)
        self._manual_edit.setStyleSheet(f"color: {COLORS['accent2']};")

    def _browse_manual(self):
        start = self._manual_edit.text().strip() or str(Path.home())
        path = QFileDialog.getExistingDirectory(self, "Select Save Folder", start)
        if path:
            self._manual_edit.setText(path)
            self._manual_radio.setChecked(True)

    def _on_confirm(self):
        checked_id = self._group.checkedId()
        if checked_id == -1:
            return  # nothing selected — leave dialog open
        if checked_id == len(self._candidates):
            manual = self._manual_edit.text().strip()
            if not manual or not Path(manual).exists():
                self._manual_edit.setStyleSheet(
                    f"color: {COLORS['danger']}; border: 1px solid {COLORS['danger']};")
                return
            self._chosen_path = manual
        else:
            self._chosen_path = str(self._candidates[checked_id]["path"])
        self.accept()

    def chosen_path(self) -> str | None:
        return self._chosen_path
