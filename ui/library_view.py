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
from dataclasses import dataclass, field
from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QScrollArea,
    QLabel, QPushButton, QLineEdit, QFrame, QSizePolicy
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QRunnable, QThreadPool, pyqtSlot, QObject, QEvent
from PyQt6.QtGui import QPixmap, QFont

import db
import metadata as meta_mod
from ui.style import COLORS

log = logging.getLogger(__name__)

TILE_WIDTHS = {"small": 130, "medium": 160, "large": 200}


# ── Filter state ──────────────────────────────────────────────────────────────

@dataclass
class FilterState:
    """
    Composable filter applied to the library tile grid.

    Groups are AND-ed: a game must pass every active group to be shown.

    Group A — sidebar quick-filter (mutually exclusive):
        "all"         show all non-hidden games (default)
        "installed"   only installed games
        "uninstalled" only not-installed games
        "favorites"   only favorited games
        "recent"      recently added/scanned games (window controlled by
                      "recently_added_days" setting, default 14 days)

    Group B — category (folder name from NAS, e.g. "PC", "Switch"):
        None = no category filter

    Group D — completion status:
        None = no completion filter
        One of: "unplayed", "in_progress", "completed", "abandoned"

    Search — substring match against title (case-insensitive):
        "" = no search filter

    Hidden games:
        Never shown unless sidebar_key == "hidden".
        Even "all" excludes hidden games.
    """
    sidebar_key:  str            = "all"
    category:     Optional[str]  = None
    completion:   Optional[str]  = None
    search:       str            = ""

    def page_title(self) -> str:
        """Return the header title appropriate for the current filter state."""
        titles = {
            "all":         "All Games",
            "installed":   "Installed",
            "uninstalled": "Not Installed",
            "favorites":   "Favorites",
            "recent":      "Recently Added",
            "hidden":      "Hidden Games",
        }
        base = titles.get(self.sidebar_key, self.sidebar_key)
        if self.category:
            # Category view — title is set by the caller from the DB display name
            return base
        return base

    def with_sidebar(self, key: str) -> "FilterState":
        """Return a copy with sidebar_key changed."""
        return FilterState(
            sidebar_key=key,
            category=self.category,
            completion=self.completion,
            search=self.search,
        )

    def with_category(self, cat: Optional[str]) -> "FilterState":
        """Return a copy with category changed."""
        return FilterState(
            sidebar_key=self.sidebar_key,
            category=cat,
            completion=self.completion,
            search=self.search,
        )

    def with_completion(self, status: Optional[str]) -> "FilterState":
        """Return a copy with completion status changed."""
        return FilterState(
            sidebar_key=self.sidebar_key,
            category=self.category,
            completion=status,
            search=self.search,
        )

    def with_search(self, text: str) -> "FilterState":
        """Return a copy with search text changed."""
        return FilterState(
            sidebar_key=self.sidebar_key,
            category=self.category,
            completion=self.completion,
            search=text,
        )


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
    clicked        = pyqtSignal(int)
    state_changed  = pyqtSignal(int)   # game_id — emitted after context menu action
    track_versions = pyqtSignal(int)   # game_id — open VersionTrackerDialog

    def __init__(self, game, tile_width: int = 160, parent=None):
        super().__init__(parent)
        self.game_id     = game["id"]
        self.tile_width  = tile_width
        self._is_favorite = bool(_safe_get(game, "is_favorite", 0))
        self._is_hidden   = bool(_safe_get(game, "is_hidden",   0))

        cover_h = int(tile_width * 1.5)
        tile_h  = cover_h + 32   # cover + footer (8px top + ~14px label + 10px bottom)

        self.setFixedSize(tile_width, tile_h)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
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

        # ── Badge overlays ────────────────────────────────────────────────────
        # Installed badge — top-right
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

        # Favorite badge — top-left gold star
        if self._is_favorite:
            fav_badge = QLabel("★")
            fav_badge.setParent(self)
            fav_badge.setFont(QFont("DM Sans", 11))
            fav_badge.setStyleSheet(f"""
                QLabel {{
                    background: rgba(232,199,106,0.18);
                    border: 1px solid rgba(232,199,106,0.45);
                    border-radius: 4px;
                    color: {COLORS['accent']};
                    padding: 1px 5px;
                }}
            """)
            fav_badge.adjustSize()
            fav_badge.move(8, 8)
            fav_badge.show()

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

    def contextMenuEvent(self, event):
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{
                background: {COLORS['surface2']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
                padding: 4px 0;
                color: {COLORS['text']};
            }}
            QMenu::item {{
                padding: 7px 20px 7px 14px;
                font-size: 12px;
            }}
            QMenu::item:selected {{
                background: {COLORS['surface3']};
                border-radius: 4px;
            }}
            QMenu::separator {{
                height: 1px;
                background: {COLORS['border']};
                margin: 3px 8px;
            }}
        """)

        # Favorite action
        if self._is_favorite:
            fav_action = menu.addAction("★  Remove from Favorites")
        else:
            fav_action = menu.addAction("☆  Add to Favorites")

        # Hide action
        menu.addSeparator()
        if self._is_hidden:
            hide_action = menu.addAction("👁  Unhide Game")
        else:
            hide_action = menu.addAction("⊘  Hide Game")

        # Version tracking
        menu.addSeparator()
        track_action = menu.addAction("🔔  Track Version Updates…")

        chosen = menu.exec(event.globalPos())
        if chosen == fav_action:
            db.set_favorite(self.game_id, not self._is_favorite)
            self.state_changed.emit(self.game_id)
        elif chosen == hide_action:
            db.set_hidden(self.game_id, not self._is_hidden)
            self.state_changed.emit(self.game_id)
        elif chosen == track_action:
            self.track_versions.emit(self.game_id)


# ── Trickle viewport event filter ────────────────────────────────────────────

class TrickleViewportFilter(QObject):
    """
    Installed on QScrollArea.viewport() during trickle rendering.

    The shift-jitter during trickle is caused by QScrollArea::viewportEvent()
    processing MouseMove/Enter/Leave events, which internally calls
    updateScrollBars() → scrollBar->setRange() → a geometry recalculation that
    repositions the content widget.  When new rows are being added every 8ms the
    content height is changing constantly, so each mouse move can flip the
    scrollbar visibility, changing the viewport width, which can in turn change
    the column count and restart the entire trickle.

    Fix: silently consume MouseMove, Enter, and Leave events on the viewport
    while trickle is active.  The scroll area still repaints (we don't block
    Paint), the user can still scroll with the wheel (we don't block Wheel),
    and clicks still work (we don't block MouseButton).  We only stop Qt from
    recalculating scrollbar geometry in response to hover position.

    The filter is installed once at construction and permanently attached to
    the viewport; _active is toggled by _start_trickle / trickle completion.
    """

    _BLOCKED = {
        QEvent.Type.MouseMove,
        QEvent.Type.Enter,
        QEvent.Type.Leave,
        QEvent.Type.HoverEnter,
        QEvent.Type.HoverMove,
        QEvent.Type.HoverLeave,
    }

    def __init__(self, parent: QObject = None):
        super().__init__(parent)
        self._active = False

    def set_active(self, active: bool):
        self._active = active

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if self._active and event.type() in self._BLOCKED:
            return True   # consume — don't propagate to QScrollArea internals
        return False      # pass through


# ── Library View ──────────────────────────────────────────────────────────────

class LibraryView(QWidget):
    game_selected          = pyqtSignal(int)
    refresh_requested      = pyqtSignal(str)
    trickle_finished       = pyqtSignal()
    game_state_changed     = pyqtSignal(int)   # game_id — emitted after favorite/hide from tile
    track_versions_requested = pyqtSignal(int) # game_id — open VersionTrackerDialog

    def __init__(self, parent=None):
        super().__init__(parent)
        self._all_games    : list = []
        self._filter_state : FilterState = FilterState()
        self._tiles        : dict[int, GameTile] = {}

        # Generation counter — incremented on each new load to cancel stale image loads
        self._trickle_queue  : list = []
        self._trickle_gen    : int  = 0
        self._trickle_active : bool = False

        # Current row state (used during synchronous grid build)
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
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.scroll.setStyleSheet(f"background: {COLORS['bg']}; border: none;")

        # Event filter that blocks mouse-move events during trickle.
        # Installed on viewport(), _outer, and _rows_widget — mouse events
        # land on the tile widgets directly, not on the viewport, so the
        # filter must be on the content widget tree as well.
        self._viewport_filter = TrickleViewportFilter(self)
        self.scroll.viewport().installEventFilter(self._viewport_filter)

        # Outer container holds rows widget + empty label
        self._outer = QWidget()
        self._outer.setStyleSheet(f"background: {COLORS['bg']};")
        outer_layout = QVBoxLayout(self._outer)
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
        # No addStretch() — a stretch spacer rebalances against _rows_widget on
        # every addWidget() during trickle, physically shifting rows upward each
        # time a new row is appended. AlignTop already pins content to the top.

        # Mouse events land on tile widgets which are children of _outer and
        # _rows_widget, not on the viewport itself — so install the filter here
        # too, not just on scroll.viewport().
        self._outer.installEventFilter(self._viewport_filter)
        self._rows_widget.installEventFilter(self._viewport_filter)

        self.scroll.setWidget(self._outer)
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
        """
        Set the Group A sidebar key.

        Accepts the same string keys as before ("all", "installed",
        "uninstalled", "cat:<folder>") plus new Group A keys ("favorites",
        "recent", "hidden"). Category keys ("cat:<folder>") also set
        filter_state.category so the two filters stay in sync.
        """
        if key.startswith("cat:"):
            folder = key[4:]
            self._filter_state = FilterState(
                sidebar_key="all",
                category=folder,
                completion=self._filter_state.completion,
                search=self._filter_state.search,
            )
        else:
            self._filter_state = FilterState(
                sidebar_key=key,
                category=None,
                completion=self._filter_state.completion,
                search=self._filter_state.search,
            )
        self._update_page_title()
        self._start_trickle(self._filtered_games())

    def set_filter_state(self, state: FilterState):
        """
        Replace the entire filter state at once.
        Used by future cogwheel menu / tag filter / completion filter UI.
        """
        self._filter_state = state
        self._update_page_title()
        self._start_trickle(self._filtered_games())

    def get_filter_state(self) -> FilterState:
        """Return the current filter state (read-only copy)."""
        return FilterState(
            sidebar_key=self._filter_state.sidebar_key,
            category=self._filter_state.category,
            completion=self._filter_state.completion,
            search=self._filter_state.search,
        )

    def set_page_title(self, title: str):
        self.page_title.setText(title)

    def _update_page_title(self):
        """Sync the header title with current filter state."""
        fs = self._filter_state
        if fs.category:
            # Caller is responsible for setting the display name via
            # set_page_title() after resolving from DB, as before.
            return
        titles = {
            "all":         "All Games",
            "installed":   "Installed",
            "uninstalled": "Not Installed",
            "favorites":   "Favorites",
            "recent":      "Recently Added",
            "hidden":      "Hidden Games",
        }
        if fs.sidebar_key in titles:
            self.page_title.setText(titles[fs.sidebar_key])

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
        """
        Apply FilterState to self._all_games and return the matching subset.

        Group A (sidebar_key) and Group B (category) are AND-ed.
        Group D (completion) is AND-ed on top of those.
        Search is AND-ed last.

        Hidden games are excluded from every view except sidebar_key == "hidden".
        """
        fs    = self._filter_state
        games = self._all_games

        # ── Hidden gate ───────────────────────────────────────────────────────
        # Must come first: "hidden" view shows ONLY hidden; everything else
        # excludes hidden entirely.
        if fs.sidebar_key == "hidden":
            games = [g for g in games if _safe_get(g, "is_hidden", 0)]
        else:
            games = [g for g in games if not _safe_get(g, "is_hidden", 0)]

        # ── Group A — sidebar quick-filter ────────────────────────────────────
        if fs.sidebar_key == "installed":
            games = [g for g in games if g["is_installed"]]
        elif fs.sidebar_key == "uninstalled":
            games = [g for g in games if not g["is_installed"]]
        elif fs.sidebar_key == "favorites":
            games = [g for g in games if _safe_get(g, "is_favorite", 0)]
        elif fs.sidebar_key == "recent":
            games = _filter_recent(games)
        # "all" and "hidden" require no additional pass here

        # ── Group B — category ────────────────────────────────────────────────
        if fs.category:
            games = [g for g in games if _safe_get(g, "category") == fs.category]

        # ── Group D — completion status ───────────────────────────────────────
        if fs.completion:
            games = [g for g in games
                     if _safe_get(g, "completion_status", "unplayed") == fs.completion]

        # ── Search ────────────────────────────────────────────────────────────
        if fs.search:
            q     = fs.search.lower()
            games = [g for g in games
                     if q in (g["title"] or g["display_name"] or "").lower()]

        return games

    def _on_search(self, text: str):
        self._filter_state = self._filter_state.with_search(text)
        self._start_trickle(self._filtered_games())

    # ── Trickle rendering ─────────────────────────────────────────────────────

    def _start_trickle(self, games: list):
        # Cancel any running trickle
        self._trickle_gen += 1
        self._trickle_active = False
        self._viewport_filter.set_active(False)

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
        avail_w   = self.scroll.viewport().width()
        cols      = max(1, avail_w // (tile_w + 16))
        self._cols      = cols
        self._last_cols = cols

        self._trickle_active = True
        self._viewport_filter.set_active(True)
        my_gen = self._trickle_gen

        def add_next():
            if self._trickle_gen != my_gen:
                self._viewport_filter.set_active(False)
                return

            tiles_placed = 0
            while tiles_placed < 5:
                if not self._trickle_queue:
                    if self._current_row_widget is not None:
                        self._current_row_layout.addStretch()
                        self._rows_layout.addWidget(self._current_row_widget)
                        self._current_row_widget = None
                        self._current_row_layout = None
                        self._tiles_in_row       = 0
                    self._trickle_active = False
                    self._viewport_filter.set_active(False)
                    self._unlock_refresh()
                    self.trickle_finished.emit()
                    return

                game   = self._trickle_queue.pop(0)
                tw     = TILE_WIDTHS.get(db.get_setting("tile_size", "medium"), 160)

                if self._current_row_layout is None:
                    row_widget = QWidget()
                    row_widget.setStyleSheet("background: transparent;")
                    row_layout = QHBoxLayout(row_widget)
                    row_layout.setContentsMargins(0, 0, 0, 0)
                    row_layout.setSpacing(16)
                    row_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
                    self._current_row_widget = row_widget
                    self._current_row_layout = row_layout
                    self._tiles_in_row       = 0

                tile = GameTile(game, tw)
                tile.clicked.connect(self.game_selected)
                tile.state_changed.connect(self.game_state_changed)
                tile.track_versions.connect(self.track_versions_requested)
                self._current_row_layout.addWidget(tile)
                self._tiles[game["id"]] = tile
                self._tiles_in_row += 1
                tiles_placed += 1

                cover_url = _safe_get(game, "cover_url")
                if cover_url:
                    loader = ImageLoader(game["id"], cover_url)
                    loader.signals.loaded.connect(self._on_image_loaded)
                    self._pool.start(loader)

                if self._tiles_in_row >= self._cols:
                    self._current_row_layout.addStretch()
                    self._rows_layout.addWidget(self._current_row_widget)
                    self._current_row_widget = None
                    self._current_row_layout = None
                    self._tiles_in_row       = 0

            QTimer.singleShot(0, add_next)

        self._trickle_queue = list(games)
        QTimer.singleShot(0, add_next)

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
            log.debug("resizeEvent: new size=%s trickle_active=%s",
                      event.size(), self._trickle_active)
            self._resize_timer.start()

    def _on_resize_settled(self):
        if not self._all_games:
            return
        tile_size = db.get_setting("tile_size", "medium")
        tile_w    = TILE_WIDTHS.get(tile_size, 160)
        avail_w   = self.scroll.viewport().width()
        new_cols  = max(1, avail_w // (tile_w + 16))
        log.debug("_on_resize_settled: avail_w=%d new_cols=%d last_cols=%d trickle_active=%s",
                  avail_w, new_cols, self._last_cols, self._trickle_active)
        if new_cols == self._last_cols:
            return
        width_delta = abs(avail_w - (self._last_cols * (tile_w + 16)))
        if self._trickle_active and width_delta < tile_w:
            return
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


def _filter_recent(games: list) -> list:
    """
    Return games first_seen within the configured recently-added window.
    Window is controlled by the "recently_added_days" setting (default 14).
    Falls back to returning all games if the setting is missing or dates
    can't be parsed, so the filter is always safe to call.
    """
    import datetime
    try:
        days = int(db.get_setting("recently_added_days", "14"))
    except (ValueError, TypeError):
        days = 14
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    result = []
    for g in games:
        first_seen = _safe_get(g, "first_seen")
        if not first_seen:
            continue
        try:
            # SQLite stores datetimes as "YYYY-MM-DD HH:MM:SS"
            dt = datetime.datetime.fromisoformat(str(first_seen).replace("T", " ")[:19])
            if dt >= cutoff:
                result.append(g)
        except (ValueError, TypeError):
            pass
    return result
