"""
ui/launch_options_section.py — Shared Launch Options widget for VaultPlay

Spec: Notion → Features → Fully Planned → Launch Options.

A single self-contained QWidget that renders the full Launch Options form
(renderer, overlay, GameMode, FSR, sync, audio latency, GPU selection,
Proton-only options, engine args, free-text env/launch args, Reset to
Defaults). Built as its own widget — not inlined into ui/cogwheel_menu.py —
specifically so ui/install_dialog.py's planned "Launch Options" tab can
embed the exact same widget later without duplicating ~300 lines of UI code.

Callers are responsible for:
  - Constructing with a game_id (and optionally is_installed=False for the
    Install dialog, where hardware-dependent installed-tool greying still
    applies but nothing is written to the DB until the caller decides to).
  - Calling set_is_proton(bool) whenever the host dialog's Proton/Wine
    selection changes, so Proton-only options show/hide reactively.
  - Calling collect() to read the current widget state back into an
    options dict shaped for launch_options.save_effective_options().
  - Calling load(effective_dict) to push a fresh effective-options dict
    into the widgets (e.g. after a Reset to Defaults).
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

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QSpinBox,
    QLineEdit, QFrame, QPushButton, QMessageBox, QSlider
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont

import launch_options as lo
from ui.style import COLORS

log = logging.getLogger(__name__)

_LABEL_BASE = "background: transparent; border: none;"


def _label(text: str, muted: bool = False, wrap: bool = False) -> QLabel:
    lbl = QLabel(text)
    lbl.setFont(QFont("DM Sans", 10))
    lbl.setStyleSheet(f"color: {COLORS['text_muted'] if muted else COLORS['text']}; {_LABEL_BASE}")
    if wrap:
        lbl.setWordWrap(True)
    return lbl


class _MiniGroup(QFrame):
    """Small bordered sub-section within the Launch Options widget — one per
    logical group (Renderer, Overlay, GameMode, FSR, ...)."""

    def __init__(self, title: str = "", parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"""
            QFrame {{
                background: {COLORS['surface2']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
            }}""")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(12, 10, 12, 10)
        self._layout.setSpacing(6)
        if title:
            lbl = QLabel(title.upper())
            lbl.setFont(QFont("DM Mono", 8))
            lbl.setStyleSheet(
                f"color: {COLORS['text_muted']}; letter-spacing: 1.5px; {_LABEL_BASE}")
            self._layout.addWidget(lbl)

    def add(self, widget):
        self._layout.addWidget(widget)
        return widget

    def add_row(self, *widgets):
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        for w in widgets:
            row.addWidget(w)
        row.addStretch()
        self._layout.addLayout(row)
        return row


def _checkbox(text: str, tooltip: str = "") -> QCheckBox:
    cb = QCheckBox(text)
    cb.setFont(QFont("DM Sans", 10))
    cb.setStyleSheet(f"color: {COLORS['text']};")
    if tooltip:
        cb.setToolTip(tooltip)
    return cb


def _grey_out(cb: QCheckBox, reason: str):
    """Force a checkbox OFF and disable it, with a tooltip explaining why."""
    cb.setChecked(False)
    cb.setEnabled(False)
    cb.setToolTip(reason)
    cb.setStyleSheet(f"color: {COLORS['text_muted']};")


class LaunchOptionsSection(QWidget):
    """
    Full Launch Options form. `changed` fires on any widget edit so a host
    dialog can e.g. enable its own Apply/Save button.
    """
    changed = pyqtSignal()
    # Fired specifically after a confirmed Reset to Defaults — separate from
    # `changed` so a host dialog can show a distinct "reset — click Apply to
    # save" message rather than its generic dirty-state handling. Reset
    # wipes the DB row immediately (per spec), but the on-disk launch_cmd/
    # .desktop is only rewritten when the host's own Apply flow runs
    # (it already reads fresh options via launch_options.get_effective_options
    # every time it calls installer.regenerate_launch()).
    reset_done = pyqtSignal()

    def __init__(self, game_id: int, parent=None):
        super().__init__(parent)
        self._game_id = game_id
        self._is_proton = True   # caller should call set_is_proton() right after construction

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        effective = lo.get_effective_options(game_id)

        # ── Renderer group (mutually exclusive) ─────────────────────────────
        renderer_box = _MiniGroup("Renderer")
        self.dxvk_cb = _checkbox(
            "DXVK",
            "Translates DirectX 9/10/11 to Vulkan. Improves performance for "
            "most Windows games. Do not use alongside VKD3D.")
        self.vkd3d_cb = _checkbox(
            "VKD3D-Proton",
            "Translates DirectX 12 to Vulkan. Required for DX12 games. "
            "Do not use alongside DXVK.")
        self._vkd3d_base_text = "VKD3D-Proton"
        self.dxvk_cb.toggled.connect(lambda v: v and self.vkd3d_cb.setChecked(False))
        self.vkd3d_cb.toggled.connect(lambda v: v and self.dxvk_cb.setChecked(False))
        renderer_box.add_row(self.dxvk_cb, self.vkd3d_cb)
        root.addWidget(renderer_box)

        # ── Overlay group (mutually exclusive) ──────────────────────────────
        overlay_box = _MiniGroup("Performance Overlay")
        self.dxvk_hud_cb = _checkbox(
            "DXVK_HUD",
            "Shows a lightweight performance overlay in-game (FPS, GPU load, "
            "VRAM). Built into DXVK. Only works when DXVK is also enabled.")
        self.mangohud_cb = _checkbox(
            "MangoHud",
            "Shows a detailed performance overlay in-game (FPS, temperatures, "
            "frame time, VRAM). Requires MangoHud to be installed on your system.")
        self.dxvk_hud_cb.toggled.connect(lambda v: v and self.mangohud_cb.setChecked(False))
        self.mangohud_cb.toggled.connect(lambda v: v and self.dxvk_hud_cb.setChecked(False))
        if not lo.mangohud_installed():
            _grey_out(self.mangohud_cb, "MangoHud is not installed on this system.")
        overlay_box.add_row(self.dxvk_hud_cb, self.mangohud_cb)
        root.addWidget(overlay_box)

        # ── GameMode ─────────────────────────────────────────────────────────
        gamemode_box = _MiniGroup()
        self.gamemode_cb = _checkbox(
            "GameMode",
            "Tells the system to prioritize this game's CPU usage while it's "
            "running. Safe to leave on for all games — has no downsides.")
        if not lo.gamemode_installed():
            _grey_out(self.gamemode_cb, "GameMode is not installed on this system.")
        gamemode_box.add(self.gamemode_cb)
        root.addWidget(gamemode_box)

        # ── FSR ──────────────────────────────────────────────────────────────
        fsr_box = _MiniGroup("FSR")
        self.fsr_cb = _checkbox(
            "Enable FSR",
            "Renders the game at a lower resolution and upscales it to your "
            "screen resolution, improving performance. Works on AMD, Nvidia, "
            "and Intel Arc GPUs. Only effective when the game runs in "
            "fullscreen at a resolution lower than your display's native "
            "resolution.")
        sharp_lbl = _label("Sharpness")
        self.fsr_sharpness = QSlider(Qt.Orientation.Horizontal)
        self.fsr_sharpness.setRange(0, 5)
        self.fsr_sharpness.setFixedWidth(120)
        self.fsr_sharpness_value_lbl = _label("2", muted=True)
        self.fsr_sharpness_value_lbl.setFixedWidth(14)
        self.fsr_sharpness.valueChanged.connect(
            lambda v: self.fsr_sharpness_value_lbl.setText(str(v)))
        fsr_box.add_row(self.fsr_cb)
        fsr_box.add_row(sharp_lbl, self.fsr_sharpness, self.fsr_sharpness_value_lbl)
        root.addWidget(fsr_box)

        # ── Sync ─────────────────────────────────────────────────────────────
        sync_box = _MiniGroup()
        self.disable_sync_cb = _checkbox(
            "Disable FSync/ESync (compatibility fix)",
            "FSync and ESync reduce CPU overhead while the game is running. "
            "They are enabled by default and improve performance on modern "
            "systems. Only disable this if a game crashes or has issues — "
            "it is a compatibility fix, not a performance improvement.")
        sync_box.add(self.disable_sync_cb)
        root.addWidget(sync_box)

        # ── Audio latency ────────────────────────────────────────────────────
        audio_box = _MiniGroup("Audio Latency Fix")
        self.audio_fix_cb = _checkbox(
            "Enable audio latency fix",
            "Fixes crackling or choppy audio in some games by adding a small "
            "audio buffer delay. Only enable this if you are experiencing "
            "audio problems — it is not something you should use by default.")
        ms_lbl = _label("Latency (ms)")
        self.audio_latency_ms = QSpinBox()
        self.audio_latency_ms.setRange(1, 1000)
        self.audio_latency_ms.setFixedWidth(64)
        audio_box.add_row(self.audio_fix_cb)
        audio_box.add_row(ms_lbl, self.audio_latency_ms)
        root.addWidget(audio_box)

        # ── GPU selection (auto-detected, hidden unless relevant) ──────────────
        self.nvidia_prime_cb = None
        self.dri_prime_cb    = None
        dual_nvidia = lo.has_dual_gpu_nvidia()
        dual_amd_intel = lo.has_dual_gpu_amd_intel()
        if dual_nvidia or dual_amd_intel:
            gpu_box = _MiniGroup("GPU Selection")
            if dual_nvidia:
                self.nvidia_prime_cb = _checkbox(
                    "Force Dedicated Nvidia GPU",
                    "Forces the game to run on your dedicated Nvidia GPU "
                    "instead of the integrated graphics chip. Only relevant "
                    "if your system has two GPUs.")
                gpu_box.add(self.nvidia_prime_cb)
            if dual_amd_intel:
                self.dri_prime_cb = _checkbox(
                    "Force Dedicated AMD GPU",
                    "Forces the game to run on your dedicated AMD GPU instead "
                    "of the integrated Intel graphics chip.")
                gpu_box.add(self.dri_prime_cb)
            root.addWidget(gpu_box)

        # ── Proton-only options (hidden entirely for plain Wine) ───────────────
        self.proton_only_box = _MiniGroup("Proton-Only Options")
        self.large_addr_cb = _checkbox(
            "Large Address Aware",
            "Allows 32-bit games to access more than 2GB of RAM. Only useful "
            "for older 32-bit games that run out of memory.")
        self.wined3d_cb = _checkbox(
            "Use Software D3D (disable DXVK)",
            "Forces the game to use Wine's built-in DirectX translation "
            "instead of DXVK. Use this as a compatibility fallback if a game "
            "crashes or has graphical issues with DXVK enabled.")
        self.proton_only_box.add(self.large_addr_cb)
        self.proton_only_box.add(self.wined3d_cb)
        root.addWidget(self.proton_only_box)

        # ── Engine-Detected Game Arguments ──────────────────────────────────────
        # Only shown once set_engine() is called with a recognized engine
        # (see launch_options.normalize_engine/ENGINE_ARG_MATRIX) — hidden
        # entirely otherwise, per spec. Rebuilt (not just re-toggled) each
        # time set_engine() runs, since a different engine has a different
        # applicable flag set. metadata doesn't store an IGDB engine field
        # yet (see installer._game_engine()'s docstring), so in practice
        # this stays empty/hidden today — it activates automatically the
        # moment a caller has real engine data to pass to set_engine().
        self._engine: str | None = None
        self._effective_engine_args: dict = {}
        self.engine_checks: dict[str, QCheckBox] = {}
        self.engine_box = _MiniGroup("Engine Arguments")
        self.engine_box.setVisible(False)
        root.addWidget(self.engine_box)

        # ── Free-text fields ─────────────────────────────────────────────────
        text_box = _MiniGroup("Advanced")
        env_lbl = _label("Environment Variables", muted=True)
        env_lbl.setToolTip(
            "Advanced: add environment variables that are set before the "
            "game launches. Format: KEY=VALUE KEY2=VALUE2.")
        self.env_vars_edit = QLineEdit()
        self.env_vars_edit.setFont(QFont("DM Mono", 10))
        self.env_vars_edit.setPlaceholderText("KEY=VALUE KEY2=VALUE2")
        self.env_vars_edit.setStyleSheet(f"color: {COLORS['accent2']};")
        text_box.add(env_lbl)
        text_box.add(self.env_vars_edit)

        args_lbl = _label("Launch Arguments", muted=True)
        args_lbl.setToolTip(
            "Advanced: add command-line arguments passed directly to the "
            "game. Check the game's documentation for supported arguments.")
        self.launch_args_edit = QLineEdit()
        self.launch_args_edit.setFont(QFont("DM Mono", 10))
        self.launch_args_edit.setPlaceholderText("-arg1 -arg2")
        self.launch_args_edit.setStyleSheet(f"color: {COLORS['accent2']};")
        text_box.add(args_lbl)
        text_box.add(self.launch_args_edit)
        root.addWidget(text_box)

        # ── Reset to Defaults ────────────────────────────────────────────────
        reset_row = QHBoxLayout()
        reset_row.setContentsMargins(0, 4, 0, 0)
        reset_row.addStretch()
        self.reset_btn = QPushButton("Reset to Defaults")
        self.reset_btn.setFont(QFont("DM Sans", 10))
        self.reset_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                border: 1px solid rgba(248,113,113,0.3);
                border-radius: 6px;
                color: {COLORS['danger']};
                padding: 5px 12px;
            }}
            QPushButton:hover {{
                background: rgba(248,113,113,0.08);
            }}
        """)
        self.reset_btn.clicked.connect(self._on_reset_clicked)
        reset_row.addWidget(self.reset_btn)
        root.addLayout(reset_row)

        self.load(effective)
        self._wire_change_signals()

    # ── Wiring ────────────────────────────────────────────────────────────────

    def _wire_change_signals(self):
        checks = [
            self.dxvk_cb, self.vkd3d_cb, self.dxvk_hud_cb, self.mangohud_cb,
            self.gamemode_cb, self.fsr_cb, self.disable_sync_cb,
            self.audio_fix_cb, self.large_addr_cb, self.wined3d_cb,
        ]
        for cb in (self.nvidia_prime_cb, self.dri_prime_cb):
            if cb is not None:
                checks.append(cb)
        for cb in checks:
            cb.toggled.connect(self.changed)
        self.fsr_sharpness.valueChanged.connect(self.changed)
        self.audio_latency_ms.valueChanged.connect(self.changed)
        self.env_vars_edit.textChanged.connect(self.changed)
        self.launch_args_edit.textChanged.connect(self.changed)

    # ── Public API ────────────────────────────────────────────────────────────

    def set_is_proton(self, is_proton: bool):
        """Call whenever the host dialog's Proton/Wine selection changes."""
        self._is_proton = is_proton
        self.proton_only_box.setVisible(is_proton)

    def set_engine(self, engine_raw: str | None):
        """
        Call with the game's raw engine string (e.g. IGDB's engine name,
        once metadata stores one — see installer._game_engine()) whenever
        it's known. Normalizes it, rebuilds the Engine Arguments group for
        whatever flags apply to that engine (restoring any previously
        stored checked state from the last load()'d effective dict), and
        adds "(Recommended)" to VKD3D-Proton's label when the engine's
        flag set includes -dx12.
        """
        self._engine = lo.normalize_engine(engine_raw)
        applicable = lo.engine_args_for(self._engine)

        self.vkd3d_cb.setText(
            f"{self._vkd3d_base_text} (Recommended)"
            if "-dx12" in applicable else self._vkd3d_base_text)

        # Merge any live edits from the currently-built checkboxes into the
        # cache BEFORE wiping them, so switching engines mid-session (e.g.
        # a future "override detected engine" control) never silently
        # drops a flag the user just checked for a shared flag name.
        self._effective_engine_args.update(
            {flag: cb.isChecked() for flag, cb in self.engine_checks.items()})

        # Rebuild the checkbox list from scratch — different engines have
        # different applicable flags, so stale checkboxes from a previous
        # engine must not linger.
        while self.engine_box._layout.count() > 1:  # keep the title label
            item = self.engine_box._layout.takeAt(1)
            if item.widget():
                item.widget().deleteLater()
        self.engine_checks.clear()

        if not applicable:
            self.engine_box.setVisible(False)
            return

        stored = self._effective_engine_args
        row_widgets = []
        for flag in applicable:
            cb = _checkbox(flag)
            cb.setChecked(bool(stored.get(flag)))
            cb.toggled.connect(self.changed)
            self.engine_checks[flag] = cb
            row_widgets.append(cb)
            if len(row_widgets) == 3:
                self.engine_box.add_row(*row_widgets)
                row_widgets = []
        if row_widgets:
            self.engine_box.add_row(*row_widgets)
        self.engine_box.setVisible(True)

    def load(self, effective: dict):
        """Push an effective-options dict into the widgets."""
        self.dxvk_cb.setChecked(bool(effective.get("dxvk")))
        self.vkd3d_cb.setChecked(bool(effective.get("vkd3d")))
        self.dxvk_hud_cb.setChecked(bool(effective.get("dxvk_hud")))
        if self.mangohud_cb.isEnabled():
            self.mangohud_cb.setChecked(bool(effective.get("mangohud")))
        if self.gamemode_cb.isEnabled():
            self.gamemode_cb.setChecked(bool(effective.get("gamemode")))
        self.fsr_cb.setChecked(bool(effective.get("fsr")))
        self.fsr_sharpness.setValue(int(effective.get("fsr_sharpness", 2)))
        self.disable_sync_cb.setChecked(bool(effective.get("disable_sync")))
        self.audio_fix_cb.setChecked(bool(effective.get("audio_latency_fix")))
        self.audio_latency_ms.setValue(int(effective.get("audio_latency_ms", 60)))
        if self.nvidia_prime_cb is not None:
            self.nvidia_prime_cb.setChecked(bool(effective.get("nvidia_prime")))
        if self.dri_prime_cb is not None:
            self.dri_prime_cb.setChecked(bool(effective.get("dri_prime")))
        self.large_addr_cb.setChecked(bool(effective.get("large_address_aware")))
        self.wined3d_cb.setChecked(bool(effective.get("use_wined3d")))
        self.env_vars_edit.setText(effective.get("env_vars", "") or "")
        self.launch_args_edit.setText(effective.get("launch_args", "") or "")

        # Cached so set_engine() can restore checked state onto whatever
        # flag checkboxes it (re)builds — engine_checks may not exist yet
        # (or may belong to a different engine) at the moment load() runs.
        self._effective_engine_args = dict(effective.get("engine_args") or {})
        for flag, cb in self.engine_checks.items():
            cb.setChecked(bool(self._effective_engine_args.get(flag)))

    def collect(self) -> dict:
        """Read current widget state into an effective-options-shaped dict."""
        return {
            "dxvk":                 self.dxvk_cb.isChecked(),
            "vkd3d":                self.vkd3d_cb.isChecked(),
            "dxvk_hud":              self.dxvk_hud_cb.isChecked(),
            "mangohud":              self.mangohud_cb.isChecked(),
            "gamemode":              self.gamemode_cb.isChecked(),
            "fsr":                   self.fsr_cb.isChecked(),
            "fsr_sharpness":         self.fsr_sharpness.value(),
            "disable_sync":          self.disable_sync_cb.isChecked(),
            "audio_latency_fix":     self.audio_fix_cb.isChecked(),
            "audio_latency_ms":      self.audio_latency_ms.value(),
            "nvidia_prime":          self.nvidia_prime_cb.isChecked() if self.nvidia_prime_cb else False,
            "dri_prime":             self.dri_prime_cb.isChecked() if self.dri_prime_cb else False,
            "large_address_aware":   self.large_addr_cb.isChecked(),
            "use_wined3d":           self.wined3d_cb.isChecked(),
            "engine_args":           {k: cb.isChecked() for k, cb in self.engine_checks.items()},
            "env_vars":              self.env_vars_edit.text().strip(),
            "launch_args":           self.launch_args_edit.text().strip(),
        }

    def save(self):
        """Persist the current widget state as this game's overrides."""
        lo.save_effective_options(self._game_id, self.collect())

    def _on_reset_clicked(self):
        confirm = QMessageBox(self)
        confirm.setWindowTitle("Reset Launch Options")
        confirm.setText("Reset all launch options to defaults?")
        confirm.setInformativeText(
            "This clears every custom launch option for this game — "
            "renderer, overlay, sync, GPU, and any custom arguments. "
            "This cannot be undone.")
        confirm.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        confirm.setDefaultButton(QMessageBox.StandardButton.Cancel)
        confirm.setStyleSheet(
            f"QMessageBox {{ background: {COLORS['surface']}; color: {COLORS['text']}; }}")
        if confirm.exec() != QMessageBox.StandardButton.Yes:
            return
        lo.reset_to_defaults(self._game_id)
        self.load(lo.resolve_defaults())
        self.changed.emit()
        self.reset_done.emit()
