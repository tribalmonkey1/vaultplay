"""
redists.py — Redistributable data management for VaultPlay

Manages ~/.config/vaultplay/redists.json — a per-game map of
Steam App ID → winetricks verbs, built from SteamCMD depot data.

SteamCMD is optional. If not installed, the file can be imported
from another machine that has it.

Public API:
    get_redists_path()            → Path to redists.json
    load_redists()                → {app_id_str: [verbs]}
    lookup(steam_app_id)          → list[str] | None
    refresh_single_game(app_id)   → list[str] | None  (one game, writes immediately)
    refresh_missing(progress_cb)  → int  (games updated)
    refresh_all(progress_cb)      → int  (games updated)
    export_redists(dest_path)     → bool
    import_redists(src_path)      → (added, updated)
    get_stats()                   → dict
    is_steamcmd_available()       → bool
    estimate_refresh_seconds(n)   → int

Single-game vs. batch:
    refresh_single_game() is the one place that actually queries SteamCMD for
    a Steam App ID and writes the result into redists.json. Both the batch
    loop (_run_refresh, used by refresh_missing/refresh_all) and any external
    single-game caller (e.g. Edit Metadata's "Steam App ID changed" re-fetch)
    go through it, so there's a single source of truth for "check one game
    and store the result" instead of duplicating that logic per caller.
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

import json
import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional, Callable

import db

log = logging.getLogger(__name__)

# Seconds per game measured from a real 649-game SteamCMD run (~16 minutes)
_SECONDS_PER_GAME = 1.48

KNOWN_REDIST_DEPOTS = {
    228981: ["vcrun2005"],
    228982: ["vcrun2008"],
    228983: ["vcrun2010"],
    228984: ["vcrun2012"],
    228985: ["vcrun2013"],
    228986: ["vcrun2015"],
    228987: ["vcrun2017"],
    228988: ["vcrun2019"],
    228989: ["vcrun2022"],
    228990: ["d3dx9", "d3dx10", "d3dcompiler_47"],
    228991: ["openal"],
    228992: ["xact"],
    228993: ["physx"],
    1826330: ["dotnet48"],
    1826331: ["dotnet48"],
}


# ── Paths ─────────────────────────────────────────────────────────────────────

def get_redists_path() -> Path:
    """Return the writable redists.json path in the VaultPlay config directory."""
    return db._resolve_config_dir() / "redists.json"


# ── Load / save ───────────────────────────────────────────────────────────────

def load_redists() -> dict:
    """Load redists.json. Returns {} if not found."""
    path = get_redists_path()
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        log.warning("redists: could not load %s: %s", path, e)
        return {}


def save_redists(data: dict):
    """Write redists.json atomically."""
    path = get_redists_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        tmp.replace(path)
        log.debug("redists: saved %d entries to %s", len(data), path)
    except Exception as e:
        log.error("redists: save failed: %s", e)
        try:
            tmp.unlink()
        except Exception:
            pass


# ── Lookup ────────────────────────────────────────────────────────────────────

def lookup(steam_app_id: int) -> Optional[list]:
    """
    Look up redistributables for a Steam App ID.
    Returns list of verbs (may be empty = confirmed no redists),
    or None if the app ID is not in the file at all.
    """
    data = load_redists()
    key = str(steam_app_id)
    if key in data:
        return data[key]
    return None


# ── SteamCMD ──────────────────────────────────────────────────────────────────

def is_steamcmd_available() -> bool:
    return shutil.which("steamcmd") is not None


def estimate_refresh_seconds(game_count: int) -> int:
    """Estimate how long a refresh will take in seconds."""
    return int(game_count * _SECONDS_PER_GAME)


def _query_steamcmd(steam_app_id: int) -> list:
    """
    Run steamcmd +login anonymous +app_info_print <id> +quit
    and parse the output for common redistributable depot declarations.
    Returns list of winetricks verbs (may be empty).
    """
    steamcmd = shutil.which("steamcmd")
    if not steamcmd:
        return []

    try:
        result = subprocess.run(
            [steamcmd, "+login", "anonymous",
             "+app_info_update", "1",
             "+app_info_print", str(steam_app_id),
             "+quit"],
            capture_output=True,
            text=True,
            timeout=30
        )
        lines = result.stdout.splitlines()
        verbs = []
        for i, line in enumerate(lines):
            stripped = line.strip().strip('"')
            try:
                depot_id = int(stripped)
            except ValueError:
                continue
            if depot_id not in KNOWN_REDIST_DEPOTS:
                continue
            block = "\n".join(lines[i:i+15])
            if "228980" in block and "depotfromapp" in block:
                for v in KNOWN_REDIST_DEPOTS[depot_id]:
                    if v not in verbs:
                        verbs.append(v)
        return verbs
    except subprocess.TimeoutExpired:
        log.warning("redists: steamcmd timed out for app_id=%d", steam_app_id)
        return []
    except Exception as e:
        log.error("redists: steamcmd error for app_id=%d: %s", steam_app_id, e)
        return []


def refresh_single_game(steam_app_id: int, title: str = "",
                         _data: Optional[dict] = None,
                         _save: bool = True,
                         _skip_availability_check: bool = False) -> Optional[list]:
    """
    Query SteamCMD for ONE Steam App ID and store the result in redists.json.

    This is the single source of truth for the "check one game" operation.
    Use this directly for a single-game refresh — e.g. Edit Metadata's
    "Steam App ID changed" flow should call
    `redists.refresh_single_game(new_app_id, title)` rather than reaching for
    steamcmd itself. The batch functions below (_run_refresh, and therefore
    refresh_missing()/refresh_all()) also call this per game, so there's only
    one code path that talks to SteamCMD and writes the file.

    Returns:
        list[str]  — verbs found (may be empty — confirmed no common redists)
        None       — SteamCMD is not available; caller should check
                     is_steamcmd_available() itself if it needs to distinguish
                     "not available" from "checked, found nothing"

    _data / _save / _skip_availability_check are internal — used by
    _run_refresh() to batch many games against one in-memory dict (avoiding a
    load/save disk round-trip and a shutil.which() PATH scan per game) instead
    of calling is_steamcmd_available() and load_redists()/save_redists() on
    every single iteration. External callers should leave these at defaults.
    """
    if not _skip_availability_check and not is_steamcmd_available():
        log.info("redists: steamcmd not available — skipping app_id=%d", steam_app_id)
        return None

    data  = _data if _data is not None else load_redists()
    verbs = _query_steamcmd(steam_app_id)
    data[str(steam_app_id)] = verbs

    if _save:
        save_redists(data)

    log.info("redists: app %d (%s) → %s", steam_app_id, title or "?", verbs)
    return verbs


def _get_games_for_refresh(missing_only: bool) -> list:
    """
    Return list of (game_id, title, steam_app_id) for games to refresh.
    missing_only=True: only games not yet in redists.json
    missing_only=False: all games with a steam_app_id
    """
    with db.get_connection() as conn:
        rows = conn.execute("""
            SELECT
                g.id AS game_id,
                COALESCE(m.title, g.display_name, g.folder_name) AS title,
                m.steam_app_id
            FROM games g
            LEFT JOIN metadata m ON m.game_id = g.id
            WHERE m.steam_app_id IS NOT NULL
              AND g.category = 'PC'
            ORDER BY title COLLATE NOCASE
        """).fetchall()

    games = [dict(r) for r in rows]

    if missing_only:
        data = load_redists()
        games = [g for g in games if str(g["steam_app_id"]) not in data]

    return games


def _run_refresh(games: list,
                 progress_cb: Optional[Callable] = None) -> int:
    """
    Core refresh loop — queries SteamCMD for each game and updates redists.json.
    progress_cb(current, total, title) — called before each game.
    Returns number of games successfully processed.
    """
    if not games:
        return 0

    data  = load_redists()
    count = 0

    for i, game in enumerate(games):
        title  = game["title"]
        app_id = game["steam_app_id"]

        if progress_cb:
            try:
                progress_cb(i + 1, len(games), title)
            except Exception:
                pass

        # _skip_availability_check=True: refresh_missing()/refresh_all() already
        # verified SteamCMD is available before calling _run_refresh, so we don't
        # re-run shutil.which() on every single game in a 700-game batch.
        # _save=False: batch against the one in-memory `data` dict and only
        # hit disk every 10 games (below) plus once at the end.
        refresh_single_game(app_id, title, _data=data, _save=False,
                            _skip_availability_check=True)
        count += 1

        # Save incrementally every 10 games so progress isn't lost on interruption
        if count % 10 == 0:
            save_redists(data)

    save_redists(data)
    log.info("redists: refresh complete — %d games processed", count)
    return count


def refresh_missing(progress_cb: Optional[Callable] = None) -> int:
    """
    Fetch redistributable data for all PC games not yet in redists.json.
    Returns number of games processed.
    """
    if not is_steamcmd_available():
        log.info("redists: steamcmd not available — skipping refresh")
        return 0
    games = _get_games_for_refresh(missing_only=True)
    log.info("redists: refresh_missing — %d games to process", len(games))
    return _run_refresh(games, progress_cb)


def refresh_all(progress_cb: Optional[Callable] = None) -> int:
    """
    Re-fetch redistributable data for ALL PC games with a Steam App ID.
    Overwrites existing entries.
    Returns number of games processed.
    """
    if not is_steamcmd_available():
        log.info("redists: steamcmd not available — skipping refresh")
        return 0
    games = _get_games_for_refresh(missing_only=False)
    log.info("redists: refresh_all — %d games to process", len(games))
    return _run_refresh(games, progress_cb)


# ── Export / import ───────────────────────────────────────────────────────────

def export_redists(dest_path: Path) -> bool:
    """
    Copy redists.json to dest_path.
    Returns True on success.
    """
    src = get_redists_path()
    if not src.exists():
        log.warning("redists: export — no redists.json to export")
        return False
    try:
        shutil.copy2(src, dest_path)
        log.info("redists: exported to %s", dest_path)
        return True
    except Exception as e:
        log.error("redists: export failed: %s", e)
        return False


def import_redists(src_path: Path) -> tuple:
    """
    Merge an external redists.json into the local one.
    Import wins for every entry it contains — imported data is assumed
    to be newer/more complete. Entries only in the local file are preserved.

    Returns (added, updated) counts.
    """
    try:
        with open(src_path) as f:
            incoming = json.load(f)
    except Exception as e:
        log.error("redists: import failed to read %s: %s", src_path, e)
        return 0, 0

    existing = load_redists()
    added = updated = 0

    for key, verbs in incoming.items():
        if key not in existing:
            added += 1
        elif existing[key] != verbs:
            updated += 1
        existing[key] = verbs

    save_redists(existing)
    log.info("redists: imported %s — %d added, %d updated", src_path, added, updated)
    return added, updated


# ── Stats ─────────────────────────────────────────────────────────────────────

def get_stats() -> dict:
    """
    Return stats about the current redists.json for display in settings.
    {
        'total':      int,   total entries
        'with_data':  int,   entries with at least one verb
        'empty':      int,   entries confirmed as having no redists
        'missing':    int,   PC games with steam_app_id not in file
        'path':       str,   path to the file
        'exists':     bool,
        'file_size':  int,   bytes
    }
    """
    path = get_redists_path()
    data = load_redists()

    total     = len(data)
    with_data = sum(1 for v in data.values() if v)
    empty     = total - with_data

    # Count PC games with steam_app_id not yet in the file
    try:
        with db.get_connection() as conn:
            rows = conn.execute("""
                SELECT m.steam_app_id
                FROM games g
                LEFT JOIN metadata m ON m.game_id = g.id
                WHERE m.steam_app_id IS NOT NULL
                  AND g.category = 'PC'
            """).fetchall()
        missing = sum(1 for r in rows if str(r["steam_app_id"]) not in data)
    except Exception:
        missing = 0

    return {
        "total":     total,
        "with_data": with_data,
        "empty":     empty,
        "missing":   missing,
        "path":      str(path),
        "exists":    path.exists(),
        "file_size": path.stat().st_size if path.exists() else 0,
    }
