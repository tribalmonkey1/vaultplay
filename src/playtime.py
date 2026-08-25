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

      wineserver --wait waits on a wineserver that's ALREADY running for
      the prefix — it doesn't start one. Because PlaytimeWatcher.start()
      fires right after the game's Popen call, the very first call can
      race ahead of Wine actually spinning up its wineserver for this
      session (connecting to X, loading DLLs, etc. all take real time),
      seeing nothing running yet and returning success in milliseconds.
      This is caught by cross-checking self._proc.poll(): if wineserver
      claims done while the process we actually launched is still alive,
      that's the startup race, not a real session end, and the wait is
      retried — see _wait_wine().

  Proton (any binary named "proton"):
      Calls proc.wait() on the original Popen handle, THEN polls /proc for
      any remaining process whose environment shows STEAM_COMPAT_DATA_PATH
      matching this session's wine_prefix, blocking until none remain.
      The second step exists because proc.wait() alone is NOT reliable —
      some games (e.g. a launcher.exe stub that spawns the real game.exe
      and exits) have their top-level Proton-launched process exit long
      before the actual game does. Every process spawned under a Proton
      launch inherits STEAM_COMPAT_DATA_PATH from the environment
      installer.py's _build_wine_env() sets, so scanning for it gives the
      same "wait for everything in the prefix" guarantee wineserver --wait
      gives plain Wine, without needing access to Proton's internal
      wineserver (which lives inside the pressure-vessel container and is
      not accessible via the system wineserver binary).

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
import signal
import subprocess
import time
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


# ── Restart-reattachment (Currently Playing Indicator) ────────────────────────
# If VaultPlay is closed and reopened while a game launched via its Play
# button is still running, these helpers let MainWindow find that process on
# /proc and attach a normal PlaytimeWatcher to it — with the correct elapsed
# time already accumulated, rather than restarting the timer from 0.

def _clock_ticks_per_sec() -> int:
    try:
        return os.sysconf("SC_CLK_TCK")
    except (ValueError, AttributeError):
        return 100


def _system_uptime_seconds() -> Optional[float]:
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def proc_start_elapsed_seconds(pid: int) -> Optional[float]:
    """
    Return how many seconds ago process `pid` started, by reading its
    /proc/<pid>/stat starttime field (in clock ticks since boot) and
    comparing against /proc/uptime. Returns None if unreadable — the
    caller should fall back to treating the session as starting "now"
    rather than crashing.

    The process name field in /proc/<pid>/stat is wrapped in parens and
    can itself contain spaces or parens, so this splits on the LAST ')'
    to safely skip past it before splitting the remaining whitespace-
    separated fields (starttime is field 22 overall, i.e. index 19 after
    the 3 fields following the closing paren).
    """
    uptime = _system_uptime_seconds()
    if uptime is None:
        return None
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            raw = f.read().decode(errors="replace")
        after = raw.rsplit(")", 1)[-1].split()
        starttime_ticks = int(after[19])
    except (OSError, ValueError, IndexError):
        return None
    hz = _clock_ticks_per_sec()
    start_seconds_since_boot = starttime_ticks / hz
    elapsed = uptime - start_seconds_since_boot
    return elapsed if elapsed >= 0 else None


def find_running_pid_for_exe(exe_path: str) -> Optional[int]:
    """
    Best-effort scan of /proc for a running process whose cmdline
    references exe_path's filename — Wine/Proton invocations commonly
    carry the translated exe path as a cmdline argument even though the
    top-level process itself is `wine`/`proton`. Returns the first
    matching PID, or None if nothing matches or /proc can't be read.
    Never raises — used at startup where a scan failure should just mean
    "nothing to reattach," not a crash.
    """
    if not exe_path:
        return None
    target_name = Path(exe_path).name.lower()
    if not target_name:
        return None
    try:
        pids = os.listdir("/proc")
    except OSError:
        return None
    for pid_str in pids:
        if not pid_str.isdigit():
            continue
        try:
            with open(f"/proc/{pid_str}/cmdline", "rb") as f:
                raw = f.read()
        except (OSError, PermissionError):
            continue
        if not raw:
            continue
        try:
            cmdline = raw.decode(errors="replace").lower()
        except Exception:
            continue
        if target_name in cmdline:
            return int(pid_str)
    return None


class AttachedProcess:
    """
    Minimal subprocess.Popen-like wrapper around a PID VaultPlay did not
    itself launch — used when reattaching to a game found still running
    on /proc after an app restart (see find_running_pid_for_exe()).
    Exposes just enough of the Popen surface (pid/poll/wait/kill) for
    PlaytimeWatcher to treat it identically to a real Popen handle,
    without needing its own reattachment-specific code path.
    """

    def __init__(self, pid: int):
        self.pid = pid
        self.returncode: Optional[int] = None

    def _alive(self) -> bool:
        try:
            os.kill(self.pid, 0)
            return True
        except OSError:
            return False

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        if not self._alive():
            self.returncode = 0
        return self.returncode

    def wait(self, timeout: Optional[float] = None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while self._alive():
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(cmd=str(self.pid), timeout=timeout)
            time.sleep(1.0)
        self.returncode = 0
        return self.returncode

    def kill(self):
        try:
            os.kill(self.pid, signal.SIGKILL)
        except OSError:
            pass


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

    def __init__(self, game_id: int, proc, wine_bin: str, wine_prefix: str,
                 elapsed_seconds: float = 0.0, parent=None):
        """
        proc: a subprocess.Popen (normal launch) or an AttachedProcess
        (restart-reattachment — see find_running_pid_for_exe()). Both
        expose poll()/wait()/kill(), so nothing below needs to know which
        one it has.

        elapsed_seconds: how much time has already elapsed for this
        session before this watcher started tracking it — non-zero only
        for reattachment, where the game was already running before
        VaultPlay (re)started. 0.0 for a normal fresh launch.
        """
        super().__init__(parent)
        self._game_id         = game_id
        self._proc            = proc
        self._wine_bin        = wine_bin
        self._wine_prefix     = wine_prefix
        self._elapsed_seconds = max(0.0, elapsed_seconds)
        # Daemon so it doesn't block app exit if still running
        self.setTerminationEnabled(True)

    def request_kill(self):
        """
        Force-terminate the tracked process (Cogwheel → Force Quit).
        Does not touch thread/session bookkeeping directly — run() is
        already blocked waiting on this same process, so it detects the
        exit and records/ends the session exactly as it would for a
        normal quit. Safe to call from any thread; never raises.
        """
        try:
            self._proc.kill()
            log.info("[PLAYTIME] Force Quit requested for game_id=%d", self._game_id)
        except Exception as e:
            log.warning("[PLAYTIME] request_kill failed for game_id=%d: %s",
                       self._game_id, e)

    def run(self):
        start = datetime.datetime.utcnow() - datetime.timedelta(
            seconds=self._elapsed_seconds)
        log.info("[PLAYTIME] Session started: game_id=%d  wine_bin=%s  prefix=%s"
                 "  elapsed_at_attach=%.0fs",
                 self._game_id, self._wine_bin, self._wine_prefix,
                 self._elapsed_seconds)

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

        wineserver --wait waits on a wineserver that's ALREADY running for
        this prefix — it does not start one. If this is called right after
        the game's process was launched (which is exactly when
        PlaytimeWatcher.start() fires), it can race ahead of Wine actually
        spinning up its wineserver for this session — connecting to the X
        server, loading DLLs, etc. all take real wall-clock time. In that
        race window, wineserver --wait sees nothing running yet for this
        prefix and returns success in milliseconds, even though the game
        is still starting. Cross-checking self._proc.poll() catches this:
        if wineserver claims nothing is running while the process we
        actually launched is still alive, that's the race, not a real
        session end — retry instead of trusting it.
        """
        if not self._wine_prefix:
            log.warning("[PLAYTIME] No wine_prefix — falling back to proc.wait()")
            self._wait_proc()
            return

        env = {**os.environ, "WINEPREFIX": self._wine_prefix}
        deadline = time.monotonic() + MAX_SESSION_SECONDS
        logged_race = False

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log.warning("[PLAYTIME] wineserver --wait retry loop hit 24h "
                           "safety cap — ending session")
                return
            try:
                t0 = time.monotonic()
                result = subprocess.run(
                    ["wineserver", "--wait"],
                    env=env,
                    timeout=remaining,
                )
                elapsed = time.monotonic() - t0
                log.debug("[PLAYTIME] wineserver --wait exited with code %d after %.2fs",
                          result.returncode, elapsed)
            except FileNotFoundError:
                log.warning("[PLAYTIME] wineserver not found — falling back to proc.wait()")
                self._wait_proc()
                return
            except subprocess.TimeoutExpired:
                log.warning("[PLAYTIME] wineserver --wait hit 24h safety cap — ending session")
                return
            except Exception as e:
                log.warning("[PLAYTIME] wineserver --wait error: %s — falling back to proc.wait()", e)
                self._wait_proc()
                return

            if self._proc.poll() is None:
                # wineserver reported nothing running, but the process we
                # actually launched is still alive — startup race. Retry.
                if not logged_race:
                    log.info("[PLAYTIME] wineserver --wait returned but the launched "
                             "process is still running (likely raced ahead of "
                             "wineserver starting up) — retrying")
                    logged_race = True
                time.sleep(1.0)
                continue

            return

    def _wait_proton(self):
        """
        Block until every process belonging to this Proton session has
        exited — not just the top-level launched process.

        proc.wait() alone is insufficient: some games run a launcher.exe
        stub under Proton that spawns the real game.exe and then exits,
        which would otherwise end the tracked session (and, previously,
        fire the post-play Save Backup prompt) within seconds of launch
        while the actual game keeps running. See module docstring.
        """
        log.debug("[PLAYTIME] Using proc.wait() for Proton session")
        self._wait_proc()
        self._wait_for_prefix_processes_to_exit()

    def _wait_for_prefix_processes_to_exit(self, poll_interval: float = 2.0):
        """
        Poll /proc for any process whose environment shows
        STEAM_COMPAT_DATA_PATH matching this session's wine_prefix, and
        block until none remain (or the 24h safety cap is hit). This is
        the Proton equivalent of _wait_wine()'s `wineserver --wait` — see
        module docstring for why it's necessary and why it's reliable.
        No-op if wine_prefix wasn't provided (nothing to match against).
        """
        if not self._wine_prefix:
            return
        target   = self._wine_prefix.rstrip("/")
        deadline = time.monotonic() + MAX_SESSION_SECONDS

        # Give a just-exited top-level process a brief moment to have its
        # spawned child actually appear in /proc before the first check —
        # avoids a race where we check in the split second between the
        # launcher exiting and the real game.exe process existing.
        time.sleep(1.0)

        logged_continuation = False
        while time.monotonic() < deadline:
            if not self._any_process_in_prefix(target):
                return
            if not logged_continuation:
                log.info("[PLAYTIME] Top-level process exited but other "
                         "process(es) still running in prefix %s — "
                         "continuing session", target)
                logged_continuation = True
            time.sleep(poll_interval)

        log.warning("[PLAYTIME] Proton prefix process check hit 24h safety "
                    "cap — ending session")

    @staticmethod
    def _any_process_in_prefix(target_prefix: str) -> bool:
        """
        True if any currently-running process has STEAM_COMPAT_DATA_PATH
        equal to target_prefix in its environment. Reading another
        process's /proc/<pid>/environ requires matching UID, which holds
        here since every Wine/Proton/Steam process runs as the same user.
        Silently skips any PID it can't read (permission errors, the
        process exiting mid-scan, zombies with empty environ) rather than
        treating a read failure as "still running".
        """
        try:
            pids = os.listdir("/proc")
        except OSError:
            return False
        for pid_str in pids:
            if not pid_str.isdigit():
                continue
            try:
                with open(f"/proc/{pid_str}/environ", "rb") as f:
                    raw = f.read()
            except (OSError, PermissionError):
                continue
            for entry in raw.split(b"\x00"):
                if entry.startswith(b"STEAM_COMPAT_DATA_PATH="):
                    value = entry.split(b"=", 1)[1].decode(errors="replace").rstrip("/")
                    if value == target_prefix:
                        return True
                    break
        return False

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
