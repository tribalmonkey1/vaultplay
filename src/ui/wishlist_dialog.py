"""
ui/wishlist_dialog.py — Add/Edit Wishlist Item dialog for VaultPlay

Spec: Notion → Features → Fully Planned → Wishlist.

Modeled on edit_metadata_dialog.py's structure (SectionBox pattern, single
scrollable QDialog). Fields: Title (required), Release Date (freetext,
optional), Notes (optional), Cover art (dual URL/Upload, same pattern as
Edit Metadata's _ArtField, plus a "Browse SteamGridDB…" button reusing
cover_art_picker_dialog.CoverArtPickerDialog in search-name-only mode).

Unlike Edit Metadata's art fields, an "Upload" pick here is copied into
the SHARED <wishlist_path>/wishlist_art/ folder (via wishlist_store.py),
not the local ~/.config/vaultplay/cache — so it's visible from any
machine pointed at the same shared folder. See wishlist_store.py's module
docstring for why.

One dialog instance handles both Add (item=None) and Edit (item=dict,
from wishlist_store.get_items()) — same "one dialog, optional initial
row" shape used elsewhere in this codebase isn't quite precedented, but
mirrors EditMetadataDialog's always-there-is-an-existing-row assumption
closely enough that duplicating the whole dialog for Add vs. Edit would
just be two near-identical copies of the same form.
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
from typing import Optional

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QTextEdit, QWidget, QFrame, QScrollArea, QFileDialog, QMessageBox,
    QButtonGroup, QRadioButton
)
from PyQt6.QtCore import Qt, pyqtSignal, QThreadPool, QRunnable, pyqtSlot, QObject
from PyQt6.QtGui import QFont, QPixmap

import db
import metadata as meta_mod
import wishlist_store
from ui.style import COLORS, accent_button_style

log = logging.getLogger(__name__)

_LABEL_BASE = "background: transparent; border: none;"


# ── Section box (mirrors edit_metadata_dialog.py's SectionBox) ───────────────

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


def _field_row(label: str, widget: QWidget) -> QWidget:
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


# ── Async thumbnail loader (URL only — local wishlist_art/ paths resolve
# synchronously off local/mounted disk, no thread needed) ────────────────────

class _ThumbSignals(QObject):
    loaded = pyqtSignal(str)   # local_path


class _ThumbLoader(QRunnable):
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


# ── Cover art field ────────────────────────────────────────────────────────

class _WishlistArtField(QWidget):
    """
    Dual URL/Upload cover art picker, mirroring edit_metadata_dialog.py's
    _ArtField — but an "Upload" pick is copied into the SHARED
    <wishlist_path>/wishlist_art/ folder (see wishlist_store.py) instead
    of the local cache, and resolve_value() returns a path relative to
    wishlist_path for uploads rather than an absolute one.

    initial_cover_url is whatever's stored in the item's cover_url field
    already — either a URL or a "wishlist_art/<uuid>.ext" relative path.
    """

    def __init__(self, initial_cover_url: str, title_provider,
                 sgdb_key: str, parent=None):
        super().__init__(parent)
        self._initial_cover_url = initial_cover_url or ""
        self._new_upload_path: Optional[str] = None   # local src path picked this session
        self._title_provider = title_provider          # zero-arg -> current title text
        self._sgdb_key = sgdb_key

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)

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
        if wishlist_store.is_local_art_path(self._initial_cover_url):
            self.upload_radio.setChecked(True)
            self.upload_status.setText(Path(self._initial_cover_url).name)
        else:
            self.url_radio.setChecked(True)
            self.url_edit.setText(self._initial_cover_url)
        self._on_mode_toggled()

        if self._initial_cover_url:
            self._load_thumb(self._initial_cover_url)

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
            self, "Select Cover Image", str(Path.home()),
            "Images (*.png *.jpg *.jpeg *.webp *.gif *.bmp)")
        if not path:
            return
        self._new_upload_path = path
        self.upload_status.setText(Path(path).name)
        # A freshly-picked local file is still a normal absolute path on
        # THIS machine until Save actually copies it into wishlist_art/ —
        # safe to preview directly from there.
        pix = QPixmap(path)
        if not pix.isNull():
            self._set_thumb_pixmap(pix)

    def _on_browse_sgdb(self):
        if not self._sgdb_key:
            QMessageBox.information(
                self, "SteamGridDB",
                "No SteamGridDB API key configured.\n"
                "Add one in Settings → API Keys, then try again.")
            return
        from ui.cover_art_picker_dialog import CoverArtPickerDialog
        # Wishlist entries carry no sgdb_id/steam_app_id — search-name-only
        # mode, same fallback path Edit Metadata's picker already supports.
        dlg = CoverArtPickerDialog(
            kind="cover",
            context_provider=lambda: {
                "sgdb_id": None, "steam_app_id": None,
                "title": (self._title_provider() or "").strip(),
            },
            sgdb_key=self._sgdb_key,
            parent=self,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            url = dlg.selected_url()
            if url:
                self.url_radio.setChecked(True)
                self.url_edit.setText(url)
                self._load_thumb(url)

    def _load_thumb(self, cover_value: str):
        """cover_value may be a URL, a wishlist_art/... relative path, or
        a plain local absolute path (fresh upload not yet saved)."""
        if wishlist_store.is_local_art_path(cover_value):
            resolved = wishlist_store.resolve_cover_path(cover_value)
            if resolved:
                pix = QPixmap(resolved)
                if not pix.isNull():
                    self._set_thumb_pixmap(pix)
            return
        try:
            p = Path(cover_value)
            if p.is_absolute() and p.is_file():
                pix = QPixmap(str(p))
                if not pix.isNull():
                    self._set_thumb_pixmap(pix)
                return
        except OSError:
            pass
        loader = _ThumbLoader(cover_value)
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

    def resolve_value(self) -> tuple[str, Optional[str]]:
        """
        Returns (cover_url_to_store, error_message). error_message is
        None on success. Value to store:
          - URL mode: whatever's in the text field (blank → "")
          - Upload mode + a NEW file picked this session: copy it into
            wishlist_art/ now and return the resulting relative path. If
            the copy fails (e.g. wishlist_path unreachable right now),
            returns ("", error_message) — caller should block Save.
          - Upload mode + nothing new picked: keep the original value
            as-is (already a wishlist_art/... path, or blank).
        """
        if self.url_radio.isChecked():
            return self.url_edit.text().strip(), None
        if self._new_upload_path:
            rel = wishlist_store.copy_art_into_wishlist(self._new_upload_path)
            if rel is None:
                return "", ("Could not copy the image into the Wishlist folder — "
                           "check that the Wishlist location in Settings → Paths "
                           "is reachable.")
            return rel, None
        return self._initial_cover_url or "", None


# ── Main dialog ───────────────────────────────────────────────────────────────

class WishlistItemDialog(QDialog):
    """
    item=None → Add mode. item=a dict from wishlist_store.get_items() →
    Edit mode, pre-filled. Emits `saved` with the item's id on success.
    """
    saved = pyqtSignal(str)   # wishlist item id

    def __init__(self, item: Optional[dict] = None, parent=None):
        super().__init__(parent)
        self._item_id = item.get("id") if item else None
        is_edit = item is not None

        self.setWindowTitle("Edit Wishlist Item" if is_edit else "Add to Wishlist")
        self.setModal(True)
        self.setMinimumWidth(520)
        self.setFixedWidth(540)
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
        basic_box = SectionBox("Details")

        self.title_edit = QLineEdit(item.get("title", "") if item else "")
        self.title_edit.setPlaceholderText("Game title")
        basic_box.add(_field_row("Title", self.title_edit))

        self.release_edit = QLineEdit(item.get("release_date", "") if item else "")
        self.release_edit.setPlaceholderText(
            "e.g. March 2027, or leave blank if already released")
        basic_box.add(_field_row("Release Date", self.release_edit))

        body_l.addWidget(basic_box)

        # ── Notes ─────────────────────────────────────────────────────────────
        notes_box = SectionBox("Notes")
        self.notes_edit = QTextEdit()
        self.notes_edit.setPlainText(item.get("notes", "") if item else "")
        self.notes_edit.setFixedHeight(80)
        self.notes_edit.setStyleSheet(f"""
            QTextEdit {{
                background: {COLORS['surface3']};
                border: 1px solid {COLORS['border']};
                border-radius: 6px;
                color: {COLORS['text']};
                padding: 6px 8px;
            }}
        """)
        notes_box.add(self.notes_edit)
        body_l.addWidget(notes_box)

        # ── Cover art ─────────────────────────────────────────────────────────
        art_box = SectionBox("Cover Art")
        sgdb_key = db.get_setting("sgdb_api_key", "")
        self.art_field = _WishlistArtField(
            item.get("cover_url", "") if item else "",
            title_provider=lambda: self.title_edit.text(),
            sgdb_key=sgdb_key,
        )
        art_box.add(self.art_field)
        body_l.addWidget(art_box)

        self._status_lbl = QLabel("")
        self._status_lbl.setFont(QFont("DM Sans", 10))
        self._status_lbl.setStyleSheet(f"color: {COLORS['danger']}; {_LABEL_BASE}")
        self._status_lbl.setWordWrap(True)
        self._status_lbl.hide()
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

    def _show_error(self, message: str):
        self._status_lbl.setText(message)
        self._status_lbl.setStyleSheet(f"color: {COLORS['danger']}; {_LABEL_BASE}")
        self._status_lbl.show()

    def _on_save(self):
        title = self.title_edit.text().strip()
        if not title:
            self._show_error("Title is required.")
            return

        if not wishlist_store.is_configured():
            self._show_error(
                "No Wishlist location configured — set one in Settings → Paths first.")
            return

        cover_url, art_err = self.art_field.resolve_value()
        if art_err:
            self._show_error(art_err)
            return

        release_date = self.release_edit.text().strip()
        notes = self.notes_edit.toPlainText().strip()

        if self._item_id:
            ok = wishlist_store.update_item(
                self._item_id, title=title, release_date=release_date,
                notes=notes, cover_url=cover_url)
        else:
            new_item = wishlist_store.add_item(
                title=title, release_date=release_date, notes=notes,
                cover_url=cover_url)
            ok = new_item is not None
            if ok:
                self._item_id = new_item["id"]

        if not ok:
            self._show_error(
                "Could not save — check that the Wishlist location in "
                "Settings → Paths is still reachable.")
            return

        self.saved.emit(self._item_id)
        self.accept()
