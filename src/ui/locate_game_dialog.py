"""
ui/locate_game_dialog.py — "Already installed? Locate Game…" for VaultPlay

Lets the user point VaultPlay at an install they already have (install
folder, Wine/Proton prefix and/or a VaultPlay .desktop file) and re-creates
the library's install record from it. Anything left blank is rebuilt from the
recommended settings a normal install would use. See adopt_install.py.
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
from pathlib import Path

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QWidget, QScrollArea, QFrame, QCheckBox, QFileDialog, QProgressBar,
    QMessageBox, QComboBox,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont, QColor

import json
import re
import subprocess

import db
import installer
import protondb as protondb_mod
import adopt_install
import dbsafety
from ui.style import COLORS, accent_button_style
from ui.install_dialog import (
    SectionBox, NoScrollComboBox, GEDownloadWorker,
    _is_ge_proton_key, _canonical_to_value_key,
)

log = logging.getLogger(__name__)


class _AdoptWorker(QThread):
    progress = pyqtSignal(str, int, str)
    done = pyqtSignal(dict)

    def __init__(self, game_id, plan):
        super().__init__()
        self.game_id = game_id
        self.plan = plan

    def run(self):
        try:
            res = adopt_install.adopt(
                self.game_id, self.plan,
                lambda s, p, m: self.progress.emit(s, int(p), m))
        except Exception as e:   # never let a thread die silently
            log.exception("adopt failed")
            res = {"success": False, "error": str(e), "warnings": []}
        self.done.emit(res)


class _PathRow(QWidget):
    """Label + line edit + Browse button."""
    changed = pyqtSignal()

    def __init__(self, label, placeholder, browse_fn, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent; border: none;")
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(4)
        lbl = QLabel(label)
        lbl.setFont(QFont("DM Mono", 8))
        lbl.setStyleSheet(f"color: {COLORS['text_muted']}; letter-spacing: 1px;")
        col.addWidget(lbl)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.edit = QLineEdit()
        self.edit.setFont(QFont("DM Mono", 10))
        self.edit.setPlaceholderText(placeholder)
        self.edit.editingFinished.connect(self.changed)
        row.addWidget(self.edit, 1)
        btn = QPushButton("Browse…")
        btn.setFont(QFont("DM Sans", 10))
        btn.clicked.connect(lambda: self._browse(browse_fn))
        row.addWidget(btn)
        col.addLayout(row)

    def _browse(self, fn):
        p = fn(self.edit.text().strip())
        if p:
            self.edit.setText(p)
            self.changed.emit()

    def text(self):
        return self.edit.text().strip()

    def setText(self, t):
        self.edit.setText(t or "")


class LocateGameDialog(QDialog):
    def __init__(self, game: dict, parent=None):
        super().__init__(parent)
        self.game = game
        self._worker = None
        self._info = {}
        self._exe = ""
        self._redist_checks = {}
        self._busy = False
        self._proton_user_set = False
        self._src_note = ""
        title = game.get("title") or game.get("display_name") or game.get("folder_name", "")

        self.setWindowTitle(f"Locate Game — {title}")
        self.setModal(True)
        self.setFixedWidth(660)
        self.setMinimumHeight(560)
        self.setMaximumHeight(880)
        self.setStyleSheet(f"QDialog {{ background: {COLORS['surface']}; }}")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        hdr = QWidget()
        hl = QVBoxLayout(hdr)
        hl.setContentsMargins(24, 20, 24, 10)
        hl.setSpacing(3)
        t = QLabel("Already installed?")
        t.setFont(QFont("Rajdhani", 18, QFont.Weight.Bold))
        hl.addWidget(t)
        sub = QLabel(
            f"Point VaultPlay at an existing install of {title}. Fill in what "
            "you have — anything you leave blank is rebuilt from the "
            "recommended settings.")
        sub.setWordWrap(True)
        sub.setFont(QFont("DM Sans", 11))
        sub.setStyleSheet(f"color: {COLORS['text_muted']};")
        hl.addWidget(sub)
        root.addWidget(hdr)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setStyleSheet(f"background: {COLORS['surface']};")
        self.body_l = QVBoxLayout(body)
        self.body_l.setContentsMargins(24, 6, 24, 16)
        self.body_l.setSpacing(12)

        # Paths
        pbox = SectionBox("What do you have?")
        self.row_install = _PathRow(
            "INSTALL FOLDER", "Folder containing the game's files",
            lambda cur: QFileDialog.getExistingDirectory(
                self, "Select install folder", cur or str(Path.home())))
        self.row_prefix = _PathRow(
            "WINE / PROTON PREFIX  (optional)",
            "Leave blank to create a new one",
            lambda cur: QFileDialog.getExistingDirectory(
                self, "Select Wine prefix folder",
                cur or str(installer._wine_prefix_root())))
        self.row_desktop = _PathRow(
            ".DESKTOP LAUNCHER  (optional)",
            "Leave blank to create a new one",
            lambda cur: QFileDialog.getOpenFileName(
                self, "Select .desktop file",
                cur or str(installer.APPS_DIR), "Desktop files (*.desktop)")[0])
        for r in (self.row_install, self.row_prefix, self.row_desktop):
            r.changed.connect(self._refresh)
            pbox.add(r)
        self.body_l.addWidget(pbox)

        # Executable
        ebox = SectionBox("Game executable")
        erow = QHBoxLayout()
        erow.setSpacing(8)
        self.exe_label = QLabel("—")
        self.exe_label.setFont(QFont("DM Mono", 10))
        self.exe_label.setWordWrap(True)
        self.exe_label.setStyleSheet(
            f"color: {COLORS['accent2']}; background: transparent; border: none;")
        erow.addWidget(self.exe_label, 1)
        self.exe_btn = QPushButton("Choose…")
        self.exe_btn.setFont(QFont("DM Sans", 10))
        self.exe_btn.clicked.connect(self._choose_exe)
        erow.addWidget(self.exe_btn)
        ebox.inner_layout().addLayout(erow)
        self.body_l.addWidget(ebox)

        # Proton
        self.proton_box = SectionBox("Proton / Wine version")
        self.proton_combo = NoScrollComboBox()
        self.proton_combo.setFont(QFont("DM Sans", 11))
        self.proton_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.proton_combo.setMinimumContentsLength(16)
        self.proton_combo.currentIndexChanged.connect(self._on_proton_changed)
        self.proton_combo.activated.connect(lambda _i: setattr(self, "_proton_user_set", True))
        self.proton_note = QLabel("")
        self.proton_note.setWordWrap(True)
        self.proton_note.setFont(QFont("DM Sans", 10))
        self.proton_note.setStyleSheet(
            f"color: {COLORS['text_muted']}; background: transparent; border: none;")
        self.proton_box.add(self.proton_combo)
        self.proton_box.add(self.proton_note)
        prow = QHBoxLayout()
        prow.setSpacing(8)
        self.get_btn = QPushButton("")
        self.get_btn.setFont(QFont("DM Sans", 10))
        self.get_btn.clicked.connect(self._get_selected_version)
        self.get_btn.hide()
        prow.addWidget(self.get_btn)
        self.check_btn = QPushButton("Check again")
        self.check_btn.setFont(QFont("DM Sans", 10))
        self.check_btn.clicked.connect(self._rescan_versions)
        self.check_btn.hide()
        prow.addWidget(self.check_btn)
        prow.addStretch()
        self.proton_box.inner_layout().addLayout(prow)
        self.dl_label = QLabel("")
        self.dl_label.setWordWrap(True)
        self.dl_label.setFont(QFont("DM Sans", 10))
        self.dl_label.setStyleSheet(
            f"color: {COLORS['text_muted']}; background: transparent; border: none;")
        self.dl_label.hide()
        self.proton_box.add(self.dl_label)
        self.body_l.addWidget(self.proton_box)

        # Redists
        self.redist_box = SectionBox("Redistributables")
        self.redist_note = QLabel("")
        self.redist_note.setWordWrap(True)
        self.redist_note.setFont(QFont("DM Sans", 10))
        self.redist_note.setStyleSheet(
            f"color: {COLORS['text_muted']}; background: transparent; border: none;")
        self.redist_box.add(self.redist_note)
        grid_w = QWidget()
        grid_w.setStyleSheet("background: transparent; border: none;")
        gl = QHBoxLayout(grid_w)
        gl.setContentsMargins(0, 0, 0, 0)
        c1, c2 = QVBoxLayout(), QVBoxLayout()
        c1.setSpacing(4)
        c2.setSpacing(4)
        for i, verb in enumerate(installer.COMMON_REDISTS):
            cb = QCheckBox(verb)
            cb.setFont(QFont("DM Mono", 10))
            cb.setStyleSheet(f"color: {COLORS['text_dim']};")
            cb.toggled.connect(self._update_summary)
            self._redist_checks[verb] = cb
            (c1 if i % 2 == 0 else c2).addWidget(cb)
        gl.addLayout(c1)
        gl.addLayout(c2)
        self.redist_box.add(grid_w)
        self.body_l.addWidget(self.redist_box)

        # Summary
        sbox = SectionBox("What will happen")
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setFont(QFont("DM Sans", 10))
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        self.summary.setStyleSheet(
            f"color: {COLORS['text']}; background: transparent; border: none;")
        sbox.add(self.summary)
        self.body_l.addWidget(sbox)
        self.body_l.addStretch()

        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        # Progress
        self.progress_panel = QWidget()
        self.progress_panel.setStyleSheet(
            f"background: {COLORS['surface2']}; border-top: 1px solid {COLORS['border']};")
        self.progress_panel.hide()
        pl = QVBoxLayout(self.progress_panel)
        pl.setContentsMargins(24, 12, 24, 12)
        pl.setSpacing(6)
        self.stage_label = QLabel("")
        self.stage_label.setFont(QFont("DM Sans", 11, QFont.Weight.Medium))
        pl.addWidget(self.stage_label)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setFixedHeight(4)
        self.bar.setTextVisible(False)
        self.bar.setStyleSheet(f"""
            QProgressBar {{ background: {COLORS['surface3']}; border-radius: 2px; border: none; }}
            QProgressBar::chunk {{ background: {COLORS['accent']}; border-radius: 2px; }}""")
        pl.addWidget(self.bar)
        self.msg_label = QLabel("")
        self.msg_label.setWordWrap(True)
        self.msg_label.setFont(QFont("DM Mono", 9))
        self.msg_label.setStyleSheet(f"color: {COLORS['text_muted']};")
        pl.addWidget(self.msg_label)
        root.addWidget(self.progress_panel)

        # Footer
        foot = QWidget()
        foot.setStyleSheet(
            f"background: {COLORS['surface']}; border-top: 1px solid {COLORS['border']};")
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(24, 14, 24, 14)
        fl.setSpacing(10)
        fl.addStretch()
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        fl.addWidget(self.cancel_btn)
        self.go_btn = QPushButton("Add to Installed →")
        self.go_btn.setFont(QFont("Rajdhani", 13, QFont.Weight.Bold))
        self.go_btn.setStyleSheet(accent_button_style())
        self.go_btn.clicked.connect(self._go)
        fl.addWidget(self.go_btn)
        root.addWidget(foot)

        self._populate_proton([])
        self._prefill()
        self._refresh()

    # ── Prefill from what VaultPlay can already find ─────────────────────────

    def _prefill(self):
        """Pre-fill from an orphaned VaultPlay .desktop for this game, or an
        existing prefix that matches the game's usual prefix name."""
        try:
            for o in dbsafety.find_orphans():
                if o.get("game_id") == self.game.get("id"):
                    self.row_desktop.setText(o["desktop_path"])
                    break
        except Exception as e:
            log.debug("orphan prefill failed: %s", e)
        try:
            guess = adopt_install.default_prefix_path(self.game)
            if adopt_install.prefix_initialized(guess) and not self.row_desktop.text():
                self.row_prefix.setText(guess)
        except Exception:
            pass

    # ── Re-inspect whenever a path changes ───────────────────────────────────

    def _refresh(self):
        try:
            info = adopt_install.inspect(
                self.game, self.row_install.text(),
                self.row_prefix.text(), self.row_desktop.text())
        except Exception as e:
            log.exception("inspect failed")
            info = {"notes": [f"Could not inspect those paths: {e}"], "exe": "",
                    "exe_candidates": [], "proton_version": "", "proton_source": "",
                    "install_path": "", "wine_prefix": "", "prefix_exists": False,
                    "installed_redists": [], "desktop": {}, "will_create_prefix": True,
                    "prefix_is_proton": False}
        self._info = info

        # Only fill blanks — never overwrite what the user typed.
        if not self.row_install.text() and info.get("install_path"):
            self.row_install.setText(info["install_path"])
        if not self.row_prefix.text() and info.get("wine_prefix"):
            self.row_prefix.setText(info["wine_prefix"])

        # Exe: keep a manual pick if still valid under the install folder.
        if not (self._exe and Path(self._exe).exists()):
            self._exe = info.get("exe", "")
        self.exe_label.setText(self._exe or "Not found yet — click Choose…")

        src = info.get("proton_source", "")
        # Only a version the user's own launcher/prefix names should override the
        # community recommendation; otherwise let the picker preselect ✦.
        pick = info.get("proton_version", "") if src in ("desktop", "prefix") else ""
        self._populate_proton(protondb_mod.get_versions_for_ui(), pick)
        self._src_note = {
            "desktop": "Taken from the .desktop launcher.",
            "prefix": "Taken from the existing Wine prefix.",
        }.get(src, "")
        if self._src_note and not self._proton_user_set:
            self.proton_note.setText(self._src_note)
            self.proton_note.setStyleSheet(
                f"color: {COLORS['text_muted']}; background: transparent; border: none;")
            self.proton_note.setVisible(True)

        # Redists: an existing prefix keeps what it has; a new one gets the
        # same auto-detected list a normal install would.
        installed = set(info.get("installed_redists") or [])
        if info.get("prefix_exists"):
            wanted = installed
            self.redist_note.setText(
                "This prefix already has the checked items. Tick more to "
                "install them into it.")
        else:
            verbs, source = adopt_install.suggest_redists(self.game)
            wanted = set(verbs)
            self.redist_note.setText(
                f"A new prefix will be created. Pre-selected from: {source}.")
        for verb, cb in self._redist_checks.items():
            cb.blockSignals(True)
            cb.setChecked(verb in wanted)
            have = verb in installed
            cb.setText(verb + ("  (installed)" if have else ""))
            cb.setStyleSheet(
                f"color: {COLORS['accent2']};" if have else f"color: {COLORS['text_dim']};")
            cb.blockSignals(False)
        self._update_summary()

    # ── Proton picker (mirrors InstallDialog: counts, ✦ top pick, uninstalled) ─

    def _load_counts(self):
        try:
            raw = self.game.get("protondb_version_counts")
            counts = json.loads(raw) if raw else {}
        except Exception:
            counts = {}
        if not isinstance(counts, dict):
            counts = {}
        self._sample_size = int(counts.pop("__sample__", 0) or 0)
        self._counts = counts
        rec = (self.game.get("recommended_proton") or "").strip()
        self._top_canonical = rec or (protondb_mod.top_version(counts) if counts else "")

    def _canonical_for_value(self, value):
        for canonical in self._counts:
            if protondb_mod.match_to_installed(canonical, [(value, value)]):
                return canonical
        return ""

    def _populate_proton(self, versions, select=""):
        if not hasattr(self, "_counts"):
            self._load_counts()
        self._installed_versions = list(versions)
        installed_values = {v for _, v in versions}
        cur = self.proton_combo.currentData() or ""
        if getattr(self, "_proton_user_set", False) and cur:
            select = cur            # never override an explicit user choice

        items = []   # (label, value, canonical, installed, count)
        for label, value in versions:
            canon = self._canonical_for_value(value)
            items.append((label, value, canon, True, self._counts.get(canon, 0) if canon else 0))
        items.sort(key=lambda x: -x[4])      # stable: keeps detected order within ties
        # Recommended / reported versions that aren't installed yet.
        for canon, count in sorted(self._counts.items(), key=lambda kv: -kv[1]):
            if count <= 0 and canon != self._top_canonical:
                continue
            if protondb_mod.match_to_installed(canon, versions):
                continue
            items.append((canon, _canonical_to_value_key(canon), canon, False, count))

        self.proton_combo.blockSignals(True)
        self.proton_combo.clear()
        top_installed_idx = top_missing_idx = -1
        for i, (label, value, canon, inst, count) in enumerate(items):
            cs = f"  ·  {count} reports" if count else ""
            is_top = bool(canon) and canon == self._top_canonical
            if not inst:
                text = f"{label}{cs}  (not installed)" + ("  ✦" if is_top else "")
            elif is_top and count:
                text = f"{label}{cs}  ✦"
            else:
                text = f"{label}{cs}"
            self.proton_combo.addItem(text, value)
            if is_top:
                self.proton_combo.setItemData(
                    i, QColor("#e8c76a") if inst else QColor(COLORS["text_muted"]),
                    Qt.ItemDataRole.ForegroundRole)
                if inst: top_installed_idx = i
                else:    top_missing_idx = i
            elif not inst:
                self.proton_combo.setItemData(
                    i, QColor(COLORS["text_muted"]), Qt.ItemDataRole.ForegroundRole)
        if not items:
            self.proton_combo.addItem("No versions found — install via ProtonUp-Qt", "")

        idx = self.proton_combo.findData(select) if select else -1
        if idx < 0:
            # No explicit/known choice → the community-recommended version,
            # installed or not (a missing one gets a Download button).
            idx = top_installed_idx if top_installed_idx >= 0 else top_missing_idx
        if idx < 0 and self.proton_combo.count():
            idx = 0
        if idx >= 0:
            self.proton_combo.setCurrentIndex(idx)
        self.proton_combo.blockSignals(False)
        self._on_proton_changed()

    def _selected_installed(self) -> bool:
        v = self.proton_combo.currentData()
        return bool(v) and v in {x for _, x in getattr(self, "_installed_versions", [])}

    def _selected_canonical(self) -> str:
        v = self.proton_combo.currentData() or ""
        if not v:
            return ""
        if self._selected_installed():
            return self._canonical_for_value(v)
        for canon in self._counts:
            if _canonical_to_value_key(canon) == v:
                return canon
        return v

    def _on_proton_changed(self, *_):
        canon = self._selected_canonical()
        inst = self._selected_installed()
        count = self._counts.get(canon, 0) if canon else 0
        top = self._top_canonical
        sample = self._sample_size
        note, color = "", COLORS["text_muted"]
        if self.proton_combo.currentData() and not inst:
            label = protondb_mod.proton_version_label(self.proton_combo.currentData()) or canon
            cs = f" ({count} of {sample} most recent reports)" if count and sample else ""
            note, color = f"{label}{cs} — not installed. Get it below before adding.", COLORS["accent"]
        elif canon and count and canon == top:
            cs = f"{count} of {sample} most recent reports" if sample else f"{count} reports"
            note, color = f"Most reported by the community  ·  {cs}", "#4ade80"
        elif canon and count:
            tc = self._counts.get(top, 0) if top else 0
            note = f"{count} reports  ·  top choice has {tc} reports"
        if note:
            self.proton_note.setText(note)
            self.proton_note.setStyleSheet(
                f"color: {color}; background: transparent; border: none;")
            self.proton_note.setVisible(True)
        elif not getattr(self, "_src_note", ""):
            self.proton_note.setVisible(False)

        # Download / Steam button for a version that isn't installed.
        if self.proton_combo.currentData() and not inst:
            self.get_btn.setText(f"↓  Download {canon}" if _is_ge_proton_key(canon)
                                 else "Install via Steam →")
            self.get_btn.show()
            self.check_btn.setVisible(not _is_ge_proton_key(canon))
        else:
            self.get_btn.hide()
            self.check_btn.hide()
        self._update_summary()

    def _rescan_versions(self):
        protondb_mod.invalidate_installed_cache()
        keep = self.proton_combo.currentData() or ""
        self._populate_proton(protondb_mod.get_versions_for_ui(), keep)
        # A just-installed version has a different value key than the placeholder.
        if keep and not self._selected_installed():
            m = protondb_mod.match_to_installed(self._selected_canonical() or "", self._installed_versions)
            if m:
                self._proton_user_set = True
                self.proton_combo.setCurrentIndex(max(0, self.proton_combo.findData(m)))
        self.dl_label.setVisible(False)

    def _get_selected_version(self):
        canon = self._selected_canonical()
        if _is_ge_proton_key(canon):
            self._start_ge_download(canon)
            return
        try:
            url, _direct = protondb_mod.get_steam_install_url(canon)
            subprocess.Popen(["xdg-open", url])
            self.dl_label.setText(
                f"Opened Steam for {canon}. Click “Check again” once it finishes downloading.")
        except Exception as e:
            self.dl_label.setText(
                f"Could not open Steam ({e}). Install {canon} from Steam → Library → Tools, "
                "then click “Check again”.")
        self.dl_label.show()

    def _start_ge_download(self, canon):
        self.get_btn.setEnabled(False)
        self.dl_label.setText("Fetching GE-Proton release list…")
        self.dl_label.show()
        releases = protondb_mod.get_ge_releases(limit=30)
        m = re.search(r"(\d+)", canon)
        major = int(m.group(1)) if m else 0
        best, best_minor = None, -1
        for rel in releases or []:
            tm = re.search(r"GE-Proton(\d+)-(\d+)", rel["tag"], re.IGNORECASE)
            if tm and int(tm.group(1)) == major and int(tm.group(2)) > best_minor:
                best, best_minor = rel, int(tm.group(2))
        if not best:
            self.dl_label.setText(f"No GE-Proton {major}.x release found (check your connection).")
            self.get_btn.setEnabled(True)
            return
        self.dl_label.setText(f"Downloading {best['tag']}…")
        self.proton_combo.setEnabled(False)
        self._dl_worker = GEDownloadWorker(best["tag"], best["download_url"])
        self._dl_worker.progress.connect(
            lambda stage, pct, msg: self.dl_label.setText(f"{stage}  {pct}%  {msg}"))
        self._dl_worker.finished.connect(self._on_ge_done)
        self._dl_worker.start()

    def _on_ge_done(self, ok, msg):
        self.proton_combo.setEnabled(True)
        self.get_btn.setEnabled(True)
        self.dl_label.setText(("✓ " if ok else "✗ ") + msg)
        if ok:
            self._rescan_versions()
            self.dl_label.setText("✓ " + msg)
            self.dl_label.show()

    def _choose_exe(self):
        inst = self.row_install.text() or self._info.get("install_path", "")
        cands = self._info.get("exe_candidates") or adopt_install.candidate_exes(inst)
        if cands:
            from ui.exe_picker_dialog import ExePickerDialog
            dlg = ExePickerDialog(
                [Path(c) for c in cands], start_dir=inst,
                intro="Pick the file that starts the game.", parent=self)
            if dlg.exec() == QDialog.DialogCode.Accepted and dlg.selected_path():
                self._exe = dlg.selected_path()
        else:
            p, _ = QFileDialog.getOpenFileName(
                self, "Select game executable", inst or str(Path.home()),
                "Windows executables (*.exe *.lnk *.bat);;All files (*)")
            if p:
                self._exe = p
        self.exe_label.setText(self._exe or "Not found yet — click Choose…")
        self._update_summary()

    # ── Summary ──────────────────────────────────────────────────────────────

    def _selected_redists(self):
        return [v for v, cb in self._redist_checks.items() if cb.isChecked()]

    def _update_summary(self, *_):
        info = self._info or {}
        lines = []
        inst = self.row_install.text() or info.get("install_path", "")
        lines.append(f"• Install folder: <b>{inst or '—'}</b>")
        lines.append(f"• Executable: <b>{Path(self._exe).name if self._exe else '— (choose one)'}</b>")
        prefix = self.row_prefix.text() or adopt_install.default_prefix_path(self.game)
        if adopt_install.prefix_initialized(adopt_install.normalize_prefix_root(prefix)):
            lines.append(f"• Wine prefix: use existing <b>{Path(prefix).name}</b>")
        else:
            lines.append(f"• Wine prefix: <b>create new</b> at {prefix}")
        installed = set(info.get("installed_redists") or [])
        todo = [v for v in self._selected_redists() if v not in installed]
        lines.append(f"• Redistributables to install: <b>{', '.join(todo) if todo else 'none'}</b>")
        if self.row_desktop.text() and info.get("desktop"):
            lines.append("• Launcher: reuse your .desktop file where it still matches, otherwise rebuild it")
        else:
            lines.append("• Launcher: <b>create new</b> .desktop")
        for n in info.get("notes", []):
            lines.append(f"<span style='color:{COLORS['accent']}'>⚠ {n}</span>")
        self.summary.setText("<br>".join(lines))
        self.go_btn.setEnabled(bool(self._exe) and not self._busy
                               and (self._selected_installed() or not self.proton_combo.currentData()))
        self.go_btn.setToolTip("" if self.go_btn.isEnabled() or not self.proton_combo.currentData() or self._selected_installed()
                               else "Download or install the selected Proton version first, or pick an installed one.")

    # ── Run ──────────────────────────────────────────────────────────────────

    def _go(self):
        if not self._exe or not Path(self._exe).exists():
            QMessageBox.warning(self, "Locate Game", "Pick the game's executable first.")
            return
        if not self.proton_combo.currentData():
            if QMessageBox.question(
                    self, "Locate Game",
                    "No Proton/Wine version is selected, so system Wine will be "
                    "used. Continue?") != QMessageBox.StandardButton.Yes:
                return
        plan = {
            "install_path": self.row_install.text() or self._info.get("install_path", ""),
            "wine_prefix": self.row_prefix.text(),
            "exe_path": self._exe,
            "desktop_path": self.row_desktop.text(),
            "proton_version": self.proton_combo.currentData() or "",
            "redists": self._selected_redists(),
        }
        self._busy = True
        for w in (self.go_btn, self.cancel_btn, self.exe_btn, self.proton_combo):
            w.setEnabled(False)
        self.progress_panel.show()
        self._worker = _AdoptWorker(self.game["id"], plan)
        self._worker.progress.connect(self._on_progress)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_progress(self, stage, pct, msg):
        self.stage_label.setText(stage)
        self.bar.setValue(pct)
        self.msg_label.setText(msg)

    def _on_done(self, res):
        self._busy = False
        self.cancel_btn.setEnabled(True)
        if res.get("success"):
            warn = res.get("warnings") or []
            if warn:
                QMessageBox.information(self, "Locate Game", "Added, with notes:\n\n" + "\n".join(warn))
            self.accept()
            return
        self.stage_label.setText("✗ Could not add the game")
        self.stage_label.setStyleSheet(f"color: {COLORS['danger']}; font-weight: 600;")
        self.msg_label.setText(res.get("error") or "Unknown error")
        self.go_btn.setEnabled(True)
        self.exe_btn.setEnabled(True)
        self.proton_combo.setEnabled(True)
