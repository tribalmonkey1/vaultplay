"""
ui/version_tracker_dialog.py — Version tracker management dialog for VaultPlay

Opened from the GameTile right-click menu ("Track Version Updates…") or from
the game detail view.  Lets the user:

  • See all trackers for the game (site label, path, last version found, status)
  • Save an edited path for an existing tracker and recheck immediately
  • Remove a tracker
  • Add a new tracker by pasting a full URL (base + path split automatically)
    and checking it immediately for a live result

Games in blacklisted categories: dialog opens but all check/save/add buttons are
disabled and an inline warning is shown, since blacklisted games are intentionally
excluded from version checks.
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
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QWidget, QScrollArea, QLineEdit, QSizePolicy,
    QMessageBox
)
from PyQt6.QtCore import Qt, pyqtSignal, QThread
from PyQt6.QtGui import QFont, QColor

import db
import version_check as vc
from ui.style import COLORS, accent_button_style

log = logging.getLogger(__name__)


# ── Background check worker ───────────────────────────────────────────────────

class CheckWorker(QThread):
    """
    Run a single version check in a background thread.
    Does NOT call db.update_version_tracker_result — caller does that so the
    UI can show the result first and let the user see it before DB write.
    """
    finished = pyqtSignal(dict)   # CheckResult dict from version_check.check_tracker

    def __init__(self, tracker_row, is_formula: bool = False):
        super().__init__()
        self._tracker_row = tracker_row
        self._is_formula  = is_formula

    def run(self):
        result = vc.check_tracker(self._tracker_row, is_formula_site=self._is_formula)
        self.finished.emit(result)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _status_style(status: str) -> tuple[str, str]:
    """Return (label_text, css_color) for a tracker last_status value."""
    return {
        "ok":       ("✓ ok",       "#4ade80"),
        "no_match": ("— no match", COLORS["text_muted"]),
        "error":    ("✗ error",    COLORS["danger"]),
    }.get(status or "", ("—", COLORS["text_muted"]))


def _staleness_label(checked_at: str) -> str:
    """Return a human-readable 'X days ago' string from an ISO datetime string."""
    if not checked_at:
        return "never"
    import datetime
    try:
        dt = datetime.datetime.fromisoformat(str(checked_at).replace("T", " ")[:19])
        delta = datetime.datetime.utcnow() - dt
        days = delta.days
        if days == 0:
            return "today"
        if days == 1:
            return "1 day ago"
        return f"{days} days ago"
    except (ValueError, TypeError):
        return "?"


def _make_btn(text: str, accent: bool = False,
              danger: bool = False, parent=None) -> QPushButton:
    """Consistent small button factory used throughout the dialog."""
    btn = QPushButton(text, parent)
    btn.setFont(QFont("DM Sans", 10))
    if accent:
        btn.setStyleSheet(f"""
            QPushButton {{
                background: {COLORS['accent']};
                border: none; border-radius: 6px;
                color: #000; padding: 5px 12px; font-weight: 600;
            }}
            QPushButton:hover {{ background: #f0d47a; }}
            QPushButton:disabled {{
                background: {COLORS['surface3']};
                color: {COLORS['text_muted']};
            }}
        """)
    elif danger:
        btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                border: 1px solid rgba(248,113,113,0.3);
                border-radius: 6px; color: {COLORS['danger']};
                padding: 5px 10px;
            }}
            QPushButton:hover {{
                background: rgba(248,113,113,0.08);
                border-color: {COLORS['danger']};
            }}
            QPushButton:disabled {{
                color: {COLORS['text_muted']};
                border-color: {COLORS['border']};
            }}
        """)
    else:
        btn.setStyleSheet(f"""
            QPushButton {{
                background: {COLORS['surface2']};
                border: 1px solid {COLORS['border']};
                border-radius: 6px; color: {COLORS['text_dim']};
                padding: 5px 12px;
            }}
            QPushButton:hover {{
                color: {COLORS['text']};
                border-color: rgba(255,255,255,0.2);
            }}
            QPushButton:disabled {{
                color: {COLORS['text_muted']};
                border-color: {COLORS['border']};
            }}
        """)
    return btn


# ── Tracker row widget ────────────────────────────────────────────────────────

class TrackerRow(QWidget):
    """
    One row representing a single (game, site) tracker.
    Contains: site label, editable path, version info, Save & Recheck, Remove.
    """
    removed   = pyqtSignal(int)   # tracker_id
    rechecked = pyqtSignal()      # row changed — dialog should refresh

    def __init__(self, tracker, blocked: bool = False, parent=None):
        super().__init__(parent)
        self._tracker_id = tracker["id"]
        self._game_id    = tracker["game_id"]
        self._site_id    = tracker["site_id"]
        self._blocked    = blocked
        self._worker: CheckWorker | None = None

        self.setStyleSheet(f"""
            QWidget {{
                background: {COLORS['surface2']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
            }}
        """)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 10, 14, 10)
        outer.setSpacing(6)

        # ── Top row: site label + version badges + action buttons ─────────────
        top = QHBoxLayout()
        top.setSpacing(8)
        top.setContentsMargins(0, 0, 0, 0)

        site_lbl = QLabel(tracker["label"] or tracker["base_url"])
        site_lbl.setFont(QFont("DM Sans", 11, QFont.Weight.Medium))
        site_lbl.setStyleSheet(f"color: {COLORS['text']}; background: transparent; border: none;")
        top.addWidget(site_lbl)
        top.addStretch()

        # Status badge
        status_text, status_color = _status_style(tracker["last_status"])
        self.status_lbl = QLabel(status_text)
        self.status_lbl.setFont(QFont("DM Mono", 9))
        self.status_lbl.setStyleSheet(
            f"color: {status_color}; background: transparent; border: none;")
        top.addWidget(self.status_lbl)

        outer.addLayout(top)

        # ── Version info row ──────────────────────────────────────────────────
        ver_row = QHBoxLayout()
        ver_row.setSpacing(16)
        ver_row.setContentsMargins(0, 0, 0, 0)

        dotted_val = tracker["dotted_version"] or "—"
        plain_val  = tracker["plain_version"]  or "—"
        checked_at = _staleness_label(tracker["last_checked_at"])

        for prefix, val in [("dotted:", dotted_val), ("build:", plain_val)]:
            lbl = QLabel(f"{prefix}  {val}")
            lbl.setFont(QFont("DM Mono", 9))
            lbl.setStyleSheet(
                f"color: {COLORS['accent2'] if val != '—' else COLORS['text_muted']};"
                f" background: transparent; border: none;")
            ver_row.addWidget(lbl)

        ver_row.addStretch()

        staleness = QLabel(f"checked {checked_at}")
        staleness.setFont(QFont("DM Mono", 8))
        staleness.setStyleSheet(
            f"color: {COLORS['text_muted']}; background: transparent; border: none;")
        ver_row.addWidget(staleness)
        outer.addLayout(ver_row)

        # ── Path row: editable path + Save & Recheck + Remove ────────────────
        path_row = QHBoxLayout()
        path_row.setSpacing(8)
        path_row.setContentsMargins(0, 0, 0, 0)

        self.path_edit = QLineEdit(tracker["path"])
        self.path_edit.setFont(QFont("DM Mono", 10))
        self.path_edit.setStyleSheet(f"""
            QLineEdit {{
                background: {COLORS['surface3']};
                border: 1px solid {COLORS['border']};
                border-radius: 5px;
                color: {COLORS['accent2']};
                padding: 4px 8px;
            }}
            QLineEdit:focus {{
                border-color: rgba(255,255,255,0.2);
            }}
        """)
        self.path_edit.setPlaceholderText("/path/on/site")
        path_row.addWidget(self.path_edit, 1)

        # URL preview label (base_url + current path)
        base_url = tracker["base_url"]
        self._base_url = base_url
        self._suffix   = tracker["suffix"] or ""
        self.url_preview = QLabel(self._computed_url())
        self.url_preview.setFont(QFont("DM Mono", 8))
        self.url_preview.setStyleSheet(
            f"color: {COLORS['text_muted']}; background: transparent; border: none;")
        self.url_preview.setWordWrap(False)
        self.path_edit.textChanged.connect(self._on_path_changed)

        self.save_btn = _make_btn("Save & Recheck", accent=True)
        self.save_btn.setEnabled(not blocked)
        self.save_btn.clicked.connect(self._on_save_recheck)
        path_row.addWidget(self.save_btn)

        self.remove_btn = _make_btn("Remove", danger=True)
        self.remove_btn.setEnabled(not blocked)
        self.remove_btn.clicked.connect(self._on_remove)
        path_row.addWidget(self.remove_btn)

        outer.addLayout(path_row)
        outer.addWidget(self.url_preview)

        # ── Inline result label (shown after recheck) ─────────────────────────
        self.result_lbl = QLabel("")
        self.result_lbl.setFont(QFont("DM Mono", 9))
        self.result_lbl.setStyleSheet(
            f"color: {COLORS['text_muted']}; background: transparent; border: none;")
        self.result_lbl.hide()
        outer.addWidget(self.result_lbl)

    def _computed_url(self) -> str:
        return vc.build_url(self._base_url, self.path_edit.text(), self._suffix)

    def _on_path_changed(self, _text: str):
        self.url_preview.setText(self._computed_url())

    def _on_remove(self):
        confirm = QMessageBox(self)
        confirm.setWindowTitle("Remove Tracker")
        confirm.setText("Remove this version tracker?")
        confirm.setInformativeText(
            "Stored version data will be lost. This cannot be undone.")
        confirm.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        confirm.setDefaultButton(QMessageBox.StandardButton.Cancel)
        confirm.setStyleSheet(
            f"QMessageBox {{ background: {COLORS['surface']}; color: {COLORS['text']}; }}")
        if confirm.exec() == QMessageBox.StandardButton.Yes:
            db.remove_version_tracker(self._tracker_id)
            self.removed.emit(self._tracker_id)

    def _on_save_recheck(self):
        if self._worker and self._worker.isRunning():
            return

        new_path = self.path_edit.text().strip()
        if not new_path:
            self._set_result("Path cannot be empty", "error")
            return

        # Save updated path (resets stored version if path changed)
        tracker = db.add_version_tracker(self._game_id, self._site_id, new_path)
        self._tracker_id = tracker["id"]

        self.save_btn.setEnabled(False)
        self.save_btn.setText("Checking…")
        self._set_result("Checking…", "muted")

        # Build a minimal tracker-like dict for check_tracker
        fake_row = {
            "id":         self._tracker_id,
            "source_url": vc.build_url(self._base_url, new_path, self._suffix),
        }
        self._worker = CheckWorker(fake_row, is_formula=False)
        self._worker.finished.connect(self._on_check_done)
        self._worker.start()

    def _on_check_done(self, result: dict):
        self.save_btn.setEnabled(not self._blocked)
        self.save_btn.setText("Save & Recheck")

        status = result["status"]
        dotted = result["dotted_version"]
        plain  = result["plain_version"]

        if status == "ok":
            parts = []
            if dotted: parts.append(f"dotted: {dotted}")
            if plain:  parts.append(f"build: {plain}")
            msg = "✓  " + "  ·  ".join(parts) if parts else "✓ ok (no version extracted)"
            self._set_result(msg, "good")
        elif status == "no_match":
            self._set_result("— no version found on this page", "muted")
        else:
            self._set_result(f"✗ {result.get('error_msg', 'error')}", "error")

        # Write to DB and notify dialog to refresh
        db.update_version_tracker_result(
            tracker_id     = self._tracker_id,
            status         = status,
            dotted_version = dotted,
            plain_version  = plain,
            error_msg      = result.get("error_msg"),
        )
        self.rechecked.emit()

    def _set_result(self, text: str, style: str):
        colors = {
            "good":  "#4ade80",
            "muted": COLORS["text_muted"],
            "error": COLORS["danger"],
        }
        self.result_lbl.setText(text)
        self.result_lbl.setStyleSheet(
            f"color: {colors.get(style, COLORS['text_muted'])};"
            f" background: transparent; border: none;")
        self.result_lbl.show()


# ── Add tracker section ───────────────────────────────────────────────────────

class AddTrackerSection(QWidget):
    """
    Paste-a-URL panel at the bottom of the dialog.
    Splits the pasted URL into base_url + path, creates/finds the site,
    creates the tracker, and runs an immediate check.
    """
    tracker_added = pyqtSignal()

    def __init__(self, game_id: int, blocked: bool = False, parent=None):
        super().__init__(parent)
        self._game_id = game_id
        self._blocked = blocked
        self._worker: CheckWorker | None = None
        self._pending_tracker = None   # tracker row dict after site creation

        self.setStyleSheet(f"""
            QWidget {{
                background: {COLORS['surface2']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        header = QLabel("ADD TRACKER")
        header.setFont(QFont("DM Mono", 8))
        header.setStyleSheet(
            f"color: {COLORS['text_muted']}; letter-spacing: 2px;"
            " background: transparent; border: none;")
        layout.addWidget(header)

        desc = QLabel(
            "Paste the full URL of a page that shows the game's version "
            "(e.g. its PCGamingWiki page, SteamDB page, or GOG product page)."
        )
        desc.setFont(QFont("DM Sans", 10))
        desc.setStyleSheet(
            f"color: {COLORS['text_muted']}; background: transparent; border: none;")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        url_row = QHBoxLayout()
        url_row.setSpacing(8)
        url_row.setContentsMargins(0, 0, 0, 0)

        self.url_edit = QLineEdit()
        self.url_edit.setFont(QFont("DM Mono", 10))
        self.url_edit.setPlaceholderText(
            "https://www.pcgamingwiki.com/wiki/Game_Title")
        self.url_edit.setStyleSheet(f"""
            QLineEdit {{
                background: {COLORS['surface3']};
                border: 1px solid {COLORS['border']};
                border-radius: 5px;
                color: {COLORS['accent2']};
                padding: 5px 10px;
            }}
            QLineEdit:focus {{ border-color: rgba(255,255,255,0.2); }}
        """)
        url_row.addWidget(self.url_edit, 1)

        self.add_btn = _make_btn("Add & Check", accent=True)
        self.add_btn.setEnabled(not blocked)
        self.add_btn.clicked.connect(self._on_add)
        url_row.addWidget(self.add_btn)

        layout.addLayout(url_row)

        self.result_lbl = QLabel("")
        self.result_lbl.setFont(QFont("DM Mono", 9))
        self.result_lbl.setStyleSheet(
            f"color: {COLORS['text_muted']}; background: transparent; border: none;")
        self.result_lbl.setWordWrap(True)
        self.result_lbl.hide()
        layout.addWidget(self.result_lbl)

    def _set_result(self, text: str, style: str):
        colors = {
            "good":    "#4ade80",
            "warning": COLORS["accent"],
            "muted":   COLORS["text_muted"],
            "error":   COLORS["danger"],
        }
        self.result_lbl.setText(text)
        self.result_lbl.setStyleSheet(
            f"color: {colors.get(style, COLORS['text_muted'])};"
            f" background: transparent; border: none;")
        self.result_lbl.show()

    def _on_add(self):
        if self._worker and self._worker.isRunning():
            return

        raw_url = self.url_edit.text().strip()

        # Validate URL
        err = vc.validate_url(raw_url)
        if err:
            self._set_result(f"✗ {err}", "error")
            return

        # Split into base + path
        try:
            base_url, path = vc.split_url_into_base_and_rest(raw_url)
        except ValueError as e:
            self._set_result(f"✗ {e}", "error")
            return

        # Get or create site
        site = db.get_or_create_version_site_by_base_url(base_url)

        # Check for existing tracker on this site
        existing = db.get_trackers_for_game(self._game_id)
        for t in existing:
            if t["site_id"] == site["id"]:
                self._set_result(
                    f"⚠  A tracker for {site['label']} already exists — "
                    f"edit its path above instead of adding a duplicate.",
                    "warning"
                )
                return

        # Create tracker
        tracker = db.add_version_tracker(self._game_id, site["id"], path)
        self._pending_tracker = tracker

        self.add_btn.setEnabled(False)
        self.add_btn.setText("Checking…")
        self._set_result("Fetching page…", "muted")

        fake_row = {
            "id":         tracker["id"],
            "source_url": vc.build_url(base_url, path, site["suffix"] or ""),
        }
        self._worker = CheckWorker(fake_row, is_formula=False)
        self._worker.finished.connect(self._on_check_done)
        self._worker.start()

    def _on_check_done(self, result: dict):
        self.add_btn.setEnabled(not self._blocked)
        self.add_btn.setText("Add & Check")

        if self._pending_tracker is None:
            return

        tracker_id = self._pending_tracker["id"]
        status = result["status"]
        dotted = result["dotted_version"]
        plain  = result["plain_version"]

        db.update_version_tracker_result(
            tracker_id     = tracker_id,
            status         = status,
            dotted_version = dotted,
            plain_version  = plain,
            error_msg      = result.get("error_msg"),
        )
        self._pending_tracker = None

        if status == "ok":
            parts = []
            if dotted: parts.append(f"dotted: {dotted}")
            if plain:  parts.append(f"build: {plain}")
            msg = "✓  " + "  ·  ".join(parts) if parts else "✓ tracker added (no version on page yet)"
            self._set_result(msg, "good")
            self.url_edit.clear()
            self.tracker_added.emit()
        elif status == "no_match":
            self._set_result(
                "✓ Tracker added — but no version was found on this page.\n"
                "Check that the URL points to a page that shows the game's version number.",
                "warning"
            )
            self.url_edit.clear()
            self.tracker_added.emit()
        else:
            err_msg = result.get("error_msg", "unknown error")
            self._set_result(
                f"✗ Tracker saved but check failed: {err_msg}\n"
                f"The tracker was created — you can recheck it above.",
                "error"
            )
            # Tracker was created even on error — notify so it appears in list
            self.tracker_added.emit()


# ── Main dialog ───────────────────────────────────────────────────────────────

class VersionTrackerDialog(QDialog):
    """
    Dialog for managing per-game version trackers.
    Opened from GameTile right-click or GameDetailView.
    """
    versions_updated = pyqtSignal(int)   # game_id — emitted when any check writes new data

    def __init__(self, game_id: int, parent=None):
        super().__init__(parent)
        self._game_id = game_id
        self._blocked = db.is_game_category_blacklisted(game_id)

        game  = db.get_game(game_id)
        title = (game["title"] or game["display_name"] or game["folder_name"]) if game else f"Game {game_id}"

        self.setWindowTitle(f"Version Trackers — {title}")
        self.setModal(True)
        self.setMinimumWidth(620)
        self.setFixedWidth(660)
        self.setMinimumHeight(400)
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

        h_title = QLabel("Track Version Updates")
        h_title.setFont(QFont("Rajdhani", 18, QFont.Weight.Bold))
        hdr_l.addWidget(h_title)

        h_sub = QLabel(title)
        h_sub.setFont(QFont("DM Sans", 11))
        h_sub.setStyleSheet(f"color: {COLORS['text_muted']};")
        hdr_l.addWidget(h_sub)
        root.addWidget(hdr)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background: {COLORS['border']}; border: none;")
        root.addWidget(sep)

        # ── Blacklist warning (shown when category is blacklisted) ────────────
        if self._blocked:
            warn = QLabel(
                "⚠  This game's category is blacklisted — version checks are "
                "disabled. Unblacklist the category in Settings → Categories "
                "to enable tracking."
            )
            warn.setFont(QFont("DM Sans", 10))
            warn.setWordWrap(True)
            warn.setStyleSheet(f"""
                background: rgba(248,113,113,0.08);
                border-bottom: 1px solid rgba(248,113,113,0.3);
                color: {COLORS['danger']};
                padding: 10px 24px;
            """)
            root.addWidget(warn)

        # ── Scrollable body ───────────────────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._body = QWidget()
        self._body.setStyleSheet(f"background: {COLORS['surface']};")
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(24, 16, 24, 16)
        self._body_layout.setSpacing(10)
        self._body_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        # Section header
        trackers_hdr = QLabel("ACTIVE TRACKERS")
        trackers_hdr.setFont(QFont("DM Mono", 8))
        trackers_hdr.setStyleSheet(
            f"color: {COLORS['text_muted']}; letter-spacing: 2px;")
        self._body_layout.addWidget(trackers_hdr)

        # Tracker rows container — rebuilt by _refresh_trackers()
        self._trackers_container = QWidget()
        self._trackers_container.setStyleSheet("background: transparent;")
        self._trackers_layout = QVBoxLayout(self._trackers_container)
        self._trackers_layout.setContentsMargins(0, 0, 0, 0)
        self._trackers_layout.setSpacing(8)
        self._body_layout.addWidget(self._trackers_container)

        self._body_layout.addSpacing(8)

        # Add tracker section
        self._add_section = AddTrackerSection(
            game_id=game_id, blocked=self._blocked)
        self._add_section.tracker_added.connect(self._refresh_trackers)
        self._body_layout.addWidget(self._add_section)

        self._body_layout.addStretch()

        scroll.setWidget(self._body)
        root.addWidget(scroll, 1)

        # ── Footer ────────────────────────────────────────────────────────────
        footer = QWidget()
        footer.setStyleSheet(
            f"background: {COLORS['surface']};"
            f" border-top: 1px solid {COLORS['border']};")
        foot_l = QHBoxLayout(footer)
        foot_l.setContentsMargins(24, 12, 24, 12)
        foot_l.addStretch()
        close_btn = QPushButton("Close")
        close_btn.setFont(QFont("DM Sans", 11))
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: {COLORS['surface2']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
                color: {COLORS['text_dim']};
                padding: 8px 20px;
            }}
            QPushButton:hover {{
                color: {COLORS['text']};
                background: {COLORS['surface3']};
            }}
        """)
        close_btn.clicked.connect(self.accept)
        foot_l.addWidget(close_btn)
        root.addWidget(footer)

        # Initial population
        self._refresh_trackers()

    def _refresh_trackers(self):
        """Rebuild the tracker rows from DB — called on open and after any mutation."""
        # Clear existing rows
        while self._trackers_layout.count():
            item = self._trackers_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        trackers = db.get_trackers_for_game(self._game_id)

        if not trackers:
            empty = QLabel("No trackers yet — add one below.")
            empty.setFont(QFont("DM Sans", 11))
            empty.setStyleSheet(
                f"color: {COLORS['text_muted']}; padding: 4px 0;")
            self._trackers_layout.addWidget(empty)
            return

        for tracker in trackers:
            row = TrackerRow(tracker, blocked=self._blocked)
            row.removed.connect(self._on_tracker_removed)
            row.rechecked.connect(self._on_tracker_rechecked)
            self._trackers_layout.addWidget(row)

    def _on_tracker_removed(self, tracker_id: int):
        self._refresh_trackers()
        self.versions_updated.emit(self._game_id)

    def _on_tracker_rechecked(self):
        self._refresh_trackers()
        self.versions_updated.emit(self._game_id)
