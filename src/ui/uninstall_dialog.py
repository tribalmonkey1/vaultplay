"""
ui/uninstall_dialog.py — one-window uninstall chooser for VaultPlay.

Replaces the old three back-to-back QMessageBoxes (record → files → prefix)
with a single dialog of checkboxes. Pure UI: it only reports what the user
chose; GameDetailView performs the deletions.

Rules:
  * "Remove from VaultPlay" (install record + shortcut + launcher script) is
    checked by default. It is forced on (and locked) whenever game files or
    the Wine prefix are being deleted, so the record can never be left
    pointing at files that no longer exist.
  * Game files and Wine prefix are UNchecked by default (destructive).
  * Items whose path doesn't exist are shown disabled with "not found".
"""
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QPushButton, QFrame,
)

from ui.style import COLORS

_BASE = "background: transparent; border: none;"


class UninstallDialog(QDialog):
    def __init__(self, title: str, install_path: str, wine_prefix: str,
                 has_shortcuts: bool = True, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Uninstall Game")
        self.setModal(True)
        self.setMinimumWidth(480)
        self.resize(560, 10)
        self.setStyleSheet(f"QDialog {{ background: {COLORS['surface']}; }}")

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 16)
        root.setSpacing(12)

        head = QLabel(f"Uninstall <b>{title}</b>?")
        head.setFont(QFont("DM Sans", 13))
        head.setWordWrap(True)
        head.setStyleSheet(f"color: {COLORS['text']}; {_BASE}")
        root.addWidget(head)

        sub = QLabel("Choose what to remove. Anything left unchecked is kept.")
        sub.setFont(QFont("DM Sans", 10))
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color: {COLORS['text_muted']}; {_BASE}")
        root.addWidget(sub)

        box = QFrame()
        box.setStyleSheet(
            f"QFrame {{ background: {COLORS['surface2']}; border: 1px solid "
            f"{COLORS['border']}; border-radius: 10px; }}")
        bl = QVBoxLayout(box)
        bl.setContentsMargins(16, 12, 16, 14)
        bl.setSpacing(12)

        self.record_cb = self._option(
            bl, "Remove from VaultPlay",
            "Deletes the install record, desktop shortcut and launcher script. "
            "The game stays in your library as not installed.", checked=True)

        files_ok = bool(install_path) and Path(install_path).exists()
        self.files_cb = self._option(
            bl, "Delete game files",
            install_path if files_ok else "No install folder found",
            checked=False, enabled=files_ok, danger=True, mono_detail=True)

        prefix_ok = bool(wine_prefix) and Path(wine_prefix).exists()
        self.prefix_cb = self._option(
            bl, "Delete Wine prefix",
            (wine_prefix + "\nOnly do this if no other game shares this prefix.")
            if prefix_ok else "No Wine prefix found",
            checked=False, enabled=prefix_ok, danger=True, mono_detail=True)
        root.addWidget(box)

        self.warn = QLabel("Deleted files cannot be recovered.")
        self.warn.setFont(QFont("DM Sans", 10))
        self.warn.setWordWrap(True)
        self.warn.setStyleSheet(f"color: {COLORS['danger']}; {_BASE}")
        self.warn.setVisible(False)
        root.addWidget(self.warn)

        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        self.go_btn = QPushButton("Uninstall")
        self.go_btn.setStyleSheet(
            f"QPushButton {{ background: rgba(248,113,113,0.15); color: {COLORS['danger']};"
            " border: 1px solid rgba(248,113,113,0.5); border-radius: 6px; padding: 6px 16px; }"
            "QPushButton:hover { background: rgba(248,113,113,0.28); }"
            "QPushButton:disabled { color: #777; border-color: #444; background: transparent; }")
        self.go_btn.clicked.connect(self.accept)
        row.addWidget(self.go_btn)
        cancel.setDefault(True)
        root.addLayout(row)

        for cb in (self.record_cb, self.files_cb, self.prefix_cb):
            cb.toggled.connect(self._sync)
        self._sync()

    def _option(self, layout, text, detail, checked, enabled=True,
                danger=False, mono_detail=False):
        cb = QCheckBox(text)
        cb.setFont(QFont("DM Sans", 11))
        cb.setChecked(checked and enabled)
        cb.setEnabled(enabled)
        cb.setStyleSheet(f"color: {COLORS['danger'] if danger and enabled else COLORS['text']};"
                         f" {_BASE}")
        layout.addWidget(cb)
        d = QLabel(detail)
        d.setFont(QFont("DM Mono" if mono_detail else "DM Sans", 9 if mono_detail else 10))
        d.setWordWrap(True)
        d.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        d.setContentsMargins(26, 0, 0, 0)
        d.setStyleSheet(f"color: {COLORS['text_muted']}; {_BASE}")
        layout.addWidget(d)
        return cb

    def _sync(self):
        destructive = self.files_cb.isChecked() or self.prefix_cb.isChecked()
        if destructive:
            self.record_cb.blockSignals(True)
            self.record_cb.setChecked(True)
            self.record_cb.blockSignals(False)
            self.record_cb.setEnabled(False)
            self.record_cb.setToolTip(
                "Required when deleting files or the prefix, so the record "
                "never points at files that no longer exist.")
        else:
            self.record_cb.setEnabled(True)
            self.record_cb.setToolTip("")
        self.warn.setVisible(destructive)
        self.go_btn.setEnabled(
            self.record_cb.isChecked() or destructive)

    # ── result ────────────────────────────────────────────────────────────
    def remove_record(self) -> bool:  return self.record_cb.isChecked()
    def delete_files(self) -> bool:   return self.files_cb.isChecked()
    def delete_prefix(self) -> bool:  return self.prefix_cb.isChecked()
