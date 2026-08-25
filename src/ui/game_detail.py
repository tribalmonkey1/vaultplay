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
    QStringListModel
)
from PyQt6.QtGui import QPixmap, QFont, QColor, QPainter, QLinearGradient

import db
import metadata as meta_mod
import scanner
import protondb as protondb_mod
import save_backup
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


class ScreenshotThumb(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(200, 113)
        self.setStyleSheet(
            f"background: {COLORS['surface2']}; border: 1px solid {COLORS['border']}; border-radius: 6px;"
        )
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setText("⬛")

    def set_image(self, local_path: str):
        pix = QPixmap(local_path)
        if not pix.isNull():
            scaled = pix.scaled(200, 113,
                                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                Qt.TransformationMode.SmoothTransformation)
            cropped = scaled.copy((scaled.width()-200)//2, (scaled.height()-113)//2, 200, 113)
            self.setPixmap(cropped)
            self.setStyleSheet(
                "border: 1px solid rgba(255,255,255,0.08); border-radius: 6px;"
            )


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

    def __init__(self, parent=None):
        super().__init__(parent)
        self._game_id = None
        self._pool = QThreadPool.globalInstance()
        self._screenshot_thumbs: list[ScreenshotThumb] = []
        # Currently Playing Indicator — set via set_currently_playing() by
        # MainWindow, which owns the actual PlaytimeWatcher/running state.
        # None = no game currently running (the common case).
        self._currently_playing_game_id: int | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Back button bar
        back_bar = QWidget()
        back_bar.setFixedHeight(44)
        back_bar.setStyleSheet(f"background: {COLORS['surface']}; border-bottom: 1px solid {COLORS['border']};")
        back_layout = QHBoxLayout(back_bar)
        back_layout.setContentsMargins(20, 0, 20, 0)

        back_btn = QPushButton("← Back to Library")
        back_btn.setFont(QFont("DM Sans", 11))
        back_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                border: none;
                color: {COLORS['text_muted']};
                padding: 0;
            }}
            QPushButton:hover {{ color: {COLORS['text']}; }}
        """)
        back_btn.clicked.connect(self.back_requested)
        back_layout.addWidget(back_btn)
        back_layout.addStretch()
        root.addWidget(back_bar)

        # Scrollable body
        scroll = QScrollArea()
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

        self._section_title(self.left_layout, "About This Game")
        self.desc_label = QLabel("No description available.")
        self.desc_label.setFont(QFont("DM Sans", 12))
        self.desc_label.setStyleSheet(f"color: {COLORS['text_dim']}; line-height: 1.7;")
        self.desc_label.setWordWrap(True)
        self.left_layout.addWidget(self.desc_label)
        self.left_layout.addSpacing(24)

        self._section_title(self.left_layout, "Screenshots")
        self.screenshots_layout = QGridLayout()
        self.screenshots_layout.setSpacing(10)
        self.left_layout.addLayout(self.screenshots_layout)
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

        # Library actions card (favorite + hide)
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

    def _section_title(self, layout, text: str):
        lbl = QLabel(text.upper())
        lbl.setFont(QFont("DM Mono", 9, QFont.Weight.DemiBold))
        lbl.setStyleSheet(f"color: {COLORS['text_muted']}; letter-spacing: 2px; padding: 0 0 8px 0;")
        layout.addWidget(lbl)

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

    # ── Load game ─────────────────────────────────────────────────────────────

    def load_game(self, game_id: int):
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

        # Screenshots
        for i in reversed(range(self.screenshots_layout.count())):
            item = self.screenshots_layout.takeAt(i)
            if item.widget():
                item.widget().deleteLater()
        self._screenshot_thumbs.clear()

        if game["screenshots"]:
            try:
                urls = json.loads(game["screenshots"]) if isinstance(game["screenshots"], str) else game["screenshots"]
                for i, url in enumerate(urls[:6]):
                    thumb = ScreenshotThumb()
                    self._screenshot_thumbs.append(thumb)
                    self.screenshots_layout.addWidget(thumb, i // 3, i % 3)
                    loader = ImageLoader(url)
                    loader.signals.loaded.connect(self._on_screenshot_loaded)
                    self._pool.start(loader)
            except Exception:
                pass

        # Async load hero + cover
        if game["hero_url"]:
            loader = ImageLoader(game["hero_url"])
            loader.signals.loaded.connect(self._on_hero_loaded)
            self._pool.start(loader)

        if game["cover_url"]:
            loader = ImageLoader(game["cover_url"])
            loader.signals.loaded.connect(self._on_cover_loaded)
            self._pool.start(loader)

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
        persists the current text (empty/whitespace-only saves as NULL —
        see db.set_notes()) and switches back to read-only.
        """
        if self.notes_edit.isReadOnly():
            self.notes_edit.setReadOnly(False)
            self.notes_edit.setFocus()
            self.notes_edit_btn.setText("💾  Save")
        else:
            if self._game_id is not None:
                db.set_notes(self._game_id, self.notes_edit.toPlainText())
            self.notes_edit.setReadOnly(True)
            self.notes_edit_btn.setText("✎  Edit")

    # ── Currently Playing Indicator ──────────────────────────────────────────

    def set_currently_playing(self, game_id: int | None):
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
        self.hero.set_image(local_path)

    def _on_cover_loaded(self, url: str, local_path: str):
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

    def _on_screenshot_loaded(self, url: str, local_path: str):
        # Find which thumb is waiting for this URL — we use insertion order
        game = db.get_game(self._game_id)
        if not game or not game["screenshots"]:
            return
        try:
            urls = json.loads(game["screenshots"]) if isinstance(game["screenshots"], str) else game["screenshots"]
            if url in urls:
                idx = urls.index(url)
                if idx < len(self._screenshot_thumbs):
                    self._screenshot_thumbs[idx].set_image(local_path)
        except Exception:
            pass

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
            existing = db.get_save_paths(self._game_id)
            source_path = existing.get("save_source_path")
            save_path   = existing.get("save_path")
            diag = save_backup.diagnose_source_path(source_path, save_path)
            log.debug("[SAVE BACKUP] Pre-launch check for game_id=%d (%s): diag=%s",
                      self._game_id, game["folder_name"], diag)

            if diag == "ok":
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
        """
        Three-step uninstall:
          1. Remove the DB record + desktop/script files (always)
          2. Ask: delete game files?
          3. Ask: delete Wine prefix?
        """
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

        # ── Step 1: confirm and remove DB record ──────────────────────────────
        confirm = QMessageBox(self)
        confirm.setWindowTitle("Uninstall Game")
        confirm.setText(f"Uninstall <b>{title}</b>?")
        confirm.setInformativeText(
            "This will remove the VaultPlay install record, "
            "desktop shortcut, and launcher script.\n\n"
            "You will be asked separately about game files and the Wine prefix."
        )
        confirm.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        confirm.setDefaultButton(QMessageBox.StandardButton.Cancel)
        confirm.setStyleSheet(
            f"QMessageBox {{ background: {COLORS['surface']}; "
            f"color: {COLORS['text']}; }}")
        if confirm.exec() != QMessageBox.StandardButton.Yes:
            return

        # Remove desktop shortcut and launcher script
        for p in [desktop_path, script_path]:
            if p and Path(p).exists():
                try:
                    Path(p).unlink()
                    log.info("Deleted: %s", p)
                except Exception as e:
                    log.warning("Could not delete %s: %s", p, e)

        # Remove install record from DB
        try:
            db.remove_install(self._game_id)
            log.info("Uninstalled game %d (%s) — DB record removed", self._game_id, title)
        except Exception as e:
            log.error("remove_install failed: %s", e)

        # ── Step 2: delete game files? ────────────────────────────────────────
        if install_path and Path(install_path).exists():
            files_msg = QMessageBox(self)
            files_msg.setWindowTitle("Delete Game Files?")
            files_msg.setText(f"Delete game files for <b>{title}</b>?")
            files_msg.setInformativeText(
                f"This will permanently delete:\n{install_path}\n\n"
                "This cannot be undone."
            )
            yes_btn    = files_msg.addButton("Delete Game Files",
                                              QMessageBox.ButtonRole.DestructiveRole)
            keep_btn   = files_msg.addButton("Keep Files",
                                              QMessageBox.ButtonRole.AcceptRole)
            files_msg.setDefaultButton(keep_btn)
            files_msg.setStyleSheet(
                f"QMessageBox {{ background: {COLORS['surface']}; "
                f"color: {COLORS['text']}; }}")
            files_msg.exec()
            if files_msg.clickedButton() == yes_btn:
                try:
                    shutil.rmtree(install_path)
                    log.info("Deleted game files: %s", install_path)
                except Exception as e:
                    QMessageBox.warning(self, "Delete Failed",
                                        f"Could not delete game files:\n{e}")

        # ── Step 3: delete Wine prefix? ───────────────────────────────────────
        if wine_prefix and Path(wine_prefix).exists():
            prefix_msg = QMessageBox(self)
            prefix_msg.setWindowTitle("Delete Wine Prefix?")
            prefix_msg.setText(f"Delete the Wine prefix for <b>{title}</b>?")
            prefix_msg.setInformativeText(
                f"This will permanently delete:\n{wine_prefix}\n\n"
                "Only do this if no other games share this prefix."
            )
            yes_btn2   = prefix_msg.addButton("Delete Prefix",
                                               QMessageBox.ButtonRole.DestructiveRole)
            keep_btn2  = prefix_msg.addButton("Keep Prefix",
                                               QMessageBox.ButtonRole.AcceptRole)
            prefix_msg.setDefaultButton(keep_btn2)
            prefix_msg.setStyleSheet(
                f"QMessageBox {{ background: {COLORS['surface']}; "
                f"color: {COLORS['text']}; }}")
            prefix_msg.exec()
            if prefix_msg.clickedButton() == yes_btn2:
                try:
                    shutil.rmtree(wine_prefix)
                    log.info("Deleted Wine prefix: %s", wine_prefix)
                except Exception as e:
                    QMessageBox.warning(self, "Delete Failed",
                                        f"Could not delete Wine prefix:\n{e}")

        # Reload and notify library
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

    The launch command is a shell string like:
        env WINEPREFIX="..." STEAM_COMPAT_DATA_PATH="..." "/path/to/proton" run "game.exe"
        env WINEPREFIX="..." wine "game.exe"

    Strategy: scan tokens left-to-right, skip 'env' and KEY=VALUE pairs,
    return the first remaining token (the actual binary path or name).
    Falls back to "wine" if nothing recognisable is found.

    This value is passed to PlaytimeWatcher so it can choose the correct
    wait strategy (proc.wait() for Proton, wineserver --wait for Wine).
    Never hardcode "wine" here — the binary changes based on what was
    configured at install time.
    """
    import shlex
    try:
        tokens = shlex.split(launch_cmd)
    except ValueError:
        # Malformed shell string — fall back to simple split
        tokens = launch_cmd.split()

    for token in tokens:
        if token == "env":
            continue
        if "=" in token and not token.startswith("/") and not token.startswith('"'):
            # KEY=VALUE env var — skip
            continue
        # First non-env token is the binary
        return token

    return "wine"  # safe fallback


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
