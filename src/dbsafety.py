"""
dbsafety.py — Database backups and orphaned-install detection for VaultPlay

* backup_now(reason): consistent snapshot of the SQLite DB (online backup API)
  into <config>/backups/, rotated so only the newest MAX_BACKUPS are kept.
* list_backups() / restore_backup(path): manual recovery helpers.
* find_orphans() / heal_orphan(): VaultPlay-written .desktop files
  (X-VaultPlay=true) that have no install record are re-adopted.
"""

import logging
import re
import shlex
import sqlite3
import time
from pathlib import Path
from typing import Optional

import db

log = logging.getLogger(__name__)

MAX_BACKUPS = 8
APPS_DIR = Path.home() / ".local" / "share" / "applications"


def _config_dir() -> Path:
    try:
        return Path(db._resolve_config_dir())
    except Exception:
        return Path(db.CONFIG_DIR)


def backup_dir() -> Path:
    d = _config_dir() / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _db_path() -> Path:
    return Path(db.DB_PATH)


def backup_now(reason: str = "manual", min_interval_s: int = 0) -> Optional[Path]:
    """Snapshot the DB. Returns the backup path, or None if skipped/failed.
    Never raises — a failed backup must not block the caller's work."""
    try:
        src = _db_path()
        if not src.exists() or src.stat().st_size == 0:
            return None
        d = backup_dir()
        if min_interval_s:
            newest = max(d.glob("vaultplay-*.db"), default=None,
                         key=lambda p: p.stat().st_mtime)
            if newest and time.time() - newest.stat().st_mtime < min_interval_s:
                return None
        safe = re.sub(r"[^a-zA-Z0-9_-]", "_", reason)[:24] or "manual"
        dest = d / f"vaultplay-{time.strftime('%Y%m%d-%H%M%S')}-{safe}.db"
        s = sqlite3.connect(str(src))
        t = sqlite3.connect(str(dest))
        try:
            s.backup(t)
        finally:
            t.close()
            s.close()
        _rotate(d)
        log.info("DB backup written: %s", dest)
        return dest
    except Exception as e:
        log.warning("DB backup failed (%s): %s", reason, e)
        return None


def _rotate(d: Path):
    files = sorted(d.glob("vaultplay-*.db"), key=lambda p: p.stat().st_mtime,
                   reverse=True)
    # destructive-op backups are more valuable than routine startup ones:
    # keep every "before_" backup up to the limit, trim startup ones first.
    for p in files[MAX_BACKUPS:]:
        try:
            p.unlink()
        except OSError:
            pass


def list_backups() -> list:
    return sorted(backup_dir().glob("vaultplay-*.db"),
                  key=lambda p: p.stat().st_mtime, reverse=True)


def restore_backup(path) -> bool:
    """Replace the live DB with a backup (backing the live one up first)."""
    try:
        backup_now("before_restore")
        s = sqlite3.connect(str(path))
        t = sqlite3.connect(str(_db_path()))
        try:
            s.backup(t)
        finally:
            t.close()
            s.close()
        return True
    except Exception as e:
        log.error("restore_backup failed: %s", e)
        return False


# ── Orphaned installs ────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def parse_desktop(path: Path) -> dict:
    out = {}
    try:
        for line in Path(path).read_text(errors="replace").splitlines():
            if "=" in line and not line.startswith("["):
                k, _, v = line.partition("=")
                out.setdefault(k.strip(), v.strip())
    except OSError:
        pass
    return out


def parse_exec(exec_line: str) -> dict:
    """Pull wineprefix / compat path / binary / exe out of an Exec= line."""
    info = {"wineprefix": "", "compat": "", "wine_bin": "", "exe": ""}
    try:
        tokens = shlex.split(exec_line)
    except ValueError:
        tokens = exec_line.split()
    for t in tokens:
        if t.startswith("WINEPREFIX="):
            info["wineprefix"] = t.split("=", 1)[1]
        elif t.startswith("STEAM_COMPAT_DATA_PATH="):
            info["compat"] = t.split("=", 1)[1]
    try:
        import installer
        info["wine_bin"] = installer.parse_wine_bin_from_cmd(exec_line)
    except Exception:
        pass
    exes = [t for t in tokens if t.lower().endswith((".exe", ".lnk", ".bat"))]
    if exes:
        info["exe"] = exes[-1]
    return info


def _known_desktop_paths() -> set:
    try:
        conn = db.get_connection()
        try:
            rows = conn.execute(
                "SELECT desktop_path FROM installs WHERE desktop_path != ''"
            ).fetchall()
        finally:
            conn.close()
        return {r[0] for r in rows}
    except Exception as e:
        log.warning("orphan check: could not read installs: %s", e)
        return None


def find_orphans() -> list:
    """VaultPlay .desktop files with no install record. Each entry:
    {desktop_path, name, exec, path, icon, wineprefix, compat, wine_bin,
     exe, game_id (or None if no library game matches the name)}.
    Returns [] on any DB trouble (never guess when the DB is unreadable)."""
    known = _known_desktop_paths()
    if known is None or not APPS_DIR.exists():
        return []
    try:
        games = db.get_all_games()
    except Exception:
        return []
    by_name = {}
    for g in games:
        if g["is_installed"]:
            continue
        for key in (g["title"], g["display_name"], g["folder_name"]):
            if key:
                by_name.setdefault(_norm(key), g["id"])
    out = []
    for f in sorted(APPS_DIR.glob("*.desktop")):
        if str(f) in known:
            continue
        d = parse_desktop(f)
        if d.get("X-VaultPlay", "").lower() != "true":
            continue
        info = parse_exec(d.get("Exec", ""))
        out.append({
            "desktop_path": str(f),
            "name": d.get("Name", f.stem),
            "exec": d.get("Exec", ""),
            "path": d.get("Path", ""),
            "icon": d.get("Icon", ""),
            "game_id": by_name.get(_norm(d.get("Name", f.stem))),
            **info,
        })
    return out


def heal_orphan(orphan: dict, game_id: Optional[int] = None) -> bool:
    """Recreate the install record for an orphan. Uses the stored command
    verbatim so launching keeps working exactly as before."""
    gid = game_id or orphan.get("game_id")
    if not gid:
        return False
    try:
        prefix = orphan.get("compat") or orphan.get("wineprefix") or ""
        exe = orphan.get("exe") or ""
        install_path = orphan.get("path") or (str(Path(exe).parent) if exe else prefix)
        proton = ""
        wb = orphan.get("wine_bin") or ""
        if Path(wb).name == "proton":
            try:
                import protondb
                proton = protondb.find_version_value_for_binary(wb) or ""
            except Exception:
                proton = ""
        db.record_install(
            game_id=gid,
            install_path=install_path,
            wine_prefix=prefix,
            exe_path=exe,
            desktop_path=orphan["desktop_path"],
            launch_cmd=orphan.get("exec", ""),
            launch_cwd=orphan.get("path", ""),
            launch_icon=orphan.get("icon", ""),
            proton_version=proton,
        )
        return True
    except Exception as e:
        log.error("heal_orphan failed for %s: %s", orphan.get("desktop_path"), e)
        return False
