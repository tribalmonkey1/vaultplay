"""
protondb.py — ProtonDB integration + installed Proton version detection for VaultPlay

Data source for version recommendations:
  Live ProtonDB reports endpoint — fetches per-game community report data directly
  from protondb.com. No GitHub dump, no tier heuristic.

  Flow per game:
    1. GET protondb.com/api/v1/reports/summaries/{app_id}.json
       → tier, total reports, score
    2. GET protondb.com/api/v1/reports/counts/{app_id}.json
       → reports count + timestamp, used to compute the internal hash
    3. Compute internal_id = _compute_hash(app_id, reports, timestamp)
       → cached in DB as protondb_internal_id (old hashes stay valid)
    4. GET protondb.com/data/reports/all-devices/app/{internal_id}.json
       → full report array, walk protonVersion fields, plurality-vote winner
    5. Store tier, recommended version, per-version counts in DB

  Error handling:
    If any step fails (network error, unexpected 404 on cached hash, schema change),
    recommended_proton is stored as None and the install dialog shows a clear
    warning: "⚠ ProtonDB data unavailable". The log will contain a
    [PROTONDB ERROR] line with the full URL and status code so the issue can be
    diagnosed and the fetch method re-evaluated if needed.

  Version normalization:
    Report strings like "Proton 9.0-4" are normalized to "9.0" before counting.
    The -4 suffix is a patch revision within the 9.0 series and is stripped.
    "GE-Proton10-27" → "GE-Proton 10" (minor/patch stripped, major kept for matching).
    This means match_to_installed() will find "GE-Proton 10.27" in your installed
    list when the report says "GE-Proton 10" or "GE-Proton10-27".

GE-Proton download:
  GE-Proton versions can be downloaded and installed directly in-app.
  Releases fetched from: github.com/GloriousEggroll/proton-ge-custom/releases
  Installed to: ~/.steam/root/compatibilitytools.d/ (or equivalent Steam root)

Official Proton versions:
  Installed via Steam using steam:// URLs. A static lookup table maps version
  strings to Steam app IDs. Unknown versions fall back to the Steam tools page.
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
import os
import re
import logging
import time
import requests
import requests.adapters
from pathlib import Path
from typing import Optional

import db

log = logging.getLogger(__name__)

# ── HTTP session ──────────────────────────────────────────────────────────────

SESSION = requests.Session()
# ProtonDB requires a browser-like UA; requests is blocked with generic UA strings
SESSION.headers.update({
    "User-Agent":  "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Referer":     "https://www.protondb.com/",
    "Accept":      "application/json, */*",
    "Accept-Language": "en-US,en;q=0.5",
})
_adapter = requests.adapters.HTTPAdapter(pool_connections=2, pool_maxsize=4, max_retries=1)
SESSION.mount("https://", _adapter)
SESSION.mount("http://", _adapter)

# ── API endpoints ─────────────────────────────────────────────────────────────

_BASE              = "https://www.protondb.com"
SUMMARY_URL        = _BASE + "/api/v1/reports/summaries/{app_id}.json"
REPORTS_URL        = _BASE + "/data/reports/all-devices/app/{internal_id}.json"
COUNTS_URL         = _BASE + "/data/counts.json"   # global file, not per-game

# GE-Proton GitHub releases API
GE_RELEASES_API    = "https://api.github.com/repos/GloriousEggroll/proton-ge-custom/releases"
GE_DOWNLOAD_URL    = "https://github.com/GloriousEggroll/proton-ge-custom/releases/download/{tag}/{tag}.tar.gz"

# Steam install URL for official Proton versions (steam://install/<appid>)
# Unknown versions fall back to the Steam tools browsing page.
STEAM_PROTON_IDS = {
    "proton experimental":    1420170,
    "proton hotfix":          2180100,
    "proton 9.0":             2805730,
    "proton 8.0":             2348590,
    "proton 7.0":             1887720,
    "proton 6.3":              677060,
    "proton 6.0":             1580130,
    "proton 5.13":            1420170,  # experimental at time
    "proton 5.0":              228981,
}
STEAM_TOOLS_URL = "https://store.steampowered.com/search/?category1=993"

# ── Tier display info ─────────────────────────────────────────────────────────

TIER_INFO = {
    "platinum": ("Platinum", "#b4c7dc"),
    "gold":     ("Gold",     "#CFB53B"),
    "silver":   ("Silver",   "#A8A9AD"),
    "bronze":   ("Bronze",   "#CD7F32"),
    "borked":   ("Borked",   "#f87171"),
    "pending":  ("Pending",  "#6b7280"),
}

# ── Internal hash computation ─────────────────────────────────────────────────
# Fully reverse-engineered from the ProtonDB React bundle (modules 99273 + 83301).
# Documented in Notion: "ProtonDB Internal ID — Reverse Engineering Notes".
#
# counts.json is a GLOBAL file — one fetch per refresh cycle, fields:
#   {"reports": <total_reports_int>, "timestamp": <unix_ts_int>, ...}
# Use the same reports/timestamp to compute hashes for ALL games in a batch.
#
# Verified against:
#   AoE:DE      steam_app_id=1017900  → internal_id=1190953666
#   GTA V       steam_app_id=271590   → internal_id=371272058

def _vp(s: str) -> int:
    """
    JS-equivalent hash: Math.abs(str.concat('m').split('').reduce(...))
    Replicates the `zk` function from ProtonDB module 99273.
    """
    h = 0
    for c in (str(s) + 'm'):
        h = ((h << 5) - h + ord(c)) | 0
        h = h & 0xFFFFFFFF
        if h >= 0x80000000:
            h -= 0x100000000
    return abs(h)


def _R(e: int, t: int, n: int) -> str:
    """
    R(steamAppId, reports, timestamp) → '{reports}p{steamAppId * (reports % timestamp)}'
    e = steamAppId (the multiplier), t = reports (the prefix), n = timestamp (the modulus).
    """
    t, n = int(t), int(n)
    return str(t) + 'p' + str(e * (t % n))


def _compute_hash(app_id: int, reports: int, timestamp: int) -> int:
    """
    Compute the ProtonDB internal app ID used to build the reports JSON URL.

    Formula (from module 83301, function jM, calling zk from module 99273):
      s = 'p' + R(app_id, reports, timestamp) + '*vRT' + R(1, app_id, timestamp) + 'undefined'
      internal_id = _vp(s)

    The 'undefined' suffix is critical — it's JS undefined stringified in the concat.
    Old hashes remain valid even as counts.json changes, so we cache in DB.
    """
    s = ('p'
         + _R(app_id, reports, timestamp)
         + '*vRT'
         + _R(1, app_id, timestamp)
         + 'undefined')
    return _vp(s)


# ── Version normalization ─────────────────────────────────────────────────────

def _normalize_version(ver: str) -> str:
    """Normalize a directory name to a display label."""
    ver = ver.strip()
    m = re.match(r"(?:Lutris-)?GE-Proton(\d+)-(\d+)", ver, re.IGNORECASE)
    if m:
        return f"GE-Proton {m.group(1)}.{m.group(2)}"
    if re.search(r"proton.{0,4}experimental", ver, re.IGNORECASE):
        return "Proton Experimental"
    m = re.match(r"Proton[\s_-]+(\d+\.\d+)", ver, re.IGNORECASE)
    if m:
        rest = ver[m.end():].strip(" -_")
        if rest and not rest.startswith("("):
            rest = re.sub(r"^[-_\s]+", "", rest)
            return f"Proton {m.group(1)} {rest}".strip()
        return f"Proton {m.group(1)}"
    m = re.match(r"wine-ge-(\d+)-(\d+)", ver, re.IGNORECASE)
    if m:
        return f"Wine-GE {m.group(1)}.{m.group(2)}"
    if re.match(r"proton-experimental", ver, re.IGNORECASE):
        return "Proton Experimental"
    return ver


def _parse_report_version(ver_str: str) -> str:
    """
    Normalize a ProtonDB report protonVersion string to a canonical counting key.

    Examples:
      "Proton 9.0-4"        → "9.0"    (-4 is a patch rev within 9.0, stripped)
      "Proton Experimental" → "experimental"
      "GE-Proton10-27"      → "GE-Proton 10"  (minor stripped for grouping)
      "native"              → "native"   (excluded from recommendation)
      ""                    → ""
    """
    if not ver_str:
        return ""
    s = ver_str.strip()
    if re.match(r"^native$", s, re.IGNORECASE):
        return "native"
    if re.search(r"experimental", s, re.IGNORECASE):
        return "experimental"
    # GE-Proton: group by major only (GE-Proton 10, GE-Proton 9, etc.)
    m = re.match(r"GE-Proton(\d+)", s, re.IGNORECASE)
    if m:
        return f"GE-Proton {m.group(1)}"
    # Official Proton X.Y-Z → strip the -Z patch revision → "X.Y"
    m = re.search(r"[Pp]roton\s*(\d+)\.(\d+)", s)
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    # Proton X (no minor)
    m = re.search(r"[Pp]roton\s*(\d+)", s)
    if m:
        return m.group(1)
    # Wine-GE
    m = re.match(r"wine-ge-(\d+)", s, re.IGNORECASE)
    if m:
        return f"Wine-GE {m.group(1)}"
    return s


# ── ProtonDB API calls ────────────────────────────────────────────────────────

def fetch_summary(steam_app_id: int) -> Optional[dict]:
    """
    Fetch tier/score/total from the ProtonDB summary API.
    Returns dict with 'tier', 'total_reports', 'score' or None.
    """
    url = SUMMARY_URL.format(app_id=steam_app_id)
    try:
        resp = SESSION.get(url, timeout=10)
        if resp.status_code == 404:
            log.debug("ProtonDB: no summary for app_id=%d (404)", steam_app_id)
            return None
        resp.raise_for_status()
        data = resp.json()
        resp.close()
        return {
            "tier":          data.get("tier", "pending"),
            "score":         data.get("score", 0.0),
            "total_reports": data.get("total", 0),
        }
    except Exception as e:
        log.warning("[PROTONDB ERROR] Summary fetch failed for app_id=%d — %s: %s",
                    steam_app_id, url, e)
        return None


def fetch_counts() -> Optional[dict]:
    """
    Fetch the global counts.json file from ProtonDB.
    Returns dict with 'reports' (int) and 'timestamp' (int), or None on failure.

    This is a GLOBAL file — one fetch gives you the salt values used to compute
    internal hashes for ALL games. Call once per refresh cycle, not per game.
    URL: https://www.protondb.com/data/counts.json
    Response: {"reports": 430668, "timestamp": 1780603361, ...}
    """
    url = "https://www.protondb.com/data/counts.json"
    try:
        resp = SESSION.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        resp.close()
        reports   = int(data.get("reports",   0))
        timestamp = int(data.get("timestamp", 0))
        if not reports or not timestamp:
            log.warning("[PROTONDB ERROR] counts.json missing expected fields: %s", data)
            return None
        log.debug("ProtonDB: counts.json → reports=%d, timestamp=%d",
                  reports, timestamp)
        return {"reports": reports, "timestamp": timestamp}
    except Exception as e:
        log.warning("[PROTONDB ERROR] counts.json fetch failed — %s: %s", url, e)
        return None


def fetch_reports(internal_id: int, steam_app_id: int,
                  is_retry: bool = False) -> Optional[list]:
    """
    Fetch the full reports array for a game using the computed internal hash.

    On a 404 (cached hash stale), recomputes a fresh hash from current counts
    and retries once. If the retry also 404s, logs a prominent [PROTONDB ERROR]
    warning indicating the endpoint structure may have changed.

    Returns list of report dicts, or None on failure.
    """
    url = REPORTS_URL.format(internal_id=internal_id)
    try:
        resp = SESSION.get(url, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            resp.close()
            return data if isinstance(data, list) else data.get("reports", [])

        if resp.status_code == 404 and not is_retry:
            resp.close()
            log.info("ProtonDB: cached hash %d stale for app_id=%d — recomputing",
                     internal_id, steam_app_id)
            counts = fetch_counts(steam_app_id)
            if counts:
                new_id = _compute_hash(steam_app_id,
                                       counts["reports"], counts["timestamp"])
                if new_id != internal_id:
                    return fetch_reports(new_id, steam_app_id, is_retry=True)
            # Counts also failed or hash unchanged — fall through to error
            log.warning(
                "[PROTONDB ERROR] Reports 404 for app_id=%d, hash=%d — "
                "could not recompute a valid hash. The ProtonDB endpoint "
                "structure may have changed. Check the URL: %s",
                steam_app_id, internal_id, url)
            return None

        if resp.status_code == 404 and is_retry:
            resp.close()
            log.warning(
                "[PROTONDB ERROR] Reports 404 on retry for app_id=%d, hash=%d — "
                "ProtonDB endpoint structure may have changed. "
                "The fetch method needs to be re-evaluated. URL: %s",
                steam_app_id, internal_id, url)
            return None

        resp.raise_for_status()

    except requests.exceptions.RequestException as e:
        log.warning("[PROTONDB ERROR] Reports fetch failed for app_id=%d, hash=%d — "
                    "%s: %s", steam_app_id, internal_id, url, e)
    return None


# ── Version counting ──────────────────────────────────────────────────────────

def count_versions(reports: list, max_reports: int = 200) -> dict:
    """
    Walk up to max_reports (newest-first) report objects and count protonVersion
    occurrences after normalizing with _parse_report_version().

    Returns dict: {canonical_version_key: count}, excluding 'native' and empty.
    """
    counts: dict[str, int] = {}
    seen = 0
    for report in reports:
        if seen >= max_reports:
            break
        try:
            ver_raw = (
                report.get("responses", {}).get("protonVersion")
                or report.get("protonVersion")
                or ""
            )
        except (AttributeError, TypeError):
            continue
        if not ver_raw:
            continue
        canonical = _parse_report_version(ver_raw)
        if not canonical or canonical == "native":
            continue
        counts[canonical] = counts.get(canonical, 0) + 1
        seen += 1
    return counts


def top_version(counts: dict) -> Optional[str]:
    """Return the canonical version key with the highest count, or None."""
    if not counts:
        return None
    return max(counts, key=lambda k: counts[k])


# ── Steam library / installed version detection ───────────────────────────────

def _steam_roots() -> list[Path]:
    """Return all Steam library root paths, including extras from libraryfolders.vdf."""
    candidates = [
        Path.home() / ".steam" / "steam",
        Path.home() / ".steam" / "root",
        Path.home() / ".local" / "share" / "Steam",
    ]
    roots = []
    seen = set()
    for c in candidates:
        try:
            real = c.resolve()
            if real.exists() and str(real) not in seen:
                seen.add(str(real))
                roots.append(real)
        except OSError:
            pass

    for root in list(roots):
        vdf_path = root / "steamapps" / "libraryfolders.vdf"
        if not vdf_path.exists():
            continue
        try:
            text = vdf_path.read_text(errors="replace")
            for m in re.finditer(r'"path"\s+"([^"]+)"', text):
                extra = Path(m.group(1))
                try:
                    real = extra.resolve()
                    if real.exists() and str(real) not in seen:
                        seen.add(str(real))
                        roots.append(real)
                except OSError:
                    pass
        except Exception as e:
            log.debug("libraryfolders.vdf parse error: %s", e)
    return roots


def _build_scan_paths() -> list[Path]:
    paths = [
        Path.home() / ".local" / "share" / "lutris" / "runners" / "wine",
    ]
    for root in _steam_roots():
        paths.append(root / "compatibilitytools.d")
        paths.append(root / "steamapps" / "common")
    paths.extend([
        Path.home() / ".steam" / "root" / "compatibilitytools.d",
        Path.home() / ".steam" / "steam" / "compatibilitytools.d",
        Path.home() / ".local" / "share" / "Steam" / "compatibilitytools.d",
    ])
    return paths


def _get_compat_dirs() -> list[Path]:
    """Return all valid compatibilitytools.d directories."""
    dirs = []
    seen = set()
    for root in _steam_roots():
        p = root / "compatibilitytools.d"
        try:
            real = p.resolve()
            if real.exists() and str(real) not in seen:
                seen.add(str(real))
                dirs.append(real)
        except OSError:
            pass
    fallbacks = [
        Path.home() / ".steam" / "root" / "compatibilitytools.d",
        Path.home() / ".steam" / "steam" / "compatibilitytools.d",
        Path.home() / ".local" / "share" / "Steam" / "compatibilitytools.d",
    ]
    for p in fallbacks:
        try:
            real = p.resolve()
            if real.exists() and str(real) not in seen:
                seen.add(str(real))
                dirs.append(real)
        except OSError:
            pass
    return dirs


def _dir_is_wine_version(path: Path) -> bool:
    return (
        (path / "bin" / "wine").exists()
        or (path / "bin" / "wine64").exists()
        or (path / "files" / "bin" / "wine").exists()
        or (path / "files" / "bin" / "wine64").exists()
        or (path / "dist" / "bin" / "wine").exists()
        or (path / "dist" / "bin" / "wine64").exists()
        or (path / "proton").exists()
    )


def scan_installed_versions() -> list[dict]:
    found: dict[str, dict] = {}
    seen_dirs = set()

    for base in _build_scan_paths():
        try:
            real_base = base.resolve()
        except OSError:
            continue
        if not real_base.exists() or str(real_base) in seen_dirs:
            continue
        seen_dirs.add(str(real_base))

        try:
            entries = sorted(real_base.iterdir())
        except (PermissionError, OSError):
            continue

        for entry in entries:
            if not entry.is_dir():
                continue
            name = entry.name
            if "steamapps/common" in str(real_base) or "steamapps/common" in str(base):
                if not re.match(r"Proton", name, re.IGNORECASE):
                    continue
            if not _dir_is_wine_version(entry):
                continue

            label = _normalize_version(name)
            value = label.lower().replace(" ", "-")

            if value not in found:
                found[value] = {"label": label, "value": value, "path": str(entry)}

    import shutil
    if shutil.which("wine"):
        import subprocess
        try:
            ver_out = subprocess.run(
                ["wine", "--version"], capture_output=True, text=True, timeout=5
            ).stdout.strip()
            sys_label = f"System Wine ({ver_out})" if ver_out else "System Wine"
        except Exception:
            sys_label = "System Wine"
        found["system-wine"] = {"label": sys_label, "value": "system-wine", "path": "wine"}

    return list(found.values())


# ── Installed version cache ───────────────────────────────────────────────────

_installed_cache: list[dict] = []
_installed_cache_ts: float = 0.0
_CACHE_TTL: float = 120.0


def _get_cached_installed() -> list[dict]:
    global _installed_cache, _installed_cache_ts
    if not _installed_cache or (time.monotonic() - _installed_cache_ts) > _CACHE_TTL:
        _installed_cache = scan_installed_versions()
        _installed_cache_ts = time.monotonic()
    return _installed_cache


def invalidate_installed_cache():
    """Force a rescan of installed versions on next access. Call after a download."""
    global _installed_cache, _installed_cache_ts
    _installed_cache = []
    _installed_cache_ts = 0.0


def get_versions_for_ui() -> list[tuple[str, str]]:
    """Return (display_label, value) tuples for installed versions, sorted for UI."""
    versions = list(_get_cached_installed())

    def sort_key(v):
        label = v["label"].lower()
        if "ge-proton" in label:
            m = re.search(r"ge-proton\s*(\d+)\.(\d+)", label)
            if m:
                return (0, -int(m.group(1)), -int(m.group(2)))
            return (0, 0, 0)
        if "experimental" in label:
            return (1, 0, 0)
        if "proton" in label:
            m = re.search(r"(\d+)\.(\d+)", label)
            if m:
                return (2, -int(m.group(1)), -int(m.group(2)))
            return (2, 0, 0)
        if "wine-ge" in label:
            return (3, 0, 0)
        return (4, 0, 0)

    versions.sort(key=sort_key)
    return [(v["label"], v["value"]) for v in versions]


def get_version_path(value: str) -> Optional[str]:
    for v in _get_cached_installed():
        if v["value"] == value:
            return v["path"]
    return None


def is_version_installed(canonical_key: str) -> bool:
    """
    Check whether a canonical version key (from ProtonDB reports) maps to
    any currently installed version.
    """
    installed = get_versions_for_ui()
    return match_to_installed(canonical_key, installed) is not None


# ── Version matching ──────────────────────────────────────────────────────────

def match_to_installed(recommended_version: str,
                        installed: list[tuple[str, str]]) -> Optional[str]:
    """
    Match a canonical version key to the closest installed version value.

    GE-Proton N  → finds highest installed minor of that major
    X.Y          → exact match first, then major-only fallback
    experimental → any Experimental build
    """
    if not recommended_version or not installed:
        return None

    rec = recommended_version.lower().strip()

    if rec in ("native", ""):
        return None

    if rec == "experimental":
        for label, value in installed:
            if "experimental" in label.lower():
                return value
        return None

    # GE-Proton major match — find highest installed minor
    m = re.match(r"ge-proton\s*(\d+)", rec)
    if m:
        major = m.group(1)
        best_val, best_minor = None, -1
        for label, value in installed:
            lbl = label.lower()
            mm = re.search(r"ge-proton\s*" + major + r"[.\s-](\d+)", lbl)
            if mm:
                minor = int(mm.group(1))
                if minor > best_minor:
                    best_minor, best_val = minor, value
        if best_val:
            return best_val

    # Official Proton X.Y — exact match, then major-only
    m = re.match(r"(\d+)\.(\d+)", rec)
    if m:
        major, minor = m.group(1), m.group(2)
        for label, value in installed:
            lbl = label.lower()
            if "ge" not in lbl and re.search(r"\b" + major + r"\." + minor + r"\b", lbl):
                return value
        for label, value in installed:
            lbl = label.lower()
            if "ge" not in lbl and re.search(r"\b" + major + r"\b", lbl):
                return value

    # Bare major number
    m = re.match(r"^(\d+)$", rec)
    if m:
        major = m.group(1)
        for label, value in installed:
            lbl = label.lower()
            if "ge" not in lbl and re.search(r"\b" + major + r"\b", lbl):
                return value

    # Substring fallback
    for label, value in installed:
        if rec in label.lower():
            return value

    return None


# ── Tier display helpers ──────────────────────────────────────────────────────

def tier_display(tier: str) -> tuple[str, str]:
    """Return (display_label, hex_color) for a tier string."""
    return TIER_INFO.get((tier or "").lower(), ("Unknown", "#6b7280"))


def proton_version_label(value: str) -> str:
    """Return the display label for an installed version value key."""
    for v in _get_cached_installed():
        if v["value"] == value:
            return v["label"]
    return value


# ── GE-Proton download ────────────────────────────────────────────────────────

def get_ge_releases(limit: int = 20) -> list[dict]:
    """
    Fetch the latest GE-Proton releases from GitHub.
    Returns list of {tag, name, download_url, size_bytes} dicts.
    """
    try:
        resp = requests.get(GE_RELEASES_API,
                            params={"per_page": limit},
                            headers={"User-Agent": "VaultPlay/1.0"},
                            timeout=15)
        resp.raise_for_status()
        releases = resp.json()
        resp.close()
        result = []
        for rel in releases:
            tag = rel.get("tag_name", "")
            if not tag:
                continue
            # Find the .tar.gz asset
            for asset in rel.get("assets", []):
                if asset.get("name", "").endswith(".tar.gz"):
                    result.append({
                        "tag":          tag,
                        "name":         rel.get("name", tag),
                        "download_url": asset["browser_download_url"],
                        "size_bytes":   asset.get("size", 0),
                    })
                    break
        return result
    except Exception as e:
        log.warning("[PROTONDB] Failed to fetch GE-Proton releases: %s", e)
        return []


def get_ge_install_dir() -> Optional[Path]:
    """Return the first valid compatibilitytools.d directory to install into."""
    dirs = _get_compat_dirs()
    if dirs:
        return dirs[0]
    # Try to create the default one
    default = Path.home() / ".steam" / "root" / "compatibilitytools.d"
    try:
        default.mkdir(parents=True, exist_ok=True)
        return default
    except OSError:
        return None


def download_ge_proton(tag: str, download_url: str,
                        progress_cb=None) -> tuple[bool, str]:
    """
    Download and install a GE-Proton release.

    progress_cb(stage: str, percent: int, message: str)
    Returns (success: bool, message: str)
    """
    import tarfile as _tarfile
    import tempfile

    def prog(stage, pct, msg):
        if progress_cb:
            progress_cb(stage, pct, msg)
        log.info("[GE-Proton download] %s %d%% — %s", stage, pct, msg)

    install_dir = get_ge_install_dir()
    if not install_dir:
        return False, "Could not find or create compatibilitytools.d directory"

    dest = install_dir / tag
    if dest.exists():
        return True, f"{tag} is already installed"

    prog("Downloading", 0, f"Downloading {tag}…")

    try:
        resp = requests.get(download_url, stream=True,
                            headers={"User-Agent": "VaultPlay/1.0"},
                            timeout=120)
        resp.raise_for_status()

        total_size = int(resp.headers.get("content-length", 0))
        downloaded = 0
        chunks = []
        for chunk in resp.iter_content(chunk_size=65536):
            chunks.append(chunk)
            downloaded += len(chunk)
            if total_size:
                pct = int(downloaded / total_size * 60)
                prog("Downloading", pct,
                     f"{downloaded // 1024 // 1024} MB / {total_size // 1024 // 1024} MB")
        resp.close()
        raw = b"".join(chunks)
        prog("Downloading", 60, "Download complete — extracting…")
    except Exception as e:
        return False, f"Download failed: {e}"

    prog("Extracting", 62, f"Extracting {tag}…")
    try:
        with _tarfile.open(fileobj=__import__("io").BytesIO(raw), mode="r:gz") as tar:
            members = tar.getmembers()
            total_m = len(members)
            for i, member in enumerate(members):
                tar.extract(member, path=str(install_dir))
                if i % 50 == 0:
                    pct = 62 + int(i / total_m * 35)
                    prog("Extracting", pct, f"Extracting {member.name[:60]}…")
    except Exception as e:
        return False, f"Extraction failed: {e}"

    prog("Done", 100, f"{tag} installed to {install_dir}")

    # Invalidate the installed versions cache so the new version appears immediately
    invalidate_installed_cache()

    return True, f"✓ {tag} installed successfully"


# ── Official Proton Steam URL ─────────────────────────────────────────────────

def get_steam_install_url(canonical_key: str) -> tuple[str, bool]:
    """
    Return (url, is_direct) for installing an official Proton version via Steam.

    is_direct=True  → steam://install/<appid>  (known version, one-click)
    is_direct=False → Steam tools search page  (unknown version, manual browse)
    """
    key = canonical_key.lower().strip()
    # Match against known versions
    for name, app_id in STEAM_PROTON_IDS.items():
        if name in key or key in name:
            return f"steam://install/{app_id}", True
    # Fuzzy: extract major.minor and try again
    m = re.search(r"(\d+)\.(\d+)", key)
    if m:
        ver_key = f"proton {m.group(1)}.{m.group(2)}"
        if ver_key in STEAM_PROTON_IDS:
            return f"steam://install/{STEAM_PROTON_IDS[ver_key]}", True
    # Check for "experimental"
    if "experimental" in key:
        return f"steam://install/{STEAM_PROTON_IDS['proton experimental']}", True
    return STEAM_TOOLS_URL, False


# ── Main fetch-and-store ──────────────────────────────────────────────────────

def fetch_and_store(game_id: int,
                    counts: Optional[dict] = None) -> Optional[dict]:
    """
    Fetch ProtonDB data for a single game and write results to DB.

    counts: pre-fetched global counts dict {"reports": int, "timestamp": int}.
            If None, fetch_and_store will fetch counts.json itself (slower —
            prefer passing counts in from a batch loop via fetch_and_store_batch).

    Steps:
      1. Summary API → tier, total_reports
      2. Use cached internal_id if available, else use provided counts (or fetch
         counts.json) → compute hash
      3. Reports endpoint → per-version counts → plurality winner
      4. Store all of: tier, recommended_proton, protondb_reports,
         protondb_internal_id, protondb_version_counts

    Returns dict with results on success, None if game has no steam_app_id
    or if all API calls failed.
    """
    try:
        game = db.get_game(game_id)
        if not game:
            return None

        try:
            steam_app_id = game["steam_app_id"]
        except (IndexError, KeyError):
            steam_app_id = None

        if not steam_app_id:
            log.debug("ProtonDB: no steam_app_id for game %d — skipping", game_id)
            return None

        # ── Step 1: Summary (tier + total) ────────────────────────────────────
        summary = fetch_summary(steam_app_id)
        tier          = summary["tier"]          if summary else "pending"
        total_reports = summary["total_reports"] if summary else 0

        # ── Step 2: Get or compute internal hash ──────────────────────────────
        try:
            cached_id = game["protondb_internal_id"]
        except (IndexError, KeyError):
            cached_id = None

        internal_id = cached_id

        if not internal_id:
            # Use provided counts or fetch globally
            c = counts
            if not c:
                c = fetch_counts()
            if c:
                internal_id = _compute_hash(steam_app_id,
                                            c["reports"], c["timestamp"])
                log.debug("ProtonDB: computed hash %d for app_id=%d",
                          internal_id, steam_app_id)
            else:
                log.warning("[PROTONDB ERROR] No counts available for app_id=%d "
                            "(game %d) — skipping reports fetch. "
                            "If this persists, the ProtonDB endpoint may have changed.",
                            steam_app_id, game_id)

        # ── Step 3: Fetch reports → count versions ────────────────────────────
        version_counts: dict = {}
        recommended_proton: Optional[str] = None

        if internal_id:
            reports_data = fetch_reports(internal_id, steam_app_id)
            if reports_data:
                version_counts = count_versions(reports_data)
                top = top_version(version_counts)
                if top:
                    recommended_proton = top
                    log.info("ProtonDB: game %d (app %d) — tier=%s  top_version=%s  "
                             "counts=%s",
                             game_id, steam_app_id, tier, top,
                             dict(sorted(version_counts.items(),
                                         key=lambda x: -x[1])[:5]))
                else:
                    log.info("ProtonDB: game %d (app %d) — tier=%s  "
                             "no usable version data in reports",
                             game_id, steam_app_id, tier)
            else:
                log.warning("[PROTONDB ERROR] Reports fetch returned no data for "
                            "game %d (app_id=%d, hash=%d). "
                            "The install dialog will show a data unavailable warning. "
                            "If this keeps happening, the ProtonDB endpoint may have changed.",
                            game_id, steam_app_id, internal_id)
                # Wipe cached hash so next refresh recomputes from scratch
                internal_id = None

        # ── Step 4: Store ─────────────────────────────────────────────────────
        db.update_protondb(
            game_id=game_id,
            tier=tier,
            recommended_proton=recommended_proton or "",
            total_reports=total_reports,
            internal_id=internal_id,
            version_counts=version_counts if version_counts else None,
        )

        return {
            "tier":               tier,
            "total_reports":      total_reports,
            "recommended_proton": recommended_proton,
            "version_counts":     version_counts,
        }

    except Exception as e:
        log.error("[PROTONDB ERROR] fetch_and_store failed for game %d: %s",
                  game_id, e, exc_info=True)
        return None


def fetch_and_store_batch(game_ids: list) -> int:
    """
    Fetch ProtonDB data for multiple games efficiently.
    Fetches counts.json ONCE and reuses it for all hash computations.
    Returns the number of games successfully updated.
    """
    counts = fetch_counts()
    if not counts:
        log.warning("[PROTONDB ERROR] Could not fetch counts.json — "
                    "batch will use cached hashes only. "
                    "Games without a cached hash will be skipped.")

    updated = 0
    for game_id in game_ids:
        result = fetch_and_store(game_id, counts=counts)
        if result:
            updated += 1
        time.sleep(0.05)
    return updated


# ── Convenience: load stored version counts for UI ───────────────────────────

def get_version_counts_for_game(game_id: int) -> dict:
    """
    Load the stored protondb_version_counts JSON for a game from DB.
    Returns dict {canonical_key: count} or empty dict.
    """
    try:
        game = db.get_game(game_id)
        if not game:
            return {}
        raw = game["protondb_version_counts"]
        if raw:
            return json.loads(raw)
    except (KeyError, IndexError, json.JSONDecodeError, Exception):
        pass
    return {}
