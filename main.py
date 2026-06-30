#!/usr/bin/env python3
"""
VaultPlay — NAS Game Launcher
Entry point
"""

import sys
import os
import resource
import time
import logging
from pathlib import Path

# ── Raise file descriptor limit immediately ───────────────────────────────────
try:
    _soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(_hard, 65536), _hard))
except Exception:
    pass

# ── Set absolute paths via environment variables BEFORE any other import ──────
_CONFIG_DIR = Path.home() / ".config" / "vaultplay"
_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
os.environ["VAULTPLAY_CONFIG_DIR"] = str(_CONFIG_DIR)
os.environ["VAULTPLAY_DB_PATH"]    = str(_CONFIG_DIR / "vaultplay.db")
os.environ["VAULTPLAY_CACHE_DIR"]  = str(_CONFIG_DIR / "cache")
(_CONFIG_DIR / "cache").mkdir(parents=True, exist_ok=True)

# ── Fix sys.path BEFORE any local imports ────────────────────────────────────
# The AppImage runtime sets $APPDIR to the squashfs mount point.
# All app source files are at $APPDIR/usr/bin/
_appdir = os.environ.get("APPDIR", "")
if _appdir:
    _appdir_bin = os.path.join(_appdir, "usr", "bin")
    if _appdir_bin not in sys.path:
        sys.path.insert(0, _appdir_bin)

# Fallback: use __file__ location
_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)
try:
    _real = os.path.dirname(os.path.realpath(__file__))
    if _real not in sys.path:
        sys.path.insert(0, _real)
except Exception:
    pass

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_DIR = _CONFIG_DIR
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "vaultplay.log", mode="a")
    ]
)
log = logging.getLogger("vaultplay")

# ── Inline debug helpers (no separate module needed) ─────────────────────────
_APP_START = time.monotonic()

def _log_phase(phase: str, detail: str = ""):
    elapsed = time.monotonic() - _APP_START
    msg = f"[PHASE +{elapsed:.3f}s] {phase}"
    if detail:
        msg += f" — {detail}"
    log.info(msg)
    print(msg, flush=True)

def _print_system_info():
    import platform, sqlite3
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    lines = [
        "",
        "  ⬡ VaultPlay startup diagnostics",
        f"  Platform:   {platform.platform()}",
        f"  Python:     {platform.python_version()}",
        f"  SQLite:     {sqlite3.sqlite_version}",
        f"  FD limit:   soft={soft}  hard={hard}",
        f"  AppImage:   {bool(os.environ.get('APPIMAGE'))}",
        f"  APPDIR:     {_appdir or 'not set'}",
        f"  DB path:    {os.environ.get('VAULTPLAY_DB_PATH')}",
        f"  Cache:      {os.environ.get('VAULTPLAY_CACHE_DIR')}",
        f"  sys.path:   {sys.path[:3]}",
        "",
    ]
    for line in lines:
        print(line, flush=True)
        if line.strip():
            log.info(line.strip())

# ── Now safe to import local modules ─────────────────────────────────────────
import db
from ui.main_window import MainWindow

def main():
    _print_system_info()
    _log_phase("main() start")

    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import QCoreApplication
    from PyQt6.QtGui import QFont

    QCoreApplication.setApplicationName("VaultPlay")
    QCoreApplication.setOrganizationName("VaultPlay")

    app = QApplication(sys.argv)
    app.setApplicationDisplayName("VaultPlay")
    app.setFont(QFont("DM Sans", 11))

    _log_phase("DB init")
    db.init_db()

    _log_phase("MainWindow create")
    window = MainWindow()
    window.show()
    _log_phase("Window shown — entering event loop")

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
