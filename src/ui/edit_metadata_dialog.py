"""
ui/edit_metadata_dialog.py — Edit Metadata dialog for VaultPlay

Spec: Notion → Features → Fully Planned → Edit Metadata.

Lets the user manually edit any metadata field for a game: title, sort
name, description, developer/publisher/IP holder, release date, genres,
cover/hero/logo art (URL or local upload), screenshots (add/remove/
reorder), and Steam App ID (typed manually, or via a "Pick from Steam"
search picker).

Entry points: GameTile's right-click context menu ("Edit Metadata…") and
the Game Detail page's Library card — see ui/library_view.py and
ui/game_detail.py.

Save behavior
-------------
Steam App ID UNCHANGED from what the dialog opened with: every field on
the form is written as-is via db.upsert_metadata() + db.set_sort_name().
sgdb_id/igdb_id are preserved from the original row (not exposed as
editable fields here, and db.upsert_metadata() has no COALESCE guard for
them the way it does for the art URLs — passing None would null them out).

Steam App ID CHANGED to a new, non-blank value: per spec, this re-fetches
everything rather than just saving the form — "All existing metadata is
overwritten — no merging with old data." After a Yes/Cancel confirmation:
  1. Sort Name is saved immediately (it's a game_state field, independent
     of game identity/metadata, unaffected by which Steam game this is).
  2. A background worker calls metadata.fetch_metadata_for_game() with
     force_steam_app_id set — this re-runs the normal title-based SGDB/
     IGDB search to refresh title/art/description/genres, but pins
     steam_app_id to exactly what the user chose (never silently
     replaced by title-search auto-discovery — see metadata.py).
  3. Then protondb.fetch_and_store() and redists.refresh_single_game()
     re-fetch compatibility/redistributable data for the new ID.
The rest of the form's manually-typed edits (description, art, etc.) are
intentionally discarded in this path — the whole point of changing the
Steam ID is "this is now a different/corrected game," so a fresh fetch
should win, not a merge with what was just typed for the old identity.

Clearing the Steam App ID to blank (when it previously had a value) is
NOT treated as a "change" for the confirm+refetch flow — there's no new
ID to fetch metadata for. It just saves as NULL like any other field.

Manual Cover Art Picker: each art field's "Browse SteamGridDB…" button opens
ui/cover_art_picker_dialog.py — a two-panel browse dialog (thumbnail grid +
preview) that auto-searches via sgdb_id, then steam_app_id, then the current
Title field, with a manual search box always available. Selecting an image
sets that field to URL mode with the chosen image's URL. Requires an SGDB
API key configured in Settings → API Keys; the button shows an inline
message and does nothing further if one isn't set.
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

import json
import logging
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Optional

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QTextEdit, QWidget, QFrame, QScrollArea, QFileDialog, QMessageBox,
    QButtonGroup, QRadioButton, QListWidget, QListWidgetItem,
    QAbstractItemView, QInputDialog
)
from PyQt6.QtCore import (
    Qt, pyqtSignal, QThread, QSize, QRunnable, QThreadPool, pyqtSlot, QObject
)
from PyQt6.QtGui import QFont, QPixmap, QIcon

import db
import metadata as meta_mod
import protondb as protondb_mod
from ui.style import COLORS, accent_button_style

log = logging.getLogger(__name__)

_LABEL_BASE = "background: transparent; border: none;"


def _looks_local(path_or_url: str) -> bool:
    """True if this string is an existing local absolute file path rather
    than a remote URL — used throughout to decide sync-local-load vs.
    async-network-load, and to default an art field's mode toggle."""
    if not path_or_url:
        return False
    try:
        p = Path(path_or_url)
        return p.is_absolute() and p.is_file()
    except OSError:
        return False


def _copy_into_cache(src_path: str, prefix: str) -> str:
    """Copy a user-picked local file into the VaultPlay cache dir under a
    unique name, returning the new absolute path — this becomes the
    stored "url" value for manually-uploaded art/screenshots."""
    cache_dir = Path(db.get_setting("cache_path", str(db.CONFIG_DIR / "cache")))
    cache_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(src_path).suffix or ".png"
    dest = cache_dir / f"{prefix}_{uuid.uuid4().hex[:12]}{ext}"
    shutil.copy2(src_path, dest)
    return str(dest)


# ── Shared async thumbnail loader ─────────────────────────────────────────────

class _ThumbSignals(QObject):
    loaded = pyqtSignal(str)   # local_path


class _ThumbLoader(QRunnable):
    """Background download+cache of a remote thumbnail URL, mirroring the
    ImageLoader pattern already used in library_view.py/game_detail.py."""

    def __init__(self, url: str):
        super().__init__()
        self.url = url
        self.signals = _ThumbSignals()

    @pyqtSlot()
    def run(self):
        try:
            path = meta_mod.download_art(self.url)
            if path:
                self.signals.loaded.emit(path)
        except Exception as e:
            log.debug("_ThumbLoader failed for %s: %s", self.url, e)


# ── Section box (mirrors cogwheel_menu.py / install_dialog.py's SectionBox) ──

class SectionBox(QFrame):
    def __init__(self, title: str = "", parent=None):
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
                f"color: {COLORS['text_muted']}; letter-spacing: 2px; {_LABEL_BASE}")
            self._inner.addWidget(lbl)

    def add(self, widget):
        self._inner.addWidget(widget)
        return widget

    def add_separator(self):
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background: {COLORS['border']}; border: none;")
        self._inner.addWidget(sep)


def _field_row(label: str, widget: QWidget) -> QWidget:
    """Labeled row: fixed-width muted label + the field, used for every
    plain text field in the Basic Info section."""
    row = QWidget()
    row.setStyleSheet("background: transparent;")
    l = QHBoxLayout(row)
    l.setContentsMargins(0, 0, 0, 0)
    l.setSpacing(10)
    lbl = QLabel(label)
    lbl.setFont(QFont("DM Sans", 10))
    lbl.setFixedWidth(100)
    lbl.setStyleSheet(f"color: {COLORS['text_muted']}; {_LABEL_BASE}")
    l.addWidget(lbl)
    l.addWidget(widget, 1)
    return row


# ── Art field (Cover / Hero / Logo) ───────────────────────────────────────────

class _ArtField(QWidget):
    """One art slot: thumbnail preview + URL/Upload mode toggle + a
    disabled 'Browse SteamGridDB…' placeholder for the not-yet-built
    Manual Cover Art Picker feature."""

    def __init__(self, kind: str, label: str, initial_url: str,
                 context_provider=None, sgdb_key: str = "", parent=None):
        super().__init__(parent)
        self._kind = kind
        self._initial_url = initial_url or ""
        self._new_upload_path: Optional[str] = None
        # Zero-arg callable returning {"sgdb_id", "steam_app_id", "title"} —
        # read lazily at browse-time (not captured once at construction) so
        # the picker sees whatever the Steam App ID / Title fields currently
        # hold, including edits the user hasn't saved yet.
        self._context_provider = context_provider
        self._sgdb_key = sgdb_key

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 6, 0, 6)
        outer.setSpacing(6)

        header = QLabel(label.upper())
        header.setFont(QFont("DM Mono", 8))
        header.setStyleSheet(
            f"color: {COLORS['text_muted']}; letter-spacing: 1.5px; {_LABEL_BASE}")
        outer.addWidget(header)

        row = QHBoxLayout()
        row.setSpacing(10)

        self.thumb = QLabel()
        self.thumb.setFixedSize(120, 68)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setStyleSheet(
            f"background: {COLORS['surface3']}; border: 1px solid {COLORS['border']};"
            " border-radius: 6px;")
        row.addWidget(self.thumb)

        controls = QVBoxLayout()
        controls.setSpacing(4)

        mode_row = QHBoxLayout()
        mode_row.setContentsMargins(0, 0, 0, 0)
        mode_row.setSpacing(12)
        self._group = QButtonGroup(self)
        self.url_radio = QRadioButton("URL")
        self.upload_radio = QRadioButton("Upload")
        self._group.addButton(self.url_radio, 0)
        self._group.addButton(self.upload_radio, 1)
        self.url_radio.toggled.connect(self._on_mode_toggled)
        mode_row.addWidget(self.url_radio)
        mode_row.addWidget(self.upload_radio)
        mode_row.addStretch()
        controls.addLayout(mode_row)

        self.url_edit = QLineEdit()
        self.url_edit.setFont(QFont("DM Mono", 9))
        self.url_edit.setPlaceholderText("https://…")
        self.url_edit.setStyleSheet(f"color: {COLORS['accent2']};")
        self.url_edit.editingFinished.connect(self._on_url_edited)
        controls.addWidget(self.url_edit)

        upload_row = QHBoxLayout()
        upload_row.setContentsMargins(0, 0, 0, 0)
        upload_row.setSpacing(8)
        self.upload_btn = QPushButton("Browse…")
        self.upload_btn.clicked.connect(self._on_browse)
        upload_row.addWidget(self.upload_btn)
        self.upload_status = QLabel("")
        self.upload_status.setFont(QFont("DM Sans", 9))
        self.upload_status.setStyleSheet(f"color: {COLORS['text_muted']}; {_LABEL_BASE}")
        upload_row.addWidget(self.upload_status, 1)
        controls.addLayout(upload_row)

        self.sgdb_btn = QPushButton("Browse SteamGridDB…")
        self.sgdb_btn.clicked.connect(self._on_browse_sgdb)
        controls.addWidget(self.sgdb_btn)

        row.addLayout(controls, 1)
        outer.addLayout(row)

        # ── Initial state ────────────────────────────────────────────────────
        if _looks_local(self._initial_url):
            self.upload_radio.setChecked(True)
            self.upload_status.setText(Path(self._initial_url).name)
        else:
            self.url_radio.setChecked(True)
            self.url_edit.setText(self._initial_url)
        self._on_mode_toggled()

        if self._initial_url:
            self._load_thumb(self._initial_url)

    def _on_mode_toggled(self):
        is_url = self.url_radio.isChecked()
        self.url_edit.setVisible(is_url)
        self.upload_btn.setVisible(not is_url)
        self.upload_status.setVisible(not is_url)

    def _on_url_edited(self):
        text = self.url_edit.text().strip()
        if text:
            self._load_thumb(text)

    def _on_browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, f"Select {self._kind.title()} Image", str(Path.home()),
            "Images (*.png *.jpg *.jpeg *.webp *.gif *.bmp)")
        if not path:
            return
        self._new_upload_path = path
        self.upload_status.setText(Path(path).name)
        self._load_thumb(path)

    def _on_browse_sgdb(self):
        if not self._sgdb_key:
            QMessageBox.information(
                self, "SteamGridDB",
                "No SteamGridDB API key configured.\n"
                "Add one in Settings → API Keys, then try again.")
            return
        from ui.cover_art_picker_dialog import CoverArtPickerDialog
        dlg = CoverArtPickerDialog(
            kind=self._kind,
            context_provider=self._context_provider or (lambda: {}),
            sgdb_key=self._sgdb_key,
            parent=self,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            url = dlg.selected_url()
            if url:
                self.url_radio.setChecked(True)
                self.url_edit.setText(url)
                self._load_thumb(url)

    def _load_thumb(self, path_or_url: str):
        if _looks_local(path_or_url):
            pix = QPixmap(path_or_url)
            if not pix.isNull():
                self._set_thumb_pixmap(pix)
            return
        loader = _ThumbLoader(path_or_url)
        loader.signals.loaded.connect(self._on_thumb_loaded)
        QThreadPool.globalInstance().start(loader)

    def _on_thumb_loaded(self, local_path: str):
        pix = QPixmap(local_path)
        if not pix.isNull():
            self._set_thumb_pixmap(pix)

    def _set_thumb_pixmap(self, pix: QPixmap):
        scaled = pix.scaled(120, 68,
                            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                            Qt.TransformationMode.SmoothTransformation)
        x = max(0, (scaled.width() - 120) // 2)
        y = max(0, (scaled.height() - 68) // 2)
        self.thumb.setPixmap(scaled.copy(x, y, 120, 68))

    def resolve_value(self) -> Optional[str]:
        """
        Value to store for this field:
          - URL mode: whatever's in the text field (blank → None)
          - Upload mode + a NEW file picked this session: copy it into the
            cache dir and return that path
          - Upload mode + nothing new picked: keep the original value as-is
        """
        if self.url_radio.isChecked():
            text = self.url_edit.text().strip()
            return text or None
        if self._new_upload_path:
            return _copy_into_cache(self._new_upload_path, f"manual_{self._kind}")
        return self._initial_url or None


# ── Screenshots section ────────────────────────────────────────────────────────

class _ScreenshotListWidget(QListWidget):
    """QListWidget subclass just to catch Delete key presses for removal —
    kept as its own tiny subclass rather than an event filter."""
    delete_pressed = pyqtSignal()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Delete:
            self.delete_pressed.emit()
        else:
            super().keyPressEvent(event)


class _ScreenshotsSection(QWidget):
    """
    Grid of screenshot thumbnails, reorderable by drag (QListWidget's
    native InternalMove drag-drop — this gives a real drag ghost/preview
    for free, which a fully custom per-item removable widget would risk
    losing: Qt skips its normal drag-pixmap rendering for items that have
    a setItemWidget() override, so an inline "✕ overlay per thumbnail"
    and a reliable drag ghost are in tension. Removal is instead "select
    + Remove Selected button" or the Delete key — same end result, more
    reliable rendering.
    """

    def __init__(self, initial_urls: list, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.list_widget = _ScreenshotListWidget()
        self.list_widget.setViewMode(QListWidget.ViewMode.IconMode)
        self.list_widget.setIconSize(QSize(160, 90))
        self.list_widget.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list_widget.setMovement(QListWidget.Movement.Snap)
        self.list_widget.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list_widget.setSpacing(8)
        self.list_widget.setFixedHeight(220)
        self.list_widget.setStyleSheet(f"""
            QListWidget {{
                background: {COLORS['surface3']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
            }}
            QListWidget::item {{
                border: 1px solid {COLORS['border']};
                border-radius: 6px;
            }}
            QListWidget::item:selected {{
                border: 2px solid {COLORS['accent']};
            }}
        """)
        self.list_widget.delete_pressed.connect(self._on_remove_selected)
        layout.addWidget(self.list_widget)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        add_url_btn = QPushButton("Add via URL…")
        add_url_btn.clicked.connect(self._on_add_url)
        btn_row.addWidget(add_url_btn)
        add_file_btn = QPushButton("Add via File…")
        add_file_btn.clicked.connect(self._on_add_file)
        btn_row.addWidget(add_file_btn)
        btn_row.addStretch()
        remove_btn = QPushButton("✕ Remove Selected")
        remove_btn.clicked.connect(self._on_remove_selected)
        btn_row.addWidget(remove_btn)
        layout.addLayout(btn_row)

        hint = QLabel("Drag thumbnails to reorder. Select one, then Remove Selected or Delete to remove it.")
        hint.setFont(QFont("DM Sans", 9))
        hint.setStyleSheet(f"color: {COLORS['text_muted']}; {_LABEL_BASE}")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        for url in initial_urls:
            self._add_item(url)

    def _add_item(self, url: str):
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, url)
        item.setSizeHint(QSize(170, 100))
        item.setToolTip(url)
        self.list_widget.addItem(item)
        self._load_icon(item, url)

    def _load_icon(self, item: QListWidgetItem, url: str):
        if _looks_local(url):
            pix = QPixmap(url)
            if not pix.isNull():
                item.setIcon(QIcon(self._scale(pix)))
            return
        loader = _ThumbLoader(url)
        loader.signals.loaded.connect(lambda path, it=item: self._on_icon_loaded(it, path))
        QThreadPool.globalInstance().start(loader)

    def _on_icon_loaded(self, item: QListWidgetItem, local_path: str):
        pix = QPixmap(local_path)
        if not pix.isNull():
            item.setIcon(QIcon(self._scale(pix)))

    @staticmethod
    def _scale(pix: QPixmap) -> QPixmap:
        scaled = pix.scaled(160, 90,
                            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                            Qt.TransformationMode.SmoothTransformation)
        x = max(0, (scaled.width() - 160) // 2)
        y = max(0, (scaled.height() - 90) // 2)
        return scaled.copy(x, y, 160, 90)

    def _on_add_url(self):
        url, ok = QInputDialog.getText(self, "Add Screenshot", "Image URL:")
        url = url.strip()
        if ok and url:
            self._add_item(url)

    def _on_add_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Screenshot", str(Path.home()),
            "Images (*.png *.jpg *.jpeg *.webp *.gif *.bmp)")
        if not path:
            return
        dest = _copy_into_cache(path, "manual_screenshot")
        self._add_item(dest)

    def _on_remove_selected(self):
        for item in self.list_widget.selectedItems():
            self.list_widget.takeItem(self.list_widget.row(item))

    def current_urls(self) -> list:
        """URLs in current visual order — reflects any drag reordering."""
        return [
            self.list_widget.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.list_widget.count())
        ]


# ── Steam App ID picker ───────────────────────────────────────────────────────

class _SteamSearchWorker(QThread):
    results_ready = pyqtSignal(list)

    def __init__(self, query: str):
        super().__init__()
        self._query = query

    def run(self):
        results = meta_mod.steam_search_results(self._query)
        self.results_ready.emit(results)


class _SteamResultRow(QFrame):
    """One selectable Steam search result — mirrors save_backup_dialog.py's
    _CandidateRow pattern (QButtonGroup radio + whole-row click-to-select)."""

    def __init__(self, group: QButtonGroup, result: dict, index: int, parent=None):
        super().__init__(parent)
        self.result = result
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(f"""
            QFrame {{
                background: {COLORS['surface2']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
            }}
        """)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(10)

        self.radio = QRadioButton()
        group.addButton(self.radio, index)
        layout.addWidget(self.radio)

        self.thumb = QLabel()
        self.thumb.setFixedSize(64, 30)
        self.thumb.setStyleSheet(
            f"background: {COLORS['surface3']}; border-radius: 4px; border: none;")
        layout.addWidget(self.thumb)
        if result.get("tiny_image"):
            loader = _ThumbLoader(result["tiny_image"])
            loader.signals.loaded.connect(self._on_thumb_loaded)
            QThreadPool.globalInstance().start(loader)

        name_lbl = QLabel(result.get("name") or "Unknown")
        name_lbl.setFont(QFont("DM Sans", 11))
        name_lbl.setStyleSheet(f"color: {COLORS['text']}; {_LABEL_BASE}")
        name_lbl.setWordWrap(True)
        layout.addWidget(name_lbl, 1)

        id_lbl = QLabel(f"AppID {result['id']}")
        id_lbl.setFont(QFont("DM Mono", 9))
        id_lbl.setStyleSheet(f"color: {COLORS['text_muted']}; {_LABEL_BASE}")
        layout.addWidget(id_lbl)

        view_btn = QPushButton("View ↗")
        view_btn.setFlat(True)
        view_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        view_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; border: none; color: {COLORS['accent2']}; padding: 2px 6px; }}
            QPushButton:hover {{ text-decoration: underline; }}
        """)
        view_btn.clicked.connect(self._open_steam_page)
        layout.addWidget(view_btn)

    def _on_thumb_loaded(self, local_path: str):
        pix = QPixmap(local_path)
        if not pix.isNull():
            self.thumb.setPixmap(pix.scaled(
                64, 30, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation))

    def _open_steam_page(self):
        try:
            subprocess.Popen(
                ["xdg-open", f"https://store.steampowered.com/app/{self.result['id']}"])
        except Exception as e:
            log.warning("Could not open Steam store page: %s", e)

    def mousePressEvent(self, event):
        self.radio.setChecked(True)
        super().mousePressEvent(event)


class SteamAppPickerDialog(QDialog):
    """
    Immediately searches Steam's store search API using the query passed
    in (the current Title field value from the parent dialog), shows
    results as selectable rows, and always keeps a manual App ID field
    available below — including as the only option when search finds
    nothing.
    """

    def __init__(self, query: str, parent=None):
        super().__init__(parent)
        self._query = query
        self._results: list = []
        self._selected_app_id: Optional[int] = None
        self._worker: Optional[_SteamSearchWorker] = None

        self.setWindowTitle("Pick from Steam")
        self.setModal(True)
        self.setMinimumWidth(480)
        self.setFixedWidth(500)
        self.setStyleSheet(f"QDialog {{ background: {COLORS['surface']}; }}")

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(10)

        self._status_lbl = QLabel(f"Searching Steam for “{query}”…")
        self._status_lbl.setFont(QFont("DM Sans", 11))
        self._status_lbl.setStyleSheet(f"color: {COLORS['text_muted']}; {_LABEL_BASE}")
        self._status_lbl.setWordWrap(True)
        root.addWidget(self._status_lbl)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setMaximumHeight(320)
        self._results_body = QWidget()
        self._results_body.setStyleSheet("background: transparent;")
        self._results_layout = QVBoxLayout(self._results_body)
        self._results_layout.setContentsMargins(0, 0, 0, 0)
        self._results_layout.setSpacing(6)
        self._scroll.setWidget(self._results_body)
        root.addWidget(self._scroll)

        self._group = QButtonGroup(self)

        manual_lbl = QLabel("OR ENTER APP ID MANUALLY")
        manual_lbl.setFont(QFont("DM Mono", 8))
        manual_lbl.setStyleSheet(
            f"color: {COLORS['text_muted']}; letter-spacing: 1.5px; {_LABEL_BASE}")
        root.addWidget(manual_lbl)

        self.manual_edit = QLineEdit()
        self.manual_edit.setFont(QFont("DM Mono", 10))
        self.manual_edit.setPlaceholderText("e.g. 1091500")
        self.manual_edit.setStyleSheet(f"color: {COLORS['accent2']};")
        root.addWidget(self.manual_edit)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        self.use_btn = QPushButton("Use Selected")
        self.use_btn.setStyleSheet(accent_button_style())
        self.use_btn.clicked.connect(self._on_use)
        btn_row.addWidget(self.use_btn)
        root.addLayout(btn_row)

        self._start_search()

    def _start_search(self):
        self._worker = _SteamSearchWorker(self._query)
        self._worker.results_ready.connect(self._on_results)
        self._worker.start()

    def _on_results(self, results: list):
        self._results = results
        if not results:
            self._status_lbl.setText(
                f"No results found for “{self._query}”. Enter an App ID manually below.")
            self._scroll.hide()
            return
        self._status_lbl.setText(
            f"{len(results)} result(s) for “{self._query}” — pick one, "
            "or enter an App ID manually below.")
        for i, r in enumerate(results):
            row = _SteamResultRow(self._group, r, i)
            self._results_layout.addWidget(row)
        first = self._group.button(0)
        if first:
            first.setChecked(True)

    def _on_use(self):
        manual = self.manual_edit.text().strip()
        if manual:
            try:
                self._selected_app_id = int(manual)
            except ValueError:
                QMessageBox.warning(self, "Invalid App ID", "App ID must be a number.")
                return
            self.accept()
            return

        checked_id = self._group.checkedId()
        if 0 <= checked_id < len(self._results):
            self._selected_app_id = self._results[checked_id]["id"]
            self.accept()
            return

        QMessageBox.information(
            self, "Pick from Steam", "Select a result, or enter an App ID manually.")

    def selected_app_id(self) -> Optional[int]:
        return self._selected_app_id


# ── Background re-fetch worker (Steam ID changed) ─────────────────────────────

class _RefetchWorker(QThread):
    finished_ = pyqtSignal(bool, str)   # success, error_msg

    def __init__(self, game_id: int, new_app_id: int, fallback_title: str):
        super().__init__()
        self._game_id = game_id
        self._new_app_id = new_app_id
        self._fallback_title = fallback_title

    def run(self):
        try:
            sgdb_key    = db.get_setting("sgdb_api_key", "")
            igdb_id_key = db.get_setting("igdb_client_id", "")
            igdb_secret = db.get_setting("igdb_client_secret", "")
            style_pref  = db.get_setting("sgdb_art_style", "alternate")

            ok = meta_mod.fetch_metadata_for_game(
                self._game_id, sgdb_key, igdb_id_key, igdb_secret, style_pref,
                force_steam_app_id=self._new_app_id)

            if not ok:
                # SGDB/IGDB found nothing at all (e.g. no keys configured) —
                # fetch_metadata_for_game() returned before writing anything.
                # The App ID itself must still be pinned regardless, since
                # ProtonDB/redists below key off it either way, and per spec
                # this is a full overwrite, not a no-op.
                db.upsert_metadata(self._game_id, {
                    "sgdb_id": None, "igdb_id": None,
                    "steam_app_id": self._new_app_id,
                    "title": self._fallback_title or None,
                    "description": None, "developer": None, "publisher": None,
                    "ip_holder": None, "release_date": None, "genres": [],
                    "cover_url": None, "hero_url": None, "logo_url": None,
                    "screenshots": [],
                })
                log.info("Edit Metadata: SGDB/IGDB found nothing for new "
                         "App ID %d (game %d) — pinned ID, cleared the rest",
                         self._new_app_id, self._game_id)

            protondb_mod.fetch_and_store(self._game_id)

            import redists as redists_mod
            game = db.get_game(self._game_id)
            title = (game["title"] if game else None) or self._fallback_title
            redists_mod.refresh_single_game(self._new_app_id, title)
            # None return from refresh_single_game() just means SteamCMD
            # isn't installed on this machine — same "skip silently"
            # handling used everywhere else redists.py is called from a
            # background flow like this.

            self.finished_.emit(True, "")
        except Exception as e:
            log.error("Edit Metadata: re-fetch failed for game %d: %s",
                      self._game_id, e, exc_info=True)
            self.finished_.emit(False, str(e))


# ── Main dialog ───────────────────────────────────────────────────────────────

class EditMetadataDialog(QDialog):
    saved = pyqtSignal(int)   # game_id

    def __init__(self, game_id: int, parent=None):
        super().__init__(parent)
        game = db.get_game(game_id)
        if not game:
            raise ValueError(f"Game {game_id} not found")

        self._game_id = game_id
        self._folder_name = game["folder_name"]
        self._original_title = game["title"] or game["display_name"] or game["folder_name"]
        self._original_sgdb_id = game["sgdb_id"]
        self._original_igdb_id = game["igdb_id"]
        self._original_steam_app_id = game["steam_app_id"]
        self._refetch_worker: Optional[_RefetchWorker] = None

        self.setWindowTitle(f"Edit Metadata — {self._original_title}")
        self.setModal(True)
        self.setMinimumWidth(600)
        self.setFixedWidth(640)
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

        # ── Basic Info ────────────────────────────────────────────────────────
        basic_box = SectionBox("Basic Info")
        self.title_edit = QLineEdit(self._original_title)
        basic_box.add(_field_row("Title", self.title_edit))

        self.sort_name_edit = QLineEdit(game["sort_name"] or "")
        self.sort_name_edit.setPlaceholderText("Uses default sort order")
        basic_box.add(_field_row("Sort Name", self.sort_name_edit))

        self.developer_edit = QLineEdit(game["developer"] or "")
        basic_box.add(_field_row("Developer", self.developer_edit))

        self.publisher_edit = QLineEdit(game["publisher"] or "")
        basic_box.add(_field_row("Publisher", self.publisher_edit))

        self.ip_holder_edit = QLineEdit(game["ip_holder"] or "")
        basic_box.add(_field_row("IP Holder", self.ip_holder_edit))

        self.release_date_edit = QLineEdit(game["release_date"] or "")
        self.release_date_edit.setPlaceholderText('e.g. "March 25, 2022"')
        basic_box.add(_field_row("Release Date", self.release_date_edit))

        genres_list = []
        if game["genres"]:
            try:
                genres_list = json.loads(game["genres"])
            except (json.JSONDecodeError, TypeError):
                pass
        self.genres_edit = QLineEdit(", ".join(genres_list))
        self.genres_edit.setPlaceholderText("Action, RPG, Adventure")
        basic_box.add(_field_row("Genres", self.genres_edit))
        body_l.addWidget(basic_box)

        # ── Description ───────────────────────────────────────────────────────
        desc_box = SectionBox("Description")
        self.desc_edit = QTextEdit()
        self.desc_edit.setPlainText(game["description"] or "")
        self.desc_edit.setFixedHeight(90)
        self.desc_edit.setStyleSheet(f"""
            QTextEdit {{
                background: {COLORS['surface3']};
                border: 1px solid {COLORS['border']};
                border-radius: 6px;
                color: {COLORS['text']};
                padding: 6px 8px;
            }}
        """)
        desc_box.add(self.desc_edit)
        body_l.addWidget(desc_box)

        # ── Art ───────────────────────────────────────────────────────────────
        # art_ctx is read lazily (see _ArtField._on_browse_sgdb), so it's
        # safe that self.title_edit / self.steam_id_edit are constructed
        # further down in this method — by the time the user actually
        # clicks "Browse SteamGridDB…" those widgets already exist.
        art_ctx = lambda: {
            "sgdb_id":      self._original_sgdb_id,
            "steam_app_id": (int(self.steam_id_edit.text().strip())
                              if self.steam_id_edit.text().strip().isdigit()
                              else None),
            "title":        self.title_edit.text().strip() or self._original_title,
        }
        sgdb_key = db.get_setting("sgdb_api_key", "")

        art_box = SectionBox("Art")
        self.cover_field = _ArtField("cover", "Cover", game["cover_url"] or "",
                                      context_provider=art_ctx, sgdb_key=sgdb_key)
        self.hero_field  = _ArtField("hero",  "Hero",  game["hero_url"]  or "",
                                      context_provider=art_ctx, sgdb_key=sgdb_key)
        self.logo_field  = _ArtField("logo",  "Logo",  game["logo_url"]  or "",
                                      context_provider=art_ctx, sgdb_key=sgdb_key)
        art_box.add(self.cover_field)
        art_box.add_separator()
        art_box.add(self.hero_field)
        art_box.add_separator()
        art_box.add(self.logo_field)
        body_l.addWidget(art_box)

        # ── Screenshots ───────────────────────────────────────────────────────
        ss_box = SectionBox("Screenshots")
        initial_screens = []
        if game["screenshots"]:
            try:
                initial_screens = json.loads(game["screenshots"])
            except (json.JSONDecodeError, TypeError):
                pass
        self.screenshots_section = _ScreenshotsSection(initial_screens)
        ss_box.add(self.screenshots_section)
        body_l.addWidget(ss_box)

        # ── Steam App ID ──────────────────────────────────────────────────────
        steam_box = SectionBox("Steam App ID")
        steam_row = QWidget()
        steam_row.setStyleSheet("background: transparent;")
        steam_row_l = QHBoxLayout(steam_row)
        steam_row_l.setContentsMargins(0, 0, 0, 0)
        steam_row_l.setSpacing(8)
        self.steam_id_edit = QLineEdit(
            str(game["steam_app_id"]) if game["steam_app_id"] else "")
        self.steam_id_edit.setPlaceholderText("e.g. 1091500")
        self.steam_id_edit.setFont(QFont("DM Mono", 10))
        self.steam_id_edit.setStyleSheet(f"color: {COLORS['accent2']};")
        steam_row_l.addWidget(self.steam_id_edit, 1)
        pick_btn = QPushButton("Pick from Steam…")
        pick_btn.clicked.connect(self._on_pick_from_steam)
        steam_row_l.addWidget(pick_btn)
        steam_box.add(steam_row)

        steam_note = QLabel(
            "Changing this re-fetches all metadata for this game from "
            "scratch — art, description, genres, ProtonDB compatibility, "
            "and redistributables — and overwrites everything currently "
            "stored. You'll be asked to confirm before it runs.")
        steam_note.setFont(QFont("DM Sans", 9))
        steam_note.setStyleSheet(f"color: {COLORS['text_muted']}; {_LABEL_BASE}")
        steam_note.setWordWrap(True)
        steam_box.add(steam_note)
        body_l.addWidget(steam_box)

        self._status_lbl = QLabel("")
        self._status_lbl.setFont(QFont("DM Sans", 10))
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
        foot_l.addStretch()
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        foot_l.addWidget(self.cancel_btn)
        self.save_btn = QPushButton("Save")
        self.save_btn.setStyleSheet(accent_button_style())
        self.save_btn.clicked.connect(self._on_save)
        foot_l.addWidget(self.save_btn)
        root.addWidget(footer)

    # ── Steam picker ──────────────────────────────────────────────────────────

    def _on_pick_from_steam(self):
        query = (self.title_edit.text().strip() or self._original_title
                 or self._folder_name)
        dlg = SteamAppPickerDialog(query, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            app_id = dlg.selected_app_id()
            if app_id:
                self.steam_id_edit.setText(str(app_id))

    # ── Save ──────────────────────────────────────────────────────────────────

    def _on_save(self):
        steam_id_text = self.steam_id_edit.text().strip()
        new_steam_app_id: Optional[int] = None
        if steam_id_text:
            try:
                new_steam_app_id = int(steam_id_text)
            except ValueError:
                QMessageBox.warning(
                    self, "Invalid Steam App ID", "Steam App ID must be a number.")
                return

        sort_name = self.sort_name_edit.text().strip() or None
        steam_id_changed = (new_steam_app_id != self._original_steam_app_id)

        # Only a genuine change TO a real new ID triggers the confirm+refetch
        # flow — clearing the field to blank just saves as NULL like any
        # other field (there's no new ID to fetch metadata for).
        if steam_id_changed and new_steam_app_id:
            reply = QMessageBox.question(
                self, "Steam App ID Changed",
                "Changing the Steam App ID will re-fetch all metadata for "
                "this game from scratch — art, description, genres, "
                "ProtonDB, and redistributables — and overwrite everything "
                "currently stored for it. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            db.set_sort_name(self._game_id, sort_name)
            self._start_refetch(new_steam_app_id)
            return

        # ── Normal path: Steam ID unchanged (or cleared) ────────────────────
        try:
            cover_url = self.cover_field.resolve_value()
            hero_url  = self.hero_field.resolve_value()
            logo_url  = self.logo_field.resolve_value()
        except Exception as e:
            QMessageBox.warning(self, "Save Failed", f"Could not process art file:\n{e}")
            return

        genres = [g.strip() for g in self.genres_edit.text().split(",") if g.strip()]

        metadata_dict = {
            # sgdb_id/igdb_id aren't exposed as editable fields, and
            # db.upsert_metadata() has no COALESCE guard for them the way
            # it does for the art URLs — pass the original values through
            # unchanged so a manual save never clobbers them to NULL.
            "sgdb_id": self._original_sgdb_id,
            "igdb_id": self._original_igdb_id,
            "steam_app_id": new_steam_app_id,
            "title": self.title_edit.text().strip() or None,
            "description": self.desc_edit.toPlainText().strip() or None,
            "developer": self.developer_edit.text().strip() or None,
            "publisher": self.publisher_edit.text().strip() or None,
            "ip_holder": self.ip_holder_edit.text().strip() or None,
            "release_date": self.release_date_edit.text().strip() or None,
            "genres": genres,
            "cover_url": cover_url,
            "hero_url": hero_url,
            "logo_url": logo_url,
            "screenshots": self.screenshots_section.current_urls(),
        }
        db.upsert_metadata(self._game_id, metadata_dict)
        db.set_sort_name(self._game_id, sort_name)
        self._finish_save()

    def _start_refetch(self, new_app_id: int):
        self.save_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self._status_lbl.setText("Re-fetching metadata for the new Steam App ID…")
        self._status_lbl.setStyleSheet(f"color: {COLORS['text_muted']}; {_LABEL_BASE}")

        title = self.title_edit.text().strip() or self._original_title or self._folder_name
        self._refetch_worker = _RefetchWorker(self._game_id, new_app_id, title)
        self._refetch_worker.finished_.connect(self._on_refetch_done)
        self._refetch_worker.start()

    def _on_refetch_done(self, success: bool, error_msg: str):
        self.save_btn.setEnabled(True)
        self.cancel_btn.setEnabled(True)
        if success:
            self._finish_save()
        else:
            self._status_lbl.setText(f"✗ Re-fetch failed: {error_msg}")
            self._status_lbl.setStyleSheet(f"color: {COLORS['danger']}; {_LABEL_BASE}")

    def _finish_save(self):
        self.saved.emit(self._game_id)
        self.accept()
