"""
debug_launch.py — On-demand debug logging for game launches (Per-Game Debug Launch Logs)

Spec: Notion → Features → Fully Planned → Per-Game Debug Launch Logs.

Triggered ONLY via the Cogwheel Menu's "Launch with Debug Log" action — the normal
Play button never logs (rather than always capturing, which the spec calls wasteful
and noisy). This module owns the actual log-capturing subprocess wrapper; the Cogwheel
menu item itself is wired up in ui/cogwheel_menu.py and dispatched through
ui/game_detail.py's shared launch path (_launch_game(debug=True)).

Log file
--------
One file per game at ~/.config/vaultplay/game_logs/<folder_name>.log — overwritten
(not appended) at the start of every debug launch. stdout and stderr are combined into
a single stream (stderr redirected into stdout) and every line is prefixed with a
timestamp: "[2026-07-17 14:23:45] <log line>".

The log is written regardless of whether the game launches successfully or crashes —
capturing output right up to a crash is the whole point, so writing happens line-by-line
as output arrives, not buffered until a clean exit.

Size cap
--------
Capped at 5MB. Once exceeded, the OLDEST lines are discarded (truncate from the top) so
the most recent output — where crashes and errors show up — is always what's kept. The
cap is only checked periodically (every _CHECK_EVERY_N_LINES lines, and once more when
the process ends) rather than after every single line, so a chatty long play session
doesn't pay for a stat()+rewrite on every write.

No in-app viewer — the user opens the log file with their own text editor.
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
import re
import subprocess
import threading
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

MAX_LOG_BYTES = 5 * 1024 * 1024   # 5MB cap — oldest lines are dropped once exceeded
_CHECK_EVERY_N_LINES = 50          # how often to check/enforce the cap while streaming


# ── Paths ─────────────────────────────────────────────────────────────────────

def _config_dir() -> Path:
    """Mirrors save_backup.py's _config_dir() — same env-var-first contract as
    every other module that needs the VaultPlay config directory."""
    env = os.environ.get("VAULTPLAY_CONFIG_DIR")
    if env:
        return Path(env)
    p = Path.home() / ".config" / "vaultplay"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _logs_dir() -> Path:
    d = _config_dir() / "game_logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_filename(folder_name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", folder_name)


def get_log_path(folder_name: str) -> Path:
    """Return the log file path for a game. Doesn't create the file — a game that has
    never had a debug launch simply has no file here yet, per spec."""
    return _logs_dir() / f"{_safe_filename(folder_name)}.log"


# ── Size cap enforcement ────────────────────────────────────────────────────────

def _truncate_from_top(path: Path):
    """
    Drop the oldest lines until the file is back under MAX_LOG_BYTES. Keeps the
    trailing MAX_LOG_BYTES of the file, then drops everything up to (and including)
    the next newline so the file never starts mid-line. Never raises — a failed
    truncation just means the file grows past the cap this once, not a crashed launch.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return
    if len(data) <= MAX_LOG_BYTES:
        return
    trimmed = data[-MAX_LOG_BYTES:]
    nl = trimmed.find(b"\n")
    if nl != -1:
        trimmed = trimmed[nl + 1:]
    try:
        path.write_bytes(trimmed)
    except OSError as e:
        log.warning("[DEBUG LOG] Could not truncate %s: %s", path, e)


# ── Background log pump ───────────────────────────────────────────────────────

class _LogPump(threading.Thread):
    """
    Reads lines from proc.stdout (stderr is already merged into stdout by the caller
    via STDOUT redirection) and appends each one, timestamped, to the per-game log
    file. Runs as a plain daemon thread — continuously draining the pipe is also what
    keeps subprocess.Popen().wait()/.poll() from deadlocking on a full pipe buffer for
    a long-running, chatty game process.
    """

    def __init__(self, proc: subprocess.Popen, log_path: Path):
        super().__init__(daemon=True)
        self._proc = proc
        self._log_path = log_path

    def run(self):
        count = 0
        try:
            with open(self._log_path, "a", encoding="utf-8", errors="replace") as f:
                for raw_line in self._proc.stdout:
                    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    f.write(f"[{ts}] {raw_line.rstrip(chr(10))}\n")
                    f.flush()
                    count += 1
                    if count % _CHECK_EVERY_N_LINES == 0:
                        _truncate_from_top(self._log_path)
        except Exception as e:
            log.warning("[DEBUG LOG] Log pump error for %s: %s", self._log_path, e)
        finally:
            # One more pass once the stream (and therefore the process) has ended,
            # so a run that crossed the cap right at the end still gets trimmed.
            _truncate_from_top(self._log_path)
            log.debug("[DEBUG LOG] Log pump finished for %s (%d lines)",
                      self._log_path, count)


# ── Public entry point ────────────────────────────────────────────────────────

def launch_with_debug_log(cmd, *, cwd: Optional[str], env: Optional[dict],
                          shell: bool, folder_name: str) -> subprocess.Popen:
    """
    Launch `cmd` with the same cwd/env/shell contract as a normal subprocess.Popen
    call elsewhere in the launch path (see game_detail.py's _spawn_process), except
    stdout+stderr are piped through a background _LogPump into this game's debug log
    file instead of the terminal.

    The log file is reset (truncated to empty) here before the process starts —
    "one file per game, overwritten on each debug launch" per spec, never an append
    across sessions.

    Returns the live Popen handle exactly like a normal launch, so the caller can wire
    it into PlaytimeWatcher/game_launched the same way — a debug launch is still
    tracked for playtime like any other.
    """
    log_path = get_log_path(folder_name)
    try:
        log_path.write_text("")   # overwrite, not append — fresh log per debug launch
    except OSError as e:
        log.warning("[DEBUG LOG] Could not reset %s: %s", log_path, e)

    popen_kwargs = dict(
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    if shell:
        proc = subprocess.Popen(cmd, shell=True, **popen_kwargs)
    else:
        proc = subprocess.Popen(cmd, **popen_kwargs)

    _LogPump(proc, log_path).start()

    log.info("[DEBUG LOG] Debug launch started for '%s' — logging to %s",
             folder_name, log_path)
    return proc
