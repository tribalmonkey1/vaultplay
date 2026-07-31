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
                protondb_internal_id INTEGER,   -- cached hash for reports URL
                protondb_version_counts TEXT,   -- JSON: {"GE-Proton 9.4": 31, ...}
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
                launch_cmd      TEXT,   -- full shell command to launch the game
                launch_cwd      TEXT,   -- working directory for launch_cmd
                launch_icon     TEXT,   -- path to best available icon PNG
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

            CREATE TABLE IF NOT EXISTS game_state (
                game_id             INTEGER PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
                is_favorite         INTEGER NOT NULL DEFAULT 0,   -- 1 = favorited
                is_hidden           INTEGER NOT NULL DEFAULT 0,   -- 1 = hidden from library
                completion_status   TEXT    NOT NULL DEFAULT 'unplayed',
                    -- 'unplayed' | 'in_progress' | 'completed' | 'abandoned'
                playtime_minutes    INTEGER NOT NULL DEFAULT 0,   -- total tracked playtime
                last_played         TEXT,                         -- ISO datetime or NULL
                sort_name           TEXT,                         -- user override for sort; NULL = use title
                -- Save Backup feature: canonical backup path and the
                -- in-Wine-prefix path it's symlinked from. Both NULL until
                -- Flow 1 (first play after install) successfully links a save.
                save_path           TEXT,
                save_source_path    TEXT
            );

            -- ── Version tracking ──────────────────────────────────────────────────────
            -- version_sites: one row per tracked source site (label, URL template).
            -- Normalized so editing a base_url fixes every game's constructed URL at once.
            CREATE TABLE IF NOT EXISTS version_sites (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                label                TEXT NOT NULL,
                base_url             TEXT NOT NULL,
                suffix               TEXT DEFAULT '',
                auto_track_new_games INTEGER DEFAULT 0,  -- 1 = formula site, applied to all new games
                created_at           TEXT DEFAULT (datetime('now'))
            );

            -- version_trackers: one row per (game, site) pair.
            -- Stored version values are monotonic — never regress to a lower value.
            -- Source URL is never stored; always computed as base_url + path + suffix.
            CREATE TABLE IF NOT EXISTS version_trackers (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id         INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
                site_id         INTEGER NOT NULL REFERENCES version_sites(id) ON DELETE CASCADE,
                path            TEXT NOT NULL DEFAULT '',
                is_manual       INTEGER DEFAULT 0,  -- 1 = human-entered URL/edit; never
                                                     -- append the site's formula suffix
                last_checked_at TEXT,
                last_status     TEXT,       -- 'ok' | 'no_match' | 'error'
                last_error      TEXT,
                dotted_version  TEXT,       -- highest dotted version EVER seen (monotonic)
                plain_version   TEXT,       -- highest plain/build-number version EVER seen (monotonic)
                date_version    TEXT,       -- highest date-based version EVER seen (chronologically monotonic)
                created_at      TEXT DEFAULT (datetime('now')),
                UNIQUE(game_id, site_id)
            );

            -- version_autotrack_log: prevents re-querying confirmed-absent (no_match) game/site pairs.
            -- 'error' results ARE retried next scan; 'no_match' results are not.
            CREATE TABLE IF NOT EXISTS version_autotrack_log (
                game_id           INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
                site_id           INTEGER NOT NULL REFERENCES version_sites(id) ON DELETE CASCADE,
                last_attempted_at TEXT,
                last_result       TEXT,     -- 'no_match' | 'error'
                PRIMARY KEY (game_id, site_id)
            );

            -- version_equivalences: inert scaffolding for future current_version feature.
            -- Records that a specific plain build number is the same actual build as a
            -- specific dotted version — manual, per-game, case-by-case. Not used yet.
            CREATE TABLE IF NOT EXISTS version_equivalences (
                game_id      INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
                plain_value  TEXT NOT NULL,
                dotted_value TEXT NOT NULL,
                created_at   TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (game_id, plain_value)
            );

            -- game_addons: updates, installable DLC, bonus content, and crackfixes
            -- found alongside a game's base archive — either as subfolders or as
            -- loose sibling archives at the same level as the base archive.
            -- One row per detected addon file/folder. addon_type determines how
            -- it's handled: 'update' and 'installable_dlc' go through the normal
            -- install pipeline (base -> updates -> installable_dlc -> crackfix);
            -- 'crackfix' always applies, no version gating; 'bonus' never auto-
            -- installs, it's a manual extract action only.
            -- Version columns: at most ONE of the three is ever populated per row
            -- (dotted/plain/date are different, non-comparable numbering schemes —
            -- see version_check.py's detect_version()).
            CREATE TABLE IF NOT EXISTS game_addons (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id                  INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
                addon_type               TEXT NOT NULL,   -- 'update' | 'installable_dlc' | 'bonus' | 'crackfix'
                nas_path                 TEXT NOT NULL,
                archive_name             TEXT,
                file_type                TEXT,
                size_bytes               INTEGER DEFAULT 0,
                detected_version_dotted  TEXT,
                detected_version_plain   TEXT,
                detected_version_date    TEXT,
                installed                INTEGER DEFAULT 0,
                installed_at             TEXT,
                first_seen               TEXT DEFAULT (datetime('now')),
                last_scanned             TEXT DEFAULT (datetime('now')),
                UNIQUE(game_id, nas_path, archive_name)
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
        "wine_prefix_root":      str(Path.home() / ".local" / "share" / "wineprefixes"),
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
        "wine_drive_mapping":      "auto",   # 'auto' | 'winetricks_default'
        "wine_scan_run_media":     "true",   # scan /run/media/<user>/ for drive mapping
        "wine_scan_mnt":           "false",  # scan /mnt/ for drive mapping
        "recently_added_days":     "14",     # window for "Recently Added" filter
        "version_check_auto":           "true",  # run recurring version check
        "version_check_interval_hours": "24",    # how often to recheck
        "version_check_last_run_at":    "",      # ISO datetime of last full recheck run
        "save_backup_enabled":          "false", # opt-in — off by default
        "save_backup_root":             str(Path.home() / "Documents" / "Game Saves"),
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
            ("protondb_tier",           "ALTER TABLE metadata ADD COLUMN protondb_tier TEXT"),
            ("protondb_reports",        "ALTER TABLE metadata ADD COLUMN protondb_reports INTEGER DEFAULT 0"),
            ("recommended_proton",      "ALTER TABLE metadata ADD COLUMN recommended_proton TEXT"),
            ("protondb_fetched_at",     "ALTER TABLE metadata ADD COLUMN protondb_fetched_at TEXT"),
            ("steam_app_id",            "ALTER TABLE metadata ADD COLUMN steam_app_id INTEGER"),
            ("protondb_internal_id",    "ALTER TABLE metadata ADD COLUMN protondb_internal_id INTEGER"),
            ("protondb_version_counts", "ALTER TABLE metadata ADD COLUMN protondb_version_counts TEXT"),
            # current_version_* — the base game's own NAS-detected version,
            # refreshed on every scan (not just at install time). Was
            # inert scaffolding until Update & DLC Install Support wired it
            # up via scanner.py's per-scan base-version detection. Three
            # columns, not one, for the same reason game_addons and
            # installs.installed_version_* use three — dotted/plain/date
            # are different, non-comparable numbering schemes, at most one
            # populated per game at a time.
            ("current_version_dotted",  "ALTER TABLE metadata ADD COLUMN current_version_dotted TEXT"),
            ("current_version_plain",   "ALTER TABLE metadata ADD COLUMN current_version_plain TEXT"),
            ("current_version_date",    "ALTER TABLE metadata ADD COLUMN current_version_date TEXT"),
        ]:
            if col not in meta_cols:
                conn.execute(sql)
        # games table migrations
        game_cols = {row[1] for row in conn.execute("PRAGMA table_info(games)").fetchall()}
        for col, sql in [
            ("install_tag",          "ALTER TABLE games ADD COLUMN install_tag TEXT"),
            ("install_tag_override", "ALTER TABLE games ADD COLUMN install_tag_override INTEGER DEFAULT 0"),
            ("category",             "ALTER TABLE games ADD COLUMN category TEXT"),
            # Update & DLC Install Support additions:
            ("update_patch_mode",    "ALTER TABLE games ADD COLUMN update_patch_mode TEXT DEFAULT 'incremental'"),
            ("missing_from_nas",     "ALTER TABLE games ADD COLUMN missing_from_nas INTEGER DEFAULT 0"),
        ]:
            if col not in game_cols:
                conn.execute(sql)
        # installs table migrations
        install_cols = {row[1] for row in conn.execute("PRAGMA table_info(installs)").fetchall()}
        for col, sql in [
            ("launch_cmd",  "ALTER TABLE installs ADD COLUMN launch_cmd TEXT"),
            ("launch_cwd",  "ALTER TABLE installs ADD COLUMN launch_cwd TEXT"),
            ("launch_icon", "ALTER TABLE installs ADD COLUMN launch_icon TEXT"),
            # Update & DLC Install Support additions — "what version is
            # actually installed right now", distinct from metadata's
            # current_version_* ("best version seen on the NAS"). Seeded
            # from the base archive's own detected version at install time;
            # cleared automatically when the install row is removed.
            ("installed_version_dotted", "ALTER TABLE installs ADD COLUMN installed_version_dotted TEXT"),
            ("installed_version_plain",  "ALTER TABLE installs ADD COLUMN installed_version_plain TEXT"),
            ("installed_version_date",   "ALTER TABLE installs ADD COLUMN installed_version_date TEXT"),
        ]:
            if col not in install_cols:
                conn.execute(sql)
        # game_state table — create if absent (existing DBs won't have it)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS game_state (
                game_id             INTEGER PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
                is_favorite         INTEGER NOT NULL DEFAULT 0,
                is_hidden           INTEGER NOT NULL DEFAULT 0,
                completion_status   TEXT    NOT NULL DEFAULT 'unplayed',
                playtime_minutes    INTEGER NOT NULL DEFAULT 0,
                last_played         TEXT,
                sort_name           TEXT,
                save_path           TEXT,
                save_source_path    TEXT
            )
        """)
        # game_state table migrations — save_path / save_source_path were
        # added after the table already existed in earlier databases, so
        # they need their own ALTER (CREATE TABLE IF NOT EXISTS above
        # won't add columns to an existing table).
        gs_cols = {row[1] for row in conn.execute("PRAGMA table_info(game_state)").fetchall()}
        for col, sql in [
            ("save_path",        "ALTER TABLE game_state ADD COLUMN save_path TEXT"),
            ("save_source_path", "ALTER TABLE game_state ADD COLUMN save_source_path TEXT"),
        ]:
            if col not in gs_cols:
                conn.execute(sql)
        # Version tracking tables — create if absent (existing DBs won't have them)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS version_sites (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                label                TEXT NOT NULL,
                base_url             TEXT NOT NULL,
                suffix               TEXT DEFAULT '',
                auto_track_new_games INTEGER DEFAULT 0,
                created_at           TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS version_trackers (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id         INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
                site_id         INTEGER NOT NULL REFERENCES version_sites(id) ON DELETE CASCADE,
                path            TEXT NOT NULL DEFAULT '',
                last_checked_at TEXT,
                last_status     TEXT,
                last_error      TEXT,
                dotted_version  TEXT,
                plain_version   TEXT,
                created_at      TEXT DEFAULT (datetime('now')),
                UNIQUE(game_id, site_id)
            );
            CREATE TABLE IF NOT EXISTS version_autotrack_log (
                game_id           INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
                site_id           INTEGER NOT NULL REFERENCES version_sites(id) ON DELETE CASCADE,
                last_attempted_at TEXT,
                last_result       TEXT,
                PRIMARY KEY (game_id, site_id)
            );
            CREATE TABLE IF NOT EXISTS version_equivalences (
                game_id      INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
                plain_value  TEXT NOT NULL,
                dotted_value TEXT NOT NULL,
                created_at   TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (game_id, plain_value)
            );
            CREATE TABLE IF NOT EXISTS game_addons (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id                  INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
                addon_type               TEXT NOT NULL,
                nas_path                 TEXT NOT NULL,
                archive_name             TEXT,
                file_type                TEXT,
                size_bytes               INTEGER DEFAULT 0,
                detected_version_dotted  TEXT,
                detected_version_plain   TEXT,
                detected_version_date    TEXT,
                installed                INTEGER DEFAULT 0,
                installed_at             TEXT,
                first_seen               TEXT DEFAULT (datetime('now')),
                last_scanned             TEXT DEFAULT (datetime('now')),
                UNIQUE(game_id, nas_path, archive_name)
            );
        """)
        # version_trackers.date_version / is_manual — added after the table
        # already existed in earlier databases, so they need their own ALTER
        # (executescript's CREATE TABLE IF NOT EXISTS above won't add
        # columns to an existing table).
        vt_cols = {row[1] for row in conn.execute("PRAGMA table_info(version_trackers)").fetchall()}
        if "date_version" not in vt_cols:
            conn.execute("ALTER TABLE version_trackers ADD COLUMN date_version TEXT")
        if "is_manual" not in vt_cols:
            conn.execute("ALTER TABLE version_trackers ADD COLUMN is_manual INTEGER DEFAULT 0")


def update_protondb(game_id: int, tier: str, recommended_proton: str,
                    total_reports: int = 0, internal_id: int = None,
                    version_counts: dict = None):
    """
    Store ProtonDB compatibility data for a game.
    version_counts: dict mapping canonical version key -> report count,
                    e.g. {"GE-Proton 9.4": 31, "experimental": 12}
    internal_id:    cached hash used to build the reports URL; safe to cache
                    indefinitely (old hashes remain valid even as counts change).
    """
    counts_json = json.dumps(version_counts) if version_counts else None
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO metadata (game_id, protondb_tier, recommended_proton,
                                  protondb_reports, protondb_fetched_at,
                                  protondb_internal_id, protondb_version_counts)
            VALUES (?, ?, ?, ?, datetime('now'), ?, ?)
            ON CONFLICT(game_id) DO UPDATE SET
                protondb_tier           = excluded.protondb_tier,
                recommended_proton      = excluded.recommended_proton,
                protondb_reports        = excluded.protondb_reports,
                protondb_fetched_at     = datetime('now'),
                protondb_internal_id    = COALESCE(excluded.protondb_internal_id,
                                                   metadata.protondb_internal_id),
                protondb_version_counts = excluded.protondb_version_counts
        """, (game_id, tier, recommended_proton, total_reports,
              internal_id, counts_json))


def reset_protondb_data():
    """
    Clear all stored ProtonDB data so the next refresh fetches everything fresh.
    Called automatically by 'Refresh ProtonDB Data' before re-fetching.
    Note: protondb_internal_id is intentionally preserved — old hashes remain
    valid and save a counts.json fetch for games we've already resolved.
    """
    with get_connection() as conn:
        conn.execute("""
            UPDATE metadata
            SET protondb_tier           = NULL,
                recommended_proton      = NULL,
                protondb_reports        = 0,
                protondb_version_counts = NULL,
                protondb_fetched_at     = NULL
        """)


def get_games_missing_protondb() -> list:
    """Return games that have a steam_app_id but no ProtonDB data yet."""
    with get_connection() as conn:
        return conn.execute("""
            SELECT g.*, m.sgdb_id, m.steam_app_id, m.protondb_tier,
                   m.protondb_internal_id
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
        conn.execute("DELETE FROM game_addons")
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
            DROP TABLE IF EXISTS version_trackers;
            DROP TABLE IF EXISTS version_autotrack_log;
            DROP TABLE IF EXISTS version_equivalences;
            DROP TABLE IF EXISTS version_sites;
            DROP TABLE IF EXISTS game_addons;
            DROP TABLE IF EXISTS metadata;
            DROP TABLE IF EXISTS game_state;
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
        game_id = row["id"]
        # Ensure a game_state row exists — INSERT OR IGNORE preserves any
        # existing user data (favorites, playtime, etc.) across rescans.
        conn.execute(
            "INSERT OR IGNORE INTO game_state (game_id) VALUES (?)",
            (game_id,)
        )
        return game_id


def get_all_games() -> list:
    with get_connection() as conn:
        return conn.execute("""
            SELECT g.*, g.install_tag, m.title, m.description, m.developer, m.publisher,
                   m.ip_holder, m.release_date, m.genres, m.cover_url,
                   m.hero_url, m.logo_url, m.screenshots, m.sgdb_id, m.igdb_id,
                   m.steam_app_id, m.protondb_tier, m.protondb_reports,
                   m.recommended_proton, m.protondb_version_counts,
                   i.install_path, i.wine_prefix, i.install_method, i.exe_path,
                   i.game_path, i.launcher_type, i.desktop_path, i.script_path,
                   i.launch_cmd, i.launch_cwd, i.launch_icon,
                   (i.id IS NOT NULL) AS is_installed,
                   COALESCE(gs.is_hidden,         0)          AS is_hidden,
                   COALESCE(gs.completion_status, 'unplayed') AS completion_status,
                   COALESCE(gs.playtime_minutes,  0)          AS playtime_minutes,
                   gs.last_played,
                   gs.sort_name
            FROM games g
            LEFT JOIN metadata m    ON m.game_id  = g.id
            LEFT JOIN installs i    ON i.game_id  = g.id
            LEFT JOIN game_state gs ON gs.game_id = g.id
            ORDER BY COALESCE(m.title, g.display_name) COLLATE NOCASE
        """).fetchall()


def get_games_for_library() -> list:
    """
    Lightweight query for the library tile grid.
    Only fetches what is needed to render tiles and apply filters.
    Skips description, screenshots, hero_url, logo_url, genres, etc.
    Includes game_state columns so the filter layer can use them without
    a second query per game.
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
                g.first_seen,
                COALESCE(m.title, g.display_name, g.folder_name) AS title,
                m.cover_url,
                (i.id IS NOT NULL) AS is_installed,
                COALESCE(gs.sort_name,
                         m.title,
                         g.display_name,
                         g.folder_name)             AS sort_key,
                COALESCE(gs.is_favorite,     0)     AS is_favorite,
                COALESCE(gs.is_hidden,       0)     AS is_hidden,
                COALESCE(gs.completion_status, 'unplayed') AS completion_status,
                COALESCE(gs.playtime_minutes, 0)    AS playtime_minutes,
                gs.last_played
            FROM games g
            LEFT JOIN metadata m   ON m.game_id  = g.id
            LEFT JOIN installs i   ON i.game_id  = g.id
            LEFT JOIN game_state gs ON gs.game_id = g.id
            ORDER BY COALESCE(gs.sort_name,
                              m.title,
                              g.display_name,
                              g.folder_name) COLLATE NOCASE
        """).fetchall()


def get_game(game_id: int) -> Optional[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute("""
            SELECT g.*, g.install_tag, m.title, m.description, m.developer, m.publisher,
                   m.ip_holder, m.release_date, m.genres, m.cover_url,
                   m.hero_url, m.logo_url, m.screenshots, m.sgdb_id, m.igdb_id,
                   m.steam_app_id, m.protondb_tier, m.protondb_reports,
                   m.recommended_proton, m.protondb_version_counts,
                   i.install_path, i.wine_prefix, i.install_method, i.exe_path,
                   i.game_path, i.launcher_type, i.desktop_path, i.script_path,
                   i.launch_cmd, i.launch_cwd, i.launch_icon,
                   (i.id IS NOT NULL) AS is_installed,
                   COALESCE(gs.is_favorite,       0)          AS is_favorite,
                   COALESCE(gs.is_hidden,         0)          AS is_hidden,
                   COALESCE(gs.completion_status, 'unplayed') AS completion_status,
                   COALESCE(gs.playtime_minutes,  0)          AS playtime_minutes,
                   gs.last_played,
                   gs.sort_name
            FROM games g
            LEFT JOIN metadata m    ON m.game_id  = g.id
            LEFT JOIN installs i    ON i.game_id  = g.id
            LEFT JOIN game_state gs ON gs.game_id = g.id
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
                   desktop_path: str = "", script_path: str = "",
                   launch_cmd: str = "", launch_cwd: str = "",
                   launch_icon: str = ""):
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO installs
                (game_id, install_path, wine_prefix, install_method, exe_path,
                 game_path, launcher_type, desktop_path, script_path,
                 launch_cmd, launch_cwd, launch_icon)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(game_id) DO UPDATE SET
                install_path   = excluded.install_path,
                wine_prefix    = excluded.wine_prefix,
                install_method = excluded.install_method,
                exe_path       = excluded.exe_path,
                game_path      = excluded.game_path,
                launcher_type  = excluded.launcher_type,
                desktop_path   = excluded.desktop_path,
                script_path    = excluded.script_path,
                launch_cmd     = excluded.launch_cmd,
                launch_cwd     = excluded.launch_cwd,
                launch_icon    = excluded.launch_icon,
                installed_at   = datetime('now')
        """, (game_id, install_path, wine_prefix, install_method, exe_path,
              game_path, launcher_type, desktop_path, script_path,
              launch_cmd, launch_cwd, launch_icon))


def set_install_tag_override(game_id: int, install_tag: str):
    """
    Persist the user's manual install tag choice and lock it against future scans.
    Sets install_tag_override=1 so scanner.upsert_game() CASE expression preserves it.
    """
    with get_connection() as conn:
        conn.execute(
            "UPDATE games SET install_tag=?, install_tag_override=1 WHERE id=?",
            (install_tag, game_id)
        )


def remove_install(game_id: int):
    with get_connection() as conn:
        conn.execute("DELETE FROM installs WHERE game_id=?", (game_id,))


# ── game_state helpers ────────────────────────────────────────────────────────

def get_game_state(game_id: int) -> Optional[sqlite3.Row]:
    """Return the game_state row for a game, or None if not found."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM game_state WHERE game_id=?", (game_id,)
        ).fetchone()


def set_favorite(game_id: int, favorite: bool):
    """
    Mark or unmark a game as a favorite.
    Favorites and hidden are mutually exclusive — favoriting a hidden game
    un-hides it automatically.
    """
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO game_state (game_id, is_favorite, is_hidden)
            VALUES (?, ?, 0)
            ON CONFLICT(game_id) DO UPDATE SET
                is_favorite = excluded.is_favorite,
                is_hidden   = CASE WHEN excluded.is_favorite = 1
                                   THEN 0
                                   ELSE game_state.is_hidden END
        """, (game_id, 1 if favorite else 0))


def set_hidden(game_id: int, hidden: bool):
    """
    Mark or unmark a game as hidden.
    Hiding a favorited game un-favorites it automatically.
    """
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO game_state (game_id, is_hidden, is_favorite)
            VALUES (?, ?, 0)
            ON CONFLICT(game_id) DO UPDATE SET
                is_hidden   = excluded.is_hidden,
                is_favorite = CASE WHEN excluded.is_hidden = 1
                                   THEN 0
                                   ELSE game_state.is_favorite END
        """, (game_id, 1 if hidden else 0))


VALID_COMPLETION_STATUSES = frozenset({"unplayed", "in_progress", "completed", "abandoned"})

def set_completion_status(game_id: int, status: str):
    """
    Set the completion status for a game.
    status must be one of: 'unplayed', 'in_progress', 'completed', 'abandoned'.
    """
    if status not in VALID_COMPLETION_STATUSES:
        raise ValueError(f"Invalid completion status: {status!r}. "
                         f"Must be one of {sorted(VALID_COMPLETION_STATUSES)}")
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO game_state (game_id, completion_status)
            VALUES (?, ?)
            ON CONFLICT(game_id) DO UPDATE SET
                completion_status = excluded.completion_status
        """, (game_id, status))


def add_playtime(game_id: int, minutes: int):
    """
    Add minutes to a game's total playtime and update last_played to now.
    minutes must be positive. Silently ignored if zero or negative.
    """
    if minutes <= 0:
        return
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO game_state (game_id, playtime_minutes, last_played)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(game_id) DO UPDATE SET
                playtime_minutes = game_state.playtime_minutes + excluded.playtime_minutes,
                last_played      = datetime('now')
        """, (game_id, minutes))


def set_sort_name(game_id: int, sort_name: Optional[str]):
    """
    Set or clear a custom sort name override for a game.
    Pass None to revert to automatic title-based sorting.
    """
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO game_state (game_id, sort_name)
            VALUES (?, ?)
            ON CONFLICT(game_id) DO UPDATE SET
                sort_name = excluded.sort_name
        """, (game_id, sort_name))


# ── Save Backup helpers ────────────────────────────────────────────────────────

def get_save_paths(game_id: int) -> dict:
    """
    Return {"save_path": str|None, "save_source_path": str|None} for a game.
    save_path is the canonical backed-up location; save_source_path is the
    in-Wine-prefix path that's symlinked to it. Both None if Save Backup
    has never successfully linked a save for this game.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT save_path, save_source_path FROM game_state WHERE game_id=?",
            (game_id,)
        ).fetchone()
    if not row:
        return {"save_path": None, "save_source_path": None}
    return {"save_path": row["save_path"], "save_source_path": row["save_source_path"]}


def set_save_paths(game_id: int, save_path: Optional[str] = None,
                   save_source_path: Optional[str] = None):
    """
    Set the canonical save path and/or the in-prefix symlink source path
    for a game. Only the field(s) passed are written — None leaves that
    column unchanged, matching set_installed_version()'s partial-update
    contract elsewhere in this file.
    """
    updates = []
    params  = []
    if save_path is not None:
        updates.append("save_path=?"); params.append(save_path)
    if save_source_path is not None:
        updates.append("save_source_path=?"); params.append(save_source_path)
    if not updates:
        return
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO game_state (game_id) VALUES (?)
            ON CONFLICT(game_id) DO NOTHING
        """, (game_id,))
        params.append(game_id)
        conn.execute(
            f"UPDATE game_state SET {', '.join(updates)} WHERE game_id=?",
            params
        )


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


# ── Version tracking helpers ──────────────────────────────────────────────────

def _normalize_base_url(url: str) -> str:
    """
    Normalize a site base_url for deduplication matching.
    Scheme-agnostic: http and https are treated as the same site.
    Strips leading 'www.', trailing slash, lowercases everything.
    Returns the normalized form WITHOUT a scheme prefix so that
    http://example.com and https://example.com both map to the same key.
    """
    import re as _re
    url = url.strip()
    # Strip scheme entirely for matching purposes
    m = _re.match(r'^https?://(?:www\.)?(.+)', url, _re.IGNORECASE)
    if m:
        rest = m.group(1).rstrip("/")
        return rest.lower()
    # No scheme — normalize as-is
    return url.lower().lstrip("www.").rstrip("/")


def get_version_sites() -> list:
    """Return all version_sites rows ordered by label."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM version_sites ORDER BY label COLLATE NOCASE"
        ).fetchall()


def get_or_create_version_site_by_base_url(base_url: str, label: str = "",
                                            suffix: str = "",
                                            auto_track: bool = False) -> sqlite3.Row:
    """
    Look up a version_sites row by normalized base_url.
    If found, return it (ignoring label/suffix/auto_track — caller uses the
    existing row as-is; those fields are only written on creation).
    If not found, create and return a new row.

    Normalization: lowercase scheme+host, strip leading www., strip trailing slash,
    treat http/https as the same site (stores whichever scheme was passed).
    """
    norm = _normalize_base_url(base_url)
    with get_connection() as conn:
        # Match against all sites' normalized base_urls
        rows = conn.execute("SELECT * FROM version_sites").fetchall()
        for row in rows:
            if _normalize_base_url(row["base_url"]) == norm:
                return row
        # Not found — create
        display_label = label or _base_url_to_label(base_url)
        conn.execute("""
            INSERT INTO version_sites (label, base_url, suffix, auto_track_new_games)
            VALUES (?, ?, ?, ?)
        """, (display_label, base_url.rstrip("/"), suffix,
              1 if auto_track else 0))
        return conn.execute(
            "SELECT * FROM version_sites WHERE id=last_insert_rowid()"
        ).fetchone()


def _base_url_to_label(base_url: str) -> str:
    """Derive a human-readable label from a base_url (e.g. 'https://example.com' → 'example.com')."""
    import re as _re
    m = _re.match(r'^https?://(?:www\.)?([^/]+)', base_url, _re.IGNORECASE)
    return m.group(1) if m else base_url


def update_version_site(site_id: int, label: str = None, base_url: str = None,
                        suffix: str = None, auto_track_new_games: bool = None):
    """Update one or more fields of a version_sites row. None = leave unchanged."""
    updates = []
    params  = []
    if label is not None:
        updates.append("label=?"); params.append(label)
    if base_url is not None:
        updates.append("base_url=?"); params.append(base_url.rstrip("/"))
    if suffix is not None:
        updates.append("suffix=?"); params.append(suffix)
    if auto_track_new_games is not None:
        updates.append("auto_track_new_games=?")
        params.append(1 if auto_track_new_games else 0)
    if not updates:
        return
    params.append(site_id)
    with get_connection() as conn:
        conn.execute(
            f"UPDATE version_sites SET {', '.join(updates)} WHERE id=?",
            params
        )


def delete_version_site(site_id: int):
    """
    Delete a version_sites row. Cascades to version_trackers and
    version_autotrack_log rows for this site via ON DELETE CASCADE.
    Caller should confirm the cascade count before calling.
    """
    with get_connection() as conn:
        conn.execute("DELETE FROM version_sites WHERE id=?", (site_id,))


def count_trackers_for_site(site_id: int) -> int:
    """Return how many version_trackers rows reference this site (for deletion warning)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM version_trackers WHERE site_id=?", (site_id,)
        ).fetchone()
        return row["n"] if row else 0


def add_version_tracker(game_id: int, site_id: int, path: str,
                        is_manual: bool = False) -> sqlite3.Row:
    """
    Create or update a version_trackers row for this (game, site) pair.
    If the row already exists (UNIQUE constraint), the path is updated and
    previously stored version values are reset — because a path change means
    the old versions came from a different page entirely.

    is_manual: True when the caller is a human directly pasting/editing a
    URL (AddTrackerSection, TrackerRow's Save & Recheck) — False when the
    caller is a formula/auto-track worker building a slug-based path
    (VersionAutoTrackWorker, VersionBackfillWorker). Controls whether the
    SITE's configured `suffix` gets appended when computing source_url (see
    get_trackers_for_game()/get_all_trackers()) — a manually-typed URL
    already represents the complete intended path; appending a formula
    site's suffix on top of it produces a broken, duplicated URL when the
    same base_url happens to already exist as a formula site for other
    games (confirmed real bug: a manually-added tracker producing
    ".../clair-obscur-expedition-33-download/-download/"). Set on every
    call, not just creation, so re-editing a tracker's path correctly
    re-asserts manual intent even if it was formula-created originally.
    Returns the row after upsert.
    """
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO version_trackers (game_id, site_id, path, is_manual)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(game_id, site_id) DO UPDATE SET
                path           = excluded.path,
                is_manual      = excluded.is_manual,
                -- Editing the path invalidates any previously-found version
                -- (the old value was from a different URL).
                dotted_version = NULL,
                plain_version  = NULL,
                date_version   = NULL,
                last_status    = NULL,
                last_error     = NULL,
                last_checked_at = NULL
        """, (game_id, site_id, path, 1 if is_manual else 0))
        return conn.execute("""
            SELECT * FROM version_trackers
            WHERE game_id=? AND site_id=?
        """, (game_id, site_id)).fetchone()


def remove_version_tracker(tracker_id: int):
    """Delete a specific tracker row by its primary key."""
    with get_connection() as conn:
        conn.execute("DELETE FROM version_trackers WHERE id=?", (tracker_id,))


def update_version_tracker_result(tracker_id: int, status: str,
                                   dotted_version: Optional[str] = None,
                                   plain_version:  Optional[str] = None,
                                   date_version:   Optional[str] = None,
                                   error_msg:      Optional[str] = None):
    """
    Record the result of a version check.

    Rules:
    - last_status, last_error, last_checked_at always update.
    - dotted_version / plain_version / date_version update ONLY when
      status == 'ok' AND the new value is strictly higher than what's
      stored (monotonic — never regress).
    - On 'error' or 'no_match', stored version values are left as-is.
    - If tracker_id no longer exists (deleted while check was running), no-op.

    version_check.py's sort_key() is used for dotted/plain comparisons.
    date_version uses date_sort_key() instead — dates are chronological,
    NOT comparable via the same numeric-tuple logic as dotted versions
    (e.g. "14.08.2023" must not be read as major.minor.patch = 14.8.2023).
    """
    try:
        import version_check as _vc
        has_vc = True
    except ImportError:
        has_vc = False

    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM version_trackers WHERE id=?", (tracker_id,)
        ).fetchone()
        if not row:
            return  # deleted mid-check — no-op

        new_dotted = row["dotted_version"]
        new_plain  = row["plain_version"]
        try:
            new_date = row["date_version"]
        except (IndexError, KeyError):
            new_date = None  # older DB row from before this column existed

        if status == "ok" and has_vc:
            if dotted_version and (
                not new_dotted
                or _vc.sort_key(dotted_version) > _vc.sort_key(new_dotted)
            ):
                new_dotted = dotted_version
            if plain_version and (
                not new_plain
                or _vc.sort_key(plain_version) > _vc.sort_key(new_plain)
            ):
                new_plain = plain_version
            if date_version and (
                not new_date
                or _vc.date_sort_key(date_version) > _vc.date_sort_key(new_date)
            ):
                new_date = date_version
        elif status == "ok":
            # version_check not available — accept values as-is
            new_dotted = dotted_version or new_dotted
            new_plain  = plain_version  or new_plain
            new_date   = date_version   or new_date

        conn.execute("""
            UPDATE version_trackers
            SET last_status     = ?,
                last_error      = ?,
                last_checked_at = datetime('now'),
                dotted_version  = ?,
                plain_version   = ?,
                date_version    = ?
            WHERE id = ?
        """, (status, error_msg, new_dotted, new_plain, new_date, tracker_id))


def get_trackers_for_game(game_id: int) -> list:
    """Return all version_trackers rows for a game, joined with site info."""
    with get_connection() as conn:
        return conn.execute("""
            SELECT vt.*, vs.label, vs.base_url, vs.suffix,
                   (vs.base_url || vt.path ||
                    CASE WHEN vt.is_manual = 1 THEN '' ELSE vs.suffix END
                   ) AS source_url
            FROM version_trackers vt
            JOIN version_sites vs ON vs.id = vt.site_id
            WHERE vt.game_id = ?
            ORDER BY vs.label COLLATE NOCASE
        """, (game_id,)).fetchall()


def get_all_trackers(include_blacklisted: bool = False) -> list:
    """
    Return all version_trackers rows joined with site and game info.
    Excludes games in blacklisted categories unless include_blacklisted=True.
    Used by the recurring recheck worker and Check All Now.
    """
    blacklist_clause = "" if include_blacklisted else """
        AND (c.blacklisted IS NULL OR c.blacklisted = 0)
    """
    with get_connection() as conn:
        return conn.execute(f"""
            SELECT vt.*, vs.label, vs.base_url, vs.suffix,
                   (vs.base_url || vt.path ||
                    CASE WHEN vt.is_manual = 1 THEN '' ELSE vs.suffix END
                   ) AS source_url,
                   g.folder_name, g.display_name, g.category,
                   COALESCE(m.title, g.display_name, g.folder_name) AS title
            FROM version_trackers vt
            JOIN version_sites vs ON vs.id  = vt.site_id
            JOIN games g          ON g.id   = vt.game_id
            LEFT JOIN metadata m  ON m.game_id = g.id
            LEFT JOIN categories c ON c.folder_name = g.category
            WHERE 1=1 {blacklist_clause}
            ORDER BY title COLLATE NOCASE
        """).fetchall()


def get_best_versions_for_game(game_id: int) -> dict:
    """
    Compute the highest version seen across ALL trackers for a game,
    per format bucket.

    Returns:
        {
          "dotted": str | None,   -- highest dotted version found
          "plain":  str | None,   -- highest plain/build-number version found
          "date":   str | None,   -- highest date-based version found (chronological)
          "dotted_url":  str | None,  -- source URL for the dotted winner
          "plain_url":   str | None,  -- source URL for the plain winner
          "date_url":    str | None,  -- source URL for the date winner
          "dotted_checked_at": str | None,
          "plain_checked_at":  str | None,
          "date_checked_at":   str | None,
        }

    Tie-breaking (multiple trackers with identical highest value):
      prefer most recently checked (last_checked_at DESC), then lowest id.

    Also folds in version_equivalences: for each distinct plain value any
    tracker has reported, checks for a dotted equivalent and includes it
    as a candidate in the dotted comparison.
    """
    try:
        import version_check as _vc
        sort_key = _vc.sort_key
        date_sort_key = _vc.date_sort_key
    except ImportError:
        sort_key = lambda v: v  # fallback: lexicographic
        date_sort_key = lambda v: v

    with get_connection() as conn:
        rows = conn.execute("""
            SELECT vt.id, vt.dotted_version, vt.plain_version, vt.date_version,
                   vt.last_checked_at,
                   (vs.base_url || vt.path || vs.suffix) AS source_url
            FROM version_trackers vt
            JOIN version_sites vs ON vs.id = vt.site_id
            WHERE vt.game_id = ?
        """, (game_id,)).fetchall()

    best_dotted = best_plain = best_date = None
    best_dotted_url = best_plain_url = best_date_url = None
    best_dotted_checked = best_plain_checked = best_date_checked = None

    # Candidates: (value, url, checked_at, tracker_id)
    dotted_candidates: list[tuple] = []
    plain_candidates:  list[tuple] = []
    date_candidates:   list[tuple] = []

    for row in rows:
        if row["dotted_version"]:
            dotted_candidates.append((
                row["dotted_version"], row["source_url"],
                row["last_checked_at"], row["id"]
            ))
        if row["plain_version"]:
            plain_candidates.append((
                row["plain_version"], row["source_url"],
                row["last_checked_at"], row["id"]
            ))
        try:
            row_date = row["date_version"]
        except (IndexError, KeyError):
            row_date = None
        if row_date:
            date_candidates.append((
                row_date, row["source_url"],
                row["last_checked_at"], row["id"]
            ))

    # Fold in dotted equivalents for every plain value seen
    with get_connection() as conn:
        equiv_rows = conn.execute("""
            SELECT ve.plain_value, ve.dotted_value,
                   vt.last_checked_at,
                   (vs.base_url || vt.path || vs.suffix) AS source_url,
                   vt.id
            FROM version_equivalences ve
            JOIN version_trackers vt ON vt.game_id = ve.game_id
            JOIN version_sites vs    ON vs.id = vt.site_id
            WHERE ve.game_id = ?
              AND vt.plain_version = ve.plain_value
        """, (game_id,)).fetchall()
    for eq in equiv_rows:
        dotted_candidates.append((
            eq["dotted_value"], eq["source_url"],
            eq["last_checked_at"], eq["id"]
        ))

    # Pick best dotted: highest sort_key, tie-break by most recent checked then lowest id
    if dotted_candidates:
        best = max(
            dotted_candidates,
            key=lambda c: (sort_key(c[0]), c[2] or "", -c[3])
        )
        best_dotted, best_dotted_url, best_dotted_checked, _ = best

    # Pick best plain
    if plain_candidates:
        best = max(
            plain_candidates,
            key=lambda c: (sort_key(c[0]), c[2] or "", -c[3])
        )
        best_plain, best_plain_url, best_plain_checked, _ = best

    # Pick best date — chronological comparison, NOT the dotted sort_key
    if date_candidates:
        best = max(
            date_candidates,
            key=lambda c: (date_sort_key(c[0]), c[2] or "", -c[3])
        )
        best_date, best_date_url, best_date_checked, _ = best

    return {
        "dotted":            best_dotted,
        "plain":             best_plain,
        "date":              best_date,
        "dotted_url":        best_dotted_url,
        "plain_url":         best_plain_url,
        "date_url":          best_date_url,
        "dotted_checked_at": best_dotted_checked,
        "plain_checked_at":  best_plain_checked,
        "date_checked_at":   best_date_checked,
    }


def log_autotrack_attempt(game_id: int, site_id: int, result: str):
    """
    Record a 'no_match' or 'error' result from the auto-track pass.
    'no_match' entries suppress future auto-track attempts for this pair.
    'error' entries allow retry on the next scan.
    """
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO version_autotrack_log
                (game_id, site_id, last_attempted_at, last_result)
            VALUES (?, ?, datetime('now'), ?)
            ON CONFLICT(game_id, site_id) DO UPDATE SET
                last_attempted_at = datetime('now'),
                last_result       = excluded.last_result
        """, (game_id, site_id, result))


def get_unattempted_new_games_for_site(site_id: int,
                                        new_game_ids: list) -> list:
    """
    Filter a list of new game IDs to those that should be auto-tracked
    for this site: not already in version_trackers for this site, and
    not logged as 'no_match' in version_autotrack_log for this site.
    'error' entries ARE retried (not excluded here).
    Excludes games in blacklisted categories.
    """
    if not new_game_ids:
        return []
    placeholders = ",".join("?" * len(new_game_ids))
    with get_connection() as conn:
        rows = conn.execute(f"""
            SELECT g.id, g.folder_name, g.display_name, g.category,
                   COALESCE(m.title, g.display_name, g.folder_name) AS title
            FROM games g
            LEFT JOIN metadata m   ON m.game_id = g.id
            LEFT JOIN categories c ON c.folder_name = g.category
            WHERE g.id IN ({placeholders})
              AND (c.blacklisted IS NULL OR c.blacklisted = 0)
              AND g.id NOT IN (
                  SELECT game_id FROM version_trackers
                  WHERE site_id = ?
              )
              AND g.id NOT IN (
                  SELECT game_id FROM version_autotrack_log
                  WHERE site_id = ?
                    AND last_result = 'no_match'
              )
        """, (*new_game_ids, site_id, site_id)).fetchall()
    return list(rows)


def get_backfill_candidates_for_site(site_id: int) -> list:
    """
    Return all games that should be considered for backfill for this site:
    - Not already in version_trackers for this site
    - Not logged as 'no_match' in version_autotrack_log for this site
      ('error' entries ARE retried)
    - Not in a blacklisted category
    """
    with get_connection() as conn:
        return conn.execute("""
            SELECT g.id, g.folder_name, g.display_name, g.category,
                   COALESCE(m.title, g.display_name, g.folder_name) AS title
            FROM games g
            LEFT JOIN metadata m   ON m.game_id = g.id
            LEFT JOIN categories c ON c.folder_name = g.category
            WHERE (c.blacklisted IS NULL OR c.blacklisted = 0)
              AND g.id NOT IN (
                  SELECT game_id FROM version_trackers
                  WHERE site_id = ?
              )
              AND g.id NOT IN (
                  SELECT game_id FROM version_autotrack_log
                  WHERE site_id = ?
                    AND last_result = 'no_match'
              )
            ORDER BY COALESCE(m.title, g.display_name, g.folder_name) COLLATE NOCASE
        """, (site_id, site_id)).fetchall()


def is_game_category_blacklisted(game_id: int) -> bool:
    """
    Return True if the game's category is currently blacklisted.
    Used by VersionTrackerDialog to block checks on blacklisted games.
    """
    with get_connection() as conn:
        row = conn.execute("""
            SELECT c.blacklisted
            FROM games g
            LEFT JOIN categories c ON c.folder_name = g.category
            WHERE g.id = ?
        """, (game_id,)).fetchone()
        if not row:
            return False
        return bool(row["blacklisted"])


# ── version_equivalences helpers (inert scaffolding for future current_version) ──

def set_version_equivalence(game_id: int, plain_value: str, dotted_value: str):
    """
    Record that a specific plain build number corresponds to a specific
    dotted version for this game. Upserts in place if the plain_value
    already has a mapping (re-associating with a different dotted value).
    Not used until the current_version feature is built.
    """
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO version_equivalences (game_id, plain_value, dotted_value)
            VALUES (?, ?, ?)
            ON CONFLICT(game_id, plain_value) DO UPDATE SET
                dotted_value = excluded.dotted_value
        """, (game_id, plain_value, dotted_value))


def get_dotted_equivalent_for_plain(game_id: int,
                                     plain_value: str) -> Optional[str]:
    """
    Look up the confirmed dotted equivalent for a plain build number.
    Returns the dotted_value string if found, or None.
    Not used until the current_version feature is built.
    """
    with get_connection() as conn:
        row = conn.execute("""
            SELECT dotted_value FROM version_equivalences
            WHERE game_id=? AND plain_value=?
        """, (game_id, plain_value)).fetchone()
        return row["dotted_value"] if row else None


# ── game_addons helpers (Update & DLC Install Support) ────────────────────────

VALID_ADDON_TYPES = frozenset({"update", "installable_dlc", "bonus", "crackfix"})


def upsert_addon(game_id: int, addon_type: str, nas_path: str,
                 archive_name: Optional[str], file_type: Optional[str],
                 size_bytes: int = 0,
                 detected_version_dotted: Optional[str] = None,
                 detected_version_plain: Optional[str] = None,
                 detected_version_date: Optional[str] = None) -> int:
    """
    Create or refresh a game_addons row. Called by scanner.py once per
    detected update/DLC/bonus/crackfix file or folder on every scan pass —
    upserting refreshes last_scanned, which is what prune_stale_addons()
    uses afterward to remove rows for anything no longer found on the NAS.

    Re-detecting a version on an existing row OVERWRITES the stored
    detected_version_* fields — unlike installed_version_* on `installs`,
    these represent "what's on the NAS right now", not a monotonic history.
    """
    if addon_type not in VALID_ADDON_TYPES:
        raise ValueError(f"Invalid addon_type: {addon_type!r}. "
                         f"Must be one of {sorted(VALID_ADDON_TYPES)}")
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO game_addons
                (game_id, addon_type, nas_path, archive_name, file_type,
                 size_bytes, detected_version_dotted, detected_version_plain,
                 detected_version_date, last_scanned)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(game_id, nas_path, archive_name) DO UPDATE SET
                addon_type               = excluded.addon_type,
                file_type                = excluded.file_type,
                size_bytes                = excluded.size_bytes,
                detected_version_dotted   = excluded.detected_version_dotted,
                detected_version_plain    = excluded.detected_version_plain,
                detected_version_date     = excluded.detected_version_date,
                last_scanned               = datetime('now')
        """, (game_id, addon_type, nas_path, archive_name, file_type,
              size_bytes, detected_version_dotted, detected_version_plain,
              detected_version_date))
        row = conn.execute("""
            SELECT id FROM game_addons
            WHERE game_id=? AND nas_path=? AND archive_name IS ?
        """, (game_id, nas_path, archive_name)).fetchone()
        return row["id"]


def get_addons_for_game(game_id: int, addon_type: Optional[str] = None) -> list:
    """
    Return game_addons rows for a game, optionally filtered to one addon_type.
    Ordered by detected version where meaningful (updates/installable_dlc are
    typically consumed in ascending version order for incremental application —
    callers needing that order should sort using version_check.sort_key /
    date_sort_key on the appropriate column, since SQL text sort isn't correct
    for version comparison).
    """
    with get_connection() as conn:
        if addon_type:
            return conn.execute("""
                SELECT * FROM game_addons
                WHERE game_id=? AND addon_type=?
                ORDER BY archive_name COLLATE NOCASE
            """, (game_id, addon_type)).fetchall()
        return conn.execute("""
            SELECT * FROM game_addons
            WHERE game_id=?
            ORDER BY addon_type, archive_name COLLATE NOCASE
        """, (game_id,)).fetchall()


def mark_addon_installed(addon_id: int, installed: bool = True):
    with get_connection() as conn:
        conn.execute("""
            UPDATE game_addons
            SET installed = ?, installed_at = CASE WHEN ? THEN datetime('now') ELSE installed_at END
            WHERE id = ?
        """, (1 if installed else 0, 1 if installed else 0, addon_id))


def prune_stale_addons(game_id: int, scan_started_at: str):
    """
    Delete game_addons rows for this game whose last_scanned is older than
    scan_started_at — meaning the current scan pass upserted everything it
    found (refreshing last_scanned to "now") but this row wasn't touched,
    so its backing archive/folder is no longer on the NAS.

    scan_started_at should be an ISO datetime string captured by the caller
    BEFORE it began upserting this game's addons, so the comparison can't
    accidentally match rows the current pass just wrote.
    Safe to call unconditionally after each game's addon scan completes —
    nothing else references a game_addons row, so pruning is not risky the
    way deleting a `games` row would be (see set_game_missing_from_nas
    for why base games are flagged instead of deleted).
    """
    with get_connection() as conn:
        conn.execute("""
            DELETE FROM game_addons
            WHERE game_id = ? AND last_scanned < ?
        """, (game_id, scan_started_at))


def delete_addon(addon_id: int):
    with get_connection() as conn:
        conn.execute("DELETE FROM game_addons WHERE id=?", (addon_id,))


# ── installed-version helpers (on `installs`, distinct from metadata's ────────
# ── current_version_* which represents "best version seen on the NAS") ────────

def set_installed_version(game_id: int, dotted: Optional[str] = None,
                          plain: Optional[str] = None,
                          date: Optional[str] = None):
    """
    Update the installed_version_* columns on `installs` after a base install
    or a successful update apply. Only touches the column(s) actually passed
    (None = leave that column unchanged) — callers pass whichever single
    field the winning detection populated, per detect_version()'s "at most
    one of dotted/plain/date" contract.
    No-op if there's no installs row for this game (nothing to update yet).
    """
    updates = []
    params  = []
    if dotted is not None:
        updates.append("installed_version_dotted=?"); params.append(dotted)
    if plain is not None:
        updates.append("installed_version_plain=?"); params.append(plain)
    if date is not None:
        updates.append("installed_version_date=?"); params.append(date)
    if not updates:
        return
    params.append(game_id)
    with get_connection() as conn:
        conn.execute(
            f"UPDATE installs SET {', '.join(updates)} WHERE game_id=?",
            params
        )


def get_installed_version(game_id: int) -> dict:
    """
    Return {"dotted": str|None, "plain": str|None, "date": str|None} for
    whatever's currently installed. All None if the game isn't installed,
    or if it was installed before this feature existed and nothing has
    triggered a re-detection yet.
    """
    with get_connection() as conn:
        row = conn.execute("""
            SELECT installed_version_dotted, installed_version_plain,
                   installed_version_date
            FROM installs WHERE game_id=?
        """, (game_id,)).fetchone()
    if not row:
        return {"dotted": None, "plain": None, "date": None}
    return {
        "dotted": row["installed_version_dotted"],
        "plain":  row["installed_version_plain"],
        "date":   row["installed_version_date"],
    }


def set_update_patch_mode(game_id: int, mode: str):
    """
    mode: 'incremental' (default — apply every pending update in version
    order, safe when unsure) or 'cumulative' (apply only the single highest
    pending update — an explicit opt-in optimization for games confirmed to
    ship full-replacement patches).
    """
    if mode not in ("incremental", "cumulative"):
        raise ValueError(f"Invalid update_patch_mode: {mode!r}")
    with get_connection() as conn:
        conn.execute(
            "UPDATE games SET update_patch_mode=? WHERE id=?",
            (mode, game_id)
        )


# ── NAS version helpers (metadata.current_version_*) ──────────────────────────
# The base game's own detected version, refreshed on every scan — distinct
# from installed_version_* on `installs` (what's actually installed right
# now) and from game_addons' detected_version_* (updates/DLC/crackfix, not
# the base game). This is "what version is sitting on the NAS right now",
# regardless of whether the game is installed at all.

def set_nas_version(game_id: int, dotted: Optional[str] = None,
                    plain: Optional[str] = None, date: Optional[str] = None):
    """
    Upsert the base game's NAS-detected version into metadata. Called by
    scanner.py on every scan pass, not just at install time — this is a
    plain overwrite (not monotonic like installed_version_*), since it
    represents "what's on the NAS right now" and should track a NAS
    replacement (e.g. Derrick's Dave the Diver incident) faithfully rather
    than remembering a stale higher value.
    Only the field(s) passed are written — passing None for a field leaves
    it unchanged, matching set_installed_version()'s partial-update contract.
    Creates the metadata row if one doesn't exist yet (e.g. metadata hasn't
    been fetched from SGDB/IGDB for this game).
    """
    updates = []
    params  = []
    if dotted is not None:
        updates.append("current_version_dotted=?"); params.append(dotted)
    if plain is not None:
        updates.append("current_version_plain=?"); params.append(plain)
    if date is not None:
        updates.append("current_version_date=?"); params.append(date)
    if not updates:
        return
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO metadata (game_id) VALUES (?)
            ON CONFLICT(game_id) DO NOTHING
        """, (game_id,))
        params.append(game_id)
        conn.execute(
            f"UPDATE metadata SET {', '.join(updates)} WHERE game_id=?",
            params
        )


def get_nas_version(game_id: int) -> dict:
    """
    Return {"dotted": str|None, "plain": str|None, "date": str|None} for
    the base game's NAS-detected version. All None if never detected
    (e.g. this game's archive/exe/folder name carries no version signal at
    all — a real, expected case, not an error).
    """
    with get_connection() as conn:
        row = conn.execute("""
            SELECT current_version_dotted, current_version_plain, current_version_date
            FROM metadata WHERE game_id=?
        """, (game_id,)).fetchone()
    if not row:
        return {"dotted": None, "plain": None, "date": None}
    return {
        "dotted": row["current_version_dotted"],
        "plain":  row["current_version_plain"],
        "date":   row["current_version_date"],
    }


# ── missing-from-NAS helpers ───────────────────────────────────────────────────
# Base games are flagged, never auto-deleted, on a scan that no longer finds
# their NAS folder — deleting outright would cascade away installs/metadata/
# game_state (favorites, playtime, completion), which would be a silent data
# loss if the NAS was just temporarily unreachable. game_addons rows don't
# have this risk (nothing references them), so those ARE safe to auto-prune —
# see prune_stale_addons() above.

def set_game_missing_from_nas(game_id: int, missing: bool):
    with get_connection() as conn:
        conn.execute(
            "UPDATE games SET missing_from_nas=? WHERE id=?",
            (1 if missing else 0, game_id)
        )


def get_missing_games() -> list:
    """Return games currently flagged missing_from_nas, for the Missing filter."""
    with get_connection() as conn:
        return conn.execute("""
            SELECT g.*, COALESCE(m.title, g.display_name, g.folder_name) AS title
            FROM games g
            LEFT JOIN metadata m ON m.game_id = g.id
            WHERE g.missing_from_nas = 1
            ORDER BY title COLLATE NOCASE
        """).fetchall()
