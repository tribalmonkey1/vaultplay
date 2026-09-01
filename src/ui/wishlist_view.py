"""
ui/wishlist_view.py — Wishlist page for VaultPlay

Spec: Notion → Features → Fully Planned → Wishlist.

Own top-level page (not a Library filter) — swapped into MainWindow.stack
the same way library_view/detail_view/settings_view are. Grid of
lightweight WishlistTile widgets — deliberately NOT GameTile (no install
badge, no favorite star, no "Now Playing" overlay) and NOT virtualized
the way library_view.py's grid is, since wishlist lists are expected to
be small.

Drag-and-drop reorder reuses the exact QDrag/QMimeData +
insertion-line-indicator pattern already built for Collections' Lock B in
library_view.py (new MIME type, same interaction shape) — see
_WishlistCanvas / _drop_index_at / _paint_drag_indicator below, which
mirror LibraryView's _LibraryCanvas / _drop_index_at / _paint_drag_indicator
almost exactly, just without the "is this collection locked" gate (whole
Wishlist grid is always reorderable — priority order is the only order
this view has, per spec).

Since the backing store is a shared NAS file another machine may also be
editing (see wishlist_store.py), this view polls wishlist.json's mtime
every few seconds while it's the visible page and reloads on change —
not real-time push, just a short poll (see refresh()/_poll_for_changes()).
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
from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QScrollArea, QLabel, QPushButton,
    QFrame, QMenu, QMessageBox
)
from PyQt6.QtCore import (
    Qt, pyqtSignal, QTimer, QRunnable, QThreadPool, pyqtSlot, QObject, QPoint,
    QMimeData
)
from PyQt6.QtGui import QPixmap, QFont, QColor, QPainter, QPen, QDrag

import metadata as meta_mod
import wishlist_store
from ui.style import COLORS, accent_button_style
from ui.wishlist_dialog import WishlistItemDialog

log = logging.getLogger(__name__)

_WISHLIST_DRAG_MIME = "application/x-vaultplay-wishlist-id"


# ── Async cover loader ────────────────────────────────────────────────────────

class _ThumbSignals(QObject):
    loaded = pyqtSignal(str, str)   # item_id, local_path


class _ThumbLoader(QRunnable):
    """Resolves either a URL (download+cache via metadata.download_art(),
    same as every other art field in the app) or a wishlist_art/... local
    path (resolved against this machine's own wishlist_path, no network
    needed)."""

    def __init__(self, item_id: str, cover_value: str):
        super().__init__()
        self.item_id = item_id
        self.cover_value = cover_value
        self.signals = _ThumbSignals()

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
            log.debug("_ThumbLoader failed for %s: %s", self.cover_value, e)


# ── Wishlist tile ──────────────────────────────────────────────────────────────

class WishlistTile(QFrame):
    # Left-click opens the same Edit dialog the context menu's "Edit…"
    # does — there's no detail page for a wishlist item to navigate to
    # (no install/launch/playtime state at all), so a click has nothing
    # else useful to do.
    clicked          = pyqtSignal(str)   # item_id
    edit_requested   = pyqtSignal(str)   # item_id
    remove_requested = pyqtSignal(str)   # item_id

    def __init__(self, item: dict, tile_width: int, parent=None):
        super().__init__(parent)
        self.item_id    = item["id"]
        self.tile_width = tile_width
        self._press_pos: Optional[QPoint] = None
        self._drag_started = False
        self._DRAG_THRESHOLD = 10

        cover_h = int(tile_width * 1.5)
        tile_h  = cover_h + 44

        self.setFixedSize(tile_width, tile_h)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self.setStyleSheet(f"""
            QFrame {{
                background: {COLORS['surface']};
                border: 1px solid {COLORS['border']};
                border-radius: 10px;
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.cover_label = QLabel("🌟")
        self.cover_label.setFixedSize(tile_width, cover_h)
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_label.setFont(QFont("DM Sans", 22))
        self.cover_label.setStyleSheet(
            f"background: {COLORS['surface2']}; color: {COLORS['text_muted']}; border: none;")
        layout.addWidget(self.cover_label)

        footer = QWidget()
        footer.setStyleSheet(f"background: {COLORS['surface']}; border: none;")
        footer_l = QVBoxLayout(footer)
        footer_l.setContentsMargins(10, 8, 10, 10)
        footer_l.setSpacing(4)

        self.title_label = QLabel()
        self.title_label.setFont(QFont("DM Sans", 10, QFont.Weight.Medium))
        self.title_label.setStyleSheet(f"color: {COLORS['text']}; background: transparent;")
        self.title_label.setFixedWidth(tile_width - 20)
        footer_l.addWidget(self.title_label)

        self.release_chip = QLabel()
        self.release_chip.setFont(QFont("DM Mono", 8))
        self.release_chip.setStyleSheet(f"""
            color: {COLORS['accent']};
            background: rgba(232,199,106,0.10);
            border-radius: 4px;
            padding: 1px 5px;
        """)
        self.release_chip.setFixedWidth(tile_width - 20)
        chip_row = QHBoxLayout()
        chip_row.setContentsMargins(0, 0, 0, 0)
        chip_row.addWidget(self.release_chip)
        chip_row.addStretch()
        footer_l.addLayout(chip_row)
        self.release_chip.hide()

        layout.addWidget(footer)

        self._apply_item(item)

    def _apply_item(self, item: dict):
        self.item_id = item["id"]
        title = item.get("title") or "(untitled)"
        elided = self.title_label.fontMetrics().elidedText(
            title, Qt.TextElideMode.ElideRight, self.tile_width - 20)
        self.title_label.setText(elided)
        self.title_label.setToolTip(title)

        release = (item.get("release_date") or "").strip()
        if release:
            chip_text = f"Releases: {release}"
            elided_chip = self.release_chip.fontMetrics().elidedText(
                chip_text, Qt.TextElideMode.ElideRight, self.tile_width - 24)
            self.release_chip.setText(elided_chip)
            self.release_chip.setToolTip(chip_text)
            self.release_chip.show()
        else:
            self.release_chip.hide()

    def set_cover_pixmap(self, pixmap: QPixmap):
        self.cover_label.setPixmap(pixmap)
        self.cover_label.setText("")
        self.cover_label.setStyleSheet("background: transparent; border: none;")

    # ── Click vs. drag ────────────────────────────────────────────────────────

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._press_pos = event.position().toPoint()
        self._drag_started = False

    def mouseMoveEvent(self, event):
        if self._press_pos is None or self._drag_started:
            return
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            return
        moved = (event.position().toPoint() - self._press_pos).manhattanLength()
        if moved < self._DRAG_THRESHOLD:
            return
        self._drag_started = True
        self._start_drag()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._drag_started:
            self._press_pos = None
            self._drag_started = False
            return
        if self._press_pos is not None:
            self.clicked.emit(self.item_id)
        self._press_pos = None

    def _start_drag(self):
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(_WISHLIST_DRAG_MIME, self.item_id.encode("utf-8"))
        drag.setMimeData(mime)

        pixmap = self.grab()
        ghost = QPixmap(pixmap.size())
        ghost.fill(Qt.GlobalColor.transparent)
        painter = QPainter(ghost)
        painter.setOpacity(0.72)
        painter.drawPixmap(0, 0, pixmap)
        painter.end()
        drag.setPixmap(ghost)
        drag.setHotSpot(QPoint(pixmap.width() // 2, pixmap.height() // 2))

        drag.exec(Qt.DropAction.MoveAction)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{
                background: {COLORS['surface2']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
                padding: 4px 0;
                color: {COLORS['text']};
            }}
            QMenu::item {{ padding: 7px 20px 7px 14px; font-size: 12px; }}
            QMenu::item:selected {{ background: {COLORS['surface3']}; border-radius: 4px; }}
            QMenu::separator {{ height: 1px; background: {COLORS['border']}; margin: 3px 8px; }}
        """)
        edit_action = menu.addAction("✏️  Edit…")
        menu.addSeparator()
        remove_action = menu.addAction("✕  Remove from Wishlist")
        chosen = menu.exec(event.globalPos())
        if chosen == edit_action:
            self.edit_requested.emit(self.item_id)
        elif chosen == remove_action:
            self.remove_requested.emit(self.item_id)


# ── Canvas (drag/drop target + insertion-line paint) ──────────────────────────

class _WishlistCanvas(QWidget):
    """Mirrors library_view.py's _LibraryCanvas: a plain positioning
    surface (tiles placed via setGeometry(), no QLayout) that forwards
    drag/drop events to its owner and paints whatever insertion index the
    owner computed. All actual reorder math lives on WishlistView."""

    def __init__(self, owner: "WishlistView", parent=None):
        super().__init__(parent)
        self._owner = owner
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        self._owner._on_canvas_drag_enter(event)

    def dragMoveEvent(self, event):
        self._owner._on_canvas_drag_move(event)

    def dragLeaveEvent(self, event):
        self._owner._on_canvas_drag_leave(event)

    def dropEvent(self, event):
        self._owner._on_canvas_drop(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        self._owner._paint_drag_indicator(self)


# ── Main view ─────────────────────────────────────────────────────────────────

class WishlistView(QWidget):
    back_requested = pyqtSignal()
    # Emitted after any local add/edit/remove/reorder so MainWindow can
    # refresh the sidebar's wishlist count badge — same "something
    # changed elsewhere, refresh" pattern install_finished/
    # collections_changed already establish on the other views.
    changed = pyqtSignal()

    MARGIN_LEFT   = 28
    MARGIN_TOP    = 24
    MARGIN_RIGHT  = 28
    MARGIN_BOTTOM = 28
    ROW_SPACING   = 16
    COL_SPACING   = 16
    TILE_W        = 150
    POLL_INTERVAL_MS = 4000   # short poll for another machine's edits — see module docstring

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list = []
        self._tiles: dict[str, WishlistTile] = {}
        self._cover_cache: dict[str, QPixmap] = {}
        self._cols   = 1
        self._tile_h = 0
        self._row_h  = 0
        self._last_mtime: Optional[float] = None
        self._drag_insert_index: Optional[int] = None
        self._pool = QThreadPool.globalInstance()

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(self.POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._poll_for_changes)

        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(150)
        self._resize_timer.timeout.connect(self._rebuild)

        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Back bar — same convention as settings_view.py/game_detail.py's
        # non-Library top-level pages ────────────────────────────────────────
        back_bar = QWidget()
        back_bar.setFixedHeight(44)
        back_bar.setStyleSheet(
            f"background: {COLORS['surface']}; border-bottom: 1px solid {COLORS['border']};")
        back_l = QHBoxLayout(back_bar)
        back_l.setContentsMargins(20, 0, 20, 0)
        back_btn = QPushButton("← Back to Library")
        back_btn.setFont(QFont("DM Sans", 11))
        back_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; border: none; color: {COLORS['text_muted']}; padding: 0; }}
            QPushButton:hover {{ color: {COLORS['text']}; }}
        """)
        back_btn.clicked.connect(self.back_requested)
        back_l.addWidget(back_btn)
        back_l.addStretch()
        root.addWidget(back_bar)

        # ── Header ────────────────────────────────────────────────────────────
        header = QWidget()
        header.setStyleSheet(
            f"background: {COLORS['bg']}; border-bottom: 1px solid {COLORS['border']};")
        header_l = QHBoxLayout(header)
        header_l.setContentsMargins(28, 16, 28, 16)
        header_l.setSpacing(10)

        title_lbl = QLabel("🌟  Wishlist")
        title_lbl.setFont(QFont("Rajdhani", 18, QFont.Weight.DemiBold))
        header_l.addWidget(title_lbl)
        header_l.addStretch()

        self.add_btn = QPushButton("➕  Add to Wishlist")
        self.add_btn.setFont(QFont("DM Sans", 11, QFont.Weight.Medium))
        self.add_btn.setStyleSheet(accent_button_style())
        self.add_btn.clicked.connect(self._on_add_clicked)
        header_l.addWidget(self.add_btn)
        root.addWidget(header)

        # ── Inline status message — shown instead of the grid when
        # wishlist_path is unconfigured or unreachable ─────────────────────
        self.status_label = QLabel("")
        self.status_label.setFont(QFont("DM Sans", 12))
        self.status_label.setStyleSheet(f"color: {COLORS['text_muted']}; padding: 60px;")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setWordWrap(True)
        self.status_label.hide()
        root.addWidget(self.status_label)

        # ── Scroll area + canvas ─────────────────────────────────────────────
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet(f"background: {COLORS['bg']}; border: none;")

        self.canvas = _WishlistCanvas(self)
        self.canvas.setStyleSheet(f"background: {COLORS['bg']};")
        self.scroll.setWidget(self.canvas)
        root.addWidget(self.scroll, 1)

        self.empty_label = QLabel(
            "Your wishlist is empty. Add games you want to get later.",
            parent=self.canvas)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setFont(QFont("DM Sans", 13))
        self.empty_label.setStyleSheet(
            f"color: {COLORS['text_muted']}; background: transparent;")
        self.empty_label.hide()

    # ── Visibility-driven polling ─────────────────────────────────────────────

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()
        self._poll_timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._poll_timer.stop()

    def refresh(self):
        """Public entry point — called by MainWindow whenever this page
        becomes the active stack widget (mirrors load_games()/load_game()/
        load_settings() elsewhere), and by showEvent() above."""
        self._last_mtime = wishlist_store.get_mtime()
        self._rebuild()

    def _poll_for_changes(self):
        mtime = wishlist_store.get_mtime()
        if mtime != self._last_mtime:
            self._last_mtime = mtime
            self._rebuild()

    # ── Rebuild ────────────────────────────────────────────────────────────

    def _rebuild(self):
        status = wishlist_store.location_status()
        if status != "ok":
            self._clear_tiles()
            self.scroll.hide()
            self.add_btn.setEnabled(False)
            if status == "unconfigured":
                self.status_label.setText(
                    "Set a Wishlist location in Settings → Paths to get started.")
            else:
                self.status_label.setText(
                    "Wishlist location not reachable:\n"
                    f"{wishlist_store.get_wishlist_path()}\n\n"
                    "Check that the shared folder is mounted, or update the "
                    "path in Settings → Paths.")
            self.status_label.show()
            return

        self.status_label.hide()
        self.scroll.show()
        self.add_btn.setEnabled(True)

        self._items = wishlist_store.get_items()
        self._clear_tiles()

        avail_w = max(1, self.scroll.viewport().width()
                      - self.MARGIN_LEFT - self.MARGIN_RIGHT)
        self._cols   = max(1, (avail_w + self.COL_SPACING)
                           // (self.TILE_W + self.COL_SPACING))
        cover_h      = int(self.TILE_W * 1.5)
        self._tile_h = cover_h + 44
        self._row_h  = self._tile_h + self.ROW_SPACING

        if not self._items:
            self.canvas.setFixedSize(self.scroll.viewport().size())
            self.empty_label.setGeometry(0, 0, self.canvas.width(), self.canvas.height())
            self.empty_label.show()
            return
        self.empty_label.hide()

        total_rows = -(-len(self._items) // self._cols)   # ceil div
        canvas_w = self.scroll.viewport().width()
        canvas_h = (self.MARGIN_TOP
                    + total_rows * self._tile_h
                    + max(0, total_rows - 1) * self.ROW_SPACING
                    + self.MARGIN_BOTTOM)
        self.canvas.setFixedSize(canvas_w, canvas_h)

        for idx, item in enumerate(self._items):
            tile = WishlistTile(item, self.TILE_W, parent=self.canvas)
            tile.clicked.connect(self._on_tile_clicked)
            tile.edit_requested.connect(self._on_tile_clicked)
            tile.remove_requested.connect(self._on_remove_requested)
            x, y = self._tile_pos(idx)
            tile.setGeometry(x, y, self.TILE_W, self._tile_h)
            tile.show()
            self._tiles[item["id"]] = tile

            cover = item.get("cover_url") or ""
            cached = self._cover_cache.get(item["id"])
            if cached is not None:
                tile.set_cover_pixmap(cached)
            elif cover:
                loader = _ThumbLoader(item["id"], cover)
                loader.signals.loaded.connect(self._on_cover_loaded)
                self._pool.start(loader)

    def _tile_pos(self, index: int) -> tuple[int, int]:
        row = index // self._cols
        col = index % self._cols
        x = self.MARGIN_LEFT + col * (self.TILE_W + self.COL_SPACING)
        y = self.MARGIN_TOP + row * self._row_h
        return x, y

    def _clear_tiles(self):
        for tile in self._tiles.values():
            tile.deleteLater()
        self._tiles.clear()

    def _on_cover_loaded(self, item_id: str, local_path: str):
        pix = QPixmap(local_path)
        if pix.isNull():
            return
        th = int(self.TILE_W * 1.5)
        scaled = pix.scaled(
            self.TILE_W, th,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation)
        x = max(0, (scaled.width() - self.TILE_W) // 2)
        y = max(0, (scaled.height() - th) // 2)
        cropped = scaled.copy(x, y, self.TILE_W, th)
        self._cover_cache[item_id] = cropped
        tile = self._tiles.get(item_id)
        if tile is not None:
            tile.set_cover_pixmap(cropped)

    # ── Add / Edit / Remove ────────────────────────────────────────────────

    def _on_add_clicked(self):
        dlg = WishlistItemDialog(item=None, parent=self)
        dlg.saved.connect(self._on_item_saved)
        dlg.exec()

    def _on_tile_clicked(self, item_id: str):
        item = wishlist_store.get_item(item_id)
        if not item:
            return
        dlg = WishlistItemDialog(item=item, parent=self)
        dlg.saved.connect(self._on_item_saved)
        dlg.exec()

    def _on_item_saved(self, item_id: str):
        self._cover_cache.pop(item_id, None)   # cover may have changed
        self._last_mtime = wishlist_store.get_mtime()
        self._rebuild()
        self.changed.emit()

    def _on_remove_requested(self, item_id: str):
        item  = wishlist_store.get_item(item_id)
        title = item.get("title") if item else "this item"
        reply = QMessageBox.question(
            self, "Remove from Wishlist",
            f"Remove '{title}' from your wishlist?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if reply != QMessageBox.StandardButton.Yes:
            return
        wishlist_store.remove_item(item_id)
        self._cover_cache.pop(item_id, None)
        self._last_mtime = wishlist_store.get_mtime()
        self._rebuild()
        self.changed.emit()

    # ── Drag-and-drop reorder ──────────────────────────────────────────────
    # Whole-grid reorder, always active (no Lock-B-style gate) — priority
    # order is the only order this view has, per spec. Mirrors
    # library_view.py's collection-reorder drop math almost exactly.

    def _drop_index_at(self, pos) -> int:
        if self._cols <= 0 or self._row_h <= 0 or not self._items:
            return 0
        x, y = pos.x(), pos.y()

        total_rows = -(-len(self._items) // self._cols)
        row = int((y - self.MARGIN_TOP) // self._row_h)
        row = max(0, min(row, max(0, total_rows - 1)))

        col_w = self.TILE_W + self.COL_SPACING
        rel_x = x - self.MARGIN_LEFT
        col_f = (rel_x / col_w) if col_w else 0.0
        col = int(col_f)
        frac = col_f - col
        insert_col = col + (1 if frac > 0.5 else 0)
        insert_col = max(0, min(insert_col, self._cols))

        index = row * self._cols + insert_col
        return max(0, min(index, len(self._items)))

    def _on_canvas_drag_enter(self, event):
        if not event.mimeData().hasFormat(_WISHLIST_DRAG_MIME):
            event.ignore()
            return
        event.acceptProposedAction()

    def _on_canvas_drag_move(self, event):
        if not event.mimeData().hasFormat(_WISHLIST_DRAG_MIME):
            event.ignore()
            self._drag_insert_index = None
            return
        event.acceptProposedAction()
        self._drag_insert_index = self._drop_index_at(event.position().toPoint())
        self.canvas.update()

    def _on_canvas_drag_leave(self, event):
        self._drag_insert_index = None
        self.canvas.update()

    def _on_canvas_drop(self, event):
        if not event.mimeData().hasFormat(_WISHLIST_DRAG_MIME):
            event.ignore()
            self._drag_insert_index = None
            self.canvas.update()
            return

        try:
            dragged_id = bytes(event.mimeData().data(_WISHLIST_DRAG_MIME)).decode("utf-8")
        except UnicodeDecodeError:
            event.ignore()
            self._drag_insert_index = None
            self.canvas.update()
            return

        current_order = [i["id"] for i in self._items]
        if dragged_id not in current_order:
            event.ignore()
            self._drag_insert_index = None
            self.canvas.update()
            return

        target_index = self._drop_index_at(event.position().toPoint())
        source_index = current_order.index(dragged_id)
        current_order.remove(dragged_id)
        # target_index was computed against the list WITH the dragged item
        # still in it — removing it shifts every later index down by one,
        # so a forward drag needs the same shift applied here (see
        # library_view.py's _on_canvas_drop() for the identical fix).
        if target_index > source_index:
            target_index -= 1
        target_index = max(0, min(target_index, len(current_order)))
        current_order.insert(target_index, dragged_id)

        wishlist_store.reorder_items(current_order)

        event.acceptProposedAction()
        self._drag_insert_index = None
        self._last_mtime = wishlist_store.get_mtime()
        self._rebuild()
        self.changed.emit()

    def _paint_drag_indicator(self, canvas_widget):
        if self._drag_insert_index is None or self._cols <= 0:
            return
        idx = self._drag_insert_index
        row = idx // self._cols
        col = idx % self._cols
        x = (self.MARGIN_LEFT + col * (self.TILE_W + self.COL_SPACING)
             - self.COL_SPACING // 2 - 1)
        y = self.MARGIN_TOP + row * self._row_h

        painter = QPainter(canvas_widget)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(COLORS["accent"]))
        pen.setWidth(3)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawLine(x, y, x, y + self._tile_h)
        painter.end()

    # ── Resize ────────────────────────────────────────────────────────────

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._resize_timer.start()
