"""
ui/library_view.py — Game library tile grid for VaultPlay

Rendering approach:
  - Tiles trickle in one per event-loop tick via chained QTimer.singleShot(0)
  - Row-based layout (QVBoxLayout of QHBoxLayout rows) so rows append at the
    bottom without shifting anything above
  - Hover effect uses CSS :hover pseudo-selector only — no enterEvent/leaveEvent,
    no runtime setStyleSheet() calls, which was the cause of vertical jitter
  - Resize debounced at 200ms; reflows only if column count changed
  - Refresh button disabled while trickle or scan is in progress
  - Status bar has fixed height so it never expands and shifts the grid
  - Scroll area always visible so header stays in position even when empty
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

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QScrollArea,
    QLabel, QPushButton, QLineEdit, QFrame, QSizePolicy
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QRunnable, QThreadPool, pyqtSlot, QObject
from PyQt6.QtGui import QPixmap, QFont

import db
import metadata as meta_mod
from ui.style import COLORS

log = logging.getLogger(__name__)

TILE_WIDTHS = {"small": 130, "medium": 160, "large": 200}


# ── Async image loader ────────────────────────────────────────────────────────

class ImageSignals(QObject):
    loaded = pyqtSignal(int, str)   # game_id, local_path


class ImageLoader(QRunnable):
    def __init__(self, game_id: int, url: str):
        super().__init__()
        self.game_id = game_id
        self.url     = url
        self.signals = ImageSignals()
        self.setAutoDelete(True)

    @pyqtSlot()
    def run(self):
        try:
            path = meta_mod.download_art(self.url)
            if path:
                self.signals.loaded.emit(self.game_id, path)
        except Exception as e:
            log.debug("ImageLoader failed for %s: %s", self.url, e)


# ── Game tile ─────────────────────────────────────────────────────────────────

class GameTile(QFrame):
    clicked = pyqtSignal(int)

    def __init__(self, game, tile_width: int = 160, parent=None):
        super().__init__(parent)
        self.game_id    = game["id"]
        self.tile_width = tile_width
        cover_h         = int(tile_width * 1.5)

        self.setFixedWidth(tile_width)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        # Hover handled entirely by CSS :hover — no enterEvent/leaveEvent,
        # no runtime setStyleSheet() calls, which caused layout jitter.
        self.setStyleSheet(f"""
            QFrame {{
                background: {COLORS['surface']};
                border: 1px solid {COLORS['border']};
                border-radius: 10px;
            }}
            QFrame:hover {{
                border-color: rgba(255,255,255,0.18);
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Cover — flat placeholder, no QPainter, no hashlib
        self.cover_label = QLabel()
        self.cover_label.setFixedSize(tile_width, cover_h)
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_label.setStyleSheet(
            f"background: {COLORS['surface2']}; border: none;"
        )
        layout.addWidget(self.cover_label)

        # Footer
        footer = QWidget()
        footer.setStyleSheet(f"background: {COLORS['surface']}; border: none;")
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(10, 8, 10, 10)
        footer_layout.setSpacing(0)

        title = game["title"] or game["display_name"] or game["folder_name"]
        title_label = QLabel()
        title_label.setFont(QFont("DM Sans", 10, QFont.Weight.Medium))
        title_label.setStyleSheet(
            f"color: {COLORS['text']}; background: transparent;"
        )
        title_label.setFixedWidth(tile_width - 20)
        elided = title_label.fontMetrics().elidedText(
            title, Qt.TextElideMode.ElideRight, tile_width - 20
        )
        title_label.setText(elided)
        title_label.setToolTip(title)
        footer_layout.addWidget(title_label)
        layout.addWidget(footer)

        # Installed badge overlay
        if game["is_installed"]:
            badge = QLabel("Installed")
            badge.setParent(self)
            badge.setFont(QFont("DM Mono", 8))
            badge.setStyleSheet(f"""
                QLabel {{
                    background: rgba(74,222,128,0.15);
                    border: 1px solid rgba(74,222,128,0.4);
                    border-radius: 4px;
                    color: {COLORS['installed']};
                    padding: 2px 6px;
                }}
            """)
            badge.adjustSize()
            badge.move(tile_width - badge.width() - 8, 8)
            badge.show()

    def set_cover_image(self, local_path: str):
        pix = QPixmap(local_path)
        if pix.isNull():
            return
        tw = self.tile_width
        th = int(tw * 1.5)
        scaled = pix.scaled(
            tw, th,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation
        )
        x = (scaled.width()  - tw) // 2
        y = (scaled.height() - th) // 2
        self.cover_label.setPixmap(scaled.copy(x, y, tw, th))
        self.cover_label.setStyleSheet("background: transparent; border: none;")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.game_id)


# ── Library View ──────────────────────────────────────────────────────────────

class LibraryView(QWidget):
    game_selected     = pyqtSignal(int)
    refresh_requested = pyqtSignal(str)
    trickle_finished  = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._all_games    : list = []
        self._filter_key   : str  = "all"
        self._search_text  : str  = ""
        self._tiles        : dict[int, GameTile] = {}

        # Trickle state
        self._trickle_queue   : list = []
        self._trickle_active  : bool = False
        self._trickle_gen     : int  = 0

        # Current row being filled
        self._current_row_widget : QWidget | None     = None
        self._current_row_layout : QHBoxLayout | None = None
        self._tiles_in_row       : int = 0
        self._cols               : int = 1

        self._last_cols    : int  = 0
        self._scan_running : bool = False

        self._pool = QThreadPool.globalInstance()
        self._pool.setMaxThreadCount(4)

        self._resize_timer = QTimer()
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(200)
        self._resize_timer.timeout.connect(self._on_resize_settled)

        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Header ────────────────────────────────────────────────────────────
        header = QWidget()
        header.setStyleSheet(
            f"background: {COLORS['bg']};"
            f"border-bottom: 1px solid {COLORS['border']};"
        )
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(28, 16, 28, 16)
        header_layout.setSpacing(10)

        self.page_title = QLabel("All Games")
        self.page_title.setFont(QFont("Rajdhani", 18, QFont.Weight.DemiBold))
        header_layout.addWidget(self.page_title)
        header_layout.addStretch()

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search games…")
        self.search_box.setFixedWidth(220)
        self.search_box.textChanged.connect(self._on_search)
        header_layout.addWidget(self.search_box)

        self.refresh_btn = QPushButton("↻")
        self.refresh_btn.setFixedSize(36, 36)
        self.refresh_btn.setToolTip("Refresh library from NAS")
        self.refresh_btn.setFont(QFont("DM Sans", 14))
        self._set_refresh_btn_style(active=False)
        self.refresh_btn.clicked.connect(self._on_refresh)
        header_layout.addWidget(self.refresh_btn)
        root.addWidget(header)

        # ── Status bar — fixed height so it never expands ─────────────────────
        self.status_bar = QLabel("")
        self.status_bar.setFont(QFont("DM Mono", 9))
        self.status_bar.setFixedHeight(28)
        self.status_bar.setStyleSheet(f"""
            background: {COLORS['surface2']};
            color: {COLORS['text_muted']};
            padding: 0px 28px;
            border-bottom: 1px solid {COLORS['border']};
        """)
        self.status_bar.hide()
        root.addWidget(self.status_bar)

        # ── Scroll area — always visible so header never shifts ───────────────
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet(f"background: {COLORS['bg']}; border: none;")

        # Outer container holds rows widget + empty label
        outer = QWidget()
        outer.setStyleSheet(f"background: {COLORS['bg']};")
        outer_layout = QVBoxLayout(outer)
        outer_layout.setContentsMargins(28, 24, 28, 28)
        outer_layout.setSpacing(0)
        outer_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        # Empty state — sits at the top of the outer container
        self.empty_label = QLabel(
            "No games found.\n"
            "Configure your NAS path in Settings to get started."
        )
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setFont(QFont("DM Sans", 13))
        self.empty_label.setStyleSheet(
            f"color: {COLORS['text_muted']}; padding: 60px;"
        )
        self.empty_label.hide()
        outer_layout.addWidget(self.empty_label)

        # Rows widget — QVBoxLayout of QHBoxLayout rows
        self._rows_widget = QWidget()
        self._rows_widget.setStyleSheet("background: transparent;")
        self._rows_layout = QVBoxLayout(self._rows_widget)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(16)
        self._rows_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        outer_layout.addWidget(self._rows_widget)
        outer_layout.addStretch()

        self.scroll.setWidget(outer)
        root.addWidget(self.scroll, 1)

        self._status_timer = QTimer()
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(lambda: self.status_bar.hide())

        self._spin_chars = ["↻", "↺"]
        self._spin_idx   = 0
        self._spin_timer = QTimer()
        self._spin_timer.setInterval(300)
        self._spin_timer.timeout.connect(self._spin_refresh)

    # ── Public API ────────────────────────────────────────────────────────────

    def load_games(self, games: list):
        self._all_games = games
        self._start_trickle(self._filtered_games())

    def apply_filter(self, key: str):
        self._filter_key = key
        titles = {
            "all":         "All Games",
            "installed":   "Installed",
            "uninstalled": "Not Installed",
        }
        if key in titles:
            self.page_title.setText(titles[key])
        elif key.startswith("cat:"):
            self.page_title.setText(key[4:])
        self._start_trickle(self._filtered_games())

    def set_page_title(self, title: str):
        self.page_title.setText(title)

    def show_status(self, message: str, timeout: int = 0):
        self.status_bar.setText(message)
        self.status_bar.show()
        self._status_timer.stop()
        if timeout:
            self._status_timer.start(timeout)

    def set_scan_running(self, running: bool):
        self._scan_running = running
        if not running and not self._trickle_active:
            self._set_refresh_btn_style(active=False)
            self.refresh_btn.setEnabled(True)

    def stop_spin(self):
        self._spin_timer.stop()
        self.refresh_btn.setText("↻")

    # ── Filtering ─────────────────────────────────────────────────────────────

    def _filtered_games(self) -> list:
        games = self._all_games
        if self._filter_key == "installed":
            games = [g for g in games if g["is_installed"]]
        elif self._filter_key == "uninstalled":
            games = [g for g in games if not g["is_installed"]]
        elif self._filter_key.startswith("cat:"):
            folder = self._filter_key[4:]
            games  = [g for g in games if _safe_get(g, "category") == folder]
        if self._search_text:
            q     = self._search_text.lower()
            games = [g for g in games
                     if q in (g["title"] or g["display_name"] or "").lower()]
        return games

    def _on_search(self, text: str):
        self._search_text = text
        self._start_trickle(self._filtered_games())

    # ── Trickle rendering ─────────────────────────────────────────────────────

    def _start_trickle(self, games: list):
        # Cancel any running trickle
        self._trickle_gen += 1
        self._trickle_active     = False

        # Clear all rows
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._tiles.clear()
        self._current_row_widget = None
        self._current_row_layout = None
        self._tiles_in_row       = 0

        if not games:
            self.empty_label.show()
            self._rows_widget.hide()
            self.trickle_finished.emit()
            self._unlock_refresh()
            return

        self.empty_label.hide()
        self._rows_widget.show()

        self.refresh_btn.setEnabled(False)
        self._set_refresh_btn_style(active=True)

        tile_size = db.get_setting("tile_size", "medium")
        tile_w    = TILE_WIDTHS.get(tile_size, 160)
        avail_w   = self.scroll.width() - 56
        cols      = max(1, avail_w // (tile_w + 16))
        self._cols      = cols
        self._last_cols = cols

        self._trickle_queue  = list(games)
        self._trickle_active = True
        my_gen = self._trickle_gen

        def add_next():
            if self._trickle_gen != my_gen:
                return

            for _ in range(5):
                if not self._trickle_queue:
                    if self._current_row_layout is not None:
                        self._current_row_layout.addStretch()
                    self._trickle_active     = False
                    self._current_row_widget = None
                    self._current_row_layout = None
                    self._tiles_in_row       = 0
                    self._unlock_refresh()
                    self.trickle_finished.emit()
                    return

                game    = self._trickle_queue.pop(0)
                tile_sz = db.get_setting("tile_size", "medium")
                tw      = TILE_WIDTHS.get(tile_sz, 160)

                if self._current_row_layout is None or self._tiles_in_row >= self._cols:
                    if self._current_row_layout is not None:
                        self._current_row_layout.addStretch()

                    row_widget = QWidget()
                    row_widget.setStyleSheet("background: transparent;")
                    row_layout = QHBoxLayout(row_widget)
                    row_layout.setContentsMargins(0, 0, 0, 0)
                    row_layout.setSpacing(16)
                    row_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
                    self._rows_layout.addWidget(row_widget)

                    self._current_row_widget = row_widget
                    self._current_row_layout = row_layout
                    self._tiles_in_row       = 0

                tile = GameTile(game, tw)
                tile.clicked.connect(self.game_selected)
                self._current_row_layout.addWidget(tile)
                self._tiles[game["id"]] = tile
                self._tiles_in_row += 1

                cover_url = _safe_get(game, "cover_url")
                if cover_url:
                    loader = ImageLoader(game["id"], cover_url)
                    loader.signals.loaded.connect(self._on_image_loaded)
                    self._pool.start(loader)

            QTimer.singleShot(8, add_next)

        QTimer.singleShot(8, add_next)

    def _on_image_loaded(self, game_id: int, local_path: str):
        tile = self._tiles.get(game_id)
        if tile:
            tile.set_cover_image(local_path)

    def refresh_tile(self, game_id: int):
        """
        Called after a single game's metadata is saved.
        If the tile is visible, fetches its cover art immediately.
        """
        tile = self._tiles.get(game_id)
        if not tile:
            return
        try:
            game = db.get_games_for_library()
            # Find this game's cover_url from a lightweight lookup
            import db as _db
            with _db.get_connection() as conn:
                row = conn.execute(
                    "SELECT cover_url FROM metadata WHERE game_id=?", (game_id,)
                ).fetchone()
            if row and row["cover_url"]:
                loader = ImageLoader(game_id, row["cover_url"])
                loader.signals.loaded.connect(self._on_image_loaded)
                self._pool.start(loader)
        except Exception as e:
            log.debug("refresh_tile failed for %d: %s", game_id, e)

    # ── Resize handling ───────────────────────────────────────────────────────

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._all_games:
            self._resize_timer.start()

    def _on_resize_settled(self):
        if not self._all_games:
            return
        tile_size = db.get_setting("tile_size", "medium")
        tile_w    = TILE_WIDTHS.get(tile_size, 160)
        avail_w   = self.scroll.width() - 56
        new_cols  = max(1, avail_w // (tile_w + 16))
        if new_cols != self._last_cols:
            self._start_trickle(self._filtered_games())

    # ── Refresh button ────────────────────────────────────────────────────────

    def _unlock_refresh(self):
        if not self._scan_running:
            self.refresh_btn.setEnabled(True)
            self._set_refresh_btn_style(active=False)

    def _set_refresh_btn_style(self, active: bool):
        if active:
            self.refresh_btn.setStyleSheet(f"""
                QPushButton {{
                    background: {COLORS['surface']};
                    border: 1px solid rgba(232,199,106,0.5);
                    border-radius: 8px;
                    color: {COLORS['accent']};
                    font-size: 16px;
                }}
            """)
        else:
            self.refresh_btn.setStyleSheet(f"""
                QPushButton {{
                    background: {COLORS['surface']};
                    border: 1px solid {COLORS['border']};
                    border-radius: 8px;
                    color: {COLORS['text_muted']};
                    font-size: 16px;
                }}
                QPushButton:hover {{
                    color: {COLORS['text']};
                    border-color: {COLORS['accent']};
                }}
                QPushButton:disabled {{
                    color: {COLORS['surface3']};
                    border-color: {COLORS['border']};
                }}
            """)

    def _on_refresh(self):
        nas_path = db.get_setting("nas_path", "")
        self._spin_timer.start()
        self._set_refresh_btn_style(active=True)
        self.refresh_requested.emit(nas_path)

    def _spin_refresh(self):
        self._spin_idx = (self._spin_idx + 1) % len(self._spin_chars)
        self.refresh_btn.setText(self._spin_chars[self._spin_idx])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_get(row, key, default=None):
    try:
        return row[key]
    except (IndexError, KeyError):
        return default
