"""
steamdb.py — Redistributable detection for VaultPlay

Detection strategy (in priority order):
  1. redists.py       — writable ~/.config/vaultplay/redists.json, populated by SteamCMD
  2. appinfo.vdf      — opportunistic: Steam client cache for owned/browsed games
  3. pc_requirements  — Steam appdetails API, catches DirectX version etc.
  4. Engine hints     — fallback from short_description/genres (last resort)
  5. Baseline         — only when nothing found at all

The 'filters' param is intentionally omitted from the appdetails API call —
using it causes Steam to silently return empty fields for name, pc_requirements
etc. while still returning HTTP 200.
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
import re
from typing import Optional

import requests
import requests.adapters

log = logging.getLogger(__name__)

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "VaultPlay/1.0"
_adapter = requests.adapters.HTTPAdapter(
    pool_connections=2, pool_maxsize=2, max_retries=1)
SESSION.mount("https://", _adapter)
SESSION.mount("http://", _adapter)

STEAM_APPDETAILS = "https://store.steampowered.com/api/appdetails"

# ── Text-based redistributable patterns ──────────────────────────────────────
# Applied against pc_requirements HTML.
# Ordered most-specific to least-specific so year-specific patterns fire
# before the generic catch-alls.
REDIST_TEXT_PATTERNS = [
    # Visual C++ — year-specific first.
    # Handles all common formats:
    #   "Visual C++ 2013"
    #   "Visual C++ Redistributable 2013"
    #   "Visual C++ Redistributable for Visual Studio 2013"
    #   "vcredist 2013"
    (re.compile(r"(?:visual c\+\+|vc ?redist)(?:[\w\s]*?)2005", re.I), ["vcrun2005"]),
    (re.compile(r"(?:visual c\+\+|vc ?redist)(?:[\w\s]*?)2008", re.I), ["vcrun2008"]),
    (re.compile(r"(?:visual c\+\+|vc ?redist)(?:[\w\s]*?)2010", re.I), ["vcrun2010"]),
    (re.compile(r"(?:visual c\+\+|vc ?redist)(?:[\w\s]*?)2012", re.I), ["vcrun2012"]),
    (re.compile(r"(?:visual c\+\+|vc ?redist)(?:[\w\s]*?)2013", re.I), ["vcrun2013"]),
    (re.compile(r"(?:visual c\+\+|vc ?redist)(?:[\w\s]*?)2015", re.I), ["vcrun2015"]),
    (re.compile(r"(?:visual c\+\+|vc ?redist)(?:[\w\s]*?)2017", re.I), ["vcrun2017"]),
    (re.compile(r"(?:visual c\+\+|vc ?redist)(?:[\w\s]*?)2019", re.I), ["vcrun2019"]),
    (re.compile(r"(?:visual c\+\+|vc ?redist)(?:[\w\s]*?)2022", re.I), ["vcrun2022"]),
    # Generic VC++ with no year in the next ~40 chars — only fires when no
    # year-specific pattern matched, suppressed otherwise by caller logic
    (re.compile(r"(?:visual c\+\+|vc ?redist)(?![\w\s]{0,40}20\d\d)", re.I),
     ["vcrun2019", "vcrun2022"]),
    # .NET — version-specific first
    (re.compile(r"\.net.*?3\.5|dotnet.*?35", re.I),   ["dotnet35"]),
    (re.compile(r"\.net.*?4\.0|dotnet.*?40", re.I),   ["dotnet40"]),
    (re.compile(r"\.net.*?4\.5|dotnet.*?45", re.I),   ["dotnet45"]),
    (re.compile(r"\.net.*?4\.8|dotnet.*?48", re.I),   ["dotnet48"]),
    (re.compile(r"\.net.*?6\.|dotnet.*?6\.", re.I),   ["dotnet48"]),
    (re.compile(r"\.net.*?7\.|dotnet.*?7\.", re.I),   ["dotnet48"]),
    (re.compile(r"\.net.*?8\.|dotnet.*?8\.", re.I),   ["dotnet48"]),
    (re.compile(r"\.net framework|dotnet", re.I),     ["dotnet48"]),
    # DirectX — handles both "DirectX 11" and "DirectX: Version 11"
    (re.compile(r"directx[\s:]*(?:version\s*)?9|dx\s*9|d3d\s*9",    re.I), ["d3dx9"]),
    (re.compile(r"directx[\s:]*(?:version\s*)?10|dx\s*10|d3d\s*10", re.I), ["d3dx10", "d3dcompiler_47"]),
    (re.compile(r"directx[\s:]*(?:version\s*)?11|dx\s*11|d3d\s*11", re.I), ["d3dx11", "d3dcompiler_47"]),
    (re.compile(r"directx[\s:]*(?:version\s*)?12|dx\s*12|d3d\s*12", re.I), ["d3dcompiler_47"]),
    # Generic DirectX — only when no version info follows
    (re.compile(r"directx(?![\s:]*(?:version\s*)?\d)", re.I),
     ["d3dx9", "d3dx11", "d3dcompiler_47"]),
    (re.compile(r"d3dcompiler", re.I),               ["d3dcompiler_47"]),
    (re.compile(r"xna\b|xna game studio", re.I),     ["xact"]),
    (re.compile(r"\bxact\b", re.I),                  ["xact"]),
    (re.compile(r"physx|nvidia physx", re.I),        ["physx"]),
    (re.compile(r"openal", re.I),                    ["openal"]),
    (re.compile(r"microsoft.*?runtime|msvcr", re.I), ["vcrun2019"]),
]

# ── Engine hints ──────────────────────────────────────────────────────────────
# Only used when all other passes find nothing — weakest signal.
ENGINE_HINTS = [
    (re.compile(r"unreal engine|unreal 4|unreal 5|ue4|ue5", re.I),
     ["vcrun2019", "vcrun2022", "d3dcompiler_47"]),
    (re.compile(r"unity\b|unity engine", re.I),
     ["vcrun2019", "vcrun2022"]),
    (re.compile(r"cryengine|cry engine", re.I),
     ["vcrun2019", "d3dx11", "d3dcompiler_47"]),
    (re.compile(r"source engine|source 2", re.I),
     ["vcrun2019", "d3dcompiler_47"]),
    (re.compile(r"gamemaker|game maker studio", re.I),
     ["vcrun2019"]),
    (re.compile(r"rpg maker|rpgmaker", re.I),
     ["vcrun2019", "d3dx9"]),
    (re.compile(r"godot engine", re.I),
     ["vcrun2019"]),
]

# ── Known Steam Common Redist depot IDs ───────────────────────────────────────
# Used for opportunistic appinfo.vdf parsing.
# Source: SteamDB depot page titles, confirmed via SteamCMD output.
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

# ── Baseline ──────────────────────────────────────────────────────────────────
# Only added when ALL detection passes find nothing at all.
# Not unconditionally appended — if we have real data, we trust it.
BASELINE_REDISTS = ["vcrun2019", "vcrun2022", "d3dcompiler_47"]


# ── Pass 0: redists.py (writable config file, populated by SteamCMD) ─────────

def _lookup_redists(steam_app_id: int) -> Optional[list]:
    """
    Look up redistributables via the redists module.
    Returns list of verbs (may be empty = confirmed no redists),
    or None if the app ID is not in the file at all.
    """
    try:
        import redists as redists_mod
        return redists_mod.lookup(steam_app_id)
    except Exception as e:
        log.debug("steamdb: redists lookup failed: %s", e)
        return None


# ── Pass 1: appinfo.vdf ───────────────────────────────────────────────────────

def _find_appinfo_path() -> Optional[Path]:
    candidates = [
        Path.home() / ".steam" / "steam" / "appcache" / "appinfo.vdf",
        Path.home() / ".steam" / "root" / "appcache" / "appinfo.vdf",
        Path.home() / ".local" / "share" / "Steam" / "appcache" / "appinfo.vdf",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def _lookup_appinfo_vdf(steam_app_id: int) -> Optional[list]:
    """
    Opportunistically check appinfo.vdf for depot data.
    Returns list of verbs if found, None if not present or parse fails.
    Uses steam.utils.appcache.parse_appinfo with mapper=dict
    (required for vdf 4.0 compatibility).
    """
    appinfo_path = _find_appinfo_path()
    if not appinfo_path:
        return None

    try:
        from steam.utils.appcache import parse_appinfo
    except ImportError:
        return None

    try:
        with open(appinfo_path, "rb") as f:
            _, apps_iter = parse_appinfo(f, mapper=dict)
            for app in apps_iter:
                if app.get("appid") != steam_app_id:
                    continue
                # Found our app — extract depot data
                data = app.get("data", {})
                appinfo_section = data.get("appinfo", {})
                depots = appinfo_section.get("depots", {})
                if not depots:
                    return None
                found = []
                for depot_id_raw in depots:
                    try:
                        depot_id = int(depot_id_raw)
                    except (ValueError, TypeError):
                        continue
                    if depot_id in KNOWN_REDIST_DEPOTS:
                        for v in KNOWN_REDIST_DEPOTS[depot_id]:
                            if v not in found:
                                found.append(v)
                return found if found else None
    except Exception as e:
        log.debug("steamdb: appinfo.vdf parse failed: %s", e)
    return None


# ── Pass 2: Steam appdetails API ──────────────────────────────────────────────

def fetch_app_details(steam_app_id: int) -> Optional[dict]:
    """
    Fetch app details from Steam public API.
    NOTE: 'filters' param intentionally omitted — using it causes Steam to
    silently return empty fields (name, pc_requirements etc.) with HTTP 200.
    """
    try:
        resp = SESSION.get(
            STEAM_APPDETAILS,
            params={"appids": steam_app_id, "cc": "US", "l": "english"},
            timeout=12
        )
        resp.raise_for_status()
        data = resp.json()
        resp.close()
        key = str(steam_app_id)
        if key in data and data[key].get("success"):
            return data[key]["data"]
    except Exception as e:
        log.warning("Steam appdetails failed for %d: %s", steam_app_id, e)
    return None


def _strip_html(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html or "")


def _match_patterns(text: str, patterns: list) -> list:
    found = []
    for pattern, verbs in patterns:
        if pattern.search(text):
            for v in verbs:
                if v not in found:
                    found.append(v)
    return found


def _detect_from_requirements(details: dict) -> list:
    pc_req = details.get("pc_requirements", {})
    text = ""
    if isinstance(pc_req, dict):
        text = _strip_html(pc_req.get("minimum", "") + " " + pc_req.get("recommended", ""))
    elif isinstance(pc_req, str):
        text = _strip_html(pc_req)
    if not text.strip():
        return []
    return _match_patterns(text, REDIST_TEXT_PATTERNS)


def _detect_from_engine_hints(details: dict) -> list:
    short_desc = details.get("short_description", "") or ""
    genres = " ".join(g.get("description", "") for g in details.get("genres", []))
    return _match_patterns(short_desc + " " + genres, ENGINE_HINTS)


# ── Public API ────────────────────────────────────────────────────────────────

def detect_redists_for_game(steam_app_id: int, game_title: str = "") -> dict:
    """
    Detect redistributables for a game using all available sources.

    Returns:
        {
          "verbs":  list[str]  — final verb list to pre-check in the dialog,
          "source": str        — human-readable description of which pass fired,
        }
    """
    # ── Pass 0: redists.py (SteamCMD-generated data) ─────────────────────────
    json_result = _lookup_redists(steam_app_id)
    if json_result is not None:
        # Entry exists — may be empty list (confirmed no redists) or populated
        if json_result:
            log.info("steamdb: app %d — redists.json hit: %s", steam_app_id, json_result)
            return {
                "verbs":  list(json_result),
                "source": "redists.json (SteamCMD)",
            }
        else:
            # Confirmed no common redist depots — skip all other passes,
            # fall through to baseline at the end
            log.info("steamdb: app %d — redists.json: confirmed no common redists",
                     steam_app_id)
            # Still run pc_requirements for DirectX detection since that's
            # separate from the Common Redist system
            details = fetch_app_details(steam_app_id)
            if details:
                req_verbs = _detect_from_requirements(details)
                if req_verbs:
                    log.info("steamdb: app %d — requirements text adds: %s",
                             steam_app_id, req_verbs)
                    return {
                        "verbs":  req_verbs,
                        "source": "redists.json + requirements text",
                    }
            return {
                "verbs":  [],
                "source": "redists.json (no common redists declared)",
            }

    # App not in redists.json at all — fall through to API-based detection

    # ── Pass 1: appinfo.vdf (opportunistic) ───────────────────────────────────
    vdf_verbs = _lookup_appinfo_vdf(steam_app_id)
    if vdf_verbs:
        log.info("steamdb: app %d — appinfo.vdf hit: %s", steam_app_id, vdf_verbs)
        # Also run requirements text to catch DirectX version
        details = fetch_app_details(steam_app_id)
        req_verbs = _detect_from_requirements(details) if details else []
        combined = list(vdf_verbs)
        for v in req_verbs:
            if v not in combined:
                combined.append(v)
        return {
            "verbs":  combined,
            "source": "appinfo.vdf" + (" + requirements text" if req_verbs else ""),
        }

    # ── Pass 2: pc_requirements text ─────────────────────────────────────────
    details = fetch_app_details(steam_app_id)
    if not details:
        log.info("steamdb: app %d — no Steam API data, using baseline", steam_app_id)
        return {
            "verbs":  list(BASELINE_REDISTS),
            "source": "baseline (no Steam API data)",
        }

    name = details.get("name", game_title)
    req_verbs = _detect_from_requirements(details)
    if req_verbs:
        log.info("steamdb: app %d ('%s') — requirements text: %s",
                 steam_app_id, name, req_verbs)
        return {
            "verbs":  req_verbs,
            "source": "requirements text (" + name + ")",
        }

    # ── Pass 3: engine hints ──────────────────────────────────────────────────
    engine_verbs = _detect_from_engine_hints(details)
    if engine_verbs:
        log.info("steamdb: app %d ('%s') — engine hints: %s",
                 steam_app_id, name, engine_verbs)
        return {
            "verbs":  engine_verbs,
            "source": "engine detection (" + name + ")",
        }

    # ── Pass 4: baseline (last resort) ───────────────────────────────────────
    log.info("steamdb: app %d ('%s') — no signals found, using baseline",
             steam_app_id, name)
    return {
        "verbs":  list(BASELINE_REDISTS),
        "source": "baseline (no redist signals for '" + name + "')",
    }


def get_redists_for_install(steam_app_id: Optional[int],
                             game_title: str = "",
                             auto_detect: bool = True) -> dict:
    """
    Main entry point for the install dialog.
    Returns dict with 'auto' (pre-checked verbs) and 'source' (label) keys.

    steam_app_id must be the real Steam App ID (from metadata.steam_app_id),
    NOT the SteamGridDB sgdb_id — completely different ID namespaces.
    """
    if not auto_detect or not steam_app_id:
        return {
            "auto":   list(BASELINE_REDISTS),
            "source": "baseline (auto-detect disabled or no Steam ID)",
        }

    result = detect_redists_for_game(steam_app_id, game_title)
    return {
        "auto":   result["verbs"],
        "source": result["source"],
    }
