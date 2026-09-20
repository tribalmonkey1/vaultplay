"""
controller.py — Gamepad input for VaultPlay (Controller Support, Phase A)

Spec: Notion → Features → Fully Planned → Controller Support.

Pure input layer — no widgets, no knowledge of the UI. ControllerWatcher is a
QThread that owns the SDL2 GameController loop and talks to the rest of the
app only through pyqtSignals, the same isolation PlaytimeWatcher gives
wineserver/proc.wait(). MainWindow decides what each signal means (see its
"Controller Support" section).

Uses pysdl2 (SDL_GameController), which normalizes Xbox / PlayStation /
Switch Pro / generic pads via SDL's gamecontrollerdb mappings. If pysdl2 or
libSDL2 isn't installed, is_available() returns False and the app runs
exactly as before — controller support is purely additive.

Signals
-------
  direction(str)           "up" | "down" | "left" | "right"  (D-pad or left
                           stick; fires on press, then repeats — 400ms
                           initial hold delay, 150ms interval)
  activate()               A / Cross
  back()                   B / Circle
  context_menu()           Y / Triangle
  page_prev() / page_next()  Left / Right bumper (MainWindow: sidebar/content)
  open_settings()          Start
  focus_search()           Back / Select
  controller_connected(str)  controller name (emitted when the first pad appears)
  controller_disconnected()  emitted when the last pad is removed

Ownership: the first controller to send input claims navigation; a second,
idle pad never steals it. Ownership is released only when the owning
controller disconnects.
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
# ─────────────────────────────────────────────────────────────────────────────

import ctypes
import logging
import time
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal

log = logging.getLogger(__name__)

try:
    import sdl2
    _IMPORT_ERROR: Optional[Exception] = None
except Exception as _e:   # ImportError, or pysdl2 failing to locate libSDL2
    sdl2 = None
    _IMPORT_ERROR = _e

STICK_DEADZONE       = 8000    # SDL2's own default (~8000/32767)
REPEAT_INITIAL_DELAY = 0.400   # seconds held before repeating starts
REPEAT_INTERVAL      = 0.150   # seconds between repeats
POLL_INTERVAL        = 0.016   # ~60Hz


def is_available() -> bool:
    """True if pysdl2 imported. (libSDL2 problems surface in run(), which
    logs and exits quietly.)"""
    return sdl2 is not None


class ControllerWatcher(QThread):
    direction               = pyqtSignal(str)
    activate                = pyqtSignal()
    back                    = pyqtSignal()
    context_menu            = pyqtSignal()
    page_prev               = pyqtSignal()
    page_next               = pyqtSignal()
    open_settings           = pyqtSignal()
    focus_search            = pyqtSignal()
    controller_connected    = pyqtSignal(str)
    controller_disconnected = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._running = True
        self._controllers: dict = {}          # instance_id -> (SDL pointer, name)
        self._active_id: Optional[int] = None  # controller that owns navigation
        self._held_dir: Optional[str] = None
        self._next_repeat = 0.0

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def stop(self, timeout_ms: int = 2000):
        self._running = False
        if not self.wait(timeout_ms):
            log.warning("[CONTROLLER] Watcher thread did not stop within %d ms", timeout_ms)

    def run(self):
        if sdl2 is None:
            log.info("[CONTROLLER] pysdl2 unavailable (%s) — watcher not running",
                     _IMPORT_ERROR)
            return
        try:
            # Keep receiving pad events even when no SDL window has focus
            # (we have none) — MainWindow gates on its own active state.
            sdl2.SDL_SetHint(b"SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", b"1")
            if sdl2.SDL_Init(sdl2.SDL_INIT_GAMECONTROLLER) != 0:
                log.warning("[CONTROLLER] SDL_Init failed: %s",
                            sdl2.SDL_GetError().decode(errors="replace"))
                return
        except Exception as e:
            log.warning("[CONTROLLER] Could not initialise SDL2: %s", e)
            return

        try:
            for i in range(sdl2.SDL_NumJoysticks()):
                self._on_added(i)
            self._loop()
        except Exception:
            log.exception("[CONTROLLER] Watcher loop crashed")
        finally:
            for ctrl, _name in list(self._controllers.values()):
                try:
                    sdl2.SDL_GameControllerClose(ctrl)
                except Exception:
                    pass
            self._controllers.clear()
            try:
                sdl2.SDL_QuitSubSystem(sdl2.SDL_INIT_GAMECONTROLLER)
            except Exception:
                pass

    # ── Main loop ─────────────────────────────────────────────────────────

    def _loop(self):
        event = sdl2.SDL_Event()
        while self._running:
            while sdl2.SDL_PollEvent(ctypes.byref(event)):
                self._handle_event(event)
            self._update_direction()
            time.sleep(POLL_INTERVAL)

    def _handle_event(self, event):
        et = event.type
        if et == sdl2.SDL_CONTROLLERDEVICEADDED:
            self._on_added(event.cdevice.which)          # device index
        elif et == sdl2.SDL_CONTROLLERDEVICEREMOVED:
            self._on_removed(event.cdevice.which)        # instance id
        elif et == sdl2.SDL_CONTROLLERBUTTONDOWN:
            self._on_button(event.cbutton.which, event.cbutton.button)
        elif et == sdl2.SDL_CONTROLLERAXISMOTION:
            if abs(event.caxis.value) > STICK_DEADZONE:
                self._owns(event.caxis.which)            # claim on real stick input

    # ── Device add / remove ───────────────────────────────────────────────

    def _on_added(self, device_index: int):
        try:
            if not sdl2.SDL_IsGameController(device_index):
                return
            try:
                iid = sdl2.SDL_JoystickGetDeviceInstanceID(device_index)
                if iid in self._controllers:
                    return   # SDL emits ADDED for pads already present at init
            except Exception:
                pass
            ctrl = sdl2.SDL_GameControllerOpen(device_index)
            if not ctrl:
                log.warning("[CONTROLLER] Could not open device %d: %s", device_index,
                            sdl2.SDL_GetError().decode(errors="replace"))
                return
            iid = sdl2.SDL_JoystickInstanceID(sdl2.SDL_GameControllerGetJoystick(ctrl))
            if iid in self._controllers:
                sdl2.SDL_GameControllerClose(ctrl)
                return
            raw = sdl2.SDL_GameControllerName(ctrl)
            name = raw.decode(errors="replace") if raw else "Controller"
            was_empty = not self._controllers
            self._controllers[iid] = (ctrl, name)
            log.info("[CONTROLLER] Opened '%s' (instance %s)", name, iid)
            if was_empty:
                self.controller_connected.emit(name)
        except Exception as e:
            log.warning("[CONTROLLER] Error adding device %s: %s", device_index, e)

    def _on_removed(self, instance_id: int):
        entry = self._controllers.pop(instance_id, None)
        if entry is None:
            return
        try:
            sdl2.SDL_GameControllerClose(entry[0])
        except Exception:
            pass
        log.info("[CONTROLLER] Removed '%s'", entry[1])
        if self._active_id == instance_id:
            self._active_id = None
            self._held_dir = None
        if not self._controllers:
            self.controller_disconnected.emit()

    # ── Input ─────────────────────────────────────────────────────────────

    def _owns(self, instance_id: int) -> bool:
        """First controller to send input claims navigation; others are ignored."""
        if instance_id not in self._controllers:
            return False
        if self._active_id is None:
            self._active_id = instance_id
        return instance_id == self._active_id

    def _on_button(self, instance_id: int, button: int):
        if not self._owns(instance_id):
            return
        S = sdl2
        if button == S.SDL_CONTROLLER_BUTTON_A:
            self.activate.emit()
        elif button == S.SDL_CONTROLLER_BUTTON_B:
            self.back.emit()
        elif button == S.SDL_CONTROLLER_BUTTON_Y:
            self.context_menu.emit()
        elif button == S.SDL_CONTROLLER_BUTTON_LEFTSHOULDER:
            self.page_prev.emit()
        elif button == S.SDL_CONTROLLER_BUTTON_RIGHTSHOULDER:
            self.page_next.emit()
        elif button == S.SDL_CONTROLLER_BUTTON_START:
            self.open_settings.emit()
        elif button == S.SDL_CONTROLLER_BUTTON_BACK:
            self.focus_search.emit()
        # D-pad buttons are handled by state polling in _update_direction()

    def _read_direction(self, ctrl) -> Optional[str]:
        S = sdl2
        get_btn = S.SDL_GameControllerGetButton
        if get_btn(ctrl, S.SDL_CONTROLLER_BUTTON_DPAD_UP):
            return "up"
        if get_btn(ctrl, S.SDL_CONTROLLER_BUTTON_DPAD_DOWN):
            return "down"
        if get_btn(ctrl, S.SDL_CONTROLLER_BUTTON_DPAD_LEFT):
            return "left"
        if get_btn(ctrl, S.SDL_CONTROLLER_BUTTON_DPAD_RIGHT):
            return "right"
        x = S.SDL_GameControllerGetAxis(ctrl, S.SDL_CONTROLLER_AXIS_LEFTX)
        y = S.SDL_GameControllerGetAxis(ctrl, S.SDL_CONTROLLER_AXIS_LEFTY)
        if max(abs(x), abs(y)) < STICK_DEADZONE:
            return None
        if abs(x) > abs(y):
            return "right" if x > 0 else "left"
        return "down" if y > 0 else "up"      # SDL: +Y is down

    def _update_direction(self):
        entry = self._controllers.get(self._active_id) if self._active_id is not None else None
        current = self._read_direction(entry[0]) if entry else None
        now = time.monotonic()
        if current != self._held_dir:
            self._held_dir = current
            if current is not None:
                self.direction.emit(current)
                self._next_repeat = now + REPEAT_INITIAL_DELAY
        elif current is not None and now >= self._next_repeat:
            self.direction.emit(current)
            self._next_repeat = now + REPEAT_INTERVAL
