"""
metadata.py — SteamGridDB + IGDB metadata fetcher for VaultPlay

Uses a single SQLite connection per fetch session to avoid file descriptor
exhaustion. HTTP connections are limited via pool adapter.
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

import os, re, logging, datetime, time, hashlib, resource
from pathlib import Path
from typing import Optional

import requests
import requests.adapters

import db

log = logging.getLogger(__name__)

# Raise file descriptor limit
try:
    _soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(_hard, 65536), _hard))
except Exception:
    pass

# Single shared HTTP session with strict pool limits
SESSION = requests.Session()
SESSION.headers["User-Agent"] = "VaultPlay/1.0"
_adapter = requests.adapters.HTTPAdapter(
    pool_connections=2, pool_maxsize=2, max_retries=1
)
SESSION.mount("https://", _adapter)
SESSION.mount("http://", _adapter)

SGDB_BASE = "https://www.steamgriddb.com/api/v2"
IGDB_BASE  = "https://api.igdb.com/v4"
_igdb_token_cache: dict = {}


# ── Cache dir ──────────────────────────────────────────────────────────────────

def _cache_dir() -> Path:
    env = os.environ.get("VAULTPLAY_CACHE_DIR")
    if env:
        p = Path(env)
        p.mkdir(parents=True, exist_ok=True)
        return p
    try:
        s = db.get_setting("cache_path", "")
        if s:
            p = Path(s)
            p.mkdir(parents=True, exist_ok=True)
            return p
    except Exception:
        pass
    p = Path.home() / ".config" / "vaultplay" / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _cache_path_for_url(url: str) -> Path:
    ext = Path(url.split("?")[0]).suffix or ".jpg"
    fname = hashlib.md5(url.encode()).hexdigest() + ext
    return _cache_dir() / fname


# ── Art download ───────────────────────────────────────────────────────────────

def download_art(url: str) -> Optional[str]:
    """Download artwork to local cache. Returns local path or None."""
    if not url:
        return None
    try:
        cached = db.get_cached_art(url)
        if cached:
            return cached
    except Exception:
        pass

    local_path = _cache_path_for_url(url)
    try:
        resp = SESSION.get(url, timeout=15, stream=True)
        resp.raise_for_status()
        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(8192):
                f.write(chunk)
        resp.close()
        try:
            db.cache_art(url, str(local_path))
        except Exception:
            pass
        return str(local_path)
    except Exception as e:
        log.debug("download_art failed for %s: %s", url, e)
        return None


# ── SGDB ───────────────────────────────────────────────────────────────────────

def _sgdb_headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def sgdb_search(name: str, key: str) -> Optional[dict]:
    try:
        resp = SESSION.get(
            f"{SGDB_BASE}/search/autocomplete/{requests.utils.quote(name)}",
            headers=_sgdb_headers(key), timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        resp.close()
        if data.get("success") and data.get("data"):
            return data["data"][0]
    except Exception as e:
        log.warning("SGDB search failed for '%s': %s", name, e)
    return None


def steam_search_app_id(game_title: str) -> Optional[int]:
    """
    Search Steam's store search API to find the Steam App ID for a game.
    No API key required. Returns the App ID integer, or None if not found.

    Uses: store.steampowered.com/api/storesearch/?term={name}&cc=US&l=en
    The first result's 'id' field is the Steam App ID.
    """
    try:
        resp = SESSION.get(
            "https://store.steampowered.com/api/storesearch/",
            params={"term": game_title, "cc": "US", "l": "en"},
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        resp.close()
        items = data.get("items", [])
        if not items:
            return None
        # Take the first result — Steam search is generally accurate for
        # exact title matches which is what SGDB already confirmed for us
        return int(items[0]["id"])
    except Exception as e:
        log.debug("Steam store search failed for '%s': %s", game_title, e)
    return None


def sgdb_get_art(sgdb_id: int, key: str, style_pref: str = "alternate") -> dict:
    art = {"cover_url": None, "hero_url": None, "logo_url": None}
    for endpoint, art_key, params in [
        ("grids",  "cover_url", "?dimensions=600x900"),
        ("heroes", "hero_url",  ""),
        ("logos",  "logo_url",  ""),
    ]:
        try:
            resp = SESSION.get(
                f"{SGDB_BASE}/{endpoint}/game/{sgdb_id}{params}",
                headers=_sgdb_headers(key), timeout=10
            )
            resp.raise_for_status()
            data = resp.json()
            resp.close()
            if data.get("success") and data.get("data"):
                items = data["data"]
                chosen = next(
                    (i for i in items if style_pref in (i.get("style") or "")),
                    items[0]
                )
                art[art_key] = chosen.get("url")
        except Exception as e:
            log.warning("SGDB %s fetch failed for id %d: %s", endpoint, sgdb_id, e)
    return art


# ── IGDB ───────────────────────────────────────────────────────────────────────

def _igdb_token(client_id: str, client_secret: str) -> Optional[str]:
    global _igdb_token_cache
    if _igdb_token_cache.get("expires_at", 0) > time.time():
        return _igdb_token_cache.get("token")
    try:
        resp = requests.post(
            "https://id.twitch.tv/oauth2/token",
            params={"client_id": client_id, "client_secret": client_secret,
                    "grant_type": "client_credentials"}, timeout=10
        )
        resp.raise_for_status()
        j = resp.json()
        resp.close()
        _igdb_token_cache = {
            "token": j["access_token"],
            "expires_at": time.time() + j["expires_in"] - 60,
        }
        return j["access_token"]
    except Exception as e:
        log.warning("IGDB auth failed: %s", e)
    return None


def igdb_search(name: str, client_id: str, client_secret: str) -> Optional[dict]:
    token = _igdb_token(client_id, client_secret)
    if not token:
        return None
    headers = {
        "Client-ID": client_id,
        "Authorization": f"Bearer {token}",
        "Content-Type": "text/plain",
    }
    query = (
        f'search "{name}"; '
        f'fields name,summary,first_release_date,involved_companies.company.name,'
        f'involved_companies.developer,involved_companies.publisher,'
        f'genres.name,screenshots.url,external_games.uid,external_games.category; limit 1;'
    )
    try:
        resp = SESSION.post(f"{IGDB_BASE}/games", headers=headers,
                            data=query, timeout=15)
        resp.raise_for_status()
        results = resp.json()
        resp.close()
        if not results:
            return None
        game = results[0]
        developer = publisher = None
        for ic in game.get("involved_companies", []):
            cname = ic.get("company", {}).get("name", "")
            if ic.get("developer") and not developer:
                developer = cname
            if ic.get("publisher") and not publisher:
                publisher = cname
        release_date = None
        if game.get("first_release_date"):
            release_date = datetime.datetime.utcfromtimestamp(
                game["first_release_date"]
            ).strftime("%B %d, %Y")
        screenshots = []
        for ss in game.get("screenshots", [])[:6]:
            url = ss.get("url", "")
            if url.startswith("//"):
                url = "https:" + url
            url = url.replace("t_thumb", "t_screenshot_big")
            screenshots.append(url)

        # Extract Steam App ID from external_games (category 1 = Steam)
        steam_app_id = None
        for eg in game.get("external_games", []):
            if eg.get("category") == 1:
                try:
                    steam_app_id = int(eg["uid"])
                except (KeyError, TypeError, ValueError):
                    pass
                break

        return {
            "igdb_id":      game.get("id"),
            "title":        game.get("name"),
            "description":  game.get("summary"),
            "developer":    developer,
            "publisher":    publisher,
            "ip_holder":    developer or publisher,
            "release_date": release_date,
            "genres":       [g["name"] for g in game.get("genres", [])],
            "screenshots":  screenshots,
            "steam_app_id": steam_app_id,   # None if not on Steam
        }
    except Exception as e:
        log.warning("IGDB search failed for '%s': %s", name, e)
    return None


# ── ProtonDB ───────────────────────────────────────────────────────────────────

import protondb as protondb_mod


# ── Main fetch function ────────────────────────────────────────────────────────

def fetch_metadata_for_game(game_id: int, sgdb_key: str, igdb_id_key: str,
                             igdb_secret: str, style_pref: str) -> bool:
    """
    Fetch metadata for one game. All API keys passed in (read once by caller).
    Returns True if metadata was saved.
    """
    from scanner import clean_folder_name
    try:
        game = db.get_game(game_id)
    except Exception as e:
        log.error("DB error loading game %d: %s", game_id, e)
        return False

    if not game:
        return False

    search_name = clean_folder_name(game["folder_name"])
    if not search_name:
        search_name = game["display_name"] or game["folder_name"]

    metadata: dict = {}

    # ── SGDB ──────────────────────────────────────────────────────────────────
    if sgdb_key:
        match = sgdb_search(search_name, sgdb_key)
        if match:
            sgdb_id = match["id"]
            metadata["sgdb_id"] = sgdb_id
            if not metadata.get("title"):
                metadata["title"] = match.get("name", search_name)
            art = sgdb_get_art(sgdb_id, sgdb_key, style_pref)
            metadata.update(art)

            # Look up the actual Steam App ID using Steam's store search API.
            # SGDB's API does not expose the Steam App ID in any endpoint.
            game_name = match.get("name", search_name)
            steam_id = steam_search_app_id(game_name)
            if steam_id:
                metadata["steam_app_id"] = steam_id
                log.debug("Steam search: '%s' → steam_app_id=%d", game_name, steam_id)
            else:
                log.debug("Steam search: no App ID found for '%s'", game_name)
        else:
            log.info("No SGDB match for '%s'", search_name)

    # ── IGDB ──────────────────────────────────────────────────────────────────
    if igdb_id_key and igdb_secret:
        igdb_data = igdb_search(search_name, igdb_id_key, igdb_secret)
        if igdb_data:
            for k, v in igdb_data.items():
                if v is not None and k not in ("cover_url", "hero_url", "logo_url"):
                    # Don't overwrite steam_app_id if SGDB already found it
                    if k == "steam_app_id" and metadata.get("steam_app_id"):
                        continue
                    metadata[k] = v
        else:
            log.info("No IGDB match for '%s'", search_name)

    if not metadata:
        return False

    if not metadata.get("title"):
        metadata["title"] = search_name

    import json
    metadata.setdefault("genres", [])
    metadata.setdefault("screenshots", [])

    # Convert lists to JSON strings for storage
    if isinstance(metadata.get("genres"), list):
        metadata["genres"] = json.dumps(metadata["genres"])
    if isinstance(metadata.get("screenshots"), list):
        metadata["screenshots"] = json.dumps(metadata["screenshots"])

    try:
        db.upsert_metadata(game_id, metadata)
        log.info("Metadata saved for '%s'", metadata.get("title", search_name))
    except Exception as e:
        log.error("Failed to save metadata for game %d: %s", game_id, e)
        return False

    # ── ProtonDB ──────────────────────────────────────────────────────────────
    # Only fetch if we have a real Steam App ID — sgdb_id is SteamGridDB's
    # internal ID and is NOT the same as a Steam App ID.
    steam_app_id = metadata.get("steam_app_id")
    if steam_app_id:
        try:
            auto = db.get_setting("protondb_auto_fetch", "true") == "true"
        except Exception:
            auto = True
        if auto:
            try:
                protondb_mod.fetch_and_store(game_id)
            except Exception as e:
                log.warning("ProtonDB fetch failed for game %d: %s", game_id, e)

    return True


def fetch_all_missing(progress_callback=None, game_done_callback=None) -> int:
    """
    Fetch metadata for all games missing it.
    progress_callback(current, total, name) — called before each game
    game_done_callback(game_id) — called after each game's metadata is saved
    Only runs if SGDB key is configured.
    """
    # Read all settings once before starting
    try:
        sgdb_key    = db.get_setting("sgdb_api_key", "")
        igdb_id_key = db.get_setting("igdb_client_id", "")
        igdb_secret = db.get_setting("igdb_client_secret", "")
        style_pref  = db.get_setting("sgdb_art_style", "alternate")
    except Exception as e:
        log.error("Could not read API settings: %s", e)
        return 0

    if not sgdb_key:
        log.info("No SGDB API key — skipping metadata fetch")
        return 0

    try:
        games = db.get_games_missing_metadata()
    except Exception as e:
        log.error("Could not load games list: %s", e)
        return 0

    log.info("[METADATA] Starting fetch for %d games (SGDB=%s, IGDB=%s)",
             len(games), bool(sgdb_key), bool(igdb_id_key and igdb_secret))
    _meta_t0 = time.monotonic()
    count = 0

    for i, game in enumerate(games):
        try:
            display = game["display_name"] or game["folder_name"]
        except Exception:
            display = f"game_{i}"

        if progress_callback:
            try:
                progress_callback(i + 1, len(games), display)
            except Exception:
                pass

        try:
            _t0 = time.monotonic()
            ok = fetch_metadata_for_game(
                game["id"], sgdb_key, igdb_id_key, igdb_secret, style_pref
            )
            _ms = (time.monotonic() - _t0) * 1000
            if ok:
                count += 1
                log.debug("[METADATA] %-40s  ✓ %.0f ms", display[:40], _ms)
                if game_done_callback:
                    try:
                        game_done_callback(game["id"])
                    except Exception:
                        pass
            else:
                log.debug("[METADATA] %-40s  – no data (%.0f ms)", display[:40], _ms)
        except Exception as e:
            log.error("[METADATA] %-40s  ✗ %s", display[:40], e)

        # Rate limiting: be polite to APIs and allow file descriptors to close
        time.sleep(0.4)

    elapsed = time.monotonic() - _meta_t0
    log.info("[METADATA] Complete: %d/%d succeeded in %.1f s (%.2f s/game avg)",
             count, len(games), elapsed,
             elapsed / len(games) if games else 0)
    return count
