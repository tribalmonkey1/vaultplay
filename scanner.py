"""
scanner.py — NAS game folder scanner for VaultPlay

Structure:
  <nas_games_path>/
    <Category>/          ← direct subfolders = filter categories
      <Game>/            ← game folder (up to 3 levels deep)
        *.rar / *.7z / *.zip / loose files

Skips: update, dlc, patch, bonus, extras, soundtrack, manual, redist, etc.
Cleans folder names for display/search: strips -(12345), MULTiN-ElAmigos, etc.
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

import os, re, logging, datetime, time
from pathlib import Path
from typing import Optional

import db

log = logging.getLogger(__name__)

SKIP_FOLDER_RE = re.compile(
    r"^(update|updates|patch|patches|dlc|bonus|extras|extra|"
    r"soundtrack|ost|manual|manuals|doc|docs|readme|redist|"
    r"redistributable|redistributables|crack|fix|nfo|sample|"
    r"subs|subtitles|subtitle|bonus.content|tool|tools)$",
    re.IGNORECASE
)

INSTALLER_RE = re.compile(
    r"(^|[\\/])(setup|install|installer|autorun|autoplay|start)[^\\/]*\.exe$",
    re.IGNORECASE
)
ISO_RE       = re.compile(r"\.iso$", re.IGNORECASE)
GAME_EXE_RE  = re.compile(r"\.exe$", re.IGNORECASE)
SKIP_EXE_RE  = re.compile(
    r"(setup|install|unins|uninstall|crash|report|redist|vcredist"
    r"|directx|dxsetup|ue4prereq|dotnet|oalinst|physx|browser"
    r"|helper|update|patch)",
    re.IGNORECASE
)
RAR_PART1_MIN_ENTRIES = 3

# ── Folder name cleaning ───────────────────────────────────────────────────────

# Patterns to strip from folder names before using as display/search name
_CLEAN_PATTERNS = [
    # DODI / FitGirl style: "– [DODI Repack]" or similar
    re.compile(r"\s*[-–]\s*\[.*?Repack.*?\]\s*$", re.IGNORECASE),
    # "(v12345 + All DLCs + MULTi6)" style blocks
    re.compile(r"\s*\(v[\d. ]+.*?\)\s*$", re.IGNORECASE),
    # MULTiN-ElAmigos / MULTiN-PLAZA etc
    re.compile(r"\s*MULTi\d+[-_]\w+\s*$", re.IGNORECASE),
    # Standalone MULTiN at end
    re.compile(r"\s*MULTi\d+\s*$", re.IGNORECASE),
    # Trailing release group after dash: -CODEX -PLAZA -RUNE -CPY -EMPRESS -FLT
    re.compile(r"\s*-(?:CODEX|PLAZA|RUNE|CPY|EMPRESS|FLT|RELOADED|SKIDROW|"
               r"PROPHET|TENOKE|elamigos|ElAmigos|GOG|DODI|TiNYiSO|GoldBerg|"
               r"HOODLUM|RAZOR|REPACK|PROPER|READNFO|DARKSiDERS)\s*$",
               re.IGNORECASE),
    # -(12345) or (12345) GOG/store IDs
    re.compile(r"\s*-?\(\d{4,7}\)\s*$"),
    # [GoldBerg] [EMPRESS] [anything in brackets] at end
    re.compile(r"\s*\[.*?\]\s*$"),
    # version strings: v1.2.3 or v1 12 3
    re.compile(r"\s*v\d[\d.\s]*$", re.IGNORECASE),
    # Build NNNNN
    re.compile(r"\s*Build\s*\d+\s*$", re.IGNORECASE),
    # + Co-op
    re.compile(r"\s*\+\s*Co-op\s*$", re.IGNORECASE),
    # Trailing hyphen/dash left over after stripping
    re.compile(r"\s*[-–]\s*$"),
]

def clean_folder_name(folder_name: str) -> str:
    """
    Convert a raw folder name into a clean display/search name.
    e.g. "Sons of the Forest MULTi16-ElAmigos" → "Sons of the Forest"
         "The Medium-(49745)"                  → "The Medium"
         "against the storm-(84925)"            → "against the storm"
         "Elden Ring Shadow of the Erdtree v1 12 3" → "Elden Ring Shadow of the Erdtree"
    """
    name = folder_name.replace("_", " ").replace(".", " ")
    name = re.sub(r"\s+", " ", name).strip()
    # Run multiple passes since stacked suffixes need multiple strips
    for _ in range(4):
        prev = name
        for pat in _CLEAN_PATTERNS:
            name = pat.sub("", name).strip()
        if name == prev:
            break
    return name or folder_name


def folder_display_name(folder_name: str) -> str:
    return clean_folder_name(folder_name)


# ── File classification ────────────────────────────────────────────────────────

def _classify_file_list(names):
    for n in names:
        if ISO_RE.search(n):
            return "iso"
    for n in names:
        if INSTALLER_RE.search(n):
            return "installer"
    for n in names:
        if GAME_EXE_RE.search(n) and not SKIP_EXE_RE.search(os.path.basename(n)):
            return "portable"
    return "portable"


def _peek_7z(path):
    try:
        import py7zr
        with py7zr.SevenZipFile(str(path), mode="r") as z:
            return z.getnames()
    except Exception as e:
        log.debug("7z peek failed %s: %s", path, e)
        return None


def _find_rar_part2(p1):
    m = re.match(r"^(.+\.part)0*1(\.rar)$", p1.name, re.IGNORECASE)
    if m:
        for cand in [f"{m.group(1)}2{m.group(2)}",
                     f"{m.group(1)}02{m.group(2)}",
                     f"{m.group(1)}002{m.group(2)}"]:
            p = p1.parent / cand
            if p.exists():
                return p
    return None


def _peek_rar(path):
    try:
        import rarfile
        with rarfile.RarFile(str(path)) as rf:
            names = rf.namelist()
        if len(names) < RAR_PART1_MIN_ENTRIES:
            p2 = _find_rar_part2(path)
            if p2:
                try:
                    with rarfile.RarFile(str(p2)) as rf2:
                        names = list(set(names) | set(rf2.namelist()))
                except Exception:
                    pass
        return names
    except Exception as e:
        log.debug("RAR peek failed %s: %s", path, e)
        return None


def _peek_zip(path):
    try:
        import zipfile
        with zipfile.ZipFile(str(path)) as z:
            return z.namelist()
    except Exception as e:
        log.debug("ZIP peek failed %s: %s", path, e)
        return None


def _find_primary_rar(folder):
    rars = [p for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() == ".rar"]
    if not rars:
        return None
    for r in rars:
        if re.search(r"\.part0*1\.rar$", r.name, re.IGNORECASE):
            return r
    return sorted(rars, key=lambda p: p.name.lower())[0]


def _find_primary_7z(folder):
    vols = sorted(
        [p for p in folder.iterdir()
         if p.is_file() and re.search(r"\.7z\.\d+$", p.name, re.IGNORECASE)],
        key=lambda p: p.name.lower()
    )
    if vols:
        return vols[0]
    singles = [p for p in folder.iterdir()
               if p.is_file() and p.suffix.lower() == ".7z"]
    return singles[0] if singles else None


def _find_primary_zip(folder):
    zips = [p for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() == ".zip"]
    return sorted(zips, key=lambda p: p.name.lower())[0] if zips else None


def _safe_scandir(path):
    """scandir that skips virtual/broken paths (e.g. /dev/fd inside AppImage)."""
    try:
        path = Path(path).resolve(strict=False)
        if not path.exists() or not path.is_dir():
            return []
        return [
            e for e in os.scandir(path)
            if _path_is_real(Path(e.path))
        ]
    except (PermissionError, FileNotFoundError, OSError):
        return []


def _path_is_real(p: Path) -> bool:
    """Return False for virtual/proc/dev paths that can appear in AppImage mounts."""
    try:
        s = str(p.resolve(strict=False))
        if s.startswith("/dev/") or s.startswith("/proc/") or s.startswith("/sys/"):
            return False
        return p.exists()
    except OSError:
        return False


def get_folder_size(path):
    total = 0
    try:
        for e in _safe_scandir(path):
            try:
                if e.is_file(follow_symlinks=False):
                    total += e.stat().st_size
                elif e.is_dir(follow_symlinks=False):
                    total += get_folder_size(Path(e.path))
            except OSError:
                pass
    except Exception:
        pass
    return total


def format_size(size_bytes):
    if size_bytes < 1024:        return f"{size_bytes} B"
    elif size_bytes < 1024**2:   return f"{size_bytes/1024:.1f} KB"
    elif size_bytes < 1024**3:   return f"{size_bytes/1024**2:.1f} MB"
    else:                        return f"{size_bytes/1024**3:.2f} GB"


# ── Game folder detection ──────────────────────────────────────────────────────

def _has_game_files(folder_path: Path) -> bool:
    """True if this folder directly contains archives, ISOs, or game EXEs."""
    try:
        for e in _safe_scandir(folder_path):
            if not e.is_file(follow_symlinks=False):
                continue
            n = e.name.lower()
            if (n.endswith(".rar") or n.endswith(".7z") or n.endswith(".zip")
                    or n.endswith(".iso") or n.endswith(".exe")
                    or re.search(r"\.7z\.\d+$", n)):
                return True
    except OSError:
        pass
    return False


def classify_folder(folder_path: Path) -> dict:
    """Classify a game folder → file_type, install_tag, archive_name, size_bytes."""
    if not _path_is_real(folder_path):
        return {"file_type": "loose", "install_tag": "portable",
                "archive_name": None, "size_bytes": 0}

    try:
        rar = _find_primary_rar(folder_path)
        if rar:
            names = _peek_rar(rar)
            return {"file_type": "rar",
                    "install_tag": _classify_file_list(names) if names else "portable",
                    "archive_name": rar.name,
                    "size_bytes": get_folder_size(folder_path)}

        sz = _find_primary_7z(folder_path)
        if sz:
            names = _peek_7z(sz)
            return {"file_type": "7zip",
                    "install_tag": _classify_file_list(names) if names else "portable",
                    "archive_name": sz.name,
                    "size_bytes": get_folder_size(folder_path)}

        zp = _find_primary_zip(folder_path)
        if zp:
            names = _peek_zip(zp)
            return {"file_type": "zip",
                    "install_tag": _classify_file_list(names) if names else "portable",
                    "archive_name": zp.name,
                    "size_bytes": get_folder_size(folder_path)}

        # Loose files
        names = [e.name for e in _safe_scandir(folder_path) if e.is_file()]
        return {"file_type": "loose",
                "install_tag": _classify_file_list(names),
                "archive_name": None,
                "size_bytes": get_folder_size(folder_path)}
    except Exception as e:
        log.debug("classify_folder error %s: %s", folder_path, e)
        return {"file_type": "loose", "install_tag": "portable",
                "archive_name": None, "size_bytes": 0}


# ── Recursive game finder (up to 3 levels inside a category) ──────────────────

def _find_games_in(folder_path: Path, category: str, depth: int,
                   new_set: set, existing: set, counters: dict):
    """
    Recursively find game folders inside folder_path (up to depth levels).
    If a folder directly contains game files → register it as a game.
    Otherwise recurse one level deeper (if depth > 0).
    Skip folders matching SKIP_FOLDER_RE.
    """
    try:
        entries = sorted(
            [e for e in _safe_scandir(folder_path) if e.is_dir(follow_symlinks=False)],
            key=lambda e: e.name.lower()
        )
    except Exception:
        return

    found_any = False
    for entry in entries:
        name = entry.name
        path = Path(entry.path)

        if SKIP_FOLDER_RE.match(name):
            log.debug("Skipping: %s", path)
            continue

        if _has_game_files(path):
            _register_game(path, category, new_set, existing, counters)
            found_any = True
        elif depth > 0:
            # Recurse deeper (e.g. /Baldur's Gate/BG3/)
            _find_games_in(path, category, depth - 1, new_set, existing, counters)
            found_any = True

    # If nothing game-like found at any depth, register the folder itself
    # (handles loose-file games with no recognised extensions)
    if not found_any and depth == 0:
        if not SKIP_FOLDER_RE.match(folder_path.name):
            _register_game(folder_path, category, new_set, existing, counters)


def _register_game(folder_path: Path, category: str,
                   new_set: set, existing: set, counters: dict):
    folder_name  = folder_path.name
    display_name = folder_display_name(folder_name)
    try:
        info = classify_folder(folder_path)
        db.upsert_game(
            folder_name  = folder_name,
            nas_path     = str(folder_path),
            display_name = display_name,
            file_type    = info["file_type"],
            archive_name = info["archive_name"],
            size_bytes   = info["size_bytes"],
            install_tag  = info["install_tag"],
            category     = category,
        )
        new_set.add(folder_name)
        if folder_name in existing:
            counters["updated"] += 1
        else:
            counters["new"] += 1
            log.info("New game: %s [%s] in %s",
                     display_name, info["install_tag"], category)
    except Exception as e:
        log.error("Error registering %s: %s", folder_path, e)
        counters["errors"].append(f"{folder_name}: {e}")


# ── Public scan API ────────────────────────────────────────────────────────────

def scan_nas(nas_path: str, progress_callback=None) -> dict:
    """
    Walk nas_path. Each immediate subfolder = a category.
    Games found recursively up to 3 levels deep inside each category.
    Blacklisted categories skipped entirely.
    """
    _scan_t0 = time.monotonic()
    log.info("[SCAN] Starting scan of: %s", nas_path)
    if not nas_path or nas_path.strip() in ("", "/"):
        log.warning("scan_nas called with empty or root path — refusing to scan")
        return {"total": 0, "new": 0, "updated": 0,
                "errors": ["NAS path is not configured or is set to root (/)."],
                "categories": []}

    root = Path(nas_path)
    if not root.exists() or not root.is_dir():
        return {"total": 0, "new": 0, "updated": 0,
                "errors": [f"Path not found: {nas_path}"], "categories": []}

    try:
        cat_dirs = sorted(
            [e for e in _safe_scandir(root) if e.is_dir(follow_symlinks=False)],
            key=lambda e: e.name.lower()
        )
    except Exception as e:
        return {"total": 0, "new": 0, "updated": 0,
                "errors": [str(e)], "categories": []}

    blacklisted = db.get_blacklisted_categories()
    existing    = {g["folder_name"] for g in db.get_all_games()}
    counters    = {"new": 0, "updated": 0, "errors": []}
    new_set     = set()
    cats_found  = []

    for i, cat_entry in enumerate(cat_dirs):
        cat_name = cat_entry.name
        cat_path = Path(cat_entry.path)

        if cat_name in blacklisted:
            log.info("Skipping blacklisted category: %s", cat_name)
            # Still register in DB as blacklisted so it shows in settings UI
            # but don't overwrite blacklisted=1 flag
            db.upsert_category_safe(cat_name, sort_order=i)
            continue

        if progress_callback:
            progress_callback(i + 1, len(cat_dirs), cat_name)

        db.upsert_category(cat_name, sort_order=i)
        cats_found.append(cat_name)

        # Search up to 3 levels deep inside the category for game folders
        _find_games_in(cat_path, cat_name, depth=2,
                       new_set=new_set, existing=existing, counters=counters)

    total = counters["new"] + counters["updated"]
    db.set_setting("last_scan_result",
                   f"{total} games, {counters['new']} new, {counters['updated']} updated")
    db.set_setting("last_scan_time", datetime.datetime.now().isoformat())
    log.info("[SCAN] Complete: %d total, %d new, %d updated, %d errors in %.1f s",
             total, counters["new"], counters["updated"], len(counters["errors"]),
             time.monotonic())

    return {
        "total":      total,
        "new":        counters["new"],
        "updated":    counters["updated"],
        "errors":     counters["errors"],
        "categories": cats_found,
    }
