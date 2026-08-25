"""
ui/cover_art_picker_dialog.py — Manual Cover Art Picker for VaultPlay

Spec: Notion → Features → Fully Planned → Manual Cover Art Picker.

Opened from Edit Metadata's "Browse SteamGridDB…" button on the Cover,
Hero, and Logo fields (see ui/edit_metadata_dialog.py's _ArtField).

Two-panel dialog: a scrollable thumbnail grid on the left, a larger
preview + "Use This" on the right. A manual search field is always
visible at the top so the user can override the automatic search at
any time.

Resolution order for the initial auto-search (see
metadata.sgdb_get_art_options()):
    1. sgdb_id       — exact match, no search needed
    2. steam_app_id  — SGDB's Steam-platform lookup, no search needed
    3. cleaned name  — SGDB autocomplete, first hit's id used

Static images only — no animated/video results are requested or shown.
Icon art is NOT supported yet (matches spec — Cover/Hero/Logo only).
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
    QWidget, QFrame, QScrollArea, QGridLayout, QSizePolicy
)
from PyQt6.QtCore import Qt, pyqtSignal, QThread, QRunnable, QThreadPool, pyqtSlot, QObject, QSize
from PyQt6.QtGui import QFont, QPixmap

import metadata as meta_mod
from ui.style import COLORS, accent_button_style

log = logging.getLogger(__name__)

_LABEL_BASE = "background: transparent; border: none;"

# (tile_width, tile_height) per art kind — mirrors each kind's real aspect
# ratio (cover ~2:3, hero ~3.1:1, logo boxed since it's usually transparent).
TILE_SIZES = {
    "cover": (130, 195),
    "hero":  (220, 71),
    "logo":  (160, 110),
}

KIND_LABELS = {"cover": "Cover", "hero": "Hero", "logo": "Logo"}


# ── Async helpers ──────────────────────────────────────────────────────────────

class _SearchWorker(QThread):
    """Runs metadata.sgdb_get_art_options() off the UI thread."""
    done = pyqtSignal(dict)   # {"images": [...], "resolved_sgdb_id":, "source":}

    def __init__(self, kind: str, key: str, sgdb_id, steam_app_id, search_name):
        super().__init__()
        self._kind = kind
        self._key = key
        self._sgdb_id = sgdb_id
        self._steam_app_id = steam_app_id
        self._search_name = search_name

    def run(self):
        result = meta_mod.sgdb_get_art_options(
            self._kind, self._key,
            sgdb_id=self._sgdb_id,
            steam_app_id=self._steam_app_id,
            search_name=self._search_name,
        )
        self.done.emit(result)


class _ThumbSignals(QObject):
    loaded = pyqtSignal(str, str)   # url, local_path


class _ThumbLoader(QRunnable):
    """Background download+cache of one grid thumbnail or the preview image.
    Mirrors the ImageLoader pattern used throughout the app (library_view.py,
    edit_metadata_dialog.py) — reuses metadata.download_art()'s local cache
    so re-opening the picker for the same game doesn't re-download."""

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
                self.signals.loaded.emit(self.url, path)
        except Exception as e:
            log.debug("_ThumbLoader failed for %s: %s", self.url, e)


# ── Grid thumbnail widget ──────────────────────────────────────────────────────

class _ArtThumb(QFrame):
    """One selectable thumbnail in the grid. Emits `picked` with its image
    dict when clicked; the dialog handles exclusive-selection styling."""
    picked = pyqtSignal(dict)

    def __init__(self, image: dict, tile_w: int, tile_h: int, parent=None):
        super().__init__(parent)
        self.image = image
        self._selected = False
        self.setFixedSize(tile_w + 8, tile_h + 8)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._apply_style()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        self.pic = QLabel()
        self.pic.setFixedSize(tile_w, tile_h)
        self.pic.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pic.setStyleSheet(f"background: {COLORS['surface3']}; border-radius: 4px;")
        layout.addWidget(self.pic)

    def _apply_style(self):
        border = f"2px solid {COLORS['accent']}" if self._selected else f"1px solid {COLORS['border']}"
        bg = "rgba(232,199,106,0.08)" if self._selected else COLORS["surface2"]
        self.setStyleSheet(f"""
            QFrame {{
                background: {bg};
                border: {border};
                border-radius: 8px;
            }}
        """)

    def set_selected(self, val: bool):
        self._selected = val
        self._apply_style()

    def set_pixmap(self, pix: QPixmap):
        scaled = pix.scaled(
            self.pic.width(), self.pic.height(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation
        )
        self.pic.setPixmap(scaled)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.picked.emit(self.image)
        super().mousePressEvent(event)


# ── Main dialog ───────────────────────────────────────────────────────────────

class CoverArtPickerDialog(QDialog):
    """
    kind: "cover" | "hero" | "logo"
    context_provider: zero-arg callable returning
        {"sgdb_id": int|None, "steam_app_id": int|None, "title": str}
    called at search time (not just once at open) so this reflects
    whatever the parent Edit Metadata dialog's fields currently hold —
    e.g. a Steam App ID the user just typed but hasn't saved yet.
    """

    def __init__(self, kind: str, context_provider: Callable[[], dict],
                 sgdb_key: str, parent=None):
        super().__init__(parent)
        self._kind = kind
        self._context_provider = context_provider
        self._sgdb_key = sgdb_key
        self._selected_image: Optional[dict] = None
        self._thumbs: list[_ArtThumb] = []
        self._search_worker: Optional[_SearchWorker] = None
        self._pool = QThreadPool.globalInstance()

        tile_w, tile_h = TILE_SIZES.get(kind, (140, 140))
        label = KIND_LABELS.get(kind, kind.title())

        self.setWindowTitle(f"Browse SteamGridDB — {label}")
        self.setModal(True)
        self.setMinimumSize(760, 560)
        self.resize(820, 600)
        self.setStyleSheet(f"QDialog {{ background: {COLORS['surface']}; }}")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Header + search bar (always visible) ────────────────────────────
        hdr = QWidget()
        hdr.setStyleSheet(f"background: {COLORS['surface']};")
        hdr_l = QVBoxLayout(hdr)
        hdr_l.setContentsMargins(20, 16, 20, 12)
        hdr_l.setSpacing(8)

        title_lbl = QLabel(f"Browse SteamGridDB — {label} Art")
        title_lbl.setFont(QFont("Rajdhani", 16, QFont.Weight.Bold))
        title_lbl.setStyleSheet(_LABEL_BASE)
        hdr_l.addWidget(title_lbl)

        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        self.search_edit = QLineEdit()
        self.search_edit.setFont(QFont("DM Sans", 10))
        self.search_edit.setPlaceholderText("Search SteamGridDB by name…")
        self.search_edit.returnPressed.connect(self._on_manual_search)
        search_row.addWidget(self.search_edit, 1)

        search_btn = QPushButton("Search")
        search_btn.clicked.connect(self._on_manual_search)
        search_row.addWidget(search_btn)
        hdr_l.addLayout(search_row)

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

        # ── Body: grid (left) + preview (right) ──────────────────────────────
        body = QWidget()
        body.setStyleSheet(f"background: {COLORS['surface']};")
        body_l = QHBoxLayout(body)
        body_l.setContentsMargins(0, 0, 0, 0)
        body_l.setSpacing(0)

        self.grid_scroll = QScrollArea()
        self.grid_scroll.setWidgetResizable(True)
        self.grid_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.grid_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.grid_canvas = QWidget()
        self.grid_canvas.setStyleSheet(f"background: {COLORS['surface']};")
        self.grid_layout = QGridLayout(self.grid_canvas)
        self.grid_layout.setContentsMargins(16, 16, 16, 16)
        self.grid_layout.setSpacing(10)
        self.grid_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

        self.empty_lbl = QLabel("")
        self.empty_lbl.setFont(QFont("DM Sans", 11))
        self.empty_lbl.setStyleSheet(f"color: {COLORS['text_muted']}; {_LABEL_BASE}")
        self.empty_lbl.setWordWrap(True)
        self.grid_layout.addWidget(self.empty_lbl, 0, 0)

        self.grid_scroll.setWidget(self.grid_canvas)
        body_l.addWidget(self.grid_scroll, 1)

        # Preview panel
        preview_panel = QWidget()
        preview_panel.setFixedWidth(280)
        preview_panel.setStyleSheet(
            f"background: {COLORS['surface2']}; border-left: 1px solid {COLORS['border']};")
        pp_l = QVBoxLayout(preview_panel)
        pp_l.setContentsMargins(16, 16, 16, 16)
        pp_l.setSpacing(10)
        pp_l.setAlignment(Qt.AlignmentFlag.AlignTop)

        pp_header = QLabel("PREVIEW")
        pp_header.setFont(QFont("DM Mono", 8))
        pp_header.setStyleSheet(
            f"color: {COLORS['text_muted']}; letter-spacing: 2px; {_LABEL_BASE}")
        pp_l.addWidget(pp_header)

        self.preview_label = QLabel("Select an image")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setFixedHeight(260)
        self.preview_label.setStyleSheet(
            f"background: {COLORS['surface3']}; border: 1px solid {COLORS['border']};"
            f" border-radius: 8px; color: {COLORS['text_muted']};")
        self.preview_label.setWordWrap(True)
        pp_l.addWidget(self.preview_label)

        self.preview_info = QLabel("")
        self.preview_info.setFont(QFont("DM Mono", 9))
        self.preview_info.setStyleSheet(f"color: {COLORS['text_muted']}; {_LABEL_BASE}")
        self.preview_info.setWordWrap(True)
        pp_l.addWidget(self.preview_info)

        pp_l.addStretch()

        self.use_btn = QPushButton("Use This →")
        self.use_btn.setFont(QFont("Rajdhani", 13, QFont.Weight.Bold))
        self.use_btn.setStyleSheet(accent_button_style())
        self.use_btn.setEnabled(False)
        self.use_btn.clicked.connect(self.accept)
        pp_l.addWidget(self.use_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        pp_l.addWidget(cancel_btn)

        body_l.addWidget(preview_panel, 0)
        root.addWidget(body, 1)

        # Kick off the automatic search using whatever context is available now.
        self._run_search(search_name=None)

    # ── Search ────────────────────────────────────────────────────────────────

    def _on_manual_search(self):
        text = self.search_edit.text().strip()
        if not text:
            return
        self._run_search(search_name=text)

    def _run_search(self, search_name: Optional[str]):
        if not self._sgdb_key:
            self._show_empty(
                "No SteamGridDB API key configured.\n"
                "Add one in Settings → API Keys, then try again.")
            return

        if self._search_worker and self._search_worker.isRunning():
            return

        ctx = self._context_provider() or {}
        sgdb_id = None if search_name else ctx.get("sgdb_id")
        steam_app_id = None if search_name else ctx.get("steam_app_id")
        name = search_name or ctx.get("title") or ""

        self._clear_grid()
        self.status_lbl.setText("Searching…")
        self.status_lbl.setStyleSheet(f"color: {COLORS['text_muted']}; {_LABEL_BASE}")

        self._search_worker = _SearchWorker(
            self._kind, self._sgdb_key, sgdb_id, steam_app_id, name)
        self._search_worker.done.connect(self._on_search_done)
        self._search_worker.start()

    def _on_search_done(self, result: dict):
        images = result.get("images") or []
        source = result.get("source") or ""

        if not images:
            self._show_empty(
                f"No {KIND_LABELS.get(self._kind, self._kind)} images found "
                f"({source or 'no match'}).\nTry a manual search above.")
            return

        self.status_lbl.setText(f"{len(images)} result(s)  ·  matched via {source}")
        self.status_lbl.setStyleSheet("color: #4ade80; " + _LABEL_BASE)
        self._populate_grid(images)

    def _show_empty(self, message: str):
        self._clear_grid()
        self.status_lbl.setText("")
        self.empty_lbl.setText(message)
        self.grid_layout.addWidget(self.empty_lbl, 0, 0)

    # ── Grid population ──────────────────────────────────────────────────────

    def _clear_grid(self):
        for i in reversed(range(self.grid_layout.count())):
            item = self.grid_layout.takeAt(i)
            w = item.widget()
            if w and w is not self.empty_lbl:
                w.deleteLater()
        self._thumbs.clear()
        self.empty_lbl.setText("")

    def _populate_grid(self, images: list):
        tile_w, tile_h = TILE_SIZES.get(self._kind, (140, 140))
        # Rough column count based on the scroll viewport width — good enough
        # for a one-shot grid (no virtualization needed at this result count).
        avail_w = max(tile_w + 8, self.grid_scroll.viewport().width() - 32)
        cols = max(1, avail_w // (tile_w + 8 + 10))

        for idx, image in enumerate(images):
            thumb = _ArtThumb(image, tile_w, tile_h)
            thumb.picked.connect(self._on_thumb_picked)
            self._thumbs.append(thumb)
            self.grid_layout.addWidget(thumb, idx // cols, idx % cols)

            loader = _ThumbLoader(image.get("thumb") or image.get("url"))
            loader.signals.loaded.connect(
                lambda url, path, t=thumb: self._on_thumb_loaded(t, path))
            self._pool.start(loader)

    def _on_thumb_loaded(self, thumb: "_ArtThumb", local_path: str):
        pix = QPixmap(local_path)
        if not pix.isNull():
            thumb.set_pixmap(pix)

    # ── Selection ─────────────────────────────────────────────────────────────

    def _on_thumb_picked(self, image: dict):
        self._selected_image = image
        for t in self._thumbs:
            t.set_selected(t.image is image)

        self.use_btn.setEnabled(True)
        w, h = image.get("width", 0), image.get("height", 0)
        style = image.get("style", "") or "—"
        self.preview_info.setText(f"Style: {style}    ·    {w}×{h}")

        self.preview_label.setText("Loading…")
        loader = _ThumbLoader(image.get("url"))
        loader.signals.loaded.connect(self._on_preview_loaded)
        self._pool.start(loader)

    def _on_preview_loaded(self, url: str, local_path: str):
        if not self._selected_image or self._selected_image.get("url") != url:
            return
        pix = QPixmap(local_path)
        if pix.isNull():
            return
        scaled = pix.scaled(
            self.preview_label.width(), self.preview_label.height(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation
        )
        self.preview_label.setPixmap(scaled)
        self.preview_label.setStyleSheet(
            f"background: {COLORS['surface3']}; border: 1px solid {COLORS['border']};"
            " border-radius: 8px;")

    # ── Result ────────────────────────────────────────────────────────────────

    def selected_url(self) -> Optional[str]:
        """Full-resolution URL of the chosen image, or None if cancelled."""
        return self._selected_image.get("url") if self._selected_image else None
