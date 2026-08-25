"""
ui/cogwheel_menu.py — Cogwheel Menu for VaultPlay (game detail view)

Spec: Notion → Features → Fully Planned → Cogwheel Menu.

v1 SCOPE — see the feature's Notion page for the full spec and what's
deferred. Built now:
    - Manage Install dialog (single page, stacked sections — matches
      install_dialog.py's layout): Proton/Wine version + Redistributables
      + Launch Options, one combined Apply. All default to whatever is
      CURRENTLY applied, not blank/first-item.
    - Launch Options section (see ui/launch_options_section.py — shared
      widget, also intended for install_dialog.py's planned Launch
      Options tab): renderer/overlay/GameMode/FSR/sync/audio/GPU/
      Proton-only toggles + free-text env vars & launch args, with Reset
      to Defaults. Saved synchronously before Apply's worker runs so
      installer.regenerate_launch() picks up the fresh values.
    - Open in file manager
    - Save Files section (back up now, relink, open directory) — only
      when Save Backup is enabled
    - Manual registration / path-repair fields (exe path, Wine prefix,
      install directory) for installs VaultPlay didn't detect itself
    - Launch with Debug Log — captures stdout/stderr to a timestamped,
      5MB-capped per-game log file (see debug_launch.py). Shown only when
      a Play button would be available (is_detected), same gate
      game_detail.py uses. Dispatched through game_detail.py's
      _launch_game(debug=True) so playtime tracking and the snapshot/
      launch_cmd/.desktop fallback chain all behave identically to a
      normal launch — only where stdout/stderr goes differs.
    - Force Quit — shown only while this specific game is currently
      running (see Currently Playing Indicator). Kills the tracked
      process via MainWindow's PlaytimeWatcher.request_kill(); the
      watcher's own wait loop, already blocked on that process, detects
      the exit and records the session exactly as a normal quit.

Deferred (blocked on features that don't exist yet — will be added as
their own menu items / sections once those features land, NOT re-planned
here):
    - Launch Options' engine-detected argument checkboxes → metadata
      doesn't store an IGDB engine field yet (see
      installer._game_engine()'s docstring). The section is wired up in
      launch_options.py/launch_options_section.py and will activate the
      moment that column exists — nothing else to build then.
    - platform_type gating       → needs Emulation Support's schema

Per spec: this button/menu only appears when a game is installed. There
is no cogwheel for uninstalled games — do not add one.
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
import subprocess
from pathlib import Path

from PyQt6.QtWidgets import (
    QMenu, QPushButton, QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QComboBox, QCheckBox, QWidget, QFileDialog, QMessageBox,
    QScrollArea, QFrame
)
from PyQt6.QtCore import Qt, pyqtSignal, QThread, QPoint, QRectF
from PyQt6.QtGui import QFont, QPainter, QBrush, QColor, QPainterPath, QTransform

import db
import installer as install_mod
import protondb as protondb_mod
import save_backup
from ui.style import COLORS, accent_button_style
from ui.launch_options_section import LaunchOptionsSection

log = logging.getLogger(__name__)

COMMON_REDISTS = install_mod.COMMON_REDISTS

# Every bare QLabel below gets this explicit style. Relying on the app-wide
# STYLESHEET (set on MainWindow, not QApplication) to cascade into modal
# dialogs is NOT reliable — confirmed: labels without this showed up as a
# visible bordered box in testing. Every other dialog in this codebase
# already sets this explicitly per-label; match that convention here too.
_LABEL_BASE = "background: transparent; border: none;"


def _safe_get(row, key, default=None):
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def _is_proton_value(value: str) -> bool:
    """
    Best-effort check of whether a Proton/Wine combo's currently-selected
    'value' key (e.g. 'ge-proton-10.5', 'proton-experimental', 'system-wine')
    refers to a Proton build rather than plain Wine — used to show/hide
    Launch Options' Proton-only section reactively as the picker changes,
    without needing to resolve the actual binary path just for that.
    """
    if not value:
        return False
    # Covers 'proton-experimental', 'proton-9.0', 'ge-proton-10.5', etc.
    # 'system-wine' and 'wine-ge-8-26' contain no 'proton' substring, so
    # this is a reliable proton-vs-wine split for every value key protondb.py
    # generates (see protondb._normalize_version / scan_installed_versions).
    return "proton" in value.lower()


# ── Small reusable bits ───────────────────────────────────────────────────────

class NoScrollComboBox(QComboBox):
    def wheelEvent(self, event):
        event.ignore()


def _label(text: str, muted: bool = False, wrap: bool = False) -> QLabel:
    lbl = QLabel(text)
    lbl.setFont(QFont("DM Sans", 10))
    color = COLORS["text_muted"] if muted else COLORS["text"]
    lbl.setStyleSheet(f"color: {color}; {_LABEL_BASE}")
    if wrap:
        lbl.setWordWrap(True)
    return lbl


def _browse_row(initial: str, placeholder: str, is_dir: bool = True) -> tuple[QWidget, QLineEdit]:
    """Build a QLineEdit + Browse button row. Returns (row_widget, edit)."""
    row = QWidget()
    row.setStyleSheet("background: transparent;")
    l = QHBoxLayout(row)
    l.setContentsMargins(0, 0, 0, 0)
    l.setSpacing(6)

    edit = QLineEdit(initial)
    edit.setFont(QFont("DM Mono", 10))
    edit.setPlaceholderText(placeholder)
    edit.setStyleSheet(f"color: {COLORS['accent2']};")
    l.addWidget(edit, 1)

    btn = QPushButton("Browse…")
    def _browse():
        if is_dir:
            p = QFileDialog.getExistingDirectory(row, "Select Folder", edit.text() or str(Path.home()))
        else:
            p, _ = QFileDialog.getOpenFileName(row, "Select File", edit.text() or str(Path.home()))
        if p:
            edit.setText(p)
    btn.clicked.connect(_browse)
    l.addWidget(btn)

    return row, edit


# ── Installation paths dialog (manual registration / path repair) ─────────────

class InstallPathsDialog(QDialog):
    """
    Set/repair the executable path, Wine prefix, and install directory for
    a game's install record. Used both for the Cogwheel's warning state
    (VaultPlay didn't detect this install — e.g. installed by hand or via
    Lutris/Steam directly) and as a general repair tool if paths ever go
    stale (moved files, renamed prefix, etc).
    """
    saved = pyqtSignal(int)   # game_id

    def __init__(self, game, parent=None):
        super().__init__(parent)
        self._game_id = game["id"]
        title = game["title"] or game["display_name"] or game["folder_name"]
        self.setWindowTitle(f"Installation Paths — {title}")
        self.setModal(True)
        self.setMinimumWidth(520)
        self.setStyleSheet(f"QDialog {{ background: {COLORS['surface']}; }}")

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(12)

        exe_path     = _safe_get(game, "exe_path", "") or ""
        wine_prefix  = _safe_get(game, "wine_prefix", "") or ""
        install_path = _safe_get(game, "install_path", "") or ""

        if not exe_path or not Path(exe_path).exists():
            warn = _label(
                "⚠  VaultPlay doesn't have a valid executable on record for "
                "this install. Fill in the fields below so it can be launched "
                "and managed normally.", wrap=True)
            warn.setFont(QFont("DM Sans", 10))
            warn.setStyleSheet(
                f"color: {COLORS['accent']}; background: rgba(232,199,106,0.08);"
                " border: none; border-radius: 6px; padding: 8px 10px;")
            root.addWidget(warn)

        self._exe_edit = self._prefix_edit = self._install_edit = None
        for label_text, initial, is_dir in [
            ("Executable (.exe)", exe_path, False),
            ("Wine Prefix Directory", wine_prefix, True),
            ("Install Directory", install_path, True),
        ]:
            lbl = QLabel(label_text.upper())
            lbl.setFont(QFont("DM Mono", 8))
            lbl.setStyleSheet(f"color: {COLORS['text_muted']}; letter-spacing: 1px; {_LABEL_BASE}")
            root.addWidget(lbl)
            row, edit = _browse_row(initial, label_text, is_dir=is_dir)
            root.addWidget(row)
            if label_text.startswith("Executable"):
                self._exe_edit = edit
            elif label_text.startswith("Wine Prefix"):
                self._prefix_edit = edit
            else:
                self._install_edit = edit

        self._status_lbl = _label("")
        self._status_lbl.setWordWrap(True)
        root.addWidget(self._status_lbl)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        save_btn = QPushButton("Save")
        save_btn.setStyleSheet(accent_button_style())
        save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(save_btn)
        root.addLayout(btn_row)

    def _on_save(self):
        exe = self._exe_edit.text().strip()
        prefix = self._prefix_edit.text().strip()
        install_path = self._install_edit.text().strip()

        warnings = []
        if exe and not Path(exe).exists():
            warnings.append("executable path doesn't exist")
        if prefix and not Path(prefix).exists():
            warnings.append("Wine prefix path doesn't exist")
        if warnings:
            self._status_lbl.setText("⚠ " + "; ".join(warnings) + " — saved anyway.")
            self._status_lbl.setStyleSheet(f"color: {COLORS['accent']}; {_LABEL_BASE}")

        db.register_manual_install(
            self._game_id, exe_path=exe, wine_prefix=prefix, install_path=install_path)
        self.saved.emit(self._game_id)
        self.accept()


# ── Section box (mirrors ui/install_dialog.py's SectionBox styling) ──────────

class SectionBox(QFrame):
    def __init__(self, title=None, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"""
            QFrame {{
                background: {COLORS['surface2']};
                border: 1px solid {COLORS['border']};
                border-radius: 10px;
            }}""")
        self._inner = QVBoxLayout(self)
        self._inner.setContentsMargins(16, 12, 16, 14)
        self._inner.setSpacing(10)
        if title:
            lbl = QLabel(title.upper())
            lbl.setFont(QFont("DM Mono", 8))
            lbl.setStyleSheet(
                f"color: {COLORS['text_muted']}; letter-spacing: 2px;"
                f" {_LABEL_BASE}")
            self._inner.addWidget(lbl)

    def add(self, widget):
        self._inner.addWidget(widget)
        return widget

    def inner_layout(self):
        return self._inner


# ── Manage Install dialog: Proton/Wine + Redistributables, one page ──────────
# Per user preference: NOT tabbed — both sections stacked on one scrollable
# page with a single combined Apply, matching install_dialog.py's layout
# style. Unlike InstallDialog, this deliberately omits install type, Wine
# prefix path, and the cleanup-tmp toggle — none of that is relevant once a
# game is already installed; those live in InstallPathsDialog / are simply
# not applicable here.

def _detect_current_proton_value(game) -> str:
    """
    Figure out which installed-version 'value' key this game is ACTUALLY
    running with right now, for defaulting the picker. Priority:
      1. installs.proton_version, if this install was made/changed after
         that column existed and got recorded.
      2. Parsed out of the stored launch_cmd (installs made before the
         column existed still have a launch_cmd encoding the real binary).
      3. default_proton_version setting.
      4. "" — caller falls back to combo index 0, but this should be rare.
    Never silently defaults to whatever sorts first without at least
    attempting 1-3 — that's what produced the "looks like System Wine but
    isn't really" bug.
    """
    stored = (_safe_get(game, "proton_version", "") or "").strip()
    if stored:
        return stored

    launch_cmd = _safe_get(game, "launch_cmd", "") or ""
    wine_bin = install_mod.parse_wine_bin_from_cmd(launch_cmd)
    detected = protondb_mod.find_version_value_for_binary(wine_bin)
    if detected:
        return detected

    return db.get_setting("default_proton_version", "")


def _currently_applied_redists(game_id: int, wine_prefix: str) -> set:
    """
    What's actually applied right now, for pre-checking the dialog.
    Two sources, unioned:
      - installs.redists — what VaultPlay itself last (re)installed via
        this Cogwheel action. Blank for installs made before this column
        existed.
      - winetricks.log inside the prefix — ground truth of what winetricks
        has actually recorded as installed, going all the way back to the
        original install-time run. This is what makes an install made
        before installs.redists existed still pre-check correctly.
    """
    stored = set(db.get_install_redists(game_id))
    on_disk: set = set()
    if wine_prefix and Path(wine_prefix).exists():
        on_disk = install_mod.get_installed_redists(Path(wine_prefix))
    return stored | on_disk


class _ManageApplyWorker(QThread):
    """
    Runs both actions in sequence on a background thread: rewrites the
    launch command for the selected Proton/Wine version, then (re)installs
    the selected redistributables. Combined into one worker so a single
    Apply click on the single-page dialog produces one status result
    instead of the caller juggling two separate async operations.
    """
    stage    = pyqtSignal(str)   # short status text, shown live in the dialog
    finished = pyqtSignal(dict)  # {"launch": {...}, "redist": {...}}

    def __init__(self, game_id: int, proton_version: str,
                 redists: list, force: bool):
        super().__init__()
        self._game_id = game_id
        self._proton_version = proton_version
        self._redists = redists
        self._force = force

    def run(self):
        self.stage.emit("Applying Proton/Wine version…")
        launch_result = install_mod.regenerate_launch(self._game_id, self._proton_version)

        self.stage.emit("Installing redistributables…")
        redist_result = install_mod.rerun_redists(
            self._game_id, self._redists, force=self._force,
            progress_cb=lambda stage, pct, msg: self.stage.emit(msg))

        self.finished.emit({"launch": launch_result, "redist": redist_result})


class ManageInstallDialog(QDialog):
    """
    Single-page home for Proton/Wine version + Redistributables — stacked
    SectionBoxes like install_dialog.py, one combined Apply button. Once
    Launch Options + environment variables exists as a real feature, its
    section slots in below Redistributables the same way (placeholder
    shown now so its eventual location is obvious).
    """
    changed = pyqtSignal(int)   # game_id

    def __init__(self, game, parent=None):
        super().__init__(parent)
        self._game_id = game["id"]
        self._worker: _ManageApplyWorker | None = None

        title = game["title"] or game["display_name"] or game["folder_name"]
        self.setWindowTitle(f"Manage Install — {title}")
        self.setModal(True)
        self.setMinimumWidth(560)
        self.setFixedWidth(600)
        self.setStyleSheet(f"QDialog {{ background: {COLORS['surface']}; }}")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        body = QWidget()
        body.setStyleSheet(f"background: {COLORS['surface']};")
        body_l = QVBoxLayout(body)
        body_l.setContentsMargins(20, 18, 20, 16)
        body_l.setSpacing(12)

        # ── Proton / Wine Version ─────────────────────────────────────────────
        current = _detect_current_proton_value(game)
        versions = protondb_mod.get_versions_for_ui()
        matched_label = next((lbl for lbl, val in versions if val == current), "")
        proton_title = "Proton / Wine Version"
        if matched_label:
            proton_title += f"  ·  currently: {matched_label}"

        proton_box = SectionBox(proton_title)
        self.proton_combo = NoScrollComboBox()
        self.proton_combo.setFont(QFont("DM Sans", 11))
        if not versions:
            self.proton_combo.addItem("No versions found — install via ProtonUp-Qt", "")
        else:
            for label, value in versions:
                self.proton_combo.addItem(label, value)
            idx = self.proton_combo.findData(current)
            if idx >= 0:
                self.proton_combo.setCurrentIndex(idx)
        proton_box.add(self.proton_combo)
        body_l.addWidget(proton_box)

        # ── Redistributables ─────────────────────────────────────────────────
        applied = _currently_applied_redists(self._game_id, _safe_get(game, "wine_prefix", ""))
        redist_box = SectionBox(f"Redistributables  ·  {len(applied)} currently applied")
        self._checks: dict[str, QCheckBox] = {}
        grid_w = QWidget()
        grid_w.setStyleSheet("background: transparent; border: none;")
        grid_l = QHBoxLayout(grid_w)
        grid_l.setContentsMargins(0, 0, 0, 0)
        col1, col2 = QVBoxLayout(), QVBoxLayout()
        col1.setSpacing(4)
        col2.setSpacing(4)
        for i, verb in enumerate(COMMON_REDISTS):
            cb = QCheckBox(verb)
            cb.setFont(QFont("DM Mono", 10))
            is_applied = verb in applied
            cb.setChecked(is_applied)
            cb.setStyleSheet(
                f"color: {COLORS['accent2']};" if is_applied
                else f"color: {COLORS['text_dim']};"
            )
            self._checks[verb] = cb
            (col1 if i % 2 == 0 else col2).addWidget(cb)
        grid_l.addLayout(col1)
        grid_l.addLayout(col2)
        redist_box.add(grid_w)

        self.force_check = QCheckBox("Force reinstall changes (even if already installed)")
        self.force_check.setFont(QFont("DM Sans", 10))
        self.force_check.setChecked(False)
        redist_box.add(self.force_check)
        body_l.addWidget(redist_box)

        # ── Launch Options ───────────────────────────────────────────────────
        # Shared widget (ui/launch_options_section.py) so the Install dialog's
        # planned Launch Options tab can embed the exact same form later.
        launch_opts_box = SectionBox("Launch Options")
        self.launch_opts = LaunchOptionsSection(self._game_id)
        self.launch_opts.set_is_proton(
            _is_proton_value(self.proton_combo.currentData()))
        self.launch_opts.set_engine(_safe_get(game, "engine"))
        self.launch_opts.reset_done.connect(self._on_launch_opts_reset)
        launch_opts_box.add(self.launch_opts)
        body_l.addWidget(launch_opts_box)

        # Keep the Proton-only options section in sync with the Proton/Wine
        # picker above — Large Address Aware / Use Software D3D are hidden
        # entirely for plain Wine selections.
        self.proton_combo.currentIndexChanged.connect(self._on_proton_combo_changed)

        self._status_lbl = _label("")
        self._status_lbl.setWordWrap(True)
        body_l.addWidget(self._status_lbl)
        body_l.addStretch()

        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        # ── Footer ────────────────────────────────────────────────────────────
        footer = QWidget()
        footer.setStyleSheet(
            f"background: {COLORS['surface']}; border-top: 1px solid {COLORS['border']};")
        foot_l = QHBoxLayout(footer)
        foot_l.setContentsMargins(20, 12, 20, 12)
        foot_l.setSpacing(10)
        foot_l.addStretch()

        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.accept)
        foot_l.addWidget(self.close_btn)

        self.apply_btn = QPushButton("Apply")
        self.apply_btn.setStyleSheet(accent_button_style())
        self.apply_btn.clicked.connect(self._on_apply)
        foot_l.addWidget(self.apply_btn)
        root.addWidget(footer)

    def _on_proton_combo_changed(self, _index: int):
        self.launch_opts.set_is_proton(
            _is_proton_value(self.proton_combo.currentData()))

    def _on_launch_opts_reset(self):
        self._status_lbl.setText(
            "Launch options reset — click Apply to save and regenerate the launcher.")
        self._status_lbl.setStyleSheet(f"color: {COLORS['accent']}; {_LABEL_BASE}")

    def _on_apply(self):
        proton_value = self.proton_combo.currentData()
        if not proton_value:
            self._status_lbl.setText("No Proton/Wine version selected.")
            self._status_lbl.setStyleSheet(f"color: {COLORS['danger']}; {_LABEL_BASE}")
            return

        selected_redists = [v for v, cb in self._checks.items() if cb.isChecked()]

        # Launch options are saved synchronously (a fast DB write, no need
        # for the background worker) BEFORE the worker starts, since
        # _ManageApplyWorker's regenerate_launch() call reads this game's
        # options fresh from the DB via launch_options.get_effective_options() —
        # saving first is what makes the just-edited options actually land
        # in the rewritten launch_cmd/.desktop, not just the DB row.
        self.launch_opts.save()

        self.apply_btn.setEnabled(False)
        self.close_btn.setEnabled(False)
        self._status_lbl.setText("Applying…")
        self._status_lbl.setStyleSheet(f"color: {COLORS['text_muted']}; {_LABEL_BASE}")

        self._worker = _ManageApplyWorker(
            self._game_id, proton_value, selected_redists, self.force_check.isChecked())
        self._worker.stage.connect(self._on_stage)
        self._worker.finished.connect(self._on_done)
        self._worker.start()

    def _on_stage(self, message: str):
        self._status_lbl.setText(message)
        self._status_lbl.setStyleSheet(f"color: {COLORS['text_muted']}; {_LABEL_BASE}")

    def _on_done(self, result: dict):
        self.apply_btn.setEnabled(True)
        self.close_btn.setEnabled(True)

        launch_result = result.get("launch", {})
        redist_result = result.get("redist", {})
        parts = []
        is_error = False

        if launch_result.get("success"):
            parts.append("✓ Proton/Wine + launch options applied")
        else:
            is_error = True
            parts.append(f"✗ Proton/Wine: {launch_result.get('error', 'failed')}")

        if redist_result.get("success"):
            failed = redist_result.get("failed") or []
            if failed:
                parts.append("⚠ Redistributables failed: " + ", ".join(failed))
            else:
                parts.append("✓ Redistributables applied")
        else:
            is_error = True
            parts.append(f"✗ Redistributables: {redist_result.get('error', 'failed')}")

        self._status_lbl.setText("   ".join(parts))
        self._status_lbl.setStyleSheet(
            f"color: {COLORS['danger'] if is_error else '#4ade80'}; {_LABEL_BASE}")
        self.changed.emit(self._game_id)




# ── Cogwheel button + menu ────────────────────────────────────────────────────

class CogwheelButton(QPushButton):
    """
    Small icon button that builds and shows the Cogwheel Menu for the
    currently-loaded game. Caller (game_detail.py) is responsible for
    calling set_game() whenever the displayed game changes, and for
    reloading the page when game_changed fires.
    """
    game_changed = pyqtSignal(int)   # game_id — something was modified, reload
    # Cogwheel → "Launch with Debug Log". This button has no launch machinery of its
    # own (that lives in game_detail.py's _launch_game()), so it just asks the caller
    # to run it in debug mode — see game_detail.py's _on_debug_launch_requested().
    debug_launch_requested = pyqtSignal(int)   # game_id
    # Cogwheel → "Force Quit". Like debug launch above, this button has no
    # process-killing machinery of its own — MainWindow owns the actual
    # PlaytimeWatcher/proc handle (see Currently Playing Indicator).
    force_quit_requested = pyqtSignal(int)   # game_id

    def __init__(self, parent=None):
        super().__init__("", parent)
        self._game_id: int | None = None
        # Set via set_playing() by GameDetailView, which is told by
        # MainWindow whenever the running-game state changes. Controls
        # whether "Force Quit" appears in the menu at all.
        self._is_playing: bool = False
        self.setFixedSize(38, 38)
        self.setToolTip("Manage install")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(f"""
            QPushButton {{
                background: {COLORS['surface2']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
            }}
            QPushButton:hover {{
                border-color: rgba(232,199,106,0.4);
                background: rgba(232,199,106,0.06);
            }}
        """)
        self.clicked.connect(self._show_menu)

    def set_game(self, game_id: int):
        self._game_id = game_id

    def set_playing(self, is_playing: bool):
        self._is_playing = is_playing

    # ── Icon painting (hand-drawn gear, see _GearIcon docstring) ─────────────

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(COLORS["accent"]) if self.underMouse() else QColor(COLORS["text_dim"])

        rim_r     = 6.5   # gear body radius
        hole_r    = 3.0   # punched-out center hole
        tooth_len = 3.0   # how far each tooth sticks out past the rim
        tooth_w   = 4.4   # tooth width
        corner_r  = 1.3   # rounded tooth corners — matches the reference's
                          # soft rounded-tooth look, not sharp/blocky
        n_teeth   = 6

        # Rim + all teeth unioned into one silhouette, then the center hole
        # subtracted out via real boolean path ops — this is what makes it
        # read as a hollow gear (like the reference) instead of a solid
        # blob or, with the wrong proportions, a sun. Using subtracted()
        # rather than painting a background-colored circle over the middle
        # also means the hole is genuinely transparent — it shows whatever
        # is actually behind the button (hover highlight included) instead
        # of a hardcoded fill that could mismatch.
        gear = QPainterPath()
        gear.addEllipse(QRectF(-rim_r, -rim_r, rim_r * 2, rim_r * 2))

        tooth = QPainterPath()
        tooth.addRoundedRect(
            QRectF(-tooth_w / 2, -(rim_r + tooth_len), tooth_w, tooth_len + 1.5),
            corner_r, corner_r)

        for i in range(n_teeth):
            t = QTransform()
            t.rotate(360 / n_teeth * i)
            gear = gear.united(t.map(tooth))

        hole = QPainterPath()
        hole.addEllipse(QRectF(-hole_r, -hole_r, hole_r * 2, hole_r * 2))
        gear = gear.subtracted(hole)

        p.translate(self.width() / 2, self.height() / 2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(color))
        p.drawPath(gear)
        p.end()

    # ── Menu construction ────────────────────────────────────────────────────

    def _show_menu(self):
        if self._game_id is None:
            return
        game = db.get_game(self._game_id)
        if not game or not game["is_installed"]:
            return  # per spec: cogwheel never shows for uninstalled games

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

        exe_path = (game["exe_path"] or "").strip()
        wine_prefix = (game["wine_prefix"] or "").strip()
        is_detected = bool(exe_path and Path(exe_path).exists())

        if not is_detected:
            warn_action = menu.addAction("⚠  Not fully set up — fix paths…")
            warn_action.triggered.connect(lambda: self._open_install_paths(game))
            menu.addSeparator()

        if wine_prefix and Path(wine_prefix).exists():
            manage_action = menu.addAction("Manage Install…  (Proton, Redistributables)")
            manage_action.triggered.connect(lambda: self._open_manage_install(game))

        # "Launch with Debug Log" — only shown when a Play button would actually be
        # available (same is_detected gate game_detail.py uses to show/hide Launch),
        # since there's nothing to debug-launch otherwise. Separate from the normal
        # Play button — this is the only path that captures stdout/stderr to a log
        # file; the normal Play button never logs. See debug_launch.py.
        if is_detected:
            debug_launch_action = menu.addAction("🐞  Launch with Debug Log")
            debug_launch_action.triggered.connect(
                lambda: self.debug_launch_requested.emit(self._game_id))

        # "Force Quit" — only shown while this specific game is currently
        # running (Currently Playing Indicator). Killing the tracked
        # process is enough; the PlaytimeWatcher already blocked waiting
        # on it detects the exit and records the session exactly as a
        # normal quit would — see playtime.PlaytimeWatcher.request_kill().
        if self._is_playing:
            force_quit_action = menu.addAction("⛔  Force Quit")
            force_quit_action.triggered.connect(
                lambda: self.force_quit_requested.emit(self._game_id))
            menu.addSeparator()

        folder_action = menu.addAction("Open in File Manager")
        folder_action.setEnabled(bool(game["install_path"]))
        folder_action.triggered.connect(lambda: self._open_folder(game))

        # ── Save Files section — only when Save Backup is enabled ────────────
        if db.get_setting("save_backup_enabled", "false") == "true":
            menu.addSeparator()
            save_menu = menu.addMenu("Save Files")
            self._build_save_files_menu(save_menu, game)

        menu.addSeparator()
        paths_action = menu.addAction("Set Executable / Prefix / Install Paths…")
        paths_action.triggered.connect(lambda: self._open_install_paths(game))

        # Launch Options now lives inside Manage Install, above.
        # Launch with Debug Log and Force Quit are both wired in above.

        menu.exec(self.mapToGlobal(QPoint(0, self.height() + 4)))

    def _build_save_files_menu(self, save_menu: QMenu, game):
        game_id = game["id"]
        save_paths = db.get_save_paths(game_id)
        source_path = save_paths.get("save_source_path")
        save_path   = save_paths.get("save_path")
        diag = save_backup.diagnose_source_path(source_path, save_path)

        backup_action = save_menu.addAction(
            "Back Up Save Now…" if diag == "unset" else "Re-Link Save Location…")
        backup_action.triggered.connect(lambda: self._manual_backup_save(game))

        relink_action = save_menu.addAction("Relink to Backup")
        relink_action.setEnabled(diag in ("missing", "plain_folder", "wrong_symlink"))
        relink_action.triggered.connect(lambda: self._relink_save(game, source_path, save_path))

        open_action = save_menu.addAction("Open Save Backup Directory")
        open_action.setEnabled(bool(save_path and Path(save_path).exists()))
        open_action.triggered.connect(lambda: self._open_path(save_path))

    # ── Action handlers ──────────────────────────────────────────────────────

    def _open_install_paths(self, game):
        dlg = InstallPathsDialog(game, parent=self)
        dlg.saved.connect(self.game_changed)
        dlg.exec()

    def _open_manage_install(self, game):
        dlg = ManageInstallDialog(game, parent=self)
        dlg.changed.connect(self.game_changed)
        dlg.exec()

    def _open_folder(self, game):
        install_path = (game["install_path"] or "").strip()
        if not install_path or not Path(install_path).exists():
            QMessageBox.information(
                self, "Open Folder",
                "Install folder not found. It may have been moved or deleted.")
            return
        self._open_path(install_path)

    def _open_path(self, path: str):
        if not path:
            return
        try:
            subprocess.Popen(["xdg-open", path])
        except Exception as e:
            QMessageBox.warning(self, "Open Folder", f"Could not open folder:\n{e}")

    def _manual_backup_save(self, game):
        """
        Manual variant of Save Backup's Flow 1 — the user points VaultPlay
        directly at the folder holding saves (inside the Wine prefix)
        instead of waiting for the automatic post-play detection to fire.
        Reuses move_and_link()'s conflict handling so this can never
        silently clobber an existing backup, same guarantee the automatic
        flow gives.
        """
        game_id = game["id"]
        wine_prefix = (game["wine_prefix"] or "").strip()
        if not wine_prefix or not Path(wine_prefix).exists():
            QMessageBox.warning(self, "Save Files",
                                "Wine prefix not found — set it first.")
            return

        launch_cmd = _safe_get(game, "launch_cmd", "") or ""
        wine_bin = install_mod.parse_wine_bin_from_cmd(launch_cmd)
        actual_prefix = install_mod._resolve_actual_prefix(Path(wine_prefix), wine_bin)
        start_dir = str(actual_prefix / "drive_c") if (actual_prefix / "drive_c").exists() \
            else str(actual_prefix)

        chosen = QFileDialog.getExistingDirectory(
            self, "Select the folder containing this game's save files", start_dir)
        if not chosen:
            return

        save_root = Path(db.get_setting(
            "save_backup_root", str(Path.home() / "Documents" / "Game Saves")))

        try:
            canonical = save_backup.move_and_link(Path(chosen), save_root, game["folder_name"])
        except save_backup.SaveMoveConflict as e:
            reply = QMessageBox.question(
                self, "Save Files",
                f"A backed-up save already exists at:\n{e.canonical_path}\n\n"
                "Continuing will overwrite it with the save you just picked. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            if reply != QMessageBox.StandardButton.Yes:
                return
            try:
                canonical = save_backup.move_and_link(
                    Path(chosen), save_root, game["folder_name"], overwrite_confirmed=True)
            except Exception as e2:
                QMessageBox.warning(self, "Save Files", f"Backup failed:\n{e2}")
                return
        except Exception as e:
            QMessageBox.warning(self, "Save Files", f"Backup failed:\n{e}")
            return

        db.set_save_paths(game_id, save_path=str(canonical), save_source_path=str(chosen))
        QMessageBox.information(self, "Save Files", f"✓ Save backed up to:\n{canonical}")
        self.game_changed.emit(game_id)

    def _relink_save(self, game, source_path, save_path):
        if not source_path or not save_path:
            return
        ok = save_backup.repair_link(source_path, save_path)
        if ok:
            QMessageBox.information(self, "Save Files", "✓ Save location relinked.")
            self.game_changed.emit(game["id"])
        else:
            QMessageBox.warning(
                self, "Save Files",
                "Couldn't relink automatically. Check the app log for details.")
