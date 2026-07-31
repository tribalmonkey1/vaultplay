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

import os, re, logging, datetime, time, subprocess
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

# ── Update & DLC Install Support — addon classification ────────────────────────
# Splits what SKIP_FOLDER_RE used to lump together as "ignore entirely" into
# two groups: things that genuinely are clutter (still fully ignored), and
# things that are real installable content VaultPlay should track as a
# game_addons row instead of silently dropping.
#
# Applied to BOTH subfolder names AND individual archive filenames sitting
# as loose siblings next to the base archive — confirmed necessary from real
# library data where update/DLC/crackfix content ships as a flat sibling
# file at least as often as a dedicated subfolder (e.g. breath.of.fire.iv's
# "extras-breath.of.fire.iv.rar" sits directly next to the base archive,
# no subfolder at all).

TRUE_SKIP_ADDON_RE = re.compile(
    r"(manual|manuals|doc|docs|readme|redist|redistributable|"
    r"redistributables|nfo|sample|subs|subtitles|subtitle|tool|tools)",
    re.IGNORECASE
)
UPDATE_ADDON_RE = re.compile(r"\bupdate\b|\bupdates\b|\bpatch\b|\bpatches\b", re.IGNORECASE)
CRACKFIX_ADDON_RE = re.compile(r"crack.?fix|\bcrack\b|\bfix\b", re.IGNORECASE)
DLC_ADDON_RE = re.compile(
    r"\bdlc\b|\bbonus\b|\bextras?\b|soundtrack|\bost\b|artbook|"
    r"season.?pass|content.?pack",
    re.IGNORECASE
)

# Multi-part archive volumes — used to collapse a base archive's own
# continuation files ("game.part2.rar", "game.7z.002") so they're never
# mistaken for a separate addon sitting next to the base archive.
_PART_VOLUME_RE = re.compile(r"^(.*?)[\.\-_]?part0*\d+(\.rar)$", re.IGNORECASE)
_SEVENZ_VOLUME_RE = re.compile(r"^(.*)(\.7z)\.\d+$", re.IGNORECASE)


def _addon_role(name: str) -> Optional[str]:
    """
    Classify a folder or archive filename for addon purposes.
    Returns 'update' | 'crackfix' | 'dlc' | 'skip' | None.
    'dlc' is not yet split into installable_dlc vs bonus here — that split
    needs to know whether an exe was found, which happens after peeking
    the archive, so it's resolved later in _scan_addons_for_game().
    'skip' means genuinely irrelevant clutter (manuals, redists, etc.) —
    ignored entirely, same as SKIP_FOLDER_RE always did for these.
    None means no keyword matched at all — for folders, treated as skip;
    for archive filenames, means "this isn't obviously an addon by name"
    and it's left alone rather than guessed at.
    """
    if TRUE_SKIP_ADDON_RE.search(name):
        return "skip"
    if UPDATE_ADDON_RE.search(name):
        return "update"
    if CRACKFIX_ADDON_RE.search(name):
        return "crackfix"
    if DLC_ADDON_RE.search(name):
        return "dlc"
    return None


def _logical_archive_name(filename: str) -> str:
    """Collapse a multi-part volume filename to its logical archive identity."""
    m = _PART_VOLUME_RE.match(filename)
    if m:
        return (m.group(1) + m.group(2)).lower()
    m = _SEVENZ_VOLUME_RE.match(filename)
    if m:
        return (m.group(1) + m.group(2)).lower()
    return filename.lower()


def _base_archive_group(folder: Path, base_archive_name: Optional[str]) -> set:
    """
    Return the set of filenames (at folder's top level) that belong to the
    SAME logical archive as base_archive_name — i.e. its own continuation
    volumes. These must be excluded when scanning for sibling addon archives,
    or a base game's own multi-part volumes would be misdetected as addons.
    """
    if not base_archive_name:
        return set()
    base_logical = _logical_archive_name(base_archive_name)
    group = set()
    try:
        for e in folder.iterdir():
            if e.is_file() and _logical_archive_name(e.name) == base_logical:
                group.add(e.name)
    except OSError:
        pass
    return group


# ── Update & DLC Install Support — version detection ────────────────────────────
# detect_version() contract: at most ONE of dotted/plain/date is ever
# populated per string — these are different, non-comparable numbering
# schemes, never merged. Priority within one string matters: date must be
# checked before the general dotted pattern, or a date like "14.08.2023"
# gets misread as version 14.8.2023.
#
# Patterns and their priority order below are grounded in a real-data scan
# of a 698-game library (see Notion project log), not assumed conventions —
# the dash-range "update X.Y - A.B" pattern is by far the most common
# (144 hits), followed by the explicit "patch_..._to_..." pattern (50 hits),
# then bare date versioning (38 hits). A literal "build X to build Y"
# pattern — the originally-assumed convention — turned out to appear
# exactly once in the whole library and wasn't even a genuine range.

_DATE_VERSION_RE = re.compile(r"\b(\d{1,2}\.\d{1,2}\.\d{4})\b")
_PATCH_TO_RE = re.compile(
    r"patch_.*?_to_.*?(\d+\.\d+(?:\.\d+)*)", re.IGNORECASE
)
_DASH_RANGE_RE = re.compile(
    r"update\s+\d+(?:\.\d+)*\s*-\s*(\d+(?:\.\d+)*)", re.IGNORECASE
)
_BARE_UPDATE_DOTTED_RE = re.compile(
    r"update\s+(\d+\.\d+(?:\.\d+)*)", re.IGNORECASE
)
_BARE_BUILD_RE = re.compile(r"(?<![a-zA-Z])build\s*[:\-_]?\s*(\d{3,})(?!\d)", re.IGNORECASE)
# Generic bare "vX.Y[letter]" or "X.Y.Z" with no trigger keyword at all —
# lowest priority, only used as a last resort. Needed specifically for a
# BASE game's own embedded version tag (e.g. "Darksiders III Deluxe Edition
# v1.4a", "Forza Horizon 4 ... v1.465.282") which carries no "update"/
# "patch"/"build" keyword to anchor on — confirmed a real, common pattern
# for seeding installed_version from the base archive itself.
# Uses (?<![\d.]) / (?![\d.]) instead of \b — underscore is a "word"
# character in regex terms, so \b fails to match between "_" and a digit
# (confirmed missing "1.5.5" in a real GOG-style installer exe name like
# "setup_..._1.5.5_64517_gog_(90232).exe", a very common separator style
# for this kind of filename).
_GENERIC_BARE_DOTTED_RE = re.compile(
    r"(?<![\d.])v?(\d+\.\d+(?:\.\d+)*[a-z]?)(?![\d.])", re.IGNORECASE
)


def detect_version(text: str) -> dict:
    """
    Extract a version from a single filename/folder-name string.
    Returns {"dotted": str|None, "plain": str|None, "date": str|None} —
    at most one key is ever populated.
    """
    result = {"dotted": None, "plain": None, "date": None}
    if not text:
        return result

    m = _DATE_VERSION_RE.search(text)
    if m:
        result["date"] = m.group(1)
        return result

    m = _PATCH_TO_RE.search(text)
    if m:
        result["dotted"] = m.group(1)
        return result

    m = _DASH_RANGE_RE.search(text)
    if m:
        result["dotted"] = m.group(1)
        return result

    m = _BARE_UPDATE_DOTTED_RE.search(text)
    if m:
        result["dotted"] = m.group(1)
        return result

    m = _BARE_BUILD_RE.search(text)
    if m:
        result["plain"] = m.group(1)
        return result

    # Last resort — no trigger keyword at all, just a bare version-shaped
    # substring. Deliberately checked last since it's the least specific
    # and most prone to false positives on unrelated digit.digit patterns.
    m = _GENERIC_BARE_DOTTED_RE.search(text)
    if m:
        result["dotted"] = m.group(1)
        return result

    return result


def detect_addon_version(archive_name: Optional[str], exe_names: list,
                         folder_name: Optional[str] = None) -> dict:
    """
    Detect a version for one addon, checking sources in priority order —
    archive filename first, then exe filenames inside it, then the
    containing folder name last. This order is confirmed from real library
    data: archive/exe filenames carry version info far more reliably than
    folder names (only 4 folder-level hits across 698 games scanned).
    Returns the FIRST non-empty detect_version() result — sources are never
    merged across each other.
    """
    if archive_name:
        result = detect_version(archive_name)
        if result["dotted"] or result["plain"] or result["date"]:
            return result
    for exe_name in exe_names:
        result = detect_version(exe_name)
        if result["dotted"] or result["plain"] or result["date"]:
            return result
    if folder_name:
        result = detect_version(folder_name)
        if result["dotted"] or result["plain"] or result["date"]:
            return result
    return {"dotted": None, "plain": None, "date": None}


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
    """
    List entries inside a .7z archive without extracting.

    Tries the system 7z binary first — confirmed necessary from real library
    data: py7zr's pure-Python parser fails with "invalid header data" on many
    multi-volume repack archives (.7z.001 etc.) that the system 7z binary
    reads without issue. This mirrors installer.py's _extract_7z(), which
    already prefers the system binary for the same reason. Falls back to
    py7zr if the binary isn't installed.
    """
    import shutil as _shutil
    binary = _shutil.which("7z") or _shutil.which("7za") or _shutil.which("7zz")
    if binary:
        try:
            result = subprocess.run(
                [binary, "l", "-slt", str(path)],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                names = re.findall(r"^Path = (.+)$", result.stdout, re.MULTILINE)
                # First "Path =" entry in -slt output is the archive itself
                # when listing a single/first-volume file — drop it if so.
                if names and names[0] == path.name:
                    names = names[1:]
                if names:
                    return names
            log.debug("7z binary listing failed for %s (rc=%d): %s",
                      path, result.returncode, result.stderr[:300])
        except Exception as e:
            log.debug("7z binary listing error for %s: %s", path, e)

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
    """
    List entries inside a .rar archive without extracting.

    Tries rarfile first, then falls back to the system unrar binary, then
    the system 7z binary — same rationale as _peek_7z(): the pure-Python
    path can silently fail to list some real-world RAR archives that the
    system tools read fine.
    """
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
        if names:
            return names
    except Exception as e:
        log.debug("RAR peek (rarfile) failed %s: %s", path, e)

    import shutil as _shutil
    unrar_bin = _shutil.which("unrar")
    if unrar_bin:
        try:
            result = subprocess.run(
                [unrar_bin, "lb", str(path)],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                names = [l for l in result.stdout.splitlines() if l.strip()]
                if names:
                    return names
            log.debug("unrar listing failed for %s (rc=%d)", path, result.returncode)
        except Exception as e:
            log.debug("RAR peek (unrar) failed %s: %s", path, e)

    binary_7z = _shutil.which("7z") or _shutil.which("7za") or _shutil.which("7zz")
    if binary_7z:
        try:
            result = subprocess.run(
                [binary_7z, "l", "-slt", str(path)],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                names = re.findall(r"^Path = (.+)$", result.stdout, re.MULTILINE)
                if names and names[0] == path.name:
                    names = names[1:]
                if names:
                    return names
        except Exception as e:
            log.debug("RAR peek (7z) failed %s: %s", path, e)

    log.debug("RAR peek: all methods failed for %s", path)
    return None


def _peek_zip(path):
    try:
        import zipfile
        with zipfile.ZipFile(str(path)) as z:
            return z.namelist()
    except Exception as e:
        log.debug("ZIP peek failed %s: %s", path, e)
        return None


def _prefer_non_addon(paths: list) -> list:
    """
    Filter out files whose name matches an update/DLC/crackfix keyword
    before a primary-archive selection picks among them — otherwise an
    update or crackfix archive can get chosen as "the base game" whenever
    it happens to sort alphabetically first (a confirmed real collision,
    not just a theoretical one). Falls back to the unfiltered list if
    filtering would leave nothing at all, matching real library cases
    where only a patch archive exists with no separately-named base file.
    """
    filtered = [p for p in paths if _addon_role(p.name) is None]
    return filtered if filtered else paths


def _find_primary_rar(folder):
    rars = [p for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() == ".rar"]
    if not rars:
        return None
    candidates = _prefer_non_addon(rars)
    for r in candidates:
        if re.search(r"\.part0*1\.rar$", r.name, re.IGNORECASE):
            return r
    return sorted(candidates, key=lambda p: p.name.lower())[0]


def _find_primary_7z(folder):
    vols = sorted(
        [p for p in folder.iterdir()
         if p.is_file() and re.search(r"\.7z\.\d+$", p.name, re.IGNORECASE)],
        key=lambda p: p.name.lower()
    )
    vols = _prefer_non_addon(vols)
    if vols:
        return vols[0]
    singles = [p for p in folder.iterdir()
               if p.is_file() and p.suffix.lower() == ".7z"]
    singles = _prefer_non_addon(singles)
    return singles[0] if singles else None


def _find_primary_zip(folder):
    zips = [p for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() == ".zip"]
    zips = _prefer_non_addon(zips)
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


# ── Update & DLC Install Support — addon scanning ────────────────────────────────

def _peek_archive_exes(path: Path) -> list:
    """Return .exe entry names found inside an archive, without extracting."""
    suffix = path.suffix.lower()
    names = None
    if suffix == ".rar":
        names = _peek_rar(path)
    elif suffix == ".7z":
        names = _peek_7z(path)
    elif suffix == ".zip":
        names = _peek_zip(path)
    if not names:
        return []
    return [Path(n).name for n in names if n.lower().endswith(".exe")]


def _archive_file_type(path: Path) -> str:
    return {".rar": "rar", ".7z": "7zip", ".zip": "zip"}.get(path.suffix.lower(), "loose")


def _scan_one_addon_item(game_id: int, addon_type: str, item_path: Path,
                         archive_path: Optional[Path], folder_name_for_version: Optional[str]):
    """
    Peek an addon's archive (if any) for exe names, resolve 'dlc' into
    'installable_dlc' or 'bonus' based on whether an exe was found, detect
    its version, and upsert the game_addons row.
    """
    exe_names: list = []
    file_type = None
    size_bytes = 0
    archive_name = None

    if archive_path is not None:
        exe_names = _peek_archive_exes(archive_path)
        file_type = _archive_file_type(archive_path)
        archive_name = archive_path.name
        try:
            size_bytes = archive_path.stat().st_size
        except OSError:
            size_bytes = 0
    elif item_path.is_dir():
        # Loose exe(s) directly in the addon folder, no archive at all
        try:
            exe_names = [e.name for e in item_path.iterdir()
                        if e.is_file() and e.name.lower().endswith(".exe")]
        except OSError:
            pass
        file_type = "loose"
        size_bytes = get_folder_size(item_path)

    if addon_type == "dlc":
        addon_type = "installable_dlc" if exe_names else "bonus"

    version = detect_addon_version(archive_name, exe_names, folder_name_for_version)

    try:
        db.upsert_addon(
            game_id=game_id,
            addon_type=addon_type,
            nas_path=str(item_path),
            archive_name=archive_name,
            file_type=file_type,
            size_bytes=size_bytes,
            detected_version_dotted=version["dotted"],
            detected_version_plain=version["plain"],
            detected_version_date=version["date"],
        )
        log.info("Addon found: [%s] %s (version: %s)",
                 addon_type, item_path.name,
                 version["dotted"] or version["plain"] or version["date"] or "unknown")
    except Exception as e:
        log.error("Error registering addon %s: %s", item_path, e)


def _scan_addons_for_game(game_folder: Path, game_id: int,
                          base_archive_name: Optional[str]):
    """
    Scan a game's own folder for update/installable-DLC/bonus/crackfix
    content — both loose sibling archives sitting next to the base archive
    (confirmed common in real library data) AND dedicated subfolders (the
    older, also-still-real convention). Anything genuinely irrelevant
    (manuals, redists, nfo, etc.) is still fully ignored, same as before.

    Called once per game, right after the base game itself is registered.
    Prunes any previously-registered addon whose backing file/folder is no
    longer present on this scan pass.
    """
    scan_started_at = datetime.datetime.utcnow().isoformat(sep=" ", timespec="seconds")
    exclude_files = _base_archive_group(game_folder, base_archive_name)

    try:
        entries = list(_safe_scandir(game_folder))
    except Exception:
        entries = []

    for e in entries:
        name = e.name
        path = Path(e.path)

        if e.is_file(follow_symlinks=False):
            if name in exclude_files:
                continue
            if path.suffix.lower() not in (".rar", ".7z", ".zip"):
                continue
            role = _addon_role(name)
            if role in (None, "skip"):
                continue
            _scan_one_addon_item(game_id, role, path, path, None)

        elif e.is_dir(follow_symlinks=False):
            role = _addon_role(name)
            if role in (None, "skip"):
                continue
            # This subfolder IS the addon — find its primary archive inside
            # (reusing the same primary-archive-selection logic as the base
            # game itself), or fall back to loose exe(s) directly inside it.
            archive_path = (_find_primary_rar(path) or _find_primary_7z(path)
                            or _find_primary_zip(path))
            _scan_one_addon_item(game_id, role, path, archive_path, name)

    db.prune_stale_addons(game_id, scan_started_at)

    # ── Base game's own NAS version — refreshed on every scan ──────────────
    # Distinct from addon detection above: this is "what version is the
    # base game itself, right now on the NAS" (e.g. "Darksiders III...
    # v1.4a"), stored in metadata.current_version_* via db.set_nas_version(),
    # NOT gated on installation the way installed_version_* is. A plain
    # overwrite each scan (not monotonic) — if the NAS copy gets replaced
    # with a different version, this should reflect that faithfully.
    if base_archive_name:
        base_archive_path = game_folder / base_archive_name
        base_exe_names = (_peek_archive_exes(base_archive_path)
                          if base_archive_path.exists() else [])
        base_version = detect_addon_version(
            base_archive_name, base_exe_names, game_folder.name)
        if base_version["dotted"] or base_version["plain"] or base_version["date"]:
            try:
                db.set_nas_version(game_id, **base_version)
                log.info("NAS version set for game_id=%d (%s): %s",
                         game_id, game_folder.name,
                         base_version["dotted"] or base_version["plain"]
                         or base_version["date"])
            except Exception as e:
                log.error("Error setting NAS version for game_id=%d: %s", game_id, e)
        else:
            log.debug("NAS version: none detected for game_id=%d (%s) — "
                      "archive_name=%r, peeked %d exe name(s): %s",
                      game_id, game_folder.name, base_archive_name,
                      len(base_exe_names), base_exe_names)


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
        game_id = db.upsert_game(
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
            # Track the game_id so the caller can pass newly-discovered games
            # to the version auto-track worker after the scan completes.
            # Blacklisted games are never registered (scanner skips those
            # categories entirely), so no blacklist filter needed here.
            counters.setdefault("new_game_ids", []).append(game_id)
            log.info("New game: %s [%s] in %s",
                     display_name, info["install_tag"], category)

        # Scan for updates/installable DLC/bonus content/crackfixes sitting
        # alongside the base game — runs on every scan pass (not just new
        # games), so newly-added updates for already-installed games are
        # caught the same way new games are, without a separate rescan.
        try:
            _scan_addons_for_game(folder_path, game_id, info["archive_name"])
        except Exception as e:
            log.error("Error scanning addons for %s: %s", folder_path, e)
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
        "total":        total,
        "new":          counters["new"],
        "updated":      counters["updated"],
        "errors":       counters["errors"],
        "categories":   cats_found,
        # List of game IDs for games first seen in this scan.
        # Used by VersionAutoTrackWorker to run the auto-track pass against
        # only the genuinely new games, not the whole library.
        # NOTE: blacklisted categories are already excluded here passively —
        # scanner.py never walks their folders, so no games from blacklisted
        # categories ever appear in this list. If scanner.py's blacklist
        # handling ever changes, revisit this assumption.
        "new_game_ids": counters.get("new_game_ids", []),
    }
