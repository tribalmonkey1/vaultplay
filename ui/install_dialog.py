"""
ui/install_dialog.py — Install dialog for VaultPlay

Three modes driven by install_tag:
  installer → Wine prefix + redists + run setup.exe
  iso       → Wine prefix + redists + mount ISO + run setup.exe
  portable  → Wine prefix + redists + copy files + game path + .desktop

Proton version section:
  - Defaults to the top-voted version from ProtonDB community reports
  - Shows per-version report counts next to each option in the dropdown
  - Uninstalled versions that have report counts appear in the dropdown
    but are marked "(not installed)"
  - If an uninstalled version is selected, Begin Install is replaced by a
    Download button. GE-Proton downloads inline with progress; official
    Proton opens Steam's install dialog via steam:// URL.
  - Begin Install is blocked until the selected version is installed.
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

import json
import logging
import shutil
import subprocess
from pathlib import Path

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QCheckBox, QComboBox, QWidget, QProgressBar,
    QScrollArea, QLineEdit, QMessageBox, QSizePolicy
)
from PyQt6.QtCore import Qt, pyqtSignal, QThread
from PyQt6.QtGui import QFont, QColor

import db
import installer as install_mod
import protondb as protondb_mod
import scanner
import steamdb as steamdb_mod
from ui.style import COLORS, accent_button_style

log = logging.getLogger(__name__)

COMMON_REDISTS = install_mod.COMMON_REDISTS

TAG_LABELS = {
    "installer": "Installer  (.exe setup)",
    "portable":  "Portable   (copy + .desktop)",
    "iso":       "ISO        (mount + setup)",
}

_ADDON_TYPE_LABELS = {
    "update":           "Update",
    "installable_dlc":  "DLC",
    "crackfix":         "Crackfix",
}


def _addon_display_label(addon) -> str:
    """Build the checklist row label for one game_addons row."""
    type_label = _ADDON_TYPE_LABELS.get(addon["addon_type"], addon["addon_type"])
    version = (addon["detected_version_dotted"] or addon["detected_version_plain"]
              or addon["detected_version_date"])
    name = addon["archive_name"] or Path(addon["nas_path"]).name
    if version:
        return f"[{type_label}]  v{version}  —  {name}"
    return f"[{type_label}]  {name}"


class SettingsToggle(QWidget):
    """Pill-shaped on/off toggle — matches the style used in settings_view.py."""
    changed = pyqtSignal(bool)

    def __init__(self, checked: bool = False, parent=None):
        super().__init__(parent)
        self.setFixedSize(44, 24)
        self._checked = checked
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, event):
        from PyQt6.QtGui import QPainter, QBrush, QPen
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QColor(COLORS["accent"]) if self._checked else QColor("#3a3f52")
        p.setBrush(QBrush(track))
        p.setPen(QPen(QColor("#555a70"), 1))
        p.drawRoundedRect(0, 0, 44, 24, 12, 12)
        knob = QColor("#ffffff") if self._checked else QColor("#aaaaaa")
        p.setBrush(QBrush(knob))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(23 if self._checked else 3, 3, 18, 18)
        p.end()

    def mousePressEvent(self, event):
        self._checked = not self._checked
        self.update()
        self.changed.emit(self._checked)

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, val: bool):
        self._checked = val
        self.update()


class NoScrollComboBox(QComboBox):
    """QComboBox that ignores scroll wheel events to prevent accidental changes."""
    def wheelEvent(self, event):
        event.ignore()


# ── Toggle group ──────────────────────────────────────────────────────────────

class ToggleGroup(QWidget):
    changed = pyqtSignal(str)

    def __init__(self, options, default=None, parent=None):
        super().__init__(parent)
        self._buttons = {}
        self._selected = default or options[0][1]
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        for label, value in options:
            btn = QPushButton(label)
            btn.setFont(QFont("DM Sans", 11))
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.clicked.connect(lambda _, v=value: self._select(v))
            self._buttons[value] = btn
            layout.addWidget(btn, 1)
        self._update_styles()

    def _select(self, value):
        self._selected = value
        self._update_styles()
        self.changed.emit(value)

    def _update_styles(self):
        for val, btn in self._buttons.items():
            if val == self._selected:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: rgba(232,199,106,0.12);
                        border: 1px solid rgba(232,199,106,0.4);
                        border-radius: 6px; color: {COLORS['accent']};
                        padding: 7px 14px; font-weight: 500;
                    }}""")
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: {COLORS['surface']};
                        border: 1px solid {COLORS['border']};
                        border-radius: 6px; color: {COLORS['text_muted']};
                        padding: 7px 14px;
                    }}
                    QPushButton:hover {{
                        color: {COLORS['text']}; background: {COLORS['surface2']};
                    }}""")

    @property
    def value(self):
        return self._selected

    def set_value(self, val):
        self._selected = val
        self._update_styles()


# ── Section box ───────────────────────────────────────────────────────────────

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
                " background: transparent; border: none;")
            self._inner.addWidget(lbl)

    def add(self, widget):
        self._inner.addWidget(widget)
        return widget

    def inner_layout(self):
        return self._inner


# ── GE-Proton download worker ─────────────────────────────────────────────────

class GEDownloadWorker(QThread):
    progress = pyqtSignal(str, int, str)   # stage, percent, message
    finished = pyqtSignal(bool, str)       # success, message

    def __init__(self, tag: str, download_url: str):
        super().__init__()
        self.tag          = tag
        self.download_url = download_url

    def run(self):
        ok, msg = protondb_mod.download_ge_proton(
            self.tag, self.download_url,
            progress_cb=lambda stage, pct, m: self.progress.emit(stage, pct, m)
        )
        self.finished.emit(ok, msg)


# ── Install worker ────────────────────────────────────────────────────────────

class InstallWorker(QThread):
    progress = pyqtSignal(str, int, str)
    finished = pyqtSignal(dict)

    def __init__(self, game_id, options):
        super().__init__()
        self.game_id = game_id
        self.options = options

    def run(self):
        result = install_mod.run_install(
            self.game_id, self.options,
            progress_cb=lambda s, p, m: self.progress.emit(s, p, m)
        )
        self.finished.emit(result)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_ge_proton_key(canonical_key: str) -> bool:
    return bool(canonical_key) and "ge-proton" in canonical_key.lower()


def _is_official_proton_key(canonical_key: str) -> bool:
    if not canonical_key:
        return False
    k = canonical_key.lower()
    return (
        "ge" not in k and (
            k == "experimental"
            or bool(__import__("re").match(r"\d+\.\d+", k))
            or bool(__import__("re").match(r"\d+$", k))
        )
    )


def _canonical_to_value_key(canonical: str) -> str:
    """Convert a canonical count key (e.g. 'GE-Proton 10') to the installed value key format."""
    return canonical.lower().replace(" ", "-")


# ── Dialog ────────────────────────────────────────────────────────────────────

class InstallDialog(QDialog):
    install_finished = pyqtSignal(int)

    def __init__(self, game, parent=None):
        super().__init__(parent)
        self.game         = game
        self._worker      = None
        self._dl_worker   = None
        self._install_tag = game.get("install_tag") or "portable"

        # Proton state
        self._installed_versions: list[tuple[str, str]] = []
        self._version_counts: dict = {}    # canonical_key → count
        self._selected_canonical: str = "" # canonical key of current combo selection
        self._selected_is_installed: bool = True

        title_str = game.get("title") or game.get("display_name") or game.get("folder_name", "")

        self.setWindowTitle(f"Install — {title_str}")
        self.setModal(True)
        self.setMinimumWidth(600)
        self.setFixedWidth(640)
        self.setStyleSheet(f"QDialog {{ background: {COLORS['surface']}; }}")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Header ────────────────────────────────────────────────────────────
        hdr = QWidget()
        hdr.setStyleSheet(f"background: {COLORS['surface']};")
        hdr_l = QVBoxLayout(hdr)
        hdr_l.setContentsMargins(24, 20, 24, 12)
        hdr_l.setSpacing(3)

        h_title = QLabel("Install Game")
        h_title.setFont(QFont("Rajdhani", 18, QFont.Weight.Bold))
        hdr_l.addWidget(h_title)

        size_str = scanner.format_size(game.get("size_bytes") or 0)
        tag_label = TAG_LABELS.get(self._install_tag, self._install_tag)
        h_sub = QLabel(f"{title_str}  ·  {size_str}  ·  {tag_label}")
        h_sub.setFont(QFont("DM Sans", 11))
        h_sub.setStyleSheet(f"color: {COLORS['text_muted']};")
        hdr_l.addWidget(h_sub)
        root.addWidget(hdr)

        # ── Scrollable body ───────────────────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        body = QWidget()
        body.setStyleSheet(f"background: {COLORS['surface']};")
        self.body_layout = QVBoxLayout(body)
        self.body_layout.setContentsMargins(24, 8, 24, 16)
        self.body_layout.setSpacing(12)

        # ── Install tag override ──────────────────────────────────────────────
        tag_box = SectionBox(f"Install Type  ·  Auto-detected: {tag_label}")
        self.tag_toggle = ToggleGroup(
            [("Installer", "installer"), ("Portable", "portable"), ("ISO", "iso")],
            default=self._install_tag
        )
        self.tag_toggle.changed.connect(self._on_tag_changed)
        tag_box.add(self.tag_toggle)
        self.body_layout.addWidget(tag_box)

        # ── Wine prefix ───────────────────────────────────────────────────────
        prefix_box = SectionBox("Wine Prefix")
        prow = QHBoxLayout()
        prow.setContentsMargins(0, 0, 0, 0)
        prow.setSpacing(10)

        plbl = QLabel("Prefix Name")
        plbl.setFont(QFont("DM Mono", 8))
        plbl.setStyleSheet(
            f"color: {COLORS['text_muted']}; letter-spacing: 1px;"
            " background: transparent; border: none;")

        default_prefix = install_mod.make_prefix_name(game.get("folder_name", ""))
        self.prefix_edit = QLineEdit(default_prefix)
        self.prefix_edit.setFont(QFont("DM Mono", 10))
        self.prefix_edit.setStyleSheet(f"color: {COLORS['accent2']};")
        self.prefix_edit.setPlaceholderText("e.g. thewitcher3")

        pcol = QVBoxLayout()
        pcol.setSpacing(4)
        pcol.setContentsMargins(0, 0, 0, 0)
        pcol.addWidget(plbl)
        pcol.addWidget(self.prefix_edit)

        prefix_path_lbl = QLabel("→  ~/.local/share/wineprefixes/<name>")
        prefix_path_lbl.setFont(QFont("DM Mono", 9))
        prefix_path_lbl.setStyleSheet(
            f"color: {COLORS['text_muted']}; background: transparent; border: none;")

        pcol2 = QVBoxLayout()
        pcol2.setSpacing(4)
        pcol2.setContentsMargins(0, 0, 0, 0)
        pcol2.addSpacing(16)
        pcol2.addWidget(prefix_path_lbl)

        prow.addLayout(pcol)
        prow.addLayout(pcol2)
        prefix_box.inner_layout().addLayout(prow)
        self.body_layout.addWidget(prefix_box)

        # ── Game path (portables only) ────────────────────────────────────────
        self.gamepath_box = SectionBox("Game Install Path")
        self.gamepath_combo = NoScrollComboBox()
        self.gamepath_combo.setFont(QFont("DM Sans", 11))
        self._populate_game_paths()
        self.gamepath_box.add(self.gamepath_combo)
        self.body_layout.addWidget(self.gamepath_box)

        # ── Proton / Wine version ─────────────────────────────────────────────
        tier    = game.get("protondb_tier", "") or ""
        reports = game.get("protondb_reports", 0) or 0
        pdb_hdr = f"Proton / Wine Version"
        if tier:
            tier_label, _ = protondb_mod.tier_display(tier)
            pdb_hdr += f"  ·  ProtonDB: {tier_label} ({reports} reports)"

        self.proton_box = SectionBox(pdb_hdr)

        self.proton_combo = NoScrollComboBox()
        self.proton_combo.setFont(QFont("DM Sans", 11))

        # Status note below the combo
        self.proton_note = QLabel("")
        self.proton_note.setFont(QFont("DM Sans", 10))
        self.proton_note.setStyleSheet(
            f"color: {COLORS['text_muted']}; background: transparent; border: none;")
        self.proton_note.setWordWrap(True)

        self.proton_box.add(self.proton_combo)
        self.proton_box.add(self.proton_note)
        self.body_layout.addWidget(self.proton_box)

        # ── Redistributables ─────────────────────────────────────────────────
        # Detection priority: redists.json → appinfo.vdf → pc_requirements
        # text → engine hints → baseline. See steamdb.py for full details.

        self._auto_redists  = steamdb_mod.BASELINE_REDISTS[:]
        self._redist_source = "baseline"

        steam_app_id = game.get("steam_app_id")
        game_title2  = game.get("title") or game.get("display_name") or ""
        auto_detect  = db.get_setting("auto_detect_redists", "true") == "true"

        if auto_detect and steam_app_id:
            try:
                result = steamdb_mod.get_redists_for_install(
                    steam_app_id, game_title2, auto_detect=True)
                self._auto_redists  = result["auto"]
                self._redist_source = result["source"]
            except Exception as _e:
                log.warning("SteamDB redist detection failed: %s", _e)
                self._auto_redists  = steamdb_mod.BASELINE_REDISTS[:]
                self._redist_source = "baseline (detection error)"
        elif not steam_app_id:
            self._auto_redists  = steamdb_mod.BASELINE_REDISTS[:]
            self._redist_source = "baseline (no Steam ID — metadata may need fetching)"

        n_auto = len(self._auto_redists)
        redist_box = SectionBox(
            f"Redistributables  ·  {n_auto} detected  ·  {self._redist_source}")
        self._redist_checks = {}
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
            is_auto = verb in self._auto_redists
            cb.setChecked(is_auto)
            cb.setStyleSheet(
                f"color: {COLORS['accent2']};" if is_auto
                else f"color: {COLORS['text_dim']};"
            )
            self._redist_checks[verb] = cb
            row_l = QHBoxLayout()
            row_l.addWidget(cb)
            row_l.addStretch()
            (col1 if i % 2 == 0 else col2).addLayout(row_l)
        grid_l.addLayout(col1)
        grid_l.addLayout(col2)
        redist_box.add(grid_w)
        self.body_layout.addWidget(redist_box)

        # ── Updates & DLC ────────────────────────────────────────────────────
        # One row per pending update/installable-DLC/crackfix addon found for
        # this game by the scanner — same visual pattern as Redistributables
        # above. Bonus content is never shown here — it has no install
        # pipeline at all, only a separate manual extract action in game
        # detail. Already-installed addons are excluded (nothing to offer on
        # a fresh install; on a reinstall they'd already show as applied).
        self._addon_checks = {}
        pending_addons = []
        try:
            pending_addons = [
                a for a in db.get_addons_for_game(game.get("id"))
                if a["addon_type"] in ("update", "installable_dlc", "crackfix")
                and not a["installed"]
            ]
        except Exception as _e:
            log.warning("Could not load addons for install dialog: %s", _e)

        if pending_addons:
            _type_order = {"update": 0, "installable_dlc": 1, "crackfix": 2}
            pending_addons.sort(
                key=lambda a: (_type_order.get(a["addon_type"], 9),
                               a["archive_name"] or "")
            )
            addon_box = SectionBox(f"Updates & DLC  ·  {len(pending_addons)} found")
            addon_w = QWidget()
            addon_w.setStyleSheet("background: transparent; border: none;")
            addon_col = QVBoxLayout(addon_w)
            addon_col.setContentsMargins(0, 0, 0, 0)
            addon_col.setSpacing(4)
            for addon in pending_addons:
                cb = QCheckBox(_addon_display_label(addon))
                cb.setFont(QFont("DM Mono", 10))
                cb.setChecked(True)
                cb.setStyleSheet(f"color: {COLORS['accent2']};")
                self._addon_checks[addon["id"]] = cb
                row_l = QHBoxLayout()
                row_l.addWidget(cb)
                row_l.addStretch()
                addon_col.addLayout(row_l)
            addon_box.add(addon_w)
            self.body_layout.addWidget(addon_box)

        # ── Cleanup toggle ────────────────────────────────────────────────────
        cleanup_box = SectionBox()
        cl_row = QHBoxLayout()
        cl_row.setContentsMargins(0, 0, 0, 0)
        cl_row.setSpacing(12)
        cl_inner = QVBoxLayout()
        cl_inner.setSpacing(2)
        cl_inner.setContentsMargins(0, 0, 0, 0)
        cl_t = QLabel("Clean up extracted files after install")
        cl_t.setFont(QFont("DM Sans", 12, QFont.Weight.Medium))
        cl_t.setStyleSheet(f"color: {COLORS['text']}; background: transparent; border: none;")
        cl_s = QLabel("Removes temp files from .tmp after completion")
        cl_s.setFont(QFont("DM Sans", 10))
        cl_s.setStyleSheet(f"color: {COLORS['text_muted']}; background: transparent; border: none;")
        cl_inner.addWidget(cl_t)
        cl_inner.addWidget(cl_s)
        self.cleanup_check = SettingsToggle(
            checked=db.get_setting("auto_cleanup_tmp", "true") == "true")
        cl_row.addLayout(cl_inner)
        cl_row.addStretch()
        cl_row.addWidget(self.cleanup_check, 0, Qt.AlignmentFlag.AlignVCenter)
        cleanup_box.inner_layout().addLayout(cl_row)
        self.body_layout.addWidget(cleanup_box)

        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        # ── Progress panel (download + install) ───────────────────────────────
        self.progress_panel = QWidget()
        self.progress_panel.setStyleSheet(
            f"background: {COLORS['surface2']}; border-top: 1px solid {COLORS['border']};")
        self.progress_panel.hide()
        pp_l = QVBoxLayout(self.progress_panel)
        pp_l.setContentsMargins(24, 14, 24, 14)
        pp_l.setSpacing(6)
        self.stage_label = QLabel("Preparing…")
        self.stage_label.setFont(QFont("DM Sans", 11, QFont.Weight.Medium))
        pp_l.addWidget(self.stage_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setFixedHeight(4)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setStyleSheet(f"""
            QProgressBar {{ background: {COLORS['surface3']}; border-radius: 2px; border: none; }}
            QProgressBar::chunk {{ background: {COLORS['accent']}; border-radius: 2px; }}
        """)
        pp_l.addWidget(self.progress_bar)
        self.progress_msg = QLabel("")
        self.progress_msg.setFont(QFont("DM Mono", 9))
        self.progress_msg.setStyleSheet(f"color: {COLORS['text_muted']};")
        pp_l.addWidget(self.progress_msg)
        root.addWidget(self.progress_panel)

        # ── Footer ────────────────────────────────────────────────────────────
        footer = QWidget()
        footer.setStyleSheet(
            f"background: {COLORS['surface']}; border-top: 1px solid {COLORS['border']};")
        foot_l = QHBoxLayout(footer)
        foot_l.setContentsMargins(24, 14, 24, 14)
        foot_l.setSpacing(10)
        foot_l.addStretch()

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        foot_l.addWidget(self.cancel_btn)

        # Primary action button — swaps between Download and Begin Install
        self.install_btn = QPushButton("Begin Install →")
        self.install_btn.setFont(QFont("Rajdhani", 13, QFont.Weight.Bold))
        self.install_btn.setStyleSheet(accent_button_style())
        self.install_btn.clicked.connect(self._on_primary_action)
        foot_l.addWidget(self.install_btn)

        # "I've installed it — check again" button for official Proton Steam flow
        self.check_installed_btn = QPushButton("Check again")
        self.check_installed_btn.setFont(QFont("DM Sans", 11))
        self.check_installed_btn.setStyleSheet(f"""
            QPushButton {{
                background: {COLORS['surface2']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px; color: {COLORS['text_muted']};
                padding: 8px 14px;
            }}
            QPushButton:hover {{ color: {COLORS['text']}; background: {COLORS['surface3']}; }}
        """)
        self.check_installed_btn.clicked.connect(self._check_steam_installed)
        self.check_installed_btn.hide()
        foot_l.addWidget(self.check_installed_btn)

        root.addWidget(footer)

        self.setMinimumHeight(520)
        self.setMaximumHeight(860)

        # Populate Proton combo now that UI is built
        self._populate_proton_combo()
        self._on_tag_changed(self._install_tag)

    # ── Proton combo population ───────────────────────────────────────────────

    def _populate_proton_combo(self):
        """
        Build the Proton version combo box.

        Installed versions are listed first (sorted GE-Proton > Experimental > Proton > Wine).
        Uninstalled versions that appear in ProtonDB report counts are listed after,
        marked "(not installed)". Only uninstalled versions with a non-zero count appear.

        Top-voted version from ProtonDB is pre-selected and shown with its count.
        """
        self.proton_combo.blockSignals(True)
        self.proton_combo.clear()

        self._installed_versions = protondb_mod.get_versions_for_ui()
        installed_values = {v for _, v in self._installed_versions}

        # Load stored version counts from DB
        try:
            raw = self.game.get("protondb_version_counts")
            self._version_counts = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, Exception):
            self._version_counts = {}

        rec_canonical = self.game.get("recommended_proton") or ""
        total_reports = self.game.get("protondb_reports", 0) or 0

        # No versions installed at all
        if not self._installed_versions and not self._version_counts:
            self.proton_combo.addItem("No versions found — install via ProtonUp-Qt", "")
            self._set_proton_note(
                "⚠ No Proton/Wine versions found. Install via ProtonUp-Qt.",
                "danger")
            self.proton_combo.blockSignals(False)
            self._update_footer_buttons()
            return

        # ── No ProtonDB data ──────────────────────────────────────────────────
        if not self._version_counts and not rec_canonical:
            # No reports for this game — use settings default, show red note
            default_val = db.get_setting("default_proton_version", "")
            for label, value in self._installed_versions:
                self.proton_combo.addItem(label, value)
            idx = self.proton_combo.findData(default_val)
            if idx >= 0:
                self.proton_combo.setCurrentIndex(idx)
            elif self._installed_versions:
                self.proton_combo.setCurrentIndex(0)

            if total_reports == 0:
                self._set_proton_note(
                    "No ProtonDB reports found for this game — using your default version.",
                    "danger")
            else:
                self._set_proton_note(
                    "⚠ ProtonDB data unavailable — showing your default version.\n"
                    "If this keeps happening, the ProtonDB endpoint may have changed.",
                    "warning")
            self.proton_combo.blockSignals(False)
            self.proton_combo.currentIndexChanged.connect(self._on_proton_selection_changed)
            self._update_footer_buttons()
            return

        # ── Build combined item list ──────────────────────────────────────────
        # Determine what the top-voted canonical key is
        top_canonical = (
            rec_canonical
            or (protondb_mod.top_version(self._version_counts) if self._version_counts else "")
        )

        # Extract sample size from __sample__ key (stored by fetch_and_store)
        self._sample_size = int(self._version_counts.pop("__sample__", 0))

        # Items: (display_label, value_key, canonical_key, is_installed, count)
        items: list[tuple[str, str, str, bool, int]] = []

        # 1. All installed versions
        for label, value in self._installed_versions:
            matched_canonical = self._find_canonical_for_value(value)
            count = self._version_counts.get(matched_canonical, 0) if matched_canonical else 0
            items.append((label, value, matched_canonical or "", True, count))

        # Sort installed items: versions with counts descending first,
        # then zero-count versions in their original detected order
        items.sort(key=lambda x: (-x[4], self._installed_versions.index((x[0], x[1]))
                                   if (x[0], x[1]) in self._installed_versions else 999))

        # 2. Uninstalled versions that have report counts (count > 0 only),
        #    sorted by count descending
        for canonical, count in sorted(self._version_counts.items(),
                                        key=lambda x: -x[1]):
            if count == 0:
                continue
            matched_val = protondb_mod.match_to_installed(
                canonical, self._installed_versions)
            if matched_val:
                continue  # already in installed list
            display_label = canonical
            value_key = _canonical_to_value_key(canonical)
            items.append((display_label, value_key, canonical, False, count))

        # ── Add items to combo ────────────────────────────────────────────────
        default_idx  = 0
        top_idx      = -1

        for i, (label, value, canonical, is_installed, count) in enumerate(items):
            count_str = f"  ·  {count} reports" if count else ""
            if not is_installed:
                display = f"{label}{count_str}  (not installed)"
            elif canonical == top_canonical and count:
                display = f"{label}{count_str}  ✦"
            elif count:
                display = f"{label}{count_str}"
            else:
                display = label

            self.proton_combo.addItem(display, value)

            if is_installed and canonical == top_canonical:
                self.proton_combo.setItemData(
                    i, QColor("#e8c76a"), Qt.ItemDataRole.ForegroundRole)
                top_idx = i

            if not is_installed:
                self.proton_combo.setItemData(
                    i, QColor(COLORS["text_muted"]), Qt.ItemDataRole.ForegroundRole)

        # Select the top-voted version by default
        if top_idx >= 0:
            default_idx = top_idx
        elif self._installed_versions:
            default_idx = 0

        self.proton_combo.setCurrentIndex(default_idx)
        self.proton_combo.blockSignals(False)
        self.proton_combo.currentIndexChanged.connect(self._on_proton_selection_changed)

        # Set initial note
        self._on_proton_selection_changed(default_idx)

    def _find_canonical_for_value(self, installed_value: str) -> str:
        """
        Given an installed version value key (e.g. 'ge-proton-10.5'),
        find the matching canonical count key (e.g. 'GE-Proton 10') by
        reverse-matching through the counts dict.
        """
        for canonical in self._version_counts:
            matched = protondb_mod.match_to_installed(
                canonical, [(installed_value, installed_value)])
            if matched:
                return canonical
        return ""

    def _on_proton_selection_changed(self, index: int):
        """Called when user changes the combo selection. Updates note and footer buttons."""
        value = self.proton_combo.itemData(index)
        if not value:
            self._selected_canonical = ""
            self._selected_is_installed = False
            self._update_footer_buttons()
            return

        # Is this value in installed versions?
        installed_values = {v for _, v in self._installed_versions}
        self._selected_is_installed = value in installed_values

        # Find canonical
        if self._selected_is_installed:
            self._selected_canonical = self._find_canonical_for_value(value)
        else:
            # Uninstalled version — value IS the canonical-derived key
            self._selected_canonical = value

        count = self._version_counts.get(self._selected_canonical, 0)
        total = self.game.get("protondb_reports", 0) or 0
        sample = getattr(self, "_sample_size", 0)

        if not self._selected_is_installed:
            label = protondb_mod.proton_version_label(value) or self._selected_canonical
            count_str = f" ({count} of {sample} most recent reports)" if count and sample else ""
            self._set_proton_note(
                f"{label}{count_str} — not installed",
                "warning")
        elif self._selected_canonical and count:
            top = protondb_mod.top_version(self._version_counts)
            if self._selected_canonical == top:
                if sample:
                    count_str = f"{count} of {sample} most recent reports"
                else:
                    count_str = f"{count} reports"
                self._set_proton_note(
                    f"Most reported by the community  ·  {count_str}",
                    "good")
            else:
                top_count = self._version_counts.get(top, 0) if top else 0
                if sample:
                    self._set_proton_note(
                        f"{count} of {sample} most recent reports  ·  "
                        f"top choice has {top_count} reports",
                        "muted")
                else:
                    self._set_proton_note(
                        f"{count} reports  ·  top choice has {top_count} reports",
                        "muted")
        else:
            self._set_proton_note("", "muted")

        self._update_footer_buttons()

    def _set_proton_note(self, text: str, style: str = "muted"):
        """Update the proton note label. style: 'good', 'warning', 'danger', 'muted'"""
        self.proton_note.setText(text)
        colors = {
            "good":    "#4ade80",
            "warning": COLORS["accent"],
            "danger":  COLORS["danger"],
            "muted":   COLORS["text_muted"],
        }
        c = colors.get(style, COLORS["text_muted"])
        self.proton_note.setStyleSheet(
            f"color: {c}; background: transparent; border: none;")
        self.proton_note.setVisible(bool(text))

    # ── Footer button management ──────────────────────────────────────────────

    def _update_footer_buttons(self):
        """
        Show the correct primary action button based on current Proton selection:
          - Selected version is installed → "Begin Install →" (active)
          - Selected version is GE-Proton, not installed → "↓ Download GE-Proton X"
          - Selected version is official Proton, not installed → "Install via Steam →"
          - No version / no value → Begin Install (disabled)
        """
        self.check_installed_btn.hide()
        value     = self.proton_combo.currentData()
        canonical = self._selected_canonical

        if not value:
            self.install_btn.setText("Begin Install →")
            self.install_btn.setStyleSheet(accent_button_style())
            self.install_btn.setEnabled(False)
            return

        if self._selected_is_installed:
            self.install_btn.setText("Begin Install →")
            self.install_btn.setStyleSheet(accent_button_style())
            self.install_btn.setEnabled(True)
            return

        # Uninstalled version selected
        if _is_ge_proton_key(canonical):
            label = canonical  # e.g. "GE-Proton 10"
            self.install_btn.setText(f"↓  Download {label}")
            self.install_btn.setStyleSheet(accent_button_style())
            self.install_btn.setEnabled(True)
        else:
            # Official Proton / experimental / other
            self.install_btn.setText("Install via Steam →")
            self.install_btn.setStyleSheet(accent_button_style())
            self.install_btn.setEnabled(True)
            self.check_installed_btn.show()

    # ── Primary action dispatch ───────────────────────────────────────────────

    def _on_primary_action(self):
        if not self._selected_is_installed:
            if _is_ge_proton_key(self._selected_canonical):
                self._start_ge_download()
            else:
                self._open_steam_install()
        else:
            self._start_install()

    # ── GE-Proton download ────────────────────────────────────────────────────

    def _start_ge_download(self):
        """Fetch GE-Proton releases from GitHub and start the download."""
        canonical = self._selected_canonical  # e.g. "GE-Proton 10"
        self.install_btn.setEnabled(False)
        self.install_btn.setText("Fetching release info…")
        self.progress_panel.show()
        self.stage_label.setText("Fetching GE-Proton release list…")
        self.progress_bar.setValue(0)
        self.progress_msg.setText("")
        self.adjustSize()

        # Find the matching release tag
        releases = protondb_mod.get_ge_releases(limit=30)
        if not releases:
            self._show_download_error(
                "Could not fetch GE-Proton releases from GitHub. "
                "Check your internet connection.")
            return

        # Match canonical "GE-Proton 10" to best available tag
        import re
        m = re.search(r"(\d+)", canonical)
        major = int(m.group(1)) if m else 0

        best = None
        best_minor = -1
        for rel in releases:
            tag = rel["tag"]
            tm = re.search(r"GE-Proton(\d+)-(\d+)", tag, re.IGNORECASE)
            if tm and int(tm.group(1)) == major:
                minor = int(tm.group(2))
                if minor > best_minor:
                    best_minor = minor
                    best = rel

        if not best:
            self._show_download_error(
                f"No GE-Proton {major}.x release found on GitHub.")
            return

        self.stage_label.setText(f"Downloading {best['tag']}…")
        self._dl_worker = GEDownloadWorker(best["tag"], best["download_url"])
        self._dl_worker.progress.connect(self._on_dl_progress)
        self._dl_worker.finished.connect(self._on_ge_download_finished)
        self._dl_worker.start()

        # Lock everything during download
        self.proton_combo.setEnabled(False)
        self.cancel_btn.setEnabled(False)

    def _on_dl_progress(self, stage: str, percent: int, message: str):
        self.stage_label.setText(stage)
        self.progress_bar.setValue(percent)
        self.progress_msg.setText(message)

    def _on_ge_download_finished(self, success: bool, message: str):
        self.proton_combo.setEnabled(True)
        self.cancel_btn.setEnabled(True)

        if success:
            self.stage_label.setText(f"✓ {message}")
            self.stage_label.setStyleSheet(f"color: {COLORS['installed']}; font-weight: 600;")
            self.progress_bar.setValue(100)
            self.progress_msg.setText("Ready to install the game.")

            # Rescan installed versions and refresh combo
            protondb_mod.invalidate_installed_cache()
            self._installed_versions = protondb_mod.get_versions_for_ui()

            # Rebuild combo and re-select the now-installed version
            self._populate_proton_combo()

        else:
            self._show_download_error(message)

    def _show_download_error(self, msg: str):
        self.stage_label.setText("✗ Download failed")
        self.stage_label.setStyleSheet(f"color: {COLORS['danger']}; font-weight: 600;")
        self.progress_msg.setText(msg)
        self.install_btn.setEnabled(True)
        self.install_btn.setText(f"↓  Retry Download")

    # ── Official Proton via Steam ─────────────────────────────────────────────

    def _open_steam_install(self):
        """Open Steam's install dialog for an official Proton version."""
        canonical = self._selected_canonical
        url, is_direct = protondb_mod.get_steam_install_url(canonical)

        try:
            if url.startswith("steam://"):
                subprocess.Popen(["xdg-open", url])
                self._set_proton_note(
                    f"Steam install dialog opened for {canonical}. "
                    "Click 'Check again' after Steam finishes downloading.",
                    "warning")
            else:
                subprocess.Popen(["xdg-open", url])
                self._set_proton_note(
                    f"Steam tools page opened. Install {canonical}, "
                    "then click 'Check again'.",
                    "warning")
        except Exception as e:
            self._set_proton_note(
                f"Could not open Steam: {e}. "
                f"Open Steam manually and install {canonical} from Library → Tools.",
                "danger")

        self.install_btn.setText("Waiting — check Steam…")
        self.install_btn.setEnabled(False)
        self.check_installed_btn.show()

    def _check_steam_installed(self):
        """Rescan installed versions and update UI after Steam download."""
        protondb_mod.invalidate_installed_cache()
        self._installed_versions = protondb_mod.get_versions_for_ui()
        installed_values = {v for _, v in self._installed_versions}

        # Check if the previously-selected canonical is now installed
        matched = protondb_mod.match_to_installed(
            self._selected_canonical, self._installed_versions)

        if matched:
            # Found it — refresh combo, select it, show confirmation
            self.progress_panel.show()
            self.stage_label.setText(
                f"✓ {self._selected_canonical} detected — ready to install")
            self.stage_label.setStyleSheet(
                f"color: {COLORS['installed']}; font-weight: 600;")
            self.progress_bar.setValue(100)
            self.progress_msg.setText("")
            self.adjustSize()

            self._populate_proton_combo()
            self.check_installed_btn.hide()
        else:
            self._set_proton_note(
                f"{self._selected_canonical} not detected yet — "
                "Steam may still be downloading. Click 'Check again' when ready.",
                "warning")

    # ── Install ───────────────────────────────────────────────────────────────

    def _start_install(self):
        # Guard: do not allow install if selected version is not installed
        if not self._selected_is_installed:
            self._set_proton_note(
                "Selected version is not installed. Download it first.",
                "danger")
            return

        # If the user overrode the install type, persist it so future scans
        # don't overwrite the choice (install_tag_override=1 in the games table).
        original_tag = self.game.get("install_tag") or "portable"
        if self._install_tag != original_tag:
            try:
                db.set_install_tag_override(self.game["id"], self._install_tag)
            except Exception as _e:
                log.warning("Could not persist install_tag override: %s", _e)

        # Check if this prefix already has some redists installed
        prefix_name = self.prefix_edit.text().strip() or \
                      install_mod.make_prefix_name(self.game.get("folder_name", ""))
        prefix_path = install_mod._wine_prefix_root() / prefix_name
        selected_redists = [v for v, cb in self._redist_checks.items() if cb.isChecked()]
        force_redists = False

        selected_addon_ids = {addon_id for addon_id, cb in self._addon_checks.items()
                              if cb.isChecked()}

        if selected_redists and prefix_path.exists():
            already = install_mod.get_installed_redists(prefix_path)
            overlap = [v for v in selected_redists if v in already]
            if overlap:
                msg = QMessageBox(self)
                msg.setWindowTitle("Redistributables Already Installed")
                msg.setText("Some redistributables are already installed in this prefix.")
                msg.setInformativeText(
                    f"Already installed: {', '.join(overlap)}\n\n"
                    "Skip them (faster) or reinstall everything?"
                )
                skip_btn      = msg.addButton("Skip Already Installed",
                                              QMessageBox.ButtonRole.AcceptRole)
                reinstall_btn = msg.addButton("Reinstall All",
                                              QMessageBox.ButtonRole.ResetRole)
                msg.setDefaultButton(skip_btn)
                msg.setStyleSheet(
                    f"QMessageBox {{ background: {COLORS['surface']}; "
                    f"color: {COLORS['text']}; }}")
                msg.exec()
                force_redists = (msg.clickedButton() == reinstall_btn)

        self.install_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self.check_installed_btn.hide()
        self.proton_combo.setEnabled(False)
        self.tag_toggle.setEnabled(False)
        for cb in self._addon_checks.values():
            cb.setEnabled(False)
        self.progress_panel.show()
        self.adjustSize()

        options = {
            "wine_prefix_name": prefix_name,
            "game_path":        self.gamepath_combo.currentData()
                                 if self._install_tag == "portable" else "",
            "redists":          selected_redists,
            "force_redists":    force_redists,
            "cleanup_tmp":      self.cleanup_check.isChecked(),
            "proton_version":   self.proton_combo.currentData(),
            "install_tag":      self._install_tag,
            "selected_addon_ids": selected_addon_ids,
        }

        self._worker = InstallWorker(self.game["id"], options)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _on_progress(self, stage, percent, message):
        self.stage_label.setText(stage)
        self.progress_bar.setValue(percent)
        self.progress_msg.setText(message)

    def _on_finished(self, result):
        self.cancel_btn.setEnabled(True)

        if result.get("success"):
            failed_redists = result.get("failed_redists") or []
            failed_addons  = result.get("failed_addons") or []
            if failed_redists or failed_addons:
                self.stage_label.setText("⚠ Installed — some items failed")
                self.stage_label.setStyleSheet(
                    f"color: {COLORS['accent']}; font-weight: 600;")
                parts = []
                if failed_redists:
                    parts.append(
                        "Redistributables: " + ", ".join(failed_redists) +
                        ". Retry individually with winetricks, or reopen "
                        "this dialog and use \"Reinstall All\"."
                    )
                if failed_addons:
                    parts.append(
                        "Updates/DLC: " + ", ".join(failed_addons) +
                        ". The base game installed fine — reopen this "
                        "dialog to retry the failed item(s)."
                    )
                self.progress_msg.setText(
                    "The game itself installed fine.\n" + "\n".join(parts)
                )
            else:
                self.stage_label.setText("✓ Installation complete!")
                self.stage_label.setStyleSheet(
                    f"color: {COLORS['installed']}; font-weight: 600;")
                self.progress_msg.setText(result.get("exe_path") or "Done.")
            self.progress_bar.setValue(100)
            self.cancel_btn.setText("Close")
            self.install_finished.emit(self.game["id"])
            return

        # ── Failure / cancellation ─────────────────────────────────────────────
        cancelled = result.get("cancelled", False)
        error_msg = result.get("error", "Unknown error")
        tmp_path  = result.get("tmp_path", "")

        if cancelled:
            self.stage_label.setText("⚠ Installation cancelled")
            self.stage_label.setStyleSheet(
                f"color: {COLORS['accent']}; font-weight: 600;")
        else:
            self.stage_label.setText("✗ Installation failed")
            self.stage_label.setStyleSheet(
                f"color: {COLORS['danger']}; font-weight: 600;")

        self.progress_msg.setText(error_msg)
        self.cancel_btn.setText("Close")
        self.install_btn.setEnabled(True)
        self.install_btn.setText("Retry")

        # Offer to clean up the temp/extracted files if they exist
        if tmp_path and Path(tmp_path).exists():
            msg = QMessageBox(self)
            msg.setWindowTitle("Extracted Files")
            if cancelled:
                msg.setText("The install was cancelled.")
            else:
                msg.setText("The installation failed.")
            msg.setInformativeText(
                f"Extracted files are still in:\n{tmp_path}\n\n"
                "Keep them to skip re-extraction on your next attempt, "
                "or delete them to free disk space."
            )
            keep_btn   = msg.addButton("Keep Files", QMessageBox.ButtonRole.AcceptRole)
            delete_btn = msg.addButton("Delete Files", QMessageBox.ButtonRole.DestructiveRole)
            msg.setDefaultButton(keep_btn)
            msg.setStyleSheet(
                f"QMessageBox {{ background: {COLORS['surface']}; "
                f"color: {COLORS['text']}; }}")
            msg.exec()
            if msg.clickedButton() == delete_btn:
                try:
                    shutil.rmtree(tmp_path)
                    self.progress_msg.setText(
                        error_msg + "\n(Extracted files deleted.)")
                except Exception as e:
                    log.warning("Could not delete tmp: %s", e)

    # ── Misc helpers ──────────────────────────────────────────────────────────

    def _populate_game_paths(self):
        self.gamepath_combo.clear()
        for path in db.get_install_paths():
            self.gamepath_combo.addItem(path, path)

    def _on_tag_changed(self, tag):
        self._install_tag = tag
        is_portable = (tag == "portable")
        self.gamepath_box.setVisible(is_portable)
