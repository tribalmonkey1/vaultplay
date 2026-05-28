# VaultPlay — Project Context & Decision History

This document exists to provide context in future chat sessions. It explains what was built, why each decision was made, and what version it was introduced in. Reference this when continuing development.

---

## Project Summary

VaultPlay is a Linux desktop application (PyQt6 + Python 3.10+) that acts as a frontend for a NAS-hosted game library. It scans game folders on a NAS, displays them as a visual tile grid with cover art, fetches metadata automatically, and installs games to a local machine using Wine prefixes — no Steam or Lutris required.

**Target user:** Derrick, running Arch Linux with KDE Plasma (one machine) and Hyprland (another). NAS is mounted via SMB/NFS at `/mnt/GoldenNAS/Games/`. Library has 700+ games.

**Distributed as:** AppImage installed to `~/Applications/VaultPlay-x86_64.AppImage` with `.desktop` entry and icon.

**Config stored at:** `~/.config/vaultplay/` (DB, log, art cache, settings)

---

## Version History

### v0.1.0-dev — Current (pre-release, in active development)

All features are part of this initial development version. No release has been cut yet.

---

## Architecture Decisions

### Why PyQt6 and not GTK/Electron/etc.
PyQt6 was chosen because it runs natively on both KDE Plasma and Hyprland without extra dependencies, has strong Python bindings, and the developer wanted a desktop-native feel. Qt6 high-DPI is always on.

### Why SQLite and not a config file
The library has 700+ games with metadata, install records, art cache URLs, and settings. SQLite handles this cleanly with WAL mode for concurrent thread access. Config file would have been too fragile.

### Why AppImage and not Flatpak/pacman
AppImage was chosen for ease of backup (single file), portability between the two Arch machines, and simplicity of distribution. The AppImage uses system Python (already confirmed 3.10+ on Arch) and bundles only pip packages.

### Why Wine-only (no Lutris/Steam integration)
Originally Lutris and Steam "Add Non-Steam Game" were planned. Removed in v0.1.0-dev because:
- Lutris integration was complex and unreliable (`.yml` import)
- Steam's `addnonsteamgame` URI is limited and doesn't support Wine prefix control
- The user's actual need is just `WINEPREFIX=~/.local/share/wineprefixes/<name> wine setup.exe`
- Keeping it Wine-only means full control over prefix naming, winetricks, and redists

---

## Feature Decisions & Why

### NAS Scanning — Category-Based with Recursive Depth

**Why:** The NAS structure is `/Games/<Category>/<Game>/`. Each category folder (PC, PS3, 3DS etc.) becomes a sidebar filter. Some games are nested one level deeper (e.g. `/PC/Baldur's Gate/BG3/`), so the scanner goes 3 levels deep recursively.

**Skip folders:** `update/`, `dlc/`, `patch/`, `extras/`, `ost/`, `manual/`, `redist/` etc. are skipped entirely to avoid registering update packs as games.

**Blacklist:** Categories can be blacklisted in Settings → Categories. Blacklisted categories are:
- Skipped during scan entirely
- Hidden from the sidebar
- Excluded from All Games count
- Excluded from metadata fetch queue

**Critical bug that was fixed:** `upsert_category()` was resetting `blacklisted=1` back to 0 on every scan. Fixed by adding `upsert_category_safe()` which only updates `sort_order` on conflict, never touching `blacklisted`.

**Critical bug that was fixed:** `get_categories()` and `get_all_games()` both lacked `WHERE blacklisted=0` in their SQL, so blacklisted categories and their games still appeared in the library. Fixed.

### Install Tags — installer / portable / iso

**Why:** Different install flows require different handling:
- `installer`: extract → run `setup.exe` via Wine
- `portable`: extract → copy to game path → create `.desktop` launcher
- `iso`: extract → mount via `udisksctl` → run setup from mount → unmount

**Detection:** The scanner peeks inside archives to classify. Priority: ISO > installer pattern > any exe = portable. For multi-part RAR, reads part 1 headers; if fewer than 3 entries found (incomplete listing), reads part 2 as well.

**ISO mounting:** `udisksctl loop-setup` + `udisksctl mount` (no sudo needed). Falls back to `fuseiso` if udisksctl fails. Unmounted and cleaned up after install.

### Folder Name Cleaning

**Why:** Games are stored with release-scene naming conventions like `Sons of the Forest MULTi16-ElAmigos` or `The Medium-(49745)`. These names make terrible search queries for SGDB/IGDB.

**What's stripped:** MULTiN-ElAmigos, -(12345) GOG IDs, [GoldBerg]/[EMPRESS] brackets, DODI Repack blocks like `– [DODI Repack]`, `(v12345 + All DLCs + MULTi6)` blocks, version strings (`v1.2.3`, `Build 16299`), release group tags (CODEX, PLAZA, RUNE, CPY, EMPRESS, FLT, RELOADED, SKIDROW, PROPHET, TENOKE, ElAmigos, GOG, DODI, TiNYiSO, GoldBerg), trailing dashes.

**4 passes:** Some names have stacked suffixes (e.g. `Game MULTi16-ElAmigos-(84926)`). The cleaning function runs 4 passes to handle stacking.

### Proton/Wine Version Detection — Dynamic

**Why:** The install dialog originally had a hardcoded list (Proton 7.0, 8.0, 9.0, Experimental, Wine-GE). This was wrong because:
- The user may not have those versions installed
- Proton 10+ was released and not in the list
- GE-Proton versions (GE-Proton9-27 etc.) weren't detected at all

**How it works now:** `protondb.py:scan_installed_versions()` scans:
- `~/.local/share/lutris/runners/wine/` — Lutris-managed builds
- `~/.steam/root/compatibilitytools.d/` — GE-Proton and custom Proton
- `~/.steam/steam/compatibilitytools.d/`
- `~/.local/share/Steam/compatibilitytools.d/`
- `~/.steam/steam/steamapps/common/` (dirs starting with "Proton")
- `~/.local/share/Steam/steamapps/common/`
- Extra Steam library locations via `libraryfolders.vdf`
- System `wine` binary

**`_dir_is_wine_version()` checks:** `bin/wine`, `bin/wine64`, `files/bin/wine`, `files/bin/wine64` (Steam Proton key path), `dist/bin/wine`, `dist/bin/wine64`, `proton` launcher script.

**Why `files/bin/wine64` was added:** Steam's official Proton (Experimental, 9.0, 10.0 etc.) ships `files/bin/wine64` but not `files/bin/wine`. The old code only checked `files/bin/wine` so every Steam-managed Proton was silently skipped. This was fixed alongside a report from the user that versions weren't detected — turned out to be user error (wrong install path) but the code fix was still correct and kept.

**Version normalization:** `GE-Proton9-27` → `GE-Proton 9.27`, `Proton - Experimental` → `Proton Experimental`, `Proton 9.0-4` → `Proton 9.0` etc.

**Stable value keys:** The internal `value` key stored in the DB is derived from the normalized label (e.g. `proton-experimental`, `ge-proton-9.27`), not from the raw folder name. This means the key is stable regardless of which Steam library the binary lives in.

**Sorting:** GE-Proton (highest version first) → Proton Experimental → numbered Proton (highest first) → Wine-GE → System Wine.

### ProtonDB Recommendations — Real Report Data

**Why:** The original recommendation was purely tier-based (`platinum` → `proton-experimental`). This was wrong — it recommended GE-Proton for Shadow Warrior even though 6/20 recent reports said Proton Experimental (the plurality).

**How it works now:**
1. Calls `protondb.max-p.me/games/{appid}/reports` (community API, no key)
2. Takes last 20 reports, extracts `proton_version` field
3. Normalizes version strings: `9.0-4` → `9.0`, `GE-Proton9-27` → `GE-Proton 9`, `Proton Experimental` → `experimental`
4. Finds most commonly reported version (plurality wins)
5. Matches against installed versions with explicit logic per type:
   - `experimental` → looks for "experimental" in installed label
   - `GE-Proton N` → finds highest available minor of that major
   - `N.M` → exact minor match on non-GE Proton, falls back to major
   - `native` → skipped (not installable)
6. Falls back to tier-based heuristic only if no report data available
7. Falls back to first installed version as last resort

**Shadow Warrior example (app_id 233130):** 6 votes Proton Experimental, 5 votes Proton 8.0, 3 votes Proton 7.0, 3 votes native, 1 each GE 7/8/9. Old code recommended GE-Proton. New code correctly recommends Proton Experimental.

### SteamDB Redistributable Detection

**Why:** The auto-detected redists were always the same 4 (`vcrun2019`, `vcrun2022`, `d3dx11`, `d3dcompiler_47`) regardless of the game. Different games need different redists (e.g. Crysis needs DirectX, XNA games need xact).

**How it works now:** `steamdb.py` calls Steam's free public `appdetails` API using the Steam App ID from SGDB. Parses package names and known depot IDs, maps them to winetricks verbs via regex patterns. Known redist depot IDs (228981, 228988 etc.) are hardcoded. Falls back to baseline if no Steam data.

**No API key required** — Steam's appdetails API is public.

### SQLite Threading — The Big Crash Source

**Why this was crashing:** The DB path was computed at import time using `Path(__file__).parent`, which inside an AppImage squashfs mount resolves to a temp path. Different threads computed different paths. Some got valid paths, some got paths that didn't exist.

**Fix:** `main.py` sets `VAULTPLAY_DB_PATH`, `VAULTPLAY_CONFIG_DIR`, `VAULTPLAY_CACHE_DIR` as environment variables before any other imports. Every module reads these env vars in `db.get_connection()`. Environment variables are process-global and inherited by all threads.

**Additional SQLite fixes:** `check_same_thread=False`, `timeout=30`, `PRAGMA busy_timeout=30000`, WAL journal mode.

### File Descriptor Exhaustion — The "Too Many Open Files" Crash

**Why this was crashing:** With 600+ games, the metadata worker made 3-4 HTTP requests per game (SGDB grids, heroes, logos, ProtonDB) while the image loader was simultaneously making HTTP requests. Each HTTP connection holds file descriptors. Linux default soft limit is 1024, which was exceeded.

**Fix:**
- `resource.setrlimit(RLIMIT_NOFILE, (65536, hard))` called in both `main.py` and `metadata.py` at startup
- HTTP adapter: `pool_connections=2, pool_maxsize=2`
- `resp.close()` called explicitly after every request
- Metadata worker sleeps 0.4s between games
- Image loader thread pool capped at 4 threads via `setMaxThreadCount(4)`
- Metadata fetch delayed 2 seconds after scan so library renders first

### AppImage Module Import Errors

**Why this was crashing:** `import db`, `import scanner`, `import steamdb` etc. fail inside AppImage because `sys.path` doesn't include the app source directory. `Path(__file__).parent` resolves inconsistently inside squashfs.

**Fix:** Every source file now has a path guard block injected at the top that:
1. Reads `$APPDIR` env var (set automatically by AppImage runtime)
2. Adds `$APPDIR/usr/bin` to `sys.path`
3. Adds `dirname(abspath(__file__))` as fallback
4. Adds parent directory (for `ui/` subpackage files to find top-level modules)

The `AppRun` script also sets `PYTHONPATH=$APPDIR/usr/bin:$APPDIR/usr/lib/pythonX.Y/site-packages` before Python starts.

### Metadata Fetch Flow

**When it runs:**
1. After every NAS scan, automatically 2 seconds later (if SGDB key configured)
2. Manual trigger via Settings → Scan & Cache → "Fetch Missing Metadata"

**What it does per game:**
1. Cleans folder name for search query
2. Searches SteamGridDB → gets Steam App ID + art URLs (cover, hero, logo)
3. If IGDB configured: searches for description, release date, developer, genres, screenshots
4. Downloads and caches all art to `~/.config/vaultplay/cache/`
5. Saves to `metadata` table in SQLite
6. If ProtonDB auto-fetch enabled: calls ProtonDB summary + reports APIs
7. Stores ProtonDB tier, report count, and matched installed version

**Gating:** Will not run at all if no SGDB API key is configured.

### Wine Prefix Naming

**Convention:** `make_prefix_name(folder_name)` in `installer.py` lowercases the folder name and replaces all non-alphanumeric characters with underscores. Example: `The Witcher 3 Wild Hunt` → `thewitcher3_wildhunt`.

**Location:** `~/.local/share/wineprefixes/<name>/`

**Creation:** `WINEPREFIX=<path> wineboot --init` creates the prefix. `WINEPREFIX=<path> winetricks -q <verb>` installs redists.

---

## Library View — Rendering System

### Why the original rendering caused a 30-second black screen on startup

The original `_render_tiles()` created all 700 `GameTile` widgets in a single synchronous loop on the main thread before yielding control to the Qt event loop. Qt does not paint until the event loop gets control back, so the window was black until all 700 tiles were constructed and added to the grid. Each tile also ran `hashlib.md5()`, created a `QPixmap`, ran a `QPainter`, and drew initials as a placeholder — expensive at scale.

Additionally `resizeEvent` called `_render_tiles()` on every pixel of resize, and Qt fires several resize events as the window geometry settles on startup, so the full 700-tile construction loop ran 3-4 times before the first paint.

### Fix: Trickle rendering via chained QTimer.singleShot(0)

**How it works:** `_start_trickle()` builds a queue of games to render. `add_next()` pops one game, creates one `GameTile`, adds it to the layout, then schedules itself again with `QTimer.singleShot(0, add_next)`. A 0ms timer yields back to the event loop — Qt processes pending paint events and input, then calls `add_next` again. Tiles appear one at a time as fast as Qt can paint them.

**Generation counter for cancellation:** Every call to `_start_trickle()` increments `_trickle_gen`. Each `add_next` closure captures its generation at creation (`my_gen`). First thing it does is `if self._trickle_gen != my_gen: return`. If a filter/search/scan triggers a new trickle while one is running, the old one stops at the next tick. No thread synchronisation needed.

**`trickle_finished` signal:** Emitted when the queue empties (or immediately for the empty-library case). `main_window.py` listens to this to start the scan.

**No placeholder art:** Cover label is a flat `surface2` coloured `QLabel`. No `QPixmap`, no `QPainter`, no `hashlib` in the hot loop. Art loads asynchronously as before.

### Fix: Row-based layout (Option B) to prevent tiles shifting during trickle

**Problem:** With `QGridLayout`, as tiles trickle in row by row, Qt recalculates the layout after each addition. A partially-filled row has different height behaviour than a full row, so existing tiles shift up and down as new tiles are added. This was visually jarring.

**Options considered:**
- **Option A — Pre-allocate spacer widgets:** Fill all grid cells with invisible spacers upfront, replace with real tiles as trickle runs. Fixes shifting but spacers become wrong when a scan adds new games (total count changes, grid expands, everything reflows again). Ruled out.
- **Option B — Row-based layout (chosen):** Each row is its own `QHBoxLayout` inside a `QVBoxLayout`. Rows are added to the vertical stack only when the first tile of that row is placed. Tiles within a row don't shift vertically. New rows append to the bottom. Existing rows never move. Works correctly when scan adds new games too — new trickle just appends more rows at the bottom.
- **Option C — Absolute positioning:** Calculate total height upfront, place tiles via `tile.move(x, y)`. Zero layout overhead but fragile — manual position management, breaks on any size/column change. Ruled out.

**Option A and C remain as future alternatives** if Option B ever causes issues, but Option B is correct for this use case because it handles both the initial trickle and the post-scan re-render without any shifting.

**How Option B works in code:** `_start_trickle()` creates a `QWidget` container with a `QVBoxLayout` (`_rows_widget`/`_rows_layout`). Each time `add_next` places the first tile of a new row, it creates a new `QHBoxLayout` row widget and appends it to `_rows_layout`. Subsequent tiles in that row are added to the current row widget. When the row is full (tile count % cols == 0), the next tile starts a new row. On reflow (resize changing column count), the entire rows widget is cleared and a new trickle starts.

### Fix: Debounced resize reflow

`resizeEvent` restarts a 200ms `QTimer`. When the timer fires (`_on_resize_settled`), the new column count is calculated. If it matches `_last_cols`, nothing happens (avoids pointless re-renders when moving the window without changing width). If it changed (maximize onto wider monitor, window resize), a new trickle starts.

### Fix: Scan sequencing — scan waits for trickle to finish

**Why:** The startup flow previously called `_load_library()` then immediately `_start_scan()`. The scan could finish and call `_load_library()` again while the trickle was still running, creating a collision (new trickle cancels old one mid-fill, starts again).

**Fix:** `_initial_load()` stores the scan args in `_pending_scan` instead of calling `_start_scan()` directly. When `trickle_finished` fires, `_on_trickle_finished()` picks up `_pending_scan` and starts the scan. The scan therefore always starts after every tile is visible on screen.

### Fix: Refresh button locked during trickle and scan

**Why:** Clicking refresh while tiles are trickling in would start a scan, which when done would call `_load_library()`, which would start a new trickle — cancelling the current one. Confusing and wasteful.

**Fix:** `library_view.set_scan_running(bool)` called by `main_window` when scan starts/finishes. The refresh button is disabled when either `_trickle_active` or `_scan_running` is True. `_unlock_refresh()` checks both flags before re-enabling.

### Fix: `_populate_wine_combo()` blockSignals

`_populate_wine_combo()` in `settings_view.py` was clearing and repopulating the Proton version combo. Qt fires `currentIndexChanged` during `clear()` and `addItem()` calls, which was connected to `self._save("default_proton_version", ...)`. This overwrote the user's saved default with whatever index happened to be current during the rebuild.

**Fix:** Wrap the clear/repopulate in `self.default_wine_combo.blockSignals(True/False)`. The saved value is read from DB and restored after repopulating.

### Fix: Lightweight DB query for library view

`get_all_games()` fetched ~25 columns per row including `description`, `screenshots`, `hero_url`, `logo_url`, `genres`, `developer`, `publisher` etc. — none of which are needed to render a tile.

Added `get_games_for_library()` which fetches 9 columns: `id, folder_name, display_name, size_bytes, category, install_tag, title, cover_url, is_installed`. `_load_library()` in `main_window.py` now calls this. `get_all_games()` is still used by the metadata worker and anything that needs the full row.

---

## Settings Structure

| Section | Key | Default | Notes |
|---|---|---|---|
| NAS Connection | `nas_path` | `""` | Must not be empty or `/` |
| NAS Connection | `nas_connection_type` | `smb` | smb/nfs/local |
| NAS Connection | `scan_on_launch` | `true` | Toggle in Settings → Scan & Cache |
| Paths | `install_path` | `~/Games` | Stored as JSON list in `install_paths` |
| Paths | `tmp_path` | `~/Games/.tmp` | Extraction temp dir |
| Paths | `auto_cleanup_tmp` | `true` | |
| API Keys | `sgdb_api_key` | `""` | SteamGridDB |
| API Keys | `igdb_client_id` | `""` | Optional |
| API Keys | `igdb_client_secret` | `""` | Optional |
| Wine | `default_prefix_mode` | `per_game` | per_game or default |
| Wine | `default_proton_version` | `""` | Set from dynamic scan |
| Wine | `auto_detect_redists` | `true` | Uses Steam API |
| Wine | `protondb_auto_fetch` | `true` | |
| Appearance | `theme` | `dark` | |
| Appearance | `accent_color` | `#e8c76a` | Gold |
| Appearance | `tile_size` | `medium` | small/medium/large |
| Scan & Cache | `scan_interval_minutes` | `30` | |
| Scan & Cache | `cache_path` | `~/.config/vaultplay/cache` | |
| Internal | `first_run_complete` | `false` | Set to true after wizard |
| Internal | `app_version` | `0.1.0-dev` | |

---

## Database Schema

**`games`** — `id, folder_name, nas_path, display_name, file_type, archive_name, size_bytes, install_tag, install_tag_override, category, first_seen, last_scanned`

**`metadata`** — `game_id, sgdb_id, igdb_id, title, description, developer, publisher, ip_holder, release_date, genres (JSON), cover_url, hero_url, logo_url, screenshots (JSON), protondb_tier, protondb_reports, recommended_proton, protondb_fetched_at, fetched_at`

**`installs`** — `id, game_id, install_path, wine_prefix, install_method, exe_path, game_path, launcher_type, desktop_path, script_path, installed_at`

**`categories`** — `id, folder_name, display_name, blacklisted, sort_order`

**`settings`** — `key, value` (key-value store)

**`art_cache`** — `url, local_path, cached_at`

---

## Known Issues / Not Yet Implemented

- **Launch installed games** — Launch button exists in UI but not yet wired to actually run the game
- **Uninstall** — No uninstall flow yet
- **Manual metadata editing** — Can't correct a wrong SGDB match yet
- **Game exe selection** — If auto-detection picks wrong exe, no UI to fix it
- **Background scan interval timer** — Configured in settings but not actually started
- **Light theme** — Not implemented
- **Multi-NAS** — Single path only
- **Console games** — Blacklisted for now (3DS, PS3, Switch etc.), planned for future
- **SteamDB depot scraping** — Currently uses package name patterns, not actual depot inspection. Full depot scraping would require authenticated SteamDB API access which doesn't exist publicly.

---

## File Structure

```
vaultplay/
├── main.py              # Entry point, env var setup, FD limit, sys.path
├── db.py                # SQLite layer — all DB operations, settings, migrations
├── scanner.py           # NAS folder scan, archive peeking, category detection
├── metadata.py          # SteamGridDB + IGDB API, art download, fetch orchestration
├── installer.py         # Extract, Wine prefix, winetricks, installer/portable/iso flows
├── protondb.py          # ProtonDB API, installed version scanning, recommendation logic
├── steamdb.py           # Steam appdetails API, redistributable detection
├── requirements.txt     # pip dependencies
├── install.sh           # AppImage builder — builds, installs to ~/Applications
├── VAULTPLAY_CONTEXT.md # This file — decisions, history, architecture notes
├── assets/
│   ├── icon.png         # 256x256 app icon
│   ├── icon_16.png
│   ├── icon_32.png
│   ├── icon_48.png
│   └── icon_128.png
└── ui/
    ├── __init__.py
    ├── main_window.py   # App shell, sidebar, scan/metadata workers, signal routing
    ├── library_view.py  # Tile grid, trickle render, async image loading, search, filter
    ├── game_detail.py   # Detail page — hero, cover, info card, install card
    ├── install_dialog.py # Install options — Wine prefix, Proton, redists, progress
    ├── settings_view.py  # All 8 settings sections including About/version
    ├── setup_wizard.py  # First-run wizard
    └── style.py         # Stylesheet constants, color tokens
```

---

## Tech Stack

| Layer | Tech |
|---|---|
| Language | Python 3.10+ |
| UI | PyQt6 |
| Database | SQLite (WAL mode, check_same_thread=False) |
| 7z extraction | py7zr |
| RAR extraction | rarfile + system unrar |
| ZIP extraction | stdlib zipfile |
| Image handling | Pillow + Qt pixmaps |
| HTTP | requests (pool_connections=2, pool_maxsize=2) |
| Wine prefix | wineboot CLI |
| Redistributables | winetricks CLI |
| ISO mounting | udisksctl (fallback: fuseiso) |
| Launcher generation | .desktop files + optional .sh wrapper |
| Art metadata | SteamGridDB API (free key) |
| Text metadata | IGDB API (free Twitch dev app, optional) |
| Compatibility | ProtonDB summary API + community reports API |
| Redist detection | Steam appdetails API (no key) |
| AppImage build | appimagetool |

---

## Signals & Communication (PyQt6)

`settings_view.py` emits three signals to `main_window.py`:

| Signal | When | What happens |
|---|---|---|
| `nas_path_changed(str)` | NAS Apply clicked with new path | Clear DB + rescan |
| `rescan_requested(str)` | Scan Now clicked, or `__metadata__` | Rescan only (no clear), or trigger metadata fetch |
| `reload_requested()` | Blacklist toggled | Reload library display only, no scan |

`library_view.py` emits to `main_window.py`:

| Signal | When | What happens |
|---|---|---|
| `trickle_finished()` | All tiles added to grid | Starts pending scan if queued |
| `game_selected(int)` | Tile clicked | Shows game detail view |
| `refresh_requested(str)` | Refresh button clicked | Starts scan |

---

## Deployment Notes

**AppImage build:** Run `bash install.sh` from the source directory. Requires `wget`, `patchelf`, `fuse2` (installed automatically). Downloads `appimagetool` if not present. Builds to `VaultPlay-x86_64.AppImage` in the source dir, then copies to `~/Applications/`.

**First run:** Wizard appears. Enter NAS path (e.g. `/mnt/GoldenNAS/Games`), SGDB API key from steamgriddb.com → Profile → API, and default install path. Click "Save & Start Scanning".

**SGDB API key:** Free. Register at steamgriddb.com, go to Profile → API → generate key.

**IGDB:** Optional. Register a free app at dev.twitch.tv. Get Client ID and Client Secret.

**ProtonDB and Steam API:** No keys needed, both are public.

**Proton versions:** Install via ProtonUp-Qt (recommended) which puts them in `~/.steam/root/compatibilitytools.d/`. VaultPlay scans this automatically. Official Steam Proton lives in `steamapps/common/` and is also detected.

---

## Session 3 additions

### Jitter root cause found — CSS hover swap via enterEvent/leaveEvent

After implementing row-based layout (Option B) and fixed-position rows (Option C hybrid), jitter persisted. The user noticed it only happened when the mouse was over the tile grid area, not when it was in the sidebar. This identified the real cause: `GameTile` had `enterEvent` and `leaveEvent` overrides that called `self.setStyleSheet()` to swap the border color on hover. Every `setStyleSheet()` call on a child widget can invalidate the parent layout's geometry, causing Qt to recalculate and repaint the surrounding area — visually manifesting as vertical jitter of nearby rows.

**Fix:** Removed `enterEvent` and `leaveEvent` entirely. The hover style is now defined once using the CSS `:hover` pseudo-selector in the stylesheet set at construction time. Qt handles hover state transitions internally with no Python callbacks, no `setStyleSheet()` calls at runtime, and no layout invalidation.

The row-based layout (Option B) was kept as it is cleaner than QGridLayout for the trickle pattern, but the layout approach was not the cause of jitter.

**Options A and C** (pre-allocated spacers and absolute positioning) were explored during debugging but ruled out:
- Option A: spacers become wrong when scan adds new games, causing reflow
- Option C: manual positioning works but adds complexity; may be revisited if row-based layout causes issues at very large library sizes (2000+ games)

### Empty state layout fix

The empty state (no games found / scanning) previously hid the scroll area and showed a large expanding label, which caused the header and status bar to shift position relative to when tiles were loaded. Fixed by keeping the scroll area always visible and placing the empty label inside it. The header and status bar stay in exactly the same position regardless of whether games are loaded.

### Status bar fixed height

The status bar previously had no fixed height, so when it appeared (showing scan progress) it would push the tile grid down. Fixed with `setFixedHeight(28)`.

### Setup wizard fixes

- Button text changed from "Save & Start Scanning →" to "Save and Start Scan →"
- Dialog minimum size increased from 640×580 to 680×660 to prevent section text truncation

### Critical bug: sgdb_id was being used as Steam App ID for ProtonDB

SteamGridDB assigns its own internal IDs to games (e.g. SGDB might give Age of Empires DE the ID 12345). Steam's own App ID for the same game is completely different (e.g. 813780). The ProtonDB API expects Steam App IDs, not SGDB IDs.

The code was storing `match["id"]` from SGDB as `sgdb_id` and then passing `sgdb_id` to ProtonDB lookups. This meant ProtonDB was being queried with a random SGDB internal ID that either matched some completely different game's Steam App ID, or returned no results at all. ProtonDB recommendations were therefore wrong or absent for every game.

**Fix:** The SGDB autocomplete response includes `external_id` which is the Steam App ID when the game has a Steam entry. Added a new `steam_app_id` column to the `metadata` table. `metadata.py` now extracts `match.get("external_id")` and stores it as `steam_app_id`. `protondb.py:fetch_and_store()` now reads `game["steam_app_id"]` instead of `game["sgdb_id"]`. The DB migration adds the column to existing databases.

**Impact:** All existing ProtonDB data in the DB was fetched using wrong IDs. After this fix, clearing ProtonDB data and re-fetching (Settings → Wine → Refresh All) will get correct recommendations.

### Bug: fallback Proton recommendation ignored user's saved default

When ProtonDB had no data for a game, `recommended_proton_for_game()` fell through to "first installed version" which was GE-Proton (highest version, sorted first). It never consulted `default_proton_version` from settings. Fixed by adding a step between the tier heuristic and the last resort that reads `db.get_setting("default_proton_version")` and matches it against installed versions.

### Session 4 fixes

**Setup wizard text clipping** — Body section wrapped in a `QScrollArea` so content never clips regardless of dialog height. `QScrollArea` and `QFrame` added to imports.

**Wizard → Settings not reflecting saved values** — Three problems:
1. `nas_edit` was a local variable in `_build_nas_page()`, never stored on `self`. `load_settings()` couldn't update it. Fixed by storing it as `self.nas_path_edit` with a local alias for closures.
2. API key edits (`sgdb_api_key`, `igdb_client_id`, `igdb_client_secret`) were local vars in `_api_key_row()`. Fixed by storing them in `self._api_key_edits[key]` dict and updating in `load_settings()`.
3. `_show_setup_wizard()` wasn't calling `settings_view.load_settings()` after wizard closed. Fixed.

**Wizard install path** — Wizard was saving `install_path` (single string, legacy key) but the settings Paths page reads `install_paths` (JSON list). Fixed by also calling `db.set_install_paths([install])` in wizard `_save()`.

**Last Scan shows "Never scanned" after a scan** — `last_scan_result` was read once at page build time and never refreshed. Fixed by storing a `self._last_scan_desc_lbl` reference and updating it in `_on_scan_done()` and `load_settings()`.

**ProtonDB never ran after metadata** — `_on_meta_done()` never called `_start_protondb_fetch()`. Fixed by adding a 1-second delayed call to `_start_protondb_fetch()` at the end of `_on_meta_done()`, gated on `protondb_auto_fetch` setting.

**ProtonDB status bar not updating** — `_start_protondb_fetch()` had progress connected via lambda but no dedicated `_on_proton_progress` method. Fixed with proper method and status bar updates.

**Tile cover art not updating live during metadata fetch** — `MetadataWorker` only had `progress` and `finished` signals. Added `game_updated(int)` signal emitted after each game's metadata saves. `fetch_all_missing()` now accepts `game_done_callback` parameter. `library_view.refresh_tile(game_id)` does a lightweight DB query for just the cover URL and starts an `ImageLoader` for that one tile. No full library reload needed.

**ProtonDB using wrong ID (sgdb_id ≠ Steam App ID)** — Also fixed `get_games_missing_protondb()` to check `steam_app_id IS NOT NULL` instead of `sgdb_id IS NOT NULL`, and fixed the ProtonDB auto-fetch trigger in `metadata.py` to check `metadata.get("steam_app_id")` instead of `metadata.get("sgdb_id")`.

### Critical bug: external_id is NOT in SGDB autocomplete response

The previous fix assumed `match.get("external_id")` would return the Steam App ID from the SGDB autocomplete result. It does not — the autocomplete endpoint (`/search/autocomplete/{name}`) only returns `id, name, types, verified, logo, release_date`. The `external_id` field (Steam App ID) is only available via a separate `GET /games/{sgdb_id}` call.

**Fix:** Added `sgdb_get_steam_id(sgdb_id, key)` function that calls `GET /games/{sgdb_id}` and returns `external_id` as an integer. Called immediately after the autocomplete search succeeds in `fetch_metadata_for_game()`. This adds one extra HTTP request per game during metadata fetch but is the only correct way to get the Steam App ID from SGDB.

**IGDB fallback:** Also updated the IGDB query to request `external_games.uid,external_games.category`. `external_games.category == 1` means Steam, and `uid` is the Steam App ID. If SGDB couldn't find a Steam App ID but IGDB can, the IGDB value is used. SGDB takes precedence if both find one.

**Settings ProtonDB refresh button stuck:** The `_work()` thread had `setEnabled(True)` only in the success path, not in `except`. If it errored, button stayed disabled until leaving and returning to the page. Fixed by moving re-enable to a `finally` block.

**Settings ProtonDB refresh using sgdb_id:** `_work()` was checking `g["sgdb_id"]` to decide whether to fetch ProtonDB data, but `fetch_and_store()` uses `steam_app_id`. Fixed to check `steam_app_id` instead.

**Note on existing data:** After this fix, clearing the database and doing a full scan+metadata fetch will correctly populate `steam_app_id` for all games that are on Steam. Games not on Steam (non-Steam platform exclusives, DRM-free only games etc.) will have `steam_app_id = NULL` and will correctly skip ProtonDB fetching.

### Critical bug: SGDB has no external_id field anywhere in their API

After confirming `steam_app_id = 0` for all 783 games in the DB despite metadata fetch running successfully, it became clear that `external_id` does not exist in any SGDB API endpoint — not in autocomplete, not in `GET /games/{id}`. The SGDB API game object only contains: `id, name, release_date, types, verified`. There is no field mapping SGDB IDs to Steam App IDs.

The IGDB `external_games` fallback works but requires IGDB to be configured (optional).

**Fix:** Replaced `sgdb_get_steam_id()` with `steam_search_app_id(game_title)` which calls Steam's free public store search API: `store.steampowered.com/api/storesearch/?term={name}&cc=US&l=en`. No API key required. Returns the first result's `id` field which is the Steam App ID. Since SGDB already confirmed the game name via its own search, the Steam search result for that same name is reliably accurate. IGDB `external_games` remains as a secondary source when IGDB is configured.

**Order of precedence for steam_app_id:**
1. Steam store search (called after SGDB match, uses SGDB's confirmed game name)
2. IGDB external_games (category=1, uid=Steam App ID) — only if IGDB configured
3. NULL if neither source finds it (non-Steam games, DRM-free only, etc.)
