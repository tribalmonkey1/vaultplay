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
Steam: reads the 'movies' array of Steam's appdetails response via
steam_store.get_app_details() — a shared, cached call, so trailer + every
movie + every screenshot (+ the Steam description/date fill) cost ONE
request per game instead of one each: HLS/DASH stream URLs (formerly direct mp4/webm), the
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

import json
import logging
import os
import re
import shutil
import subprocess
import time
from typing import Optional

import requests
import requests.adapters

import steam_store

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


# ── Diagnostics ────────────────────────────────────────────────────────────
# Why did a lookup find nothing? Silent debug logging made "0 trailers"
# impossible to tell apart from "yt-dlp missing" / "GOG blocked" / "GOG has
# no videos". Every tier bumps a counter here; the Settings backfill shows
# diagnostics_summary() after a run. last_errors keeps the most recent
# failure message per tier so a real error string can be shown/logged.

import collections as _collections

_DIAG = _collections.Counter()
_GOG_MISMATCH_SAMPLES: list = []
_LAST_ERR: dict = {}


def _note(key: str, err=None):
    _DIAG[key] += 1
    if err is not None:
        first = key not in _LAST_ERR
        _LAST_ERR[key] = str(err)[:200]
        if first:
            log.warning("trailers: %s — %s", key, _LAST_ERR[key])


def reset_diagnostics():
    _DIAG.clear()
    _LAST_ERR.clear()
    _GOG_MISMATCH_SAMPLES.clear()


def diagnostics_summary() -> str:
    """One-line human summary of how each tier did since reset_diagnostics()."""
    d = _DIAG
    parts = [f"Steam {d['steam_hit']}"]
    gog = f"GOG {d['gog_hit']}"
    gog_bits = []
    if d["gog_error"]:
        gog_bits.append(f"{d['gog_error']} request errors")
    if d["gog_no_results"]:
        gog_bits.append(f"{d['gog_no_results']} not on GOG")
    if d["gog_mismatch"]:
        gog_bits.append(f"{d['gog_mismatch']} search results didn't match the title")
    if d["gog_no_videos"]:
        gog_bits.append(f"{d['gog_no_videos']} listed w/o videos")
    if d["gog_unparsed"]:
        gog_bits.append(f"{d['gog_unparsed']} unrecognized video shape")
    if gog_bits:
        gog += " (" + ", ".join(gog_bits) + ")"
    parts.append(gog)
    if d["yt_missing"]:
        parts.append("YouTube skipped (yt-dlp not installed)")
    else:
        yt = f"YouTube {d['yt_hit']}"
        yt_bits = []
        if d["yt_error"]:
            yt_bits.append(f"{d['yt_error']} search errors")
        if d["yt_empty"]:
            yt_bits.append(f"{d['yt_empty']} empty")
        if yt_bits:
            yt += " (" + ", ".join(yt_bits) + ")"
        parts.append(yt)
    out = " · ".join(parts)
    if _GOG_MISMATCH_SAMPLES:
        ex = "; ".join(f"'{t}' → {got}" for t, got in _GOG_MISMATCH_SAMPLES[:3])
        out += f"  —  GOG title-mismatch examples: {ex}"
    errs = [f"{k}: {v}" for k, v in _LAST_ERR.items()]
    if errs:
        out += "  —  last errors: " + "; ".join(errs)
    return out


# ── Steam ──────────────────────────────────────────────────────────────────

def _movie_url(movie: dict) -> Optional[str]:
    """
    Best playable URL from one Steam appdetails 'movies' entry.

    Steam dropped the old direct 'mp4'/'webm' links from appdetails (observed
    2026-10-09: entries now carry only 'dash_av1', 'dash_h264', 'hls_h264' and
    a thumbnail). Legacy mp4/webm are still honoured first in case an old or
    cached response has them, then the adaptive-stream URLs. hls_h264 (a single
    .m3u8 master playlist) is preferred — mpv, VLC and ffmpeg-backed Qt
    playback all handle it, and it's one self-contained URL.
    """
    for key in ("mp4", "webm"):
        d = movie.get(key)
        if isinstance(d, dict):
            u = d.get("max") or d.get("480")
            if u:
                return u
        elif isinstance(d, str) and d:
            return d
    for key in ("hls_h264", "dash_h264", "dash_av1"):
        u = movie.get(key)
        if u and isinstance(u, str):
            return u
    return None


def fetch_steam_trailer(steam_app_id: int) -> Optional[dict]:
    """
    Best trailer for a Steam app id, straight from the 'movies' array in
    Steam's public appdetails response (via steam_store.get_app_details(),
    already called elsewhere for redistributable detection).

    Returns {"url", "thumbnail", "source": "steam"} or None if the game has
    no Steam page, or has a page but no movies listed. Never raises.
    """
    if not steam_app_id:
        return None
    try:
        details = steam_store.get_app_details(steam_app_id)
    except Exception as e:
        log.debug("trailers: steam_store.get_app_details failed for app_id=%s: %s",
                  steam_app_id, e)
        return None
    if not details:
        return None
    movies = details.get("movies") or []
    if not movies:
        return None
    # Prefer the entry Steam flags as the highlight (its main trailer), else
    # the first one that yields a usable URL.
    ordered = sorted(movies, key=lambda m: not m.get("highlight"))
    for movie in ordered:
        url = _movie_url(movie)
        if url:
            return {"url": url, "thumbnail": movie.get("thumbnail"),
                    "source": "steam"}
    return None


def fetch_steam_movies(steam_app_id: int) -> list:
    """
    EVERY trailer/movie for a Steam app id, in Steam's own listed order —
    for the media gallery's video slots. (fetch_steam_trailer() above only
    returns the single best one, for the primary trailer_url column.)

    Returns a list of {"url", "thumbnail"} dicts, [] on any failure or if
    the game has no movies. Never raises.
    """
    if not steam_app_id:
        return []
    try:
        details = steam_store.get_app_details(steam_app_id)
    except Exception as e:
        log.debug("trailers: steam_store.get_app_details failed for app_id=%s: %s",
                  steam_app_id, e)
        return []
    if not details:
        return []
    results = []
    for movie in (details.get("movies") or []):
        url = _movie_url(movie)
        if url:
            results.append({"url": url, "thumbnail": movie.get("thumbnail")})
    return results


def fetch_steam_screenshots(steam_app_id: int) -> list:
    """
    EVERY screenshot from the same appdetails response, for the media
    gallery (Steam-sourced screenshots take priority over the IGDB
    screenshots path when available).

    Returns a list of {"url", "thumbnail"} dicts — "url" is the full-size
    image (shown in the gallery's big preview), "thumbnail" is Steam's own
    much smaller path_thumbnail (used for the thumbnail strip, so the strip
    doesn't have to download every full-size 1080p image up front). [] on
    any failure or if the game has none. Never raises.
    """
    if not steam_app_id:
        return []
    try:
        details = steam_store.get_app_details(steam_app_id)
    except Exception as e:
        log.debug("trailers: steam_store.get_app_details failed for app_id=%s: %s",
                  steam_app_id, e)
        return []
    if not details:
        return []
    results = []
    for shot in (details.get("screenshots") or []):
        full = shot.get("path_full") or shot.get("path_thumbnail")
        if full:
            results.append({"url": full,
                            "thumbnail": shot.get("path_thumbnail") or full})
    return results


def build_media_gallery(steam_app_id: Optional[int], igdb_screenshots: list,
                        primary_trailer: Optional[dict]) -> list:
    """
    Build the ordered media-gallery list stored by db.set_media_gallery()
    and rendered by game_detail.py's MediaGalleryWidget — a list of
    {"type": "video"|"image", "url", "thumbnail_url", "source", "label"}.

    Steam data wins when there is any: every Steam movie first (Steam's own
    order), then every Steam screenshot. A manually-pinned or GOG/YouTube
    primary trailer is NOT dropped in that case — it is inserted ahead of
    Steam's movies if it isn't one of them already, so a pin always shows
    up as the first video.

    Without Steam data: the video slot is the primary trailer (if any) and
    the image slots are the existing IGDB screenshots, unchanged.

    primary_trailer: {"url", "source", optional "thumbnail"} or None.
    igdb_screenshots: list of plain image URLs (may be empty).
    Never raises; returns [] if there is nothing to show at all.
    """
    gallery: list = []

    steam_movies = fetch_steam_movies(steam_app_id) if steam_app_id else []
    steam_shots  = fetch_steam_screenshots(steam_app_id) if steam_app_id else []

    if steam_movies or steam_shots:
        steam_movie_urls = {m["url"] for m in steam_movies}
        if primary_trailer and primary_trailer.get("url") not in steam_movie_urls:
            gallery.append({
                "type": "video", "url": primary_trailer["url"],
                "thumbnail_url": primary_trailer.get("thumbnail"),
                "source": primary_trailer.get("source") or "youtube",
                "label": "Trailer",
            })
        for m in steam_movies:
            n_videos = sum(1 for g in gallery if g["type"] == "video") + 1
            gallery.append({
                "type": "video", "url": m["url"],
                "thumbnail_url": m.get("thumbnail"), "source": "steam",
                "label": "Trailer" if n_videos == 1 else f"Trailer {n_videos}",
            })
        for i, s in enumerate(steam_shots):
            gallery.append({
                "type": "image", "url": s["url"],
                "thumbnail_url": s["thumbnail"], "source": "steam",
                "label": f"Screenshot {i + 1}",
            })
        return gallery

    if primary_trailer and primary_trailer.get("url"):
        gallery.append({
            "type": "video", "url": primary_trailer["url"],
            "thumbnail_url": primary_trailer.get("thumbnail"),
            "source": primary_trailer.get("source") or "youtube",
            "label": "Trailer",
        })

    for i, url in enumerate(igdb_screenshots or []):
        if url:
            gallery.append({
                "type": "image", "url": url, "thumbnail_url": url,
                "source": "igdb", "label": f"Screenshot {i + 1}",
            })

    return gallery


# ── GOG ───────────────────────────────────────────────────────────────────

_EDITION_WORDS = {
    "edition", "goty", "remastered", "remaster", "definitive", "complete",
    "deluxe", "ultimate", "enhanced", "gold", "anniversary", "hd",
}


def _norm_title(t: str) -> str:
    """Loose comparison key: lowercase, drop (..)/[..] tags (scene/version
    junk), '&'→'and', punctuation out, leading 'the' and edition words
    ('GOTY', 'Remastered', 'Definitive'...) out."""
    t = (t or "").lower()
    t = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", t)
    t = t.replace("&", " and ")
    t = t.replace("game of the year", " ")
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    words = [w for w in t.split() if w not in _EDITION_WORDS]
    if words and words[0] == "the":
        words = words[1:]
    return " ".join(words)


def _gog_search_product_id(title: str) -> Optional[int]:
    try:
        resp = SESSION.get(GOG_SEARCH_URL,
                           params={"mediaType": "game", "search": title},
                           timeout=10)
        resp.raise_for_status()
        data = resp.json()
        resp.close()
    except Exception as e:
        _note("gog_error", e)
        return None
    products = data.get("products") or []
    if not products:
        _DIAG["gog_no_results"] += 1
        return None
    want = _norm_title(title)
    # Only accept a product whose title actually matches ours — GOG's search
    # is fuzzy, and taking products[0] blindly would attach another game's
    # trailer. Exact normalized match first, then a prefix match either way.
    best = None
    for prod in products:
        got = _norm_title(prod.get("title"))
        if got == want:
            best = prod
            break
        if best is None and want and got and (got.startswith(want) or want.startswith(got)):
            best = prod
    if not best:
        _DIAG["gog_mismatch"] += 1
        if len(_GOG_MISMATCH_SAMPLES) < 4:
            _GOG_MISMATCH_SAMPLES.append(
                (title, [str(p.get("title")) for p in products[:3]]))
        return None
    return best.get("id")


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
        log.warning("trailers: unrecognized GOG video entry shape, keys=%s "
                    "(please report — parser needs updating)", list(entry.keys()))
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
        _note("gog_error", e)
        return None
    videos = data.get("videos") or []
    if not videos:
        _DIAG["gog_no_videos"] += 1
        return None
    for entry in videos:
        parsed = _parse_gog_video(entry)
        if parsed:
            _DIAG["gog_hit"] += 1
            return parsed
    _DIAG["gog_unparsed"] += 1
    return None


# ── YouTube fallback (yt-dlp) ────────────────────────────────────────────

def _youtube_thumbnail(video_id: str) -> str:
    return f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"


_ytdlp_missing_logged = False


def _find_ytdlp_cli() -> Optional[str]:
    """Path to a yt-dlp executable. Many setups (Arch's /usr/local/bin/yt-dlp,
    a standalone release binary, pipx) have the CLI but NOT an importable
    Python module in VaultPlay's own interpreter — so the CLI counts too.
    Also checks the usual bin dirs explicitly, since an AppImage launched
    from a desktop entry can have a trimmed PATH."""
    extra = ["/usr/local/bin", "/usr/bin", "/bin",
             os.path.expanduser("~/.local/bin")]
    path = os.pathsep.join([os.environ.get("PATH", "")] + extra)
    return shutil.which("yt-dlp", path=path)


def _clean_subprocess_env() -> dict:
    """Environment for running the system yt-dlp from inside an AppImage:
    the AppImage's bundled Python/libs (PYTHONHOME, LD_LIBRARY_PATH, ...)
    must not leak into the user's own yt-dlp script."""
    env = dict(os.environ)
    if env.get("APPDIR"):
        for k in ("PYTHONHOME", "PYTHONPATH", "LD_LIBRARY_PATH", "LD_PRELOAD"):
            env.pop(k, None)
    return env


def _ytdlp_cli_search(exe: str, query: str, n: int = 5) -> list:
    """Run `yt-dlp --flat-playlist -j ytsearchN:query`, return entry dicts.
    Raises RuntimeError with stderr's tail if it fails and printed nothing."""
    proc = subprocess.run(
        [exe, "--flat-playlist", "-j", "--no-warnings", "--skip-download",
         f"ytsearch{n}:{query}"],
        capture_output=True, text=True, timeout=60,
        env=_clean_subprocess_env())
    entries = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
    if not entries and proc.returncode != 0:
        err_lines = (proc.stderr or "").strip().splitlines()
        raise RuntimeError(err_lines[-1] if err_lines
                           else f"yt-dlp exited {proc.returncode}")
    return entries


def ytdlp_available() -> bool:
    """True if yt-dlp is usable either as an importable module or as an
    executable on the system (the YouTube fallback tier needs one)."""
    try:
        import yt_dlp  # noqa: F401
        return True
    except ImportError:
        return _find_ytdlp_cli() is not None


def search_youtube_candidates(title: str, developer: str = "",
                              publisher: str = "") -> list:
    """
    yt-dlp-backed search (Python module if importable, otherwise the
    yt-dlp executable). Returns a ranked list of
    {"url", "thumbnail", "title", "channel", "source": "youtube", "score"}
    dicts, highest score first — the full list is what the manual picker
    dialog shows; fetch_youtube_trailer() below just takes the top one.

    Never raises. yt-dlp not being available, or every search failing, both
    just mean an empty list.
    """
    global _ytdlp_missing_logged
    ydl = None
    exe = None
    try:
        import yt_dlp
        ydl = yt_dlp.YoutubeDL({
            "quiet": True, "no_warnings": True, "extract_flat": True,
            "skip_download": True,
        })
    except ImportError:
        exe = _find_ytdlp_cli()
    except Exception as e:
        _note("yt_error", e)
        return []

    if ydl is None and exe is None:
        _DIAG["yt_missing"] += 1
        if not _ytdlp_missing_logged:
            log.info("trailers: yt-dlp not found (neither Python module nor "
                     "executable) — skipping YouTube fallback")
            _ytdlp_missing_logged = True
        return []

    if not title:
        return []

    def run_search(query: str) -> list:
        if ydl is not None:
            result = ydl.extract_info(f"ytsearch5:{query}", download=False)
            return (result or {}).get("entries") or []
        return _ytdlp_cli_search(exe, query, 5)

    queries = [f"{title} official trailer"]
    if publisher:
        queries.append(f"{publisher} {title} trailer")
    if developer and developer != publisher:
        queries.append(f"{developer} {title} trailer")

    known_channel_ids = set(PLATFORM_YOUTUBE_CHANNELS.values())
    seen_ids: set = set()
    candidates: list = []

    try:
        for i, query in enumerate(queries):
            try:
                entries = run_search(query)
            except Exception as e:
                _note("yt_error", e)
                continue
            for entry in entries:
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
                    "channel":   entry.get("channel") or entry.get("uploader") or "",
                    "source":    "youtube",
                    "score":     score,
                })
            # Same politeness spirit as metadata.py's 0.4s inter-request
            # sleep — this is several searches per game, not one.
            if i < len(queries) - 1:
                time.sleep(0.3)
    except Exception as e:
        _note("yt_error", e)
        return []

    candidates.sort(key=lambda c: -c["score"])
    if candidates:
        _DIAG["yt_hit"] += 1
    else:
        _DIAG["yt_empty"] += 1
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
            _DIAG["steam_hit"] += 1
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
