"""
steam_store.py — Steam store data for VaultPlay (descriptions, dates, genres…)

Two jobs:

1. get_app_details(app_id): ONE shared, cached call to Steam's public
   appdetails endpoint. Trailer detection (trailers.py) used to hit that
   endpoint 3 separate times per game (primary trailer, every movie, every
   screenshot) on top of the redistributable pass — and Steam rate-limits it
   (HTTP 429, roughly 200 requests per 5 minutes), so a library-wide
   backfill could silently lose games to throttling. Everything new goes
   through this cache instead: one request per game, a short back-off retry
   when Steam says 429, and failures are never cached.

2. extract_fields() / fill_gaps(): turn that response into VaultPlay's
   metadata fields (description, developer, publisher, release_date, genres)
   and fill ONLY the ones other sources (IGDB) left empty. Steam never
   overwrites anything already present.

No Qt here — pure logic, same split as trailers.py / protondb.py.
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
import html
import logging
import re
import threading
import time
from typing import Optional

import requests
import requests.adapters

log = logging.getLogger(__name__)

STEAM_APPDETAILS = "https://store.steampowered.com/api/appdetails"

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "VaultPlay/1.0"
_adapter = requests.adapters.HTTPAdapter(pool_connections=2, pool_maxsize=2, max_retries=1)
SESSION.mount("https://", _adapter)

_CACHE_TTL = 600          # seconds
_CACHE_MAX = 256
_cache: dict = {}         # app_id -> (timestamp, details dict | None)
_cache_lock = threading.Lock()
_MISSING = object()

# Observed/diagnostic counters, same idea as trailers.py's.
stats = {"requests": 0, "cache_hits": 0, "rate_limited": 0, "errors": 0}


def clear_cache():
    with _cache_lock:
        _cache.clear()


def get_app_details(steam_app_id: int) -> Optional[dict]:
    """
    Steam appdetails 'data' dict for an app id, or None (no such app / not
    available / request failed). Cached for 10 minutes — including the
    "Steam says this app doesn't exist" answer, but NOT network failures or
    rate limiting, so a retry later actually retries.

    'filters' is deliberately not sent: using it makes Steam return empty
    fields with HTTP 200 (see steamdb.py's note).
    """
    if not steam_app_id:
        return None
    key = int(steam_app_id)
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key, _MISSING)
        if hit is not _MISSING and now - hit[0] < _CACHE_TTL:
            stats["cache_hits"] += 1
            return hit[1]

    details = _MISSING
    for attempt in range(3):
        try:
            stats["requests"] += 1
            resp = SESSION.get(
                STEAM_APPDETAILS,
                params={"appids": key, "cc": "US", "l": "english"},
                timeout=12)
            if resp.status_code == 429:
                resp.close()
                stats["rate_limited"] += 1
                wait = 4 * (attempt + 1)
                log.warning("steam_store: rate limited on app %s — waiting %ds",
                            key, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            resp.close()
            entry = data.get(str(key)) or {}
            details = entry.get("data") if entry.get("success") else None
            break
        except Exception as e:
            stats["errors"] += 1
            log.warning("steam_store: appdetails failed for %s: %s", key, e)
            break

    if details is _MISSING:      # network error / still rate-limited — don't cache
        return None
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            _cache.pop(next(iter(_cache)))
        _cache[key] = (time.time(), details)
    return details


# ── Field extraction ──────────────────────────────────────────────────────────

_DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%d %b, %Y", "%d %B, %Y", "%d %b %Y")
_MONTH_YEAR_FORMATS = ("%b %Y", "%B %Y")


def _plain(text: str) -> str:
    text = re.sub(r"<br\s*/?>|</p>|</li>|</h\d>", "\n", text or "", flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    return text.strip()


def _format_release_date(raw: str) -> Optional[str]:
    """Steam's free-text date → the same 'November 01, 2007' shape IGDB
    results are stored in. Unparseable values are kept verbatim rather than
    dropped (e.g. 'Q4 2026'); empty/'Coming soon' placeholders → None."""
    raw = (raw or "").strip()
    if not raw or raw.lower() in ("coming soon", "to be announced", "tba"):
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.datetime.strptime(raw, fmt).strftime("%B %d, %Y")
        except ValueError:
            pass
    for fmt in _MONTH_YEAR_FORMATS:
        try:
            return datetime.datetime.strptime(raw, fmt).strftime("%B %Y")
        except ValueError:
            pass
    return raw


def extract_fields(details: Optional[dict]) -> dict:
    """
    Map a Steam appdetails dict to VaultPlay metadata fields. Only keys with
    real values are present, so callers can use `in`/`.get()` to see what
    Steam actually had:
      description, developer, publisher, ip_holder, release_date, genres
    """
    out: dict = {}
    if not details:
        return out

    desc = _plain(details.get("short_description") or "")
    if not desc:
        about = _plain(details.get("about_the_game") or "")
        if about:
            desc = about if len(about) <= 700 else about[:700].rsplit(" ", 1)[0] + "…"
    if desc:
        out["description"] = desc

    devs = [d for d in (details.get("developers") or []) if d]
    pubs = [p for p in (details.get("publishers") or []) if p]
    if devs:
        out["developer"] = devs[0]
    if pubs:
        out["publisher"] = pubs[0]
    if devs or pubs:
        out["ip_holder"] = devs[0] if devs else pubs[0]

    rd = details.get("release_date") or {}
    if isinstance(rd, dict) and not rd.get("coming_soon"):
        date = _format_release_date(rd.get("date") or "")
        if date:
            out["release_date"] = date

    genres = [g.get("description") for g in (details.get("genres") or [])
              if isinstance(g, dict) and g.get("description")]
    if genres:
        out["genres"] = genres
    return out


def fetch_fields(steam_app_id: int) -> dict:
    """get_app_details() + extract_fields(). {} on any failure. Never raises."""
    try:
        return extract_fields(get_app_details(steam_app_id))
    except Exception as e:
        log.debug("steam_store: fetch_fields failed for %s: %s", steam_app_id, e)
        return {}


def _is_empty(v) -> bool:
    return v is None or (isinstance(v, str) and v.strip() in ("", "[]")) \
        or (isinstance(v, (list, tuple)) and not v)


def fill_gaps(metadata: dict, steam_app_id: int) -> list:
    """
    In-place: fill any EMPTY description/developer/publisher/ip_holder/
    release_date/genres in `metadata` (the dict metadata.py builds from
    SGDB + IGDB before saving) from Steam. Anything already present is left
    exactly as it is. Returns the list of keys that were filled.
    """
    filled = []
    for k, v in fetch_fields(steam_app_id).items():
        if _is_empty(metadata.get(k)):
            metadata[k] = v
            filled.append(k)
    return filled
