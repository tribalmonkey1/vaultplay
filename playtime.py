"""
playtime.py — Background playtime tracking for VaultPlay

Tracks how long a game session lasts and records it to game_state.playtime_minutes.

Design
------
PlaytimeWatcher is a QThread started immediately after a successful game launch.
It blocks on a single wait call appropriate for the Wine variant in use:

  Plain Wine / Wine-GE (wine, wine64, wine-ge-*):
      Runs `wineserver --wait` as a subprocess with WINEPREFIX set.
      wineserver blocks until every process in the prefix has exited —
      meaning it naturally handles multi-process games and the case where
      the Wine launcher wrapper exits immediately after spawning the game.

  Proton (any binary named "proton"):
      Calls proc.wait() on the original Popen handle.
      The Proton wrapper stays alive as a parent process for the full session,
      so proc.wait() is reliable here.
      wineserver --wait is NOT used for Proton because Proton's wineserver
      lives inside the pressure-vessel container and is not accessible via
      the system wineserver binary.

Safety cap
----------
Both strategies use a 24-hour (86400s) timeout. If the wait exceeds this,
the session is ended and whatever time elapsed is recorded. This prevents
the watcher from running forever if something goes wrong.

Minimum session length
----------------------
Sessions under MIN_SESSION_MINUTES (2 minutes) are discarded entirely.
This filters out accidental double-clicks, launcher windows that close
immediately, and crashes at startup.

Usage
-----
  proc = subprocess.Popen(cmd, env=env)
  watcher = PlaytimeWatcher(
      game_id    = game_id,
      proc       = proc,
      wine_bin   = wine_bin,           # full path or "wine"
      wine_prefix = str(prefix_path),  # WINEPREFIX value
  )
  watcher.session_ended.connect(on_session_ended)
  watcher.start()

  # on_session_ended(game_id: int, minutes: int)
  # minutes == 0 means session was under MIN_SESSION_MINUTES (not recorded)
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

import datetime
import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal

import db

log = logging.getLogger(__name__)

MIN_SESSION_MINUTES: int = 2        # sessions shorter than this are discarded
MAX_SESSION_SECONDS: int = 86400    # 24-hour safety cap


def is_proton(wine_bin: str) -> bool:
    """Return True if wine_bin is a Proton script (not plain Wine/Wine-GE)."""
    return Path(wine_bin).name == "proton"


class PlaytimeWatcher(QThread):
    """
    Background thread that tracks a single game session.

    Signals
    -------
    session_ended(game_id, minutes_recorded)
        Emitted when the session ends.
        minutes_recorded == 0 means the session was under MIN_SESSION_MINUTES
        and was NOT written to the database.
        minutes_recorded > 0 means the session was recorded via db.add_playtime.
    """
    session_ended = pyqtSignal(int, int)   # game_id, minutes_recorded

    def __init__(self, game_id: int, proc: subprocess.Popen,
                 wine_bin: str, wine_prefix: str, parent=None):
        super().__init__(parent)
        self._game_id     = game_id
        self._proc        = proc
        self._wine_bin    = wine_bin
        self._wine_prefix = wine_prefix
        # Daemon so it doesn't block app exit if still running
        self.setTerminationEnabled(True)

    def run(self):
        start = datetime.datetime.utcnow()
        log.info("[PLAYTIME] Session started: game_id=%d  wine_bin=%s  prefix=%s",
                 self._game_id, self._wine_bin, self._wine_prefix)

        if is_proton(self._wine_bin):
            self._wait_proton()
        else:
            self._wait_wine()

        end     = datetime.datetime.utcnow()
        elapsed = (end - start).total_seconds()
        minutes = int(elapsed / 60)

        log.info("[PLAYTIME] Session ended: game_id=%d  elapsed=%.1fs  minutes=%d",
                 self._game_id, elapsed, minutes)

        if minutes < MIN_SESSION_MINUTES:
            log.info("[PLAYTIME] Session too short (%d min < %d min threshold) — not recorded",
                     minutes, MIN_SESSION_MINUTES)
            self.session_ended.emit(self._game_id, 0)
            return

        try:
            db.add_playtime(self._game_id, minutes)
            log.info("[PLAYTIME] Recorded %d minutes for game_id=%d",
                     minutes, self._game_id)
        except Exception as e:
            log.error("[PLAYTIME] Failed to record playtime for game_id=%d: %s",
                      self._game_id, e)

        self.session_ended.emit(self._game_id, minutes)

    def _wait_wine(self):
        """
        Block until all Wine processes in the prefix have exited, using
        `wineserver --wait`. Falls back to proc.wait() if wineserver is
        not available or if the prefix path is not set.
        """
        if not self._wine_prefix:
            log.warning("[PLAYTIME] No wine_prefix — falling back to proc.wait()")
            self._wait_proc()
            return

        try:
            env = {**os.environ, "WINEPREFIX": self._wine_prefix}
            result = subprocess.run(
                ["wineserver", "--wait"],
                env=env,
                timeout=MAX_SESSION_SECONDS,
            )
            log.debug("[PLAYTIME] wineserver --wait exited with code %d",
                      result.returncode)
        except FileNotFoundError:
            log.warning("[PLAYTIME] wineserver not found — falling back to proc.wait()")
            self._wait_proc()
        except subprocess.TimeoutExpired:
            log.warning("[PLAYTIME] wineserver --wait hit 24h safety cap — ending session")
        except Exception as e:
            log.warning("[PLAYTIME] wineserver --wait error: %s — falling back to proc.wait()", e)
            self._wait_proc()

    def _wait_proton(self):
        """
        Block until the Proton wrapper process exits using proc.wait().
        The Proton wrapper stays alive for the full session, so this is reliable.
        """
        log.debug("[PLAYTIME] Using proc.wait() for Proton session")
        self._wait_proc()

    def _wait_proc(self):
        """
        Fall back: wait on the original Popen handle with a 24-hour cap.
        This is reliable for Proton and acceptable as a fallback for Wine
        (though Wine launchers sometimes exit early — prefer wineserver --wait).
        """
        try:
            self._proc.wait(timeout=MAX_SESSION_SECONDS)
            log.debug("[PLAYTIME] proc.wait() returned, returncode=%s",
                      self._proc.returncode)
        except subprocess.TimeoutExpired:
            log.warning("[PLAYTIME] proc.wait() hit 24h safety cap — ending session")
            try:
                self._proc.kill()
            except Exception:
                pass
        except Exception as e:
            log.warning("[PLAYTIME] proc.wait() error: %s", e)


def format_playtime(minutes: int) -> str:
    """
    Format a total playtime in minutes as a human-readable string.

    Examples:
      0   → "No playtime recorded"
      45  → "45 minutes"
      60  → "1 hour"
      90  → "1 hour 30 minutes"
      120 → "2 hours"
      1445 → "1 day 0 hours"  (over 24h shown in days)
    """
    if minutes <= 0:
        return "No playtime recorded"
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = minutes // 60
    remainder = minutes % 60
    if hours >= 24:
        days  = hours // 24
        hours = hours % 24
        return f"{days} day{'s' if days != 1 else ''} {hours} hour{'s' if hours != 1 else ''}"
    if remainder == 0:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return (f"{hours} hour{'s' if hours != 1 else ''} "
            f"{remainder} minute{'s' if remainder != 1 else ''}")
