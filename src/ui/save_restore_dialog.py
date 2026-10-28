"""
ui/save_restore_dialog.py — "We found your backed-up save" prompt.

Shown before a game launches when VaultPlay finds a backup (saves and/or
achievements) that isn't linked into this prefix yet — typically after a fresh
install or a recreated Wine prefix. Pure UI; see save_restore.restore_all().

Result: exec() == Accepted → restore what's ticked.
        self.never is True → user chose "Don't ask again for this game".
"""
import datetime
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QPushButton, QFrame,
)

from ui.style import COLORS, accent_button_style

_BASE = "background: transparent; border: none;"


class SaveRestoreDialog(QDialog):
    def __init__(self, title: str, info: dict, parent=None):
        super().__init__(parent)
        self.never = False
        self.setWindowTitle("Restore Your Backup")
        self.setModal(True)
        self.setMinimumWidth(500)
        self.resize(580, 10)
        self.setStyleSheet(f"QDialog {{ background: {COLORS['surface']}; }}")

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 16)
        root.setSpacing(12)

        head = QLabel(f"Found your backed-up progress for <b>{title}</b>")
        head.setWordWrap(True)
        head.setFont(QFont("DM Sans", 13))
        head.setStyleSheet(f"color: {COLORS['text']}; {_BASE}")
        root.addWidget(head)

        sub = QLabel(
            "This game isn't linked to your backup in this install yet. Restore "
            "it now so you continue where you left off. Anything currently in "
            "those locations is moved to a “_replaced” folder next to your "
            "backups — never deleted.")
        sub.setWordWrap(True)
        sub.setFont(QFont("DM Sans", 10))
        sub.setStyleSheet(f"color: {COLORS['text_muted']}; {_BASE}")
        root.addWidget(sub)

        box = QFrame()
        box.setStyleSheet(
            f"QFrame {{ background: {COLORS['surface2']}; border: 1px solid "
            f"{COLORS['border']}; border-radius: 10px; }}")
        bl = QVBoxLayout(box)
        bl.setContentsMargins(16, 12, 16, 14)
        bl.setSpacing(10)

        self.main_cb = self.extras_cb = None
        m = info.get("main")
        if m:
            when = ""
            if m.get("newest"):
                when = ", last changed " + datetime.datetime.fromtimestamp(
                    m["newest"]).strftime("%b %d %Y")
            self.main_cb = self._opt(
                bl, "Save game",
                f"{m['files']} file(s){when}\n→ {m['target']}")
        ex = info.get("extras") or []
        if ex:
            names = ", ".join(e["name"] for e in ex[:6]) + ("…" if len(ex) > 6 else "")
            self.extras_cb = self._opt(
                bl, "Achievements / stats", f"{len(ex)} file(s): {names}")
        root.addWidget(box)

        row = QHBoxLayout()
        never = QPushButton("Don't ask again for this game")
        never.clicked.connect(self._never)
        row.addWidget(never)
        row.addStretch()
        later = QPushButton("Not now")
        later.clicked.connect(self.reject)
        row.addWidget(later)
        go = QPushButton("Restore")
        go.setStyleSheet(accent_button_style())
        go.setDefault(True)
        go.clicked.connect(self.accept)
        row.addWidget(go)
        root.addLayout(row)

    def _opt(self, layout, text, detail):
        cb = QCheckBox(text)
        cb.setChecked(True)
        cb.setFont(QFont("DM Sans", 11))
        cb.setStyleSheet(f"color: {COLORS['text']}; {_BASE}")
        layout.addWidget(cb)
        d = QLabel(detail)
        d.setWordWrap(True)
        d.setFont(QFont("DM Mono", 9))
        d.setContentsMargins(26, 0, 0, 0)
        d.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        d.setStyleSheet(f"color: {COLORS['text_muted']}; {_BASE}")
        layout.addWidget(d)
        return cb

    def _never(self):
        self.never = True
        self.reject()

    def want_main(self) -> bool:
        return bool(self.main_cb and self.main_cb.isChecked())

    def want_extras(self) -> bool:
        return bool(self.extras_cb and self.extras_cb.isChecked())
