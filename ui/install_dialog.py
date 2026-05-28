"""
ui/install_dialog.py — Install dialog for VaultPlay

Three modes driven by install_tag:
  installer → Wine prefix + redists + run setup.exe
  iso       → Wine prefix + redists + mount ISO + run setup.exe
  portable  → Wine prefix + redists + copy files + game path + .desktop
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
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QCheckBox, QComboBox, QWidget, QProgressBar,
    QScrollArea, QLineEdit
)
from PyQt6.QtCore import Qt, pyqtSignal, QThread
from PyQt6.QtGui import QFont, QColor

import db
import installer as install_mod
import protondb as protondb_mod
import scanner
from ui.style import COLORS, accent_button_style

log = logging.getLogger(__name__)

COMMON_REDISTS = install_mod.COMMON_REDISTS

AUTO_DETECT_REDISTS = ["vcrun2019", "vcrun2022", "d3dx11", "d3dcompiler_47"]


TAG_LABELS = {
    "installer": "Installer  (.exe setup)",
    "portable":  "Portable   (copy + .desktop)",
    "iso":       "ISO        (mount + setup)",
}


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
            btn.clicked.connect(lambda _, v=value: self._select(v))
            self._buttons[value] = btn
            layout.addWidget(btn)
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


# ── Worker ────────────────────────────────────────────────────────────────────

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


# ── Dialog ────────────────────────────────────────────────────────────────────

class InstallDialog(QDialog):
    install_finished = pyqtSignal(int)

    def __init__(self, game, parent=None):
        super().__init__(parent)
        self.game    = game
        self._worker = None
        self._install_tag = game["install_tag"] or "portable"

        title_str = game["title"] or game["display_name"] or game["folder_name"]

        self.setWindowTitle(f"Install — {title_str}")
        self.setModal(True)
        self.setFixedWidth(540)
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

        size_str = scanner.format_size(game["size_bytes"] or 0)
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

        default_prefix = install_mod.make_prefix_name(game["folder_name"])
        self.prefix_edit = QLineEdit(default_prefix)
        self.prefix_edit.setFont(QFont("DM Mono", 10))
        self.prefix_edit.setStyleSheet(f"color: {COLORS['accent2']};")
        self.prefix_edit.setPlaceholderText("e.g. thewitcher3")

        pcol = QVBoxLayout()
        pcol.setSpacing(4)
        pcol.setContentsMargins(0, 0, 0, 0)
        pcol.addWidget(plbl)
        pcol.addWidget(self.prefix_edit)

        prefix_path_lbl = QLabel(
            f"→  ~/.local/share/wineprefixes/<name>")
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
        self.gamepath_combo = QComboBox()
        self.gamepath_combo.setFont(QFont("DM Sans", 11))
        self._populate_game_paths()
        self.gamepath_box.add(self.gamepath_combo)
        self.body_layout.addWidget(self.gamepath_box)

        # ── Launcher type (portables only) ────────────────────────────────────
        self.launcher_box = SectionBox("Launcher Type")
        self.launcher_toggle = ToggleGroup(
            [("Direct (.desktop → wine)", "direct"),
             ("Script wrapper (.sh + .desktop)", "script")],
            default="direct"
        )
        self.launcher_box.add(self.launcher_toggle)

        launcher_note = QLabel(
            "Script wrapper is easier to tweak later "
            "(env vars, DXVK flags, etc.)")
        launcher_note.setFont(QFont("DM Sans", 10))
        launcher_note.setStyleSheet(
            f"color: {COLORS['text_muted']}; background: transparent; border: none;")
        launcher_note.setWordWrap(True)
        self.launcher_box.add(launcher_note)
        self.body_layout.addWidget(self.launcher_box)

        # ── Proton / Wine version ─────────────────────────────────────────────
        # ── Dynamic Proton/Wine version detection ─────────────────────────────
        tier      = game.get("protondb_tier", "") or ""
        reports   = game.get("protondb_reports", 0) or 0
        pdb_str   = ""
        if tier:
            tier_label, _ = protondb_mod.tier_display(tier)
            pdb_str = f"  ·  ProtonDB: {tier_label} ({reports} reports)"
        proton_box = SectionBox(f"Proton / Wine Version{pdb_str}")

        # Scan what's actually installed
        self._installed_versions = protondb_mod.get_versions_for_ui()

        proton_note = QLabel("")
        proton_note.setFont(QFont("DM Sans", 10))
        proton_note.setStyleSheet(
            f"color: {COLORS['text_muted']}; background: transparent; border: none;")
        proton_note.setWordWrap(True)

        self.proton_combo = QComboBox()
        self.proton_combo.setFont(QFont("DM Sans", 11))

        self._recommended_proton = game.get("recommended_proton") or None

        if not self._installed_versions:
            proton_note.setText(
                "⚠ No Proton/Wine versions found. Install via ProtonUp-Qt, "
                "Lutris, or from GitHub (GloriousEggroll/proton-ge-custom).")
            self.proton_combo.addItem("No versions found", "")
        else:
            # Determine which version to recommend
            sgdb_id = game.get("sgdb_id")
            rec_val = self._recommended_proton
            rec_label_suffix = ""

            if sgdb_id and not rec_val:
                # Try live lookup
                try:
                    rec_val, rec_reason = protondb_mod.recommended_proton_for_game(
                        sgdb_id, self._installed_versions)
                    rec_label_suffix = f" ✦ Recommended"
                    proton_note.setText(f"ProtonDB: {rec_reason}")
                    proton_note.setStyleSheet(
                        "color: #4ade80; background: transparent; border: none;")
                except Exception:
                    pass

            default_val = rec_val or db.get_setting("default_proton_version", "")
            if not default_val and self._installed_versions:
                default_val = self._installed_versions[0][1]

            for label, val in self._installed_versions:
                display = f"{label}{rec_label_suffix}" if val == rec_val else label
                self.proton_combo.addItem(display, val)
                if val == rec_val:
                    self.proton_combo.setItemData(
                        self.proton_combo.count() - 1,
                        QColor("#e8c76a"),
                        Qt.ItemDataRole.ForegroundRole)

            # Select recommended/default
            idx = self.proton_combo.findData(default_val)
            if idx >= 0:
                self.proton_combo.setCurrentIndex(idx)

        proton_box.add(self.proton_combo)
        proton_box.add(proton_note)
        self.body_layout.addWidget(proton_box)

        # ── Redistributables (detected via Steam API) ─────────────────────────
        # Fetch redists in background - start with baseline while loading
        self._auto_redists = AUTO_DETECT_REDISTS[:]
        self._redist_source = "Loading…"

        sgdb_id     = game.get("sgdb_id")
        game_title2 = game.get("title") or game.get("display_name") or ""
        auto_detect = db.get_setting("auto_detect_redists", "true") == "true"

        if auto_detect and sgdb_id:
            try:
                import steamdb as steamdb_mod
                result = steamdb_mod.get_redists_for_install(
                    sgdb_id, game_title2, auto_detect=True)
                self._auto_redists  = result["auto"]
                self._redist_source = result["source"]
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(
                    "SteamDB redist detection failed: %s", e)

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

        # ── Cleanup toggle ────────────────────────────────────────────────────
        cleanup_box = SectionBox()
        cl_row = QHBoxLayout()
        cl_row.setContentsMargins(0, 0, 0, 0)
        cl_inner = QVBoxLayout()
        cl_inner.setSpacing(2)
        cl_t = QLabel("Clean up extracted files after install")
        cl_t.setFont(QFont("DM Sans", 12, QFont.Weight.Medium))
        cl_t.setStyleSheet(f"color: {COLORS['text']}; background: transparent; border: none;")
        cl_s = QLabel("Removes temp files from .tmp after completion")
        cl_s.setFont(QFont("DM Sans", 10))
        cl_s.setStyleSheet(f"color: {COLORS['text_muted']}; background: transparent; border: none;")
        cl_inner.addWidget(cl_t)
        cl_inner.addWidget(cl_s)
        self.cleanup_check = QCheckBox()
        self.cleanup_check.setChecked(
            db.get_setting("auto_cleanup_tmp", "true") == "true")
        cl_row.addLayout(cl_inner)
        cl_row.addStretch()
        cl_row.addWidget(self.cleanup_check)
        cleanup_box.inner_layout().addLayout(cl_row)
        self.body_layout.addWidget(cleanup_box)

        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        # ── Progress panel ────────────────────────────────────────────────────
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
        self.install_btn = QPushButton("Begin Install →")
        self.install_btn.setFont(QFont("Rajdhani", 13, QFont.Weight.Bold))
        self.install_btn.setStyleSheet(accent_button_style())
        self.install_btn.clicked.connect(self._start_install)
        foot_l.addWidget(self.install_btn)
        root.addWidget(footer)

        self.setMinimumHeight(520)
        self.setMaximumHeight(820)
        self._on_tag_changed(self._install_tag)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _populate_game_paths(self):
        self.gamepath_combo.clear()
        for path in db.get_install_paths():
            self.gamepath_combo.addItem(path, path)

    def _on_tag_changed(self, tag):
        self._install_tag = tag
        is_portable = (tag == "portable")
        self.gamepath_box.setVisible(is_portable)
        self.launcher_box.setVisible(is_portable)

    def _start_install(self):
        self.install_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self.progress_panel.show()
        self.adjustSize()

        options = {
            "wine_prefix_name": self.prefix_edit.text().strip() or
                                 install_mod.make_prefix_name(self.game["folder_name"]),
            "game_path":        self.gamepath_combo.currentData()
                                 if self._install_tag == "portable" else "",
            "launcher_type":    self.launcher_toggle.value
                                 if self._install_tag == "portable" else "direct",
            "redists":          [v for v, cb in self._redist_checks.items() if cb.isChecked()],
            "cleanup_tmp":      self.cleanup_check.isChecked(),
            "proton_version":   self.proton_combo.currentData(),
            # Pass the (possibly overridden) tag
            "install_tag":      self._install_tag,
        }

        # Patch game dict with overridden tag so installer uses it
        game_patched = dict(self.game)
        game_patched["install_tag"] = self._install_tag

        self._worker = InstallWorker(self.game["id"], options)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _on_progress(self, stage, percent, message):
        self.stage_label.setText(stage)
        self.progress_bar.setValue(percent)
        self.progress_msg.setText(message)

    def _on_finished(self, result):
        if result["success"]:
            self.stage_label.setText("✓ Installation complete!")
            self.stage_label.setStyleSheet(
                f"color: {COLORS['installed']}; font-weight: 600;")
            self.progress_bar.setValue(100)
            self.progress_msg.setText(result.get("exe_path") or "Done.")
            self.cancel_btn.setText("Close")
            self.cancel_btn.setEnabled(True)
            self.install_finished.emit(self.game["id"])
        else:
            self.stage_label.setText("✗ Installation failed")
            self.stage_label.setStyleSheet(
                f"color: {COLORS['danger']}; font-weight: 600;")
            self.progress_msg.setText(result.get("error", "Unknown error"))
            self.cancel_btn.setText("Close")
            self.cancel_btn.setEnabled(True)
            self.install_btn.setEnabled(True)
            self.install_btn.setText("Retry")
