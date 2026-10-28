"""
playtime.py — Background playtime tracking for VaultPlay

Tracks how long a game session lasts and records it to game_state.playtime_minutes.

PlaytimeWatcher is a QThread started right after a successful game launch.

  Plain Wine / Wine-GE: runs `wineserver --wait` (WINEPREFIX set) so multi-process
      games and launcher stubs are handled. wineserver --wait only waits on an
      ALREADY running wineserver, so a result that arrives while the launched
      process is still alive is the startup race and is retried.

  Proton: waits on the Popen handle, THEN polls /proc for any process whose
      environment shows STEAM_COMPAT_DATA_PATH == this prefix.

Shutdown safety (2026-10-28)
----------------------------
Every wait below is interruptible: it polls in short slices and checks
self._abort. MainWindow.closeEvent calls request_abort() on every watcher, so
the thread exits quickly WITHOUT recording a session (the game keeps running;
on next start VaultPlay re-attaches to it). Previously the thread sat in a
blocking wait / time.sleep, and destroying the QThread at interpreter exit
aborted the process ("QThread: Destroyed while thread is still running").

Force Quit (request_kill) now terminates every process belonging to the prefix
(Proton: anything with matching STEAM_COMPAT_DATA_PATH; Wine: `wineserver -k`),
not only the top-level launcher, so a hung game that ignored its own Exit is
actually stopped.

Sessions under MIN_SESSION_MINUTES are discarded. 24h safety cap on every wait.
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
_SLICE: float = 0.5                 # granularity of interruptible waits


def is_proton(wine_bin: str) -> bool:
    """Return True if wine_bin is a Proton script (not plain Wine/Wine-GE)."""
    return Path(wine_bin).name == "proton"


# ── Restart-reattachment (Currently Playing Indicator) ────────────────────────

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
    """Seconds since process `pid` started (from /proc/<pid>/stat starttime),
    or None if unreadable. Splits on the LAST ')' because the comm field can
    contain spaces/parens."""
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
    elapsed = uptime - starttime_ticks / _clock_ticks_per_sec()
    return elapsed if elapsed >= 0 else None


def find_running_pid_for_exe(exe_path: str) -> Optional[int]:
    """Best-effort /proc scan for a process whose cmdline references exe_path's
    filename. Never raises."""
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


def pids_in_prefix(target_prefix: str) -> list:
    """PIDs of all processes whose STEAM_COMPAT_DATA_PATH equals target_prefix."""
    target = (target_prefix or "").rstrip("/")
    out = []
    if not target:
        return out
    try:
        pids = os.listdir("/proc")
    except OSError:
        return out
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
                if value == target:
                    out.append(int(pid_str))
                break
    return out


class AttachedProcess:
    """Minimal Popen-like wrapper around a PID VaultPlay did not launch."""

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
            time.sleep(0.25)
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

    session_ended(game_id, minutes_recorded): minutes 0 == too short / not
    recorded. NOT emitted when the watcher was aborted for app shutdown.
    """
    session_ended = pyqtSignal(int, int)   # game_id, minutes_recorded

    def __init__(self, game_id: int, proc, wine_bin: str, wine_prefix: str,
                 elapsed_seconds: float = 0.0, parent=None):
        super().__init__(parent)
        self._game_id         = game_id
        self._proc            = proc
        self._wine_bin        = wine_bin
        self._wine_prefix     = wine_prefix
        self._elapsed_seconds = max(0.0, elapsed_seconds)
        self._abort           = False
        self._wineserver_proc: Optional[subprocess.Popen] = None

    # ── control ───────────────────────────────────────────────────────────
    def request_abort(self):
        """Ask run() to return promptly without recording (app shutting down).
        Safe from any thread."""
        self._abort = True
        ws = self._wineserver_proc
        if ws is not None:
            try:
                ws.terminate()      # only the `wineserver --wait` helper, not the game
            except Exception:
                pass

    def request_kill(self):
        """Force Quit: terminate the tracked process AND everything else in
        the prefix. run() then sees the exit and records the session normally."""
        gid = self._game_id
        try:
            if is_proton(self._wine_bin):
                pids = pids_in_prefix(self._wine_prefix)
                for pid in pids:
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except OSError:
                        pass
                # Give them a moment, then SIGKILL stragglers.
                time.sleep(1.5)
                for pid in pids_in_prefix(self._wine_prefix):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
            else:
                env = {**os.environ, "WINEPREFIX": self._wine_prefix}
                try:
                    subprocess.run(["wineserver", "-k"], env=env, timeout=10)
                except Exception as e:
                    log.debug("[PLAYTIME] wineserver -k failed: %s", e)
            self._proc.kill()
            log.info("[PLAYTIME] Force Quit requested for game_id=%d", gid)
        except Exception as e:
            log.warning("[PLAYTIME] request_kill failed for game_id=%d: %s", gid, e)

    # ── main ──────────────────────────────────────────────────────────────
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

        if self._abort:
            log.info("[PLAYTIME] Watcher for game_id=%d aborted (app closing) — "
                     "session not recorded; will re-attach on next start",
                     self._game_id)
            return

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

    def _sleep(self, seconds: float):
        """Interruptible sleep."""
        end = time.monotonic() + seconds
        while not self._abort and time.monotonic() < end:
            time.sleep(min(_SLICE, max(0.0, end - time.monotonic())))

    def _wait_wine(self):
        """Block until all Wine processes in the prefix exit (wineserver --wait),
        falling back to proc.wait(). Interruptible via request_abort()."""
        if not self._wine_prefix:
            log.warning("[PLAYTIME] No wine_prefix — falling back to proc.wait()")
            self._wait_proc()
            return

        env = {**os.environ, "WINEPREFIX": self._wine_prefix}
        deadline = time.monotonic() + MAX_SESSION_SECONDS
        logged_race = False

        while not self._abort:
            if time.monotonic() >= deadline:
                log.warning("[PLAYTIME] wineserver --wait hit 24h safety cap — ending session")
                return
            try:
                ws = subprocess.Popen(["wineserver", "--wait"], env=env)
            except FileNotFoundError:
                log.warning("[PLAYTIME] wineserver not found — falling back to proc.wait()")
                self._wait_proc()
                return
            except Exception as e:
                log.warning("[PLAYTIME] wineserver --wait error: %s — falling back to proc.wait()", e)
                self._wait_proc()
                return

            self._wineserver_proc = ws
            try:
                while ws.poll() is None:
                    if self._abort:
                        try:
                            ws.terminate()
                        except Exception:
                            pass
                        break
                    if time.monotonic() >= deadline:
                        try:
                            ws.terminate()
                        except Exception:
                            pass
                        return
                    time.sleep(_SLICE)
            finally:
                self._wineserver_proc = None
                try:
                    ws.wait(timeout=2)
                except Exception:
                    pass

            if self._abort:
                return

            if self._proc.poll() is None:
                # wineserver said done but our launched process is alive — startup race.
                if not logged_race:
                    log.info("[PLAYTIME] wineserver --wait returned but the launched "
                             "process is still running (startup race) — retrying")
                    logged_race = True
                self._sleep(1.0)
                continue
            return

    def _wait_proton(self):
        """Wait for the launched process, then for every process in the prefix."""
        log.debug("[PLAYTIME] Using proc.wait() for Proton session")
        self._wait_proc()
        if not self._abort:
            self._wait_for_prefix_processes_to_exit()

    def _wait_for_prefix_processes_to_exit(self, poll_interval: float = 2.0):
        if not self._wine_prefix:
            return
        target   = self._wine_prefix.rstrip("/")
        deadline = time.monotonic() + MAX_SESSION_SECONDS

        self._sleep(1.0)

        logged_continuation = False
        while not self._abort and time.monotonic() < deadline:
            if not self._any_process_in_prefix(target):
                return
            if not logged_continuation:
                log.info("[PLAYTIME] Top-level process exited but other "
                         "process(es) still running in prefix %s — "
                         "continuing session", target)
                logged_continuation = True
            self._sleep(poll_interval)

        if not self._abort:
            log.warning("[PLAYTIME] Proton prefix process check hit 24h safety "
                        "cap — ending session")

    @staticmethod
    def _any_process_in_prefix(target_prefix: str) -> bool:
        return bool(pids_in_prefix(target_prefix))

    def _wait_proc(self):
        """Wait on the original handle in short slices (interruptible), 24h cap."""
        deadline = time.monotonic() + MAX_SESSION_SECONDS
        try:
            while not self._abort:
                try:
                    self._proc.wait(timeout=_SLICE)
                    log.debug("[PLAYTIME] proc.wait() returned, returncode=%s",
                              self._proc.returncode)
                    return
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        log.warning("[PLAYTIME] proc.wait() hit 24h safety cap — ending session")
                        try:
                            self._proc.kill()
                        except Exception:
                            pass
                        return
        except Exception as e:
            log.warning("[PLAYTIME] proc.wait() error: %s", e)


def format_playtime(minutes: int) -> str:
    """Human-readable playtime: 0 → "No playtime recorded", 90 → "1 hour 30 minutes",
    >24h shown in days."""
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
