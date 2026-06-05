"""
db.py — SQLite database layer for VaultPlay
All persistent state: games, metadata, settings, install records
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

import sqlite3
import json
import os
from pathlib import Path
from typing import Optional

# Use environment variables set by main.py at startup.
# This guarantees the same absolute path in every thread and every module,
# regardless of cwd, AppImage mount, or Python's idea of home directory.
def _resolve_config_dir() -> Path:
    env = os.environ.get("VAULTPLAY_CONFIG_DIR")
    if env:
        return Path(env)
    p = Path.home() / ".config" / "vaultplay"
    p.mkdir(parents=True, exist_ok=True)
    return p

def _resolve_db_path() -> Path:
    env = os.environ.get("VAULTPLAY_DB_PATH")
    if env:
        return Path(env)
    return _resolve_config_dir() / "vaultplay.db"

CONFIG_DIR = _resolve_config_dir()
DB_PATH    = _resolve_db_path()


def get_connection() -> sqlite3.Connection:
    """
    Always open a fresh connection using the env-var path.
    Safe to call from any thread.
    """
    db_path = _resolve_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(db_path),
        timeout=30,
        check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db():
    """Create all tables if they don't exist."""
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS games (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                folder_name     TEXT NOT NULL UNIQUE,
                nas_path        TEXT NOT NULL,
                display_name    TEXT,
                file_type       TEXT,   -- 'rar', '7zip', 'loose', 'installer'
                archive_name    TEXT,   -- primary archive filename if compressed
                size_bytes      INTEGER DEFAULT 0,
                install_tag     TEXT,   -- 'installer' | 'portable' | 'iso'
                install_tag_override INTEGER DEFAULT 0,  -- 1 if user manually set
                first_seen      TEXT DEFAULT (datetime('now')),
                category        TEXT,           -- subfolder name e.g. "PC", "3ds"
                last_scanned    TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS metadata (
                game_id              INTEGER PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
                sgdb_id              INTEGER,
                igdb_id              INTEGER,
                steam_app_id         INTEGER,
                title                TEXT,
                description          TEXT,
                developer            TEXT,
                publisher            TEXT,
                ip_holder            TEXT,
                release_date         TEXT,
                genres               TEXT,
                cover_url            TEXT,
                hero_url             TEXT,
                logo_url             TEXT,
                screenshots          TEXT,
                protondb_tier        TEXT,
                protondb_reports     INTEGER DEFAULT 0,
                recommended_proton   TEXT,
                protondb_fetched_at  TEXT,
                fetched_at           TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS installs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id         INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
                install_path    TEXT NOT NULL,
                wine_prefix     TEXT,
                install_method  TEXT,   -- 'lutris' or 'steam'
                exe_path        TEXT,   -- path to the game's launch exe
                game_path       TEXT,   -- chosen install path (portables)
                launcher_type   TEXT,   -- 'direct' or 'script'
                desktop_path    TEXT,   -- path to generated .desktop file
                script_path     TEXT,   -- path to generated launch script
                installed_at    TEXT DEFAULT (datetime('now')),
                UNIQUE(game_id)
            );

            CREATE TABLE IF NOT EXISTS settings (
                key             TEXT PRIMARY KEY,
                value           TEXT
            );

            CREATE TABLE IF NOT EXISTS art_cache (
                url             TEXT PRIMARY KEY,
                local_path      TEXT NOT NULL,
                cached_at       TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS categories (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                folder_name     TEXT NOT NULL UNIQUE,  -- e.g. "PC", "3ds"
                display_name    TEXT NOT NULL,          -- user-editable label
                blacklisted     INTEGER DEFAULT 0,      -- 1 = skip scan + hide
                sort_order      INTEGER DEFAULT 0
            );
        """)
    _migrate_db()
    _init_default_settings()


def _init_default_settings():
    defaults = {
        "nas_path":              "",
        "nas_connection_type":   "smb",
        "nas_auto_mount":        "false",
        "scan_on_launch":        "true",
        "scan_interval_minutes": "30",
        "install_path":          str(Path.home() / "Games"),
        "tmp_path":              str(Path.home() / "Games" / ".tmp"),
        "wine_prefix_root":      str(Path.home() / ".wine_prefixes"),
        "auto_cleanup_tmp":      "true",
        "default_install_method":"lutris",
        "default_prefix_mode":   "per_game",
        "wine_version":          "wine-ge",
        "auto_detect_redists":   "true",
        "always_common_bundle":  "false",
        "sgdb_api_key":          "",
        "igdb_client_id":        "",
        "igdb_client_secret":    "",
        "sgdb_art_style":        "alternate",
        "theme":                 "dark",
        "accent_color":          "#e8c76a",
        "tile_size":             "medium",
        "show_filesize_on_tile": "false",
        "install_paths":          "[\"~/Games\"]",  # JSON list of paths
        "cache_path":            str(CONFIG_DIR / "cache"),
        "default_proton_version":  "proton-experimental",
        "protondb_auto_fetch":     "true",
        "first_run_complete":      "false",
        "app_version":             "0.1.0-dev",
    }
    with get_connection() as conn:
        for key, value in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value)
            )


def _migrate_db():
    """Add any missing columns to existing databases (safe to run repeatedly)."""
    with get_connection() as conn:
        # metadata table migrations
        meta_cols = {row[1] for row in conn.execute("PRAGMA table_info(metadata)").fetchall()}
        for col, sql in [
            ("protondb_tier",       "ALTER TABLE metadata ADD COLUMN protondb_tier TEXT"),
            ("protondb_reports",    "ALTER TABLE metadata ADD COLUMN protondb_reports INTEGER DEFAULT 0"),
            ("recommended_proton",  "ALTER TABLE metadata ADD COLUMN recommended_proton TEXT"),
            ("protondb_fetched_at", "ALTER TABLE metadata ADD COLUMN protondb_fetched_at TEXT"),
            ("steam_app_id",        "ALTER TABLE metadata ADD COLUMN steam_app_id INTEGER"),
        ]:
            if col not in meta_cols:
                conn.execute(sql)
        # games table migrations
        game_cols = {row[1] for row in conn.execute("PRAGMA table_info(games)").fetchall()}
        for col, sql in [
            ("install_tag",          "ALTER TABLE games ADD COLUMN install_tag TEXT"),
            ("install_tag_override", "ALTER TABLE games ADD COLUMN install_tag_override INTEGER DEFAULT 0"),
            ("category",             "ALTER TABLE games ADD COLUMN category TEXT"),
        ]:
            if col not in game_cols:
                conn.execute(sql)


def update_protondb(game_id: int, tier: str, recommended_proton: str,
                    total_reports: int = 0):
    """Store ProtonDB compatibility data for a game."""
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO metadata (game_id, protondb_tier, recommended_proton,
                                  protondb_reports, protondb_fetched_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(game_id) DO UPDATE SET
                protondb_tier       = excluded.protondb_tier,
                recommended_proton  = excluded.recommended_proton,
                protondb_reports    = excluded.protondb_reports,
                protondb_fetched_at = datetime('now')
        """, (game_id, tier, recommended_proton, total_reports))


def reset_protondb_data():
    """
    Clear all stored ProtonDB data so the next refresh fetches everything fresh.
    Called automatically by 'Refresh ProtonDB Data' before re-fetching.
    Users should never need to do this manually via SQLite.
    """
    with get_connection() as conn:
        conn.execute("""
            UPDATE metadata
            SET protondb_tier       = NULL,
                recommended_proton  = NULL,
                protondb_reports    = 0,
                protondb_fetched_at = NULL
        """)


def get_games_missing_protondb() -> list:
    """Return games that have a steam_app_id but no ProtonDB data yet."""
    with get_connection() as conn:
        return conn.execute("""
            SELECT g.*, m.sgdb_id, m.steam_app_id, m.protondb_tier
            FROM games g
            LEFT JOIN metadata m ON m.game_id = g.id
            WHERE m.steam_app_id IS NOT NULL
              AND m.protondb_tier IS NULL
        """).fetchall()


# ── Settings helpers ──────────────────────────────────────────────────────────

def get_setting(key: str, fallback: str = "") -> str:
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else fallback


def set_setting(key: str, value: str):
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value)
        )


def get_all_settings() -> dict:
    with get_connection() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {row["key"]: row["value"] for row in rows}


def clear_all_games():
    """Remove all games and their metadata/installs. Called when NAS path changes."""
    with get_connection() as conn:
        conn.execute("DELETE FROM installs")
        conn.execute("DELETE FROM metadata")
        conn.execute("DELETE FROM art_cache")
        conn.execute("DELETE FROM games")
        conn.execute("DELETE FROM categories")
    import shutil
    cache = Path(get_setting("cache_path", str(CONFIG_DIR / "cache")))
    if cache.exists():
        shutil.rmtree(cache)
    cache.mkdir(parents=True, exist_ok=True)


def clear_database():
    """
    Nuclear option: wipe everything including settings, then re-init.
    Used by the 'Clear Database' button in settings.
    """
    import shutil
    # Drop and recreate all tables
    with get_connection() as conn:
        conn.executescript("""
            DROP TABLE IF EXISTS installs;
            DROP TABLE IF EXISTS art_cache;
            DROP TABLE IF EXISTS metadata;
            DROP TABLE IF EXISTS games;
            DROP TABLE IF EXISTS categories;
            DROP TABLE IF EXISTS settings;
        """)
    # Wipe art cache directory
    cache = Path.home() / ".config" / "vaultplay" / "cache"
    if cache.exists():
        shutil.rmtree(cache)
    cache.mkdir(parents=True, exist_ok=True)
    # Re-initialize
    init_db()


def get_categories() -> list:
    """Return ONLY non-blacklisted categories for sidebar display."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM categories WHERE blacklisted=0 "
            "ORDER BY sort_order, display_name COLLATE NOCASE"
        ).fetchall()


def get_all_categories() -> list:
    """Return ALL categories including blacklisted ones (for settings UI)."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM categories ORDER BY sort_order, display_name COLLATE NOCASE"
        ).fetchall()


def upsert_category(folder_name: str, display_name: str = "", sort_order: int = 0):
    """Add or update a non-blacklisted category."""
    display_name = display_name or folder_name
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO categories (folder_name, display_name, sort_order, blacklisted)
            VALUES (?, ?, ?, 0)
            ON CONFLICT(folder_name) DO UPDATE SET
                display_name = CASE WHEN categories.display_name = categories.folder_name
                                    THEN excluded.display_name
                                    ELSE categories.display_name END,
                sort_order   = excluded.sort_order
        """, (folder_name, display_name, sort_order))


def upsert_category_safe(folder_name: str, sort_order: int = 0):
    """
    Register a category without touching its blacklisted flag.
    Used for blacklisted categories so they appear in settings but stay blacklisted.
    """
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO categories (folder_name, display_name, sort_order, blacklisted)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(folder_name) DO UPDATE SET
                sort_order = excluded.sort_order
        """, (folder_name, folder_name, sort_order))


def rename_category(folder_name: str, new_display_name: str):
    with get_connection() as conn:
        conn.execute(
            "UPDATE categories SET display_name=? WHERE folder_name=?",
            (new_display_name, folder_name)
        )


def set_category_blacklisted(folder_name: str, blacklisted: bool):
    with get_connection() as conn:
        conn.execute(
            "UPDATE categories SET blacklisted=? WHERE folder_name=?",
            (1 if blacklisted else 0, folder_name)
        )


def get_blacklisted_categories() -> set:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT folder_name FROM categories WHERE blacklisted=1"
        ).fetchall()
        return {r["folder_name"] for r in rows}


def get_install_paths() -> list:
    """Return the list of configured install paths."""
    import json
    raw = get_setting("install_paths", '["~/Games"]')
    try:
        paths = json.loads(raw)
        return [os.path.expanduser(p) for p in paths]
    except Exception:
        return [os.path.expanduser("~/Games")]


def set_install_paths(paths: list):
    import json
    set_setting("install_paths", json.dumps(paths))


# ── Game helpers ──────────────────────────────────────────────────────────────

def upsert_game(folder_name: str, nas_path: str, display_name: str,
                file_type: str, archive_name: Optional[str],
                size_bytes: int, install_tag: str,
                category: str = "") -> int:
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO games
                (folder_name, nas_path, display_name, file_type, archive_name,
                 size_bytes, install_tag, category, last_scanned)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(folder_name) DO UPDATE SET
                nas_path     = excluded.nas_path,
                display_name = excluded.display_name,
                file_type    = excluded.file_type,
                archive_name = excluded.archive_name,
                size_bytes   = excluded.size_bytes,
                install_tag  = CASE WHEN install_tag_override=1
                                    THEN games.install_tag
                                    ELSE excluded.install_tag END,
                category     = excluded.category,
                last_scanned = datetime('now')
        """, (folder_name, nas_path, display_name, file_type,
              archive_name, size_bytes, install_tag, category))
        row = conn.execute(
            "SELECT id FROM games WHERE folder_name=?", (folder_name,)
        ).fetchone()
        return row["id"]


def get_all_games() -> list:
    with get_connection() as conn:
        return conn.execute("""
            SELECT g.*, g.install_tag, m.title, m.description, m.developer, m.publisher,
                   m.ip_holder, m.release_date, m.genres, m.cover_url,
                   m.hero_url, m.logo_url, m.screenshots, m.sgdb_id, m.igdb_id,
                   m.steam_app_id, m.protondb_tier, m.protondb_reports,
                   m.recommended_proton,
                   i.install_path, i.wine_prefix, i.install_method, i.exe_path,
                   (i.id IS NOT NULL) AS is_installed
            FROM games g
            LEFT JOIN metadata m ON m.game_id = g.id
            LEFT JOIN installs i ON i.game_id = g.id
            ORDER BY COALESCE(m.title, g.display_name) COLLATE NOCASE
        """).fetchall()


def get_games_for_library() -> list:
    """
    Lightweight query for the library tile grid.
    Only fetches what is needed to render tiles and apply filters.
    Skips description, screenshots, hero_url, logo_url, genres, etc.
    """
    with get_connection() as conn:
        return conn.execute("""
            SELECT
                g.id,
                g.folder_name,
                g.display_name,
                g.size_bytes,
                g.category,
                g.install_tag,
                COALESCE(m.title, g.display_name, g.folder_name) AS title,
                m.cover_url,
                (i.id IS NOT NULL) AS is_installed
            FROM games g
            LEFT JOIN metadata m ON m.game_id = g.id
            LEFT JOIN installs i ON i.game_id = g.id
            ORDER BY COALESCE(m.title, g.display_name, g.folder_name) COLLATE NOCASE
        """).fetchall()


def get_game(game_id: int) -> Optional[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute("""
            SELECT g.*, g.install_tag, m.title, m.description, m.developer, m.publisher,
                   m.ip_holder, m.release_date, m.genres, m.cover_url,
                   m.hero_url, m.logo_url, m.screenshots, m.sgdb_id, m.igdb_id,
                   m.steam_app_id, m.protondb_tier, m.protondb_reports,
                   m.recommended_proton,
                   i.install_path, i.wine_prefix, i.install_method, i.exe_path,
                   (i.id IS NOT NULL) AS is_installed
            FROM games g
            LEFT JOIN metadata m ON m.game_id = g.id
            LEFT JOIN installs i ON i.game_id = g.id
            WHERE g.id = ?
        """, (game_id,)).fetchone()


def get_games_missing_metadata() -> list:
    with get_connection() as conn:
        return conn.execute("""
            SELECT g.* FROM games g
            LEFT JOIN metadata m ON m.game_id = g.id
            LEFT JOIN categories c ON c.folder_name = g.category
            WHERE m.game_id IS NULL
              AND (c.blacklisted IS NULL OR c.blacklisted = 0)
        """).fetchall()


def upsert_metadata(game_id: int, data: dict):
    def _to_json(v):
        """Accept already-serialized strings or lists."""
        if v is None:
            return "[]"
        if isinstance(v, str):
            return v   # already serialized
        return json.dumps(v)

    with get_connection() as conn:
        conn.execute("""
            INSERT INTO metadata
                (game_id, sgdb_id, igdb_id, steam_app_id, title, description, developer,
                 publisher, ip_holder, release_date, genres, cover_url,
                 hero_url, logo_url, screenshots, fetched_at)
            VALUES (:game_id, :sgdb_id, :igdb_id, :steam_app_id, :title, :description,
                    :developer, :publisher, :ip_holder, :release_date,
                    :genres, :cover_url, :hero_url, :logo_url, :screenshots,
                    datetime('now'))
            ON CONFLICT(game_id) DO UPDATE SET
                sgdb_id      = excluded.sgdb_id,
                igdb_id      = excluded.igdb_id,
                steam_app_id = excluded.steam_app_id,
                title        = excluded.title,
                description  = excluded.description,
                developer    = excluded.developer,
                publisher    = excluded.publisher,
                ip_holder    = excluded.ip_holder,
                release_date = excluded.release_date,
                genres       = excluded.genres,
                cover_url    = COALESCE(excluded.cover_url, metadata.cover_url),
                hero_url     = COALESCE(excluded.hero_url,  metadata.hero_url),
                logo_url     = COALESCE(excluded.logo_url,  metadata.logo_url),
                screenshots  = COALESCE(excluded.screenshots, metadata.screenshots),
                fetched_at   = datetime('now')
        """, {
            "game_id":      game_id,
            "sgdb_id":      data.get("sgdb_id"),
            "igdb_id":      data.get("igdb_id"),
            "steam_app_id": data.get("steam_app_id"),
            "title":        data.get("title"),
            "description":  data.get("description"),
            "developer":    data.get("developer"),
            "publisher":    data.get("publisher"),
            "ip_holder":    data.get("ip_holder"),
            "release_date": data.get("release_date"),
            "genres":       _to_json(data.get("genres")),
            "cover_url":    data.get("cover_url"),
            "hero_url":     data.get("hero_url"),
            "logo_url":     data.get("logo_url"),
            "screenshots":  _to_json(data.get("screenshots")),
        })


def record_install(game_id: int, install_path: str, wine_prefix: str,
                   install_method: str = "", exe_path: str = "",
                   game_path: str = "", launcher_type: str = "direct",
                   desktop_path: str = "", script_path: str = ""):
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO installs
                (game_id, install_path, wine_prefix, install_method, exe_path,
                 game_path, launcher_type, desktop_path, script_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(game_id) DO UPDATE SET
                install_path   = excluded.install_path,
                wine_prefix    = excluded.wine_prefix,
                install_method = excluded.install_method,
                exe_path       = excluded.exe_path,
                game_path      = excluded.game_path,
                launcher_type  = excluded.launcher_type,
                desktop_path   = excluded.desktop_path,
                script_path    = excluded.script_path,
                installed_at   = datetime('now')
        """, (game_id, install_path, wine_prefix, install_method, exe_path,
              game_path, launcher_type, desktop_path, script_path))


def remove_install(game_id: int):
    with get_connection() as conn:
        conn.execute("DELETE FROM installs WHERE game_id=?", (game_id,))


def cache_art(url: str, local_path: str):
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO art_cache (url, local_path) VALUES (?, ?)",
                (url, local_path)
            )
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("cache_art failed: %s", e)


def get_cached_art(url: str) -> Optional[str]:
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT local_path FROM art_cache WHERE url=?", (url,)
            ).fetchone()
            if row and os.path.exists(row["local_path"]):
                return row["local_path"]
    except Exception:
        pass
    return None


def get_cache_size_bytes() -> int:
    cache_path = Path(get_setting("cache_path", str(CONFIG_DIR / "cache")))
    if not cache_path.exists():
        return 0
    return sum(f.stat().st_size for f in cache_path.rglob("*") if f.is_file())


def clear_metadata_cache():
    """Remove all cached artwork files and art_cache records."""
    import shutil
    cache_path = Path(get_setting("cache_path", str(CONFIG_DIR / "cache")))
    if cache_path.exists():
        shutil.rmtree(cache_path)
    cache_path.mkdir(parents=True, exist_ok=True)
    with get_connection() as conn:
        conn.execute("DELETE FROM art_cache")
        conn.execute("DELETE FROM metadata")
