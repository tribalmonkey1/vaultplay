"""
ui/wishlist_restore_dialog.py — Bring back removed wishlist items

Diffs the automatic wishlist backups (wishlist_store.list_backups()) against
the live wishlist and shows a tile for every item that exists in a backup but
no longer in the wishlist. Tick the ones to bring back; they're re-added with
their original details/cover, at the bottom of the priority list.
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

import datetime
import logging

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame, QWidget,
    QScrollArea, QComboBox, QCheckBox, QGridLayout, QMessageBox
)
from PyQt6.QtCore import Qt, pyqtSignal, QRunnable, QThreadPool, pyqtSlot, QObject
from PyQt6.QtGui import QPixmap, QFont

import metadata as meta_mod
import wishlist_store
from ui.style import COLORS, accent_button_style

log = logging.getLogger(__name__)

TILE_W = 120
COLS = 5


class _Sig(QObject):
    loaded = pyqtSignal(str, str)   # item_id, local_path


class _CoverLoader(QRunnable):
    def __init__(self, item_id: str, cover_value: str):
        super().__init__()
        self.item_id = item_id
        self.cover_value = cover_value
        self.signals = _Sig()

    @pyqtSlot()
    def run(self):
        try:
            if wishlist_store.is_local_art_path(self.cover_value):
                resolved = wishlist_store.resolve_cover_path(self.cover_value)
            else:
                resolved = meta_mod.download_art(self.cover_value)
            if resolved:
                self.signals.loaded.emit(self.item_id, resolved)
        except Exception as e:
            log.debug("restore dialog cover load failed: %s", e)


class _RestoreTile(QFrame):
    toggled = pyqtSignal()

    def __init__(self, record: dict, parent=None):
        super().__init__(parent)
        self.item = record["item"]
        cover_h = int(TILE_W * 1.5)
        self.setFixedSize(TILE_W, cover_h + 62)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._style(False)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.cover = QLabel("🌟")
        self.cover.setFixedSize(TILE_W, cover_h)
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover.setFont(QFont("DM Sans", 20))
        self.cover.setStyleSheet(
            f"background: {COLORS['surface2']}; color: {COLORS['text_muted']}; border: none;")
        lay.addWidget(self.cover)

        foot = QWidget()
        foot.setStyleSheet("background: transparent; border: none;")
        fl = QVBoxLayout(foot)
        fl.setContentsMargins(8, 6, 8, 6)
        fl.setSpacing(2)

        self.check = QCheckBox()
        title = self.item.get("title") or "(untitled)"
        self.check.setText(
            self.check.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, TILE_W - 40))
        self.check.setToolTip(title)
        self.check.setStyleSheet(f"color: {COLORS['text']}; background: transparent;")
        self.check.toggled.connect(self._on_check)
        fl.addWidget(self.check)

        when = ""
        if record.get("backup_mtime"):
            when = datetime.datetime.fromtimestamp(record["backup_mtime"]).strftime("%b %d")
        sub = QLabel(f"in backup {when}" if when else "")
        sub.setFont(QFont("DM Mono", 8))
        sub.setStyleSheet(f"color: {COLORS['text_muted']}; background: transparent;")
        fl.addWidget(sub)
        lay.addWidget(foot)

    def _style(self, on: bool):
        border = COLORS["accent"] if on else COLORS["border"]
        self.setStyleSheet(f"""
            QFrame {{ background: {COLORS['surface']}; border: 2px solid {border};
                      border-radius: 10px; }}""")

    def _on_check(self, on: bool):
        self._style(on)
        self.toggled.emit()

    def is_checked(self) -> bool:
        return self.check.isChecked()

    def set_checked(self, on: bool):
        self.check.setChecked(on)

    def set_cover(self, path: str):
        pix = QPixmap(path)
        if pix.isNull():
            return
        h = int(TILE_W * 1.5)
        s = pix.scaled(TILE_W, h, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                       Qt.TransformationMode.SmoothTransformation)
        s = s.copy(max(0, (s.width() - TILE_W) // 2), max(0, (s.height() - h) // 2), TILE_W, h)
        self.cover.setPixmap(s)
        self.cover.setText("")
        self.cover.setStyleSheet("background: transparent; border: none;")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.check.toggle()


class WishlistRestoreDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._tiles: list = []
        self._restored = 0
        self._pool = QThreadPool.globalInstance()
        self._gen = 0   # bumps on every rebuild so stale cover loads are ignored

        self.setWindowTitle("Restore Wishlist Items")
        self.setModal(True)
        self.resize(720, 640)
        self.setStyleSheet(f"QDialog {{ background: {COLORS['surface']}; }}")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        hdr = QWidget()
        hl = QVBoxLayout(hdr)
        hl.setContentsMargins(24, 20, 24, 10)
        hl.setSpacing(6)
        t = QLabel("Restore removed items")
        t.setFont(QFont("Rajdhani", 18, QFont.Weight.Bold))
        hl.addWidget(t)
        sub = QLabel("Items below are in a backup but no longer in your wishlist. "
                     "Tick the ones to bring back.")
        sub.setWordWrap(True)
        sub.setFont(QFont("DM Sans", 11))
        sub.setStyleSheet(f"color: {COLORS['text_muted']};")
        hl.addWidget(sub)

        row = QHBoxLayout()
        row.addWidget(QLabel("Compare against:"))
        self.combo = QComboBox()
        self.combo.addItem("All backups (combined)", None)
        for b in wishlist_store.list_backups():
            when = datetime.datetime.fromtimestamp(b["mtime"]).strftime("%b %d, %Y  %H:%M")
            self.combo.addItem(f"{when} — {b['count']} items", b["path"])
        self.combo.currentIndexChanged.connect(self._populate)
        row.addWidget(self.combo, 1)
        hl.addLayout(row)
        root.addWidget(hdr)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.body = QWidget()
        self.grid = QGridLayout(self.body)
        self.grid.setContentsMargins(24, 8, 24, 16)
        self.grid.setSpacing(14)
        self.scroll.setWidget(self.body)
        root.addWidget(self.scroll, 1)

        self.empty = QLabel("")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        self.empty.setStyleSheet(f"color: {COLORS['text_muted']}; padding: 40px;")
        root.addWidget(self.empty)

        footer = QWidget()
        footer.setStyleSheet(
            f"background: {COLORS['surface']}; border-top: 1px solid {COLORS['border']};")
        fl = QHBoxLayout(footer)
        fl.setContentsMargins(24, 12, 24, 12)
        self.all_btn = QPushButton("Select all")
        self.all_btn.clicked.connect(self._select_all)
        fl.addWidget(self.all_btn)
        self.count_lbl = QLabel("")
        self.count_lbl.setStyleSheet(f"color: {COLORS['text_muted']};")
        fl.addWidget(self.count_lbl)
        fl.addStretch()
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        fl.addWidget(close)
        self.restore_btn = QPushButton("Restore Selected")
        self.restore_btn.setStyleSheet(accent_button_style())
        self.restore_btn.clicked.connect(self._do_restore)
        fl.addWidget(self.restore_btn)
        root.addWidget(footer)

        self._populate()

    def restored_count(self) -> int:
        return self._restored

    def _populate(self):
        self._gen += 1
        for t in self._tiles:
            t.deleteLater()
        self._tiles = []
        records = wishlist_store.get_deleted_from_backups(self.combo.currentData())
        gen = self._gen
        for i, rec in enumerate(records):
            tile = _RestoreTile(rec)
            tile.toggled.connect(self._update_footer)
            self.grid.addWidget(tile, i // COLS, i % COLS,
                                alignment=Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
            self._tiles.append(tile)
            cover = rec["item"].get("cover_url") or ""
            if cover:
                ld = _CoverLoader(rec["item"]["id"], cover)
                ld.signals.loaded.connect(
                    lambda iid, path, t=tile, g=gen: t.set_cover(path) if g == self._gen else None)
                self._pool.start(ld)
        has = bool(self._tiles)
        self.scroll.setVisible(has)
        self.empty.setVisible(not has)
        if not has:
            self.empty.setText(
                "Nothing to restore — every item in the backups is already on your wishlist."
                if self.combo.count() > 1 else
                "No backups yet. Backups are created automatically each time the wishlist changes.")
        self.all_btn.setEnabled(has)
        self._update_footer()

    def _selected(self) -> list:
        return [t for t in self._tiles if t.is_checked()]

    def _update_footer(self):
        n = len(self._selected())
        self.count_lbl.setText(f"{n} selected" if self._tiles else "")
        self.restore_btn.setEnabled(n > 0)

    def _select_all(self):
        on = not all(t.is_checked() for t in self._tiles)
        for t in self._tiles:
            t.set_checked(on)

    def _do_restore(self):
        items = [t.item for t in self._selected()]
        if not items:
            return
        n = wishlist_store.restore_items(items)
        if n == 0:
            QMessageBox.warning(self, "Restore", "Could not write the wishlist — "
                                "check that the wishlist folder is reachable.")
            return
        self._restored += n
        self._populate()
