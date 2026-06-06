"""
steamdb.py — Steam App Details + redistributable detection for VaultPlay

Uses Steam's free public API (no key required):
  https://store.steampowered.com/api/appdetails?appids={appid}

Detection strategy (in priority order):
  1. pc_requirements HTML — mentions vcredist, .NET, DirectX, XNA, PhysX etc.
     directly in the game's own store page system requirements text
  2. categories list — Steam category IDs that indicate DirectX feature level,
     VR, etc. (e.g. category 24 = Steam Achievements, but there is no reliable
     DX-version category, so we use the requirements text primarily)
  3. genres / short_description / detailed_description — engine keyword hints
     (Unity → vcrun2019; Unreal → vcrun2019 + d3dcompiler_47; etc.)
  4. Known DLC depot IDs — a short hardcoded list of well-known redist depots
  5. Baseline — always included for any Windows game

Note: The API's "packages" field is a list of integer package IDs, NOT names,
so name-based matching against it is impossible without extra API calls.
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
import requests
import requests.adapters
from typing import Optional

log = logging.getLogger(__name__)

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "VaultPlay/1.0"
_adapter = requests.adapters.HTTPAdapter(
    pool_connections=2, pool_maxsize=2, max_retries=1)
SESSION.mount("https://", _adapter)
SESSION.mount("http://", _adapter)

STEAM_APPDETAILS = "https://store.steampowered.com/api/appdetails"

# ── Text-based redistributable patterns ──────────────────────────────────────
# Applied against pc_requirements HTML + short_description + detailed_description.
# Each entry: (compiled regex, [winetricks verbs]).
# Ordered from most specific to least specific so more targeted matches
# come before the broad catch-alls.
REDIST_TEXT_PATTERNS = [
    # Visual C++ — year-specific first (must come before the generic catch-all).
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
    # Generic VC++ mention with no year anywhere in the next ~40 chars
    (re.compile(r"(?:visual c\+\+|vc ?redist)(?![\w\s]{0,40}20\d\d)", re.I),
     ["vcrun2019", "vcrun2022"]),
    # .NET — version-specific first
    (re.compile(r"\.net.*?3\.5|dotnet.*?35", re.I),                ["dotnet35"]),
    (re.compile(r"\.net.*?4\.0|dotnet.*?40", re.I),                ["dotnet40"]),
    (re.compile(r"\.net.*?4\.5|dotnet.*?45", re.I),                ["dotnet45"]),
    (re.compile(r"\.net.*?4\.8|dotnet.*?48", re.I),                ["dotnet48"]),
    (re.compile(r"\.net.*?6\.|dotnet.*?6\.", re.I),                ["dotnet48"]),  # closest winetricks verb
    (re.compile(r"\.net.*?7\.|dotnet.*?7\.", re.I),                ["dotnet48"]),
    (re.compile(r"\.net.*?8\.|dotnet.*?8\.", re.I),                ["dotnet48"]),
    # Generic .NET / dotnet mention
    (re.compile(r"\.net framework|dotnet", re.I),                  ["dotnet48"]),
    # DirectX — version-specific, including the Steam format 'DirectX: Version N'
    (re.compile(r"directx[\s:]*(?:version\s*)?9|dx\s*9|d3d\s*9", re.I),
     ["d3dx9"]),
    (re.compile(r"directx[\s:]*(?:version\s*)?10|dx\s*10|d3d\s*10", re.I),
     ["d3dx10", "d3dcompiler_47"]),
    (re.compile(r"directx[\s:]*(?:version\s*)?11|dx\s*11|d3d\s*11", re.I),
     ["d3dx11", "d3dcompiler_47"]),
    (re.compile(r"directx[\s:]*(?:version\s*)?12|dx\s*12|d3d\s*12", re.I),
     ["d3dcompiler_47"]),  # DX12 is built into Win10+; only d3dcompiler needed
    # Generic DirectX — only when no version info follows (colon or digit)
    (re.compile(r"directx(?![\s:]*(?:version\s*)?\d)", re.I),
     ["d3dx9", "d3dx11", "d3dcompiler_47"]),
    # D3D compiler standalone mention
    (re.compile(r"d3dcompiler", re.I),                             ["d3dcompiler_47"]),
    # XNA / XACT (often mentioned in system requirements)
    (re.compile(r"xna\b|xna game studio", re.I),                   ["xact"]),
    (re.compile(r"\bxact\b", re.I),                                ["xact"]),
    # PhysX
    (re.compile(r"physx|nvidia physx", re.I),                      ["physx"]),
    # OpenAL
    (re.compile(r"openal", re.I),                                  ["openal"]),
    # Microsoft C++ runtime (less specific than VC++ above)
    (re.compile(r"microsoft.*?runtime|msvcr", re.I),               ["vcrun2019"]),
]

# ── Engine-based hints from genres/descriptions ───────────────────────────────
# When pc_requirements doesn't have explicit redist mentions, we fall back
# to engine detection from the game's text fields.
ENGINE_HINTS = [
    # Unreal Engine — very common, needs VC++ and d3dcompiler
    (re.compile(r"unreal engine|unreal 4|unreal 5|ue4|ue5", re.I),
     ["vcrun2019", "vcrun2022", "d3dcompiler_47"]),
    # Unity — needs VC++ 2019
    (re.compile(r"unity\b|unity engine", re.I),
     ["vcrun2019", "vcrun2022"]),
    # CryEngine / CRYENGINE
    (re.compile(r"cryengine|cry engine", re.I),
     ["vcrun2019", "d3dx11", "d3dcompiler_47"]),
    # Source / Source 2
    (re.compile(r"source engine|source 2", re.I),
     ["vcrun2019", "d3dcompiler_47"]),
    # GameMaker
    (re.compile(r"gamemaker|game maker studio", re.I),
     ["vcrun2019"]),
    # RPG Maker (usually needs directx9)
    (re.compile(r"rpg maker|rpgmaker", re.I),
     ["vcrun2019", "d3dx9"]),
    # Godot (native, minimal redist needs)
    (re.compile(r"godot engine", re.I),
     ["vcrun2019"]),
]

# ── Known Steam DLC/depot IDs that are redistributable packages ───────────────
# These depot IDs appear in the "dlc" list of the appdetails API response when
# a game opts into Steam's Common Redistributables system (app 228980).
# Confirmed from SteamDB depot page titles and from the Steamworks documentation.
#
# The "packages" field in appdetails is a list of integer package IDs (not names),
# so name-matching against it is not possible. The "dlc" field, however, IS the
# mechanism by which Valve exposes which common redist depots a game opts into —
# so this table is the right place to catch games that declare their dependencies
# through Steam's built-in system rather than via their requirements text.
#
# Source: https://steamdb.info/app/228980/depots/
# Confirmed depot names (from SteamDB page titles via search):
#   228981 = VC 2005 Redist       228982 = VC 2008 Redist
#   228983 = VC 2010 Redist       228984 = VC 2012 Redist
#   228985 = VC 2013 Redist       228986 = VC 2015 Redist
#   228987 = VC 2017 Redist       228988 = VC 2019 Redist
#   228989 = VC 2022 Redist       228990 = DirectX Jun 2010 Redist
#   228991 = OpenAL               228992 = XNA / XACT
#   228993 = PhysX                1826330/1826331 = .NET 4.8
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
    228990: ["d3dx9", "d3dx10", "d3dcompiler_47"],   # DirectX Jun 2010
    228991: ["openal"],
    228992: ["xact"],                                 # XNA / XACT
    228993: ["physx"],
    1826330: ["dotnet48"],                            # .NET 4.8
    1826331: ["dotnet48"],                            # .NET 4.8 (alt depot)
}

# ── Minimum always-recommended set for any Windows game via Wine ──────────────
# These are genuinely near-universal; kept intentionally small so the per-game
# detection above is what drives the per-game differences.
BASELINE_REDISTS = ["vcrun2019", "vcrun2022", "d3dcompiler_47"]


# ── Steam API ─────────────────────────────────────────────────────────────────

def fetch_app_details(steam_app_id: int) -> Optional[dict]:
    """
    Fetch app details from Steam public API.
    Requests the fields we actually use for redist detection.
    """
    try:
        resp = SESSION.get(
            STEAM_APPDETAILS,
            params={
                "appids":  steam_app_id,
                "filters": "name,categories,genres,pc_requirements,short_description,dlc",
            },
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


# ── Detection helpers ─────────────────────────────────────────────────────────

def _strip_html(html: str) -> str:
    """Remove HTML tags for cleaner text matching."""
    return re.sub(r"<[^>]+>", " ", html)


def _match_patterns(text: str, patterns: list) -> list:
    """Run a list of (regex, verbs) patterns against text. Returns deduplicated verbs."""
    found = []
    for pattern, verbs in patterns:
        if pattern.search(text):
            for v in verbs:
                if v not in found:
                    found.append(v)
    return found


def _detect_from_requirements(details: dict) -> list:
    """
    Parse pc_requirements text for explicit redistributable mentions.
    Steam returns pc_requirements as {"minimum": "<html>", "recommended": "<html>"}
    or occasionally as a plain string.
    """
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
    """
    Look for engine keywords in short_description and genres.
    Used as a fallback when pc_requirements gives nothing useful.
    """
    short_desc = details.get("short_description", "") or ""
    genres = " ".join(g.get("description", "") for g in details.get("genres", []))
    text = short_desc + " " + genres
    return _match_patterns(text, ENGINE_HINTS)


def _detect_from_known_dlc(details: dict) -> list:
    """Check the DLC list against known redistributable depot IDs."""
    found = []
    for dlc_id in details.get("dlc", []):
        if dlc_id in KNOWN_REDIST_DEPOTS:
            for v in KNOWN_REDIST_DEPOTS[dlc_id]:
                if v not in found:
                    found.append(v)
            log.info("SteamDB: known redist depot %d → %s",
                     dlc_id, KNOWN_REDIST_DEPOTS[dlc_id])
    return found


# ── Public API ────────────────────────────────────────────────────────────────

def detect_redists_for_game(steam_app_id: int, game_title: str = "") -> dict:
    """
    Detect required redistributables for a game using Steam's public API.

    Returns:
        {
          "detected": list[str]  — per-game detected verbs (may be empty),
          "baseline": list[str]  — always-recommended verbs,
          "all":      list[str]  — baseline + detected, deduplicated,
          "source":   str        — human-readable description of detection method,
        }
    """
    detected: list[str] = []
    source = "baseline only"

    details = fetch_app_details(steam_app_id)
    if not details:
        log.info("SteamDB: no Steam data for app_id=%d, using baseline", steam_app_id)
        return {
            "detected": [],
            "baseline": BASELINE_REDISTS,
            "all":      list(BASELINE_REDISTS),
            "source":   "baseline (no Steam API data)",
        }

    name = details.get("name", game_title)
    log.info("SteamDB: fetched details for '%s' (app_id=%d)", name, steam_app_id)

    sources_used: list[str] = []

    # ── Pass 1: pc_requirements text ─────────────────────────────────────────
    # Most reliable: developers explicitly list what their game needs.
    # Returns empty list when requirements text is missing or sparse.
    req_verbs = _detect_from_requirements(details)
    if req_verbs:
        detected = req_verbs
        sources_used.append("requirements")
        log.info("SteamDB: requirements text → %s", detected)

    # ── Pass 2: known DLC depot IDs ──────────────────────────────────────────
    # Always runs regardless of Pass 1 — games that use Steam's Common Redist
    # system declare their deps here, independently of what they write in
    # their requirements text. The two sources are complementary, not redundant.
    dlc_verbs = _detect_from_known_dlc(details)
    if dlc_verbs:
        for v in dlc_verbs:
            if v not in detected:
                detected.append(v)
        sources_used.append("depot IDs")
        log.info("SteamDB: depot IDs → %s", dlc_verbs)

    # ── Pass 3: engine hints — only when the above found nothing ─────────────
    # Weakest signal: inferred from engine name in description/genre. Use only
    # as a last resort since many games don't mention their engine at all.
    if not detected:
        engine_verbs = _detect_from_engine_hints(details)
        if engine_verbs:
            detected = engine_verbs
            sources_used.append("engine detection")
            log.info("SteamDB: engine hints → %s", detected)

    if sources_used:
        source = f"{' + '.join(sources_used)} ({name})"
    else:
        source = f"baseline (no redist signals for '{name}')"
        log.info("SteamDB: no specific redists found for %d", steam_app_id)

    # Baseline always included; detected verbs added on top
    all_verbs: list[str] = list(BASELINE_REDISTS)
    for v in detected:
        if v not in all_verbs:
            all_verbs.append(v)

    log.info("SteamDB: final redists for '%s': %s (source: %s)",
             name, all_verbs, source)
    return {
        "detected": detected,
        "baseline": BASELINE_REDISTS,
        "all":      all_verbs,
        "source":   source,
    }


def get_redists_for_install(steam_app_id: Optional[int],
                             game_title: str = "",
                             auto_detect: bool = True) -> dict:
    """
    Main entry point for the install dialog.
    Returns dict with 'auto' (pre-checked verbs) and 'source' (label) keys.

    IMPORTANT: steam_app_id must be the real Steam App ID (from metadata.steam_app_id),
    NOT the SteamGridDB sgdb_id — those are completely different ID namespaces.
    """
    if not auto_detect or not steam_app_id:
        return {
            "auto":   list(BASELINE_REDISTS),
            "source": "baseline (auto-detect disabled or no Steam ID)",
        }

    result = detect_redists_for_game(steam_app_id, game_title)
    return {
        "auto":   result["all"],
        "source": result["source"],
    }
