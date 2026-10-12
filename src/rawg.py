"""
rawg.py — RAWG (rawg.io) metadata for VaultPlay: a free-key gap-filler for
descriptions, release dates, genres, developers and publishers, used after
IGDB and Steam. Needs only a free API key (Settings → API Keys); nothing is
submitted anywhere.

RAWG's free-tier terms (from their API docs page, checked 2026-10-09): up to
20,000 requests/month, personal/hobby projects, an active backlink to RAWG
wherever its data is shown, and no redistribution of the data. Storing results
in the user's own local database for their own library view is the intended
use; game_detail.py shows the required "provided by RAWG" credit link.

Matching: search by cleaned title, then ONLY accept a result whose normalised
title equals ours (preferring one released on the game's platform when the
library category tells us what that is). A loose/"closest" match is never
taken — a wrong game's description is worse than an empty field.

No Qt — pure logic. Never raises to callers except RawgAuthError, which the
Settings backfill uses to stop early on a bad key.
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
import logging
import re
import time
from typing import Optional

import requests
import requests.adapters

import gamematch

log = logging.getLogger(__name__)

RAWG_BASE = "https://api.rawg.io/api"

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "VaultPlay/1.0"
SESSION.mount("https://", requests.adapters.HTTPAdapter(
    pool_connections=2, pool_maxsize=2, max_retries=1))

stats = {"requests": 0, "rate_limited": 0, "errors": 0, "no_match": 0}


class RawgAuthError(Exception):
    """The API key was rejected (HTTP 401/403) — no point continuing a batch."""


def _get(path: str, key: str, params: dict) -> Optional[dict]:
    params = dict(params, key=key)
    for attempt in range(2):
        try:
            stats["requests"] += 1
            resp = SESSION.get(f"{RAWG_BASE}{path}", params=params, timeout=15)
            if resp.status_code in (401, 403):
                resp.close()
                raise RawgAuthError(f"RAWG rejected the API key (HTTP {resp.status_code})")
            if resp.status_code == 429:
                resp.close()
                stats["rate_limited"] += 1
                time.sleep(3 * (attempt + 1))
                continue
            resp.raise_for_status()
            data = resp.json()
            resp.close()
            return data
        except RawgAuthError:
            raise
        except Exception as e:
            stats["errors"] += 1
            log.warning("rawg: request %s failed: %s", path, e)
            return None
    return None


# ── Matching ──────────────────────────────────────────────────────────────────

def _result_platform_names(result: dict) -> set:
    names = set()
    for p in (result.get("platforms") or []):
        name = ((p or {}).get("platform") or {}).get("name")
        if name:
            names.add(gamematch.norm_title(name))
    return names


def pick_result(results: list, title: str, category: Optional[str] = None) -> Optional[dict]:
    """Best acceptable search result, or None. Exact normalised-title match
    required; among exact matches prefer one on the game's platform, then the
    one RAWG ranked first."""
    want = gamematch.norm_title(title)
    if not want:
        return None
    wanted_platforms = {gamematch.norm_title(n) for n in gamematch.rawg_platform_names(category)}
    exact = [r for r in (results or [])
             if gamematch.norm_title(r.get("name")) == want]
    if not exact:
        return None
    if wanted_platforms:
        for r in exact:
            if _result_platform_names(r) & wanted_platforms:
                return r
    return exact[0]


def search_game(title: str, key: str, category: Optional[str] = None) -> Optional[int]:
    """RAWG game id for a title (+ optional library category), or None."""
    if not title or not key:
        return None
    data = _get("/games", key, {"search": title, "search_precise": "true",
                                "page_size": 10})
    if not data:
        return None
    best = pick_result(data.get("results") or [], title, category)
    if not best:
        stats["no_match"] += 1
        return None
    return best.get("id")


# ── Field extraction ─────────────────────────────────────────────────────────

def _format_date(raw: str) -> Optional[str]:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.datetime.strptime(raw, "%Y-%m-%d").strftime("%B %d, %Y")
    except ValueError:
        return raw


def _clean_description(text: str) -> str:
    """RAWG's description_raw is plain text but often appends the same blurb
    in other languages after a bare 'Español' (etc.) heading — keep only the
    English part, and cap the length."""
    text = (text or "").replace("\r", "").strip()
    text = re.split(r"\n\s*(?:Español|Español|Deutsch|Français|Italiano|Português|Русский)\s*\n",
                    text, maxsplit=1)[0]
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > 2000:
        text = text[:2000].rsplit(" ", 1)[0] + "…"
    return text


def extract_fields(details: Optional[dict]) -> dict:
    """RAWG game-detail dict → VaultPlay fields (only keys with real values)."""
    out: dict = {}
    if not details:
        return out
    desc = _clean_description(details.get("description_raw") or "")
    if desc:
        out["description"] = desc
    date = _format_date(details.get("released") or "")
    if date:
        out["release_date"] = date
    genres = [g.get("name") for g in (details.get("genres") or [])
              if isinstance(g, dict) and g.get("name")]
    if genres:
        out["genres"] = genres
    devs = [d.get("name") for d in (details.get("developers") or [])
            if isinstance(d, dict) and d.get("name")]
    pubs = [p.get("name") for p in (details.get("publishers") or [])
            if isinstance(p, dict) and p.get("name")]
    if devs:
        out["developer"] = devs[0]
    if pubs:
        out["publisher"] = pubs[0]
    if devs or pubs:
        out["ip_holder"] = devs[0] if devs else pubs[0]
    return out


def fetch_fields(title: str, key: str, category: Optional[str] = None) -> dict:
    """Search + detail fetch → fields dict ({} if no confident match or on
    failure). Raises only RawgAuthError."""
    game_id = search_game(title, key, category)
    if not game_id:
        return {}
    details = _get(f"/games/{game_id}", key, {})
    return extract_fields(details)


def _is_empty(v) -> bool:
    return v is None or (isinstance(v, str) and v.strip() in ("", "[]")) \
        or (isinstance(v, (list, tuple)) and not v)


def fill_gaps(metadata: dict, title: str, key: str,
              category: Optional[str] = None) -> list:
    """In-place: fill only EMPTY description/developer/publisher/ip_holder/
    release_date/genres in `metadata` from RAWG. Returns keys filled. Skips
    the network entirely when nothing is missing."""
    wanted = ("description", "developer", "publisher", "release_date", "genres")
    if not any(_is_empty(metadata.get(k)) for k in wanted):
        return []
    filled = []
    for k, v in fetch_fields(title, key, category).items():
        if _is_empty(metadata.get(k)):
            metadata[k] = v
            filled.append(k)
    return filled
