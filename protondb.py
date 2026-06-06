"""
protondb.py — ProtonDB integration + installed Proton version detection for VaultPlay

- Scans system for actually-installed Proton/Wine versions
  (Steam official, GE-Proton, Lutris, system Wine)
- Uses the official ProtonDB summary API (tier/score/total) to recommend
  the best installed version for each game
- Normalizes version strings for display
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

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "VaultPlay/1.0"
_adapter = requests.adapters.HTTPAdapter(pool_connections=2, pool_maxsize=2, max_retries=1)
SESSION.mount("https://", _adapter)
SESSION.mount("http://", _adapter)

PROTONDB_SUMMARY_API = "https://www.protondb.com/api/v1/reports/summaries/{app_id}.json"
# The official API only exposes tier/score/total — no per-report version data.
# The mirror provides individual reports. Confirmed response format from live API:
#   { "id": int, "appId": int, "timestamp": int (unix epoch),
#     "protonVersion": str, "rating": str, "notes": str, ... }
# Reports are returned in oldest-first order, so we sort by timestamp descending.
PROTONDB_REPORTS_API = "https://protondb.max-p.me/games/{app_id}/reports"

TIER_INFO = {
    "platinum": ("Platinum", "#b4c7dc"),
    "gold":     ("Gold",     "#CFB53B"),
    "silver":   ("Silver",   "#A8A9AD"),
    "bronze":   ("Bronze",   "#CD7F32"),
    "borked":   ("Borked",   "#f87171"),
    "pending":  ("Pending",  "#6b7280"),
}


# ── Steam library path discovery ──────────────────────────────────────────────

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
    """Build full list of directories to scan for Proton/Wine versions."""
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


# ── Version normalization ──────────────────────────────────────────────────────

def _normalize_version(ver: str) -> str:
    """
    Normalize a folder/dir name to a clean display label.
    'GE-Proton9-27'         -> 'GE-Proton 9.27'
    'Proton - Experimental' -> 'Proton Experimental'
    'Proton Experimental'   -> 'Proton Experimental'
    'Proton 9.0'            -> 'Proton 9.0'
    'proton-9.0-4'          -> 'Proton 9.0'
    'wine-ge-8-26-x86_64'   -> 'Wine-GE 8.26'
    """
    ver = ver.strip()

    # GE-ProtonN-M (e.g. GE-Proton9-27, Lutris-GE-Proton9-3-x86_64)
    m = re.match(r"(?:Lutris-)?GE-Proton(\d+)-(\d+)", ver, re.IGNORECASE)
    if m:
        return f"GE-Proton {m.group(1)}.{m.group(2)}"

    # Proton Experimental (various spellings)
    if re.search(r"proton.{0,4}experimental", ver, re.IGNORECASE):
        return "Proton Experimental"

    # Proton N.M[-suffix]
    m = re.match(r"Proton[\s_-]+(\d+\.\d+)", ver, re.IGNORECASE)
    if m:
        rest = ver[m.end():].strip(" -_")
        if rest and not rest.startswith("("):
            rest = re.sub(r"^[-_\s]+", "", rest)
            return f"Proton {m.group(1)} {rest}".strip()
        return f"Proton {m.group(1)}"

    # wine-ge-N-M-x86_64
    m = re.match(r"wine-ge-(\d+)-(\d+)", ver, re.IGNORECASE)
    if m:
        return f"Wine-GE {m.group(1)}.{m.group(2)}"

    # proton-experimental (Lutris runner naming)
    if re.match(r"proton-experimental", ver, re.IGNORECASE):
        return "Proton Experimental"

    return ver


# ── Version detection ─────────────────────────────────────────────────────────

def _dir_is_wine_version(path: Path) -> bool:
    """Return True if this directory looks like a Wine or Proton installation."""
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
    """Scan all known locations for installed Proton/Wine versions."""
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
                log.debug("Found Proton/Wine: %s at %s", label, entry)

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

    result = list(found.values())
    if not result:
        log.warning("No Proton/Wine versions found on this system.")
    log.info("Found %d Proton/Wine version(s): %s",
             len(result), [r["label"] for r in result])
    return result


def get_versions_for_ui() -> list[tuple[str, str]]:
    """Return (display_label, value) tuples sorted for UI dropdown."""
    versions = list(_get_cached_installed())  # copy so sort doesn't mutate the cache

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


# ── ProtonDB API ──────────────────────────────────────────────────────────────

def fetch_protondb(steam_app_id: int) -> Optional[dict]:
    try:
        url = PROTONDB_SUMMARY_API.format(app_id=steam_app_id)
        resp = SESSION.get(url, timeout=10)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        resp.close()
        return {
            "tier":          data.get("tier", "pending"),
            "score":         data.get("score", 0.0),
            "trending_tier": data.get("trendingTier", "pending"),
            "total_reports": data.get("total", 0),
        }
    except Exception as e:
        log.warning("ProtonDB summary fetch failed for %d: %s", steam_app_id, e)
        return None


def get_most_recommended_version(steam_app_id: int, max_reports: int = 30) -> Optional[str]:
    """
    Fetch the most recent ProtonDB community reports and return the most commonly
    reported Proton version as a canonical key (e.g. '9.0', 'GE-Proton 10').

    The mirror returns all reports in oldest-first order. We sort by the confirmed
    'timestamp' field (unix epoch) descending so we read the most recent reports,
    not years-old ones that reflect ancient Proton versions.

    Returns None if the mirror is unavailable or has no useful data for this game,
    so the caller can fall back to the tier heuristic.
    """
    try:
        url = PROTONDB_REPORTS_API.format(app_id=steam_app_id)
        resp = SESSION.get(url, timeout=15)
        if resp.status_code == 404:
            log.debug("ProtonDB mirror: no reports for app_id=%d", steam_app_id)
            return None
        if resp.status_code != 200:
            log.debug("ProtonDB mirror returned HTTP %d for app_id=%d",
                      resp.status_code, steam_app_id)
            return None
        reports = resp.json()
        resp.close()
    except Exception as e:
        log.warning("ProtonDB mirror fetch failed for app_id=%d: %s", steam_app_id, e)
        return None

    if not reports:
        return None

    # Sort newest-first by confirmed 'timestamp' field (unix epoch int).
    # Without this, [:max_reports] reads the oldest reports in the dataset.
    reports = sorted(reports, key=lambda r: r.get("timestamp", 0), reverse=True)

    counts: dict[str, int] = {}
    for report in reports[:max_reports]:
        # Confirmed field name from live API response: 'protonVersion' (camelCase)
        ver_raw = report.get("protonVersion") or report.get("proton_version") or ""
        if not ver_raw:
            continue
        normalized = _parse_major_version(str(ver_raw))
        if normalized and normalized != "native":
            counts[normalized] = counts.get(normalized, 0) + 1

    if not counts:
        log.debug("ProtonDB mirror: %d reports for app_id=%d but none had version data",
                  min(len(reports), max_reports), steam_app_id)
        return None

    best = max(counts, key=lambda k: counts[k])
    log.info("ProtonDB mirror: app_id=%d top %d reports → %s → best: %s",
             steam_app_id, max_reports,
             dict(sorted(counts.items(), key=lambda x: -x[1])), best)
    return best


def _parse_major_version(ver_str: str) -> str:
    """
    Normalize a ProtonDB report version string to a canonical key.
    'Proton 9.0-4'        -> '9.0'
    'Proton Experimental' -> 'experimental'
    'GE-Proton10-27'      -> 'GE-Proton 10'
    'native'              -> 'native'
    """
    if not ver_str:
        return ""
    s = ver_str.strip()
    if re.search(r"^native$", s, re.IGNORECASE):
        return "native"
    if re.search(r"experimental", s, re.IGNORECASE):
        return "experimental"
    m = re.match(r"GE-Proton(\d+)", s, re.IGNORECASE)
    if m:
        return f"GE-Proton {m.group(1)}"
    m = re.search(r"[Pp]roton\s*(\d+)\.(\d+)", s)
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    m = re.search(r"[Pp]roton\s*(\d+)", s)
    if m:
        return m.group(1)
    m = re.match(r"wine-ge-(\d+)", s, re.IGNORECASE)
    if m:
        return f"Wine-GE {m.group(1)}"
    return s


def match_to_installed(recommended_version: str,
                        installed: list[tuple[str, str]]) -> Optional[str]:
    """
    Match a canonical version key (e.g. '9.0', 'GE-Proton 10') to the closest
    installed version value. For numbered versions, picks the newest minor version
    of the matching major. Returns None if nothing matches.
    """
    if not recommended_version or not installed:
        return None

    rec = recommended_version.lower().strip()

    if rec == "native":
        return None

    if rec == "experimental":
        for label, value in installed:
            if "experimental" in label.lower():
                return value
        return None

    # GE-Proton N — pick highest installed minor of that major
    m = re.match(r"ge-proton\s*(\d+)", rec)
    if m:
        major = m.group(1)
        best_val, best_minor = None, -1
        for label, value in installed:
            lbl = label.lower()
            mm = re.search(r"ge-proton\s*" + major + r"\.(\d+)", lbl)
            if mm:
                minor = int(mm.group(1))
                if minor > best_minor:
                    best_minor, best_val = minor, value
        if best_val:
            return best_val

    # Stable Proton N.M — exact match first, then major-only
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

    # Major-only integer (e.g. rec = "9")
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


def recommended_proton_for_game(steam_app_id: int,
                                 installed: list[tuple[str, str]]) -> tuple[Optional[str], str]:
    """
    Get the recommended Proton version for a game.

    Priority:
      1. ProtonDB mirror  — most-reported version from recent community reports,
                            matched to an installed version
      2. Tier heuristic  — uses official ProtonDB tier (platinum/gold/silver/etc.)
                            to pick a sensible installed version
      3. User default setting
      4. First installed version
    """
    # ── 1. Community reports (mirror) ────────────────────────────────────────
    rec_ver = get_most_recommended_version(steam_app_id)
    if rec_ver:
        matched = match_to_installed(rec_ver, installed)
        if matched:
            return matched, f"Recommended by ProtonDB community ({rec_ver})"
        # Mirror had data but the recommended version isn't installed —
        # fall through to tier heuristic rather than returning "not installed"
        log.info("ProtonDB mirror recommends %s for app_id=%d but it is not installed "
                 "— falling back to tier heuristic", rec_ver, steam_app_id)

    # ── 2. Tier heuristic (official summary API) ─────────────────────────────
    summary = fetch_protondb(steam_app_id)
    if summary:
        tier = summary.get("tier", "pending")

        stable_proton = [
            (lbl, val) for lbl, val in installed
            if "proton" in lbl.lower()
            and "ge" not in lbl.lower()
            and "experimental" not in lbl.lower()
            and "hotfix" not in lbl.lower()
        ]
        ge_proton = [
            (lbl, val) for lbl, val in installed
            if "ge-proton" in lbl.lower()
        ]
        experimental = [
            (lbl, val) for lbl, val in installed
            if "experimental" in lbl.lower()
        ]

        if tier in ("platinum", "gold"):
            if stable_proton:
                return stable_proton[0][1], f"Tier: {tier} — recommended stable Proton"
            if ge_proton:
                return ge_proton[0][1], f"Tier: {tier} — recommended GE-Proton"
        elif tier in ("silver", "pending"):
            if ge_proton:
                return ge_proton[0][1], f"Tier: {tier} — recommended GE-Proton"
            if stable_proton:
                return stable_proton[0][1], f"Tier: {tier} — recommended stable Proton"
        elif tier == "bronze":
            if ge_proton:
                return ge_proton[0][1], f"Tier: {tier} — recommended GE-Proton"
            if experimental:
                return experimental[0][1], f"Tier: {tier} — try Experimental"
        if experimental:
            return experimental[0][1], f"Tier: {tier} — try Experimental"
        if installed:
            return installed[0][1], f"Tier: {tier} — using newest installed version"

    # ── 3. User default / first installed ────────────────────────────────────
    try:
        saved_default = db.get_setting("default_proton_version", "")
        if saved_default:
            for label, value in installed:
                if value == saved_default:
                    return value, "User default (no ProtonDB data)"
    except Exception:
        pass

    if installed:
        return installed[0][1], "Default (first installed version)"
    return None, "No Proton/Wine versions found — install via ProtonUp-Qt or Lutris"


def tier_display(tier: str) -> tuple[str, str]:
    return TIER_INFO.get((tier or "").lower(), ("Unknown", "#6b7280"))


# Module-level cache so proton_version_label() and get_versions_for_ui()
# don't re-scan the filesystem on every call.
_installed_cache: list[dict] = []
_installed_cache_ts: float = 0.0
_CACHE_TTL: float = 120.0   # seconds


def _get_cached_installed() -> list[dict]:
    global _installed_cache, _installed_cache_ts
    if not _installed_cache or (time.monotonic() - _installed_cache_ts) > _CACHE_TTL:
        _installed_cache = scan_installed_versions()
        _installed_cache_ts = time.monotonic()
    return _installed_cache


def proton_version_label(value: str) -> str:
    """Return the human-readable label for a stored version value key."""
    for v in _get_cached_installed():
        if v["value"] == value:
            return v["label"]
    return value


def fetch_and_store(game_id: int) -> Optional[dict]:
    """
    Fetch ProtonDB tier summary and store version recommendation in DB.

    Flow:
      1. fetch_protondb()              → tier, score, total_reports (official summary API)
      2. recommended_proton_for_game() → tier heuristic → best installed version
      3. Store both in metadata row
    """
    try:
        game = db.get_game(game_id)
        if not game:
            return None

        steam_app_id = None
        try:
            steam_app_id = game["steam_app_id"]
        except (IndexError, KeyError):
            pass

        if not steam_app_id:
            log.debug("No Steam App ID for game %d — skipping ProtonDB", game_id)
            return None

        data = fetch_protondb(steam_app_id)
        if not data:
            log.debug("ProtonDB: no summary for steam_app_id=%d (game %d)", steam_app_id, game_id)
            return None

        tier      = data["tier"]
        installed = get_versions_for_ui()

        matched_value, rec_reason = recommended_proton_for_game(steam_app_id, installed)
        rec_value = matched_value or (installed[0][1] if installed else "")

        db.update_protondb(game_id, tier, rec_value, data.get("total_reports", 0))
        log.info(
            "ProtonDB: game %d (app %d) — tier=%s  rec=%s  reason=%s",
            game_id, steam_app_id, tier, rec_value or "none", rec_reason,
        )
        return {**data, "recommended_proton": rec_value}
    except Exception as e:
        log.warning("fetch_and_store failed for game %d: %s", game_id, e)
        return None
