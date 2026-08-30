"""
ui/main_window.py — VaultPlay main application window

Hosts:
  - Left sidebar (filters, NAS status)
  - Stacked content area (Library, Game Detail, Settings)
  - Background worker threads for scanning and metadata fetching
  - Refresh / scan controls
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

import datetime
import logging
import time
from pathlib import Path
from typing import Optional

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QLabel, QPushButton, QStackedWidget, QSizePolicy,
    QFrame, QApplication, QMessageBox, QLineEdit
)
from PyQt6.QtCore import Qt, QThread, QThreadPool, QTimer, pyqtSignal, QSize
from PyQt6.QtGui import QFont, QIcon, QPixmap, QColor

import db
import scanner
import metadata as meta_mod
import protondb as protondb_mod
import version_checker as vc_mod
import version_check
import playtime as playtime_mod
import save_backup
import update_check

from ui.library_view import (
    LibraryView, ImageLoader as _CoverImageLoader, COMPLETION_FILTER_OPTIONS
)
from ui.setup_wizard import SetupWizard
from ui.game_detail import GameDetailView
from ui.settings_view import SettingsView
from ui.save_backup_dialog import SaveBackupDialog
from ui.style import STYLESHEET, COLORS

log = logging.getLogger(__name__)


# ── Background workers ────────────────────────────────────────────────────────

class ScanWorker(QThread):
    progress     = pyqtSignal(int, int, str)
    finished     = pyqtSignal(dict)

    def __init__(self, nas_path: str):
        super().__init__()
        self.nas_path = nas_path

    def run(self):
        import time
        log.info("[PHASE] ScanWorker.run() — %s", self.nas_path)
        t0 = time.monotonic()
        result = scanner.scan_nas(
            self.nas_path,
            progress_callback=lambda cur, tot, name: self.progress.emit(cur, tot, name)
        )
        log.info("[SCAN WORKER] Finished in %.1f s — %s",
                 time.monotonic() - t0, result)
        self.finished.emit(result)


class MetadataWorker(QThread):
    progress     = pyqtSignal(int, int, str)
    finished     = pyqtSignal(int)
    game_updated = pyqtSignal(int)   # game_id — emitted after each game's metadata saved

    def run(self):
        import time
        log.info("[PHASE] MetadataWorker.run()")
        t0 = time.monotonic()
        count = meta_mod.fetch_all_missing(
            progress_callback=lambda cur, tot, name: self.progress.emit(cur, tot, name),
            game_done_callback=lambda gid: self.game_updated.emit(gid),
        )
        log.info("[META WORKER] Finished in %.1f s — %d games updated",
                 time.monotonic() - t0, count)
        self.finished.emit(count)


class BulkMetadataWorker(QThread):
    """
    Multi-Select & Bulk Tile Actions — "Refresh Metadata" on a specific set
    of selected games. Unlike MetadataWorker above (which only ever
    processes games MISSING metadata, via meta_mod.fetch_all_missing()),
    this always re-fetches every game it's given, selected or not — the
    whole point of choosing this action is "these already have data, but
    I want it refreshed."
    """
    progress     = pyqtSignal(int, int, str)
    finished     = pyqtSignal(int)
    game_updated = pyqtSignal(int)   # game_id — emitted after each game's metadata saved

    def __init__(self, game_ids: list):
        super().__init__()
        self._game_ids = list(game_ids)

    def run(self):
        sgdb_key    = db.get_setting("sgdb_api_key", "")
        igdb_id_key = db.get_setting("igdb_client_id", "")
        igdb_secret = db.get_setting("igdb_client_secret", "")
        style_pref  = db.get_setting("sgdb_art_style", "alternate")

        if not sgdb_key:
            log.info("[BULK METADATA] No SGDB API key — skipping")
            self.finished.emit(0)
            return

        total = len(self._game_ids)
        count = 0
        log.info("[BULK METADATA] Starting refresh for %d selected game(s)", total)

        for i, game_id in enumerate(self._game_ids):
            game = db.get_game(game_id)
            display = (game["title"] or game["display_name"] or game["folder_name"]) \
                      if game else f"game_{game_id}"
            self.progress.emit(i + 1, total, display)
            try:
                ok = meta_mod.fetch_metadata_for_game(
                    game_id, sgdb_key, igdb_id_key, igdb_secret, style_pref)
                if ok:
                    count += 1
                    self.game_updated.emit(game_id)
            except Exception as e:
                log.error("[BULK METADATA] %s failed: %s", display, e)
            # Same rate-limit spirit as MetadataWorker's underlying
            # fetch_all_missing() — be polite to APIs between requests.
            time.sleep(0.4)

        log.info("[BULK METADATA] Complete: %d/%d succeeded", count, total)
        self.finished.emit(count)


class ProtonDBWorker(QThread):
    progress = pyqtSignal(str)
    finished = pyqtSignal(int)   # count updated
    # Notifications — protondb_change: emitted whenever a game's tier after
    # this fetch differs from what it was immediately before. Old tier is
    # captured per-game right before its own fetch_and_store() call, so this
    # correctly reflects real transitions rather than "None → Gold" for a
    # game that simply never had data before (this worker only processes
    # games from get_games_missing_protondb(), so in practice a genuine
    # transition here is rare — the manual "Refresh ProtonDB Data" flow in
    # settings_view.py is where real tier changes are actually observed —
    # but the comparison is correct either way and costs nothing extra).
    tier_changed = pyqtSignal(int, str, str)   # game_id, old_tier, new_tier

    def run(self):
        import time as _time
        import db as _db

        # Fetch counts.json once, then process all games in a batch.
        # counts.json is a global file — one fetch gives the salt values
        # used to compute internal hashes for all games.
        games = _db.get_games_missing_protondb()
        if not games:
            self.finished.emit(0)
            return

        game_ids = [g["id"] for g in games]
        self.progress.emit(
            f"Fetching ProtonDB data for {len(game_ids)} games…")

        # Fetch counts once for the whole batch
        counts = protondb_mod.fetch_counts()
        if not counts:
            self.progress.emit(
                "⚠ Could not fetch ProtonDB counts.json — "
                "will use cached hashes only")

        count = 0
        for i, game in enumerate(games):
            self.progress.emit(
                f"Fetching ProtonDB data… {i+1}/{len(game_ids)} — "
                f"{game['display_name'] or ''}")
            old_tier = None
            try:
                old_tier = game["protondb_tier"]
            except (IndexError, KeyError):
                pass
            result = protondb_mod.fetch_and_store(game["id"], counts=counts)
            if result:
                count += 1
                new_tier = result.get("tier")
                if old_tier and new_tier and old_tier != new_tier:
                    self.tier_changed.emit(game["id"], old_tier, new_tier)
            _time.sleep(0.05)
        self.finished.emit(count)


class UpdateCheckWorker(QThread):
    """
    Silent background check against GitHub on every launch — see
    AppImage Self-Update Check. Runs update_check.fetch_latest_release() off
    the UI thread and hands the raw result (or None on any failure) back to
    MainWindow._on_update_check_done(), which decides what to do with it.
    Deliberately has no progress signal — this is a single fire-and-forget
    network call, not a multi-step operation.
    """
    finished = pyqtSignal(object)   # dict | None — see update_check.fetch_latest_release()

    def run(self):
        self.finished.emit(update_check.fetch_latest_release())


# ── Currently Playing Indicator (sidebar widget) ──────────────────────────────

class PlayingIndicator(QWidget):
    """
    Sidebar widget shown only while a game is running — cover thumbnail,
    "Playing" label, game title, and a live HH:MM:SS timer. Completely
    hidden (no placeholder, no reserved space) when nothing is playing.
    Clicking it navigates to that game's detail page.

    Driven entirely by MainWindow, which owns the actual PlaytimeWatcher/
    running-game state — this widget just renders whatever it's told via
    show_playing()/hide_playing()/set_cover_pixmap().
    """
    clicked = pyqtSignal(int)   # game_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self._game_id: Optional[int] = None
        self._started_at: Optional[datetime.datetime] = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(f"""
            QWidget {{
                background: rgba(232,199,106,0.08);
                border: 1px solid rgba(232,199,106,0.35);
                border-radius: 8px;
            }}
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        self.cover_label = QLabel()
        self.cover_label.setFixedSize(34, 48)
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_label.setStyleSheet(
            f"background: {COLORS['surface3']}; border-radius: 4px; border: none;")
        layout.addWidget(self.cover_label)

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        text_col.setContentsMargins(0, 0, 0, 0)

        playing_lbl = QLabel("▶  PLAYING")
        playing_lbl.setFont(QFont("DM Mono", 7))
        playing_lbl.setStyleSheet(
            f"color: {COLORS['accent']}; letter-spacing: 1.5px; background: transparent; border: none;")
        text_col.addWidget(playing_lbl)

        self.title_lbl = QLabel("")
        self.title_lbl.setFont(QFont("DM Sans", 10, QFont.Weight.Medium))
        self.title_lbl.setStyleSheet(
            f"color: {COLORS['text']}; background: transparent; border: none;")
        text_col.addWidget(self.title_lbl)

        self.timer_lbl = QLabel("00:00:00")
        self.timer_lbl.setFont(QFont("DM Mono", 9))
        self.timer_lbl.setStyleSheet(
            f"color: {COLORS['text_muted']}; background: transparent; border: none;")
        text_col.addWidget(self.timer_lbl)

        layout.addLayout(text_col, 1)

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._tick)

        self.hide()

    def show_playing(self, game_id: int, title: str, started_at: datetime.datetime):
        self._game_id = game_id
        self._started_at = started_at
        elided = self.title_lbl.fontMetrics().elidedText(
            title, Qt.TextElideMode.ElideRight, 120)
        self.title_lbl.setText(elided)
        self.title_lbl.setToolTip(title)
        self.cover_label.setPixmap(QPixmap())
        self._tick()
        self._tick_timer.start()
        self.show()

    def hide_playing(self):
        self._tick_timer.stop()
        self._game_id = None
        self._started_at = None
        self.hide()

    def set_cover_pixmap(self, pixmap: QPixmap):
        scaled = pixmap.scaled(
            34, 48, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation)
        self.cover_label.setPixmap(scaled)

    def current_game_id(self) -> Optional[int]:
        return self._game_id

    def _tick(self):
        if not self._started_at:
            return
        elapsed = datetime.datetime.utcnow() - self._started_at
        total = max(0, int(elapsed.total_seconds()))
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        self.timer_lbl.setText(f"{h:02d}:{m:02d}:{s:02d}")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._game_id is not None:
            self.clicked.emit(self._game_id)
        super().mousePressEvent(event)


# ── Sidebar ───────────────────────────────────────────────────────────────────

class SidebarItem(QWidget):
    clicked = pyqtSignal(str)  # emits the filter key

    def __init__(self, label: str, key: str, count: int = 0, parent=None):
        super().__init__(parent)
        self.key     = key
        self._active = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(8)

        self.dot = QLabel("●")
        self.dot.setFixedWidth(12)
        self.dot.setFont(QFont("monospace", 7))

        self.label_w = QLabel(label)
        self.label_w.setFont(QFont("DM Sans", 10))

        self.count_w = QLabel(str(count) if count else "")
        self.count_w.setFont(QFont("DM Mono", 9))
        self.count_w.setAlignment(Qt.AlignmentFlag.AlignRight)

        layout.addWidget(self.dot)
        layout.addWidget(self.label_w, 1)
        layout.addWidget(self.count_w)

        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_style()

    @property
    def active(self):
        return self._active

    @active.setter
    def active(self, val: bool):
        self._active = val
        self._update_style()

    def _update_style(self):
        if self._active:
            self.setStyleSheet(f"""
                QWidget {{ background: {COLORS['surface3']}; border-radius: 6px; }}
            """)
            self.label_w.setStyleSheet(f"color: {COLORS['accent']};")
            self.dot.setStyleSheet(f"color: {COLORS['accent']};")
            self.count_w.setStyleSheet(f"color: {COLORS['accent']}; opacity: 0.7;")
        else:
            self.setStyleSheet("QWidget { background: transparent; border-radius: 6px; }")
            self.label_w.setStyleSheet(f"color: {COLORS['text_dim']};")
            self.dot.setStyleSheet(f"color: {COLORS['text_muted']};")
            self.count_w.setStyleSheet(f"color: {COLORS['text_muted']};")

    def update_count(self, count: int):
        self.count_w.setText(str(count) if count else "")

    def mousePressEvent(self, event):
        self.clicked.emit(self.key)


class TagChipItem(QWidget):
    """One toggleable tag row in the sidebar's Tags group — unlike
    SidebarItem (single-select Library/Category filters), any number of
    these can be active at once (Group C, AND logic — see Tags spec)."""
    toggled = pyqtSignal(int, bool)   # tag_id, now_active

    def __init__(self, tag_id: int, name: str, count: int, parent=None):
        super().__init__(parent)
        self.tag_id = tag_id
        self._active = False
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(8)
        self.label_w = QLabel(name)
        self.label_w.setFont(QFont("DM Sans", 10))
        self.count_w = QLabel(str(count) if count else "")
        self.count_w.setFont(QFont("DM Mono", 9))
        self.count_w.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self.label_w, 1)
        layout.addWidget(self.count_w)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_style()

    def _update_style(self):
        if self._active:
            self.setStyleSheet(f"background: {COLORS['surface3']}; border-radius: 6px;")
            self.label_w.setStyleSheet(f"color: {COLORS['accent']};")
            self.count_w.setStyleSheet(f"color: {COLORS['accent']};")
        else:
            self.setStyleSheet("background: transparent; border-radius: 6px;")
            self.label_w.setStyleSheet(f"color: {COLORS['text_dim']};")
            self.count_w.setStyleSheet(f"color: {COLORS['text_muted']};")

    def set_active(self, val: bool):
        self._active = val
        self._update_style()

    def mousePressEvent(self, event):
        self._active = not self._active
        self._update_style()
        self.toggled.emit(self.tag_id, self._active)


class CollapsibleSectionHeader(QWidget):
    """
    Clickable section header for a collapsible sidebar group (Collapsible
    Sidebar Groups feature). Shows a chevron (⌄ expanded / › collapsed) plus
    the group title. Purely a display + click-toggle widget — Sidebar owns
    the actual content-widget show/hide and settings persistence, since the
    same header shape is reused for every group (Library, Categories, Tags,
    Completion Status).
    """
    toggled = pyqtSignal(bool)   # now_collapsed

    def __init__(self, title: str, collapsed: bool = False, parent=None):
        super().__init__(parent)
        self._collapsed = collapsed
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 6, 16, 4)
        layout.setSpacing(6)

        self.chevron = QLabel()
        self.chevron.setFixedWidth(12)
        self.chevron.setFont(QFont("DM Sans", 9))
        self.chevron.setStyleSheet(f"color: {COLORS['text_muted']}; background: transparent;")
        layout.addWidget(self.chevron)

        self.title_lbl = QLabel(title.upper())
        self.title_lbl.setFont(QFont("DM Mono", 8))
        self.title_lbl.setStyleSheet(
            f"color: {COLORS['text_muted']}; letter-spacing: 2px; background: transparent;")
        layout.addWidget(self.title_lbl, 1)

        self._update_chevron()

    def _update_chevron(self):
        self.chevron.setText("›" if self._collapsed else "⌄")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._collapsed = not self._collapsed
            self._update_chevron()
            self.toggled.emit(self._collapsed)
        super().mousePressEvent(event)


class Sidebar(QWidget):
    filter_changed = pyqtSignal(str)
    tag_filter_changed = pyqtSignal(object)   # frozenset[int]
    # Collapsible Sidebar Groups / Completion Status — emits the selected
    # completion value ("unplayed"/"in_progress"/"completed"/"abandoned")
    # or None for "All Statuses". Independent AND filter (Group D), same as
    # the header dropdown it replaces — never exclusive with Group A/B.
    completion_filter_changed = pyqtSignal(object)
    settings_requested = pyqtSignal()
    # Collections / Playlists
    collection_selected              = pyqtSignal(int, str)  # collection_id, name
    new_collection_requested         = pyqtSignal()
    collection_rename_requested      = pyqtSignal(int, str)  # collection_id, new_name
    collection_delete_requested      = pyqtSignal(int, str)  # collection_id, name
    collection_lock_toggle_requested = pyqtSignal(int)       # collection_id
    collection_move_requested        = pyqtSignal(int, int)  # collection_id, direction (-1/+1)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(220)
        self.setStyleSheet(f"background: {COLORS['surface']}; border-right: 1px solid {COLORS['border']};")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.setSpacing(0)

        # App title
        title_container = QWidget()
        title_layout = QHBoxLayout(title_container)
        title_layout.setContentsMargins(16, 0, 16, 16)
        self.title_label = QLabel("⬡  VAULTPLAY")
        self.title_label.setFont(QFont("Rajdhani", 16, QFont.Weight.Bold))
        self.title_label.setStyleSheet(f"color: {COLORS['accent']}; letter-spacing: 2px;")
        title_layout.addWidget(self.title_label)
        layout.addWidget(title_container)

        # Library section — Collapsible Sidebar Groups
        self.library_container = QWidget()
        self.library_layout = QVBoxLayout(self.library_container)
        self.library_layout.setContentsMargins(0, 0, 0, 0)
        self.library_layout.setSpacing(0)
        self.items: dict[str, SidebarItem] = {}
        self._add_item(self.library_layout, "All Games",       "all",         0)
        self._add_item(self.library_layout, "Installed",        "installed",   0)
        self._add_item(self.library_layout, "Not Installed",    "uninstalled", 0)
        self._add_item(self.library_layout, "★  Favorites",     "favorites",   0)
        self._add_item(self.library_layout, "🕐  Recently Added","recent",      0)
        self._add_item(self.library_layout, "⊘  Hidden",        "hidden",      0)
        self._add_collapsible_group(layout, "Library", "library", self.library_container)

        # Genre section — Collapsible Sidebar Groups
        self.genre_container = QWidget()
        self.genre_layout = QVBoxLayout(self.genre_container)
        self.genre_layout.setContentsMargins(0, 0, 0, 0)
        self.genre_layout.setSpacing(0)
        self._add_collapsible_group(layout, "Categories", "categories", self.genre_container)

        # Collections section — same static (non-collapsible) style as
        # Categories above. A truly collapsible group is spec'd but depends
        # on the not-yet-built Collapsible Sidebar Groups feature; this
        # mirrors Categories' existing non-collapsible section instead of
        # guessing at that feature's eventual shape.
        self.collections_label = self._add_section_label(layout, "Collections")
        self.collections_container = QWidget()
        self.collections_layout = QVBoxLayout(self.collections_container)
        self.collections_layout.setContentsMargins(0, 0, 0, 0)
        self.collections_layout.setSpacing(0)
        layout.addWidget(self.collections_container)

        new_collection_container = QWidget()
        nc_layout = QHBoxLayout(new_collection_container)
        nc_layout.setContentsMargins(16, 4, 16, 8)
        new_collection_btn = QPushButton("+ New Collection")
        new_collection_btn.setFont(QFont("DM Sans", 10))
        new_collection_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                border: 1px dashed rgba(255,255,255,0.15);
                border-radius: 6px;
                color: {COLORS['text_muted']};
                padding: 5px 8px;
                text-align: left;
            }}
            QPushButton:hover {{
                border-color: {COLORS['accent']};
                color: {COLORS['accent']};
            }}
        """)
        new_collection_btn.clicked.connect(self._on_new_collection_clicked)
        nc_layout.addWidget(new_collection_btn)
        layout.addWidget(new_collection_container)

        # Tags section — Group C filter (AND logic), wrapped as a
        # collapsible group (Collapsible Sidebar Groups). Search box lives
        # INSIDE the collapsible container along with the chip list, per
        # spec — collapsing the group hides the search box too, but its
        # typed text is preserved (just a QLineEdit under a hidden parent,
        # never destroyed) and restored on expand.
        self.tags_group_container = QWidget()
        tgc_layout = QVBoxLayout(self.tags_group_container)
        tgc_layout.setContentsMargins(0, 0, 0, 0)
        tgc_layout.setSpacing(0)

        self.tags_search = QLineEdit()
        self.tags_search.setPlaceholderText("Filter tags…")
        self.tags_search.setFont(QFont("DM Sans", 9))
        self.tags_search.textChanged.connect(self._on_tags_search_changed)
        tags_search_container = QWidget()
        tsc_l = QHBoxLayout(tags_search_container)
        tsc_l.setContentsMargins(16, 2, 16, 4)
        tsc_l.addWidget(self.tags_search)
        tgc_layout.addWidget(tags_search_container)

        self.tags_container = QWidget()
        self.tags_layout = QVBoxLayout(self.tags_container)
        self.tags_layout.setContentsMargins(0, 0, 0, 0)
        self.tags_layout.setSpacing(0)
        tgc_layout.addWidget(self.tags_container)

        self._tag_items: dict[int, TagChipItem] = {}
        self._active_tag_ids: set = set()

        self._add_collapsible_group(layout, "Tags", "tags", self.tags_group_container)

        # Completion Status section — Collapsible Sidebar Groups, resolved
        # 2026-08-28 as option (b): migrated into the sidebar as a real
        # group (replacing library_view.py's header dropdown), matching
        # this feature's original spec rather than leaving Completion
        # Status as a header-only control. Independent AND filter (Group D)
        # — clicking one of these never touches Group A/B/C selection.
        self.completion_container = QWidget()
        self.completion_layout = QVBoxLayout(self.completion_container)
        self.completion_layout.setContentsMargins(0, 0, 0, 0)
        self.completion_layout.setSpacing(0)
        self._completion_items: dict[str, tuple] = {}   # key -> (SidebarItem, value)
        self._active_completion_value = None
        for label, value in COMPLETION_FILTER_OPTIONS:
            key = f"status:{value or 'all'}"
            item = SidebarItem(label, key, 0)
            item.clicked.connect(self._on_completion_item_clicked)
            self._completion_items[key] = (item, value)
            container = QWidget()
            cl = QHBoxLayout(container)
            cl.setContentsMargins(8, 0, 8, 0)
            cl.addWidget(item)
            self.completion_layout.addWidget(container)
        # "All Statuses" active by default — matches the dropdown's old default.
        self._completion_items["status:all"][0].active = True
        self._add_collapsible_group(
            layout, "Completion Status", "completion", self.completion_container)

        layout.addStretch()

        # Currently Playing Indicator — always present in the layout, but
        # hidden (no reserved space beyond its own margins) unless a game
        # is running. See PlayingIndicator and MainWindow._set_currently_playing.
        self.playing_indicator = PlayingIndicator()
        pi_container = QWidget()
        pi_layout = QHBoxLayout(pi_container)
        pi_layout.setContentsMargins(8, 0, 8, 8)
        pi_layout.addWidget(self.playing_indicator)
        layout.addWidget(pi_container)

        # Status bar at bottom
        status_frame = QFrame()
        status_frame.setStyleSheet(f"border-top: 1px solid {COLORS['border']}; background: transparent;")
        status_layout = QVBoxLayout(status_frame)
        status_layout.setContentsMargins(16, 12, 16, 12)
        status_layout.setSpacing(4)

        self.nas_status = QLabel("● NAS: Not connected")
        self.nas_status.setFont(QFont("DM Mono", 9))
        self.nas_status.setStyleSheet(f"color: {COLORS['text_muted']}; border: none;")
        status_layout.addWidget(self.nas_status)

        settings_btn = QPushButton("⚙  Settings")
        settings_btn.setFont(QFont("DM Sans", 10))
        settings_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                border: 1px solid {COLORS['border']};
                border-radius: 6px;
                color: {COLORS['text_muted']};
                padding: 6px 10px;
                text-align: left;
            }}
            QPushButton:hover {{
                background: {COLORS['surface2']};
                color: {COLORS['text']};
            }}
        """)
        settings_btn.clicked.connect(self.settings_requested)
        status_layout.addWidget(settings_btn)

        layout.addWidget(status_frame)

        # Activate 'all' by default
        self.items["all"].active = True
        self._current = "all"

    def _add_collapsible_group(self, layout, title: str, setting_key: str,
                               content_widget: QWidget) -> CollapsibleSectionHeader:
        """
        Wrap content_widget in a collapsible group: a clickable chevron
        header above it. Collapse state is read from (and persisted to)
        settings key f"sidebar_{setting_key}_collapsed" — all groups start
        expanded ("false") by default, per Collapsible Sidebar Groups spec.
        """
        collapsed = db.get_setting(f"sidebar_{setting_key}_collapsed", "false") == "true"
        header = CollapsibleSectionHeader(title, collapsed=collapsed)
        content_widget.setVisible(not collapsed)

        def _on_toggle(now_collapsed: bool, key=setting_key, cw=content_widget):
            cw.setVisible(not now_collapsed)
            db.set_setting(f"sidebar_{key}_collapsed", "true" if now_collapsed else "false")

        header.toggled.connect(_on_toggle)
        layout.addWidget(header)
        layout.addWidget(content_widget)
        return header

    def _add_section_label(self, layout, text):
        lbl = QLabel(text.upper())
        lbl.setFont(QFont("DM Mono", 8))
        lbl.setStyleSheet(f"color: {COLORS['text_muted']}; letter-spacing: 2px; padding: 4px 16px 4px 16px;")
        layout.addWidget(lbl)
        return lbl

    def _add_item(self, layout, label, key, count):
        item = SidebarItem(label, key, count)
        item.clicked.connect(self._on_item_clicked)
        self.items[key] = item
        container = QWidget()
        cl = QHBoxLayout(container)
        cl.setContentsMargins(8, 0, 8, 0)
        cl.addWidget(item)
        layout.addWidget(container)

    def _on_item_clicked(self, key: str):
        for k, item in self.items.items():
            item.active = (k == key)
        self._current = key
        self.filter_changed.emit(key)

    def update_counts(self, all_count, installed, uninstalled, cat_counts: dict,
                      favorites: int = 0, hidden: int = 0):
        """cat_counts: {folder_name: (display_name, count)}"""
        self.items["all"].update_count(all_count)
        self.items["installed"].update_count(installed)
        self.items["uninstalled"].update_count(uninstalled)

        # Update favorites/hidden counts if those items exist
        if "favorites" in self.items:
            self.items["favorites"].update_count(favorites)
        if "hidden" in self.items:
            self.items["hidden"].update_count(hidden)

        # Rebuild category items
        for i in reversed(range(self.genre_layout.count())):
            w = self.genre_layout.itemAt(i).widget()
            if w:
                w.deleteLater()
        # Remove old category keys
        for k in list(self.items.keys()):
            if k.startswith("cat:"):
                del self.items[k]

        for folder, (display, count) in cat_counts.items():
            key = f"cat:{folder}"
            item = SidebarItem(display, key, count)
            item.clicked.connect(self._on_item_clicked)
            self.items[key] = item
            container = QWidget()
            cl = QHBoxLayout(container)
            cl.setContentsMargins(8, 0, 8, 0)
            cl.addWidget(item)
            self.genre_layout.addWidget(container)

    # ── Tags ──────────────────────────────────────────────────────────────

    def _on_tags_search_changed(self, text: str):
        q = text.strip().lower()
        for item in self._tag_items.values():
            item.parentWidget().setVisible(q in item.label_w.text().lower())

    def update_tags(self, tags: list):
        """tags: list of sqlite3.Row from db.get_all_tags() (id/name/usage_count)."""
        for i in reversed(range(self.tags_layout.count())):
            w = self.tags_layout.itemAt(i).widget()
            if w:
                w.deleteLater()
        self._tag_items.clear()

        if not tags:
            empty = QLabel("No tags yet")
            empty.setFont(QFont("DM Sans", 9))
            empty.setStyleSheet(f"color: {COLORS['text_muted']}; padding: 2px 16px;")
            self.tags_layout.addWidget(empty)
            return

        for tag in tags:
            item = TagChipItem(tag["id"], tag["name"], tag["usage_count"])
            item.set_active(tag["id"] in self._active_tag_ids)
            item.toggled.connect(self._on_tag_toggled)
            container = QWidget()
            cl = QHBoxLayout(container)
            cl.setContentsMargins(8, 0, 8, 0)
            cl.addWidget(item)
            self.tags_layout.addWidget(container)
            self._tag_items[tag["id"]] = item

    def _on_tag_toggled(self, tag_id: int, now_active: bool):
        if now_active:
            self._active_tag_ids.add(tag_id)
        else:
            self._active_tag_ids.discard(tag_id)
        self.tag_filter_changed.emit(frozenset(self._active_tag_ids))

    # ── Completion Status (Collapsible Sidebar Groups) ────────────────────

    def _on_completion_item_clicked(self, key: str):
        """
        Single-select within the Completion Status group only — deliberately
        does NOT touch self.items (Group A/B/B' selection), since completion
        status is an independent AND filter (Group D), same relationship the
        header dropdown it replaces always had.
        """
        entry = self._completion_items.get(key)
        if not entry:
            return
        value = entry[1]
        for item, _v in self._completion_items.values():
            item.active = (item is entry[0])
        self._active_completion_value = value
        self.completion_filter_changed.emit(value)

    # ── Collections / Playlists ──────────────────────────────────────────────

    def _on_new_collection_clicked(self):
        self.new_collection_requested.emit()

    def _on_collection_item_clicked(self, key: str, collection_id: int, name: str):
        """Mirrors _on_item_clicked()'s 'deactivate everyone else, activate
        the clicked one' behavior, so selecting a collection correctly
        un-highlights whatever Library/Category item was active before —
        collection rows aren't wired through _on_item_clicked itself since
        they need to emit collection_selected (id + name), not a bare
        filter key string."""
        for k, item in self.items.items():
            item.active = (k == key)
        self._current = key
        self.collection_selected.emit(collection_id, name)

    def update_collections(self, collections: list, active_collection_id=None):
        """
        collections: list of sqlite3.Row from db.get_collections(), each
        with .id/.name/.sort_order/.game_order_locked. Rebuilds the sidebar
        Collections section from scratch — cheap, small list, same
        rebuild-on-every-change approach the Categories section already uses.
        """
        for i in reversed(range(self.collections_layout.count())):
            w = self.collections_layout.itemAt(i).widget()
            if w:
                w.deleteLater()
        for k in list(self.items.keys()):
            if k.startswith("coll:"):
                del self.items[k]

        if not collections:
            empty = QLabel("No collections yet")
            empty.setFont(QFont("DM Sans", 9))
            empty.setStyleSheet(f"color: {COLORS['text_muted']}; padding: 2px 16px;")
            self.collections_layout.addWidget(empty)
            return

        for i, coll in enumerate(collections):
            row = self._build_collection_row(
                coll, i, len(collections),
                active=(coll["id"] == active_collection_id))
            self.collections_layout.addWidget(row)

    def _build_collection_row(self, coll, index: int, total: int, active: bool) -> QWidget:
        key = f"coll:{coll['id']}"
        count = db.get_collection_game_count(coll["id"])
        item = SidebarItem(coll["name"], key, count)
        item.active = active
        item.clicked.connect(
            lambda _k, cid=coll["id"], name=coll["name"], key=key:
                self._on_collection_item_clicked(key, cid, name))
        self.items[key] = item

        container = QWidget()
        cl = QHBoxLayout(container)
        cl.setContentsMargins(8, 0, 8, 0)
        cl.setSpacing(2)
        cl.addWidget(item, 1)

        item.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        item.customContextMenuRequested.connect(
            lambda _pos, c=coll: self._show_collection_menu(c))

        return container

    def _show_collection_menu(self, coll):
        from PyQt6.QtWidgets import QMenu, QInputDialog
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
        rename_action = menu.addAction("Rename")
        lock_label = ("Unlock Game Order" if coll["game_order_locked"]
                      else "Lock Game Order")
        lock_action = menu.addAction(lock_label)
        menu.addSeparator()
        up_action = menu.addAction("↑ Move Up")
        down_action = menu.addAction("↓ Move Down")
        menu.addSeparator()
        delete_action = menu.addAction("Delete")

        chosen = menu.exec(self.cursor().pos())
        if chosen == rename_action:
            name, ok = QInputDialog.getText(
                self, "Rename Collection", "Name:", text=coll["name"])
            name = name.strip()
            if ok and name:
                self.collection_rename_requested.emit(coll["id"], name)
        elif chosen == lock_action:
            self.collection_lock_toggle_requested.emit(coll["id"])
        elif chosen == up_action:
            self.collection_move_requested.emit(coll["id"], -1)
        elif chosen == down_action:
            self.collection_move_requested.emit(coll["id"], 1)
        elif chosen == delete_action:
            self.collection_delete_requested.emit(coll["id"], coll["name"])

    def set_nas_connected(self, connected: bool, address: str = ""):
        if connected:
            self.nas_status.setText(f"● NAS · {address}")
            self.nas_status.setStyleSheet(f"color: {COLORS['installed']}; border: none;")
        else:
            self.nas_status.setText("○ NAS: Not connected")
            self.nas_status.setStyleSheet(f"color: {COLORS['text_muted']}; border: none;")


# ── Main Window ───────────────────────────────────────────────────────────────

def _safe_get(row, key, default=None):
    """Safely get a value from a sqlite3.Row, returning default if key missing."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        db.init_db()

        self.setWindowTitle("VaultPlay")
        self.setMinimumSize(1100, 700)
        self.resize(1280, 800)

        self.setStyleSheet(STYLESHEET)

        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QHBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # Sidebar
        self.sidebar = Sidebar()
        self.sidebar.filter_changed.connect(self._on_filter_changed)
        self.sidebar.settings_requested.connect(self._show_settings)
        self.sidebar.collection_selected.connect(self._on_collection_selected)
        self.sidebar.new_collection_requested.connect(self._on_new_collection_requested)
        self.sidebar.collection_rename_requested.connect(self._on_collection_rename_requested)
        self.sidebar.collection_delete_requested.connect(self._on_collection_delete_requested)
        self.sidebar.collection_lock_toggle_requested.connect(
            self._on_collection_lock_toggle_requested)
        self.sidebar.collection_move_requested.connect(self._on_collection_move_requested)
        self.sidebar.tag_filter_changed.connect(self._on_tag_filter_changed)
        self.sidebar.completion_filter_changed.connect(self._on_completion_filter_changed)
        root_layout.addWidget(self.sidebar)

        # Content stack
        self.stack = QStackedWidget()
        root_layout.addWidget(self.stack, 1)

        # Views
        self.library_view = LibraryView()
        self.library_view.game_selected.connect(self._show_game_detail)
        self.library_view.refresh_requested.connect(self._start_scan)
        # When the trickle finishes, run any pending scan
        self.library_view.trickle_finished.connect(self._on_trickle_finished)
        # When a tile context menu changes favorite/hidden state, reload library
        self.library_view.game_state_changed.connect(self._on_game_state_changed)
        # When a tile context menu requests version tracking dialog
        self.library_view.track_versions_requested.connect(
            self._open_version_tracker_dialog)
        # When a tile context menu requests the Edit Metadata dialog
        self.library_view.edit_metadata_requested.connect(
            self._open_edit_metadata_dialog)
        # Notifications — dropdown panel row clicked, dispatch navigation
        self.library_view.notification_navigate_requested.connect(
            self._on_notification_navigate)
        # Collections / Playlists — membership changed from inside a tile's
        # context menu (toggle/create/remove-from-active); refresh sidebar
        # badges to match.
        self.library_view.collections_changed.connect(self._refresh_collections_sidebar)
        # Multi-Select & Bulk Tile Actions — "Refresh Metadata" bulk action.
        self.library_view.bulk_metadata_refresh_requested.connect(
            self._start_bulk_metadata_refresh)

        self.detail_view = GameDetailView()
        self.detail_view.back_requested.connect(self._show_library)
        self.detail_view.install_finished.connect(self._on_install_finished)
        self.detail_view.collections_changed.connect(self._refresh_collections_sidebar)

        self.settings_view = SettingsView()
        self.settings_view.back_requested.connect(self._show_library)
        self.settings_view.nas_path_changed.connect(self._on_nas_path_changed)
        self.settings_view.rescan_requested.connect(self._on_rescan_requested)
        self.settings_view.reload_requested.connect(self._load_library)

        self.stack.addWidget(self.library_view)   # index 0
        self.stack.addWidget(self.detail_view)    # index 1
        self.stack.addWidget(self.settings_view)  # index 2

        # Workers
        self._scan_worker:   ScanWorker    | None = None
        self._meta_worker:   MetadataWorker | None = None
        self._proton_worker: ProtonDBWorker | None = None
        self._update_check_worker: UpdateCheckWorker | None = None
        self._bulk_meta_worker: BulkMetadataWorker | None = None

        # Version tracking workers — ONE shared running guard across all four
        # entry points (auto-track, recheck, check-all-now, backfill).
        self._version_worker: QThread | None = None
        self._version_worker_running: bool   = False

        # Playtime watchers — keyed by game_id so we never double-track
        self._playtime_watchers: dict[int, playtime_mod.PlaytimeWatcher] = {}

        # Currently Playing Indicator — single "live" game tracked across
        # sidebar/tile-overlay/launch-button/Force Quit. Multiple watchers
        # can technically be running at once (each still recorded to DB
        # correctly via _playtime_watchers above), but the UI surfaces are
        # all spec'd around one game at a time — see _set_currently_playing.
        self._currently_playing_game_id: Optional[int] = None
        self._pool = QThreadPool.globalInstance()

        # Connect game_launched from detail view
        self.detail_view.game_launched.connect(self._on_game_launched)
        self.detail_view.force_quit_requested.connect(self._on_force_quit_requested)
        self.sidebar.playing_indicator.clicked.connect(self._show_game_detail)

        # Recurring version check timer (persisted across restarts)
        from PyQt6.QtCore import QTimer as _QTimer
        self._version_check_timer = _QTimer(self)
        self._version_check_timer.setSingleShot(True)
        self._version_check_timer.timeout.connect(self._on_version_check_timer)

        # Pending scan: if a scan is requested while the trickle is running,
        # store its args here and fire it when trickle_finished fires.
        self._pending_scan: dict | None = None   # {"nas_path": str, "clear_first": bool}
        # IDs of games found in the most recent scan, for auto-track pass
        self._pending_new_game_ids: list = []

        # Show setup wizard on first run
        if db.get_setting("first_run_complete", "false") == "false":
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(200, self._show_setup_wizard)
        else:
            self._initial_load()

    # ── Views ─────────────────────────────────────────────────────────────────

    def _show_setup_wizard(self):
        wizard = SetupWizard(self)
        wizard.setup_complete.connect(self._on_wizard_complete)
        wizard.exec()
        # Reload settings view so it reflects whatever the wizard saved
        self.settings_view.load_settings()
        self._initial_load()

    def _on_wizard_complete(self, nas_path: str):
        log.info("Setup wizard complete. NAS path: %s", nas_path)
        import os
        self.sidebar.set_nas_connected(os.path.exists(nas_path), nas_path)
        self._start_scan(nas_path, clear_first=True)

    def _initial_load(self):
        """
        Load the library from DB (fast lightweight query + trickle render),
        then — once the trickle finishes — run the startup scan if enabled.
        The scan is stored as pending here and consumed by _on_trickle_finished.
        """
        self._load_library()

        # Collections / Playlists — populate the sidebar's Collections
        # section on startup. Independent of the games-library load above
        # (badge counts are computed straight from DB), but grouped here
        # since this is the one-time startup sequence.
        self._refresh_collections_sidebar()

        # Currently Playing Indicator — restart-reattachment. Must happen
        # after the library is loaded (needs db.get_all_games()) but
        # doesn't depend on the scan below, so it's safe to run regardless
        # of whether a scan is about to be queued.
        self._reattach_running_games()

        nas_path = db.get_setting("nas_path", "")
        if nas_path and nas_path.strip() not in ("", "/"):
            self.sidebar.set_nas_connected(True, nas_path)
            if db.get_setting("scan_on_launch", "true") == "true":
                # Don't start the scan now — queue it for after the trickle
                self._pending_scan = {"nas_path": nas_path, "clear_first": False}
                self.library_view.show_status("Loading library…")
        else:
            self.library_view.show_status(
                "Go to Settings → NAS Connection to configure your library.",
                timeout=0
            )

        # Arm the persisted version check timer now that the app is loaded
        self._arm_version_check_timer()

        # AppImage Self-Update Check — silent background check, every launch.
        self._start_update_check()

    def _on_trickle_finished(self):
        """
        Called when LibraryView finishes adding all tiles to the grid.
        If a scan was queued while the trickle was running, start it now.
        """
        if self._pending_scan is not None:
            args = self._pending_scan
            self._pending_scan = None
            self._start_scan(args["nas_path"], args["clear_first"])

    def _on_rescan_requested(self, path: str):
        if path == "__metadata__":
            log.info("Manual metadata fetch requested")
            self._start_metadata_fetch()
        elif path and path.strip() not in ("", "/"):
            self._start_scan(path, clear_first=False)

    def _on_nas_path_changed(self, path: str):
        """Called when NAS path Apply is clicked or blacklist toggled."""
        if path == "__reload__":
            self._load_library()
        else:
            import os
            if os.path.exists(path):
                self.sidebar.set_nas_connected(True, path)
            else:
                self.sidebar.set_nas_connected(False, path)
            self._start_scan(path, clear_first=True)

    def _show_library(self):
        self._load_library()
        self.stack.setCurrentIndex(0)

    def _show_game_detail(self, game_id: int):
        self.detail_view.load_game(game_id)
        self.stack.setCurrentIndex(1)

    def _show_settings(self):
        self.settings_view.load_settings()
        self.stack.setCurrentIndex(2)

    # ── Library loading ───────────────────────────────────────────────────────

    def _load_library(self):
        """
        Pull the minimal tile data from DB and hand it to the library view,
        which will trickle tiles in one per event-loop tick.
        """
        games = db.get_games_for_library()

        # Counts for sidebar badges.
        # all_count excludes hidden games (they only appear in the Hidden view).
        hidden      = sum(1 for g in games if _safe_get(g, "is_hidden", 0))
        visible     = [g for g in games if not _safe_get(g, "is_hidden", 0)]
        all_count   = len(visible)
        installed   = sum(1 for g in visible if g["is_installed"])
        uninstalled = all_count - installed
        favorites   = sum(1 for g in visible if _safe_get(g, "is_favorite", 0))

        categories = db.get_categories()
        cat_counts = {}
        for cat in categories:
            folder  = cat["folder_name"]
            display = cat["display_name"]
            count   = sum(1 for g in visible if _safe_get(g, "category") == folder)
            if count > 0:
                cat_counts[folder] = (display, count)

        self.sidebar.update_counts(all_count, installed, uninstalled,
                                   cat_counts, favorites=favorites, hidden=hidden)
        self.sidebar.update_tags(db.get_all_tags())
        self.library_view.load_games(games)

    def _on_filter_changed(self, key: str):
        self.library_view.apply_filter(key)
        if key.startswith("cat:"):
            # Set the page title to the category's display name (not the raw folder name)
            cats = db.get_categories()
            for c in cats:
                if c["folder_name"] == key[4:]:
                    self.library_view.set_page_title(c["display_name"])
                    return
            # Fallback to folder name if display name not found
            self.library_view.set_page_title(key[4:])

    def _on_tag_filter_changed(self, tag_ids):
        self.library_view.set_filter_state(
            self.library_view.get_filter_state().with_tags(tag_ids))

    def _on_completion_filter_changed(self, value):
        self.library_view.set_filter_state(
            self.library_view.get_filter_state().with_completion(value))

    # ── Collections / Playlists ────────────────────────────────────────────────

    def _refresh_collections_sidebar(self):
        active = self.library_view.get_filter_state().collection
        self.sidebar.update_collections(db.get_collections(), active_collection_id=active)

    def _on_collection_selected(self, collection_id: int, name: str):
        self.library_view.apply_filter(f"coll:{collection_id}")
        self.library_view.set_page_title(name)
        self._refresh_collections_sidebar()

    def _on_new_collection_requested(self):
        from PyQt6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "New Collection", "Collection name:")
        name = name.strip()
        if ok and name:
            db.create_collection(name)
            self._refresh_collections_sidebar()

    def _on_collection_rename_requested(self, collection_id: int, name: str):
        db.rename_collection(collection_id, name)
        self._refresh_collections_sidebar()
        fs = self.library_view.get_filter_state()
        if fs.collection == collection_id:
            self.library_view.set_page_title(name)

    def _on_collection_delete_requested(self, collection_id: int, name: str):
        reply = QMessageBox.question(
            self, "Delete Collection",
            f"Delete '{name}'? Games will not be removed from your library.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if reply != QMessageBox.StandardButton.Yes:
            return
        db.delete_collection(collection_id)
        fs = self.library_view.get_filter_state()
        if fs.collection == collection_id:
            self.sidebar._on_item_clicked("all")
        self._refresh_collections_sidebar()

    def _on_collection_lock_toggle_requested(self, collection_id: int):
        coll = db.get_collection(collection_id)
        if not coll:
            return
        db.set_collection_game_order_locked(
            collection_id, not coll["game_order_locked"])
        self._refresh_collections_sidebar()
        # If the library header's Lock B button is showing this same
        # collection right now, refresh it too so a lock toggled from the
        # sidebar's right-click menu is immediately reflected there as well.
        fs = self.library_view.get_filter_state()
        if fs.collection == collection_id:
            self.library_view._refresh_collection_header()

    def _on_collection_move_requested(self, collection_id: int, direction: int):
        colls = list(db.get_collections())
        ids = [c["id"] for c in colls]
        idx = ids.index(collection_id) if collection_id in ids else -1
        new_idx = idx + direction
        if idx < 0 or not (0 <= new_idx < len(ids)):
            return
        ids[idx], ids[new_idx] = ids[new_idx], ids[idx]
        db.reorder_collections(ids)
        self._refresh_collections_sidebar()

    # ── Scanning ──────────────────────────────────────────────────────────────

    def _start_scan(self, nas_path: str = "", clear_first: bool = False):
        if not nas_path:
            nas_path = db.get_setting("nas_path", "")
        if not nas_path or nas_path.strip() in ("", "/"):
            self.library_view.show_status(
                "NAS path not configured. Go to Settings → NAS Connection.")
            return

        if self._scan_worker and self._scan_worker.isRunning():
            return  # already running

        if clear_first:
            self.library_view.show_status("NAS path changed — clearing library…")
            db.clear_all_games()
            self._load_library()

        # Tell the library view a scan is running → keeps refresh btn locked
        self.library_view.set_scan_running(True)
        self.library_view.show_status("Scanning NAS…")

        self._scan_worker = ScanWorker(nas_path)
        self._scan_worker.progress.connect(self._on_scan_progress)
        self._scan_worker.finished.connect(self._on_scan_done)
        self._scan_worker.start()

    def _on_scan_progress(self, current: int, total: int, name: str):
        self.library_view.show_status(f"Scanning… {current}/{total} — {name}")

    def _on_scan_done(self, result: dict):
        new    = result.get("new", 0)
        total  = result.get("total", 0)
        errors = result.get("errors", [])

        msg = f"Scan complete — {total} games found"
        if new:
            msg += f", {new} new"
        if errors:
            msg += f", {len(errors)} error(s)"

        self._load_library()
        self.library_view.show_status(msg, timeout=4000)
        self.sidebar.set_nas_connected(True, db.get_setting("nas_path", ""))
        self.library_view.set_scan_running(False)
        self.library_view.stop_spin()

        # Notifications — scan_summary: always a fresh row, only when the
        # scan actually found new games (an all-updated/no-op scan doesn't
        # warrant a notification). corrupted_archive: one per game whose
        # archive couldn't be read this pass (deduped against any unread
        # one already showing for that game — see db.add_notification()).
        if new:
            db.add_notification(
                "scan_summary", "Scan Complete",
                f"Scan found {new} new game{'s' if new != 1 else ''}.",
                game_id=None, dedup=False)
        for corrupted in result.get("corrupted_games", []):
            db.add_notification(
                "corrupted_archive", "Corrupted Archive",
                f"Archive could not be read — may be corrupted. "
                f"Check: {corrupted['nas_path']}",
                game_id=corrupted["game_id"], dedup=True)
        if new or result.get("corrupted_games"):
            self.library_view.refresh_notification_badge()

        # Update last scan label in settings if visible
        try:
            if hasattr(self.settings_view, "_last_scan_desc_lbl"):
                self.settings_view._last_scan_desc_lbl.setText(
                    db.get_setting("last_scan_result", "Never scanned")
                )
        except Exception:
            pass

        if new and not (self._meta_worker and self._meta_worker.isRunning()):
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(2000, self._start_metadata_fetch)

        # Fire auto-track pass for new games, independent of the metadata chain.
        # Stored here so the pass can be deferred if a version worker is already
        # running — it will be consumed by _on_version_worker_done().
        new_game_ids = result.get("new_game_ids", [])
        if new_game_ids:
            self._pending_new_game_ids = new_game_ids
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(1000, self._start_auto_track)

    def _start_metadata_fetch(self):
        if self._meta_worker and self._meta_worker.isRunning():
            return
        self._meta_worker = MetadataWorker()
        self._meta_worker.progress.connect(self._on_meta_progress)
        self._meta_worker.game_updated.connect(self._on_game_metadata_updated)
        self._meta_worker.finished.connect(self._on_meta_done)
        self._meta_worker.start()

    def _on_game_metadata_updated(self, game_id: int):
        """Called after each individual game's metadata is saved.
        Updates the tile's cover art immediately without a full library reload."""
        self.library_view.refresh_tile(game_id)

    def _on_meta_progress(self, current: int, total: int, name: str):
        self.library_view.show_status(f"Fetching metadata… {current}/{total} — {name}")

    def _on_meta_done(self, count: int):
        log.info("Metadata fetch complete: %d games updated", count)
        self._load_library()
        if count:
            self.library_view.show_status(
                f"✓ Metadata updated for {count} game(s)", timeout=5000)
        else:
            self.library_view.show_status(
                "Metadata fetch complete — no new data found", timeout=4000)
        try:
            if hasattr(self.settings_view, "fetch_meta_btn"):
                self.settings_view.fetch_meta_btn.setEnabled(True)
                self.settings_view.fetch_meta_btn.setText("Fetch Now")
                self.settings_view.meta_status_lbl.setText(
                    f"✓ Done — {count} game(s) updated")
                self.settings_view.meta_status_lbl.setStyleSheet("color: #4ade80;")
        except Exception:
            pass

        # Chain ProtonDB fetch after metadata completes
        if db.get_setting("protondb_auto_fetch", "true") == "true":
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(1000, self._start_protondb_fetch)

    # ── Multi-Select & Bulk Tile Actions — Refresh Metadata ──────────────────

    def _start_bulk_metadata_refresh(self, game_ids: list):
        if not game_ids:
            return
        if not db.get_setting("sgdb_api_key", ""):
            self.library_view.show_status(
                "⚠ No SteamGridDB API key configured — go to Settings → API Keys.",
                timeout=5000)
            return
        if self._bulk_meta_worker and self._bulk_meta_worker.isRunning():
            self.library_view.show_status(
                "A metadata refresh is already running…", timeout=3000)
            return

        self.library_view.show_status(f"Refreshing metadata for {len(game_ids)} game(s)…")
        self._bulk_meta_worker = BulkMetadataWorker(game_ids)
        self._bulk_meta_worker.progress.connect(self._on_bulk_meta_progress)
        self._bulk_meta_worker.game_updated.connect(self.library_view.refresh_tile)
        self._bulk_meta_worker.finished.connect(self._on_bulk_meta_done)
        self._bulk_meta_worker.start()
        log.info("BulkMetadataWorker started for %d game(s)", len(game_ids))

    def _on_bulk_meta_progress(self, current: int, total: int, name: str):
        self.library_view.show_status(f"Refreshing metadata… {current}/{total} — {name}")

    def _on_bulk_meta_done(self, count: int):
        self._load_library()
        if count:
            self.library_view.show_status(
                f"✓ Metadata refreshed for {count} game(s)", timeout=4000)
        else:
            self.library_view.show_status(
                "Metadata refresh complete — no data found", timeout=4000)

    def _start_protondb_fetch(self):
        if self._proton_worker and self._proton_worker.isRunning():
            return
        self.library_view.show_status("Fetching ProtonDB compatibility data…")
        self._proton_worker = ProtonDBWorker()
        self._proton_worker.progress.connect(self._on_proton_progress)
        self._proton_worker.tier_changed.connect(self._on_protondb_tier_changed)
        self._proton_worker.finished.connect(self._on_proton_done)
        self._proton_worker.start()

    def _on_proton_progress(self, message: str):
        self.library_view.show_status(message)

    def _on_protondb_tier_changed(self, game_id: int, old_tier: str, new_tier: str):
        """Notifications — protondb_change. Message must say FROM and TO
        per spec ("Changed from Silver to Gold"), never just "tier changed"."""
        db.add_notification(
            "protondb_change", "ProtonDB Tier Changed",
            f"Changed from {old_tier.title()} to {new_tier.title()}",
            game_id=game_id, dedup=True)
        self.library_view.refresh_notification_badge()

    def _on_proton_done(self, count: int):
        if count:
            self._load_library()
            self.library_view.show_status(
                f"✓ ProtonDB compatibility fetched for {count} game(s)", timeout=3000
            )
        else:
            self.library_view.show_status(
                "ProtonDB fetch complete — no new data", timeout=3000
            )

    def _on_install_finished(self, game_id: int):
        self._load_library()

    def _on_game_state_changed(self, game_id: int):
        """
        Called when a tile context menu changes favorite or hidden state.
        Reloads the library so sidebar counts and tile visibility update
        immediately without requiring a full NAS rescan.
        """
        self._load_library()

    def _on_notification_navigate(self, type_: str, game_id):
        """
        Notifications dropdown panel — a row was clicked. Dispatch per type,
        per spec:
          version_update      → Installed filter for now. MUST be updated to
                                 navigate to the future "Update Available"
                                 Group A filter instead, if that ever gets
                                 built — the spec calls this connection out
                                 explicitly, don't forget it.
          scan_summary         → Recently Added filter (spec allows this
                                 since the filter already exists in this app).
          app_update            → Settings → About (AppImage Self-Update Check).
          protondb_change / save_backup_failed / corrupted_archive
                                → game detail page for game_id.
        Routes through Sidebar._on_item_clicked() rather than calling
        library_view.apply_filter() directly, so the sidebar's own active-item
        highlighting stays in sync with wherever this navigation lands.
        """
        if type_ == "version_update":
            self.stack.setCurrentIndex(0)
            self.sidebar._on_item_clicked("installed")
        elif type_ == "scan_summary":
            self.stack.setCurrentIndex(0)
            self.sidebar._on_item_clicked("recent")
        elif type_ == "app_update":
            self._show_settings()
            self.settings_view.show_about()
        elif type_ in ("protondb_change", "save_backup_failed", "corrupted_archive") and game_id is not None:
            self._show_game_detail(game_id)

    def _open_version_tracker_dialog(self, game_id: int):
        """Open the VersionTrackerDialog for a specific game."""
        from ui.version_tracker_dialog import VersionTrackerDialog
        dlg = VersionTrackerDialog(game_id, parent=self)
        dlg.versions_updated.connect(self._on_versions_updated)
        dlg.exec()

    def _on_versions_updated(self, game_id: int):
        """
        Called when VersionTrackerDialog writes new version data.
        If the game detail view is currently showing this game, reload it
        so the version rows in the info card update immediately.
        """
        if (self.stack.currentIndex() == 1 and
                self.detail_view._game_id == game_id):
            self.detail_view.load_game(game_id)

    def _open_edit_metadata_dialog(self, game_id: int):
        """Open the EditMetadataDialog for a specific game (tile context menu entry point)."""
        from ui.edit_metadata_dialog import EditMetadataDialog
        dlg = EditMetadataDialog(game_id, parent=self)
        dlg.saved.connect(self._on_metadata_saved)
        dlg.exec()

    def _on_metadata_saved(self, game_id: int):
        """
        Called when EditMetadataDialog writes new metadata. Reloads the
        library (tiles show title/cover art/sort order) and, if the game
        detail view is currently showing this game, reloads it too — same
        pattern _on_versions_updated() above uses.
        """
        self._load_library()
        if (self.stack.currentIndex() == 1 and
                self.detail_view._game_id == game_id):
            self.detail_view.load_game(game_id)

    # ── Version checking ──────────────────────────────────────────────────────

    def _version_worker_busy(self) -> bool:
        """Return True if any version worker is currently running."""
        return self._version_worker_running

    def _on_version_worker_done(self, found: int, checked: int, errors: int):
        """
        Shared completion handler for all three version workers.
        Clears the running guard, shows a status message, re-arms the timer,
        and consumes any pending auto-track pass that was deferred.
        """
        # Notifications — version_update: per spec, this fires specifically
        # after VersionCheckWorker (the recurring/manual "Check All Now"
        # pass over every existing tracker), not the auto-track or backfill
        # workers — those only ever create NEW trackers, they don't
        # re-confirm whether an already-tracked game now has a newer
        # version than what's on the NAS. Checked before the shared
        # cleanup below clears self._version_worker.
        if isinstance(self._version_worker, vc_mod.VersionCheckWorker):
            try:
                game_ids = self._compute_games_with_version_updates()
                db.upsert_version_update_notification(game_ids)
                self.library_view.refresh_notification_badge()
            except Exception as e:
                log.warning("Notifications: version_update check failed: %s", e)

        self._version_worker_running = False
        self._version_worker = None

        if checked > 0:
            msg = f"✓ Version check done — {found} updated"
            if errors:
                msg += f", {errors} error(s)"
            self.library_view.show_status(msg, timeout=4000)

        # Notify settings page if it's watching
        try:
            if hasattr(self.settings_view, "_on_version_check_done"):
                self.settings_view._on_version_check_done(found, checked, errors)
        except Exception:
            pass

        # Re-arm recurring timer for the next cycle
        self._arm_version_check_timer()

        # Run any deferred auto-track pass now that the worker slot is free
        if self._pending_new_game_ids:
            ids = self._pending_new_game_ids
            self._pending_new_game_ids = []
            self._start_auto_track(ids)

    def _compute_games_with_version_updates(self) -> list:
        """
        Notifications — version_update: "tracker_version > nas_version for
        any game." Compares each tracked game's best-known tracker version
        (db.get_best_versions_for_game — the highest value across all of
        that game's trackers) against the base game's own NAS-detected
        version (db.get_nas_version), per format bucket (dotted/plain/date
        are never compared cross-bucket — same rule version_check.py and
        db.py already follow everywhere else). A game only counts if both
        sides have a value in the SAME bucket to compare — an unknown NAS
        version isn't treated as "behind".
        Returns a sorted list of game_ids with a genuine update available.
        """
        game_ids = set()
        try:
            tracked_game_ids = {t["game_id"] for t in db.get_all_trackers()}
        except Exception:
            return []
        for game_id in tracked_game_ids:
            try:
                best = db.get_best_versions_for_game(game_id)
                nas  = db.get_nas_version(game_id)
                if best.get("dotted") and nas.get("dotted") and \
                        version_check.sort_key(best["dotted"]) > version_check.sort_key(nas["dotted"]):
                    game_ids.add(game_id)
                    continue
                if best.get("plain") and nas.get("plain") and \
                        version_check.sort_key(best["plain"]) > version_check.sort_key(nas["plain"]):
                    game_ids.add(game_id)
                    continue
                if best.get("date") and nas.get("date") and \
                        version_check.date_sort_key(best["date"]) > version_check.date_sort_key(nas["date"]):
                    game_ids.add(game_id)
                    continue
            except Exception:
                continue
        return sorted(game_ids)

    def _start_auto_track(self, game_ids: list = None):
        """
        Start the auto-track pass for new games.
        If game_ids is None, consumes self._pending_new_game_ids.
        No-ops if a version worker is already running (stores ids for later).
        """
        if game_ids is None:
            game_ids = self._pending_new_game_ids
            self._pending_new_game_ids = []

        if not game_ids:
            return

        if self._version_worker_busy():
            # Defer — _on_version_worker_done will consume pending ids
            self._pending_new_game_ids = game_ids
            log.debug("_start_auto_track: version worker busy, deferring %d ids",
                      len(game_ids))
            return

        self._version_worker_running = True
        worker = vc_mod.VersionAutoTrackWorker(game_ids)
        worker.progress.connect(self._on_version_progress)
        worker.finished.connect(self._on_version_worker_done)
        worker.finished.connect(lambda f, c, e: worker.deleteLater())
        self._version_worker = worker
        worker.start()
        log.info("VersionAutoTrackWorker started for %d new games", len(game_ids))

    def _start_version_check(self, manual: bool = False):
        """
        Start the recurring background recheck (or manual Check All Now).
        No-ops if a version worker is already running.
        """
        if self._version_worker_busy():
            if manual:
                self.library_view.show_status(
                    "Version check already running…", timeout=3000)
            return

        self._version_worker_running = True
        self.library_view.show_status("Checking for version updates…")
        worker = vc_mod.VersionCheckWorker()
        worker.progress.connect(self._on_version_progress)
        worker.finished.connect(self._on_version_worker_done)
        worker.finished.connect(lambda f, c, e: worker.deleteLater())
        self._version_worker = worker
        worker.start()
        log.info("VersionCheckWorker started (manual=%s)", manual)

    def start_version_backfill(self, site_id: int):
        """
        Start a backfill pass for a specific site.
        Called from Settings → Version Tracking's Backfill button.
        No-ops if a version worker is already running.
        """
        if self._version_worker_busy():
            self.library_view.show_status(
                "Version check already running…", timeout=3000)
            return

        self._version_worker_running = True
        self.library_view.show_status("Running version backfill…")
        worker = vc_mod.VersionBackfillWorker(site_id)
        worker.progress.connect(self._on_version_progress)
        worker.finished.connect(self._on_version_worker_done)
        worker.finished.connect(lambda f, c, e: worker.deleteLater())
        self._version_worker = worker
        worker.start()
        log.info("VersionBackfillWorker started for site_id=%d", site_id)

    def _on_version_progress(self, current: int, total: int, message: str):
        self.library_view.show_status(f"{message}  ({current}/{total})")

    def _arm_version_check_timer(self):
        """
        Compute remaining time until next check and arm the one-shot timer.
        Called on startup and after every completed check.
        """
        self._version_check_timer.stop()
        delay_ms = vc_mod.get_next_check_delay_ms()
        if delay_ms is None:
            log.debug("Version check auto disabled — timer not armed")
            return
        log.info("Version check timer armed: %.1f minutes",
                 delay_ms / 60000)
        self._version_check_timer.start(delay_ms)

    def _on_version_check_timer(self):
        """Fired by the recurring timer — start a background recheck."""
        log.info("Version check timer fired")
        self._start_version_check(manual=False)

    # ── AppImage Self-Update Check ────────────────────────────────────────────

    def _start_update_check(self):
        """
        Silent background check against GitHub, run on every launch. Never
        blocks startup (it's a QThread) and never surfaces anything on
        failure — a failed check just does nothing, per spec.
        """
        worker = UpdateCheckWorker()
        worker.finished.connect(self._on_update_check_done)
        worker.finished.connect(lambda _r: worker.deleteLater())
        self._update_check_worker = worker
        worker.start()

    def _on_update_check_done(self, release):
        """
        Shared completion handler for the silent launch check. Records the
        result in settings either way (so Settings → About can show it
        without forcing a fresh check), and — only when a newer version
        exists and its version hasn't been explicitly dismissed before —
        fires the app_update notification. Manual checks from Settings →
        About do NOT go through this method; see SettingsView._on_manual_check_done,
        which always shows the result and never touches skipped_app_version.
        """
        if release is None:
            return  # failed check — do nothing silently, per spec

        db.set_setting("update_check_last_at", datetime.datetime.utcnow().isoformat())
        current_version = self.settings_view.APP_VERSION

        if update_check.is_update_available(current_version, release["tag"]):
            db.set_setting("update_available_tag", release["tag"])
            db.set_setting("update_available_date", release["release_date"])
            db.set_setting("update_available_url", release.get("download_url") or "")

            version = update_check.normalize_tag(release["tag"])
            skipped = db.get_setting("skipped_app_version", "")
            if version != skipped:
                message = f"VaultPlay v{version} is available"
                if release["release_date"]:
                    message += f" (released {release['release_date']})"
                message += ". Click to view in Settings → About."
                db.add_notification(
                    "app_update", f"Update Available: v{version}", message,
                    game_id=None, dedup=True)
                self.library_view.refresh_notification_badge()
        else:
            # Current build is up to date — clear any stale "update available"
            # settings left over from a previous check (e.g. before this
            # exact build was installed via a self-update).
            db.set_setting("update_available_tag", "")
            db.set_setting("update_available_date", "")
            db.set_setting("update_available_url", "")

        # If Settings → About happens to be open (or gets opened later),
        # let it reflect what this check just found without re-checking.
        try:
            if hasattr(self.settings_view, "_on_update_check_result"):
                self.settings_view._on_update_check_result(release, current_version)
        except Exception as e:
            log.debug("Settings About page refresh after update check failed: %s", e)

    # ── Playtime tracking ─────────────────────────────────────────────────────

    def _on_game_launched(self, game_id: int, proc, wine_bin: str,
                          wine_prefix: str):
        """
        Start a PlaytimeWatcher for a game that was just launched.
        Ignores the launch if a watcher is already running for this game
        (i.e. the game is still open from a previous launch click).
        The watcher removes itself from _playtime_watchers when it finishes,
        so a subsequent launch after the game closes works normally.
        """
        if game_id in self._playtime_watchers:
            existing = self._playtime_watchers[game_id]
            if existing.isRunning():
                log.info(
                    "[PLAYTIME] Ignoring duplicate launch for game_id=%d "
                    "(watcher already running)", game_id)
                return
            # Watcher finished but wasn't cleaned up yet — remove it
            del self._playtime_watchers[game_id]

        watcher = playtime_mod.PlaytimeWatcher(
            game_id     = game_id,
            proc        = proc,
            wine_bin    = wine_bin,
            wine_prefix = wine_prefix,
            parent      = self,
        )
        self._register_watcher(game_id, watcher, datetime.datetime.utcnow())

    def _register_watcher(self, game_id: int, watcher: playtime_mod.PlaytimeWatcher,
                          started_at: datetime.datetime):
        """
        Shared registration path for both a fresh launch (_on_game_launched)
        and a restart-reattached process (_reattach_running_games) — wires
        up the watcher's signals, starts it, and updates the Currently
        Playing Indicator state. started_at may be in the past for a
        reattached process (elapsed time already accumulated).
        """
        watcher.session_ended.connect(self._on_session_ended)
        watcher.finished.connect(
            lambda gid=game_id: self._playtime_watchers.pop(gid, None))
        self._playtime_watchers[game_id] = watcher
        watcher.start()
        self._set_currently_playing(game_id, started_at)
        log.info("[PLAYTIME] Watcher started for game_id=%d", game_id)

    # ── Currently Playing Indicator ───────────────────────────────────────────

    def _set_currently_playing(self, game_id: Optional[int],
                               started_at: Optional[datetime.datetime] = None):
        """
        Single source of truth for "which game is currently playing" —
        propagates to the sidebar indicator, the library tile overlay, and
        the game detail page's launch button / Force Quit visibility.
        Pass game_id=None to clear (game closed or force-quit).
        """
        self._currently_playing_game_id = game_id
        self.library_view.set_currently_playing(game_id)
        self.detail_view.set_currently_playing(game_id)

        if game_id is None:
            self.sidebar.playing_indicator.hide_playing()
            return

        game = db.get_game(game_id)
        title = ((game["title"] or game["display_name"] or game["folder_name"])
                 if game else f"Game {game_id}")
        self.sidebar.playing_indicator.show_playing(
            game_id, title, started_at or datetime.datetime.utcnow())

        cover_url = game["cover_url"] if game else None
        if cover_url:
            loader = _CoverImageLoader(game_id, cover_url)
            loader.signals.loaded.connect(self._on_playing_cover_loaded)
            self._pool.start(loader)

    def _on_playing_cover_loaded(self, game_id: int, local_path: str):
        # Ignore a late-arriving load for a game that's no longer the one
        # showing in the indicator (e.g. it closed and a different game
        # started before this download finished).
        if game_id != self.sidebar.playing_indicator.current_game_id():
            return
        pix = QPixmap(local_path)
        if not pix.isNull():
            self.sidebar.playing_indicator.set_cover_pixmap(pix)

    def _on_force_quit_requested(self, game_id: int):
        """Cogwheel → Force Quit. Kills the tracked process; the watcher's
        own wait loop (already blocked on it) detects the exit and records
        the session normally — see PlaytimeWatcher.request_kill()."""
        watcher = self._playtime_watchers.get(game_id)
        if watcher and watcher.isRunning():
            watcher.request_kill()
        else:
            log.warning("[PLAYTIME] Force Quit requested for game_id=%d but "
                       "no active watcher was found", game_id)

    def _reattach_running_games(self):
        """
        Restart-reattachment: if VaultPlay was closed and reopened while a
        Play-button-launched game was still running, find it on /proc
        (playtime.find_running_pid_for_exe) and attach a normal
        PlaytimeWatcher to it with the correct elapsed time already
        counted — rather than losing track of it until it happens to exit.

        Only the first match found is reattached to the live UI state — the
        sidebar/tile/launch-button surfaces are all single-game by design
        (see _set_currently_playing). A second real game running
        concurrently is a rare edge case; its playtime is simply not shown
        live, and would be reconsidered on a future restart of this scan.
        Best-effort throughout — never raises, never blocks startup for long
        (bounded by however many installed games exist, a plain /proc walk
        per candidate).
        """
        try:
            games = db.get_all_games()
        except Exception as e:
            log.warning("[PLAYTIME] Reattach scan: could not load games: %s", e)
            return

        import installer as install_mod

        for game in games:
            if not game["is_installed"]:
                continue
            exe_path = (game["exe_path"] or "").strip()
            if not exe_path or not Path(exe_path).exists():
                continue

            game_id = game["id"]
            if game_id in self._playtime_watchers:
                continue

            pid = playtime_mod.find_running_pid_for_exe(exe_path)
            if not pid:
                continue

            elapsed = playtime_mod.proc_start_elapsed_seconds(pid) or 0.0
            wine_bin = install_mod.parse_wine_bin_from_cmd(game["launch_cmd"] or "")
            wine_prefix = game["wine_prefix"] or ""

            attached_proc = playtime_mod.AttachedProcess(pid)
            watcher = playtime_mod.PlaytimeWatcher(
                game_id=game_id, proc=attached_proc, wine_bin=wine_bin,
                wine_prefix=wine_prefix, elapsed_seconds=elapsed, parent=self)
            started_at = (datetime.datetime.utcnow()
                         - datetime.timedelta(seconds=elapsed))
            self._register_watcher(game_id, watcher, started_at)
            log.info("[PLAYTIME] Reattached to running game_id=%d "
                     "(pid=%d, elapsed=%.0fs)", game_id, pid, elapsed)
            break

    def _on_session_ended(self, game_id: int, minutes: int):
        """
        Called when a PlaytimeWatcher finishes.
        Refreshes the detail view if it's currently showing this game
        so the updated playtime and last-played rows appear immediately.
        """
        if self._currently_playing_game_id == game_id:
            self._set_currently_playing(None)

        if minutes > 0:
            log.info("[PLAYTIME] Session recorded: game_id=%d  %d min",
                     game_id, minutes)
            # Auto-flip Completion Status unplayed → in_progress. Piggybacks
            # on PlaytimeWatcher's own MIN_SESSION_MINUTES gate — minutes > 0
            # here already means the session cleared that bar, so no separate
            # threshold check is needed.
            if self._maybe_flip_to_in_progress(game_id):
                self._load_library()
        else:
            log.info("[PLAYTIME] Session too short, not recorded: game_id=%d",
                     game_id)

        # Save Backup Flow 1 — run regardless of whether playtime met the
        # minimum threshold to be recorded; even a short session can have
        # written a save. No-ops immediately if the feature is off, the
        # game is already linked, or no pre-launch snapshot was taken.
        self._maybe_run_save_backup_flow(game_id)

        # Refresh detail view if it's showing this game
        if (self.stack.currentIndex() == 1 and
                self.detail_view._game_id == game_id):
            self.detail_view.load_game(game_id)

    def _maybe_flip_to_in_progress(self, game_id: int) -> bool:
        """
        Auto-flip completion_status from 'unplayed' to 'in_progress' once a
        session has actually been recorded (called only when minutes > 0,
        i.e. PlaytimeWatcher.MIN_SESSION_MINUTES was already cleared — no
        separate threshold check needed here).

        Never overrides a status the user set themselves — a game already
        'in_progress', 'completed', or 'abandoned' is left exactly as-is.
        This only ever moves a game out of the default 'unplayed' state,
        the same one-directional nudge the feature spec describes.

        Returns True if the status was actually changed, so the caller
        knows whether a library refresh is worth doing.
        """
        try:
            gs = db.get_game_state(game_id)
            status = gs["completion_status"] if gs else "unplayed"
        except Exception as e:
            log.warning("[COMPLETION] Could not read game_state for game_id=%d: %s",
                       game_id, e)
            return False

        if status != "unplayed":
            return False

        try:
            db.set_completion_status(game_id, "in_progress")
        except Exception as e:
            log.warning("[COMPLETION] Could not auto-flip completion status "
                       "for game_id=%d: %s", game_id, e)
            return False

        log.info("[COMPLETION] game_id=%d auto-flipped unplayed → in_progress "
                 "after a recorded play session", game_id)
        return True

    # ── Save Backup ───────────────────────────────────────────────────────────

    def _maybe_run_save_backup_flow(self, game_id: int):
        """
        Post-play Save Backup detection (Flow 1 — see save_backup.py).
        No-ops immediately if: the feature is disabled, this game is
        already linked (save_source_path set — Flow 2 territory, not
        implemented yet), or no pending snapshot was persisted for this
        game (meaning no snapshot was taken at launch — e.g. the feature
        was toggled on mid-session, or the game has no wine_prefix).
        """
        if db.get_setting("save_backup_enabled", "false") != "true":
            return

        game = db.get_game(game_id)
        if not game:
            return

        try:
            existing = db.get_save_paths(game_id)
        except Exception:
            existing = {"save_source_path": None}
        if existing.get("save_source_path"):
            return  # already linked

        pending = save_backup.load_pending_snapshot(game["folder_name"])
        if not pending:
            return

        actual_prefix = Path(pending["prefix_path"])
        snapshot      = pending["snapshot"]
        title = game["title"] or game["display_name"] or game["folder_name"]

        # Fast path: known common save locations
        known = save_backup.scan_known_locations(actual_prefix, title)
        if known:
            candidates = save_backup.candidates_from_known_locations(known)
        else:
            changed = save_backup.diff_snapshot(actual_prefix, snapshot)
            drive_c = actual_prefix / "drive_c"
            kept, filtered = save_backup.filter_noise(changed, drive_c)
            if filtered:
                log.debug("[SAVE BACKUP] Filtered %d noise file(s) for game_id=%d: %s",
                          len(filtered), game_id,
                          [str(f) for f in filtered[:20]])
            candidates = save_backup.rank_candidate_folders(kept, drive_c, title)

        if not candidates:
            log.debug("[SAVE BACKUP] No changed save folders detected for "
                      "game_id=%d (%s)", game_id, title)
            save_backup.delete_pending_snapshot(game["folder_name"])
            return

        dlg = SaveBackupDialog(title, candidates, parent=self)
        if dlg.exec() != SaveBackupDialog.DialogCode.Accepted:
            # Skipped — save_source_path stays unset, so Flow 1 runs again
            # the next time this game is played.
            log.info("[SAVE BACKUP] User skipped backup prompt for game_id=%d (%s)",
                     game_id, title)
            save_backup.delete_pending_snapshot(game["folder_name"])
            return

        chosen = dlg.chosen_path()
        save_backup.delete_pending_snapshot(game["folder_name"])
        if not chosen:
            return

        save_root = db.get_setting(
            "save_backup_root", str(Path.home() / "Documents" / "Game Saves"))
        self._apply_save_backup(game_id, game["folder_name"], Path(chosen), Path(save_root))

    def _apply_save_backup(self, game_id: int, folder_name: str,
                           chosen_folder: Path, save_root: Path):
        """Move the user-chosen folder to the canonical save path and
        record the link in game_state. Handles the overwrite-conflict
        confirmation if a previous backup already exists at that path."""
        try:
            canonical = save_backup.move_and_link(chosen_folder, save_root, folder_name)
        except save_backup.SaveMoveConflict as e:
            reply = QMessageBox.question(
                self, "Save Backup",
                f"A backed-up save already exists at:\n{e.canonical_path}\n\n"
                "Continuing will overwrite it with the save you just picked. "
                "Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel
            )
            if reply != QMessageBox.StandardButton.Yes:
                log.info("[SAVE BACKUP] User declined overwrite for game_id=%d — skipped",
                         game_id)
                return
            try:
                canonical = save_backup.move_and_link(
                    chosen_folder, save_root, folder_name, overwrite_confirmed=True)
            except Exception as e2:
                log.error("[SAVE BACKUP] move_and_link failed after overwrite confirm: %s", e2)
                self.library_view.show_status(f"⚠ Save Backup failed: {e2}", timeout=6000)
                self._notify_save_backup_failed(game_id, folder_name, e2)
                return
        except Exception as e:
            log.error("[SAVE BACKUP] move_and_link failed: %s", e)
            self.library_view.show_status(f"⚠ Save Backup failed: {e}", timeout=6000)
            self._notify_save_backup_failed(game_id, folder_name, e)
            return

        db.set_save_paths(game_id, save_path=str(canonical), save_source_path=str(chosen_folder))
        log.info("[SAVE BACKUP] Linked game_id=%d: %s → %s", game_id, chosen_folder, canonical)
        self.library_view.show_status(f"✓ Save backed up to {canonical}", timeout=5000)

    def _notify_save_backup_failed(self, game_id: int, folder_name: str, error: Exception):
        """Notifications — save_backup_failed. Deduped in db.add_notification()
        against any unread save_backup_failed already showing for this game."""
        db.add_notification(
            "save_backup_failed", "Save Backup Failed",
            f"Could not back up save for {folder_name}: {error}",
            game_id=game_id, dedup=True)
        self.library_view.refresh_notification_badge()
