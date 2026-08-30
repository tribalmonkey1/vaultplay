"""
ui/library_view.py — Game library tile grid for VaultPlay

Rendering approach: VIRTUALIZED grid (replaces the old trickle-load approach).

  - Tiles are NOT managed by a QLayout. QGridLayout has no virtualization
    support, and a layout-per-tile approach doesn't scale — every tile has
    to exist as a widget the whole time the library is loaded.
  - Instead, a single fixed-size "canvas" QWidget sits inside the
    QScrollArea. Its size is computed once from the full filtered game
    count (rows * tile height), and every tile is a plain child widget of
    the canvas, positioned directly with setGeometry().
  - Only tiles that fall within the current viewport (plus a small buffer
    of rows above/below, for smooth scrolling) are ever instantiated.
    Scrolling diffs the "wanted" index range against the currently-live
    tile pool: tiles that scrolled out get deleted, tiles that scrolled in
    get created.
  - Because the canvas size never changes while tiles are created/destroyed
    (only on filter change, resize, or tile-size change), there is nothing
    for QScrollArea to recalculate mid-scroll — this is the property that
    made virtualization worth trying as an alternative to the old
    trickle-based renderer.
  - Cover art still loads asynchronously per visible tile, same as before.
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
import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QScrollArea, QStackedWidget,
    QLabel, QPushButton, QLineEdit, QFrame, QSizePolicy, QComboBox,
    QMessageBox
)
from PyQt6.QtCore import (
    Qt, pyqtSignal, QTimer, QRunnable, QThreadPool, pyqtSlot, QObject,
    QPointF, QRectF, QPoint, QMimeData
)
from PyQt6.QtGui import (
    QPixmap, QFont, QColor, QPainter, QPen, QBrush, QPolygonF, QDrag,
    QShortcut, QKeySequence
)

import db
import metadata as meta_mod
from ui.style import COLORS

log = logging.getLogger(__name__)

# Collections / Playlists — drag-and-drop in-collection reordering.
# Custom MIME type carrying the dragged game's id as UTF-8 bytes; shared
# between GameTile (drag source) and LibraryView's canvas (drop target).
_GAME_DRAG_MIME = "application/x-vaultplay-game-id"

TILE_WIDTHS = {"small": 130, "medium": 160, "large": 200}


class NoScrollComboBox(QComboBox):
    """QComboBox that ignores scroll wheel events — same pattern used
    throughout the codebase (install_dialog.py, cogwheel_menu.py), so
    scrolling the page while the cursor happens to be over the header
    filter never silently changes it."""
    def wheelEvent(self, event):
        event.ignore()


class _CollectionTitleLabel(QLabel):
    """
    Plain QLabel has no click signal — this is the library header's page
    title (see LibraryView._build_ui()), which becomes clickable-to-rename
    only while a collection is actively being viewed (see
    LibraryView._on_title_clicked() / _refresh_collection_header()). Emits
    `clicked` unconditionally on every left-click; the handler itself is
    what no-ops when there's no active collection to rename, so this class
    stays a completely generic clickable label.
    """
    clicked = pyqtSignal()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


# ── Spinning scan/refresh button (Notifications feature) ─────────────────────
# Replaces the old text-cycling "↻"/"↺" refresh button. The U+21BB glyph does
# not render at all under this app's forced "DM Sans" font (confirmed in
# production), so the icon is hand-painted instead — a 270° arc with a small
# arrowhead, matching the hand-drawn-icon approach CogwheelButton already
# uses for its gear. Idle: static, text_muted. Spinning: same arc rotates
# continuously via a 16ms timer (~6°/tick, roughly one rotation per second),
# accent-colored while spinning.

class SpinButton(QPushButton):
    def __init__(self, parent=None):
        super().__init__("", parent)
        self._angle = 0
        self._spinning = False
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._on_tick)

    def start_spin(self):
        self._spinning = True
        self._timer.start()
        self.update()

    def stop_spin(self):
        self._spinning = False
        self._timer.stop()
        self._angle = 0
        self.update()

    def _on_tick(self):
        self._angle = (self._angle + 6) % 360
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(COLORS["accent"]) if self._spinning else QColor(COLORS["text_muted"])

        p.translate(self.width() / 2, self.height() / 2)
        if self._spinning:
            p.rotate(self._angle)

        r = min(self.width(), self.height()) / 2 - 7
        pen = QPen(color, 2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawArc(QRectF(-r, -r, r * 2, r * 2), 0, 270 * 16)

        # Arrowhead at the trailing end of the 270° arc.
        end_rad = math.radians(-270)
        tip = QPointF(r * math.cos(end_rad), -r * math.sin(end_rad))
        tangent = math.radians(-270 - 90)
        head = 4.0
        wing1 = QPointF(tip.x() + head * math.cos(tangent + 0.5),
                        tip.y() - head * math.sin(tangent + 0.5))
        wing2 = QPointF(tip.x() + head * math.cos(tangent - 0.5),
                        tip.y() - head * math.sin(tangent - 0.5))
        p.setBrush(QBrush(color))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPolygon(QPolygonF([tip, wing1, wing2]))
        p.end()


# ── Notifications (bell icon + dropdown panel) ────────────────────────────────
# Spec: Notion → Features → Fully Planned → Notifications.

_NOTIF_TYPE_ICONS = {
    "version_update":     "🔄",
    "protondb_change":    "🎮",
    "save_backup_failed": "⚠️",
    "scan_summary":       "📦",
    "corrupted_archive":  "🗄",
    "app_update":         "⬆️",
}


def _relative_time(created_at) -> str:
    """'3 minutes ago' / '2 hours ago' / '5 days ago' style label, matching
    the age-formatting convention already used elsewhere (game_detail.py's
    _VersionRow, main_window.py's last-played row)."""
    if not created_at:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(str(created_at).replace("T", " ")[:19])
    except (ValueError, TypeError):
        return ""
    secs = (datetime.datetime.utcnow() - dt).total_seconds()
    if secs < 60:
        return "just now"
    mins = int(secs // 60)
    if mins < 60:
        return f"{mins} minute{'s' if mins != 1 else ''} ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"


class NotificationBell(QPushButton):
    """
    Bell icon button for the library header — opens the notifications
    dropdown panel. The unread-count badge is a small child QLabel overlay,
    the same pattern GameTile uses for badge_installed/badge_favorite
    elsewhere in this file.
    """

    def __init__(self, parent=None):
        super().__init__("🔔", parent)
        self.setFixedSize(36, 36)
        self.setFont(QFont("DM Sans", 14))
        self.setToolTip("Notifications")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(f"""
            QPushButton {{
                background: {COLORS['surface']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
                color: {COLORS['text_muted']};
            }}
            QPushButton:hover {{
                color: {COLORS['text']};
                border-color: rgba(255,255,255,0.15);
            }}
        """)
        self.badge = QLabel("", parent=self)
        self.badge.setFont(QFont("DM Mono", 8, QFont.Weight.Bold))
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge.setStyleSheet(f"""
            QLabel {{
                background: {COLORS['accent']};
                color: #000;
                border-radius: 8px;
            }}
        """)
        self.badge.hide()

    def set_count(self, count: int):
        if count <= 0:
            self.badge.hide()
            return
        text = str(count) if count <= 99 else "99+"
        self.badge.setText(text)
        self.badge.adjustSize()
        w = max(16, self.badge.width() + 6)
        self.badge.setFixedSize(w, 16)
        self.badge.move(self.width() - w + 6, -4)
        self.badge.show()


class _NotificationRow(QFrame):
    """One row in the notifications dropdown panel."""
    dismissed   = pyqtSignal(int)          # notification id
    clicked_row = pyqtSignal(str, object)  # type, game_id (may be None)

    def __init__(self, notif, parent=None):
        super().__init__(parent)
        self._id = notif["id"]
        self._type = notif["type"]
        self._game_id = _safe_get(notif, "game_id")
        is_read = bool(notif["is_read"])

        self.setCursor(Qt.CursorShape.PointingHandCursor)
        bg = COLORS["surface2"] if is_read else "rgba(232,199,106,0.07)"
        border = COLORS["border"] if is_read else "rgba(232,199,106,0.25)"
        self.setStyleSheet(f"""
            QFrame {{
                background: {bg};
                border: 1px solid {border};
                border-radius: 8px;
            }}
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 8, 8)
        layout.setSpacing(8)

        icon = QLabel(_NOTIF_TYPE_ICONS.get(self._type, "•"))
        icon.setFont(QFont("DM Sans", 12))
        icon.setFixedWidth(20)
        icon.setStyleSheet("background: transparent; border: none;")
        layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        text_col.setContentsMargins(0, 0, 0, 0)

        title_lbl = QLabel(notif["title"])
        title_lbl.setFont(QFont("DM Sans", 10, QFont.Weight.DemiBold))
        title_lbl.setStyleSheet(f"color: {COLORS['text']}; background: transparent; border: none;")
        title_lbl.setWordWrap(True)
        text_col.addWidget(title_lbl)

        msg_lbl = QLabel(notif["message"] or "")
        msg_lbl.setFont(QFont("DM Sans", 9))
        msg_lbl.setStyleSheet(f"color: {COLORS['text_muted']}; background: transparent; border: none;")
        msg_lbl.setWordWrap(True)
        text_col.addWidget(msg_lbl)

        time_lbl = QLabel(_relative_time(notif["created_at"]))
        time_lbl.setFont(QFont("DM Mono", 8))
        time_lbl.setStyleSheet(f"color: {COLORS['text_muted']}; background: transparent; border: none;")
        text_col.addWidget(time_lbl)

        layout.addLayout(text_col, 1)

        dismiss_btn = QPushButton("✕")
        dismiss_btn.setFixedSize(20, 20)
        dismiss_btn.setFont(QFont("DM Sans", 9))
        dismiss_btn.setToolTip("Dismiss")
        dismiss_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; border: none; color: {COLORS['text_muted']}; }}
            QPushButton:hover {{ color: {COLORS['danger']}; }}
        """)
        dismiss_btn.clicked.connect(lambda: self.dismissed.emit(self._id))
        layout.addWidget(dismiss_btn, 0, Qt.AlignmentFlag.AlignTop)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked_row.emit(self._type, self._game_id)
        super().mousePressEvent(event)


class NotificationPanel(QWidget):
    """
    Dropdown panel shown below the bell icon. A frameless Popup window so it
    closes automatically when the user clicks anywhere outside it (Qt's
    built-in behavior for Qt.WindowType.Popup — no manual focus-out handling
    needed).

    notification_clicked(type, game_id): re-emitted upward so LibraryView
    can dispatch navigation per type (see LibraryView._on_notification_clicked).
    state_changed: emitted after anything that could change the unread count
    (open/read, dismiss, clear all) so the bell badge can be refreshed.
    """
    notification_clicked = pyqtSignal(str, object)
    state_changed         = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setFixedWidth(360)
        self.setMaximumHeight(440)
        self.setStyleSheet(f"""
            QWidget {{
                background: {COLORS['surface2']};
                border: 1px solid {COLORS['border']};
                border-radius: 10px;
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QWidget()
        header.setStyleSheet("background: transparent; border: none;")
        h_l = QHBoxLayout(header)
        h_l.setContentsMargins(14, 10, 10, 10)
        h_l.setSpacing(8)

        title = QLabel("Notifications")
        title.setFont(QFont("DM Sans", 12, QFont.Weight.DemiBold))
        title.setStyleSheet(f"color: {COLORS['text']}; background: transparent; border: none;")
        h_l.addWidget(title)
        h_l.addStretch()

        clear_btn = QPushButton("Clear all")
        clear_btn.setFont(QFont("DM Sans", 9))
        clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; border: none; color: {COLORS['text_muted']}; padding: 2px 4px; }}
            QPushButton:hover {{ color: {COLORS['danger']}; }}
        """)
        clear_btn.clicked.connect(self._on_clear_all)
        h_l.addWidget(clear_btn)
        layout.addWidget(header)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background: {COLORS['border']}; border: none;")
        layout.addWidget(sep)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setMaximumHeight(380)

        self.list_body = QWidget()
        self.list_body.setStyleSheet("background: transparent;")
        self.list_layout = QVBoxLayout(self.list_body)
        self.list_layout.setContentsMargins(6, 6, 6, 6)
        self.list_layout.setSpacing(4)
        self.list_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll.setWidget(self.list_body)
        layout.addWidget(self.scroll)

    def refresh(self):
        while self.list_layout.count():
            item = self.list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        notifs = db.get_notifications()
        if not notifs:
            empty = QLabel("No notifications")
            empty.setFont(QFont("DM Sans", 10))
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setStyleSheet(
                f"color: {COLORS['text_muted']}; background: transparent; "
                "border: none; padding: 24px 0;")
            self.list_layout.addWidget(empty)
            return

        for n in notifs:
            row = _NotificationRow(n)
            row.dismissed.connect(self._on_dismiss)
            row.clicked_row.connect(self._on_row_clicked)
            self.list_layout.addWidget(row)

    def showEvent(self, event):
        super().showEvent(event)
        # Build the list reflecting the CURRENT read/unread state first, so
        # anything unread is visibly highlighted the moment the user opens
        # the panel — then mark everything read so the badge clears and a
        # future re-open shows these as already-seen.
        self.refresh()
        db.mark_all_read()
        self.state_changed.emit()

    def _on_dismiss(self, notif_id: int):
        # AppImage Self-Update Check: dismissing an app_update notification
        # skips that version's notification on future launch checks (manual
        # checks in Settings → About are unaffected — see update_check.py).
        notif = db.get_notification_by_id(notif_id)
        if notif and notif["type"] == "app_update":
            import update_check
            version = update_check.extract_version_from_title(notif["title"])
            if version:
                db.set_setting("skipped_app_version", version)
        db.dismiss_notification(notif_id)
        self.refresh()
        self.state_changed.emit()

    def _on_clear_all(self):
        db.clear_all_notifications()
        self.refresh()
        self.state_changed.emit()

    def _on_row_clicked(self, type_: str, game_id):
        self.notification_clicked.emit(type_, game_id)
        self.hide()


# Sort Library — (display label, FilterState.sort value), standard views.
# Collection views prepend ("Custom Order", "custom") — see
# LibraryView._sort_options_for_current_view().
SORT_OPTIONS_STANDARD = [
    ("A → Z",                     "az"),
    ("Z → A",                     "za"),
    ("Playtime (Most First)",     "playtime_desc"),
    ("Playtime (Least First)",    "playtime_asc"),
    ("Last Played (Recent First)", "last_played_desc"),
    ("Last Played (Oldest First)", "last_played_asc"),
]

# Group D filter — (display label, FilterState.completion value). None = no filter.
COMPLETION_FILTER_OPTIONS = [
    ("All Statuses", None),
    ("Unplayed",     "unplayed"),
    ("In Progress",  "in_progress"),
    ("Completed",    "completed"),
    ("Abandoned",    "abandoned"),
]


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

    Group B' — collection (Collections / Playlists feature):
        None = no collection filter
        Mutually exclusive with category — the sidebar picks one or the
        other, never both (selecting a collection clears category and
        vice versa — see with_category()/with_collection()).

    Group D — completion status:
        None = no completion filter
        One of: "unplayed", "in_progress", "completed", "abandoned"

    Search — substring match against title (case-insensitive):
        "" = no search filter

    Hidden games:
        Never shown unless sidebar_key == "hidden".
        Even "all" excludes hidden games.

    Sort (Sort Library feature) — one of SORT_OPTIONS_STANDARD's values, or
    "custom" (collections only, orders by collection_games.sort_order —
    see with_sort()'s docstring for the persistence rules). Defaults to
    "az" for every standard view and "custom" when entering a collection —
    matching the sort dropdown's per-view default. Resets to that default
    on sidebar navigation (with_sidebar()/with_category()/with_collection());
    preserved across search/completion/tag changes and across a detail-page
    round trip, since those never touch sort at all.
    """
    sidebar_key:  str            = "all"
    category:     Optional[str]  = None
    collection:   Optional[int]  = None   # active collection id, or None
    completion:   Optional[str]  = None
    search:       str            = ""
    tags:         frozenset      = field(default_factory=frozenset)
    sort:         str            = "az"

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
        if self.category or self.collection:
            # Category/collection view — title is set by the caller from
            # the DB display name / collection name
            return base
        return base

    def with_sidebar(self, key: str) -> "FilterState":
        """Return a copy with sidebar_key changed. Resets sort to the
        standard-view default (A→Z) — sidebar navigation always resets sort,
        per Sort Library's persistence rules."""
        return FilterState(
            sidebar_key=key,
            category=self.category,
            collection=None,
            completion=self.completion,
            search=self.search,
            tags=self.tags,
            sort="az",
        )

    def with_category(self, cat: Optional[str]) -> "FilterState":
        """Return a copy with category changed. Clears collection — the two
        are mutually exclusive Group B selections. Resets sort to A→Z, same
        as with_sidebar() — picking a category is sidebar navigation."""
        return FilterState(
            sidebar_key=self.sidebar_key,
            category=cat,
            collection=None,
            completion=self.completion,
            search=self.search,
            tags=self.tags,
            sort="az",
        )

    def with_collection(self, collection_id: Optional[int]) -> "FilterState":
        """Return a copy with collection changed. Clears category — the two
        are mutually exclusive Group B selections. Resets sort to Custom
        Order when entering a collection, or back to A→Z when leaving one
        (collection_id=None) — sidebar navigation always resets sort."""
        return FilterState(
            sidebar_key=self.sidebar_key,
            category=None,
            collection=collection_id,
            completion=self.completion,
            search=self.search,
            tags=self.tags,
            sort=("custom" if collection_id is not None else "az"),
        )

    def with_completion(self, status: Optional[str]) -> "FilterState":
        """Return a copy with completion status changed. Sort is preserved —
        narrowing by completion status within the same view isn't sidebar
        navigation."""
        return FilterState(
            sidebar_key=self.sidebar_key,
            category=self.category,
            collection=self.collection,
            completion=status,
            search=self.search,
            tags=self.tags,
            sort=self.sort,
        )

    def with_search(self, text: str) -> "FilterState":
        """Return a copy with search text changed. Sort is preserved."""
        return FilterState(
            sidebar_key=self.sidebar_key,
            category=self.category,
            collection=self.collection,
            completion=self.completion,
            search=text,
            tags=self.tags,
            sort=self.sort,
        )

    def with_tags(self, tag_ids) -> "FilterState":
        """Return a copy with the active tag filter changed (Group C). Sort
        is preserved."""
        return FilterState(
            sidebar_key=self.sidebar_key,
            category=self.category,
            collection=self.collection,
            completion=self.completion,
            search=self.search,
            tags=frozenset(tag_ids),
            sort=self.sort,
        )

    def with_sort(self, sort: str) -> "FilterState":
        """Return a copy with sort changed — the sort dropdown's own
        handler. Nothing else about the view changes."""
        return FilterState(
            sidebar_key=self.sidebar_key,
            category=self.category,
            collection=self.collection,
            completion=self.completion,
            search=self.search,
            tags=self.tags,
            sort=sort,
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
    # game_id, Qt.KeyboardModifier — modifiers are what let LibraryView tell
    # a plain click from a modifier-held click (multi-select entry) or a
    # shift-held click (range select) apart, without GameTile itself needing
    # to know anything about selection policy — see Multi-Select & Bulk Tile
    # Actions.
    clicked        = pyqtSignal(int, object)
    state_changed  = pyqtSignal(int)   # game_id — emitted after context menu action
    track_versions = pyqtSignal(int)   # game_id — open VersionTrackerDialog
    edit_metadata  = pyqtSignal(int)   # game_id — open EditMetadataDialog
    # Multi-Select & Bulk Tile Actions
    long_press_triggered = pyqtSignal(int)             # game_id — held past the configured duration
    bulk_menu_requested  = pyqtSignal(int, QPoint)      # game_id, global pos — right-click while selected
    # Collections / Playlists
    collection_toggled = pyqtSignal(int, int, bool)  # game_id, collection_id, now_in
    collection_created = pyqtSignal(int, str)         # game_id, new_collection_name
    removed_from_active_collection = pyqtSignal(int)  # game_id

    def __init__(self, game, tile_width: int = 160, parent=None,
                 is_playing_fn=None, active_collection_fn=None,
                 is_selected_fn=None, multi_select_active_fn=None,
                 long_press_enabled_fn=None, long_press_ms_fn=None):
        super().__init__(parent)
        self.game_id     = game["id"]
        self.tile_width  = tile_width
        # Zero-arg-per-call callable: is_playing_fn(game_id) -> bool.
        # Bound once at construction (tiles are pooled/reused across many
        # different games — see LibraryView._tile_pool — so this must be
        # re-invoked on every _apply_game() rebind, never cached as a
        # fixed bool). Passed in by LibraryView rather than read from a
        # module-level global so pooled tiles always reflect the CURRENT
        # currently-playing game, even after being reused for a different
        # one. None (e.g. tiles built without this wiring) just means the
        # overlay never shows — safe default.
        self._is_playing_fn = is_playing_fn
        # Zero-arg-per-call callable: active_collection_fn() -> (collection_id,
        # name) | (None, None) — whichever collection the library is
        # CURRENTLY viewing, if any. Same "call it fresh every rebind, never
        # cache" contract as is_playing_fn above — a pooled tile reused while
        # the user is inside a collection view must reflect that, not
        # whatever was true when the tile was first constructed. None means
        # the "Remove from [Collection]" menu item never appears.
        self._active_collection_fn = active_collection_fn

        # Multi-Select & Bulk Tile Actions — zero/one-arg callables re-invoked
        # on every interaction and every pool rebind (same "never cache"
        # contract as is_playing_fn/active_collection_fn above), so a pooled
        # tile reused for a different game always reflects the CURRENT
        # selection state and CURRENT selection-method setting, not whatever
        # was true when the tile was first constructed.
        self._is_selected_fn         = is_selected_fn
        self._multi_select_active_fn = multi_select_active_fn
        self._long_press_enabled_fn  = long_press_enabled_fn
        self._long_press_ms_fn       = long_press_ms_fn
        self._selected = False

        # Collections / Playlists — drag-and-drop in-collection reordering.
        # Disabled by default; LibraryView turns this on per-tile only while
        # viewing an unlocked collection (see set_drag_enabled() and
        # LibraryView._collection_reorder_enabled()/_update_tiles_drag_enabled()).
        # Outside that context tiles behave exactly as before — a plain
        # click-to-open, no drag detection at all.
        self._drag_enabled = False
        self._press_pos: Optional[QPoint] = None
        self._press_modifiers = Qt.KeyboardModifier.NoModifier
        self._drag_started = False
        self._DRAG_THRESHOLD = 10   # px of mouse movement before a press counts as a drag

        # Multi-Select long-press timer — one per tile, reused across pool
        # rebinds (stopped/reset in _apply_game(), same pattern as the drag
        # state above). Only started on press when the configured selection
        # method includes long-press AND the tile isn't currently drag-capable
        # (collection-reorder drag takes precedence over long-press-to-select
        # in that narrow context — see mousePressEvent()).
        self._long_press_timer = QTimer(self)
        self._long_press_timer.setSingleShot(True)
        self._long_press_timer.timeout.connect(self._on_long_press_timeout)
        self._long_press_fired = False

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
        self._has_cover = False   # tracks whether cover_label currently shows a
                                   # real cover vs. the placeholder — lets
                                   # _apply_game() skip setStyleSheet() (a real
                                   # Qt CSS re-parse) on rebinds that never had
                                   # one loaded in the first place
        layout.addWidget(self.cover_label)

        # Footer
        footer = QWidget()
        footer.setStyleSheet(f"background: {COLORS['surface']}; border: none;")
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(10, 8, 10, 10)
        footer_layout.setSpacing(0)

        self.title_label = QLabel()
        self.title_label.setFont(QFont("DM Sans", 10, QFont.Weight.Medium))
        self.title_label.setStyleSheet(
            f"color: {COLORS['text']}; background: transparent;"
        )
        self.title_label.setFixedWidth(tile_width - 20)
        footer_layout.addWidget(self.title_label)
        layout.addWidget(footer)

        # ── Badge overlays ────────────────────────────────────────────────────
        # Created once and reused across rebinds — toggled via show()/hide()
        # rather than being torn down and recreated, since setStyleSheet()
        # parsing is one of the more expensive things Qt does per-widget and
        # this class gets rebound frequently while scrolling under
        # virtualization (see LibraryView's tile pool).
        self.badge_installed = QLabel("Installed", parent=self)
        self.badge_installed.setFont(QFont("DM Mono", 8))
        self.badge_installed.setStyleSheet(f"""
            QLabel {{
                background: rgba(74,222,128,0.15);
                border: 1px solid rgba(74,222,128,0.4);
                border-radius: 4px;
                color: {COLORS['installed']};
                padding: 2px 6px;
            }}
        """)
        self.badge_installed.adjustSize()
        self.badge_installed.move(tile_width - self.badge_installed.width() - 8, 8)
        self.badge_installed.hide()

        self.badge_favorite = QLabel("★", parent=self)
        self.badge_favorite.setFont(QFont("DM Sans", 11))
        self.badge_favorite.setStyleSheet(f"""
            QLabel {{
                background: rgba(232,199,106,0.18);
                border: 1px solid rgba(232,199,106,0.45);
                border-radius: 4px;
                color: {COLORS['accent']};
                padding: 1px 5px;
            }}
        """)
        self.badge_favorite.adjustSize()
        self.badge_favorite.move(8, 8)
        self.badge_favorite.hide()

        # "Now Playing" overlay — banner across the lower portion of the
        # cover art. Created once and reused across rebinds like the
        # badges above, toggled via update_playing_overlay() rather than
        # torn down/recreated for the same performance reason (see the
        # badge comment above this).
        self.playing_overlay = QLabel("▶  NOW PLAYING", parent=self)
        self.playing_overlay.setFont(QFont("Rajdhani", 10, QFont.Weight.Bold))
        self.playing_overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.playing_overlay.setStyleSheet(f"""
            QLabel {{
                background: rgba(232,199,106,0.88);
                color: #0d0f14;
                letter-spacing: 1px;
                padding: 5px 0;
            }}
        """)
        self.playing_overlay.setFixedWidth(tile_width)
        self.playing_overlay.move(0, cover_h - 26)
        self.playing_overlay.hide()

        # Multi-Select & Bulk Tile Actions — checkmark overlay. Bottom-left
        # of the cover, above the playing-overlay strip, so it never
        # collides with badge_favorite (top-left) or badge_installed
        # (top-right). Toggled via set_selected(); the tile's own border
        # also changes there for a stronger "this one's picked" signal.
        self.selection_badge = QLabel("✓", parent=self)
        self.selection_badge.setFont(QFont("DM Sans", 11, QFont.Weight.Bold))
        self.selection_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.selection_badge.setFixedSize(22, 22)
        self.selection_badge.setStyleSheet(f"""
            QLabel {{
                background: {COLORS['accent']};
                border-radius: 11px;
                color: #0d0f14;
            }}
        """)
        self.selection_badge.move(8, cover_h - 30)
        self.selection_badge.hide()

        self._apply_game(game)

    def update_playing_overlay(self, is_playing: bool):
        self.playing_overlay.setVisible(is_playing)

    def _apply_game(self, game):
        """
        Bind this tile to a (possibly different) game row. Used both for
        initial construction and for reuse via LibraryView's tile pool —
        keeping this as a single code path means pooled tiles and freshly
        constructed tiles always end up in an identical state.
        """
        self.game_id       = game["id"]
        self._is_favorite  = bool(_safe_get(game, "is_favorite", 0))
        self._is_hidden    = bool(_safe_get(game, "is_hidden",   0))
        self._is_installed = bool(game["is_installed"])

        title = game["title"] or game["display_name"] or game["folder_name"]
        elided = self.title_label.fontMetrics().elidedText(
            title, Qt.TextElideMode.ElideRight, self.tile_width - 20
        )
        self.title_label.setText(elided)
        self.title_label.setToolTip(title)

        self.badge_installed.setVisible(self._is_installed)
        self.badge_favorite.setVisible(self._is_favorite)

        # Re-check playing state on every rebind (not just construction) —
        # this is what makes the overlay correct for a pooled tile that
        # just got reused for a different game. See __init__'s comment on
        # _is_playing_fn for why this can't be a cached bool.
        is_playing = bool(self._is_playing_fn and self._is_playing_fn(self.game_id))
        self.update_playing_overlay(is_playing)

        # Multi-Select — re-check on every rebind, not just construction,
        # same reasoning as is_playing above: a pooled tile reused for a
        # different game must reflect THAT game's selection state.
        is_selected = bool(self._is_selected_fn and self._is_selected_fn(self.game_id))
        self.set_selected(is_selected)

        # Reset cover to the placeholder — a stale pixmap from whatever game
        # previously occupied this pooled tile must never show, even briefly.
        # Only touch styling if this tile actually had a real cover showing;
        # a tile that was already on the placeholder needs no CSS re-parse.
        if self._has_cover:
            self.cover_label.setPixmap(QPixmap())
            self.cover_label.setStyleSheet(
                f"background: {COLORS['surface2']}; border: none;"
            )
            self._has_cover = False

        # Defensive reset — a pooled tile should never carry a stale
        # in-progress drag/press state from whatever game it was last bound
        # to. set_drag_enabled() is called again by the caller right after
        # every rebind (see LibraryView._update_visible_tiles()), so this
        # only guards against an interrupted drag, not normal drag-state
        # setup.
        self._press_pos = None
        self._drag_started = False
        self._long_press_timer.stop()
        self._long_press_fired = False

    def set_selected(self, selected: bool):
        """
        Multi-Select & Bulk Tile Actions — toggle this tile's "picked"
        visual: the accent checkmark badge plus an accent border on the
        tile itself. Safe to call on every rebind (see _apply_game()) since
        it no-ops when the visual state already matches, same perf
        reasoning as the badge/cover-reset guards elsewhere in this class.
        """
        if self._selected == selected:
            return
        self._selected = selected
        self.selection_badge.setVisible(selected)
        if selected:
            self.setStyleSheet(f"""
                QFrame {{
                    background: {COLORS['surface']};
                    border: 2px solid {COLORS['accent']};
                    border-radius: 10px;
                }}
            """)
        else:
            self.setStyleSheet(f"""
                QFrame {{
                    background: {COLORS['surface']};
                    border: 1px solid {COLORS['border']};
                    border-radius: 10px;
                }}
            """)

    def set_cover_pixmap(self, pixmap: QPixmap):
        """
        Apply an already-scaled-and-cropped cover pixmap. Scaling/cropping is
        done once by LibraryView (see _build_cover_pixmap()) and cached by
        game_id, so a tile revisited while scrolling back and forth never
        re-decodes or re-scales the image — this just swaps a pixmap in.
        """
        self.cover_label.setPixmap(pixmap)
        if not self._has_cover:
            self.cover_label.setStyleSheet("background: transparent; border: none;")
            self._has_cover = True

    def set_drag_enabled(self, enabled: bool):
        """
        Collections / Playlists — turn drag-to-reorder on/off for this tile.
        Called by LibraryView on every rebind/construction, reflecting
        whether the library is currently viewing an UNLOCKED collection
        (see LibraryView._collection_reorder_enabled()). When disabled,
        mouse handling is completely unchanged from a plain click-to-open
        tile — no drag detection overhead outside a collection view.
        """
        if self._drag_enabled == enabled:
            return
        self._drag_enabled = enabled
        self.setCursor(Qt.CursorShape.OpenHandCursor if enabled
                       else Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        # Every click now resolves on release rather than press — needed so
        # a real click, a drag-reorder, and a long-press can all start
        # identically and only diverge once (and if) the mouse moves past
        # _DRAG_THRESHOLD or the long-press timer fires. The timing
        # difference between "on press" and "on release" for a genuine
        # click is imperceptible; see mouseMoveEvent()/mouseReleaseEvent().
        self._press_pos = event.position().toPoint()
        self._press_modifiers = event.modifiers()
        self._drag_started = False
        self._long_press_fired = False
        # Long-press competes with collection drag-reorder only in the
        # narrow window where both are active (viewing an unlocked
        # collection) — drag-reorder wins there, since holding+moving in
        # that context reads as "I'm curating order," not "I'm selecting."
        # Modifier-click multi-select entry still works in that context
        # regardless, since it doesn't depend on this timer at all.
        if (not self._drag_enabled and self._long_press_enabled_fn
                and self._long_press_enabled_fn()):
            ms = self._long_press_ms_fn() if self._long_press_ms_fn else 500
            self._long_press_timer.start(ms)

    def mouseMoveEvent(self, event):
        if self._press_pos is None:
            return
        moved = (event.position().toPoint() - self._press_pos).manhattanLength()
        if moved >= self._DRAG_THRESHOLD:
            self._long_press_timer.stop()   # real movement cancels a long-press
        if not self._drag_enabled or self._drag_started:
            return
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            return
        if moved < self._DRAG_THRESHOLD:
            return
        self._drag_started = True
        self._start_drag()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._long_press_timer.stop()
        fired_long_press = self._long_press_fired
        self._long_press_fired = False
        if self._drag_enabled and self._drag_started:
            # A real reorder drag happened — suppress the click entirely,
            # same as before.
            self._press_pos = None
            self._drag_started = False
            return
        if not fired_long_press and self._press_pos is not None:
            # Neither a drag nor a long-press claimed this press — a
            # genuine click. LibraryView decides what a click means right
            # now (open detail, toggle selection, enter multi-select, or
            # range-select) based on modifiers and current mode.
            self.clicked.emit(self.game_id, self._press_modifiers)
        self._press_pos = None
        self._drag_started = False

    def _on_long_press_timeout(self):
        """Fires only if the press is still live and hasn't moved past the
        drag threshold (mouseMoveEvent() stops this timer the moment that
        happens) — see LibraryView._on_tile_long_press() for what a fired
        long-press actually does."""
        if self._press_pos is None:
            return
        self._long_press_fired = True
        self.long_press_triggered.emit(self.game_id)

    def _start_drag(self):
        """
        Collections / Playlists — kick off a Qt drag carrying this tile's
        game_id, with a semi-transparent grab of the tile itself as the
        cursor's drag visual. Blocks (Qt pumps the event loop internally)
        until the drag ends via a drop or Escape; the actual reorder work
        happens on the drop side — see LibraryView._on_canvas_drop().
        """
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(_GAME_DRAG_MIME, str(self.game_id).encode("utf-8"))
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
        if self._multi_select_active_fn and self._multi_select_active_fn():
            # Multi-select is active — right-click never shows the normal
            # per-game menu here. An unselected tile blocks the menu
            # entirely (right-clicking a tile you haven't picked isn't a
            # meaningful "act on my selection" gesture); a selected tile
            # opens the bulk menu, built by LibraryView since it needs the
            # whole selection's state, not just this one tile's.
            if not self._selected:
                event.ignore()
                return
            self.bulk_menu_requested.emit(self.game_id, event.globalPos())
            return

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
            QMenu::item:disabled {{
                color: {COLORS['text_muted']};
            }}
            QMenu::separator {{
                height: 1px;
                background: {COLORS['border']};
                margin: 3px 8px;
            }}
        """)

        # ── Open directories — only shown/enabled when there's somewhere to
        # open. Install dir only exists for installed games; archive dir is
        # resolved fresh at click time (see _open_archive_directory) since
        # the lightweight library query this tile was built from doesn't
        # carry nas_path.
        open_install_action = None
        if self._is_installed:
            open_install_action = menu.addAction("📁  Open Install Directory")
        open_archive_action = menu.addAction("🗄  Open Archive Directory")

        # Metadata
        menu.addSeparator()
        edit_meta_action = menu.addAction("✏️  Edit Metadata…")

        # Favorite / Hide
        menu.addSeparator()
        if self._is_favorite:
            fav_action = menu.addAction("★  Remove from Favorites")
        else:
            fav_action = menu.addAction("☆  Add to Favorites")
        if self._is_hidden:
            hide_action = menu.addAction("👁  Unhide Game")
        else:
            hide_action = menu.addAction("⊘  Hide Game")

        # Version tracking
        menu.addSeparator()
        track_action = menu.addAction("🔔  Track Version Updates…")

        # ── Collections ──────────────────────────────────────────────────────
        menu.addSeparator()
        active_collection_id, active_collection_name = (
            self._active_collection_fn() if self._active_collection_fn else (None, None)
        )

        remove_from_active_action = None
        if active_collection_id is not None:
            remove_from_active_action = menu.addAction(
                f"➖  Remove from {active_collection_name}")

        add_to_collection_menu = menu.addMenu("➕  Add to Collection")
        member_ids = db.get_collection_ids_for_game(self.game_id)
        collections = db.get_collections_for_tile_menu()
        collection_actions = {}
        for c in collections:
            is_member = c["id"] in member_ids
            label = ("✓  " if is_member else "") + c["name"]
            act = add_to_collection_menu.addAction(label)
            collection_actions[act] = (c["id"], is_member)
        add_to_collection_menu.addSeparator()
        new_collection_action = add_to_collection_menu.addAction("+ New Collection…")

        chosen = menu.exec(event.globalPos())
        if open_install_action is not None and chosen == open_install_action:
            self._open_install_directory()
        elif chosen == open_archive_action:
            self._open_archive_directory()
        elif chosen == edit_meta_action:
            self.edit_metadata.emit(self.game_id)
        elif chosen == fav_action:
            db.set_favorite(self.game_id, not self._is_favorite)
            self.state_changed.emit(self.game_id)
        elif chosen == hide_action:
            db.set_hidden(self.game_id, not self._is_hidden)
            self.state_changed.emit(self.game_id)
        elif chosen == track_action:
            self.track_versions.emit(self.game_id)
        elif remove_from_active_action is not None and chosen == remove_from_active_action:
            self.removed_from_active_collection.emit(self.game_id)
        elif chosen == new_collection_action:
            from PyQt6.QtWidgets import QInputDialog
            name, ok = QInputDialog.getText(self, "New Collection", "Collection name:")
            name = name.strip()
            if ok and name:
                self.collection_created.emit(self.game_id, name)
        elif chosen in collection_actions:
            collection_id, was_member = collection_actions[chosen]
            self.collection_toggled.emit(self.game_id, collection_id, not was_member)

    # ── Open directory actions ───────────────────────────────────────────────
    # Mirrors ui/cogwheel_menu.py's CogwheelButton._open_folder()/_open_path()
    # pattern exactly, so behavior (xdg-open, missing-folder messaging) is
    # identical regardless of which entry point the user used. Re-fetches
    # the full game row here rather than widening the lightweight library
    # query (db.get_games_for_library() deliberately skips install_path/
    # nas_path for every tile — see its docstring) since this only runs
    # once, on an explicit user action.

    def _open_install_directory(self):
        game = db.get_game(self.game_id)
        if not game:
            return
        install_path = (game["install_path"] or "").strip()
        if not install_path or not Path(install_path).exists():
            QMessageBox.information(
                self, "Open Folder",
                "Install folder not found. It may have been moved or deleted.")
            return
        self._open_path(install_path)

    def _open_archive_directory(self):
        game = db.get_game(self.game_id)
        if not game:
            return
        nas_path = (game["nas_path"] or "").strip()
        if not nas_path or not Path(nas_path).exists():
            QMessageBox.information(
                self, "Open Folder",
                "Archive folder not found on the NAS. It may be unreachable or moved.")
            return
        self._open_path(nas_path)

    def _open_path(self, path: str):
        try:
            subprocess.Popen(["xdg-open", path])
        except Exception as e:
            QMessageBox.warning(self, "Open Folder", f"Could not open folder:\n{e}")


# ── Library View ──────────────────────────────────────────────────────────────

class _LibraryCanvas(QWidget):
    """
    The virtualized grid's positioning surface (see LibraryView's class
    docstring) — a plain QWidget with tiles positioned via setGeometry(),
    no QLayout. Subclassed only so it can accept drops for in-collection
    drag-and-drop reordering (Collections / Playlists' Lock B) and paint a
    thin insertion-line indicator while a drag is over it.

    All the actual reorder logic and grid geometry math stays on
    LibraryView (the owner) — this class only forwards Qt drag/drop events
    to it and renders whatever insertion index LibraryView computed, so
    there's a single source of truth for column count, tile size, and
    margins instead of duplicating that bookkeeping here.
    """

    def __init__(self, owner: "LibraryView", parent=None):
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


class LibraryView(QWidget):
    game_selected             = pyqtSignal(int)
    refresh_requested         = pyqtSignal(str)
    # Kept for MainWindow compatibility (it chains a pending scan off this
    # signal). With virtualization there's no multi-tick trickle anymore —
    # this now just fires once the initial visible tiles have been built.
    trickle_finished          = pyqtSignal()
    game_state_changed        = pyqtSignal(int)   # game_id — favorite/hide from tile
    track_versions_requested  = pyqtSignal(int)   # game_id — open VersionTrackerDialog
    edit_metadata_requested   = pyqtSignal(int)   # game_id — open EditMetadataDialog
    # Notifications — re-emitted from NotificationPanel.notification_clicked
    # so MainWindow can dispatch navigation per type (see
    # MainWindow._on_notification_navigate). game_id is None for types with
    # no single associated game (scan_summary, version_update).
    notification_navigate_requested = pyqtSignal(str, object)   # type, game_id
    # Collections / Playlists — re-emitted upward so MainWindow can refresh
    # the sidebar's game-count badges after membership changes made from
    # inside a tile's context menu (toggle/create/remove-from-active).
    collections_changed       = pyqtSignal()
    # Multi-Select & Bulk Tile Actions — "Refresh Metadata" bulk action.
    # MainWindow owns the SGDB/IGDB keys and the actual fetch worker (same
    # split as refresh_requested/track_versions_requested above), so this
    # just hands up the list of game_ids to refresh.
    bulk_metadata_refresh_requested = pyqtSignal(list)   # list[int]

    # Canvas layout constants — mirror the old outer_layout margins/spacing
    # so the grid looks the same as before.
    MARGIN_LEFT   = 28
    MARGIN_TOP    = 24
    MARGIN_RIGHT  = 28
    MARGIN_BOTTOM = 28
    ROW_SPACING   = 16
    COL_SPACING   = 16
    # Extra rows rendered above/below the visible viewport so tiles are
    # already in place (and their art already loading) before they scroll
    # into view, rather than popping in right at the edge. Kept modest
    # deliberately — with many columns (wide/maximized windows) each extra
    # buffer row costs a full row's worth of tiles, and tile reuse + the
    # cover pixmap cache make popping a tile in right at the edge cheap
    # enough that a large buffer isn't needed to stay smooth.
    BUFFER_ROWS   = 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self._all_games    : list = []
        self._filtered      : list = []
        self._filter_state : FilterState = FilterState()

        # index into self._filtered -> live GameTile. Only tiles currently
        # within [first_index, last_index] (viewport + buffer) exist here.
        self._tiles: dict[int, GameTile] = {}
        # game_id -> live GameTile, maintained in lockstep with self._tiles.
        # ImageLoader completions arrive keyed by game_id (often in bursts —
        # a whole row's worth, ~cols of them, scrolling in at once), and were
        # previously found by scanning every live tile; this makes that O(1).
        self._tiles_by_game_id: dict[int, GameTile] = {}

        # Tiles that scrolled out of range are hidden and parked here for
        # reuse rather than destroyed — recreating a GameTile from scratch
        # (QFrame + setStyleSheet parsing + child widgets) on every scroll
        # tick was the main source of scroll lag. Reusing a pooled tile via
        # GameTile._apply_game() skips almost all of that cost. Capped so a
        # brief huge-viewport moment (e.g. maximizing) can't leak widgets.
        self._tile_pool: list = []
        self._TILE_POOL_MAX = 400
        self._pool_tile_w: Optional[int] = None   # tile width the pool was built at

        # game_id -> pre-scaled, pre-cropped QPixmap sized for the CURRENT
        # tile width. Scrolling back over a game you've already seen this
        # session skips the disk-cache lookup, the ImageLoader thread-pool
        # round trip, AND the QPixmap scale/crop — this is the biggest win
        # for repeated back-and-forth scrolling. Bounded + roughly LRU via
        # _cover_cache_get()/_cover_cache_put() below so it can't grow
        # unbounded on a 1000+ game library. Cleared whenever tile size
        # changes, since cached pixmaps are scaled for the old size.
        self._cover_cache: dict[int, QPixmap] = {}
        self._COVER_CACHE_MAX = 600
        self._cover_cache_tile_w: Optional[int] = None

        # Coalesces bursts of QScrollBar.valueChanged (fired many times per
        # frame during a fast drag/kinetic scroll) into at most one
        # _update_visible_tiles() call per event-loop tick.
        self._scroll_update_pending = False

        self._cols       : int = 1
        self._tile_w      : int = 160
        self._tile_h      : int = 0
        self._row_h       : int = 0
        self._total_rows  : int = 0

        self._scan_running : bool = False

        # Currently Playing Indicator — persistent state, not a one-shot
        # "find the tile and toggle it" action. Required because the grid
        # is virtualized (see class docstring): the currently-playing
        # game's tile may not exist as a live widget at all when playback
        # starts (scrolled out of view), so GameTile._apply_game() checks
        # this via _is_game_playing() every time a tile is bound/rebound,
        # rather than this class reaching into the tile pool once at
        # launch time. Driven by MainWindow via set_currently_playing().
        self._currently_playing_game_id: Optional[int] = None

        self._pool = QThreadPool.globalInstance()
        # 4 was tuned for narrower grids. A wide/maximized window can have
        # 10+ columns, so a single row scrolling into view needs that many
        # cover fetches at once — with only 4 workers, most of a new row
        # sat queued and popped in staggered rather than together. These are
        # I/O-bound (disk cache check, occasional network fetch) rather than
        # CPU-heavy, so raising this costs little.
        self._pool.setMaxThreadCount(8)

        self._resize_timer = QTimer()
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(150)
        self._resize_timer.timeout.connect(self._on_resize_settled)

        # Debounces search-as-you-type — without this, every keystroke ran a
        # full filter + tile release/repopulate pass immediately. Same
        # pattern as the resize debounce above.
        self._pending_search_text = ""
        self._search_timer = QTimer()
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(150)
        self._search_timer.timeout.connect(self._apply_pending_search)

        # Multi-Select & Bulk Tile Actions — selection lives here rather
        # than on any tile, since tiles are pooled/virtualized and a
        # selected game's tile may not even exist as a live widget right
        # now (scrolled out of view). Mirrors the Currently Playing
        # Indicator's game_id-keyed-state approach for the same reason.
        self._multi_select_active: bool = False
        self._selected_game_ids: set = set()
        # Anchor for shift+click range selection — the last tile clicked
        # (toggled, entered-via, or ranged-to) while in multi-select mode.
        self._last_selected_game_id: Optional[int] = None

        # Collections / Playlists — in-collection drag-and-drop reordering.
        # _drag_insert_index is the grid index (row-major, same numbering
        # as self._filtered) the dragged tile would land at if dropped
        # right now — None means no drag is currently over the canvas.
        # Only ever non-None while _collection_reorder_enabled() is True.
        self._drag_insert_index: Optional[int] = None
        self._autoscroll_direction = 0   # -1 up, 0 idle, +1 down
        self._AUTOSCROLL_MARGIN = 40     # px from a viewport edge that triggers scrolling
        self._AUTOSCROLL_STEP   = 18     # px nudged per autoscroll tick
        self._autoscroll_timer = QTimer(self)
        self._autoscroll_timer.setInterval(30)
        self._autoscroll_timer.timeout.connect(self._autoscroll_tick)

        self._build_ui()

        # Multi-Select & Bulk Tile Actions — Escape clears the current
        # selection and exits multi-select. WindowShortcut context (the
        # default) means it only fires while this window has focus, so it
        # won't steal Escape from a modal dialog opened on top.
        self._escape_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self._escape_shortcut.activated.connect(self._clear_selection)

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

        self.page_title = _CollectionTitleLabel("All Games")
        self.page_title.setFont(QFont("Rajdhani", 18, QFont.Weight.DemiBold))
        self.page_title.clicked.connect(self._on_title_clicked)
        header_layout.addWidget(self.page_title)

        # Lock B (per-collection game-order lock) toggle — only shown while
        # actively viewing a collection (see _refresh_collection_header()).
        # Reordering games within a collection isn't built as live drag (see
        # Collections / Playlists' documented ↑/↓-buttons-instead-of-drag
        # scope decision) so this toggle currently only records intent for
        # a future reorder UI — clicking it still persists immediately via
        # db.set_collection_game_order_locked() so the state is correct and
        # visible (also editable from the sidebar's right-click menu) the
        # moment that UI exists.
        self.collection_lock_btn = QPushButton("")
        self.collection_lock_btn.setFont(QFont("DM Sans", 10))
        self.collection_lock_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.collection_lock_btn.setStyleSheet(f"""
            QPushButton {{
                background: {COLORS['surface2']};
                border: 1px solid {COLORS['border']};
                border-radius: 6px;
                color: {COLORS['text_muted']};
                padding: 4px 10px;
            }}
            QPushButton:hover {{
                color: {COLORS['text']};
                background: {COLORS['surface3']};
            }}
        """)
        self.collection_lock_btn.clicked.connect(self._on_collection_lock_btn_clicked)
        self.collection_lock_btn.hide()
        header_layout.addWidget(self.collection_lock_btn)

        header_layout.addStretch()

        # Sort Library — sits left of the completion filter and search box.
        # Layout conflict (flagged 2026-08-03 when Completion Status's Group D
        # combo landed in this same spot) resolved as: sort dropdown, then
        # completion filter, then search — both combos left of search, sort
        # first since it's the more frequently-reached-for of the two.
        self.sort_combo = NoScrollComboBox()
        self.sort_combo.setFont(QFont("DM Sans", 10))
        self.sort_combo.setFixedWidth(170)
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        header_layout.addWidget(self.sort_combo)
        self._refresh_sort_combo()

        # Group D filter (completion status) previously lived here as a
        # header combo box. Collapsible Sidebar Groups (resolved 2026-08-28
        # as option b) migrated it into the sidebar as a real collapsible
        # group instead — see ui/main_window.py's Sidebar completion-status
        # section and MainWindow._on_completion_filter_changed(), which
        # calls set_filter_state()...with_completion(value) the same way
        # this combo used to. COMPLETION_FILTER_OPTIONS stays defined below
        # since the sidebar reuses it for its own item list.

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search games…")
        self.search_box.setFixedWidth(220)
        self.search_box.textChanged.connect(self._on_search)
        header_layout.addWidget(self.search_box)

        # Notifications bell — next to the refresh button, per spec.
        self.notification_bell = NotificationBell()
        self.notification_bell.clicked.connect(self._on_notification_bell_clicked)
        header_layout.addWidget(self.notification_bell)
        self._notification_panel = NotificationPanel()
        self._notification_panel.notification_clicked.connect(self._on_notification_row_clicked)
        self._notification_panel.state_changed.connect(self.refresh_notification_badge)
        self.refresh_notification_badge()

        self.refresh_btn = SpinButton()
        self.refresh_btn.setFixedSize(36, 36)
        self.refresh_btn.setToolTip("Refresh library from NAS")
        self.refresh_btn.clicked.connect(self._on_refresh)
        header_layout.addWidget(self.refresh_btn)
        root.addWidget(header)

        # ── Selection bar — Multi-Select & Bulk Tile Actions ────────────────
        # Hidden whenever nothing is selected; shown the moment multi-select
        # is entered. "Actions ▾" opens the same bulk menu a right-click on
        # a selected tile does — a mouse-only, non-context-menu way to reach
        # bulk actions, and the natural home for the "how do I act on my
        # selection without right-clicking" question the feature planning
        # left open.
        self.selection_bar = QWidget()
        self.selection_bar.setFixedHeight(40)
        self.selection_bar.setStyleSheet(f"""
            background: rgba(232,199,106,0.08);
            border-bottom: 1px solid rgba(232,199,106,0.25);
        """)
        sel_l = QHBoxLayout(self.selection_bar)
        sel_l.setContentsMargins(28, 0, 28, 0)
        sel_l.setSpacing(10)

        self.selection_count_lbl = QLabel("0 selected")
        self.selection_count_lbl.setFont(QFont("DM Sans", 11, QFont.Weight.Medium))
        self.selection_count_lbl.setStyleSheet(
            f"color: {COLORS['accent']}; background: transparent; border: none;")
        sel_l.addWidget(self.selection_count_lbl)
        sel_l.addStretch()

        self.selection_actions_btn = QPushButton("Actions ▾")
        self.selection_actions_btn.setFont(QFont("DM Sans", 10))
        self.selection_actions_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.selection_actions_btn.clicked.connect(self._on_selection_actions_clicked)
        sel_l.addWidget(self.selection_actions_btn)

        self.selection_clear_btn = QPushButton("Clear Selection")
        self.selection_clear_btn.setFont(QFont("DM Sans", 10))
        self.selection_clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.selection_clear_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                border: 1px solid rgba(255,255,255,0.15);
                border-radius: 6px;
                color: {COLORS['text_muted']};
                padding: 5px 12px;
            }}
            QPushButton:hover {{ color: {COLORS['text']}; }}
        """)
        self.selection_clear_btn.clicked.connect(self._clear_selection)
        sel_l.addWidget(self.selection_clear_btn)

        self.selection_bar.hide()
        root.addWidget(self.selection_bar)

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

        # ── Scroll area + fixed-size canvas ────────────────────────────────────
        # No layout is ever installed on the canvas — tiles are positioned
        # directly with setGeometry(). The canvas is resized to its final
        # full-content size up front (see _rebuild()), so creating/removing
        # tiles as the user scrolls never changes the canvas geometry and
        # never triggers a scrollbar/viewport recalculation.
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.scroll.setStyleSheet(f"background: {COLORS['bg']}; border: none;")
        self.scroll.verticalScrollBar().valueChanged.connect(self._on_scrolled)

        self.canvas = _LibraryCanvas(self)
        self.canvas.setStyleSheet(f"background: {COLORS['bg']};")
        self.scroll.setWidget(self.canvas)

        # Empty state — its own stack page so it cleanly replaces the scroll
        # area instead of trying to overlay it inside one layout slot.
        self.empty_label = QLabel(
            "No games found.\n"
            "Configure your NAS path in Settings to get started."
        )
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setFont(QFont("DM Sans", 13))
        self.empty_label.setStyleSheet(
            f"color: {COLORS['text_muted']}; padding: 60px; background: {COLORS['bg']};"
        )

        empty_page = QWidget()
        empty_page.setStyleSheet(f"background: {COLORS['bg']};")
        empty_layout = QVBoxLayout(empty_page)
        empty_layout.setContentsMargins(0, 0, 0, 0)
        empty_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self.empty_label)
        self._empty_page = empty_page

        self._content_stack = QStackedWidget()
        self._content_stack.addWidget(self.scroll)      # index 0
        self._content_stack.addWidget(empty_page)        # index 1
        root.addWidget(self._content_stack, 1)

        self._status_timer = QTimer()
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(lambda: self.status_bar.hide())

    # ── Public API ────────────────────────────────────────────────────────────

    def load_games(self, games: list):
        self._all_games = games
        self._rebuild(reset_scroll=True)

    def apply_filter(self, key: str):
        """
        Set the Group A sidebar key.

        Accepts the same string keys as before ("all", "installed",
        "uninstalled", "cat:<folder>") plus new Group A keys ("favorites",
        "recent", "hidden"). Category keys ("cat:<folder>") also set
        filter_state.category so the two filters stay in sync.
        """
        # Multi-Select — real navigation (a different sidebar view) clears
        # any active selection, same "reset on navigation" precedent
        # elsewhere in this file. Search/completion-status narrowing within
        # the SAME view deliberately does NOT clear it — see
        # set_filter_state()'s callers.
        self._clear_selection()
        if key.startswith("cat:"):
            folder = key[4:]
            self._filter_state = FilterState(
                sidebar_key="all",
                category=folder,
                collection=None,
                completion=self._filter_state.completion,
                search=self._filter_state.search,
                tags=self._filter_state.tags,
                sort="az",
            )
        elif key.startswith("coll:"):
            collection_id = int(key[5:])
            self._filter_state = FilterState(
                sidebar_key="all",
                category=None,
                collection=collection_id,
                completion=self._filter_state.completion,
                search=self._filter_state.search,
                tags=self._filter_state.tags,
                sort="custom",
            )
            # Real display name is set by the caller (MainWindow) via
            # set_page_title() right after this call — same pattern
            # already used for categories in MainWindow._on_filter_changed().
            self._update_page_title()
            self._refresh_sort_combo()
            self._rebuild(reset_scroll=True)
            return
        else:
            self._filter_state = FilterState(
                sidebar_key=key,
                category=None,
                collection=None,
                completion=self._filter_state.completion,
                search=self._filter_state.search,
                tags=self._filter_state.tags,
                sort="az",
            )
        self._update_page_title()
        self._refresh_sort_combo()
        self._rebuild(reset_scroll=True)

    # ── Sort Library ──────────────────────────────────────────────────────

    def _sort_options_for_current_view(self) -> list:
        """Custom Order only makes sense — and only appears — while actively
        viewing a collection; never in standard library views."""
        if self._filter_state.collection is not None:
            return [("Custom Order", "custom")] + SORT_OPTIONS_STANDARD
        return list(SORT_OPTIONS_STANDARD)

    def _refresh_sort_combo(self):
        """Rebuild the sort dropdown's item list for the current view
        (adding/removing Custom Order) and sync its selection to
        filter_state.sort, without firing _on_sort_changed."""
        self.sort_combo.blockSignals(True)
        self.sort_combo.clear()
        for label, value in self._sort_options_for_current_view():
            self.sort_combo.addItem(label, value)
        idx = self.sort_combo.findData(self._filter_state.sort)
        self.sort_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.sort_combo.blockSignals(False)

    def _on_sort_changed(self, _index: int):
        value = self.sort_combo.currentData()
        if value is None or value == self._filter_state.sort:
            return
        self._filter_state = self._filter_state.with_sort(value)
        # A full rebuild recreates every visible tile via
        # _update_visible_tiles(), which already calls set_drag_enabled()
        # per tile from _collection_reorder_enabled() — so switching sort
        # in/out of "custom" picks up the correct drag-handle visibility
        # for free, no separate call needed here.
        self._rebuild(reset_scroll=True)

    @staticmethod
    def _sort_games(games: list, sort_key: str) -> list:
        """
        Apply one of SORT_OPTIONS_STANDARD's sort keys to an already-filtered
        game list. "custom" is a deliberate no-op — Custom Order for a
        collection is established earlier in _filtered_games() (Group B',
        via collection_games.sort_order) and must be left exactly as-is here.

        Games with no playtime / no last-played date always sort to the
        bottom, A→Z among themselves, for both playtime and last-played
        sorts — per spec. Ties within a sort are left in whatever relative
        order they arrived in (Python's sort is stable) — no secondary sort
        is required.
        """
        def title_key(g):
            return (_safe_get(g, "sort_key") or _safe_get(g, "title") or "").lower()

        if sort_key == "az":
            return sorted(games, key=title_key)
        if sort_key == "za":
            return sorted(games, key=title_key, reverse=True)

        if sort_key in ("playtime_desc", "playtime_asc"):
            has_time = [g for g in games if (_safe_get(g, "playtime_minutes", 0) or 0) > 0]
            no_time  = [g for g in games if not (_safe_get(g, "playtime_minutes", 0) or 0) > 0]
            has_time.sort(key=lambda g: _safe_get(g, "playtime_minutes", 0) or 0,
                          reverse=(sort_key == "playtime_desc"))
            no_time.sort(key=title_key)
            return has_time + no_time

        if sort_key in ("last_played_desc", "last_played_asc"):
            played   = [g for g in games if _safe_get(g, "last_played")]
            unplayed = [g for g in games if not _safe_get(g, "last_played")]
            played.sort(key=lambda g: _safe_get(g, "last_played") or "",
                       reverse=(sort_key == "last_played_desc"))
            unplayed.sort(key=title_key)
            return played + unplayed

        return games   # "custom" (see docstring) or anything unrecognized

    def set_filter_state(self, state: FilterState):
        """
        Replace the entire filter state at once.
        Used by future cogwheel menu / tag filter / completion filter UI.
        """
        self._filter_state = state
        self._update_page_title()
        self._refresh_sort_combo()
        self._rebuild(reset_scroll=True)

    def get_filter_state(self) -> FilterState:
        """Return the current filter state (read-only copy)."""
        return FilterState(
            sidebar_key=self._filter_state.sidebar_key,
            category=self._filter_state.category,
            collection=self._filter_state.collection,
            completion=self._filter_state.completion,
            search=self._filter_state.search,
            tags=self._filter_state.tags,
            sort=self._filter_state.sort,
        )

    def set_page_title(self, title: str):
        self.page_title.setText(title)
        self._refresh_collection_header()

    def _update_page_title(self):
        """Sync the header title with current filter state."""
        fs = self._filter_state
        if fs.category:
            # Caller is responsible for setting the display name via
            # set_page_title() after resolving from DB, as before.
            self._refresh_collection_header()
            return
        if fs.collection is not None:
            # Real collection name is set by the caller (MainWindow) via
            # set_page_title() right after apply_filter("coll:<id>") —
            # same pattern already used for categories. set_page_title()
            # already calls _refresh_collection_header(), so nothing
            # further to do here beyond leaving the current text alone
            # until that call lands.
            self._refresh_collection_header()
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
        self._refresh_collection_header()

    # ── Collections / Playlists ──────────────────────────────────────────────

    def _refresh_collection_header(self):
        """
        Show/hide the Lock B toggle and the title's click-to-rename
        affordance based on whether a collection is currently active, and
        keep every live tile's drag-to-reorder state in sync with it.
        Called after anything that could change either (filter changes,
        set_page_title(), a lock toggle from the sidebar or the header).
        """
        fs = self._filter_state
        if fs.collection is None:
            self.collection_lock_btn.hide()
            self.page_title.setCursor(Qt.CursorShape.ArrowCursor)
            self.page_title.setToolTip("")
            self._update_tiles_drag_enabled()
            return

        coll = db.get_collection(fs.collection)
        locked = bool(coll["game_order_locked"]) if coll else False
        self.collection_lock_btn.setText(
            "🔒  Order Locked" if locked else "🔓  Order Unlocked")
        self.collection_lock_btn.setToolTip(
            "Click to unlock game reordering for this collection"
            if locked else
            "Click to lock game order for this collection")
        self.collection_lock_btn.show()

        self.page_title.setCursor(Qt.CursorShape.PointingHandCursor)
        self.page_title.setToolTip("Click to rename this collection")
        self._update_tiles_drag_enabled()

    def _on_title_clicked(self):
        """Library header title — click-to-rename, only meaningful while
        actively viewing a collection (see Collections / Playlists spec's
        'Collection Title / Rename' section)."""
        collection_id = self._filter_state.collection
        if collection_id is None:
            return
        from PyQt6.QtWidgets import QInputDialog
        coll = db.get_collection(collection_id)
        current_name = coll["name"] if coll else self.page_title.text()
        name, ok = QInputDialog.getText(
            self, "Rename Collection", "Name:", text=current_name)
        name = name.strip()
        if ok and name:
            db.rename_collection(collection_id, name)
            self.set_page_title(name)
            self.collections_changed.emit()

    def _on_collection_lock_btn_clicked(self):
        collection_id = self._filter_state.collection
        if collection_id is None:
            return
        coll = db.get_collection(collection_id)
        if not coll:
            return
        db.set_collection_game_order_locked(
            collection_id, not coll["game_order_locked"])
        self._refresh_collection_header()

    def _active_collection_info(self):
        """active_collection_fn contract for GameTile — see that class's
        docstring. Returns (collection_id, name) while a collection is
        being viewed, else (None, None)."""
        fs = self._filter_state
        if fs.collection is None:
            return (None, None)
        return (fs.collection, self.page_title.text())

    def _on_collection_toggled(self, game_id: int, collection_id: int, now_in: bool):
        if now_in:
            db.add_game_to_collection(collection_id, game_id)
        else:
            db.remove_game_from_collection(collection_id, game_id)
        self.collections_changed.emit()
        if self._filter_state.collection == collection_id:
            self._rebuild(reset_scroll=False)

    def _on_collection_created_from_tile(self, game_id: int, name: str):
        db.create_collection_with_game(name, game_id)
        self.collections_changed.emit()

    def _on_removed_from_active_collection(self, game_id: int):
        collection_id = self._filter_state.collection
        if collection_id is None:
            return
        db.remove_game_from_collection(collection_id, game_id)
        self.collections_changed.emit()
        self._rebuild(reset_scroll=False)

    # ── Collections / Playlists — drag-and-drop in-collection reordering ────
    # Lock B target: reordering games WITHIN a collection. Implemented as
    # manual Qt drag-and-drop (QDrag/QMimeData) rather than a QAbstractItemView
    # drag mechanism, since the tile grid is virtualized — tiles are plain
    # child widgets positioned with setGeometry(), not a model/view — see
    # this file's module docstring. Only active while viewing a collection
    # that isn't Lock-B-locked (_collection_reorder_enabled()); everywhere
    # else tiles behave exactly as they did before this feature existed.

    def _collection_reorder_enabled(self) -> bool:
        fs = self._filter_state
        if fs.collection is None:
            return False
        if fs.sort != "custom":
            # Sort Library: drag-to-reorder only makes sense while actually
            # viewing the collection's own order. A non-custom sort is a
            # temporary view — the saved custom order underneath is left
            # untouched and reappears exactly as it was when the user
            # switches back to Custom Order (see _sort_games()'s no-op for
            # "custom" and Group B' in _filtered_games()).
            return False
        coll = db.get_collection(fs.collection)
        return bool(coll) and not bool(coll["game_order_locked"])

    def _update_tiles_drag_enabled(self):
        """Sync every currently-live tile's drag state with
        _collection_reorder_enabled() — called whenever that could have
        changed (filter switch, lock toggle) so a tile already on screen
        doesn't need to scroll out and back in to pick up the new state."""
        enabled = self._collection_reorder_enabled()
        for tile in self._tiles.values():
            tile.set_drag_enabled(enabled)

    def _drop_index_at(self, pos) -> int:
        """
        Map a canvas-local drop position to a grid index (row-major, same
        numbering as self._filtered) representing 'insert before this
        index'. Uses the horizontal midpoint of the nearest column to
        decide insert-before vs. insert-after, so dropping on the left
        half of a tile lands before it and the right half lands after —
        the usual convention for this kind of reordering.
        """
        if self._cols <= 0 or self._row_h <= 0 or not self._filtered:
            return 0
        x, y = pos.x(), pos.y()

        row = int((y - self.MARGIN_TOP) // self._row_h)
        row = max(0, min(row, max(0, self._total_rows - 1)))

        col_w = self._tile_w + self.COL_SPACING
        rel_x = x - self.MARGIN_LEFT
        col_f = (rel_x / col_w) if col_w else 0.0
        col = int(col_f)
        frac = col_f - col
        insert_col = col + (1 if frac > 0.5 else 0)
        insert_col = max(0, min(insert_col, self._cols))

        index = row * self._cols + insert_col
        return max(0, min(index, len(self._filtered)))

    def _on_canvas_drag_enter(self, event):
        if not self._collection_reorder_enabled() or not event.mimeData().hasFormat(_GAME_DRAG_MIME):
            event.ignore()
            return
        event.acceptProposedAction()

    def _on_canvas_drag_move(self, event):
        if not self._collection_reorder_enabled() or not event.mimeData().hasFormat(_GAME_DRAG_MIME):
            event.ignore()
            self._drag_insert_index = None
            self._stop_autoscroll()
            return
        event.acceptProposedAction()
        pos = event.position().toPoint()
        self._drag_insert_index = self._drop_index_at(pos)
        self._update_autoscroll(pos)
        self.canvas.update()

    def _on_canvas_drag_leave(self, event):
        self._drag_insert_index = None
        self._stop_autoscroll()
        self.canvas.update()

    def _on_canvas_drop(self, event):
        self._stop_autoscroll()
        collection_id = self._filter_state.collection

        if not self._collection_reorder_enabled() or not event.mimeData().hasFormat(_GAME_DRAG_MIME):
            event.ignore()
            self._drag_insert_index = None
            self.canvas.update()
            return

        try:
            dragged_game_id = int(bytes(event.mimeData().data(_GAME_DRAG_MIME)).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            event.ignore()
            self._drag_insert_index = None
            self.canvas.update()
            return

        current_order = [g["id"] for g in self._filtered]
        if dragged_game_id not in current_order:
            # Dropped a tile that isn't part of the currently-filtered
            # collection view at all (shouldn't normally happen — the
            # drag source is always one of these tiles) — no-op safely.
            event.ignore()
            self._drag_insert_index = None
            self.canvas.update()
            return

        target_index = self._drop_index_at(event.position().toPoint())
        source_index = current_order.index(dragged_game_id)
        current_order.remove(dragged_game_id)
        # target_index was computed against the list WITH the dragged item
        # still in it (self._filtered's own numbering). Removing it shifts
        # every later index down by one, so a target that was after the
        # dragged item's original slot needs the same shift applied here —
        # otherwise a drag-forward always lands one slot too far right.
        if target_index > source_index:
            target_index -= 1
        target_index = max(0, min(target_index, len(current_order)))
        current_order.insert(target_index, dragged_game_id)

        self._persist_collection_reorder(collection_id, current_order)

        event.acceptProposedAction()
        self._drag_insert_index = None
        self._rebuild(reset_scroll=False)

    def _persist_collection_reorder(self, collection_id: int, new_filtered_order: list):
        """
        Splice the just-reordered VISIBLE subsequence back into the
        collection's full stored order. Games excluded from view by another
        active filter (hidden, search, completion, etc.) never appear as
        drag targets, so their relative position must be left untouched —
        only the slots that held a visible game get overwritten, in the
        new order the user just dragged them into. Mirrors the same
        order-preserving-intersection idea _filtered_games() already uses
        the other direction (full order -> filtered view).
        """
        full = db.get_game_ids_in_collection(collection_id)
        visible_set = set(new_filtered_order)
        it = iter(new_filtered_order)
        result = [next(it) if gid in visible_set else gid for gid in full]
        db.reorder_games_in_collection(collection_id, result)

    def _paint_drag_indicator(self, canvas_widget):
        """Thin accent-colored insertion line at self._drag_insert_index,
        drawn by _LibraryCanvas.paintEvent(). No-op unless a drag is
        currently over the canvas."""
        if self._drag_insert_index is None or self._cols <= 0:
            return
        idx = self._drag_insert_index
        row = idx // self._cols
        col = idx % self._cols
        x = (self.MARGIN_LEFT + col * (self._tile_w + self.COL_SPACING)
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

    def _update_autoscroll(self, pos_in_canvas):
        """Start/stop/redirect the autoscroll timer based on how close the
        drag position is to the top/bottom edge of the scroll viewport —
        lets a drag reach rows currently scrolled out of view instead of
        being stuck wherever the drag started."""
        viewport = self.scroll.viewport()
        pos_in_viewport = self.canvas.mapTo(viewport, pos_in_canvas)
        y = pos_in_viewport.y()
        vp_h = viewport.height()

        if y < self._AUTOSCROLL_MARGIN:
            self._start_autoscroll(-1)
        elif y > vp_h - self._AUTOSCROLL_MARGIN:
            self._start_autoscroll(1)
        else:
            self._stop_autoscroll()

    def _start_autoscroll(self, direction: int):
        self._autoscroll_direction = direction
        if not self._autoscroll_timer.isActive():
            self._autoscroll_timer.start()

    def _stop_autoscroll(self):
        self._autoscroll_direction = 0
        self._autoscroll_timer.stop()

    def _autoscroll_tick(self):
        if self._autoscroll_direction == 0:
            self._stop_autoscroll()
            return
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.value() + self._autoscroll_direction * self._AUTOSCROLL_STEP)

    def show_status(self, message: str, timeout: int = 0):
        self.status_bar.setText(message)
        self.status_bar.show()
        self._status_timer.stop()
        if timeout:
            self._status_timer.start(timeout)

    def set_scan_running(self, running: bool):
        self._scan_running = running
        if not running:
            self.refresh_btn.stop_spin()
            self.refresh_btn.setEnabled(True)

    def stop_spin(self):
        self.refresh_btn.stop_spin()

    # ── Notifications ─────────────────────────────────────────────────────

    def refresh_notification_badge(self):
        """Sync the bell's unread-count badge with the DB. Safe to call
        anytime — cheap COUNT(*) query."""
        self.notification_bell.set_count(db.get_unread_count())

    def _on_notification_bell_clicked(self):
        panel = self._notification_panel
        pos = self.notification_bell.mapToGlobal(
            self.notification_bell.rect().bottomRight())
        pos.setX(pos.x() - panel.width())
        panel.move(pos)
        panel.show()

    def _on_notification_row_clicked(self, type_: str, game_id):
        self.notification_navigate_requested.emit(type_, game_id)

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

        # ── Group B' — collection ────────────────────────────────────────────
        # Mutually exclusive with category (sidebar picks one or the other —
        # see FilterState.with_category()/with_collection()). Membership AND
        # order both come from collection_games — the filtered intersection
        # PRESERVES the collection's relative order (e.g. saved order
        # 3-1-5-2-4, only 3/5/4 pass other filters → shown as 3-5-4). This
        # must run before the search pass below — search is a pure filter
        # and doesn't touch order, and since list.sort() is stable, ordering
        # here and filtering afterward keeps collection order intact all the
        # way to the final result; no later pass in this method re-sorts.
        if fs.collection is not None:
            ordered_ids = db.get_game_ids_in_collection(fs.collection)
            order_index = {gid: i for i, gid in enumerate(ordered_ids)}
            games = [g for g in games if g["id"] in order_index]
            games.sort(key=lambda g: order_index[g["id"]])

        # ── Group C — tags (AND logic) ────────────────────────────────────────
        if fs.tags:
            matching_ids = db.get_games_with_all_tags(list(fs.tags))
            games = [g for g in games if g["id"] in matching_ids]

        # ── Group D — completion status ───────────────────────────────────────
        if fs.completion:
            games = [g for g in games
                     if _safe_get(g, "completion_status", "unplayed") == fs.completion]

        # ── Search ────────────────────────────────────────────────────────────
        # Title is always searched. Developer/Publisher are togglable in
        # Settings → Appearance → Library → Search Scope (default: both ON).
        # OR logic across whichever fields are currently active. Tags and
        # Genres are intentionally excluded — Tags gets its own dedicated
        # search in the Tags sidebar section (see Search Scope Expansion spec).
        if fs.search:
            q = fs.search.lower()
            search_developer = db.get_setting("search_scope_developer", "true") == "true"
            search_publisher = db.get_setting("search_scope_publisher", "true") == "true"

            def _matches_search(g):
                if q in (g["title"] or g["display_name"] or "").lower():
                    return True
                if search_developer and q in (_safe_get(g, "developer") or "").lower():
                    return True
                if search_publisher and q in (_safe_get(g, "publisher") or "").lower():
                    return True
                return False

            games = [g for g in games if _matches_search(g)]

        # ── Sort Library ─────────────────────────────────────────────────────
        # Always applied last. For a collection left on Custom Order this is
        # a deliberate no-op (see _sort_games()) — the collection ordering
        # established above (Group B') is what's kept.
        games = self._sort_games(games, fs.sort)

        return games

    def _on_search(self, text: str):
        self._pending_search_text = text
        self._search_timer.start()

    def _apply_pending_search(self):
        self._filter_state = self._filter_state.with_search(self._pending_search_text)
        self._rebuild(reset_scroll=True)

    def _search_empty_message(self, term: str) -> str:
        """
        Build the "no results" message for an active search term, reflecting
        exactly which fields are currently in scope (Search Scope Expansion) —
        Title is always included; Developer/Publisher only if enabled in
        Settings → Appearance → Library → Search Scope.
        """
        fields = ["title"]
        if db.get_setting("search_scope_developer", "true") == "true":
            fields.append("developer")
        if db.get_setting("search_scope_publisher", "true") == "true":
            fields.append("publisher")

        if len(fields) == 1:
            fields_str = fields[0]
        elif len(fields) == 2:
            fields_str = f"{fields[0]} and {fields[1]}"
        else:
            fields_str = f"{', '.join(fields[:-1])}, or {fields[-1]}"

        return f"No games matching '{term}' in {fields_str}."

    # ── Virtualized grid ──────────────────────────────────────────────────────

    def _rebuild(self, reset_scroll: bool = False):
        """
        Full refilter + regeometry pass. Called on load, filter/search change,
        tile-size change, and column-count-affecting resizes.

        Computes the canvas size for the ENTIRE filtered list up front (so
        the scrollbar range is correct immediately), clears any currently
        live tiles, then populates only the tiles that fall in the current
        viewport (+ buffer).
        """
        self._filtered = self._filtered_games()

        if not self._filtered:
            self._clear_tiles()
            if self._filter_state.collection is not None:
                # Distinguish "nothing in this collection at all" from
                # "this collection has games, but none pass the other
                # active filters (search, completion, hidden, etc.)" —
                # per spec these are two different messages.
                if db.get_collection_game_count(self._filter_state.collection) == 0:
                    self.empty_label.setText("No games in this collection yet.")
                else:
                    self.empty_label.setText(
                        "No games in this collection match the current filters."
                    )
            elif self._filter_state.search:
                self.empty_label.setText(
                    self._search_empty_message(self._filter_state.search))
            else:
                self.empty_label.setText(
                    "No games found.\n"
                    "Configure your NAS path in Settings to get started."
                )
            self._content_stack.setCurrentWidget(self._empty_page)
            self._unlock_refresh()
            self.trickle_finished.emit()
            return

        self._content_stack.setCurrentWidget(self.scroll)

        tile_size   = db.get_setting("tile_size", "medium")
        self._tile_w = TILE_WIDTHS.get(tile_size, 160)
        self._tile_h = int(self._tile_w * 1.5) + 32
        self._row_h  = self._tile_h + self.ROW_SPACING

        if self._pool_tile_w is not None and self._pool_tile_w != self._tile_w:
            # Pooled tiles were built at the old tile size (GameTile locks
            # its size in __init__) — they can't be resized via _apply_game,
            # so drop them rather than risk handing out a wrong-size tile.
            for tile in self._tile_pool:
                tile.deleteLater()
            self._tile_pool.clear()
        self._pool_tile_w = self._tile_w

        if self._cover_cache_tile_w != self._tile_w:
            # Cached pixmaps are scaled/cropped for the old tile width —
            # wrong size to reuse, so drop them rather than risk showing a
            # mis-scaled cover.
            self._cover_cache.clear()
            self._cover_cache_tile_w = self._tile_w

        avail_w    = max(1, self.scroll.viewport().width()
                          - self.MARGIN_LEFT - self.MARGIN_RIGHT)
        self._cols = max(1, (avail_w + self.COL_SPACING)
                          // (self._tile_w + self.COL_SPACING))
        self._total_rows = -(-len(self._filtered) // self._cols)  # ceil div

        canvas_w = self.scroll.viewport().width()
        canvas_h = (self.MARGIN_TOP
                    + self._total_rows * self._tile_h
                    + max(0, self._total_rows - 1) * self.ROW_SPACING
                    + self.MARGIN_BOTTOM)
        self.canvas.setFixedSize(canvas_w, canvas_h)

        self._clear_tiles()
        if reset_scroll:
            self.scroll.verticalScrollBar().setValue(0)

        self._update_visible_tiles()
        self._unlock_refresh()
        self.trickle_finished.emit()

    def _clear_tiles(self):
        """
        Used on full rebuild (filter change, tile-size change, column-count
        change) where every tile position is about to be recomputed anyway.
        Sends everything to the pool rather than destroying it outright, so
        a filter switch doesn't throw away perfectly reusable widgets.
        """
        for tile in self._tiles.values():
            self._release_tile(tile)
        self._tiles.clear()

    def _release_tile(self, tile: "GameTile"):
        """Hide a tile and either pool it for reuse or destroy it if the pool is full."""
        tile.hide()
        self._tiles_by_game_id.pop(tile.game_id, None)
        if len(self._tile_pool) < self._TILE_POOL_MAX:
            self._tile_pool.append(tile)
        else:
            tile.deleteLater()

    def _on_scrolled(self, _value: int):
        if not self._filtered:
            return
        if self._scroll_update_pending:
            return
        self._scroll_update_pending = True
        QTimer.singleShot(0, self._deferred_scroll_update)

    def _deferred_scroll_update(self):
        self._scroll_update_pending = False
        if self._filtered:
            self._update_visible_tiles()

    def _tile_pos(self, index: int) -> tuple[int, int]:
        row = index // self._cols
        col = index % self._cols
        x = self.MARGIN_LEFT + col * (self._tile_w + self.COL_SPACING)
        y = self.MARGIN_TOP + row * self._row_h
        return x, y

    def _update_visible_tiles(self):
        """
        Diff the currently-live tile pool against the index range implied by
        the current scroll position (+ BUFFER_ROWS on each side). Tiles that
        fell out of range are deleted; tiles newly in range are created.
        """
        if not self._filtered or self._cols <= 0 or self._row_h <= 0:
            return

        vp_top    = self.scroll.verticalScrollBar().value()
        vp_height = self.scroll.viewport().height()
        vp_bottom = vp_top + vp_height

        first_row = max(0, (vp_top - self.MARGIN_TOP) // self._row_h - self.BUFFER_ROWS)
        last_row  = (vp_bottom - self.MARGIN_TOP) // self._row_h + self.BUFFER_ROWS
        last_row  = min(self._total_rows - 1, max(int(first_row), int(last_row)))
        first_row = min(int(first_row), last_row)

        first_index = first_row * self._cols
        last_index  = min(len(self._filtered) - 1, (last_row + 1) * self._cols - 1)

        wanted = set(range(first_index, last_index + 1)) if last_index >= first_index else set()

        # Return tiles that scrolled out of range to the pool
        for idx in list(self._tiles.keys()):
            if idx not in wanted:
                self._release_tile(self._tiles.pop(idx))

        # Populate tiles that scrolled into range — reuse a pooled tile
        # (just rebinds its content, no widget construction) when one is
        # available, only building a fresh GameTile as a last resort.
        for idx in sorted(wanted):
            if idx in self._tiles:
                continue
            game = self._filtered[idx]

            if self._tile_pool:
                tile = self._tile_pool.pop()
                tile._apply_game(game)
            else:
                tile = GameTile(game, self._tile_w, parent=self.canvas,
                                is_playing_fn=self._is_game_playing,
                                active_collection_fn=self._active_collection_info,
                                is_selected_fn=self._is_game_selected,
                                multi_select_active_fn=self._is_multi_select_active,
                                long_press_enabled_fn=self._long_press_enabled,
                                long_press_ms_fn=self._long_press_ms)
                tile.clicked.connect(self._on_tile_clicked)
                tile.long_press_triggered.connect(self._on_tile_long_press)
                tile.bulk_menu_requested.connect(self._on_tile_bulk_menu_requested)
                tile.state_changed.connect(self.game_state_changed)
                tile.track_versions.connect(self.track_versions_requested)
                tile.edit_metadata.connect(self.edit_metadata_requested)
                tile.collection_toggled.connect(self._on_collection_toggled)
                tile.collection_created.connect(self._on_collection_created_from_tile)
                tile.removed_from_active_collection.connect(
                    self._on_removed_from_active_collection)

            x, y = self._tile_pos(idx)
            tile.setGeometry(x, y, self._tile_w, self._tile_h)
            tile.set_drag_enabled(self._collection_reorder_enabled())
            tile.show()
            self._tiles[idx] = tile
            self._tiles_by_game_id[tile.game_id] = tile

            cached_pixmap = self._cover_cache_get(game["id"])
            if cached_pixmap is not None:
                tile.set_cover_pixmap(cached_pixmap)
            else:
                cover_url = _safe_get(game, "cover_url")
                if cover_url:
                    loader = ImageLoader(game["id"], cover_url)
                    loader.signals.loaded.connect(self._on_image_loaded)
                    self._pool.start(loader)

    def _cover_cache_get(self, game_id: int) -> Optional[QPixmap]:
        pix = self._cover_cache.pop(game_id, None)
        if pix is not None:
            self._cover_cache[game_id] = pix   # re-insert → most-recently-used
        return pix

    def _cover_cache_put(self, game_id: int, pixmap: QPixmap):
        self._cover_cache[game_id] = pixmap
        if len(self._cover_cache) > self._COVER_CACHE_MAX:
            # dicts preserve insertion order — the first key is the
            # least-recently-used one thanks to the re-insert in
            # _cover_cache_get() above.
            self._cover_cache.pop(next(iter(self._cover_cache)))

    def _on_image_loaded(self, game_id: int, local_path: str):
        pixmap = _build_cover_pixmap(local_path, self._tile_w)
        if pixmap is None:
            return
        self._cover_cache_put(game_id, pixmap)
        tile = self._tiles_by_game_id.get(game_id)
        if tile is not None:
            tile.set_cover_pixmap(pixmap)

    # ── Currently Playing Indicator ──────────────────────────────────────────

    def _is_game_playing(self, game_id: int) -> bool:
        return game_id == self._currently_playing_game_id

    def set_currently_playing(self, game_id: Optional[int]):
        """
        Update which game (if any) shows the "Now Playing" tile overlay.
        Only the two potentially-affected tiles (the previous one, if any,
        and the new one, if any) are touched directly here — every other
        live or pooled tile picks up the correct state automatically the
        next time it's bound via GameTile._apply_game(), so there's no
        need to walk the whole visible set on every call.
        """
        old = self._currently_playing_game_id
        if old == game_id:
            return
        self._currently_playing_game_id = game_id
        for gid in (old, game_id):
            if gid is None:
                continue
            tile = self._tiles_by_game_id.get(gid)
            if tile is not None:
                tile.update_playing_overlay(self._is_game_playing(gid))

    def refresh_tile(self, game_id: int):
        """
        Called after a single game's metadata is saved.
        If the tile is currently visible, fetches its cover art immediately.
        """
        if game_id not in self._tiles_by_game_id:
            return
        try:
            with db.get_connection() as conn:
                row = conn.execute(
                    "SELECT cover_url FROM metadata WHERE game_id=?", (game_id,)
                ).fetchone()
            if row and row["cover_url"]:
                loader = ImageLoader(game_id, row["cover_url"])
                loader.signals.loaded.connect(self._on_image_loaded)
                self._pool.start(loader)
        except Exception as e:
            log.debug("refresh_tile failed for %d: %s", game_id, e)

    # ── Multi-Select & Bulk Tile Actions ─────────────────────────────────────

    def _selection_method(self) -> str:
        return db.get_setting("selection_method", "both")

    def _long_press_enabled(self) -> bool:
        return self._selection_method() in ("long_press", "both")

    def _modifier_enabled(self) -> bool:
        return self._selection_method() in ("modifier", "both")

    def _long_press_ms(self) -> int:
        try:
            return max(50, int(db.get_setting("selection_long_press_ms", "500")))
        except (TypeError, ValueError):
            return 500

    def _configured_modifier_flag(self):
        key = db.get_setting("selection_modifier_key", "ctrl")
        return {
            "ctrl": Qt.KeyboardModifier.ControlModifier,
            "alt":  Qt.KeyboardModifier.AltModifier,
            "meta": Qt.KeyboardModifier.MetaModifier,
        }.get(key, Qt.KeyboardModifier.ControlModifier)

    def _is_multi_select_active(self) -> bool:
        return self._multi_select_active

    def _is_game_selected(self, game_id: int) -> bool:
        return game_id in self._selected_game_ids

    def _on_tile_clicked(self, game_id: int, modifiers):
        """
        Single dispatch point for every tile click, routed here instead of
        straight to game_selected — what a click MEANS depends on whether
        multi-select is active and which modifier (if any) was held.
        """
        if self._multi_select_active:
            if modifiers & Qt.KeyboardModifier.ShiftModifier:
                self._range_select(game_id)
            else:
                self._toggle_selection(game_id)
            return
        if self._modifier_enabled() and (modifiers & self._configured_modifier_flag()):
            self._enter_multi_select(game_id)
            return
        self.game_selected.emit(game_id)

    def _on_tile_long_press(self, game_id: int):
        if self._multi_select_active:
            # Already selecting — a long-press on another tile is just as
            # useful as a plain toggle-click here, so treat it the same
            # rather than no-op-ing.
            self._toggle_selection(game_id)
            return
        self._enter_multi_select(game_id)

    def _on_tile_bulk_menu_requested(self, game_id: int, global_pos):
        self._show_bulk_menu(global_pos)

    def _enter_multi_select(self, game_id: int):
        self._multi_select_active = True
        self._selected_game_ids = {game_id}
        self._last_selected_game_id = game_id
        self._sync_tile_selection_visuals()
        self._update_selection_bar()

    def _toggle_selection(self, game_id: int):
        if game_id in self._selected_game_ids:
            self._selected_game_ids.discard(game_id)
        else:
            self._selected_game_ids.add(game_id)
        self._last_selected_game_id = game_id
        if not self._selected_game_ids:
            self._exit_multi_select()
            return
        self._sync_tile_selection_visuals()
        self._update_selection_bar()

    def _range_select(self, game_id: int):
        """Shift+click while in multi-select — select every game between
        the last-interacted tile and this one, in the currently filtered
        order. Falls back to a plain toggle if there's no usable anchor
        (first click after entering multi-select some other way, or the
        anchor scrolled out of the current filtered set entirely)."""
        ids_in_order = [g["id"] for g in self._filtered]
        if (self._last_selected_game_id is None
                or self._last_selected_game_id not in ids_in_order
                or game_id not in ids_in_order):
            self._toggle_selection(game_id)
            return
        i1 = ids_in_order.index(self._last_selected_game_id)
        i2 = ids_in_order.index(game_id)
        lo, hi = min(i1, i2), max(i1, i2)
        for gid in ids_in_order[lo:hi + 1]:
            self._selected_game_ids.add(gid)
        self._last_selected_game_id = game_id
        self._sync_tile_selection_visuals()
        self._update_selection_bar()

    def _clear_selection(self):
        self._exit_multi_select()

    def _exit_multi_select(self):
        had_selection = bool(self._selected_game_ids) or self._multi_select_active
        self._multi_select_active = False
        self._selected_game_ids = set()
        self._last_selected_game_id = None
        if had_selection:
            self._sync_tile_selection_visuals()
            self._update_selection_bar()

    def _sync_tile_selection_visuals(self):
        for gid, tile in self._tiles_by_game_id.items():
            tile.set_selected(gid in self._selected_game_ids)

    def _update_selection_bar(self):
        n = len(self._selected_game_ids)
        if self._multi_select_active and n > 0:
            self.selection_count_lbl.setText(f"{n} selected")
            self.selection_bar.show()
        else:
            self.selection_bar.hide()

    def _on_selection_actions_clicked(self):
        pos = self.selection_actions_btn.mapToGlobal(
            self.selection_actions_btn.rect().bottomLeft())
        self._show_bulk_menu(pos)

    # ── Bulk actions ──────────────────────────────────────────────────────

    def _show_bulk_menu(self, global_pos):
        from PyQt6.QtWidgets import QMenu, QInputDialog

        game_ids = set(self._selected_game_ids)
        if not game_ids:
            return
        games = [g for g in self._all_games if g["id"] in game_ids]
        n = len(game_ids)

        any_not_fav    = any(not _safe_get(g, "is_favorite", 0) for g in games)
        any_fav        = any(_safe_get(g, "is_favorite", 0) for g in games)
        any_not_hidden = any(not _safe_get(g, "is_hidden", 0) for g in games)
        any_hidden     = any(_safe_get(g, "is_hidden", 0) for g in games)

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
            QMenu::item:disabled {{ color: {COLORS['text_muted']}; }}
            QMenu::separator {{ height: 1px; background: {COLORS['border']}; margin: 3px 8px; }}
        """)

        header_action = menu.addAction(f"{n} game{'s' if n != 1 else ''} selected")
        header_action.setEnabled(False)
        menu.addSeparator()

        fav_add_action    = menu.addAction("☆  Add to Favorites")    if any_not_fav    else None
        fav_remove_action = menu.addAction("★  Remove from Favorites") if any_fav       else None
        if fav_add_action or fav_remove_action:
            menu.addSeparator()

        hide_action   = menu.addAction("⊘  Hide")   if any_not_hidden else None
        unhide_action = menu.addAction("👁  Unhide") if any_hidden     else None
        if hide_action or unhide_action:
            menu.addSeparator()

        add_coll_menu = menu.addMenu("➕  Add to Collection")
        collections = db.get_collections_for_tile_menu()
        # One membership lookup per selected game (not per game × per
        # collection) — see the module's tri-state note below.
        membership_by_game = {gid: db.get_collection_ids_for_game(gid) for gid in game_ids}
        coll_actions = {}
        for c in collections:
            count = sum(1 for gid in game_ids if c["id"] in membership_by_game[gid])
            if count == 0:
                label = c["name"]
            elif count == n:
                label = "✓  " + c["name"]
            else:
                # Mixed selection — some but not all of the selected games
                # are already in this collection. Clicking brings it to
                # "all in" (adds the missing ones) rather than "all out" —
                # the same convention a tri-state checkbox usually follows.
                label = f"◐  {c['name']}  ({count}/{n})"
            act = add_coll_menu.addAction(label)
            coll_actions[act] = c["id"]
        add_coll_menu.addSeparator()
        new_coll_action = add_coll_menu.addAction("+ New Collection…")

        remove_from_active_action = None
        if self._filter_state.collection is not None:
            remove_from_active_action = menu.addAction(
                f"➖  Remove from {self.page_title.text()}")

        menu.addSeparator()
        status_menu = menu.addMenu("✓  Set Completion Status")
        status_actions = {}
        for value, label in COMPLETION_FILTER_OPTIONS[1:]:   # skip "All Statuses"
            act = status_menu.addAction(label)
            status_actions[act] = value

        menu.addSeparator()
        refresh_meta_action = menu.addAction("🔄  Refresh Metadata")

        chosen = menu.exec(global_pos)
        if chosen is None:
            return

        if chosen == fav_add_action:
            self._bulk_add_favorites(game_ids)
        elif chosen == fav_remove_action:
            self._bulk_set_favorite(game_ids, False)
            self._finish_bulk_action()
        elif chosen == hide_action:
            self._bulk_set_hidden(game_ids, True)
            self._finish_bulk_action()
        elif chosen == unhide_action:
            self._bulk_set_hidden(game_ids, False)
            self._finish_bulk_action()
        elif chosen == new_coll_action:
            name, ok = QInputDialog.getText(self, "New Collection", "Collection name:")
            name = name.strip()
            if ok and name:
                cid = db.create_collection(name)
                for gid in game_ids:
                    db.add_game_to_collection(cid, gid)
                self._finish_bulk_action(collections_touched=True)
        elif chosen in coll_actions:
            cid = coll_actions[chosen]
            count = sum(1 for gid in game_ids if cid in membership_by_game[gid])
            if count == n:
                for gid in game_ids:
                    db.remove_game_from_collection(cid, gid)
            else:
                for gid in game_ids:
                    db.add_game_to_collection(cid, gid)
            self._finish_bulk_action(collections_touched=True)
        elif remove_from_active_action is not None and chosen == remove_from_active_action:
            cid = self._filter_state.collection
            for gid in game_ids:
                db.remove_game_from_collection(cid, gid)
            self._finish_bulk_action(collections_touched=True)
        elif chosen in status_actions:
            status = status_actions[chosen]
            for gid in game_ids:
                db.set_completion_status(gid, status)
            self._finish_bulk_action()
        elif chosen == refresh_meta_action:
            self.bulk_metadata_refresh_requested.emit(list(game_ids))
            self._clear_selection()

    def _bulk_add_favorites(self, game_ids: set):
        """
        Favorites and hidden are mutually exclusive (db.set_favorite()
        auto-unhides). Bulk-favoriting a selection that includes hidden
        games would silently unhide them, so — unlike bulk Hide, which
        already silently unfavorites elsewhere in the app with no warning —
        this direction gets a confirmation naming exactly how many hidden
        games are affected.
        """
        from PyQt6.QtWidgets import QMessageBox
        games = [g for g in self._all_games if g["id"] in game_ids]
        hidden_count = sum(1 for g in games if _safe_get(g, "is_hidden", 0))
        if hidden_count:
            reply = QMessageBox.question(
                self, "Add to Favorites",
                f"{hidden_count} hidden game{'s' if hidden_count != 1 else ''} in your "
                "selection will be automatically unhidden in order to be favorited. "
                "Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            if reply != QMessageBox.StandardButton.Yes:
                return
        self._bulk_set_favorite(game_ids, True)
        self._finish_bulk_action()

    def _bulk_set_favorite(self, game_ids, value: bool):
        for gid in game_ids:
            db.set_favorite(gid, value)

    def _bulk_set_hidden(self, game_ids, value: bool):
        for gid in game_ids:
            db.set_hidden(gid, value)

    def _finish_bulk_action(self, collections_touched: bool = False):
        """
        Shared wrap-up for every bulk action except Refresh Metadata (which
        has its own async completion path — see bulk_metadata_refresh_requested).
        Clears the selection (a "select, act, done" workflow — see the
        feature's implementation notes) and tells MainWindow to reload.
        game_state_changed's game_id argument is ignored by MainWindow's
        handler (it just calls _load_library() unconditionally), so 0 is a
        safe filler here — same signal every single-tile favorite/hide
        action already reuses.
        """
        self._clear_selection()
        self.game_state_changed.emit(0)
        if collections_touched:
            self.collections_changed.emit()

    # ── Resize handling ───────────────────────────────────────────────────────

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._all_games:
            self._resize_timer.start()

    def _on_resize_settled(self):
        if not self._all_games or not self._filtered:
            return

        tile_size = db.get_setting("tile_size", "medium")
        tile_w    = TILE_WIDTHS.get(tile_size, 160)
        avail_w   = max(1, self.scroll.viewport().width()
                         - self.MARGIN_LEFT - self.MARGIN_RIGHT)
        new_cols  = max(1, (avail_w + self.COL_SPACING) // (tile_w + self.COL_SPACING))

        if new_cols == self._cols and tile_w == self._tile_w:
            # Column count and tile size unchanged (e.g. just the scrollbar
            # gutter appearing/disappearing) — only the canvas width needs
            # to track the viewport; tile positions stay exactly as they
            # are, no rebuild needed.
            self.canvas.setFixedSize(self.scroll.viewport().width(), self.canvas.height())
            return

        # Column count or tile size genuinely changed — full regeometry.
        # This is cheap under virtualization (only visible tiles get
        # recreated), unlike the old trickle renderer where this had to be
        # guarded against with a width-delta threshold to avoid flicker.
        self._rebuild(reset_scroll=False)

    # ── Refresh button ────────────────────────────────────────────────────────

    def _unlock_refresh(self):
        if not self._scan_running:
            self.refresh_btn.setEnabled(True)
            self.refresh_btn.stop_spin()

    def _on_refresh(self):
        nas_path = db.get_setting("nas_path", "")
        self.refresh_btn.start_spin()
        self.refresh_requested.emit(nas_path)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_cover_pixmap(local_path: str, tile_w: int) -> Optional[QPixmap]:
    """
    Load, scale, and center-crop a cover image file into a QPixmap sized for
    a tile_w-wide grid tile. Pulled out as a standalone function (rather than
    living inside GameTile) so LibraryView can build it once and cache it by
    game_id — a tile scrolled back into view reuses the cached pixmap
    directly instead of re-decoding and re-scaling the source image file.
    Returns None if the file can't be loaded as an image.
    """
    pix = QPixmap(local_path)
    if pix.isNull():
        return None
    th = int(tile_w * 1.5)
    scaled = pix.scaled(
        tile_w, th,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation
    )
    x = (scaled.width()  - tile_w) // 2
    y = (scaled.height() - th) // 2
    return scaled.copy(x, y, tile_w, th)


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
