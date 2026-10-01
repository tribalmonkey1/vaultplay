"""
trailers.py — Automatic trailer detection for VaultPlay

Spec: Notion → Features → Fully Planned → Automatic Trailer Detection & Media Gallery.

Priority chain used by detect_trailer_for_game(): Steam → GOG → YouTube
fallback (yt-dlp search). Manual overrides are never touched here — callers
(metadata.py's pipeline hook, the future Settings backfill action, and a
future wishlist detection pass) are responsible for checking
trailer_manual_override themselves before ever calling into this module;
every function here always returns its best automatic guess for whatever
it's given, with no notion of "should I even be running right now."

Sources
-------
Steam: reuses steamdb.fetch_app_details() (already called there for
redistributable detection — no extra HTTP request beyond what that module
already makes) and reads its 'movies' array: direct mp4/webm CDN URLs, the
only source here that yields a genuinely playable file rather than a
webpage/embed reference. That distinction matters for a future in-app
player — see the Notion page's "Designing for future in-app playback"
section for why trailer_source is worth storing per game even though today
nothing does more than xdg-open the URL.

GOG: two unofficial-but-public endpoints, verified reachable and documented
2026-09-17 (community tools including the live gogdb.org scanner use the
same ones):
  1. https://embed.gog.com/games/ajax/filtered?mediaType=game&search=<title>
     → {"products": [{"id": ..., "title": ..., "slug": ..., "url": ...}, ...]}
  2. https://api.gog.com/products/<id>?expand=videos
     → {..., "videos": [...]}
GOG doesn't self-host trailer files the way Steam does — its own video
entries are themselves just references to a YouTube video, so a GOG hit
and a YouTube hit end up needing the same playback backend later; the only
difference is a GOG hit is a pre-curated *official* video rather than a
ranked search result. The exact shape of a POPULATED 'videos' entry
couldn't be confirmed during the spike (every real example found had an
empty list) — _parse_gog_video() below tries several plausible key names
defensively and logs the raw keys if none match, rather than assuming a
shape that turns out to be wrong. Revisit once a real populated response
has actually been seen in the wild.

YouTube fallback: yt-dlp's search (no API key, no quota needed — see the
Notion page for why this was picked over the official Data API v3), ranked
by known platform channels, then publisher/developer, then a general
search, with a score bump for "official trailer" in the video title.
Requires the `yt-dlp` package — not yet added to install.sh's pip line;
this module degrades to "no YouTube fallback" (logs once, returns [])
rather than raising if it isn't installed.
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

import logging
import time
from typing import Optional

import requests
import requests.adapters

import steamdb

log = logging.getLogger(__name__)

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "VaultPlay/1.0"
_adapter = requests.adapters.HTTPAdapter(pool_connections=2, pool_maxsize=2, max_retries=1)
SESSION.mount("https://", _adapter)
SESSION.mount("http://", _adapter)

GOG_SEARCH_URL  = "https://embed.gog.com/games/ajax/filtered"
GOG_PRODUCT_URL = "https://api.gog.com/products/{id}"

# Known, verified-2026-09-17 official first-party YouTube channel IDs, used
# to rank YouTube fallback search results (see search_youtube_candidates()).
# PlayStation and Nintendo of America confirmed by cross-checking their
# channel "About" pages. Xbox's main channel ID could NOT be confirmed
# during the spike — search only turned up truncated/unverified IDs and the
# "ID@Xbox" indie-only sub-channel, which is deliberately NOT used here
# since it would exclude first-party AAA trailers. Left out rather than
# guessing wrong — a wrong ID would silently and permanently zero out the
# platform-channel ranking bonus for every Xbox game with no error at all.
# Fill in once confirmed; until then Xbox games simply fall through to the
# publisher/developer and general-search tiers below, same as any other
# unmapped platform.
PLATFORM_YOUTUBE_CHANNELS = {
    "playstation": "UC-2Y8dQb0S6DtpxNgAKoJKA",
    "nintendo":    "UCGIY_O-8vW4rfX98KlMkvRg",
}


# ── Steam ──────────────────────────────────────────────────────────────────

def fetch_steam_trailer(steam_app_id: int) -> Optional[dict]:
    """
    Best trailer for a Steam app id, straight from the 'movies' array in
    Steam's public appdetails response (via steamdb.fetch_app_details(),
    already called elsewhere for redistributable detection).

    Returns {"url", "thumbnail", "source": "steam"} or None if the game has
    no Steam page, or has a page but no movies listed. Never raises.
    """
    if not steam_app_id:
        return None
    try:
        details = steamdb.fetch_app_details(steam_app_id)
    except Exception as e:
        log.debug("trailers: steamdb.fetch_app_details failed for app_id=%s: %s",
                  steam_app_id, e)
        return None
    if not details:
        return None
    movies = details.get("movies") or []
    if not movies:
        return None
    movie = movies[0]
    mp4 = movie.get("mp4") or {}
    url = mp4.get("max") or mp4.get("480")
    if not url:
        webm = movie.get("webm") or {}
        url = webm.get("max") or webm.get("480")
    if not url:
        return None
    return {"url": url, "thumbnail": movie.get("thumbnail"), "source": "steam"}


def fetch_steam_screenshots(steam_app_id: int) -> list:
    """
    Full-size screenshot URLs from the same appdetails response, for the
    planned media gallery (Steam-sourced screenshots take priority over the
    existing IGDB-screenshots path when available). Returns [] on any
    failure or if the game has none. Never raises.
    """
    if not steam_app_id:
        return []
    try:
        details = steamdb.fetch_app_details(steam_app_id)
    except Exception as e:
        log.debug("trailers: steamdb.fetch_app_details failed for app_id=%s: %s",
                  steam_app_id, e)
        return []
    if not details:
        return []
    shots = details.get("screenshots") or []
    urls = []
    for shot in shots:
        url = shot.get("path_full") or shot.get("path_thumbnail")
        if url:
            urls.append(url)
    return urls


# ── GOG ───────────────────────────────────────────────────────────────────

def _gog_search_product_id(title: str) -> Optional[int]:
    try:
        resp = SESSION.get(GOG_SEARCH_URL,
                           params={"mediaType": "game", "search": title},
                           timeout=10)
        resp.raise_for_status()
        data = resp.json()
        resp.close()
        products = data.get("products") or []
        if not products:
            return None
        return products[0].get("id")
    except Exception as e:
        log.debug("trailers: GOG search failed for '%s': %s", title, e)
        return None


def _parse_gog_video(entry: dict) -> Optional[dict]:
    """Best-effort extraction from one GOG 'videos' entry. Shape not
    confirmed against a real populated response during the 2026-09-17
    spike (see module docstring) — tries several plausible key names and
    gives up quietly, logging what keys WERE actually present, rather than
    raising or guessing wrong."""
    if not isinstance(entry, dict):
        return None
    url = entry.get("video_url") or entry.get("url") or entry.get("youtube_url")
    if not url and entry.get("youtube_id"):
        url = f"https://www.youtube.com/watch?v={entry['youtube_id']}"
    if not url:
        log.debug("trailers: unrecognized GOG video entry shape, keys=%s",
                  list(entry.keys()))
        return None
    thumbnail = entry.get("thumbnail_url") or entry.get("thumbnail") or entry.get("image")
    return {"url": url, "thumbnail": thumbnail, "source": "gog"}


def fetch_gog_trailer(title: str) -> Optional[dict]:
    """
    Best trailer for a game, searched by title against GOG's catalog.
    Returns {"url", "thumbnail", "source": "gog"} or None — no GOG listing
    found, the listing has no videos, or the video shape wasn't
    recognized (see _parse_gog_video()). Never raises.
    """
    if not title:
        return None
    product_id = _gog_search_product_id(title)
    if not product_id:
        return None
    try:
        resp = SESSION.get(GOG_PRODUCT_URL.format(id=product_id),
                           params={"expand": "videos"}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        resp.close()
    except Exception as e:
        log.debug("trailers: GOG product fetch failed for id=%s ('%s'): %s",
                  product_id, title, e)
        return None
    videos = data.get("videos") or []
    for entry in videos:
        parsed = _parse_gog_video(entry)
        if parsed:
            return parsed
    return None


# ── YouTube fallback (yt-dlp) ────────────────────────────────────────────

def _youtube_thumbnail(video_id: str) -> str:
    return f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"


_ytdlp_missing_logged = False


def search_youtube_candidates(title: str, developer: str = "",
                              publisher: str = "") -> list:
    """
    yt-dlp-backed search. Returns a ranked list of
    {"url", "thumbnail", "title", "channel", "source": "youtube", "score"}
    dicts, highest score first — the full list is what a future manual
    picker dialog would show; fetch_youtube_trailer() below just takes the
    top one.

    Never raises. yt-dlp not being installed, or every search failing, both
    just mean an empty list — caller treats that the same as "no YouTube
    candidates found" either way.
    """
    global _ytdlp_missing_logged
    try:
        import yt_dlp
    except ImportError:
        if not _ytdlp_missing_logged:
            log.info("trailers: yt-dlp not installed — skipping YouTube "
                     "fallback (pip install yt-dlp --break-system-packages)")
            _ytdlp_missing_logged = True
        return []

    if not title:
        return []

    ydl_opts = {
        "quiet": True, "no_warnings": True, "extract_flat": True,
        "skip_download": True, "default_search": "ytsearch5",
    }

    queries = [f"{title} official trailer"]
    if publisher:
        queries.append(f"{publisher} {title} trailer")
    if developer and developer != publisher:
        queries.append(f"{developer} {title} trailer")

    known_channel_ids = set(PLATFORM_YOUTUBE_CHANNELS.values())
    seen_ids: set = set()
    candidates: list = []

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            for i, query in enumerate(queries):
                try:
                    result = ydl.extract_info(query, download=False)
                except Exception as e:
                    log.debug("trailers: yt-dlp search failed for %r: %s", query, e)
                    continue
                for entry in ((result or {}).get("entries") or []):
                    if not entry:
                        continue
                    video_id = entry.get("id")
                    if not video_id or video_id in seen_ids:
                        continue
                    seen_ids.add(video_id)
                    vtitle = entry.get("title") or ""
                    channel_id = entry.get("channel_id") or ""

                    score = 0
                    if channel_id in known_channel_ids:
                        score += 30
                    if "official trailer" in vtitle.lower():
                        score += 10

                    candidates.append({
                        "url":       f"https://www.youtube.com/watch?v={video_id}",
                        "thumbnail": _youtube_thumbnail(video_id),
                        "title":     vtitle,
                        "channel":   entry.get("channel") or "",
                        "source":    "youtube",
                        "score":     score,
                    })
                # Same politeness spirit as metadata.py's 0.4s inter-request
                # sleep — this is several searches per game, not one.
                if i < len(queries) - 1:
                    time.sleep(0.3)
    except Exception as e:
        log.warning("trailers: yt-dlp search failed entirely for '%s': %s", title, e)
        return []

    candidates.sort(key=lambda c: -c["score"])
    return candidates


def fetch_youtube_trailer(title: str, developer: str = "",
                          publisher: str = "") -> Optional[dict]:
    """Convenience wrapper: just the top-ranked YouTube candidate, or None."""
    candidates = search_youtube_candidates(title, developer, publisher)
    return candidates[0] if candidates else None


# ── Orchestration ─────────────────────────────────────────────────────────

def detect_trailer_for_game(steam_app_id: Optional[int], title: str,
                            developer: str = "", publisher: str = "") -> Optional[dict]:
    """
    Runs the full priority chain — Steam → GOG → YouTube fallback — and
    returns the first hit. Manual overrides are the caller's job; this
    function has no notion of them and always tries every tier it's given
    enough information to try.

    Returns {"url", "thumbnail", "source"} or None if nothing was found
    anywhere. Never raises.
    """
    if steam_app_id:
        result = fetch_steam_trailer(steam_app_id)
        if result:
            log.info("trailers: '%s' (app %s) \u2192 Steam trailer", title, steam_app_id)
            return result

    result = fetch_gog_trailer(title)
    if result:
        log.info("trailers: '%s' \u2192 GOG trailer", title)
        return result

    result = fetch_youtube_trailer(title, developer, publisher)
    if result:
        log.info("trailers: '%s' \u2192 YouTube trailer (score=%s, channel=%s)",
                 title, result.get("score"), result.get("channel"))
        return result

    log.info("trailers: '%s' \u2014 no trailer found on any source", title)
    return None


def get_media_options(steam_app_id: Optional[int], title: str,
                      developer: str = "", publisher: str = "") -> dict:
    """
    Full-list-not-best-guess variant for a future manual picker dialog
    (mirrors metadata.sgdb_get_art_options()'s contract) — every candidate
    from every source, not just the winner, grouped by source so a picker
    can show them side by side.

    Returns {"steam": dict|None, "gog": dict|None, "youtube": list[dict]}.
    Never raises.
    """
    return {
        "steam":   fetch_steam_trailer(steam_app_id) if steam_app_id else None,
        "gog":     fetch_gog_trailer(title),
        "youtube": search_youtube_candidates(title, developer, publisher),
    }
