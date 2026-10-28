"""
ui/game_detail.py — Game detail page for VaultPlay

Shows:
  - Hero banner image (async loaded)
  - Cover thumbnail
  - Title, tags, developer info
  - Full description
  - Screenshot grid (async loaded)
  - Metadata info card (right column)
  - Install / Launch card (right column)
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
import os
import re
import shutil
import subprocess
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QFrame, QGridLayout, QSizePolicy, QMessageBox, QComboBox,
    QTextEdit, QLineEdit, QCompleter
)
from PyQt6.QtCore import (
    Qt, pyqtSignal, QRunnable, QThreadPool, pyqtSlot, QObject, QTimer,
    QStringListModel, QPointF, QRectF
)
from PyQt6.QtGui import (
    QPixmap, QFont, QColor, QPainter, QLinearGradient, QBrush, QPolygonF,
    QPainterPath, QPen
)

import db
import metadata as meta_mod
import scanner
import protondb as protondb_mod
import save_backup
import wishlist_store
from ui.style import COLORS, accent_button_style, card_style
from ui.install_dialog import InstallDialog
from ui.cogwheel_menu import CogwheelButton

log = logging.getLogger(__name__)


class NoScrollComboBox(QComboBox):
    """QComboBox that ignores scroll wheel events — same pattern used in
    install_dialog.py / cogwheel_menu.py, so accidental scrolling over the
    dropdown never silently changes a game's completion status."""
    def wheelEvent(self, event):
        event.ignore()


# Matches db.VALID_COMPLETION_STATUSES — (value, display label) in display order.
COMPLETION_STATUS_OPTIONS = [
    ("unplayed",    "Unplayed"),
    ("in_progress", "In Progress"),
    ("completed",   "Completed"),
    ("abandoned",   "Abandoned"),
]


class ImageSignals(QObject):
    loaded = pyqtSignal(str, str)   # url, local_path


class ImageLoader(QRunnable):
    def __init__(self, url: str):
        super().__init__()
        self.url = url
        self.signals = ImageSignals()

    @pyqtSlot()
    def run(self):
        path = meta_mod.download_art(self.url)
        if path:
            self.signals.loaded.emit(self.url, path)


class _WishlistArtSignals(QObject):
    loaded = pyqtSignal(str)   # local_path


class _WishlistArtLoader(QRunnable):
    """
    Resolves a wishlist item's cover_url the same way wishlist_view.py's
    own _ThumbLoader does — either a wishlist_art/... path (resolved
    against this machine's current wishlist_path, no network) or a plain
    URL (downloaded+cached via metadata.download_art(), same as every
    other art field in the app). Kept as its own small loader here rather
    than importing wishlist_view.py's, matching this codebase's existing
    convention of each file owning its own near-identical loader class.
    """
    def __init__(self, cover_value: str):
        super().__init__()
        self.cover_value = cover_value
        self.signals = _WishlistArtSignals()

    @pyqtSlot()
    def run(self):
        try:
            if wishlist_store.is_local_art_path(self.cover_value):
                resolved = wishlist_store.resolve_cover_path(self.cover_value)
            else:
                resolved = meta_mod.download_art(self.cover_value)
            if resolved:
                self.signals.loaded.emit(resolved)
        except Exception as e:
            log.debug("_WishlistArtLoader failed for %s: %s", self.cover_value, e)


_YOUTUBE_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?v=|embed/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{6,})"
)


def _youtube_thumbnail_url(url: str) -> str | None:
    """
    Best-effort thumbnail URL for a YouTube link, using the well-known
    predictable img.youtube.com pattern — no API call needed, just a
    regex pull of the video id out of the usual URL shapes. Returns None
    for anything that doesn't look like a YouTube URL (Steam/GOG videos
    aren't YouTube-hosted the same way, and a fully generic video page has
    no thumbnail VaultPlay can derive on its own). This is a stopgap for
    manually-pasted trailer links only — once automatic Steam/GOG/YouTube
    detection exists (see trailers.py, planned), that pipeline will supply
    a real thumbnail URL directly and this heuristic won't be needed for
    those cases.
    """
    if not url:
        return None
    m = _YOUTUBE_ID_RE.search(url)
    if not m:
        return None
    return f"https://img.youtube.com/vi/{m.group(1)}/hqdefault.jpg"


class TrailerThumbnail(QLabel):
    """
    Clickable video thumbnail with a centered play-button overlay —
    mirrors a Steam store page's trailer tile (thumbnail loads, clicking
    opens the video). No in-app playback yet: clicking always opens the
    URL externally via xdg-open, the same thing the wishlist tile's old
    on-cover trailer badge did. This is the one call site that changes
    when in-app playback is eventually built — see the Notion Wishlist /
    Game Detail Redesign pages for that still-open decision.

    Hidden entirely (see set_trailer()) when there's no trailer to show —
    callers don't need to guard visibility themselves.
    """

    WIDTH  = 440
    HEIGHT = 248   # 16:9

    def __init__(self, parent=None):
        super().__init__(parent)
        self._url = ""
        self._pix: QPixmap | None = None
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            f"background: {COLORS['surface2']}; border: 1px solid {COLORS['border']};"
            " border-radius: 8px;")

    def set_trailer(self, url: str):
        """Show/hide and reset for a (possibly blank) trailer URL. Caller
        is responsible for kicking off thumbnail loading separately (see
        game_detail.py's _load_trailer_thumbnail()) once this returns."""
        self._url = (url or "").strip()
        self._pix = None
        self.setVisible(bool(self._url))
        self.update()

    def set_thumbnail_pixmap(self, pix: QPixmap):
        if pix.isNull():
            return
        scaled = pix.scaled(
            self.WIDTH, self.HEIGHT,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation)
        x = max(0, (scaled.width() - self.WIDTH) // 2)
        y = max(0, (scaled.height() - self.HEIGHT) // 2)
        self._pix = scaled.copy(x, y, self.WIDTH, self.HEIGHT)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._pix is not None:
            painter.drawPixmap(0, 0, self._pix)
        else:
            painter.fillRect(self.rect(), QColor(COLORS["surface2"]))
        # Dark scrim so the play glyph reads clearly over any thumbnail.
        painter.fillRect(self.rect(), QColor(0, 0, 0, 90))

        cx, cy = self.width() // 2, self.height() // 2
        r = 30
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(255, 255, 255, 235)))
        painter.drawEllipse(QPointF(cx, cy), r, r)
        tri = QPolygonF([
            QPointF(cx - 10, cy - 15),
            QPointF(cx - 10, cy + 15),
            QPointF(cx + 16, cy),
        ])
        painter.setBrush(QBrush(QColor(COLORS["bg"])))
        painter.drawPolygon(tri)
        painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._url:
            _open_video(self._url)
            event.accept()
            return
        super().mousePressEvent(event)


class HeroBanner(QLabel):
    """Fixed-height label that shows a blurred/dark hero image with gradient overlay."""

    HEIGHT = 340

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(self.HEIGHT)
        self._bg_pix = None
        self.setStyleSheet(f"background: {COLORS['surface2']};")

    def set_image(self, local_path: str):
        pix = QPixmap(local_path)
        if not pix.isNull():
            self._bg_pix = pix.scaled(
                self.width() or 1280,
                self.HEIGHT,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation
            )
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        if self._bg_pix:
            # Centre crop
            x = (self._bg_pix.width() - self.width()) // 2
            painter.drawPixmap(0, 0, self._bg_pix, max(0, x), 0, self.width(), self.HEIGHT)
        else:
            painter.fillRect(self.rect(), QColor(COLORS["surface2"]))

        # Gradient overlay bottom → top
        grad = QLinearGradient(0, 0, 0, self.HEIGHT)
        grad.setColorAt(0.0,  QColor(13, 15, 20, 50))
        grad.setColorAt(0.45, QColor(13, 15, 20, 130))
        grad.setColorAt(1.0,  QColor(13, 15, 20, 255))
        painter.fillRect(self.rect(), grad)
        painter.end()


# ── Media gallery (Automatic Trailer Detection & Media Gallery) ──────────────
# Spec: Notion → Features → Fully Planned → Automatic Trailer Detection &
# Media Gallery. Replaces the old plain Screenshots grid for real library
# games: one Steam-store-style carousel — a big preview area on top, a
# horizontally scrollable thumbnail strip underneath mixing trailers and
# screenshots, click a thumbnail to swap the preview.
#
# Items come from db.get_media_gallery() (see trailers.build_media_gallery()):
#   {"type": "video"|"image", "url", "thumbnail_url", "source", "label"}
# Clicking a VIDEO item's big preview opens it with xdg-open, exactly like
# TrailerThumbnail does for wishlist items — no in-app playback yet. Wishlist
# mode keeps the single TrailerThumbnail and never shows this widget.

_GALLERY_SOURCE_LABELS = {
    "steam": "Steam", "gog": "GOG", "youtube": "YouTube",
    "manual": "Pinned", "igdb": "IGDB",
}


def _open_video(url: str):
    """
    Open a trailer URL externally. Steam trailers are now HLS (.m3u8) / DASH
    (.mpd) streams, which a browser won't play and xdg-open often has no
    handler for — so try a real media player first, then fall back to the
    Steam store page (parsed from the CDN path), then plain xdg-open.
    """
    url = url or ""
    base = url.split("?", 1)[0].lower()
    if base.endswith((".m3u8", ".mpd")):
        for player in ("mpv", "vlc", "celluloid", "haruna", "smplayer", "ffplay"):
            exe = shutil.which(player)
            if not exe:
                continue
            try:
                args = [exe, url]
                if player == "ffplay":
                    args = [exe, "-autoexit", url]
                subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                return
            except Exception as e:
                log.debug("Player %s failed for %s: %s", player, url, e)
        m = re.search(r"/store_trailers/(\d+)/", url)
        if m:
            url = f"https://store.steampowered.com/app/{m.group(1)}"
    try:
        subprocess.Popen(["xdg-open", url])
    except Exception as e:
        log.warning("Could not open trailer URL %s: %s", url, e)


def _cover_crop(pix: QPixmap, w: int, h: int) -> QPixmap:
    """Scale pix to fill w×h (keeping aspect ratio) and center-crop the overflow."""
    scaled = pix.scaled(
        max(1, w), max(1, h),
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation)
    x = max(0, (scaled.width() - w) // 2)
    y = max(0, (scaled.height() - h) // 2)
    return scaled.copy(x, y, w, h)


def _paint_play_button(p: QPainter, w: int, h: int, radius: int = 34):
    """Dim overlay + white circle + dark triangle, centered on a w×h area —
    same look TrailerThumbnail paints, as a plain helper so the gallery
    preview doesn't need to embed that widget."""
    p.fillRect(0, 0, w, h, QColor(0, 0, 0, 70))
    cx, cy = w / 2, h / 2
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor(255, 255, 255, 235)))
    p.drawEllipse(QPointF(cx, cy), radius, radius)
    p.setBrush(QBrush(QColor(13, 15, 20, 230)))
    t = radius * 0.5
    p.drawPolygon(QPolygonF([
        QPointF(cx - t * 0.45, cy - t),
        QPointF(cx - t * 0.45, cy + t),
        QPointF(cx + t * 0.95, cy),
    ]))


class _GalleryPreview(QWidget):
    """The big preview area. Height follows the width at 16:9 (capped), and
    the image is scaled to FIT inside it with its aspect ratio preserved —
    never stretched or cropped — centered on a dark panel, like Steam's own
    media viewer."""
    clicked = pyqtSignal()
    MAX_HEIGHT = 480
    MIN_HEIGHT = 220

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(self.MAX_HEIGHT)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._pix: QPixmap | None = None
        self._is_video = False
        self._scaled: QPixmap | None = None
        self._scaled_key = None
        self._ring = False   # Controller Support focus ring

    def set_ring(self, on: bool):
        self._ring = on
        self.update()

    def set_content(self, pix, is_video: bool):
        self._pix = pix
        self._is_video = is_video
        self._scaled_key = None
        self.setCursor(Qt.CursorShape.PointingHandCursor if is_video
                       else Qt.CursorShape.ArrowCursor)
        self.update()

    def resizeEvent(self, event):
        h = max(self.MIN_HEIGHT, min(self.MAX_HEIGHT, int(event.size().width() * 9 / 16)))
        if h != self.height():
            self.setFixedHeight(h)
        super().resizeEvent(event)

    def _image_rect(self):
        """Where the (aspect-preserving) image sits inside the widget."""
        w, h = self.width(), self.height()
        if self._pix is None or self._pix.isNull():
            return QRectF(0, 0, w, h)
        pw, ph = self._pix.width(), self._pix.height()
        scale = min(w / pw, h / ph)
        iw, ih = pw * scale, ph * scale
        return QRectF((w - iw) / 2, (h - ih) / 2, iw, ih)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(0, 0, w, h), 10, 10)
        p.setClipPath(clip)
        p.fillRect(self.rect(), QColor(COLORS["surface2"]))

        if self._pix is not None and not self._pix.isNull():
            r = self._image_rect()
            tw, th = max(1, round(r.width())), max(1, round(r.height()))
            key = (self._pix.cacheKey(), tw, th)
            if key != self._scaled_key:
                # Scaling a 1080p screenshot is not free — only redo it when
                # the image or the widget size actually changed.
                self._scaled = self._pix.scaled(
                    tw, th, Qt.AspectRatioMode.IgnoreAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
                self._scaled_key = key
            p.drawPixmap(round(r.x()), round(r.y()), self._scaled)
        elif not self._is_video:
            p.setPen(QColor(COLORS["text_muted"]))
            p.setFont(QFont("DM Mono", 9))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Loading…")

        if self._is_video:
            _paint_play_button(p, w, h)

        p.setClipping(False)
        p.setBrush(Qt.BrushStyle.NoBrush)
        if self._ring:
            p.setPen(QPen(QColor(COLORS["accent"]), 3))
            p.drawRoundedRect(QRectF(1.5, 1.5, w - 3, h - 3), 10, 10)
        else:
            p.setPen(QPen(QColor(255, 255, 255, 20), 1))
            p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 10, 10)
        p.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._is_video:
            self.clicked.emit()
        super().mousePressEvent(event)


class _GalleryThumb(QWidget):
    """One thumbnail in the strip. Video items get a small corner TRAILER
    badge so they're distinguishable from screenshots at a glance; unselected
    thumbnails are slightly dimmed, the selected one gets an accent border."""
    clicked = pyqtSignal(int)   # index into the gallery's item list
    W, H = 160, 90

    def __init__(self, index: int, is_video: bool, parent=None):
        super().__init__(parent)
        self._index = index
        self._is_video = is_video
        self._pix: QPixmap | None = None
        self._selected = False
        self.setFixedSize(self.W, self.H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_image(self, pix: QPixmap):
        self._pix = _cover_crop(pix, self.W, self.H)
        self.update()

    def set_selected(self, selected: bool):
        if self._selected != selected:
            self._selected = selected
            self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(0, 0, self.W, self.H), 6, 6)
        p.setClipPath(clip)

        p.fillRect(self.rect(), QColor(COLORS["surface2"]))
        if self._pix is not None:
            p.drawPixmap(0, 0, self._pix)
        if not self._selected:
            p.fillRect(self.rect(), QColor(13, 15, 20, 95))

        if self._is_video:
            p.setFont(QFont("DM Mono", 7, QFont.Weight.DemiBold))
            text = "▶ TRAILER"
            tw = p.fontMetrics().horizontalAdvance(text) + 10
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(13, 15, 20, 200)))
            p.drawRoundedRect(QRectF(5, 5, tw, 15), 4, 4)
            p.setPen(QColor(255, 255, 255, 235))
            p.drawText(QRectF(5, 5, tw, 15), Qt.AlignmentFlag.AlignCenter, text)

        p.setClipping(False)
        p.setBrush(Qt.BrushStyle.NoBrush)
        if self._selected:
            p.setPen(QPen(QColor(COLORS["accent"]), 2))
            p.drawRoundedRect(QRectF(1, 1, self.W - 2, self.H - 2), 6, 6)
        else:
            p.setPen(QPen(QColor(255, 255, 255, 20), 1))
            p.drawRoundedRect(QRectF(0.5, 0.5, self.W - 1, self.H - 1), 6, 6)
        p.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._index)
        super().mousePressEvent(event)


class _HorizontalScrollArea(QScrollArea):
    """A QScrollArea whose mouse wheel scrolls HORIZONTALLY — the thumbnail
    strip has no vertical content, so a plain wheel over it should move
    through the strip rather than being swallowed. Falls through to normal
    (page) scrolling when the strip has nothing to scroll."""

    def wheelEvent(self, event):
        bar = self.horizontalScrollBar()
        if bar.maximum() > 0:
            bar.setValue(bar.value() - event.angleDelta().y())
            event.accept()
        else:
            event.ignore()


class MediaGalleryWidget(QWidget):
    """
    Steam-style media gallery: big preview + scrollable thumbnail strip.
    Self-contained — call set_items() with the gallery list and it handles
    its own async image loading, selection, and click-to-open. Hides itself
    when there's nothing to show.
    """

    # Preview tops out at 480px tall at 16:9 = ~854 wide; the whole widget
    # (preview + thumbnail strip + caption) is capped to that width so the
    # strip scrolls instead of the section stretching across the page with
    # empty bars either side of the image.
    MAX_WIDTH = 854

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMaximumWidth(self.MAX_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._items: list = []
        self._thumbs: list = []
        self._pix_cache: dict = {}      # url -> raw loaded QPixmap
        self._requested: set = set()    # urls with a load already in flight/done
        self._current = -1
        self._pool = QThreadPool.globalInstance()

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        self.preview = _GalleryPreview()
        self.preview.clicked.connect(self._on_preview_clicked)
        root.addWidget(self.preview)

        self.strip_scroll = _HorizontalScrollArea()
        self.strip_scroll.setWidgetResizable(True)
        self.strip_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.strip_scroll.setFixedHeight(_GalleryThumb.H + 34)
        self.strip_scroll.setMinimumWidth(0)
        self.strip_scroll.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.strip_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.strip_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Chunky horizontal scrollbar (easy to grab and drag) — the default
        # one was only a few px tall and hard to hit.
        self.strip_scroll.setStyleSheet("""
            QScrollArea { background: transparent; border: none; }
            QScrollBar:horizontal {
                height: 16px; margin: 4px 0 0 0;
                background: rgba(255,255,255,0.06); border-radius: 6px;
            }
            QScrollBar::handle:horizontal {
                min-width: 48px; background: rgba(255,255,255,0.30);
                border-radius: 6px;
            }
            QScrollBar::handle:horizontal:hover { background: rgba(255,255,255,0.50); }
            QScrollBar::handle:horizontal:pressed { background: rgba(255,255,255,0.65); }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
                width: 0; height: 0; background: none; border: none;
            }
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
                background: none;
            }
        """)

        self.strip_body = QWidget()
        self.strip_body.setStyleSheet("background: transparent;")
        self.strip_layout = QHBoxLayout(self.strip_body)
        self.strip_layout.setContentsMargins(0, 0, 0, 6)
        self.strip_layout.setSpacing(8)
        self.strip_layout.addStretch()
        self.strip_scroll.setWidget(self.strip_body)
        root.addWidget(self.strip_scroll)

        self.caption = QLabel("")
        self.caption.setFont(QFont("DM Mono", 9))
        self.caption.setStyleSheet(
            f"color: {COLORS['text_muted']}; background: transparent;")
        root.addWidget(self.caption)

        self.hide()

    # ── Public API ────────────────────────────────────────────────────────

    def has_items(self) -> bool:
        return bool(self._items)

    # ── Controller Support ────────────────────────────────────────────────

    def controller_step(self, delta: int) -> bool:
        """Select the previous/next gallery item. False at either end."""
        i = self._current + delta
        if not (0 <= i < len(self._items)):
            return False
        self._select(i)
        return True

    def controller_activate(self):
        """A button — plays the selected item if it's a video."""
        self._on_preview_clicked()

    def set_controller_focused(self, on: bool):
        self.preview.set_ring(on)

    def clear(self):
        for t in self._thumbs:
            self.strip_layout.removeWidget(t)
            t.deleteLater()
        self._thumbs = []
        self._items = []
        self._pix_cache.clear()
        self._requested.clear()
        self._current = -1
        self.preview.set_content(None, False)
        self.caption.setText("")
        self.hide()

    def set_items(self, items: list):
        self.clear()
        valid = [i for i in (items or []) if isinstance(i, dict) and i.get("url")]
        if not valid:
            return
        self._items = valid
        for idx, item in enumerate(valid):
            thumb = _GalleryThumb(idx, item.get("type") == "video")
            thumb.clicked.connect(self._select)
            self._thumbs.append(thumb)
            self.strip_layout.insertWidget(idx, thumb)
            self._request(self._thumb_url(item))
        self.show()
        self._select(0)

    # ── Internals ─────────────────────────────────────────────────────────

    @staticmethod
    def _thumb_url(item: dict):
        """URL to use for an item's thumbnail: its own thumbnail_url, else
        (videos) a YouTube-derived thumbnail when the video URL is a YouTube
        one, else (images) the image itself. None when nothing's available —
        the thumb just keeps its plain placeholder."""
        t = item.get("thumbnail_url")
        if t:
            return t
        if item.get("type") == "video":
            return _youtube_thumbnail_url(item.get("url") or "")
        return item.get("url")

    def _request(self, url):
        if not url or url in self._pix_cache or url in self._requested:
            return
        self._requested.add(url)
        loader = ImageLoader(url)
        loader.signals.loaded.connect(self._on_loaded)
        self._pool.start(loader)

    def _on_loaded(self, url: str, local_path: str):
        pix = QPixmap(local_path)
        if pix.isNull():
            return
        self._pix_cache[url] = pix
        for idx, item in enumerate(self._items):
            if self._thumb_url(item) == url and idx < len(self._thumbs):
                self._thumbs[idx].set_image(pix)
        if 0 <= self._current < len(self._items):
            self._refresh_preview()

    def _select(self, index: int):
        if not (0 <= index < len(self._items)):
            return
        self._current = index
        for i, t in enumerate(self._thumbs):
            t.set_selected(i == index)
        item = self._items[index]
        if item.get("type") != "video":
            # Full-size image is loaded lazily, only for the one being
            # viewed — the strip itself only ever downloads thumbnails.
            self._request(item.get("url"))
        self._refresh_preview()
        self.strip_scroll.ensureWidgetVisible(self._thumbs[index], 40, 0)

        label = item.get("label") or ("Trailer" if item.get("type") == "video"
                                      else "Screenshot")
        source = _GALLERY_SOURCE_LABELS.get(item.get("source") or "", "")
        text = f"{label}   ·   {source}" if source else label
        if item.get("type") == "video":
            text += "   ·   click to watch"
        self.caption.setText(text)

    def _refresh_preview(self):
        item = self._items[self._current]
        if item.get("type") == "video":
            pix = self._pix_cache.get(self._thumb_url(item))
            self.preview.set_content(pix, True)
        else:
            pix = (self._pix_cache.get(item.get("url"))
                   or self._pix_cache.get(self._thumb_url(item)))
            self.preview.set_content(pix, False)

    def _on_preview_clicked(self):
        if not (0 <= self._current < len(self._items)):
            return
        item = self._items[self._current]
        if item.get("type") != "video":
            return
        _open_video(item["url"])


class InfoRow(QWidget):
    def __init__(self, label: str, value: str, mono_value: bool = False, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 7, 0, 7)
        layout.setSpacing(8)

        lbl = QLabel(label)
        lbl.setFont(QFont("DM Sans", 11))
        lbl.setStyleSheet(f"color: {COLORS['text_muted']};")
        lbl.setFixedWidth(100)

        val = QLabel(value or "—")
        val.setFont(QFont("DM Mono" if mono_value else "DM Sans", 10 if mono_value else 11))
        val.setStyleSheet(
            f"color: {COLORS['accent2']};" if mono_value
            else f"color: {COLORS['text']}; font-weight: 500;"
        )
        val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        val.setWordWrap(True)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {COLORS['border']};")

        layout.addWidget(lbl)
        layout.addWidget(val, 1)

        # Separator below
        outer = QVBoxLayout()
        outer.setSpacing(0)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addLayout(layout)
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"background: {COLORS['border']}; border: none; max-height: 1px;")
        outer.addWidget(sep2)
        # Can't re-set layout, so just use the HBox and add separator to parent
        self.setLayout(layout)
        self._sep = sep2

    def add_to(self, parent_layout):
        parent_layout.addWidget(self)
        parent_layout.addWidget(self._sep)


class _TagChip(QFrame):
    """One removable pill-shaped tag chip on the game detail page's Tags
    card. Purely a display widget — GameDetailView owns the actual
    add/remove DB calls (see _on_tag_remove_clicked)."""
    remove_clicked = pyqtSignal(int)   # tag_id

    def __init__(self, tag_id: int, name: str, parent=None):
        super().__init__(parent)
        self.tag_id = tag_id
        self.setStyleSheet(f"""
            QFrame {{
                background: rgba(232,199,106,0.10);
                border: 1px solid rgba(232,199,106,0.3);
                border-radius: 12px;
            }}
        """)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 3, 6, 3)
        layout.setSpacing(6)

        name_lbl = QLabel(name)
        name_lbl.setFont(QFont("DM Sans", 10))
        name_lbl.setStyleSheet(f"color: {COLORS['accent']}; background: transparent; border: none;")
        layout.addWidget(name_lbl)

        remove_btn = QPushButton("✕")
        remove_btn.setFixedSize(16, 16)
        remove_btn.setFont(QFont("DM Sans", 8))
        remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        remove_btn.setToolTip("Remove tag")
        remove_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; border: none;
                color: {COLORS['accent']}; padding: 0;
            }}
            QPushButton:hover {{ color: {COLORS['danger']}; }}
        """)
        remove_btn.clicked.connect(lambda: self.remove_clicked.emit(self.tag_id))
        layout.addWidget(remove_btn)


class GameDetailView(QWidget):
    back_requested    = pyqtSignal()
    install_finished  = pyqtSignal(int)   # game_id
    # Emitted after a successful launch — main window starts PlaytimeWatcher.
    # Carries everything the watcher needs: game_id, Popen handle, wine_bin, wine_prefix.
    # Using object type for proc since PyQt6 can't signal with subprocess.Popen directly.
    game_launched     = pyqtSignal(int, object, str, str)  # game_id, proc, wine_bin, wine_prefix
    # Cogwheel → "Force Quit", re-emitted upward since this view doesn't own
    # the PlaytimeWatcher/proc handle needed to actually kill anything —
    # MainWindow does (see CogwheelButton.force_quit_requested).
    force_quit_requested = pyqtSignal(int)   # game_id
    # Collections / Playlists — emitted after Add to Collection… changes
    # membership, so MainWindow can refresh the sidebar's game-count badges
    # (same "something changed elsewhere, refresh" pattern install_finished
    # already establishes for favorite/hide/completion-status changes).
    collections_changed = pyqtSignal()
    # Wishlist mode — emitted after the Edit Wishlist Item dialog saves, or
    # after Remove from Wishlist, so MainWindow can refresh the sidebar's
    # Wishlist count badge (mirrors WishlistView.changed's role for the
    # Wishlist grid page itself).
    wishlist_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._game_id = None
        # Wishlist mode — see load_wishlist_item(). "game" (default) reads
        # from the games/metadata/installs/game_state tables via db.py as
        # this class always has; "wishlist" reads from wishlist_store.py
        # instead and hides every section that has no wishlist-side
        # equivalent (install/launch, playtime, tags, collections,
        # completion status, version tracking, ProtonDB). Checked by
        # _apply_mode_visibility() and the notes-save/back-button-label
        # branches below rather than threaded through every method
        # individually.
        self._mode = "game"                 # "game" | "wishlist"
        self._wishlist_item_id: str | None = None
        self._pool = QThreadPool.globalInstance()
        # Currently Playing Indicator — set via set_currently_playing() by
        # MainWindow, which owns the actual PlaytimeWatcher/running state.
        # None = no game currently running (the common case).
        self._currently_playing_game_id: int | None = None

        # Controller Support (Phase B) — focus is tracked by widget
        # reference, not index: these buttons persist for the page's
        # whole lifetime (shown/hidden, never destroyed/recreated), unlike
        # the virtualized library grid, so a plain reference is safe here.
        # Only the right column's primary action buttons + the completion
        # status combo are gamepad-reachable; deep per-row controls (tag
        # chips, screenshots, collection submenu) stay mouse-only for now.
        self._ctrl_focused_widget = None
        self._ctrl_focus_visible = False
        self._ctrl_orig_style: dict = {}   # id(widget) -> its original styleSheet

        # Stale-art guards: the URL each async art load is currently allowed
        # to paint. A loader finishing for any other URL (e.g. the previous
        # game's) is ignored, and load_game()/load_wishlist_item() reset the
        # hero + cover up front so a game with no art shows none.
        self._hero_url_expected = ""
        self._cover_url_expected = ""
        self._trailer_thumb_expected = ""
        self._wishlist_art_token = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Back button bar
        back_bar = QWidget()
        back_bar.setFixedHeight(44)
        back_bar.setStyleSheet(f"background: {COLORS['surface']}; border-bottom: 1px solid {COLORS['border']};")
        back_layout = QHBoxLayout(back_bar)
        back_layout.setContentsMargins(20, 0, 20, 0)

        self.back_btn = QPushButton("← Back to Library")
        self.back_btn.setFont(QFont("DM Sans", 11))
        self.back_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                border: none;
                color: {COLORS['text_muted']};
                padding: 0;
            }}
            QPushButton:hover {{ color: {COLORS['text']}; }}
        """)
        self.back_btn.clicked.connect(self.back_requested)
        back_layout.addWidget(self.back_btn)
        back_layout.addStretch()
        root.addWidget(back_bar)

        # Scrollable body
        scroll = QScrollArea()
        self.scroll = scroll   # Controller Support — right-stick scroll target
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        body = QWidget()
        body.setStyleSheet(f"background: {COLORS['bg']};")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        # Hero banner
        self.hero = HeroBanner()
        body_layout.addWidget(self.hero)

        # Hero content overlay (title, tags, etc.) — positioned below hero
        self.hero_content = QWidget()
        self.hero_content.setStyleSheet(f"background: {COLORS['bg']};")
        hero_content_layout = QHBoxLayout(self.hero_content)
        hero_content_layout.setContentsMargins(48, 16, 48, 0)
        hero_content_layout.setSpacing(24)

        self.cover_img = QLabel()
        self.cover_img.setFixedSize(110, 165)
        self.cover_img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_img.setStyleSheet(f"""
            background: {COLORS['surface2']};
            border: 2px solid rgba(255,255,255,0.12);
            border-radius: 8px;
        """)
        hero_content_layout.addWidget(self.cover_img, 0, Qt.AlignmentFlag.AlignTop)

        meta_widget = QWidget()
        meta_layout = QVBoxLayout(meta_widget)
        meta_layout.setContentsMargins(0, 0, 0, 0)
        meta_layout.setSpacing(6)

        self.title_label = QLabel("")
        self.title_label.setFont(QFont("Rajdhani", 28, QFont.Weight.Bold))
        self.title_label.setStyleSheet(f"color: {COLORS['text']};")
        self.title_label.setWordWrap(True)
        meta_layout.addWidget(self.title_label)

        self.tags_layout = QHBoxLayout()
        self.tags_layout.setSpacing(6)
        self.tags_layout.setContentsMargins(0, 0, 0, 0)
        meta_layout.addLayout(self.tags_layout)

        self.dev_label = QLabel("")
        self.dev_label.setFont(QFont("DM Sans", 11))
        self.dev_label.setStyleSheet(f"color: {COLORS['text_muted']};")
        meta_layout.addWidget(self.dev_label)

        hero_content_layout.addWidget(meta_widget, 1)
        body_layout.addWidget(self.hero_content)

        # Main two-column body
        main_section = QWidget()
        main_section.setStyleSheet(f"background: {COLORS['bg']};")
        main_layout = QHBoxLayout(main_section)
        main_layout.setContentsMargins(48, 28, 48, 48)
        main_layout.setSpacing(40)
        main_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        # Left column
        left = QWidget()
        left.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.left_layout = QVBoxLayout(left)
        self.left_layout.setContentsMargins(0, 0, 0, 0)
        self.left_layout.setSpacing(0)

        # Trailer — hidden unless the loaded game/wishlist item has one.
        # Sits above Screenshots, mirroring a Steam store page's media
        # order (trailer first, then shots). See TrailerThumbnail's
        # docstring for the no-in-app-playback-yet contract.
        self._trailer_title = self._section_title(self.left_layout, "Trailer")
        self.trailer_thumb = TrailerThumbnail()
        self.left_layout.addWidget(self.trailer_thumb)
        self._trailer_title.hide()
        self.trailer_thumb.hide()
        self.left_layout.addSpacing(24)

        # Real-game mode only — trailers + screenshots in one carousel (see
        # MediaGalleryWidget). Wishlist mode keeps the standalone Trailer
        # section above and never shows this. The attribute keeps its old
        # _screenshots_title name so _apply_mode_visibility()'s existing
        # toggling still applies; the label text is what changed.
        self._screenshots_title = self._section_title(self.left_layout, "Media")
        self.media_gallery = MediaGalleryWidget()
        # Left-aligned and capped at MediaGalleryWidget.MAX_WIDTH: the row's
        # trailing stretch soaks up whatever the gallery can't use.
        self._gallery_row = QWidget()
        _gallery_row_lay = QHBoxLayout(self._gallery_row)
        _gallery_row_lay.setContentsMargins(0, 0, 0, 0)
        _gallery_row_lay.setSpacing(0)
        _gallery_row_lay.addWidget(self.media_gallery, 1)
        _gallery_row_lay.addStretch(0)
        self.left_layout.addWidget(self._gallery_row)
        self.left_layout.addSpacing(28)

        # About This Game sits BELOW the media carousel (real-game mode only;
        # hidden in wishlist mode by _apply_mode_visibility()).
        self._about_title = self._section_title(self.left_layout, "About This Game")
        self.desc_label = QLabel("No description available.")
        self.desc_label.setFont(QFont("DM Sans", 12))
        self.desc_label.setStyleSheet(f"color: {COLORS['text_dim']}; line-height: 1.7;")
        self.desc_label.setWordWrap(True)
        self.left_layout.addWidget(self.desc_label)
        # RAWG's free-tier terms require an active link back to RAWG wherever
        # its data is shown. Only visible when a RAWG key is configured and
        # auto-fill is on (i.e. when RAWG data could be on the page).
        self.rawg_credit = QLabel(
            '<a href="https://rawg.io" style="color: inherit;">'
            'Some game details provided by RAWG</a>')
        self.rawg_credit.setFont(QFont("DM Sans", 9))
        self.rawg_credit.setStyleSheet(f"color: {COLORS['text_muted']};")
        self.rawg_credit.setOpenExternalLinks(True)
        self.rawg_credit.setContentsMargins(0, 6, 0, 0)
        self.rawg_credit.hide()
        self.left_layout.addWidget(self.rawg_credit)
        self.left_layout.addSpacing(24)
        self.left_layout.addStretch()

        # Right column
        right = QWidget()
        right.setFixedWidth(300)
        right.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        self.right_layout = QVBoxLayout(right)
        self.right_layout.setContentsMargins(0, 0, 0, 0)
        self.right_layout.setSpacing(14)
        self.right_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        # Info card
        self.info_card = QFrame()
        self.info_card.setStyleSheet(f"""
            QFrame {{
                background: {COLORS['surface']};
                border: 1px solid {COLORS['border']};
                border-radius: 12px;
            }}
        """)
        self.info_card_layout = QVBoxLayout(self.info_card)
        self.info_card_layout.setContentsMargins(16, 14, 16, 14)
        self.info_card_layout.setSpacing(0)
        self._section_title(self.info_card_layout, "Game Info")
        self.right_layout.addWidget(self.info_card)

        # Library actions card (favorite + hide, or — in wishlist mode —
        # Edit Wishlist Item / Remove from Wishlist; see
        # _apply_mode_visibility())
        self.library_card = QFrame()
        self.library_card.setStyleSheet(f"""
            QFrame {{
                background: {COLORS['surface']};
                border: 1px solid {COLORS['border']};
                border-radius: 12px;
            }}
        """)
        library_card_layout = QVBoxLayout(self.library_card)
        library_card_layout.setContentsMargins(16, 14, 16, 14)
        library_card_layout.setSpacing(8)
        self._section_title(library_card_layout, "Library")

        lib_btn_row = QHBoxLayout()
        lib_btn_row.setSpacing(8)
        lib_btn_row.setContentsMargins(0, 0, 0, 0)

        self.fav_btn = QPushButton("☆  Favorite")
        self.fav_btn.setFont(QFont("DM Sans", 11))
        self.fav_btn.setCheckable(False)
        self.fav_btn.clicked.connect(self._on_favorite_clicked)
        lib_btn_row.addWidget(self.fav_btn)

        self.hide_btn = QPushButton("⊘  Hide")
        self.hide_btn.setFont(QFont("DM Sans", 11))
        self.hide_btn.setCheckable(False)
        self.hide_btn.clicked.connect(self._on_hide_clicked)
        lib_btn_row.addWidget(self.hide_btn)

        library_card_layout.addLayout(lib_btn_row)

        self.track_versions_btn = QPushButton("🔔  Track Version Updates…")
        self.track_versions_btn.setFont(QFont("DM Sans", 11))
        self.track_versions_btn.setStyleSheet(f"""
            QPushButton {{
                background: {COLORS['surface2']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
                color: {COLORS['text_muted']};
                padding: 7px 10px;
                text-align: left;
            }}
            QPushButton:hover {{
                color: {COLORS['text']};
                background: {COLORS['surface3']};
            }}
        """)
        self.track_versions_btn.clicked.connect(self._on_track_versions_clicked)
        library_card_layout.addWidget(self.track_versions_btn)

        self.edit_metadata_btn = QPushButton("✏️  Edit Metadata…")
        self.edit_metadata_btn.setFont(QFont("DM Sans", 11))
        self.edit_metadata_btn.setStyleSheet(f"""
            QPushButton {{
                background: {COLORS['surface2']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
                color: {COLORS['text_muted']};
                padding: 7px 10px;
                text-align: left;
            }}
            QPushButton:hover {{
                color: {COLORS['text']};
                background: {COLORS['surface3']};
            }}
        """)
        self.edit_metadata_btn.clicked.connect(self._on_edit_metadata_clicked)
        library_card_layout.addWidget(self.edit_metadata_btn)

        self.add_to_collection_btn = QPushButton("➕  Add to Collection…")
        self.add_to_collection_btn.setFont(QFont("DM Sans", 11))
        self.add_to_collection_btn.setStyleSheet(f"""
            QPushButton {{
                background: {COLORS['surface2']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
                color: {COLORS['text_muted']};
                padding: 7px 10px;
                text-align: left;
            }}
            QPushButton:hover {{
                color: {COLORS['text']};
                background: {COLORS['surface3']};
            }}
        """)
        self.add_to_collection_btn.clicked.connect(self._on_add_to_collection_clicked)
        library_card_layout.addWidget(self.add_to_collection_btn)

        # ── Wishlist-mode actions — same card, different buttons. Built
        # here (not lazily) so _apply_mode_visibility() can just toggle
        # .show()/.hide() like every other mode-gated widget rather than
        # constructing/destroying widgets on every load.
        self.wishlist_edit_btn = QPushButton("✏️  Edit Wishlist Item…")
        self.wishlist_edit_btn.setFont(QFont("DM Sans", 11))
        self.wishlist_edit_btn.setStyleSheet(f"""
            QPushButton {{
                background: {COLORS['surface2']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
                color: {COLORS['text_muted']};
                padding: 7px 10px;
                text-align: left;
            }}
            QPushButton:hover {{
                color: {COLORS['text']};
                background: {COLORS['surface3']};
            }}
        """)
        self.wishlist_edit_btn.clicked.connect(self._on_wishlist_edit_clicked)
        library_card_layout.addWidget(self.wishlist_edit_btn)

        self.wishlist_remove_btn = QPushButton("✕  Remove from Wishlist")
        self.wishlist_remove_btn.setFont(QFont("DM Sans", 11))
        self.wishlist_remove_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                border: 1px solid rgba(248,113,113,0.3);
                border-radius: 8px;
                color: {COLORS['danger']};
                padding: 7px 10px;
                text-align: left;
            }}
            QPushButton:hover {{
                background: rgba(248,113,113,0.08);
                border-color: {COLORS['danger']};
                color: #fca5a5;
            }}
        """)
        self.wishlist_remove_btn.clicked.connect(self._on_wishlist_remove_clicked)
        library_card_layout.addWidget(self.wishlist_remove_btn)

        self.right_layout.addWidget(self.library_card)

        # ── Tags Card ─────────────────────────────────────────────────────────
        self.tags_card = QFrame()
        self.tags_card.setStyleSheet(f"""
            QFrame {{
                background: {COLORS['surface']};
                border: 1px solid {COLORS['border']};
                border-radius: 12px;
            }}
        """)
        tags_card_layout = QVBoxLayout(self.tags_card)
        tags_card_layout.setContentsMargins(16, 14, 16, 14)
        tags_card_layout.setSpacing(8)
        self._section_title(tags_card_layout, "Tags")

        # Horizontally-scrolling chip row — keeps the card's height fixed
        # regardless of how many tags a game has, rather than wrapping and
        # growing the whole right column.
        self.tags_scroll = QScrollArea()
        self.tags_scroll.setWidgetResizable(True)
        self.tags_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.tags_scroll.setFixedHeight(34)
        self.tags_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.tags_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.tags_chip_row = QWidget()
        self.tags_chip_row.setStyleSheet("background: transparent;")
        self.tags_chip_layout = QHBoxLayout(self.tags_chip_row)
        self.tags_chip_layout.setContentsMargins(0, 0, 0, 0)
        self.tags_chip_layout.setSpacing(6)
        self.tags_chip_layout.addStretch()
        self.tags_scroll.setWidget(self.tags_chip_row)
        tags_card_layout.addWidget(self.tags_scroll)

        self.tags_empty_lbl = QLabel("No tags yet.")
        self.tags_empty_lbl.setFont(QFont("DM Sans", 10))
        self.tags_empty_lbl.setStyleSheet(f"color: {COLORS['text_muted']}; background: transparent;")
        tags_card_layout.addWidget(self.tags_empty_lbl)

        tag_add_row = QHBoxLayout()
        tag_add_row.setContentsMargins(0, 0, 0, 0)
        tag_add_row.setSpacing(6)

        self.tag_input = QLineEdit()
        self.tag_input.setFont(QFont("DM Sans", 10))
        self.tag_input.setPlaceholderText("Add a tag…")
        self.tag_input.returnPressed.connect(self._on_tag_add)
        self._tag_completer = QCompleter([])
        self._tag_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.tag_input.setCompleter(self._tag_completer)
        tag_add_row.addWidget(self.tag_input, 1)

        tag_add_btn = QPushButton("+")
        tag_add_btn.setFixedWidth(30)
        tag_add_btn.setFont(QFont("DM Sans", 12, QFont.Weight.Bold))
        tag_add_btn.clicked.connect(self._on_tag_add)
        tag_add_row.addWidget(tag_add_btn)

        tags_card_layout.addLayout(tag_add_row)
        self.right_layout.addWidget(self.tags_card)

        # Install card
        self.install_card = QFrame()
        self.install_card.setStyleSheet(f"""
            QFrame {{
                background: {COLORS['surface']};
                border: 1px solid {COLORS['border']};
                border-radius: 12px;
            }}
        """)
        install_card_layout = QVBoxLayout(self.install_card)
        install_card_layout.setContentsMargins(16, 14, 16, 16)
        install_card_layout.setSpacing(10)
        self._section_title(install_card_layout, "Install Options")

        # Tag info label (shows detected type)
        self.tag_info_label = QLabel("")
        self.tag_info_label.setFont(QFont("DM Mono", 9))
        self.tag_info_label.setStyleSheet(f"color: {COLORS['text_muted']}; background: transparent;")
        install_card_layout.addWidget(self.tag_info_label)

        self.install_btn = QPushButton("↓  Install Game")
        self.install_btn.setFont(QFont("Rajdhani", 13, QFont.Weight.Bold))
        self.install_btn.setStyleSheet(accent_button_style())
        self.install_btn.clicked.connect(self._on_install_clicked)
        install_card_layout.addWidget(self.install_btn)

        # Already have it set up outside VaultPlay (or lost the DB entry)?
        # Re-create the install record from an existing install folder,
        # wine prefix and/or .desktop file. Only visible when not installed.
        self.locate_btn = QPushButton("📂  Already installed? Locate Game…")
        self.locate_btn.setFont(QFont("DM Sans", 11))
        self.locate_btn.setStyleSheet(f"""
            QPushButton {{
                background: {COLORS['surface2']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
                color: {COLORS['text_muted']};
                padding: 8px 14px;
            }}
            QPushButton:hover {{
                color: {COLORS['text']};
                background: {COLORS['surface3']};
            }}
        """)
        self.locate_btn.clicked.connect(self._on_locate_clicked)
        install_card_layout.addWidget(self.locate_btn)

        # Launch button + Cogwheel Menu button, side by side (Blizzard-style —
        # see Cogwheel Menu spec). Cogwheel only ever shows for installed
        # games, launch-detected or not (the warning state still needs it,
        # to fix paths for an install VaultPlay didn't detect itself).
        launch_row = QHBoxLayout()
        launch_row.setContentsMargins(0, 0, 0, 0)
        launch_row.setSpacing(8)

        self.launch_btn = QPushButton("▶  Launch Game")
        self.launch_btn.setFont(QFont("Rajdhani", 13, QFont.Weight.Bold))
        self.launch_btn.setStyleSheet(accent_button_style())
        self.launch_btn.clicked.connect(self._on_launch_clicked)
        self.launch_btn.hide()
        launch_row.addWidget(self.launch_btn, 1)

        self.cogwheel_btn = CogwheelButton()
        self.cogwheel_btn.game_changed.connect(self._on_cogwheel_changed)
        self.cogwheel_btn.debug_launch_requested.connect(self._on_debug_launch_requested)
        self.cogwheel_btn.force_quit_requested.connect(self.force_quit_requested)
        self.cogwheel_btn.hide()
        launch_row.addWidget(self.cogwheel_btn, 0)

        install_card_layout.addLayout(launch_row)

        # Shown when game is installed but exe_path is empty/missing
        self.exe_missing_label = QLabel(
            "⚠ Launch exe not detected — open the game folder to run manually."
        )
        self.exe_missing_label.setFont(QFont("DM Sans", 10))
        self.exe_missing_label.setStyleSheet(
            f"color: {COLORS['accent']}; background: transparent;")
        self.exe_missing_label.setWordWrap(True)
        self.exe_missing_label.hide()
        install_card_layout.addWidget(self.exe_missing_label)

        self.open_folder_btn = QPushButton("📁  Open Game Folder")
        self.open_folder_btn.setFont(QFont("DM Sans", 11))
        self.open_folder_btn.setStyleSheet(f"""
            QPushButton {{
                background: {COLORS['surface2']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
                color: {COLORS['text_muted']};
                padding: 8px 14px;
            }}
            QPushButton:hover {{
                color: {COLORS['text']};
                background: {COLORS['surface3']};
            }}
        """)
        self.open_folder_btn.clicked.connect(self._on_open_folder)
        self.open_folder_btn.hide()
        install_card_layout.addWidget(self.open_folder_btn)

        self.uninstall_btn = QPushButton("✕  Uninstall")
        self.uninstall_btn.setFont(QFont("DM Sans", 11))
        self.uninstall_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                border: 1px solid rgba(248,113,113,0.3);
                border-radius: 8px;
                color: {COLORS['danger']};
                padding: 7px 14px;
            }}
            QPushButton:hover {{
                background: rgba(248,113,113,0.08);
                border-color: {COLORS['danger']};
                color: #fca5a5;
            }}
        """)
        self.uninstall_btn.clicked.connect(self._on_uninstall_clicked)
        self.uninstall_btn.hide()
        install_card_layout.addWidget(self.uninstall_btn)

        self.right_layout.addWidget(self.install_card)

        # ── Notes Card — last in the right column, per Game Detail UI ──────────
        # Layout spec (the newer, cross-feature-aware source of truth —
        # supersedes Per-Game Notes' own older "inside the Info Card" note).
        # Plain freetext, no formatting/markdown. Starts read-only showing
        # either the saved text or a muted placeholder; Edit switches it to
        # an editable QTextEdit and the button becomes Save.
        self.notes_card = QFrame()
        self.notes_card.setStyleSheet(f"""
            QFrame {{
                background: {COLORS['surface']};
                border: 1px solid {COLORS['border']};
                border-radius: 12px;
            }}
        """)
        notes_card_layout = QVBoxLayout(self.notes_card)
        notes_card_layout.setContentsMargins(16, 14, 16, 14)
        notes_card_layout.setSpacing(8)
        self._section_title(notes_card_layout, "Notes")

        self.notes_edit = QTextEdit()
        self.notes_edit.setFixedHeight(120)
        self.notes_edit.setReadOnly(True)
        self.notes_edit.setPlaceholderText("Add notes…")
        self.notes_edit.setStyleSheet(f"""
            QTextEdit {{
                background: {COLORS['surface2']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
                color: {COLORS['text']};
                padding: 8px 10px;
                font-family: 'DM Sans';
                font-size: 12px;
            }}
        """)
        notes_card_layout.addWidget(self.notes_edit)

        self.notes_edit_btn = QPushButton("✎  Edit")
        self.notes_edit_btn.setFont(QFont("DM Sans", 11))
        self.notes_edit_btn.setStyleSheet(f"""
            QPushButton {{
                background: {COLORS['surface2']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
                color: {COLORS['text_muted']};
                padding: 7px 14px;
            }}
            QPushButton:hover {{
                color: {COLORS['text']};
                background: {COLORS['surface3']};
            }}
        """)
        self.notes_edit_btn.clicked.connect(self._on_notes_edit_save_clicked)
        notes_card_layout.addWidget(self.notes_edit_btn)

        self.right_layout.addWidget(self.notes_card)

        main_layout.addWidget(left, 1)
        main_layout.addWidget(right, 0, Qt.AlignmentFlag.AlignTop)

        body_layout.addWidget(main_section, 1)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

    def _section_title(self, layout, text: str) -> QLabel:
        lbl = QLabel(text.upper())
        lbl.setFont(QFont("DM Mono", 9, QFont.Weight.DemiBold))
        lbl.setStyleSheet(f"color: {COLORS['text_muted']}; letter-spacing: 2px; padding: 0 0 8px 0;")
        layout.addWidget(lbl)
        return lbl

    def _tag(self, text: str, color: str = None, bg: str = None) -> QLabel:
        tag = QLabel(text)
        tag.setFont(QFont("DM Mono", 9))
        c = color or COLORS["text_dim"]
        b = bg or "rgba(255,255,255,0.07)"
        # Use setStyleSheet with simple concatenation to avoid f-string parse issues
        tag.setStyleSheet(
            "QLabel { color: " + c + "; background: " + b + "; "
            "border: 1px solid rgba(255,255,255,0.1); "
            "border-radius: 4px; padding: 2px 8px; }"
        )
        return tag

    # ── Mode-gated visibility ─────────────────────────────────────────────

    def _apply_mode_visibility(self):
        """
        Show/hide every section that only makes sense for one of the two
        modes. Called once per load, right after self._mode is set, before
        either load path populates its own content — keeps "what's visible
        in this mode" defined in exactly one place instead of scattered
        across load_game()/load_wishlist_item().
        """
        is_game = (self._mode == "game")

        self.back_btn.setText("← Back to Library" if is_game else "← Back to Wishlist")

        # Library card: favorite/hide/track/edit-metadata/add-to-collection
        # vs. edit-wishlist-item/remove-from-wishlist.
        self.fav_btn.setVisible(is_game)
        self.hide_btn.setVisible(is_game)
        self.track_versions_btn.setVisible(is_game)
        self.edit_metadata_btn.setVisible(is_game)
        self.add_to_collection_btn.setVisible(is_game)
        self.wishlist_edit_btn.setVisible(not is_game)
        self.wishlist_remove_btn.setVisible(not is_game)

        # Sections with no wishlist-side equivalent at all.
        self.tags_card.setVisible(is_game)
        self.install_card.setVisible(is_game)
        self._about_title.setVisible(is_game)
        self.desc_label.setVisible(is_game)
        try:
            _rawg_on = bool(db.get_setting("rawg_api_key", "").strip()) and \
                db.get_setting("rawg_auto_fill", "true") == "true"
        except Exception:
            _rawg_on = False
        self.rawg_credit.setVisible(is_game and _rawg_on)
        self._screenshots_title.setVisible(is_game)

        if not is_game:
            self.media_gallery.clear()

    def _reset_art(self):
        """Clear hero + cover back to the empty placeholder. Called on every
        load so a game with no art never inherits the previous page's art."""
        self.hero._bg_pix = None
        self.hero.update()
        self.cover_img.setPixmap(QPixmap())
        self.cover_img.setStyleSheet(f"""
            background: {COLORS['surface2']};
            border: 2px solid rgba(255,255,255,0.12);
            border-radius: 8px;
        """)

    def _reset_art_cover_only(self):
        self.cover_img.setPixmap(QPixmap())
        self.cover_img.setStyleSheet(f"""
            background: {COLORS['surface2']};
            border: 2px solid rgba(255,255,255,0.12);
            border-radius: 8px;
        """)

    # ── Load game ─────────────────────────────────────────────────────────────

    def load_game(self, game_id: int):
        # Reset art only when switching to a different game/mode; a reload of
        # the same game (favorite toggle etc.) keeps its art without flicker.
        _switching = (self._mode != "game" or self._game_id != game_id)
        self._mode = "game"
        self._wishlist_item_id = None
        self._wishlist_art_token = None
        if _switching:
            self._reset_art()
            self._hero_url_expected = ""
            self._cover_url_expected = ""
        self._apply_mode_visibility()
        self._ctrl_focused_widget = None

        self._game_id = game_id
        game = db.get_game(game_id)
        if not game:
            return

        title = game["title"] or game["display_name"] or game["folder_name"]
        self.title_label.setText(title)

        # Developer line
        dev_parts = []
        if game["developer"]:
            dev_parts.append(f"Developed by {game['developer']}")
        if game["publisher"] and game["publisher"] != game["developer"]:
            dev_parts.append(f"Published by {game['publisher']}")
        self.dev_label.setText(" · ".join(dev_parts) if dev_parts else "")

        # Tags
        while self.tags_layout.count():
            item = self.tags_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if game["genres"]:
            try:
                genres = json.loads(game["genres"]) if isinstance(game["genres"], str) else game["genres"]
                for g in genres[:3]:
                    t = self._tag(g, COLORS["accent2"],
                                  "rgba(91,141,238,0.08)")
                    self.tags_layout.addWidget(t)
            except Exception:
                pass

        if game["release_date"]:
            self.tags_layout.addWidget(self._tag(str(game["release_date"])[:4]))

        # Install tag badge (installer / portable / iso)
        try:
            raw_tag = game["install_tag"]
        except (IndexError, KeyError):
            raw_tag = None
        if raw_tag:
            tag_lbl, tag_color, tag_bg = _install_tag_label(raw_tag)
            t = QLabel(tag_lbl)
            t.setFont(QFont("DM Mono", 9))
            border = tag_color.replace(")", ", 0.35)").replace("rgb(", "rgba(") if "rgb" in tag_color else tag_color
            t.setStyleSheet(f"""
                QLabel {{
                    color: {tag_color};
                    background: {tag_bg};
                    border: 1px solid {tag_color.replace(")", "").replace("rgb(","rgba(") + ", 0.35)" if "rgb" in tag_color else tag_color.replace("#", "rgba(") };
                    border-radius: 4px;
                    padding: 2px 8px;
                }}
            """)
            self.tags_layout.addWidget(t)

        if game["is_installed"]:
            t = self._tag("● Installed", COLORS["installed"], "rgba(74,222,128,0.07)")
            self.tags_layout.addWidget(t)

        # ProtonDB badge
        try:
            tier = game["protondb_tier"] if game["protondb_tier"] else None
        except (IndexError, KeyError):
            tier = None
        if tier:
            label, color = protondb_mod.tier_display(tier)
            # Build background from color with low opacity
            bg = f"rgba({int(color[1:3],16)},{int(color[3:5],16)},{int(color[5:7],16)},0.10)"
            border = f"rgba({int(color[1:3],16)},{int(color[3:5],16)},{int(color[5:7],16)},0.35)"
            t = self._tag(f"ProtonDB: {label}", color, bg)
            t.setStyleSheet(f"""
                QLabel {{
                    color: {color};
                    background: {bg};
                    border: 1px solid {border};
                    border-radius: 4px;
                    padding: 2px 8px;
                    font-family: 'DM Mono';
                    font-size: 9px;
                }}
            """)
            self.tags_layout.addWidget(t)

        self.tags_layout.addStretch()

        # Description
        self.desc_label.setText(
            game["description"] or "No description available."
        )

        # Info card rows
        while self.info_card_layout.count() > 1:  # keep section title
            item = self.info_card_layout.takeAt(1)
            if item.widget():
                item.widget().deleteLater()

        # ProtonDB row
        proton_row_val = None
        try:
            _ptier = game["protondb_tier"]
            _prep  = game["protondb_reports"] or 0
        except (IndexError, KeyError):
            _ptier = None
            _prep  = 0
        if _ptier:
            label, _ = protondb_mod.tier_display(_ptier)
            proton_row_val = f"{label}  ({_prep} reports)"
        try:
            rec = game["recommended_proton"]
        except (IndexError, KeyError):
            rec = None
        if rec:
            proton_rec_val = protondb_mod.proton_version_label(rec)
        else:
            proton_rec_val = None

        try:
            raw_tag = game["install_tag"]
        except (IndexError, KeyError):
            raw_tag = None
        tag_display = _install_tag_label(raw_tag)[0] if raw_tag else None

        info_rows = [
            ("Developer",    game["developer"]),
            ("Publisher",    game["publisher"]),
            ("IP Holder",    game["ip_holder"]),
            ("Release Date", game["release_date"]),
            ("Genre",        ", ".join(json.loads(game["genres"])) if game["genres"] else None),
            ("Install Type", tag_display),
            ("ProtonDB",     proton_row_val),
            ("Best Proton",  proton_rec_val),
            ("NAS Size",     scanner.format_size(game["size_bytes"] or 0)),
        ]
        if game["is_installed"]:
            if game["wine_prefix"]:
                prefix_name = Path(game["wine_prefix"]).name
                info_rows.append(("Wine Prefix", prefix_name))
            if game["exe_path"]:
                info_rows.append(("Exe Path", game["exe_path"]))

        for label, value in info_rows:
            if value:
                row = _InfoRow(label, value, mono=(label == "Wine Prefix"))
                self.info_card_layout.addWidget(row)

        # ── Playtime row ──────────────────────────────────────────────────────
        try:
            playtime_mins = int(_safe_get(game, "playtime_minutes", 0) or 0)
        except (TypeError, ValueError):
            playtime_mins = 0
        last_played = _safe_get(game, "last_played")

        from playtime import format_playtime
        playtime_str = format_playtime(playtime_mins)
        playtime_row = _InfoRow("Playtime", playtime_str)
        self.info_card_layout.addWidget(playtime_row)

        if last_played:
            try:
                import datetime as _dt
                dt = _dt.datetime.fromisoformat(
                    str(last_played).replace("T", " ")[:19])
                delta = (_dt.datetime.utcnow() - dt).days
                if delta == 0:
                    lp_str = "Today"
                elif delta == 1:
                    lp_str = "Yesterday"
                else:
                    lp_str = f"{delta} days ago"
                lp_row = _InfoRow("Last Played", lp_str)
                self.info_card_layout.addWidget(lp_row)
            except (ValueError, TypeError):
                pass

        # ── Version tracking rows ─────────────────────────────────────────────
        # Always show, even when Unknown — gives user a clear hook to add trackers.
        try:
            best = db.get_best_versions_for_game(game_id)
        except Exception:
            best = {"dotted": None, "plain": None,
                    "dotted_url": None, "plain_url": None,
                    "dotted_checked_at": None, "plain_checked_at": None}

        import version_check as _vc
        for label, val_key, url_key, checked_key in [
            ("Latest (v)",     "dotted", "dotted_url", "dotted_checked_at"),
            ("Latest (build)", "plain",  "plain_url",  "plain_checked_at"),
        ]:
            val     = best.get(val_key)
            src_url = best.get(url_key)
            checked = best.get(checked_key)
            display = val or "Unknown"
            row = _VersionRow(label, display, src_url, checked)
            self.info_card_layout.addWidget(row)

        # ── NAS Version / Installed Version (Update & DLC Install Support) ─────
        # Distinct from Latest(v)/Latest(build) above, which come from the
        # web version tracker (a scraped page). These reflect what the
        # scanner itself detected directly from the NAS archive/exe/folder
        # name (NAS Version, refreshed on every scan), and what's actually
        # installed right now (Installed Version, set at install time /
        # after applying an update) — always shown, "Unknown" fallback,
        # never a blank row.
        try:
            nas_version = db.get_nas_version(game_id)
        except Exception:
            nas_version = {"dotted": None, "plain": None, "date": None}
        nas_ver_row = _InfoRow("NAS Version", _format_version_dict(nas_version))
        self.info_card_layout.addWidget(nas_ver_row)

        if game["is_installed"]:
            try:
                installed_version = db.get_installed_version(game_id)
            except Exception:
                installed_version = {"dotted": None, "plain": None, "date": None}
            installed_ver_row = _InfoRow(
                "Installed Ver.", _format_version_dict(installed_version))
            self.info_card_layout.addWidget(installed_ver_row)

        # ── Save Backup status ─────────────────────────────────────────────────
        # Linking happens automatically after the first play session when
        # the feature is enabled in Settings → Paths (Flow 1). On every
        # later view of this page, the symlink is re-checked (Flow 2's
        # cheap check, see save_backup.check_link_status) so a broken link
        # is visible here even without launching the game.
        if db.get_setting("save_backup_enabled", "false") == "true":
            try:
                save_paths = db.get_save_paths(game_id)
            except Exception:
                save_paths = {"save_path": None, "save_source_path": None}
            link_status = save_backup.check_link_status(
                save_paths.get("save_source_path"), save_paths.get("save_path"))
            if link_status == "ok":
                save_row = _InfoRow("Save Backup", save_paths["save_path"], mono=True)
            elif link_status == "broken":
                save_row = _InfoRow(
                    "Save Backup",
                    f"⚠ Link broken (was: {save_paths.get('save_path')})",
                    warn=True)
            else:
                save_row = _InfoRow("Save Backup", "Not yet linked")
            self.info_card_layout.addWidget(save_row)

        # ── Completion Status ──────────────────────────────────────────────────
        # Always last in the info card, per the Game Detail UI Layout spec.
        # Rebuilt every load_game() call (info_card_layout is cleared above),
        # same as every other row in this card.
        completion_row = QWidget()
        completion_row.setStyleSheet("background: transparent;")
        cr_layout = QHBoxLayout(completion_row)
        cr_layout.setContentsMargins(0, 10, 0, 0)
        cr_layout.setSpacing(8)

        cr_label = QLabel("Completion Status")
        cr_label.setFont(QFont("DM Sans", 11))
        cr_label.setStyleSheet(f"color: {COLORS['text_muted']}; background: transparent;")
        cr_layout.addWidget(cr_label)
        cr_layout.addStretch()

        self.completion_combo = NoScrollComboBox()
        self.completion_combo.setFont(QFont("DM Sans", 10))
        self.completion_combo.setFixedWidth(140)
        for value, label in COMPLETION_STATUS_OPTIONS:
            self.completion_combo.addItem(label, value)
        current_status = _safe_get(game, "completion_status", "unplayed") or "unplayed"
        idx = self.completion_combo.findData(current_status)
        if idx >= 0:
            self.completion_combo.setCurrentIndex(idx)
        # Connected after setCurrentIndex so restoring the saved value above
        # doesn't itself fire a redundant write back to the DB.
        self.completion_combo.currentIndexChanged.connect(self._on_completion_status_changed)
        cr_layout.addWidget(self.completion_combo)

        self.info_card_layout.addWidget(completion_row)

        # Install tag info
        try:
            raw_tag2 = game["install_tag"]
        except (IndexError, KeyError):
            raw_tag2 = None
        if raw_tag2:
            tag_lbl2, _, _ = _install_tag_label(raw_tag2)
            self.tag_info_label.setText(f"Detected type: {tag_lbl2}")
        else:
            self.tag_info_label.setText("")

        # Install / launch button state
        self.exe_missing_label.hide()
        self.open_folder_btn.hide()
        self.uninstall_btn.hide()

        if game["is_installed"]:
            self.install_btn.hide()
            self.locate_btn.hide()
            self.cogwheel_btn.set_game(game_id)
            self.cogwheel_btn.show()
            exe = (game["exe_path"] or "").strip()
            if exe and Path(exe).exists():
                self.launch_btn.show()
                self._launch_btn_available = True
                self.exe_missing_label.hide()
                self.open_folder_btn.hide()
            else:
                self._launch_btn_available = False
                # Installed but exe path missing or stale — cogwheel's
                # "Set Executable / Prefix / Install Paths…" is the fix.
                self.launch_btn.hide()
                self.exe_missing_label.show()
                install_path = (game["install_path"] or "").strip()
                if install_path:
                    self.open_folder_btn.show()
            self.uninstall_btn.show()
        else:
            self.install_btn.show()
            self.locate_btn.show()
            self.launch_btn.hide()
            self.cogwheel_btn.hide()
            self._launch_btn_available = False

        # Currently Playing Indicator — apply after the block above so it
        # can override "▶ Launch Game" → "Playing..." if this game happens
        # to already be running (e.g. navigating to it while it's playing,
        # or reattachment on app restart already having set the state
        # before this page was ever loaded).
        self._update_launch_button_state()

        # Update library action buttons (favorite / hide)
        self._update_library_buttons(game)

        # Media gallery — trailers + screenshots in one carousel. The title
        # is hidden outright when there's nothing to show (a game with no
        # trailer AND no screenshots), rather than leaving an empty section.
        self.media_gallery.set_items(self._build_gallery_items(game))
        self._screenshots_title.setVisible(self.media_gallery.has_items())

        # Async load hero + cover
        new_hero = game["hero_url"] or ""
        new_cover = game["cover_url"] or ""
        if new_hero != self._hero_url_expected:
            # URL changed (e.g. edited metadata) — drop the old hero.
            self.hero._bg_pix = None
            self.hero.update()
        if new_cover != self._cover_url_expected:
            self._reset_art_cover_only()
        self._hero_url_expected = new_hero
        self._cover_url_expected = new_cover

        if new_hero:
            loader = ImageLoader(new_hero)
            loader.signals.loaded.connect(self._on_hero_loaded)
            self._pool.start(loader)

        if new_cover:
            loader = ImageLoader(new_cover)
            loader.signals.loaded.connect(self._on_cover_loaded)
            self._pool.start(loader)

        # ── Trailer ───────────────────────────────────────────────────────────
        # Real-game mode no longer uses the standalone Trailer section — the
        # primary trailer is the first video in the media gallery above (see
        # _build_gallery_items()). Explicitly hidden here, rather than just
        # not shown, because this widget is reused across games AND modes: a
        # wishlist item viewed just before this game would otherwise leave
        # its Trailer section visible. Wishlist mode (load_wishlist_item)
        # still calls _load_trailer() with the item's own URL.
        self._load_trailer(None)

        # ── Notes ─────────────────────────────────────────────────────────────
        # Always reset to read-only view mode on load — a fresh page load
        # should never land mid-edit, even if the user had it open for a
        # different game last time this widget was reused.
        try:
            saved_notes = db.get_notes(game_id)
        except Exception:
            saved_notes = None
        self.notes_edit.setPlainText(saved_notes or "")
        self.notes_edit.setReadOnly(True)
        self.notes_edit_btn.setText("✎  Edit")

        # ── Tags ──────────────────────────────────────────────────────────────
        self.tag_input.clear()
        self._refresh_tags_display()

    # ── Load wishlist item ───────────────────────────────────────────────────

    def load_wishlist_item(self, item_id: str):
        """
        Wishlist mode — populates the same page shell load_game() uses,
        from wishlist_store.py instead of db.py, with every section that
        has no wishlist-side equivalent hidden by _apply_mode_visibility()
        (called first — see that method's docstring for the full list).
        """
        self._mode = "wishlist"
        self._wishlist_item_id = item_id
        self._hero_url_expected = ""
        self._cover_url_expected = ""
        self._wishlist_art_token = object()
        self._apply_mode_visibility()
        self._ctrl_focused_widget = None

        self._game_id = None
        item = wishlist_store.get_item(item_id)
        if not item:
            return

        title = item.get("title") or "(untitled)"
        self.title_label.setText(title)

        dev_parts = []
        developer = item.get("developer") or ""
        publisher = item.get("publisher") or ""
        if developer:
            dev_parts.append(f"Developed by {developer}")
        if publisher and publisher != developer:
            dev_parts.append(f"Published by {publisher}")
        self.dev_label.setText(" · ".join(dev_parts) if dev_parts else "")

        # Tags row: genres + release date + a "Wishlist" badge, mirroring
        # load_game()'s genre/release/status-badge row as closely as a
        # wishlist item's smaller data set allows.
        while self.tags_layout.count():
            titem = self.tags_layout.takeAt(0)
            if titem.widget():
                titem.widget().deleteLater()

        for g in (item.get("genres") or [])[:3]:
            t = self._tag(g, COLORS["accent2"], "rgba(91,141,238,0.08)")
            self.tags_layout.addWidget(t)

        release_date = (item.get("release_date") or "").strip()
        if release_date:
            self.tags_layout.addWidget(self._tag(release_date))

        self.tags_layout.addWidget(
            self._tag("🌟 Wishlist", COLORS["accent"], "rgba(232,199,106,0.10)"))
        self.tags_layout.addStretch()

        # ── Info card — just release date + genres (the fields a wishlist
        # item actually has); everything install/playtime/version/ProtonDB
        # related has no wishlist-side source and is skipped entirely
        # rather than shown as a row of "Unknown"s.
        while self.info_card_layout.count() > 1:
            citem = self.info_card_layout.takeAt(1)
            if citem.widget():
                citem.widget().deleteLater()

        info_rows = [
            ("Developer",    developer),
            ("Publisher",    publisher),
            ("Release Date", release_date),
            ("Genre",        ", ".join(item.get("genres") or [])),
        ]
        for label, value in info_rows:
            if value:
                row = _InfoRow(label, value)
                self.info_card_layout.addWidget(row)

        # ── Cover / hero — a wishlist item only has one image, used for both.
        self.cover_img.setPixmap(QPixmap())
        self.cover_img.setStyleSheet(f"""
            background: {COLORS['surface2']};
            border: 2px solid rgba(255,255,255,0.12);
            border-radius: 8px;
        """)
        self.hero._bg_pix = None
        self.hero.update()
        cover_url = item.get("cover_url") or ""
        if cover_url:
            _tok = self._wishlist_art_token
            loader = _WishlistArtLoader(cover_url)
            loader.signals.loaded.connect(
                lambda path, _t=_tok: self._on_wishlist_art_loaded(path)
                if _t is self._wishlist_art_token else None)
            self._pool.start(loader)

        # ── Trailer ───────────────────────────────────────────────────────────
        self._load_trailer(item.get("trailer_url"))

        # ── Notes ─────────────────────────────────────────────────────────────
        self.notes_edit.setPlainText(item.get("notes") or "")
        self.notes_edit.setReadOnly(True)
        self.notes_edit_btn.setText("✎  Edit")

    def _on_wishlist_art_loaded(self, local_path: str):
        pix = QPixmap(local_path)
        if pix.isNull():
            return
        scaled = pix.scaled(110, 165,
                            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                            Qt.TransformationMode.SmoothTransformation)
        self.cover_img.setPixmap(scaled)
        self.cover_img.setStyleSheet("""
            border: 2px solid rgba(255,255,255,0.15);
            border-radius: 8px;
        """)
        self.hero.set_image(local_path)

    # ── Trailer (shared between game mode and wishlist mode) ─────────────────

    def _load_trailer(self, trailer_url):
        trailer_url = (trailer_url or "").strip()
        self.trailer_thumb.set_trailer(trailer_url)
        self._trailer_title.setVisible(bool(trailer_url))
        if not trailer_url:
            return
        thumb_url = _youtube_thumbnail_url(trailer_url)
        self._trailer_thumb_expected = thumb_url or ""
        if thumb_url:
            loader = ImageLoader(thumb_url)
            loader.signals.loaded.connect(self._on_trailer_thumb_loaded)
            self._pool.start(loader)

    def _on_trailer_thumb_loaded(self, url: str, local_path: str):
        if url != self._trailer_thumb_expected:
            return
        pix = QPixmap(local_path)
        if not pix.isNull():
            self.trailer_thumb.set_thumbnail_pixmap(pix)

    # ── Wishlist mode actions ────────────────────────────────────────────────

    def _on_wishlist_edit_clicked(self):
        if not self._wishlist_item_id:
            return
        item = wishlist_store.get_item(self._wishlist_item_id)
        if not item:
            return
        from ui.wishlist_dialog import WishlistItemDialog
        dlg = WishlistItemDialog(item=item, parent=self)
        dlg.saved.connect(self._on_wishlist_item_saved)
        dlg.exec()

    def _on_wishlist_item_saved(self, item_id: str):
        self.load_wishlist_item(item_id)
        self.wishlist_changed.emit()

    def _on_wishlist_remove_clicked(self):
        if not self._wishlist_item_id:
            return
        item = wishlist_store.get_item(self._wishlist_item_id)
        title = item.get("title") if item else "this item"
        reply = QMessageBox.question(
            self, "Remove from Wishlist",
            f"Remove '{title}' from your wishlist?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if reply != QMessageBox.StandardButton.Yes:
            return
        wishlist_store.remove_item(self._wishlist_item_id)
        self.wishlist_changed.emit()
        # The item no longer exists to display — same "hand off to
        # whichever page opened this one" back-routing a normal back-button
        # click uses (MainWindow._on_detail_back() sends this to the
        # Wishlist page, which refreshes itself on becoming visible again).
        self.back_requested.emit()

    def _refresh_tags_display(self):
        """Rebuild the tag chip row and the add-tag autocomplete list from
        DB for whichever game is currently loaded. Called after load_game()
        and after any add/remove so the card reflects the current state
        without a full page reload."""
        while self.tags_chip_layout.count():
            item = self.tags_chip_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        try:
            current_tags = db.get_tags_for_game(self._game_id) if self._game_id else []
        except Exception:
            current_tags = []

        self.tags_empty_lbl.setVisible(not current_tags)
        self.tags_scroll.setVisible(bool(current_tags))

        for tag in current_tags:
            chip = _TagChip(tag["id"], tag["name"])
            chip.remove_clicked.connect(self._on_tag_remove_clicked)
            self.tags_chip_layout.addWidget(chip)
        self.tags_chip_layout.addStretch()

        try:
            all_names = [t["name"] for t in db.get_all_tags()]
        except Exception:
            all_names = []
        self._tag_completer.setModel(None)
        self._tag_completer.setModel(QStringListModel(all_names, self._tag_completer))

    def _on_tag_add(self):
        if not self._game_id:
            return
        name = self.tag_input.text().strip()
        if not name:
            return
        tag = db.get_or_create_tag(name, auto_import=False)
        if not tag:
            return
        db.add_tag_to_game(self._game_id, tag["id"])
        self.tag_input.clear()
        self._refresh_tags_display()
        self.install_finished.emit(self._game_id)

    def _on_tag_remove_clicked(self, tag_id: int):
        if not self._game_id:
            return
        db.remove_tag_from_game(self._game_id, tag_id)
        self._refresh_tags_display()
        self.install_finished.emit(self._game_id)

    def _on_notes_edit_save_clicked(self):
        """
        Toggle button: Edit switches the notes field to editable; Save
        persists the current text and switches back to read-only. Save
        target depends on mode: a real game writes through db.set_notes()
        (empty/whitespace-only saves as NULL — see that function); a
        wishlist item writes through wishlist_store.update_item() instead
        (empty saves as "" — wishlist_store.py's own convention, see
        update_item()'s docstring).
        """
        if self.notes_edit.isReadOnly():
            self.notes_edit.setReadOnly(False)
            self.notes_edit.setFocus()
            self.notes_edit_btn.setText("💾  Save")
        else:
            text = self.notes_edit.toPlainText()
            if self._mode == "wishlist" and self._wishlist_item_id:
                wishlist_store.update_item(self._wishlist_item_id, notes=text)
                self.wishlist_changed.emit()
            elif self._mode == "game" and self._game_id is not None:
                db.set_notes(self._game_id, text)
            self.notes_edit.setReadOnly(True)
            self.notes_edit_btn.setText("✎  Edit")

    # ── Currently Playing Indicator ──────────────────────────────────────────

    def set_currently_playing(self, game_id):
        """
        Called by MainWindow whenever the running-game state changes
        (launch, restart-reattachment, session end, or Force Quit). Only
        actually touches this page's widgets if the change is relevant to
        whichever game is currently loaded here — matches
        LibraryView.set_currently_playing()'s "only touch what's affected"
        approach.
        """
        old = self._currently_playing_game_id
        if old == game_id:
            return
        self._currently_playing_game_id = game_id
        if self._game_id is not None and self._game_id in (old, game_id):
            self._update_launch_button_state()

    def _update_launch_button_state(self):
        """
        Reflects whether the currently-loaded game is the one currently
        playing. Only meaningful when the launch button would otherwise be
        shown at all (installed + exe present) — an exe-missing or
        not-installed game has nothing to disable here.
        """
        is_playing = (self._game_id is not None
                     and self._game_id == self._currently_playing_game_id)
        self.cogwheel_btn.set_playing(is_playing)

        if not getattr(self, "_launch_btn_available", False):
            return

        if is_playing:
            self.launch_btn.setText("Playing...")
            self.launch_btn.setEnabled(False)
        else:
            self.launch_btn.setText("▶  Launch Game")
            self.launch_btn.setEnabled(True)

    def _on_hero_loaded(self, url: str, local_path: str):
        if self._mode != "game" or url != self._hero_url_expected:
            return   # stale load for a previously viewed game
        self.hero.set_image(local_path)

    def _on_cover_loaded(self, url: str, local_path: str):
        if self._mode != "game" or url != self._cover_url_expected:
            return   # stale load for a previously viewed game
        pix = QPixmap(local_path)
        if not pix.isNull():
            scaled = pix.scaled(110, 165,
                                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                Qt.TransformationMode.SmoothTransformation)
            self.cover_img.setPixmap(scaled)
            self.cover_img.setStyleSheet("""
                border: 2px solid rgba(255,255,255,0.15);
                border-radius: 8px;
            """)

    def _build_gallery_items(self, game) -> list:
        """
        Resolve the media gallery list for a real game:
          1. The stored media_gallery JSON (Steam movies + screenshots, or
             whatever trailers.build_media_gallery() produced), if any.
          2. The primary trailer_url is always forced to the FRONT as the
             first video — covers a manual pin made after the gallery was
             last built, and games that got a trailer from before a gallery
             existed for them.
          3. If there are no images at all, fall back to the game's IGDB
             screenshots (the old Screenshots grid's data source), so every
             game that used to show screenshots still does.
        """
        items: list = []
        raw = _safe_get(game, "media_gallery")
        if raw:
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(parsed, list):
                    items = [dict(i) for i in parsed
                             if isinstance(i, dict) and i.get("url")]
            except (TypeError, ValueError):
                items = []

        trailer_url = (_safe_get(game, "trailer_url") or "").strip()
        if trailer_url:
            existing = next((i for i in items if i.get("url") == trailer_url), None)
            if existing is not None:
                items.remove(existing)
                primary = existing
            else:
                primary = {
                    "type": "video", "url": trailer_url, "thumbnail_url": None,
                    "source": _safe_get(game, "trailer_source") or "manual",
                    "label": "Trailer",
                }
            primary["type"] = "video"
            items.insert(0, primary)

        if not any(i.get("type") == "image" for i in items):
            raw_shots = _safe_get(game, "screenshots")
            try:
                shots = (json.loads(raw_shots)
                         if isinstance(raw_shots, str)
                         else (raw_shots or []))
                if not isinstance(shots, list):
                    shots = []
            except (TypeError, ValueError):
                shots = []
            for n, url in enumerate(shots[:12]):
                if url:
                    items.append({
                        "type": "image", "url": url, "thumbnail_url": url,
                        "source": "igdb", "label": f"Screenshot {n + 1}",
                    })
        return items

    def _update_library_buttons(self, game):
        """Sync fav_btn and hide_btn labels/styles with current game_state."""
        try:
            is_fav    = bool(game["is_favorite"]) if "is_favorite" in game.keys() else False
            is_hidden = bool(game["is_hidden"])   if "is_hidden"   in game.keys() else False
        except (TypeError, KeyError):
            gs = db.get_game_state(self._game_id)
            is_fav    = bool(gs["is_favorite"]) if gs else False
            is_hidden = bool(gs["is_hidden"])   if gs else False

        # Favorite button
        if is_fav:
            self.fav_btn.setText("★  Unfavorite")
            self.fav_btn.setStyleSheet(f"""
                QPushButton {{
                    background: rgba(232,199,106,0.12);
                    border: 1px solid rgba(232,199,106,0.5);
                    border-radius: 8px;
                    color: {COLORS['accent']};
                    padding: 7px 10px;
                    font-weight: 600;
                }}
                QPushButton:hover {{
                    background: rgba(232,199,106,0.20);
                }}
            """)
        else:
            self.fav_btn.setText("☆  Favorite")
            self.fav_btn.setStyleSheet(f"""
                QPushButton {{
                    background: {COLORS['surface2']};
                    border: 1px solid {COLORS['border']};
                    border-radius: 8px;
                    color: {COLORS['text_muted']};
                    padding: 7px 10px;
                }}
                QPushButton:hover {{
                    color: {COLORS['accent']};
                    border-color: rgba(232,199,106,0.4);
                    background: rgba(232,199,106,0.06);
                }}
            """)

        # Hide button
        if is_hidden:
            self.hide_btn.setText("👁  Unhide")
            self.hide_btn.setStyleSheet(f"""
                QPushButton {{
                    background: rgba(107,114,128,0.15);
                    border: 1px solid rgba(107,114,128,0.4);
                    border-radius: 8px;
                    color: {COLORS['text_dim']};
                    padding: 7px 10px;
                    font-weight: 600;
                }}
                QPushButton:hover {{
                    background: rgba(107,114,128,0.25);
                    color: {COLORS['text']};
                }}
            """)
        else:
            self.hide_btn.setText("⊘  Hide")
            self.hide_btn.setStyleSheet(f"""
                QPushButton {{
                    background: {COLORS['surface2']};
                    border: 1px solid {COLORS['border']};
                    border-radius: 8px;
                    color: {COLORS['text_muted']};
                    padding: 7px 10px;
                }}
                QPushButton:hover {{
                    color: {COLORS['text_dim']};
                    background: {COLORS['surface3']};
                }}
            """)

    def _on_favorite_clicked(self):
        if not self._game_id:
            return
        gs = db.get_game_state(self._game_id)
        currently_fav = bool(gs["is_favorite"]) if gs else False
        db.set_favorite(self._game_id, not currently_fav)
        # Reload detail view and notify main window
        self.load_game(self._game_id)
        self.install_finished.emit(self._game_id)

    def _on_hide_clicked(self):
        if not self._game_id:
            return
        gs = db.get_game_state(self._game_id)
        currently_hidden = bool(gs["is_hidden"]) if gs else False
        db.set_hidden(self._game_id, not currently_hidden)
        # Reload detail view and notify main window
        self.load_game(self._game_id)
        self.install_finished.emit(self._game_id)

    def _on_completion_status_changed(self, index: int):
        """
        Persist the dropdown's new value to game_state.completion_status.
        Does NOT call load_game() here — that would rebuild (and briefly
        re-emit currentIndexChanged on) the very combo the user just
        interacted with. install_finished is reused as the general
        "something changed, refresh library" signal (same pattern as
        favorite/hide above) so sidebar counts and any Group D filter
        elsewhere pick up the change without touching this page's own
        widgets.
        """
        if not self._game_id:
            return
        status = self.completion_combo.currentData()
        if not status:
            return
        try:
            db.set_completion_status(self._game_id, status)
        except Exception as e:
            log.warning("Could not set completion status for game %d: %s",
                       self._game_id, e)
            return
        self.install_finished.emit(self._game_id)

    def _on_track_versions_clicked(self):
        """Open the VersionTrackerDialog for this game."""
        if not self._game_id:
            return
        from ui.version_tracker_dialog import VersionTrackerDialog
        dlg = VersionTrackerDialog(self._game_id, parent=self)
        dlg.versions_updated.connect(lambda gid: self.load_game(gid))
        dlg.exec()

    def _on_edit_metadata_clicked(self):
        """Open the EditMetadataDialog for this game."""
        if not self._game_id:
            return
        from ui.edit_metadata_dialog import EditMetadataDialog
        dlg = EditMetadataDialog(self._game_id, parent=self)
        dlg.saved.connect(self._on_metadata_edited)
        dlg.exec()

    def _on_metadata_edited(self, game_id: int):
        """
        Reload this page and notify the main window to refresh the library —
        same pattern _on_completion_status_changed()/_on_cogwheel_changed()
        already use for "something changed elsewhere, refresh everything."
        """
        self.load_game(game_id)
        self.install_finished.emit(game_id)

    def _on_add_to_collection_clicked(self):
        """
        Library Card's "Add to Collection…" button — same submenu content
        as GameTile's right-click "Add to Collection" (checkmarks per
        collection + inline "+ New Collection…"), popped up under the
        button since the game detail page has no context-menu surface to
        piggyback on the way the tile grid does.
        """
        if not self._game_id:
            return
        from PyQt6.QtWidgets import QMenu, QInputDialog
        menu = QMenu(self)
        member_ids = db.get_collection_ids_for_game(self._game_id)
        collections = db.get_collections_for_tile_menu()
        actions = {}
        for c in collections:
            is_member = c["id"] in member_ids
            label = ("✓  " if is_member else "") + c["name"]
            act = menu.addAction(label)
            actions[act] = (c["id"], is_member)
        menu.addSeparator()
        new_action = menu.addAction("+ New Collection…")

        chosen = menu.exec(self.add_to_collection_btn.mapToGlobal(
            self.add_to_collection_btn.rect().bottomLeft()))

        if chosen == new_action:
            name, ok = QInputDialog.getText(self, "New Collection", "Collection name:")
            name = name.strip()
            if ok and name:
                db.create_collection_with_game(name, self._game_id)
                self.collections_changed.emit()
        elif chosen in actions:
            collection_id, was_member = actions[chosen]
            if was_member:
                db.remove_game_from_collection(collection_id, self._game_id)
            else:
                db.add_game_to_collection(collection_id, self._game_id)
            self.collections_changed.emit()

    def _on_install_clicked(self):
        if not self._game_id:
            return
        game = db.get_game(self._game_id)
        if not game:
            return
        # Convert sqlite3.Row to plain dict so InstallDialog can use .get()
        game_dict = dict(game)
        dlg = InstallDialog(game_dict, parent=self)
        dlg.install_finished.connect(self._on_install_finished)
        dlg.exec()

    def _on_locate_clicked(self):
        """Open the Locate Game dialog (adopt an existing install)."""
        if not self._game_id:
            return
        game = db.get_game(self._game_id)
        if not game:
            return
        from ui.locate_game_dialog import LocateGameDialog
        dlg = LocateGameDialog(dict(game), parent=self)
        if dlg.exec():
            self.load_game(self._game_id)
            self.install_finished.emit(self._game_id)

    def _maybe_snapshot_before_launch(self, game, wine_bin: str, wine_prefix: str):
        """
        Save Backup pre-launch hook, covering all three flows:

        Flow 1 (not yet linked — save_source_path unset, or the canonical
        backup itself is gone): snapshot the Wine prefix's drive_c before
        launch so the post-play diff (main_window._maybe_run_save_backup_flow)
        has a baseline to compare against. Persisted to disk immediately
        via save_backup.save_pending_snapshot() so it survives the app
        being closed before the user responds to the post-play prompt.

        Flow 2 (already linked): a cheap check via
        save_backup.diagnose_source_path() for whether the symlink is
        still intact.

        Flow 3 (already linked, but the symlink isn't intact — most
        commonly because the Wine prefix was deleted and recreated):
        "missing" and "plain_folder" are repaired automatically via
        save_backup.repair_link() with no confirmation needed — see that
        function's docstring for why that's safe. "wrong_symlink" (an
        existing symlink pointing somewhere else entirely) is surprising
        enough that the user is asked before anything is touched, per
        spec. If a repair is attempted and fails, this falls back to the
        same warning Flow 2 always showed for a broken link.

        Never blocks or fails the launch — any error here is logged and
        swallowed, since a missed backup opportunity for one session is
        far better than a broken launch.
        """
        if db.get_setting("save_backup_enabled", "false") != "true":
            return
        if not wine_prefix:
            return
        try:
            # Fresh install / recreated prefix: offer to restore the backup
            # BEFORE the game runs, so it starts from the user's progress.
            self._offer_save_restore(game, wine_bin, wine_prefix)

            existing = db.get_save_paths(self._game_id)
            source_path = existing.get("save_source_path")
            save_path   = existing.get("save_path")
            diag = save_backup.diagnose_source_path(source_path, save_path)
            log.debug("[SAVE BACKUP] Pre-launch check for game_id=%d (%s): diag=%s",
                      self._game_id, game["folder_name"], diag)

            if diag == "ok":
                # Back-fill the restore manifest for saves linked before it
                # existed, so a future fresh install can restore automatically.
                try:
                    import save_restore
                    _root = Path(db.get_setting(
                        "save_backup_root", str(Path.home() / "Documents" / "Game Saves")))
                    if save_restore.read_manifest(_root, game["folder_name"]) is None:
                        save_restore.write_manifest(_root, game["folder_name"], source_path)
                except Exception as _e:
                    log.debug("[SAVE RESTORE] manifest backfill skipped: %s", _e)
                return  # linked and intact — nothing to do

            if diag == "canonical_missing":
                # Backup itself is gone (e.g. user deleted it directly).
                # Nothing to restore from — per spec, launch normally.
                # Not re-triggering Flow 1: this game WAS linked, and
                # silently starting a brand-new detection pass here could
                # surprise the user with a second, unrelated backup link
                # for the same game. Left as-is (visible as "Link Broken"
                # on the detail page) until the user decides what to do.
                log.warning(
                    "[SAVE BACKUP] Canonical save path missing for game_id=%d "
                    "(%s, was: %s) — launching without managing saves this "
                    "session", self._game_id, game["folder_name"], save_path)
                return

            if diag in ("missing", "plain_folder"):
                ok = save_backup.repair_link(source_path, save_path)
                if ok:
                    log.info(
                        "[SAVE BACKUP] Flow 3: auto-repaired link for "
                        "game_id=%d (%s)", self._game_id, game["folder_name"])
                else:
                    self._warn_broken_save_link(game, source_path, save_path)
                return

            if diag == "wrong_symlink":
                current_target = save_backup.current_symlink_target(source_path) or "(unreadable)"
                msg = QMessageBox(self)
                msg.setWindowTitle("Save Backup — Unexpected Save Link")
                msg.setText(
                    "The save location for this game currently points "
                    "somewhere unexpected.")
                msg.setInformativeText(
                    f"Currently points to:\n{current_target}\n\n"
                    f"Expected to point to your backed-up save at:\n{save_path}\n\n"
                    "This can happen if something else changed it since "
                    "VaultPlay last linked it. Relink it to your backed-up "
                    "save, or leave it as-is?"
                )
                relink_btn = msg.addButton("Relink to Backup", QMessageBox.ButtonRole.AcceptRole)
                leave_btn  = msg.addButton("Leave As Is", QMessageBox.ButtonRole.RejectRole)
                msg.setDefaultButton(leave_btn)
                msg.exec()
                if msg.clickedButton() == relink_btn:
                    ok = save_backup.repair_link(source_path, save_path)
                    if ok:
                        log.info(
                            "[SAVE BACKUP] Flow 3: relinked game_id=%d (%s) "
                            "after user confirmed overwrite of unexpected "
                            "symlink", self._game_id, game["folder_name"])
                    else:
                        self._warn_broken_save_link(game, source_path, save_path)
                else:
                    log.info(
                        "[SAVE BACKUP] User left unexpected symlink as-is "
                        "for game_id=%d (%s)", self._game_id, game["folder_name"])
                return

            # diag == "unset" — Flow 1 path, not linked yet
            import installer as install_mod
            actual_prefix = install_mod._resolve_actual_prefix(Path(wine_prefix), wine_bin)
            snapshot = save_backup.snapshot_prefix(actual_prefix)
            save_backup.save_pending_snapshot(game["folder_name"], snapshot, actual_prefix)
            log.debug("[SAVE BACKUP] Pre-launch snapshot taken for %s (%d files)",
                      game["folder_name"], len(snapshot))
        except Exception as e:
            log.warning("[SAVE BACKUP] Pre-launch check/snapshot failed: %s", e)

    def _offer_save_restore(self, game, wine_bin: str, wine_prefix: str,
                            manual: bool = False) -> bool:
        """
        Look for a backup (main save and/or achievements/stats) that exists in
        the Save Backup folder but isn't linked into this game's prefix, and
        offer to restore it. See save_restore.py for why this was missing.
        manual=True (Cogwheel → Restore from Backup) ignores "don't ask again"
        and reports when there is nothing to restore. Returns True if anything
        was restored. Never raises.
        """
        try:
            import save_restore
            import installer as install_mod
            gid = self._game_id
            if not manual and db.get_setting(f"save_restore_declined_{gid}", "") == "true":
                return False

            save_root = Path(db.get_setting(
                "save_backup_root", str(Path.home() / "Documents" / "Game Saves")))
            actual = install_mod._resolve_actual_prefix(Path(wine_prefix), wine_bin)
            drive_c = actual / "drive_c"
            if not drive_c.exists():
                return False
            proton = Path(wine_bin or "").name == "proton"
            linked = [e.get("canonical_path") for e in db.get_save_extra_paths(gid)
                      if e.get("canonical_path")]
            info = save_restore.find_restorable(
                save_root, game["folder_name"], drive_c, proton, linked)

            # The main save is only restorable when it isn't linked yet.
            paths = db.get_save_paths(gid)
            if save_backup.diagnose_source_path(
                    paths.get("save_source_path"), paths.get("save_path")) == "ok":
                info["main"] = None

            if not info["main"] and not info["extras"]:
                if manual:
                    msg = "Nothing to restore — your backup is already linked."
                    if info.get("legacy_main"):
                        msg = ("A backup exists, but it was made before VaultPlay recorded "
                               "where it belongs, so it can't be placed automatically.\n\n"
                               "Use Save Files → Re-Link Save Location… and pick this game's "
                               "save folder — choose “Use my existing backup” when asked.")
                    QMessageBox.information(self, "Restore from Backup", msg)
                return False

            from ui.save_restore_dialog import SaveRestoreDialog
            title = game["title"] or game["display_name"] or game["folder_name"]
            dlg = SaveRestoreDialog(title, info, parent=self)
            if dlg.exec() != SaveRestoreDialog.DialogCode.Accepted:
                if dlg.never:
                    db.set_setting(f"save_restore_declined_{gid}", "true")
                return False

            res = save_restore.restore_all(
                gid, game["folder_name"], save_root, info,
                dlg.want_main(), dlg.want_extras())
            if res["errors"]:
                QMessageBox.warning(self, "Restore from Backup",
                                    "Some items could not be restored:\n\n" + "\n".join(res["errors"]))
            did = bool(res["main"] or res["extras"])
            if did:
                log.info("[SAVE RESTORE] game_id=%d restored main=%s extras=%s",
                         gid, res["main"], res["extras"])
            return did
        except Exception as e:
            log.warning("[SAVE RESTORE] restore offer failed: %s", e)
            return False

    def _warn_broken_save_link(self, game, source_path, save_path):
        """Shared fallback warning: shown when a broken link couldn't be
        auto-repaired (Flow 3 repair_link() failed) or, previously, for
        every broken-link case before Flow 3 existed."""
        log.warning(
            "[SAVE BACKUP] Link broken for game_id=%d (%s): expected %s "
            "→ %s — game will write a new, unlinked save this session",
            self._game_id, game["folder_name"], source_path, save_path)
        QMessageBox.warning(
            self, "Save Backup — Link Broken",
            "The linked save folder for this game is missing or has "
            "changed, and VaultPlay couldn't automatically fix it:\n\n"
            f"{source_path}\n\n"
            "The game will still launch, but it will write a NEW save at "
            "that location instead of using your backed-up save at:\n\n"
            f"{save_path}\n\n"
            "Check the app log for details, or restore the save manually."
        )

    def _spawn_process(self, cmd, *, shell: bool, cwd, env=None,
                       debug: bool, folder_name: str):
        """
        Single choke point for every subprocess.Popen call in the launch flow below.
        debug=True (Cogwheel → "Launch with Debug Log") pipes stdout/stderr into this
        game's timestamped debug log file via debug_launch.py instead of the terminal;
        debug=False (the normal Play button) behaves exactly as before — inherits the
        terminal, no capturing, no logging. See debug_launch.py for the log format,
        5MB cap, and why a debug launch is still returned as a normal Popen handle
        (so PlaytimeWatcher/game_launched wiring downstream doesn't need to care).
        """
        if debug:
            import debug_launch
            return debug_launch.launch_with_debug_log(
                cmd, cwd=cwd, env=env, shell=shell, folder_name=folder_name)
        if shell:
            return subprocess.Popen(cmd, shell=True, cwd=cwd, env=env)
        return subprocess.Popen(cmd, cwd=cwd, env=env)

    def _launch_game(self, debug: bool = False):
        """
        Launch the installed game via Wine/Proton with its saved prefix.

        debug=True routes the process's stdout/stderr through debug_launch.py's
        per-game log file instead of the terminal — triggered only from the
        Cogwheel's "Launch with Debug Log" action (see _on_debug_launch_requested
        below). The normal Play button always calls this with debug=False and never
        logs, per the Per-Game Debug Launch Logs spec.
        """
        if not self._game_id:
            return
        game = db.get_game(self._game_id)
        if not game:
            return

        folder_name  = game["folder_name"]
        exe_path     = (game["exe_path"]     or "").strip()
        wine_prefix  = (game["wine_prefix"]  or "").strip()
        desktop_path = (game["desktop_path"] or "").strip()

        # Preferred path: use stored launch_cmd and launch_cwd directly.
        # These are set by _extract_launch_info at install time and never
        # depend on the .desktop file being present on disk.
        launch_cmd = (game["launch_cmd"] or "").strip() if "launch_cmd" in game.keys() else ""
        launch_cwd = (game["launch_cwd"] or "").strip() if "launch_cwd" in game.keys() else ""

        if launch_cmd:
            try:
                cwd      = launch_cwd if (launch_cwd and Path(launch_cwd).exists()) else None
                wine_bin = _parse_wine_bin(launch_cmd)
                self._maybe_snapshot_before_launch(game, wine_bin, wine_prefix)
                proc     = self._spawn_process(
                    launch_cmd, shell=True, cwd=cwd,
                    debug=debug, folder_name=folder_name)
                log.info("Launched via launch_cmd: %s (cwd=%s wine_bin=%s debug=%s)",
                         launch_cmd, cwd, wine_bin, debug)
                self.game_launched.emit(
                    self._game_id, proc, wine_bin, wine_prefix)
                return
            except Exception as e:
                log.warning("launch_cmd failed: %s — falling back to .desktop", e)

        # Legacy fallback: parse .desktop file (for games installed before
        # launch_cmd was added, or where launch_cmd is blank).
        if desktop_path and Path(desktop_path).exists():
            try:
                exec_line = None
                cwd_line  = None
                for line in Path(desktop_path).read_text().splitlines():
                    if line.startswith("Exec="):
                        exec_line = line[5:].strip()
                    elif line.startswith("Path="):
                        cwd_line = line[5:].strip()
                if exec_line:
                    cwd      = cwd_line if (cwd_line and Path(cwd_line).exists()) else None
                    wine_bin = _parse_wine_bin(exec_line)
                    self._maybe_snapshot_before_launch(game, wine_bin, wine_prefix)
                    proc     = self._spawn_process(
                        exec_line, shell=True, cwd=cwd,
                        debug=debug, folder_name=folder_name)
                    log.info("Launched via .desktop Exec: %s (cwd=%s wine_bin=%s debug=%s)",
                             exec_line, cwd, wine_bin, debug)
                    self.game_launched.emit(
                        self._game_id, proc, wine_bin, wine_prefix)
                    return
            except Exception as e:
                log.warning(".desktop exec failed: %s — falling back to exe", e)

        # Fallback: reconstruct the launch command from stored data
        if not exe_path or not Path(exe_path).exists():
            QMessageBox.warning(
                self, "Launch Error",
                f"Game executable not found:\n{exe_path or '(not set)'}\n\n"
                "Try reinstalling the game, or open the game folder and "
                "run the .exe manually."
            )
            return

        try:
            import installer as install_mod
            stored_version = db.get_setting("default_proton_version", "")
            wine_bin = install_mod._resolve_wine_bin(
                protondb_mod.get_version_path(stored_version)
            ) if stored_version else "wine"
            prefix_path = Path(wine_prefix) if wine_prefix else None
            if prefix_path:
                env = install_mod._build_wine_env(wine_bin, prefix_path)
            else:
                env = {**os.environ}
            if Path(wine_bin).name == "proton":
                cmd = [wine_bin, "run", exe_path]
            else:
                cmd = [wine_bin, exe_path]
            self._maybe_snapshot_before_launch(game, wine_bin, wine_prefix)
            proc = self._spawn_process(
                cmd, shell=False, cwd=None, env=env,
                debug=debug, folder_name=folder_name)
            log.info("Launched: %s %s (WINEPREFIX=%s debug=%s)",
                     wine_bin, exe_path, wine_prefix, debug)
            self.game_launched.emit(self._game_id, proc, wine_bin, wine_prefix)
        except Exception as e:
            QMessageBox.warning(self, "Launch Error", f"Could not launch game:\n{e}")

    def _on_launch_clicked(self):
        self._launch_game(debug=False)

    def _on_debug_launch_requested(self, game_id: int):
        """
        Cogwheel → "Launch with Debug Log". The Cogwheel button is always scoped to
        whichever game is currently loaded on this page, so game_id should always
        equal self._game_id — the check is just a safety net against a stale signal.
        """
        if game_id != self._game_id:
            return
        self._launch_game(debug=True)

    def _on_open_folder(self):
        """Open the game's install folder in the file manager."""
        if not self._game_id:
            return
        game = db.get_game(self._game_id)
        if not game:
            return
        install_path = (game["install_path"] or "").strip()
        if not install_path or not Path(install_path).exists():
            QMessageBox.information(
                self, "Open Folder",
                "Install folder not found. The game may have been moved or deleted."
            )
            return
        try:
            subprocess.Popen(["xdg-open", install_path])
        except Exception as e:
            QMessageBox.warning(self, "Open Folder",
                                f"Could not open folder:\n{e}")

    def _on_uninstall_clicked(self):
        """Single-window uninstall: ui/uninstall_dialog.py asks what to remove
        (record/shortcut, game files, Wine prefix) with checkboxes."""
        if not self._game_id:
            return
        game = db.get_game(self._game_id)
        if not game:
            return

        title        = game["title"] or game["display_name"] or game["folder_name"]
        install_path = (game["install_path"] or "").strip()
        wine_prefix  = (game["wine_prefix"]  or "").strip()
        desktop_path = (game["desktop_path"] or "").strip()
        script_path  = (game["script_path"]  or "").strip()

        from PyQt6.QtWidgets import QDialog
        from ui.uninstall_dialog import UninstallDialog
        dlg = UninstallDialog(title, install_path, wine_prefix, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        errors = []

        if dlg.remove_record():
            for p in [desktop_path, script_path]:
                if p and Path(p).exists():
                    try:
                        Path(p).unlink()
                        log.info("Deleted: %s", p)
                    except Exception as e:
                        log.warning("Could not delete %s: %s", p, e)
            try:
                db.remove_install(self._game_id)
                log.info("Uninstalled game %d (%s) — DB record removed",
                         self._game_id, title)
            except Exception as e:
                log.error("remove_install failed: %s", e)
                errors.append(f"Could not remove the install record:\n{e}")

        if dlg.delete_files() and install_path and Path(install_path).exists():
            try:
                shutil.rmtree(install_path)
                log.info("Deleted game files: %s", install_path)
            except Exception as e:
                errors.append(f"Could not delete game files:\n{e}")

        if dlg.delete_prefix() and wine_prefix and Path(wine_prefix).exists():
            try:
                shutil.rmtree(wine_prefix)
                log.info("Deleted Wine prefix: %s", wine_prefix)
            except Exception as e:
                errors.append(f"Could not delete Wine prefix:\n{e}")

        if errors:
            QMessageBox.warning(self, "Uninstall", "\n\n".join(errors))

        self.load_game(self._game_id)
        self.install_finished.emit(self._game_id)

    def _on_install_finished(self, game_id: int):
        self.load_game(game_id)
        self.install_finished.emit(game_id)

    def _on_cogwheel_changed(self, game_id: int):
        """Any Cogwheel Menu action (Proton change, redists, path repair,
        save relink) can change info shown on this page — just reload."""
        self.load_game(game_id)
        self.install_finished.emit(game_id)

    # ── Controller Support (gamepad navigation) ─────────────────────────────
    # Real 2D layout, not a flat list — Favorite/Hide and Launch/Cogwheel
    # actually sit side by side on the page, so each "row" below is a list
    # of 1+ widgets laid out left-to-right. Up/down move between rows
    # (clamping to a shorter row's last column rather than losing focus);
    # left/right move within the current row only. B is handled by
    # MainWindow._ctrl_back() directly (it calls back_requested itself),
    # so the back button is deliberately not included here.
    #
    # completion_combo is rebuilt fresh by load_game() on every call (see
    # that method) rather than created once in __init__ like every other
    # widget referenced below — it doesn't exist at all until a game has
    # actually been loaded, so every read of it here is guarded.

    def _ctrl_rows(self) -> list:
        def ok(w):
            return w.isVisibleTo(self) and w.isEnabled()

        rows = []
        if self._mode == "game":
            if self.media_gallery.isVisibleTo(self) and self.media_gallery.has_items():
                rows.append([self.media_gallery])
            if hasattr(self, "completion_combo"):
                rows.append([self.completion_combo])
            row = [w for w in (self.fav_btn, self.hide_btn) if ok(w)]
            if row:
                rows.append(row)
            for w in (self.track_versions_btn, self.edit_metadata_btn,
                      self.add_to_collection_btn):
                if ok(w):
                    rows.append([w])
            if ok(self.install_btn):
                rows.append([self.install_btn])
            if ok(self.locate_btn):
                rows.append([self.locate_btn])
            row = [w for w in (self.launch_btn, self.cogwheel_btn) if ok(w)]
            if row:
                rows.append(row)
            for w in (self.open_folder_btn, self.uninstall_btn):
                if ok(w):
                    rows.append([w])
        else:
            for w in (self.wishlist_edit_btn, self.wishlist_remove_btn):
                if ok(w):
                    rows.append([w])
        rows.append([self.notes_edit_btn])
        return rows

    @staticmethod
    def _ctrl_find_position(rows, widget):
        for r, row in enumerate(rows):
            if widget in row:
                return r, row.index(widget)
        return None

    def _ctrl_style_widget(self, widget, focused: bool):
        if isinstance(widget, MediaGalleryWidget):
            widget.set_controller_focused(focused)
            return
        key = id(widget)
        orig = self._ctrl_orig_style.get(key)
        if orig is None:
            orig = widget.styleSheet()
            self._ctrl_orig_style[key] = orig
        if focused:
            selector = "QComboBox" if isinstance(widget, QComboBox) else "QPushButton"
            widget.setStyleSheet(
                orig + f"\n{selector} {{ border: 2px solid {COLORS['accent']}; }}")
        else:
            widget.setStyleSheet(orig)

    def _ctrl_apply_focus(self):
        for row in self._ctrl_rows():
            for w in row:
                self._ctrl_style_widget(
                    w, self._ctrl_focus_visible and w is self._ctrl_focused_widget)
        if self._ctrl_focus_visible and self._ctrl_focused_widget is not None:
            self.scroll.ensureWidgetVisible(self._ctrl_focused_widget, 20, 60)

    def controller_has_targets(self) -> bool:
        return bool(self._ctrl_rows())

    def controller_focus_default(self) -> bool:
        rows = self._ctrl_rows()
        if not rows:
            return False
        self._ctrl_focus_visible = True
        if self._ctrl_find_position(rows, self._ctrl_focused_widget) is None:
            self._ctrl_focused_widget = rows[0][0]
        self._ctrl_apply_focus()
        return True

    def controller_move(self, direction: str) -> bool:
        rows = self._ctrl_rows()
        if not rows:
            return False
        if (self._ctrl_focused_widget is self.media_gallery
                and direction in ("left", "right")):
            return self.media_gallery.controller_step(-1 if direction == "left" else 1)
        pos = self._ctrl_find_position(rows, self._ctrl_focused_widget)
        if pos is None:
            self._ctrl_focused_widget = rows[0][0]
            self._ctrl_apply_focus()
            return True
        r, c = pos
        if direction == "left":
            if c <= 0:
                return False
            c -= 1
        elif direction == "right":
            if c >= len(rows[r]) - 1:
                return False
            c += 1
        elif direction in ("up", "down"):
            nr = r + (-1 if direction == "up" else 1)
            if not (0 <= nr < len(rows)):
                return False
            r = nr
            c = min(c, len(rows[r]) - 1)   # shorter row — clamp, don't lose focus
        else:
            return False
        self._ctrl_focused_widget = rows[r][c]
        self._ctrl_apply_focus()
        return True

    def controller_activate(self):
        w = self._ctrl_focused_widget
        if w is None:
            return
        if w is self.media_gallery:
            w.controller_activate()
            return
        if isinstance(w, QComboBox):
            w.showPopup()
        elif isinstance(w, QPushButton) and w.isEnabled():
            w.click()

    def set_controller_focus_visible(self, visible: bool):
        self._ctrl_focus_visible = visible
        self._ctrl_apply_focus()

    def controller_scroll(self, delta: int):
        """Right-stick scroll (see controller.py) — applies to this page's
        own QScrollArea regardless of whether the D-pad focus ring is
        currently showing on it."""
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.value() + delta)



def _safe_get(row, key, default=None):
    """Safely get a value from a sqlite3.Row, returning default if key missing."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def _format_version_dict(v: dict) -> str:
    """
    Return whichever of dotted/plain/date is populated in a version dict
    (db.get_nas_version() / db.get_installed_version()'s return shape), or
    "Unknown" if none are — per decision: show whichever field is set, hide
    the fact that the others are null, never show a blank row.
    """
    if v.get("dotted"):
        return v["dotted"]
    if v.get("plain"):
        return v["plain"]
    if v.get("date"):
        return v["date"]
    return "Unknown"


def _parse_wine_bin(launch_cmd: str) -> str:
    """
    Extract the Wine or Proton binary name from a launch command string.

    Thin wrapper around installer.parse_wine_bin_from_cmd() — that copy is
    the single source of truth (it also has to stay in sync with
    launch_options.py's list of command wrappers, e.g. "gamemoderun", that
    can precede the real env/wine/proton invocation). Kept as a
    module-level function here rather than inlined at call sites purely so
    existing call sites in this file don't need to change.

    This value is passed to PlaytimeWatcher so it can choose the correct
    wait strategy (proc.wait() for Proton, wineserver --wait for Wine), and
    to _maybe_snapshot_before_launch() so Save Backup snapshots/diffs the
    correct directory (pfx/drive_c for Proton vs. drive_c for plain Wine).
    Never hardcode "wine" here — the binary changes based on what was
    configured at install time.
    """
    import installer as install_mod
    return install_mod.parse_wine_bin_from_cmd(launch_cmd)


def _file_type_label(file_type: str) -> str:
    return {
        "rar":   "RAR Archive",
        "7zip":  "7-Zip Archive",
        "loose": "Loose Files",
    }.get(file_type or "", "Unknown")


def _install_tag_label(tag: str) -> tuple[str, str, str]:
    """Returns (display_label, color, bg_color) for an install tag."""
    return {
        "installer": ("Installer",  "#5b8dee", "rgba(91,141,238,0.10)"),
        "portable":  ("Portable",   "#e8c76a", "rgba(232,199,106,0.10)"),
        "iso":       ("ISO",        "#b55be8", "rgba(181,91,232,0.10)"),
    }.get(tag or "", ("Unknown", "#6b7280", "rgba(107,114,128,0.10)"))


class _InfoRow(QWidget):
    def __init__(self, label: str, value: str, mono: bool = False,
                warn: bool = False, parent=None):
        super().__init__(parent)
        # Single outer layout to avoid double-parenting issues
        outer = QVBoxLayout(self)
        outer.setSpacing(0)
        outer.setContentsMargins(0, 0, 0, 0)

        content = QWidget()
        content.setStyleSheet("background: transparent;")
        row = QHBoxLayout(content)
        row.setContentsMargins(0, 6, 0, 6)
        row.setSpacing(8)

        lbl = QLabel(label)
        lbl.setFont(QFont("DM Sans", 11))
        lbl.setStyleSheet(f"color: {COLORS['text_muted']}; background: transparent;")
        lbl.setFixedWidth(95)

        val = QLabel(value)
        val.setFont(QFont("DM Mono" if mono else "DM Sans", 10 if mono else 11))
        if warn:
            val.setStyleSheet(
                f"color: {COLORS['danger']}; font-weight: 500; background: transparent;")
        else:
            val.setStyleSheet(
                f"color: {COLORS['accent2']}; background: transparent;" if mono
                else f"color: {COLORS['text']}; font-weight: 500; background: transparent;"
            )
        val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        val.setWordWrap(True)

        row.addWidget(lbl)
        row.addWidget(val, 1)
        outer.addWidget(content)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background: {COLORS['border']}; border: none;")
        outer.addWidget(sep)


class _VersionRow(QWidget):
    """
    Info card row for "Latest (v)" and "Latest (build)" version data.
    Shows the version value (or "Unknown") with a small staleness note.
    When a source URL is known, the value is rendered as a clickable link.
    """
    def __init__(self, label: str, value: str,
                 source_url: str = None, checked_at: str = None,
                 parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setSpacing(0)
        outer.setContentsMargins(0, 0, 0, 0)

        content = QWidget()
        content.setStyleSheet("background: transparent;")
        row = QHBoxLayout(content)
        row.setContentsMargins(0, 6, 0, 4)
        row.setSpacing(8)

        lbl = QLabel(label)
        lbl.setFont(QFont("DM Sans", 11))
        lbl.setStyleSheet(f"color: {COLORS['text_muted']}; background: transparent;")
        lbl.setFixedWidth(95)
        row.addWidget(lbl)

        val_col = QVBoxLayout()
        val_col.setSpacing(1)
        val_col.setContentsMargins(0, 0, 0, 0)

        is_unknown = (value == "Unknown" or not value)
        val_color  = COLORS["text_muted"] if is_unknown else COLORS["text"]

        if source_url and not is_unknown:
            # Clickable link
            val_lbl = QLabel(
                f'<a href="{source_url}" style="color:{COLORS["accent2"]};'
                f' text-decoration:underline;">{value}</a>'
            )
            val_lbl.setOpenExternalLinks(True)
        else:
            val_lbl = QLabel(value)
            val_lbl.setStyleSheet(
                f"color: {val_color}; font-weight: 500; background: transparent;")

        val_lbl.setFont(QFont("DM Mono", 10))
        val_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        val_col.addWidget(val_lbl)

        # Staleness note
        if checked_at and not is_unknown:
            import datetime
            try:
                dt = datetime.datetime.fromisoformat(
                    str(checked_at).replace("T", " ")[:19])
                delta = (datetime.datetime.utcnow() - dt).days
                if delta == 0:
                    age = "checked today"
                elif delta == 1:
                    age = "checked 1 day ago"
                else:
                    age = f"checked {delta} days ago"
                age_lbl = QLabel(age)
                age_lbl.setFont(QFont("DM Mono", 8))
                age_lbl.setStyleSheet(
                    f"color: {COLORS['text_muted']}; background: transparent;")
                age_lbl.setAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                val_col.addWidget(age_lbl)
            except (ValueError, TypeError):
                pass

        row.addLayout(val_col, 1)
        outer.addWidget(content)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background: {COLORS['border']}; border: none;")
        outer.addWidget(sep)
