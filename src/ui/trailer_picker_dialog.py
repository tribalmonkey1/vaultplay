"""
ui/trailer_picker_dialog.py — Manual Trailer Picker for VaultPlay

Spec: Notion → Features → Fully Planned → Automatic Trailer Detection & Media Gallery.

Opened from Edit Metadata's "Browse Trailers…" button (see edit_metadata_dialog.py —
not yet wired up as of this file landing; this dialog is built standalone first,
same "the reusable piece exists" sequencing cover_art_picker_dialog.py followed).

Structurally close to ui/cover_art_picker_dialog.py: a manual URL entry always
available at the top, automatic candidates below. Diverges from that dialog's
single scrollable thumbnail grid + separate preview panel because trailer
candidates are few per source (Steam: at most one, GOG: at most one, YouTube:
up to ~15 ranked candidates across three searches — see trailers.py) and,
unlike cover art, there's no live preview possible yet (no in-app playback —
see the Notion page's "Designing for future in-app playback" section). So
instead: three short grouped sections (Steam / GOG / YouTube), each candidate
its own clickable row with a thumbnail, title/channel line, and a "Use This"
button — closer to edit_metadata_dialog.py's SteamAppPickerDialog shape than
CoverArtPickerDialog's.

Selecting ANY candidate here — including an automatically-detected Steam/GOG/
YouTube one — sets trailer_manual_override=True on save, exactly like typing
a URL by hand would. The user explicitly chose it in this dialog, even though
the URL itself came from an automatic source; that choice must stick the same
way a hand-typed URL would, per the "never let auto-detection silently
overwrite an explicit user choice" rule db.set_trailer()'s docstring describes.
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
from typing import Optional, Callable

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QWidget, QFrame, QScrollArea
)
from PyQt6.QtCore import Qt, pyqtSignal, QThread, QRunnable, QThreadPool, pyqtSlot, QObject
from PyQt6.QtGui import QFont, QPixmap

import metadata as meta_mod
from ui.style import COLORS, accent_button_style

log = logging.getLogger(__name__)

_LABEL_BASE = "background: transparent; border: none;"

_SOURCE_LABELS = {"steam": "Steam", "gog": "GOG", "youtube": "YouTube", "manual": "Manual"}


# ── Async thumbnail loader — mirrors cover_art_picker_dialog.py's _ThumbLoader ──

class _ThumbSignals(QObject):
    loaded = pyqtSignal(str)   # local_path


class _ThumbLoader(QRunnable):
    def __init__(self, url: str):
        super().__init__()
        self.url = url
        self.signals = _ThumbSignals()
        self.setAutoDelete(True)

    @pyqtSlot()
    def run(self):
        try:
            path = meta_mod.download_art(self.url)
            if path:
                self.signals.loaded.emit(path)
        except Exception as e:
            log.debug("_ThumbLoader failed for %s: %s", self.url, e)


# ── Background search worker ──────────────────────────────────────────────────

class _SearchWorker(QThread):
    """Runs trailers.get_media_options() off the UI thread."""
    done = pyqtSignal(dict)   # {"steam": dict|None, "gog": dict|None, "youtube": list[dict]}

    def __init__(self, steam_app_id, title: str, developer: str, publisher: str):
        super().__init__()
        self._steam_app_id = steam_app_id
        self._title = title
        self._developer = developer
        self._publisher = publisher

    def run(self):
        try:
            import trailers as trailers_mod
            result = trailers_mod.get_media_options(
                self._steam_app_id, self._title, self._developer, self._publisher)
        except Exception as e:
            log.warning("TrailerPickerDialog: search failed: %s", e)
            result = {"steam": None, "gog": None, "youtube": []}
        self.done.emit(result)


# ── One candidate row ─────────────────────────────────────────────────────────

class _CandidateRow(QFrame):
    """One selectable trailer candidate: thumbnail + title/subtitle + Use This.
    The whole row is clickable (mirrors edit_metadata_dialog.py's
    _SteamResultRow), not just the button, for a bigger/easier click target."""
    picked = pyqtSignal(str, str)   # url, source

    def __init__(self, url: str, source: str, title: str, subtitle: str,
                 thumbnail_url: Optional[str], parent=None):
        super().__init__(parent)
        self._url = url
        self._source = source
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(f"""
            QFrame {{
                background: {COLORS['surface2']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
            }}
            QFrame:hover {{
                border-color: rgba(232,199,106,0.4);
            }}
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(10)

        self.thumb = QLabel("▶")
        self.thumb.setFixedSize(120, 68)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setFont(QFont("DM Sans", 16))
        self.thumb.setStyleSheet(
            f"background: {COLORS['surface3']}; border-radius: 6px; "
            f"color: {COLORS['text_muted']};")
        layout.addWidget(self.thumb)
        if thumbnail_url:
            loader = _ThumbLoader(thumbnail_url)
            loader.signals.loaded.connect(self._on_thumb_loaded)
            QThreadPool.globalInstance().start(loader)

        text_col = QVBoxLayout()
        text_col.setSpacing(3)
        text_col.setContentsMargins(0, 0, 0, 0)

        title_lbl = QLabel(title)
        title_lbl.setFont(QFont("DM Sans", 11, QFont.Weight.Medium))
        title_lbl.setStyleSheet(f"color: {COLORS['text']}; {_LABEL_BASE}")
        title_lbl.setWordWrap(True)
        text_col.addWidget(title_lbl)

        if subtitle:
            sub_lbl = QLabel(subtitle)
            sub_lbl.setFont(QFont("DM Mono", 9))
            sub_lbl.setStyleSheet(f"color: {COLORS['text_muted']}; {_LABEL_BASE}")
            text_col.addWidget(sub_lbl)

        text_col.addStretch()
        layout.addLayout(text_col, 1)

        use_btn = QPushButton("Use This")
        use_btn.setFont(QFont("DM Sans", 10, QFont.Weight.Medium))
        use_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        use_btn.setStyleSheet(f"""
            QPushButton {{
                background: {COLORS['accent']};
                border: none; border-radius: 6px;
                color: #000; padding: 6px 14px; font-weight: 600;
            }}
            QPushButton:hover {{ background: #f0d47a; }}
        """)
        use_btn.clicked.connect(self._emit_pick)
        layout.addWidget(use_btn, 0, Qt.AlignmentFlag.AlignVCenter)

    def _on_thumb_loaded(self, local_path: str):
        pix = QPixmap(local_path)
        if pix.isNull():
            return
        scaled = pix.scaled(120, 68,
                            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                            Qt.TransformationMode.SmoothTransformation)
        x = max(0, (scaled.width() - 120) // 2)
        y = max(0, (scaled.height() - 68) // 2)
        self.thumb.setPixmap(scaled.copy(x, y, 120, 68))
        self.thumb.setText("")
        self.thumb.setStyleSheet("background: transparent; border-radius: 6px;")

    def _emit_pick(self):
        self.picked.emit(self._url, self._source)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._emit_pick()
        super().mousePressEvent(event)


def _section_label(text: str) -> QLabel:
    lbl = QLabel(text.upper())
    lbl.setFont(QFont("DM Mono", 8))
    lbl.setStyleSheet(
        f"color: {COLORS['text_muted']}; letter-spacing: 2px; {_LABEL_BASE}")
    return lbl


# ── Main dialog ───────────────────────────────────────────────────────────────

class TrailerPickerDialog(QDialog):
    """
    context_provider: zero-arg callable returning {"steam_app_id", "title",
    "developer", "publisher"} — called at search time (not just once at
    open), same lazy-read contract cover_art_picker_dialog.py's
    context_provider already uses, so this reflects whatever the parent
    Edit Metadata dialog's fields currently hold.
    """

    def __init__(self, context_provider: Callable[[], dict], parent=None):
        super().__init__(parent)
        self._context_provider = context_provider
        self._selected_url: Optional[str] = None
        self._selected_source: Optional[str] = None
        self._search_worker: Optional[_SearchWorker] = None

        self.setWindowTitle("Browse Trailers")
        self.setModal(True)
        self.setMinimumSize(560, 520)
        self.resize(600, 620)
        self.setStyleSheet(f"QDialog {{ background: {COLORS['surface']}; }}")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Header + manual URL entry ────────────────────────────────────────
        hdr = QWidget()
        hdr.setStyleSheet(f"background: {COLORS['surface']};")
        hdr_l = QVBoxLayout(hdr)
        hdr_l.setContentsMargins(20, 16, 20, 12)
        hdr_l.setSpacing(8)

        title_lbl = QLabel("Browse Trailers")
        title_lbl.setFont(QFont("Rajdhani", 16, QFont.Weight.Bold))
        title_lbl.setStyleSheet(_LABEL_BASE)
        hdr_l.addWidget(title_lbl)

        manual_row = QHBoxLayout()
        manual_row.setSpacing(8)
        self.manual_edit = QLineEdit()
        self.manual_edit.setFont(QFont("DM Mono", 10))
        self.manual_edit.setPlaceholderText("Paste a trailer URL directly…")
        self.manual_edit.setStyleSheet(f"color: {COLORS['accent2']};")
        self.manual_edit.returnPressed.connect(self._on_manual_use)
        manual_row.addWidget(self.manual_edit, 1)

        manual_btn = QPushButton("Use URL")
        manual_btn.clicked.connect(self._on_manual_use)
        manual_row.addWidget(manual_btn)
        hdr_l.addLayout(manual_row)

        self.status_lbl = QLabel("Searching…")
        self.status_lbl.setFont(QFont("DM Mono", 9))
        self.status_lbl.setStyleSheet(f"color: {COLORS['text_muted']}; {_LABEL_BASE}")
        hdr_l.addWidget(self.status_lbl)

        root.addWidget(hdr)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background: {COLORS['border']}; border: none;")
        root.addWidget(sep)

        # ── Scrollable results body ──────────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.body = QWidget()
        self.body.setStyleSheet(f"background: {COLORS['surface']};")
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(20, 14, 20, 14)
        self.body_layout.setSpacing(8)
        self.body_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.empty_lbl = QLabel("")
        self.empty_lbl.setFont(QFont("DM Sans", 11))
        self.empty_lbl.setStyleSheet(f"color: {COLORS['text_muted']}; {_LABEL_BASE}")
        self.empty_lbl.setWordWrap(True)
        self.body_layout.addWidget(self.empty_lbl)

        scroll.setWidget(self.body)
        root.addWidget(scroll, 1)

        # ── Footer ────────────────────────────────────────────────────────────
        footer = QWidget()
        footer.setStyleSheet(
            f"background: {COLORS['surface']}; border-top: 1px solid {COLORS['border']};")
        foot_l = QHBoxLayout(footer)
        foot_l.setContentsMargins(20, 12, 20, 12)
        foot_l.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        foot_l.addWidget(cancel_btn)
        root.addWidget(footer)

        self._run_search()

    # ── Search ────────────────────────────────────────────────────────────────

    def _run_search(self):
        if self._search_worker and self._search_worker.isRunning():
            return

        ctx = self._context_provider() or {}
        title = ctx.get("title") or ""
        if not title:
            self._show_empty("No title available to search with — enter a "
                             "trailer URL manually above.")
            return

        self.status_lbl.setText(f"Searching Steam, GOG, and YouTube for \u201c{title}\u201d…")
        self.status_lbl.setStyleSheet(f"color: {COLORS['text_muted']}; {_LABEL_BASE}")

        self._search_worker = _SearchWorker(
            ctx.get("steam_app_id"), title,
            ctx.get("developer") or "", ctx.get("publisher") or "")
        self._search_worker.done.connect(self._on_search_done)
        self._search_worker.start()

    def _on_search_done(self, result: dict):
        while self.body_layout.count():
            item = self.body_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        steam = result.get("steam")
        gog = result.get("gog")
        youtube = result.get("youtube") or []

        if not steam and not gog and not youtube:
            self.status_lbl.setText("No trailers found on any source.")
            self.status_lbl.setStyleSheet(f"color: {COLORS['text_muted']}; {_LABEL_BASE}")
            self._show_empty(
                "No trailers found on Steam, GOG, or YouTube for this game. "
                "Paste a URL directly above if you know one.")
            return

        n = (1 if steam else 0) + (1 if gog else 0) + len(youtube)
        self.status_lbl.setText(f"{n} result(s)")
        self.status_lbl.setStyleSheet("color: #4ade80; " + _LABEL_BASE)
        self.empty_lbl.setText("")

        if steam:
            self.body_layout.addWidget(_section_label("Steam"))
            row = _CandidateRow(
                steam["url"], "steam", "Official Steam trailer",
                "Direct video file — best playback quality later",
                steam.get("thumbnail"))
            row.picked.connect(self._on_candidate_picked)
            self.body_layout.addWidget(row)

        if gog:
            self.body_layout.addWidget(_section_label("GOG"))
            row = _CandidateRow(
                gog["url"], "gog", "Official GOG trailer",
                "Pre-curated — likely a YouTube video under the hood",
                gog.get("thumbnail"))
            row.picked.connect(self._on_candidate_picked)
            self.body_layout.addWidget(row)

        if youtube:
            self.body_layout.addWidget(_section_label(f"YouTube  ·  {len(youtube)} result(s)"))
            for cand in youtube:
                subtitle = cand.get("channel") or "YouTube"
                row = _CandidateRow(
                    cand["url"], "youtube", cand.get("title") or "(untitled)",
                    subtitle, cand.get("thumbnail"))
                row.picked.connect(self._on_candidate_picked)
                self.body_layout.addWidget(row)

    def _show_empty(self, message: str):
        while self.body_layout.count():
            item = self.body_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.empty_lbl.setText(message)
        self.body_layout.addWidget(self.empty_lbl)

    # ── Selection ─────────────────────────────────────────────────────────────

    def _on_candidate_picked(self, url: str, source: str):
        self._selected_url = url
        self._selected_source = source
        self.accept()

    def _on_manual_use(self):
        url = self.manual_edit.text().strip()
        if not url:
            return
        self._selected_url = url
        self._selected_source = "manual"
        self.accept()

    # ── Result ────────────────────────────────────────────────────────────────

    def selected_url(self) -> Optional[str]:
        return self._selected_url

    def selected_source(self) -> Optional[str]:
        """One of 'steam' | 'gog' | 'youtube' | 'manual', matching the
        values db.set_trailer()'s source column already uses for
        automatic detection — a manual pick from this dialog is stored
        the same way a hand-typed URL would be."""
        return self._selected_source
