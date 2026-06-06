"""
steamdb.py — Steam App Details + redistributable detection for VaultPlay

Uses Steam's free public API (no key required):
  https://store.steampowered.com/api/appdetails?appids={appid}

Parses the response to find packages that contain redistributables,
then maps known Steam depot/package names to winetricks verbs.
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

# Known redistributable depot/DLC names → winetricks verbs
# Based on common Steam depot names and what they install
REDIST_NAME_MAP = [
    # Visual C++ runtimes
    (re.compile(r"vcredist.*2005|vc.*2005", re.I),          ["vcrun2005"]),
    (re.compile(r"vcredist.*2008|vc.*2008", re.I),          ["vcrun2008"]),
    (re.compile(r"vcredist.*2010|vc.*2010", re.I),          ["vcrun2010"]),
    (re.compile(r"vcredist.*2012|vc.*2012", re.I),          ["vcrun2012"]),
    (re.compile(r"vcredist.*2013|vc.*2013", re.I),          ["vcrun2013"]),
    (re.compile(r"vcredist.*2015|vc.*2015", re.I),          ["vcrun2015"]),
    (re.compile(r"vcredist.*2017|vc.*2017", re.I),          ["vcrun2017"]),
    (re.compile(r"vcredist.*2019|vc.*2019", re.I),          ["vcrun2019"]),
    (re.compile(r"vcredist.*2022|vc.*2022", re.I),          ["vcrun2022"]),
    (re.compile(r"microsoft visual c\+\+", re.I),           ["vcrun2019", "vcrun2022"]),
    # .NET
    (re.compile(r"\.net.*4\.0|dotnet.*40", re.I),           ["dotnet40"]),
    (re.compile(r"\.net.*4\.5|dotnet.*45", re.I),           ["dotnet45"]),
    (re.compile(r"\.net.*4\.8|dotnet.*48", re.I),           ["dotnet48"]),
    (re.compile(r"\.net framework", re.I),                  ["dotnet48"]),
    # DirectX
    (re.compile(r"directx", re.I),                          ["d3dx9", "d3dx11"]),
    (re.compile(r"d3dx9", re.I),                            ["d3dx9"]),
    (re.compile(r"d3dx11", re.I),                           ["d3dx11"]),
    (re.compile(r"d3dcompiler", re.I),                      ["d3dcompiler_47"]),
    # XNA / XACT
    (re.compile(r"xna", re.I),                              ["xact"]),
    (re.compile(r"xact", re.I),                             ["xact"]),
    # PhysX
    (re.compile(r"physx", re.I),                            ["physx"]),
    # OpenAL
    (re.compile(r"openal", re.I),                           ["openal"]),
    # Steam common redists depot (228981, 228988 etc.)
    (re.compile(r"steam.*redist|common.*redist", re.I),     ["vcrun2019", "vcrun2022",
                                                              "d3dx11", "d3dcompiler_47"]),
]

# Known Steam depot IDs for redistributables
KNOWN_REDIST_DEPOTS = {
    228981: ["vcrun2019", "vcrun2022", "d3dcompiler_47"],   # Steam Common Redist
    228988: ["vcrun2019"],                                   # VC++ 2019 x64
    1826330: ["dotnet48"],                                   # .NET 4.8
    1826331: ["dotnet48"],
}

# Minimum always-recommend set for Windows games
BASELINE_REDISTS = ["vcrun2019", "vcrun2022", "d3dcompiler_47"]


def fetch_app_details(steam_app_id: int) -> Optional[dict]:
    """Fetch app details from Steam public API."""
    try:
        resp = SESSION.get(
            STEAM_APPDETAILS,
            params={"appids": steam_app_id, "filters": "packages,dlc,name"},
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


def _match_redists_from_name(name: str) -> list:
    """Match a depot/package name against known redistributable patterns."""
    result = []
    for pattern, verbs in REDIST_NAME_MAP:
        if pattern.search(name):
            result.extend(verbs)
    return list(dict.fromkeys(result))  # dedupe preserving order


def detect_redists_for_game(steam_app_id: int,
                             game_title: str = "") -> dict:
    """
    Detect required redistributables for a game using Steam's API.

    Returns:
        {
          "detected":  list of winetricks verbs (auto-detected),
          "baseline":  list of always-recommended verbs,
          "source":    str describing how they were detected,
        }
    """
    detected = []
    source = "baseline only"

    details = fetch_app_details(steam_app_id)
    if details:
        name = details.get("name", game_title)
        log.info("SteamDB: fetched details for '%s' (id=%d)", name, steam_app_id)

        # Check packages list for redist-sounding names
        for pkg in details.get("packages", []):
            pkg_name = str(pkg)
            verbs = _match_redists_from_name(pkg_name)
            if verbs:
                detected.extend(verbs)
                log.debug("SteamDB: package %s → %s", pkg_name, verbs)

        # Check DLC list (some games ship redists as DLC)
        for dlc_id in details.get("dlc", []):
            if dlc_id in KNOWN_REDIST_DEPOTS:
                verbs = KNOWN_REDIST_DEPOTS[dlc_id]
                detected.extend(verbs)
                log.info("SteamDB: known redist depot %d → %s", dlc_id, verbs)

        if detected:
            source = f"Steam API ({name})"
        else:
            # No specific redists found — try a broader search on the game name
            log.info("SteamDB: no specific redists found for %d, using baseline", steam_app_id)
            source = f"baseline (no Steam redist data for {name})"
    else:
        log.info("SteamDB: no Steam data for app_id=%d, using baseline", steam_app_id)
        source = "baseline (no Steam API data)"

    # Always include baseline
    all_verbs = list(dict.fromkeys(BASELINE_REDISTS + detected))

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
    Main entry point for install dialog.
    Returns dict with 'auto' (pre-checked) and 'source' keys.
    """
    if not auto_detect or not steam_app_id:
        return {
            "auto":   BASELINE_REDISTS[:],
            "source": "baseline (auto-detect disabled or no Steam ID)",
        }

    result = detect_redists_for_game(steam_app_id, game_title)
    return {
        "auto":   result["all"],
        "source": result["source"],
    }
