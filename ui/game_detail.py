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
    QScrollArea, QFrame, QGridLayout, QSizePolicy, QMessageBox
)
from PyQt6.QtCore import (
    Qt, pyqtSignal, QRunnable, QThreadPool, pyqtSlot, QObject, QTimer
)
from PyQt6.QtGui import QPixmap, QFont, QColor, QPainter, QLinearGradient

import db
import metadata as meta_mod
import scanner
import protondb as protondb_mod
from ui.style import COLORS, accent_button_style, card_style
from ui.install_dialog import InstallDialog

log = logging.getLogger(__name__)


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


class GameDetailView(QWidget):
    back_requested    = pyqtSignal()
    install_finished  = pyqtSignal(int)   # game_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self._game_id = None
        self._pool = QThreadPool.globalInstance()
        self._screenshot_thumbs: list[ScreenshotThumb] = []

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

        self.launch_btn = QPushButton("▶  Launch Game")
        self.launch_btn.setFont(QFont("Rajdhani", 13, QFont.Weight.Bold))
        self.launch_btn.setStyleSheet(accent_button_style())
        self.launch_btn.clicked.connect(self._on_launch_clicked)
        self.launch_btn.hide()
        install_card_layout.addWidget(self.launch_btn)

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
            exe = (game["exe_path"] or "").strip()
            if exe and Path(exe).exists():
                self.launch_btn.show()
                self.exe_missing_label.hide()
                self.open_folder_btn.hide()
            else:
                # Installed but exe path missing or stale
                self.launch_btn.hide()
                self.exe_missing_label.show()
                install_path = (game["install_path"] or "").strip()
                if install_path:
                    self.open_folder_btn.show()
            self.uninstall_btn.show()
        else:
            self.install_btn.show()
            self.launch_btn.hide()

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

    def _on_launch_clicked(self):
        """Launch the installed game via Wine/Proton with its saved prefix."""
        if not self._game_id:
            return
        game = db.get_game(self._game_id)
        if not game:
            return

        exe_path     = (game["exe_path"]     or "").strip()
        wine_prefix  = (game["wine_prefix"]  or "").strip()
        desktop_path = (game["desktop_path"] or "").strip()

        # Best option: parse and run the Exec= line from the .desktop file —
        # it has all the correct env vars baked in. xdg-open on a .desktop
        # opens it as a text document rather than executing it.
        if desktop_path and Path(desktop_path).exists():
            try:
                exec_line = None
                for line in Path(desktop_path).read_text().splitlines():
                    if line.startswith("Exec="):
                        exec_line = line[5:].strip()
                        break
                if exec_line:
                    subprocess.Popen(exec_line, shell=True)
                    log.info("Launched via .desktop Exec: %s", exec_line)
                    return
            except Exception as e:
                log.warning(".desktop exec failed: %s — falling back", e)

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
            subprocess.Popen(cmd, env=env)
            log.info("Launched: %s %s (WINEPREFIX=%s)", wine_bin, exe_path, wine_prefix)
        except Exception as e:
            QMessageBox.warning(self, "Launch Error", f"Could not launch game:\n{e}")
        except Exception as e:
            QMessageBox.warning(self, "Launch Error",
                                f"Could not start game:\n{e}")

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
    def __init__(self, label: str, value: str, mono: bool = False, parent=None):
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
