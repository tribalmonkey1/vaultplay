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

import logging
from pathlib import Path

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QLabel, QPushButton, QStackedWidget, QSizePolicy,
    QFrame, QApplication
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSize
from PyQt6.QtGui import QFont, QIcon, QPixmap, QColor

import db
import scanner
import metadata as meta_mod
import protondb as protondb_mod

from ui.library_view import LibraryView
from ui.setup_wizard import SetupWizard
from ui.game_detail import GameDetailView
from ui.settings_view import SettingsView
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


class ProtonDBWorker(QThread):
    progress = pyqtSignal(str)
    finished = pyqtSignal(int)   # count updated

    def run(self):
        import time as _time
        import db as _db

        # Ensure the local index is built before trying to apply recommendations.
        # On first run the index won't exist yet — download the latest dump.
        # On subsequent runs, only download if a newer dump is available.
        meta = protondb_mod.get_index_meta()
        if not meta:
            self.progress.emit("No ProtonDB index found — downloading data dump…")
            latest = protondb_mod.get_latest_dump_filename()
            if latest:
                protondb_mod.build_index_from_dump(
                    latest,
                    progress_cb=lambda stage, pct, msg: self.progress.emit(
                        f"{stage}: {msg}"))
            else:
                self.progress.emit("Could not reach GitHub — using tier heuristic only")
        else:
            needs_upd, _, latest = protondb_mod.needs_update()
            if needs_upd and latest:
                self.progress.emit(f"New ProtonDB dump available ({latest}) — updating…")
                protondb_mod.build_index_from_dump(
                    latest,
                    progress_cb=lambda stage, pct, msg: self.progress.emit(
                        f"{stage}: {msg}"))

        # Apply recommendations to all games missing ProtonDB data
        games = _db.get_games_missing_protondb()
        count = 0
        for i, game in enumerate(games):
            self.progress.emit(
                f"Applying ProtonDB data… {i+1}/{len(games)} — "
                f"{game['display_name'] or ''}")
            result = protondb_mod.fetch_and_store(game["id"])
            if result:
                count += 1
            _time.sleep(0.05)
        self.finished.emit(count)


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


class Sidebar(QWidget):
    filter_changed = pyqtSignal(str)
    settings_requested = pyqtSignal()

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

        # Library section
        self._add_section_label(layout, "Library")
        self.items: dict[str, SidebarItem] = {}
        self._add_item(layout, "All Games",    "all",         0)
        self._add_item(layout, "Installed",    "installed",   0)
        self._add_item(layout, "Not Installed","uninstalled", 0)

        # Genre section
        self.genre_label = self._add_section_label(layout, "Categories")
        self.genre_container = QWidget()
        self.genre_layout = QVBoxLayout(self.genre_container)
        self.genre_layout.setContentsMargins(0, 0, 0, 0)
        self.genre_layout.setSpacing(0)
        layout.addWidget(self.genre_container)

        layout.addStretch()

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

    def update_counts(self, all_count, installed, uninstalled, cat_counts: dict):
        """cat_counts: {folder_name: (display_name, count)}"""
        self.items["all"].update_count(all_count)
        self.items["installed"].update_count(installed)
        self.items["uninstalled"].update_count(uninstalled)

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

        self.detail_view = GameDetailView()
        self.detail_view.back_requested.connect(self._show_library)
        self.detail_view.install_finished.connect(self._on_install_finished)

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

        # Pending scan: if a scan is requested while the trickle is running,
        # store its args here and fire it when trickle_finished fires.
        self._pending_scan: dict | None = None   # {"nas_path": str, "clear_first": bool}

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

        all_count   = len(games)
        installed   = sum(1 for g in games if g["is_installed"])
        uninstalled = all_count - installed

        categories = db.get_categories()
        cat_counts = {}
        for cat in categories:
            folder  = cat["folder_name"]
            display = cat["display_name"]
            count   = sum(1 for g in games if _safe_get(g, "category") == folder)
            if count > 0:
                cat_counts[folder] = (display, count)

        self.sidebar.update_counts(all_count, installed, uninstalled, cat_counts)
        self.library_view.load_games(games)

    def _on_filter_changed(self, key: str):
        self.library_view.apply_filter(key)
        if key.startswith("cat:"):
            cats = db.get_categories()
            for c in cats:
                if c["folder_name"] == key[4:]:
                    self.library_view.set_page_title(c["display_name"])
                    return

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

    def _start_protondb_fetch(self):
        if self._proton_worker and self._proton_worker.isRunning():
            return
        self.library_view.show_status("Fetching ProtonDB compatibility data…")
        self._proton_worker = ProtonDBWorker()
        self._proton_worker.progress.connect(self._on_proton_progress)
        self._proton_worker.finished.connect(self._on_proton_done)
        self._proton_worker.start()

    def _on_proton_progress(self, message: str):
        self.library_view.show_status(message)

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
