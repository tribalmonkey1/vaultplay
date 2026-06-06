"""
protondb.py — ProtonDB integration + installed Proton version detection for VaultPlay

Data source for version recommendations:
  - Monthly data dumps from github.com/bdefore/protondb-data (complete, reliable,
    offline after first download, updated ~monthly)
  - Downloaded once, parsed into a compact per-game index stored at
    ~/.config/vaultplay/protondb_index.json
  - "Refresh ProtonDB Data" checks for a newer monthly dump and re-indexes if found

Tier/score data:
  - Official ProtonDB summary API (protondb.com) — tier, score, total reports
  - Used as fallback when index has no data for a game, and always fetched for
    the tier badge display
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

import gzip
import hashlib
import io
import json
import os
import re
import logging
import tarfile
import time
import requests
import requests.adapters
from pathlib import Path
from typing import Optional

import db

log = logging.getLogger(__name__)

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "VaultPlay/1.0"
_adapter = requests.adapters.HTTPAdapter(pool_connections=2, pool_maxsize=4, max_retries=1)
SESSION.mount("https://", _adapter)
SESSION.mount("http://", _adapter)

# ── Constants ─────────────────────────────────────────────────────────────────

PROTONDB_SUMMARY_API  = "https://www.protondb.com/api/v1/reports/summaries/{app_id}.json"
PROTONDB_DUMP_API     = "https://api.github.com/repos/bdefore/protondb-data/contents/reports"
PROTONDB_DUMP_BASE    = "https://github.com/bdefore/protondb-data/raw/master/reports/{filename}"

# How many recent reports per game to keep in the index.
# Newer reports are weighted higher; we only count the most recent MAX_REPORTS_PER_GAME.
MAX_REPORTS_PER_GAME  = 50

TIER_INFO = {
    "platinum": ("Platinum", "#b4c7dc"),
    "gold":     ("Gold",     "#CFB53B"),
    "silver":   ("Silver",   "#A8A9AD"),
    "bronze":   ("Bronze",   "#CD7F32"),
    "borked":   ("Borked",   "#f87171"),
    "pending":  ("Pending",  "#6b7280"),
}


# ── Index paths ───────────────────────────────────────────────────────────────

def _config_dir() -> Path:
    env = os.environ.get("VAULTPLAY_CONFIG_DIR")
    if env:
        return Path(env)
    return Path.home() / ".config" / "vaultplay"


def _index_path() -> Path:
    return _config_dir() / "protondb_index.json"


def _meta_path() -> Path:
    """Stores the filename of the dump currently indexed, for update checks."""
    return _config_dir() / "protondb_index_meta.json"


# ── Index management ──────────────────────────────────────────────────────────

# In-memory cache of the loaded index: {str(app_id): [version_str, ...]}
# Versions are already sorted newest-first and limited to MAX_REPORTS_PER_GAME.
_index_cache: dict = {}
_index_loaded: bool = False


def _load_index() -> dict:
    """Load the local index into memory. Returns empty dict if not built yet."""
    global _index_cache, _index_loaded
    if _index_loaded:
        return _index_cache
    p = _index_path()
    if p.exists():
        try:
            _index_cache = json.loads(p.read_text())
            _index_loaded = True
            log.info("ProtonDB index loaded: %d games", len(_index_cache))
        except Exception as e:
            log.warning("Failed to load ProtonDB index: %s", e)
            _index_cache = {}
    else:
        log.info("ProtonDB index not yet built — will use tier heuristic until first download")
    return _index_cache


def _invalidate_index_cache():
    """Force a reload from disk on next access."""
    global _index_loaded
    _index_loaded = False


def get_index_meta() -> dict:
    """Return metadata about the current index (which dump file it came from)."""
    p = _meta_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}


def _save_index(index: dict, dump_filename: str, total_reports: int):
    """Write the index and its metadata to disk."""
    _config_dir().mkdir(parents=True, exist_ok=True)
    _index_path().write_text(json.dumps(index, separators=(",", ":")))
    _meta_path().write_text(json.dumps({
        "dump_filename":  dump_filename,
        "total_reports":  total_reports,
        "indexed_at":     time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }, indent=2))
    _invalidate_index_cache()
    log.info("ProtonDB index saved: %d games from %s", len(index), dump_filename)


# ── Dump discovery ────────────────────────────────────────────────────────────

def get_latest_dump_filename() -> Optional[str]:
    """
    Query GitHub to find the most recent monthly dump filename.
    Returns e.g. 'reports_jun1_2026.tar.gz' or None on failure.
    """
    try:
        resp = SESSION.get(PROTONDB_DUMP_API, timeout=15)
        resp.raise_for_status()
        files = resp.json()
        resp.close()
    except Exception as e:
        log.warning("Failed to list ProtonDB dump files: %s", e)
        return None

    # Filter to .tar.gz report files and sort by embedded date
    def _dump_sort_key(name: str):
        # e.g. "reports_jun1_2026.tar.gz"
        months = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
                  "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}
        m = re.search(r"reports_([a-z]+)(\d+)_(\d{4})", name)
        if m:
            mon = months.get(m.group(1).lower(), 0)
            day = int(m.group(2))
            yr  = int(m.group(3))
            return (yr, mon, day)
        return (0, 0, 0)

    dumps = [
        f["name"] for f in files
        if isinstance(f, dict) and f.get("name", "").startswith("reports_")
        and f["name"].endswith(".tar.gz")
    ]
    if not dumps:
        return None

    dumps.sort(key=_dump_sort_key, reverse=True)
    return dumps[0]


def needs_update() -> tuple[bool, str, str]:
    """
    Check if a newer dump is available.
    Returns (needs_update, current_filename, latest_filename).
    """
    latest = get_latest_dump_filename()
    if not latest:
        return False, "", ""
    current = get_index_meta().get("dump_filename", "")
    return (latest != current), current, latest


# ── Dump download + index build ───────────────────────────────────────────────

def build_index_from_dump(dump_filename: str,
                           progress_cb=None) -> bool:
    """
    Download a ProtonDB dump, parse it, and build the local index.

    The dump is a .tar.gz containing a single JSON file — a flat array of
    report objects. Each object has:
      app.steam.appId    — Steam App ID string
      responses.protonVersion — version string e.g. "Proton 9.0-4"
      timestamp          — unix epoch int

    We group by app_id, sort each game's reports newest-first, keep the top
    MAX_REPORTS_PER_GAME, and store just the normalized version strings.

    progress_cb(stage: str, percent: int, message: str)
    """
    def prog(stage, pct, msg):
        if progress_cb:
            progress_cb(stage, pct, msg)
        log.info("[ProtonDB dump] %s %d%% — %s", stage, pct, msg)

    url = PROTONDB_DUMP_BASE.format(filename=dump_filename)
    prog("Downloading", 0, f"Downloading {dump_filename}…")

    try:
        resp = SESSION.get(url, timeout=120, stream=True)
        resp.raise_for_status()

        # Stream into memory (66MB compressed → ~300MB uncompressed)
        # We stream the download but decompress in one shot
        total_size = int(resp.headers.get("content-length", 0))
        downloaded = 0
        chunks = []
        for chunk in resp.iter_content(chunk_size=65536):
            chunks.append(chunk)
            downloaded += len(chunk)
            if total_size:
                pct = int(downloaded / total_size * 40)
                prog("Downloading", pct, f"Downloaded {downloaded // 1024 // 1024} MB…")
        resp.close()
        raw = b"".join(chunks)
        prog("Downloading", 40, "Download complete, parsing…")
    except Exception as e:
        log.error("Failed to download dump %s: %s", dump_filename, e)
        return False

    # Parse tar.gz → extract the JSON file inside
    prog("Parsing", 42, "Extracting archive…")
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
            # Find the JSON member
            json_member = next(
                (m for m in tar.getmembers() if m.name.endswith(".json")), None
            )
            if not json_member:
                log.error("No JSON file found in dump %s", dump_filename)
                return False
            json_bytes = tar.extractfile(json_member).read()
        prog("Parsing", 50, "Parsing JSON…")
    except Exception as e:
        log.error("Failed to extract dump %s: %s", dump_filename, e)
        return False

    try:
        reports = json.loads(json_bytes)
        prog("Parsing", 65, f"Parsed {len(reports):,} reports, building index…")
    except Exception as e:
        log.error("Failed to parse JSON from dump %s: %s", dump_filename, e)
        return False

    # Build index: {app_id_str: [(timestamp, normalized_version), ...]}
    # We collect all (timestamp, version) pairs per game first, then sort and trim
    prog("Indexing", 67, "Grouping by game…")
    raw_index: dict[str, list] = {}
    skipped = 0
    for report in reports:
        try:
            app_id = str(report["app"]["steam"]["appId"])
            ver_raw = report["responses"].get("protonVersion", "")
            ts = int(report.get("timestamp", 0))
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue

        if not ver_raw or ver_raw.lower() in ("", "default", "unknown"):
            continue

        normalized = _parse_major_version(ver_raw)
        if not normalized or normalized == "native":
            continue

        if app_id not in raw_index:
            raw_index[app_id] = []
        raw_index[app_id].append((ts, normalized))

    prog("Indexing", 85, f"Sorting and trimming {len(raw_index):,} games…")

    # Sort each game newest-first, keep only top MAX_REPORTS_PER_GAME version strings
    final_index: dict[str, list] = {}
    for app_id, entries in raw_index.items():
        entries.sort(key=lambda x: x[0], reverse=True)
        final_index[app_id] = [v for _, v in entries[:MAX_REPORTS_PER_GAME]]

    prog("Saving", 95, f"Saving index ({len(final_index):,} games)…")
    _save_index(final_index, dump_filename, len(reports))
    prog("Done", 100, f"Index built: {len(final_index):,} games, {len(reports):,} reports")
    return True


# ── Version lookup from index ─────────────────────────────────────────────────

def get_most_recommended_version(steam_app_id: int, max_reports: int = 30) -> Optional[str]:
    """
    Look up the most commonly reported Proton version for a game from the
    local index. Returns a canonical key e.g. '9.0', 'GE-Proton 10', or None.
    """
    index = _load_index()
    entries = index.get(str(steam_app_id))
    if not entries:
        log.debug("ProtonDB index: no data for app_id=%d", steam_app_id)
        return None

    # entries is already sorted newest-first; take top max_reports
    counts: dict[str, int] = {}
    for ver in entries[:max_reports]:
        counts[ver] = counts.get(ver, 0) + 1

    best = max(counts, key=lambda k: counts[k])
    log.info("ProtonDB index: app_id=%d, %d reports → %s → best: %s",
             steam_app_id, len(entries),
             dict(sorted(counts.items(), key=lambda x: -x[1])), best)
    return best


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


def _parse_major_version(ver_str: str) -> str:
    """
    Normalize a ProtonDB report version string to a canonical key for counting.
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


# ── Installed version detection ───────────────────────────────────────────────

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


# ── Installed version cache ────────────────────────────────────────────────────

_installed_cache: list[dict] = []
_installed_cache_ts: float = 0.0
_CACHE_TTL: float = 120.0


def _get_cached_installed() -> list[dict]:
    global _installed_cache, _installed_cache_ts
    if not _installed_cache or (time.monotonic() - _installed_cache_ts) > _CACHE_TTL:
        _installed_cache = scan_installed_versions()
        _installed_cache_ts = time.monotonic()
    return _installed_cache


def get_versions_for_ui() -> list[tuple[str, str]]:
    """Return (display_label, value) tuples sorted for UI dropdown."""
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


# ── ProtonDB official summary API ─────────────────────────────────────────────

def fetch_protondb(steam_app_id: int) -> Optional[dict]:
    """Fetch tier/score/total from the official ProtonDB summary API."""
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


# ── Version matching ──────────────────────────────────────────────────────────

def match_to_installed(recommended_version: str,
                        installed: list[tuple[str, str]]) -> Optional[str]:
    """
    Match a canonical version key to the closest installed version.
    For GE-Proton N, picks the highest installed minor of that major.
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

    m = re.match(r"^(\d+)$", rec)
    if m:
        major = m.group(1)
        for label, value in installed:
            lbl = label.lower()
            if "ge" not in lbl and re.search(r"\b" + major + r"\b", lbl):
                return value

    for label, value in installed:
        if rec in label.lower():
            return value

    return None


# ── Recommendation logic ──────────────────────────────────────────────────────

def recommended_proton_for_game(steam_app_id: int,
                                 installed: list[tuple[str, str]]) -> tuple[Optional[str], str]:
    """
    Get the recommended Proton version for a game.

    Priority:
      1. Local index (built from monthly GitHub dump) — most-reported version
         from community reports, matched to an installed version
      2. Tier heuristic — official ProtonDB tier drives selection when index
         has no data for this game
      3. User default setting
      4. First installed version
    """
    # ── 1. Local index ────────────────────────────────────────────────────────
    rec_ver = get_most_recommended_version(steam_app_id)
    if rec_ver:
        matched = match_to_installed(rec_ver, installed)
        if matched:
            return matched, f"Recommended by ProtonDB community ({rec_ver})"
        log.info("ProtonDB index recommends %s for app_id=%d but it is not installed "
                 "— falling back to tier heuristic", rec_ver, steam_app_id)

    # ── 2. Tier heuristic ─────────────────────────────────────────────────────
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

    # ── 3. Fallbacks ──────────────────────────────────────────────────────────
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


# ── Tier display + label helpers ──────────────────────────────────────────────

def tier_display(tier: str) -> tuple[str, str]:
    return TIER_INFO.get((tier or "").lower(), ("Unknown", "#6b7280"))


def proton_version_label(value: str) -> str:
    for v in _get_cached_installed():
        if v["value"] == value:
            return v["label"]
    return value


# ── fetch_and_store ───────────────────────────────────────────────────────────

def fetch_and_store(game_id: int) -> Optional[dict]:
    """
    Compute and store the ProtonDB recommendation for a single game.

    Uses the local index for version recommendation and the official summary
    API for tier/score/total. Called for each game during a ProtonDB refresh.
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
            log.debug("ProtonDB: no summary for steam_app_id=%d (game %d)",
                      steam_app_id, game_id)
            return None

        tier      = data["tier"]
        installed = get_versions_for_ui()

        matched_value, rec_reason = recommended_proton_for_game(steam_app_id, installed)
        rec_value = matched_value or (installed[0][1] if installed else "")

        db.update_protondb(game_id, tier, rec_value, data.get("total_reports", 0))
        log.info("ProtonDB: game %d (app %d) — tier=%s  rec=%s  reason=%s",
                 game_id, steam_app_id, tier, rec_value or "none", rec_reason)
        return {**data, "recommended_proton": rec_value}
    except Exception as e:
        log.warning("fetch_and_store failed for game %d: %s", game_id, e)
        return None
