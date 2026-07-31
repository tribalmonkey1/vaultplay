"""
installer.py — Game installation engine for VaultPlay

Three install flows based on install_tag:
  'installer' — extract archive → run setup.exe via Wine prefix
  'iso'       — extract archive → mount ISO → run setup.exe via Wine prefix → unmount
  'portable'  — extract archive → move files to chosen game_path →
                create Wine prefix → generate .desktop (direct or script wrapper)

Wine prefix path: ~/.local/share/wineprefixes/<name>
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

import os
import re
import shutil
import subprocess
import logging
import tempfile
from pathlib import Path
from typing import Optional, Callable

import db
import scanner

log = logging.getLogger(__name__)

_WINE_PREFIX_ROOT_DEFAULT = Path.home() / ".local" / "share" / "wineprefixes"


def _build_wine_env(wine_bin: str, prefix_path: Path) -> dict:
    """
    Build the environment dict for a Wine/Proton subprocess call.
    For plain wine/wine64: just set WINEPREFIX.
    For Proton: set STEAM_COMPAT_DATA_PATH to prefix_path (Proton creates
    pfx/ inside it), and set WINEPREFIX to prefix_path/pfx/.
    """
    is_proton = Path(wine_bin).name == "proton"

    if is_proton:
        wineprefix = str(prefix_path / "pfx")
    else:
        wineprefix = str(prefix_path)

    env = {**os.environ, "WINEPREFIX": wineprefix}

    if is_proton:
        proton_dir = Path(wine_bin).parent
        env.update({
            "STEAM_COMPAT_DATA_PATH":           str(prefix_path),
            "STEAM_COMPAT_CLIENT_INSTALL_PATH": _find_steam_root(),
            "PROTON_LOG":                       "0",
            "PROTON_DIR":                       str(proton_dir),
        })
        log.info("Proton env: STEAM_COMPAT_DATA_PATH=%s  WINEPREFIX=%s",
                 prefix_path, wineprefix)

    return env


def _find_steam_root() -> str:
    """Return the Steam installation root, or a safe default."""
    candidates = [
        Path.home() / ".steam" / "steam",
        Path.home() / ".steam" / "root",
        Path.home() / ".local" / "share" / "Steam",
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return str(Path.home() / ".steam" / "steam")


def _resolve_wine_bin(version_path: str) -> str:
    """
    Given a Proton/Wine installation directory (as returned by
    protondb.get_version_path()), find the actual executable to invoke.

    Proton directories contain a 'proton' script at their root.
    GE-Proton and Lutris Wine builds expose wine/wine64 under bin/ or
    files/bin/ or dist/bin/. Falls back to system 'wine' if nothing found.
    """
    if not version_path or version_path == "wine":
        return "wine"
    base = Path(version_path)
    # Proton (Steam official + GE-Proton): root-level 'proton' script
    proton_script = base / "proton"
    if proton_script.exists() and os.access(str(proton_script), os.X_OK):
        return str(proton_script)
    # Lutris / Wine-GE: bin/wine64 or bin/wine
    for subdir in ("bin", "files/bin", "dist/bin"):
        for binary in ("wine64", "wine"):
            candidate = base / subdir / binary
            if candidate.exists() and os.access(str(candidate), os.X_OK):
                return str(candidate)
    log.warning("Could not find executable in %s — falling back to system wine",
                version_path)
    return "wine"


def _wine_prefix_root() -> Path:
    """Read the wine prefix root from settings, falling back to the standard path."""
    try:
        s = db.get_setting("wine_prefix_root", "")
        if s:
            p = Path(s).expanduser()
            p.mkdir(parents=True, exist_ok=True)
            return p
    except Exception:
        pass
    _WINE_PREFIX_ROOT_DEFAULT.mkdir(parents=True, exist_ok=True)
    return _WINE_PREFIX_ROOT_DEFAULT

COMMON_REDISTS = [
    "vcrun2005", "vcrun2008", "vcrun2010", "vcrun2012",
    "vcrun2013", "vcrun2015", "vcrun2017", "vcrun2019", "vcrun2022",
    "dotnet40", "dotnet48",
    "d3dx9", "d3dx10", "d3dx11", "d3dcompiler_47",
    "xact", "physx", "openal",
]


def make_prefix_name(folder_name: str) -> str:
    name = folder_name.lower()
    name = re.sub(r"[^a-z0-9]+", "_", name)
    return name.strip("_")


# ── Extraction ────────────────────────────────────────────────────────────────

def _extract_7z(archive_path: Path, dest: Path, progress_cb=None) -> bool:
    """
    Extract a .7z archive.
    Tries the system 7z binary first (real progress bar, handles all methods).
    Falls back to py7zr if the binary isn't installed.
    """
    import shutil as _shutil
    binary = _shutil.which("7z") or _shutil.which("7za") or _shutil.which("7zz")
    if binary:
        return _extract_7z_binary(archive_path, dest, progress_cb, binary)

    # No system binary — fall back to py7zr (no progress, but works)
    log.warning("7z binary not found — falling back to py7zr (no progress bar). "
                "Install p7zip for progress: sudo pacman -S p7zip")
    return _extract_7z_py7zr(archive_path, dest, progress_cb)


def _extract_7z_binary(archive_path: Path, dest: Path,
                       progress_cb=None, binary: str = "7z") -> bool:
    """
    Extract using the system 7z binary.
    Progress is estimated by watching the output directory grow relative to
    the archive's uncompressed size (from 7z l), since piped -bsp1 output
    is unreliable in some terminal/pipe configurations.
    """
    import threading

    if progress_cb:
        progress_cb("Extracting", 0, f"Extracting {archive_path.name}…")

    # Get uncompressed size from 7z list output for progress estimation
    uncompressed_bytes = 0
    try:
        list_result = subprocess.run(
            [binary, "l", str(archive_path)],
            capture_output=True, text=True, timeout=30
        )
        # Last summary line looks like: "   123456789   87654321  12 files"
        import re as _re
        for line in reversed(list_result.stdout.splitlines()):
            m = _re.search(r"^\s+(\d+)\s+\d+\s+\d+", line)
            if m:
                uncompressed_bytes = int(m.group(1))
                break
    except Exception:
        pass

    # Start extraction in subprocess
    proc = subprocess.Popen(
        [binary, "x", "-y", f"-o{dest}", str(archive_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Poll output directory size in a background thread for progress
    stop_polling = threading.Event()

    def poll_progress():
        last_pct = 0
        while not stop_polling.is_set():
            if uncompressed_bytes > 0 and dest.exists():
                try:
                    current = sum(
                        f.stat().st_size
                        for f in dest.rglob("*") if f.is_file()
                    )
                    pct = min(99, int(current / uncompressed_bytes * 100))
                    if pct != last_pct and progress_cb:
                        last_pct = pct
                        mb_done = current // 1024 // 1024
                        mb_total = uncompressed_bytes // 1024 // 1024
                        progress_cb("Extracting", pct,
                                    f"Extracting… {mb_done} MB / {mb_total} MB")
                except Exception:
                    pass
            stop_polling.wait(timeout=0.5)

    poll_thread = threading.Thread(target=poll_progress, daemon=True)
    if uncompressed_bytes > 0:
        poll_thread.start()

    proc.wait()
    stop_polling.set()
    if uncompressed_bytes > 0:
        poll_thread.join(timeout=2)

    if proc.returncode == 0:
        if progress_cb:
            progress_cb("Extracting", 100, "Extraction complete.")
        return True

    stderr_out = proc.stderr.read() if proc.stderr else ""
    log.error("7z exited with code %d for %s: %s",
              proc.returncode, archive_path, stderr_out[:300])
    return False


def _extract_7z_py7zr(archive_path: Path, dest: Path, progress_cb=None) -> bool:
    """Extract using py7zr. No progress — just a spinner at 0% until done."""
    try:
        import py7zr
        if progress_cb:
            progress_cb("Extracting", 0, f"Extracting {archive_path.name}…")
        with py7zr.SevenZipFile(str(archive_path), mode="r") as z:
            z.extractall(path=str(dest))
        if progress_cb:
            progress_cb("Extracting", 100, "Extraction complete.")
        return True
    except Exception as e:
        log.error("py7zr extraction failed: %s", e)
        return False


def _extract_rar(archive_path: Path, dest: Path, progress_cb=None) -> bool:
    try:
        import rarfile
        with rarfile.RarFile(str(archive_path)) as rf:
            names = rf.namelist()
            for i, name in enumerate(names):
                rf.extract(name, str(dest))
                if progress_cb:
                    progress_cb("Extracting", int((i + 1) / len(names) * 100),
                                f"Extracting {name}…")
        return True
    except Exception:
        try:
            result = subprocess.run(
                ["unrar", "x", "-y", str(archive_path), str(dest)],
                capture_output=True
            )
            return result.returncode == 0
        except FileNotFoundError:
            log.error("unrar not found. Install with: sudo pacman -S unrar")
            return False


def _extract_zip(archive_path: Path, dest: Path, progress_cb=None) -> bool:
    try:
        import zipfile
        with zipfile.ZipFile(str(archive_path)) as z:
            names = z.namelist()
            for i, name in enumerate(names):
                z.extract(name, str(dest))
                if progress_cb:
                    progress_cb("Extracting", int((i + 1) / len(names) * 100),
                                f"Extracting {name}…")
        return True
    except Exception as e:
        log.error("ZIP extraction failed: %s", e)
        return False


def extract_archive(nas_path: str, archive_name: str, file_type: str,
                    dest: Path, progress_cb=None) -> bool:
    dest.mkdir(parents=True, exist_ok=True)
    archive_path = Path(nas_path) / archive_name
    if file_type == "7zip":
        return _extract_7z(archive_path, dest, progress_cb)
    elif file_type == "rar":
        return _extract_rar(archive_path, dest, progress_cb)
    elif file_type == "zip":
        return _extract_zip(archive_path, dest, progress_cb)
    return False


# ── ISO mounting ──────────────────────────────────────────────────────────────

def _find_iso(search_root: Path) -> Optional[Path]:
    for p in search_root.rglob("*.iso"):
        return p
    return None


def _find_existing_mount(iso_path: Path) -> tuple[Optional[Path], str]:
    """
    Check if this ISO is already mounted (leftover from a previous failed attempt).
    Returns (mount_point, loop_dev) if found, (None, "") otherwise.
    Parses /proc/mounts for a loop device backed by this file.
    """
    try:
        iso_str = str(iso_path.resolve())
        # Find loop device backed by this file
        loop_dev = None
        lo_dir = Path("/sys/block")
        for entry in lo_dir.iterdir():
            if not entry.name.startswith("loop"):
                continue
            backing = entry / "loop" / "backing_file"
            try:
                if backing.read_text().strip() == iso_str:
                    loop_dev = f"/dev/{entry.name}"
                    break
            except OSError:
                continue

        if not loop_dev:
            return None, ""

        # Find where it's mounted
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == loop_dev:
                    return Path(parts[1]), loop_dev
                # Also check partition suffixes (loop0p1 etc.)
                if len(parts) >= 2 and parts[0].startswith(loop_dev):
                    return Path(parts[1]), loop_dev
    except Exception as e:
        log.debug("_find_existing_mount error: %s", e)
    return None, ""


def mount_iso_udisks(iso_path: Path) -> tuple[Optional[Path], str]:
    """Returns (mount_point, loop_dev) or (None, "")."""
    # Reuse an existing mount if the ISO is already attached
    existing_mp, existing_loop = _find_existing_mount(iso_path)
    if existing_mp and existing_mp.exists():
        log.info("ISO already mounted at %s (loop %s) — reusing",
                 existing_mp, existing_loop)
        return existing_mp, existing_loop

    try:
        result = subprocess.run(
            ["udisksctl", "loop-setup", "-f", str(iso_path), "--no-user-interaction"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            log.warning("udisksctl loop-setup failed: %s", result.stderr.strip())
            return None, ""
        # Parse loop device from output: "Mapped file … as /dev/loop0."
        m = re.search(r"(/dev/loop\d+)", result.stdout + result.stderr)
        if not m:
            return None, ""
        loop_dev = m.group(1)

        mount_result = subprocess.run(
            ["udisksctl", "mount", "-b", loop_dev, "--no-user-interaction"],
            capture_output=True, text=True, timeout=30
        )
        if mount_result.returncode != 0:
            # If already mounted, parse the existing mount point from the error
            already = re.search(r"already mounted at `([^']+)'",
                                mount_result.stderr)
            if already:
                mp = Path(already.group(1))
                log.info("Loop %s was already mounted at %s — reusing",
                         loop_dev, mp)
                return mp, loop_dev
            log.warning("udisksctl mount failed: %s", mount_result.stderr.strip())
            _unmount_loop_udisks(loop_dev)
            return None, ""

        # Parse mount point: "Mounted /dev/loop0 at /run/media/user/LABEL."
        mp = re.search(r"at\s+(\S+?)\.?\s*$", mount_result.stdout.strip())
        if mp:
            return Path(mp.group(1)), loop_dev
        return None, ""
    except FileNotFoundError:
        log.warning("udisksctl not found, trying fuseiso")
        return None, ""
    except Exception as e:
        log.error("udisksctl mount error: %s", e)
        return None, ""


def _unmount_loop_udisks(loop_dev: str):
    try:
        subprocess.run(["udisksctl", "unmount", "-b", loop_dev,
                        "--no-user-interaction"], capture_output=True, timeout=15)
        subprocess.run(["udisksctl", "loop-delete", "-b", loop_dev,
                        "--no-user-interaction"], capture_output=True, timeout=15)
    except Exception:
        pass


def mount_iso_fuse(iso_path: Path) -> Optional[Path]:
    mount_point = Path(tempfile.mkdtemp(prefix="vaultplay_iso_"))
    try:
        result = subprocess.run(
            ["fuseiso", str(iso_path), str(mount_point)],
            capture_output=True, timeout=30
        )
        if result.returncode == 0:
            return mount_point
        log.error("fuseiso failed: %s", result.stderr.decode())
        mount_point.rmdir()
        return None
    except FileNotFoundError:
        log.error("fuseiso not found. Install with: sudo pacman -S fuseiso")
        mount_point.rmdir()
        return None


def mount_iso(iso_path: Path) -> tuple[Optional[Path], str, str]:
    """
    Try udisksctl first, fall back to fuseiso.
    Returns (mount_point, method, loop_dev).
    loop_dev is only set for udisksctl mounts — needed for clean unmount.
    """
    mp, loop_dev = mount_iso_udisks(iso_path)
    if mp:
        return mp, "udisksctl", loop_dev
    mp = mount_iso_fuse(iso_path)
    if mp:
        return mp, "fuseiso", ""
    return None, "", ""


def unmount_iso(mount_point: Path, method: str, loop_dev: str = ""):
    try:
        if method == "udisksctl" and loop_dev:
            _unmount_loop_udisks(loop_dev)
        elif method == "fuseiso":
            subprocess.run(["fusermount", "-u", str(mount_point)],
                           capture_output=True, timeout=15)
            try:
                mount_point.rmdir()
            except Exception:
                pass
        else:
            # Best-effort generic unmount
            subprocess.run(["umount", str(mount_point)],
                           capture_output=True, timeout=15)
    except Exception as e:
        log.warning("Unmount error: %s", e)


def map_wine_drive(prefix_path: Path, drive_letter: str, target_path: Path):
    """
    Map a Linux path as a Wine drive letter by creating a symlink in
    the prefix's dosdevices directory.
    drive_letter: single letter, e.g. 'd'
    For Proton prefixes, pass the actual Wine prefix (pfx/ subdirectory),
    not the compat data directory.
    """
    try:
        dosdevices = prefix_path / "dosdevices"
        dosdevices.mkdir(parents=True, exist_ok=True)
        link = dosdevices / f"{drive_letter.lower()}:"
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(target_path)
        log.info("Mapped Wine drive %s: → %s", drive_letter.upper(), target_path)
    except Exception as e:
        log.warning("map_wine_drive failed: %s", e)


def _resolve_actual_prefix(prefix_path: Path, wine_bin: str) -> Path:
    """
    Return the actual Wine prefix directory for dosdevices mapping etc.
    For Proton, the real prefix is at <prefix_path>/pfx/ after initialization.
    For plain Wine, it's prefix_path itself.
    """
    if Path(wine_bin).name == "proton":
        pfx = prefix_path / "pfx"
        if not pfx.exists():
            pfx.mkdir(parents=True, exist_ok=True)
            log.info("_resolve_actual_prefix: created pfx/ at %s", pfx)
        return pfx
    return prefix_path


def _discover_mount_points() -> list[Path]:
    """
    Discover user-accessible mounted drives/partitions.
    Scans locations based on settings:
      scan_run_media: /run/media/<user>/  (systemd/udisks automount — Arch default)
      scan_mnt:       /mnt/               (manual/NAS mounts)
    Also scans /media/<user>/ and /media/ unconditionally as legacy fallbacks.
    Skips virtual/system/optical filesystems and the configured NAS path.
    """
    import getpass
    username = getpass.getuser()

    scan_run_media = db.get_setting("wine_scan_run_media", "true") == "true"
    scan_mnt       = db.get_setting("wine_scan_mnt",       "false") == "true"

    scan_roots = []
    if scan_run_media:
        scan_roots.append(Path("/run/media") / username)
    if scan_mnt:
        scan_roots.append(Path("/mnt"))
    # Legacy automount locations — always included if they exist
    scan_roots += [
        Path("/media") / username,
        Path("/media"),
        Path("/run/mount"),
    ]

    SKIP_FS = {
        "tmpfs", "devtmpfs", "sysfs", "proc", "cgroup", "cgroup2",
        "pstore", "efivarfs", "bpf", "tracefs", "debugfs", "securityfs",
        "fusectl", "hugetlbfs", "mqueue", "configfs", "ramfs",
        "udf", "iso9660",
    }

    fs_by_path: dict[str, str] = {}
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3:
                    fs_by_path[parts[1]] = parts[2]
    except Exception:
        pass

    # Build set of NAS roots to exclude
    nas_roots: set[str] = set()
    nas_path = db.get_setting("nas_path", "")
    if nas_path:
        try:
            p = Path(nas_path).resolve()
            for parent in [p] + list(p.parents):
                if parent.parent in [
                    Path("/mnt"),
                    Path("/run/media") / username,
                    Path("/media") / username,
                    Path("/media"),
                ]:
                    nas_roots.add(str(parent))
                    break
        except Exception:
            pass

    seen: set[str] = set()
    result: list[Path] = []

    for root in scan_roots:
        if not root.exists():
            continue
        try:
            entries = sorted(root.iterdir())
        except PermissionError:
            continue
        for entry in entries:
            if not entry.is_dir():
                continue
            try:
                real = str(entry.resolve())
            except OSError:
                continue
            if real in seen:
                continue
            if any(real == nr or real.startswith(nr + "/") for nr in nas_roots):
                log.debug("Skipping NAS mount %s", entry)
                continue
            fstype = fs_by_path.get(real) or fs_by_path.get(str(entry)) or ""
            if fstype in SKIP_FS:
                log.debug("Skipping mount %s (fstype=%s)", entry, fstype)
                continue
            is_mount = (real in fs_by_path or str(entry) in fs_by_path)
            if not is_mount:
                try:
                    is_mount = entry.stat().st_dev != entry.parent.stat().st_dev
                except OSError:
                    pass
            if not is_mount:
                continue
            seen.add(real)
            result.append(entry)
            log.info("Discovered mount point: %s (fstype=%s)", entry, fstype)

    return result


def map_all_drives(prefix_path: Path) -> dict[str, Path]:
    """
    Map all discovered mount points as Wine drive letters (D: onward).
    C: and Z: are reserved by Wine (C: = drive_c, Z: = filesystem root).
    Returns dict of {letter: path} for all drives that were mapped.

    Drive letter assignment is stable across calls for the same set of mounts
    because we sort mount points before assigning letters.
    """
    mounts = _discover_mount_points()
    if not mounts:
        log.info("map_all_drives: no mount points discovered")
        return {}

    # Letters available for mapping (skip A/B floppy, C drive_c, Z filesystem root)
    available = [chr(c) for c in range(ord('d'), ord('z')) if chr(c) not in ('z',)]
    mapped: dict[str, Path] = {}

    for letter, mount in zip(available, mounts):
        map_wine_drive(prefix_path, letter, mount)
        mapped[letter] = mount

    log.info("map_all_drives: mapped %d drives: %s",
             len(mapped),
             {k.upper() + ":": str(v) for k, v in mapped.items()})
    return mapped


# ── Wine prefix ───────────────────────────────────────────────────────────────

def create_wine_prefix(prefix_path: Path, progress_cb=None,
                       wine_bin: str = "wine") -> bool:
    """
    Create a Wine/Proton prefix at prefix_path if it doesn't already exist.
    For Proton, the actual prefix lands at prefix_path/pfx/ — we detect this
    by checking for prefix_path/pfx/system.reg.
    If the prefix already exists, skips creation entirely.
    """
    is_proton = Path(wine_bin).name == "proton"
    system_reg = (prefix_path / "pfx" / "system.reg") if is_proton \
                 else (prefix_path / "system.reg")

    if system_reg.exists():
        log.info("Wine prefix already exists at %s — skipping creation", prefix_path)
        if progress_cb:
            progress_cb("Wine Prefix", 100,
                        f"Prefix {prefix_path.name} already exists — skipping.")
        return True

    prefix_path.mkdir(parents=True, exist_ok=True)
    if progress_cb:
        progress_cb("Creating Wine prefix", 0, f"Initialising {prefix_path.name}…")
    try:
        if is_proton:
            # Use Proton to initialize so pfx/ is created with the correct
            # Proton runtime libraries. 'wineboot' is the verb that
            # initializes the prefix without running any exe.
            env = _build_wine_env(wine_bin, prefix_path)
            subprocess.run([wine_bin, "wineboot"], env=env,
                           capture_output=True, timeout=120)
        else:
            env = {**os.environ, "WINEPREFIX": str(prefix_path)}
            subprocess.run(["wineboot", "--init"], env=env,
                           capture_output=True, timeout=120)
        if progress_cb:
            progress_cb("Creating Wine prefix", 100, "Prefix ready.")
        return True
    except Exception as e:
        log.error("wineboot failed: %s", e)
        return False


def get_installed_redists(prefix_path: Path) -> set:
    """
    Return the set of winetricks verbs already installed in this prefix,
    by reading the winetricks.log file it maintains automatically.
    Returns an empty set if the log doesn't exist or can't be read.
    """
    log_path = prefix_path / "winetricks.log"
    try:
        if log_path.exists():
            return {line.strip() for line in log_path.read_text().splitlines()
                    if line.strip()}
    except Exception:
        pass
    return set()


def install_redists(prefix_path: Path, redists: list, progress_cb=None,
                    force: bool = False) -> list:
    """
    Install winetricks verbs into the prefix.
    Skips verbs already recorded in winetricks.log unless force=True.
    Returns list of verbs that failed.
    """
    already_installed = get_installed_redists(prefix_path)
    env    = {**os.environ, "WINEPREFIX": str(prefix_path)}
    failed = []

    to_install = redists if force else [
        v for v in redists if v not in already_installed
    ]
    skipped = [v for v in redists if v in already_installed] if not force else []

    if skipped:
        log.info("Skipping already-installed redists: %s", skipped)

    if not to_install:
        log.info("All redists already installed — skipping.")
        if progress_cb:
            progress_cb("Redistributables", 100,
                        "All redistributables already installed.")
        return []

    for i, verb in enumerate(to_install):
        if progress_cb:
            progress_cb("Redistributables",
                        int(i / len(to_install) * 100),
                        f"Installing {verb}…")
        try:
            r = subprocess.run(["winetricks", "-q", verb], env=env,
                               capture_output=True, timeout=300)
            if r.returncode != 0:
                failed.append(verb)
        except subprocess.TimeoutExpired:
            failed.append(verb)
        except FileNotFoundError:
            log.error("winetricks not found")
            failed.append(verb)
            break

    if progress_cb and to_install:
        progress_cb("Redistributables", 100, "Redistributables complete.")

    return failed


# ── Executable finders ────────────────────────────────────────────────────────

INSTALLER_RE = re.compile(
    r"(^|[\\/])(setup|install|installer|autorun|autoplay)[^\\/]*\.exe$",
    re.IGNORECASE
)
SKIP_EXE_RE = re.compile(
    r"(setup|install|unins|uninstall|crash|report|redist|vcredist"
    r"|directx|dxsetup|ue4prereq|dotnet|oalinst|physx|browser|helper"
    r"|update|patch)",
    re.IGNORECASE
)


def find_installer_exe(search_root: Path) -> Optional[Path]:
    for root, dirs, files in os.walk(search_root):
        for f in files:
            if INSTALLER_RE.search(f):
                return Path(root) / f
    return None


def find_game_exe(search_root: Path) -> Optional[Path]:
    candidates = []
    for root, dirs, files in os.walk(search_root):
        dirs[:] = [d for d in dirs if not SKIP_EXE_RE.search(d)]
        for f in files:
            if f.lower().endswith(".exe") and not SKIP_EXE_RE.search(f):
                candidates.append(Path(root) / f)
    if not candidates:
        return None
    candidates.sort(key=lambda p: (len(p.parts), p.name.lower()))
    return candidates[0]


def extract_exe_icon(exe_path: Path, output_dir: Path) -> Optional[str]:
    """
    Extract the largest icon from a Windows .exe using icoutils (wrestool + icotool).
    Returns the path to the extracted PNG, or None if extraction fails.
    icoutils: sudo pacman -S icoutils
    """
    import shutil as _shutil
    if not _shutil.which("wrestool") or not _shutil.which("icotool"):
        log.debug("icoutils not installed — skipping icon extraction")
        return None
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        # Extract the icon group resource from the exe into a .ico file
        ico_path = output_dir / f"{exe_path.stem}.ico"
        result = subprocess.run(
            ["wrestool", "-x", "--output", str(ico_path), "-t", "14", str(exe_path)],
            capture_output=True, timeout=15
        )
        if result.returncode != 0 or not ico_path.exists():
            log.debug("wrestool failed for %s: %s", exe_path, result.stderr)
            return None
        # Convert .ico to PNG(s) — icotool writes files like name_NxN.png
        result2 = subprocess.run(
            ["icotool", "-x", "--output", str(output_dir), str(ico_path)],
            capture_output=True, timeout=15
        )
        if result2.returncode != 0:
            log.debug("icotool failed: %s", result2.stderr)
            return None
        # Find the largest PNG extracted
        pngs = sorted(output_dir.glob(f"{exe_path.stem}*.png"),
                      key=lambda p: p.stat().st_size, reverse=True)
        if pngs:
            log.info("Extracted icon from %s → %s", exe_path.name, pngs[0].name)
            return str(pngs[0])
    except Exception as e:
        log.debug("extract_exe_icon failed: %s", e)
    return None


# ── Desktop / launcher generation ────────────────────────────────────────────

APPS_DIR = Path.home() / ".local" / "share" / "applications"


def _safe_name(title: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", title).strip("_").lower()


def create_direct_desktop(title: str, launch_cmd: str,
                           launch_cwd: str = "", icon_path: str = "") -> str:
    """
    Write a .desktop file from pre-computed launch values stored in the DB.
    This is a pure file-writing utility — it does not figure out what to launch.
    _extract_launch_info() is responsible for determining the correct values.
    """
    APPS_DIR.mkdir(parents=True, exist_ok=True)
    safe_title = re.sub(r"[/\x00]", "_", title).strip()
    desktop = APPS_DIR / f"{safe_title}.desktop"

    content = f"""[Desktop Entry]
Name={title}
Exec={launch_cmd}
Type=Application
Categories=Game;
StartupNotify=true
"""
    if launch_cwd and Path(launch_cwd).exists():
        content += f"Path={launch_cwd}\n"
    if icon_path and Path(icon_path).exists():
        content += f"Icon={icon_path}\n"

    desktop.write_text(content)
    log.info("Wrote .desktop: %s", desktop)
    return str(desktop)


def create_script_launcher(title: str, exe_path: str,
                            prefix_path: Path, icon_path: str = "") -> tuple[str, str]:
    """
    Create a shell script wrapper AND a .desktop that points to it.
    Returns (script_path, desktop_path).
    """
    scripts_dir = Path.home() / ".local" / "share" / "vaultplay" / "launchers"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    APPS_DIR.mkdir(parents=True, exist_ok=True)

    slug   = _safe_name(title)
    script = scripts_dir / f"{slug}.sh"
    desktop = APPS_DIR / f"vaultplay_{slug}.desktop"

    script_content = f"""#!/bin/bash
# VaultPlay launcher for {title}
export WINEPREFIX="{prefix_path}"
# Add any additional environment variables below:
# export WINEDLLOVERRIDES="..."

exec wine "{exe_path}" "$@"
"""
    script.write_text(script_content)
    script.chmod(0o755)

    desktop_content = f"""[Desktop Entry]
Name={title}
Exec="{script}"
Type=Application
Categories=Game;
"""
    if icon_path and Path(icon_path).exists():
        desktop_content += f"Icon={icon_path}\n"
    desktop.write_text(desktop_content)

    log.info("Created script launcher: %s + %s", script, desktop)
    return str(script), str(desktop)


def _extract_launch_info(title: str, prefix_path: Path, exe_path: str,
                          wine_bin: str, game_dir: str) -> tuple:
    """
    Determine the best launch command, working directory, and icon for a game.

    Priority:
      1. If winemenubuilder created a .desktop for this game (after the Windows
         installer ran), parse its Exec= and Path= lines — these represent what
         the game's own installer decided was the correct launch target, which
         may be a .lnk shortcut rather than a raw .exe.
      2. Otherwise, construct the Exec= line from exe_path and wine_bin directly.

    Returns (launch_cmd, launch_cwd, icon_path) — all strings, any may be "".
    """
    # Try to find a winemenubuilder .desktop first
    for f in APPS_DIR.glob("*.desktop"):
        try:
            text  = f.read_text(errors="replace")
            lines = {}
            for l in text.splitlines():
                if "=" in l:
                    k, _, v = l.partition("=")
                    lines[k.strip()] = v.strip()
            name  = lines.get("Name", "")
            exec_ = lines.get("Exec", "")
            path_ = lines.get("Path", "")
            # Must reference our prefix path
            if str(prefix_path) not in exec_:
                continue
            # Title must loosely match
            if (title.lower() not in name.lower() and
                    name.lower() not in title.lower()):
                continue
            # Found a match — use its Exec= and Path=.
            # Do NOT use its Icon= value — winemenubuilder Icon= fields are
            # bare icon names (e.g. "2035_DELTARUN-2.0"), not file paths.
            # Icon resolution is handled by the caller using SteamGridDB art.
            cwd = path_ if (path_ and Path(path_).exists()) else game_dir
            log.info("Using winemenubuilder Exec= for '%s': %s", title, exec_)
            return exec_, cwd, ""   # icon always "" — caller fills it in
        except Exception:
            continue

    # No winemenubuilder match — construct from raw values
    if Path(wine_bin).name == "proton":
        steam_root  = _find_steam_root()
        wineprefix  = str(prefix_path / "pfx")
        compat_data = str(prefix_path)
        launch_cmd = (
            f'env WINEPREFIX="{wineprefix}"'
            f' STEAM_COMPAT_DATA_PATH="{compat_data}"'
            f' STEAM_COMPAT_CLIENT_INSTALL_PATH="{steam_root}"'
            f' PROTON_LOG="0"'
            f' "{wine_bin}" run "{exe_path}"'
        )
    else:
        launch_cmd = f'env WINEPREFIX="{prefix_path}" "{wine_bin}" "{exe_path}"'

    return launch_cmd, game_dir, ""


# ── Pre-flight helpers ────────────────────────────────────────────────────────

def force_unmount_iso(iso_path: Path):
    """
    Forcibly detach any loop device backed by this ISO file.
    Called at the start of each install attempt so stale mounts from previous
    failed attempts don't block re-tries.
    """
    try:
        iso_str = str(iso_path.resolve())
        lo_dir = Path("/sys/block")
        for entry in lo_dir.iterdir():
            if not entry.name.startswith("loop"):
                continue
            backing = entry / "loop" / "backing_file"
            try:
                if backing.read_text().strip() != iso_str:
                    continue
            except OSError:
                continue
            loop_dev = f"/dev/{entry.name}"
            log.info("force_unmount_iso: detaching stale %s for %s",
                     loop_dev, iso_path.name)
            # Try udisks first, then fall back to losetup
            r = subprocess.run(
                ["udisksctl", "unmount", "-b", loop_dev, "--no-user-interaction"],
                capture_output=True, timeout=10
            )
            subprocess.run(
                ["udisksctl", "loop-delete", "-b", loop_dev, "--no-user-interaction"],
                capture_output=True, timeout=10
            )
            if r.returncode != 0:
                subprocess.run(
                    ["sudo", "losetup", "-d", loop_dev],
                    capture_output=True, timeout=10
                )
    except Exception as e:
        log.debug("force_unmount_iso error: %s", e)


def tmp_has_contents(tmp_path: Path, archive_name: str) -> bool:
    """
    Return True if tmp_path already contains extracted files from a previous
    attempt, so extraction can be skipped.
    We consider it populated if it exists, is a directory, and contains at
    least one file other than the archive itself.
    """
    if not tmp_path.exists() or not tmp_path.is_dir():
        return False
    for entry in tmp_path.rglob("*"):
        if entry.is_file() and entry.name.lower() != archive_name.lower():
            return True
    return False


def _snapshot_dir(path: Path) -> set:
    """
    Return a set of (relative_path_str, size) tuples for all files
    currently in path. Used to detect what's new after an install.
    """
    result = set()
    if not path.exists():
        return result
    try:
        for f in path.rglob("*"):
            if f.is_file():
                try:
                    result.add((str(f.relative_to(path)), f.stat().st_size))
                except (OSError, ValueError):
                    pass
    except Exception:
        pass
    return result


def _find_new_exe(install_path: Path, before: set) -> Optional[Path]:
    """
    Find a game exe that appeared in install_path after the install ran,
    by comparing against the before snapshot.
    Returns the most likely game exe (shallowest, non-skip).
    """
    candidates = []
    try:
        for f in install_path.rglob("*.exe"):
            if SKIP_EXE_RE.search(f.name):
                continue
            key = (str(f.relative_to(install_path)), f.stat().st_size)
            if key not in before:
                candidates.append(f)
    except Exception:
        pass
    if not candidates:
        return None
    candidates.sort(key=lambda p: (len(p.parts), p.name.lower()))
    return candidates[0]


# ── Main install pipeline ─────────────────────────────────────────────────────

# ── Update & DLC Install Support — addon application ────────────────────────

def _addon_sort_key(addon_row) -> tuple:
    """
    Comparable sort key for ordering addon rows in ascending version order
    (used to apply updates in the correct sequence). Handles whichever of
    dotted/plain/date is populated on the row — per detect_version()'s
    contract, exactly one is. Rows with no detected version at all sort
    first (lowest), so an unversioned update still gets applied, just
    without a defined position relative to versioned siblings.
    """
    import version_check as vc
    try:
        if addon_row["detected_version_date"]:
            return (2, vc.date_sort_key(addon_row["detected_version_date"]))
    except (IndexError, KeyError):
        pass
    try:
        if addon_row["detected_version_dotted"]:
            return (1, vc.sort_key(addon_row["detected_version_dotted"]))
    except (IndexError, KeyError):
        pass
    try:
        if addon_row["detected_version_plain"]:
            return (1, vc.sort_key(addon_row["detected_version_plain"]))
    except (IndexError, KeyError):
        pass
    return (0, ())


def _addon_containing_dir(addon_row) -> Path:
    """
    Return the directory that actually contains addon_row's archive.
    nas_path means different things depending on how the addon was found —
    for a subfolder-style addon it's already a directory; for a loose
    sibling archive it's the archive file itself (see scanner.py's
    _scan_one_addon_item) — this normalizes both to "the directory to hand
    to extract_archive()".
    """
    p = Path(addon_row["nas_path"])
    return p if p.is_dir() else p.parent


def _run_addon_installer(addon_row, prefix_path: Path, wine_bin: str,
                         tmp_base: Path, progress_cb: Callable = None) -> bool:
    """
    Extract (if archived) one addon and run its installer/exe via Wine in
    the given, already-created prefix — the SAME prefix the base game was
    just installed into, never a new one. Returns True if the installer
    process completed without a timeout/missing-binary error.

    Like the base install flows, this can't reliably distinguish "the
    installer ran and genuinely succeeded" from "it ran and silently did
    nothing" for arbitrary third-party installers — this mirrors the same
    best-effort completion check the base flows already use.
    """
    archive_name = addon_row["archive_name"]
    file_type    = addon_row["file_type"]

    if archive_name and file_type in ("rar", "7zip", "zip"):
        containing_dir = _addon_containing_dir(addon_row)
        tmp_path = tmp_base / f"addon_{addon_row['id']}"
        ok = extract_archive(str(containing_dir), archive_name, file_type,
                             tmp_path, progress_cb)
        if not ok:
            log.error("Addon extraction failed: %s", archive_name)
            return False
        search_root = tmp_path
    else:
        # Loose exe(s) directly at nas_path, nothing to extract
        search_root = Path(addon_row["nas_path"])
        if search_root.is_file():
            search_root = search_root.parent

    installer_exe = find_installer_exe(search_root) or find_game_exe(search_root)
    if not installer_exe:
        log.warning("No installer/exe found for addon at %s", search_root)
        return False

    env = _build_wine_env(wine_bin, prefix_path)
    try:
        cmd = ([wine_bin, "run", str(installer_exe)]
               if Path(wine_bin).name == "proton"
               else [wine_bin, str(installer_exe)])
        subprocess.run(cmd, env=env, timeout=1800)
        return True
    except subprocess.TimeoutExpired:
        log.error("Addon installer timed out: %s", installer_exe)
        return False
    except FileNotFoundError:
        log.error("wine not found while installing addon: %s", installer_exe)
        return False


def _install_pending_addons(game_id: int, prefix_path: Path, wine_bin: str,
                            selected_addon_ids, tmp_base: Path,
                            progress_cb: Callable = None) -> dict:
    """
    Apply selected update/installable-DLC/crackfix addons, in the confirmed
    order: updates first (ascending version, respecting update_patch_mode),
    then installable DLC, then crackfix — all run via Wine in the SAME
    prefix the base game was just installed into.

    selected_addon_ids: set/list of game_addons.id the user left checked in
    the Install dialog's Updates & DLC section. Addons not in this set are
    skipped entirely — this is how unchecking a box in the UI takes effect.
    Pass None to apply every pending addon found (reserved for a future
    automatic "install all pending updates" action — not currently used by
    the Install dialog).

    Already-installed addons are always skipped regardless of selection, as
    a safety net against ever reapplying — matches the decision that DLC
    only installs if not already installed, extended defensively to
    updates/crackfix too.

    Bonus content is NEVER applied here regardless of selection — it has no
    install pipeline at all, only the separate manual extract action.

    Returns:
        {
            "failed": list[str],            -- addon display names that failed
            "installed_version": {           -- from the highest UPDATE applied,
                "dotted": str|None,           -- for seeding installs.installed_version_*
                "plain":  str|None,
                "date":   str|None,
            },
        }
    """
    def _selected(addon_row) -> bool:
        return selected_addon_ids is None or addon_row["id"] in selected_addon_ids

    failed: list = []
    installed_version = {"dotted": None, "plain": None, "date": None}

    # ── Updates — ascending version order ──────────────────────────────────
    updates = [a for a in db.get_addons_for_game(game_id, "update")
              if _selected(a) and not a["installed"]]
    updates.sort(key=_addon_sort_key)

    game = db.get_game(game_id)
    patch_mode = "incremental"
    if game and "update_patch_mode" in game.keys() and game["update_patch_mode"]:
        patch_mode = game["update_patch_mode"]
    if patch_mode == "cumulative" and updates:
        # Only the single highest pending update needs applying — it's a
        # full replacement patch, not a delta requiring the ones before it.
        updates = [updates[-1]]

    for addon in updates:
        label = addon["archive_name"] or Path(addon["nas_path"]).name
        if progress_cb:
            progress_cb("Installing updates", 0, f"Applying update: {label}…")
        ok = _run_addon_installer(addon, prefix_path, wine_bin, tmp_base, progress_cb)
        if ok:
            db.mark_addon_installed(addon["id"])
            if addon["detected_version_date"]:
                installed_version["date"] = addon["detected_version_date"]
            elif addon["detected_version_dotted"]:
                installed_version["dotted"] = addon["detected_version_dotted"]
            elif addon["detected_version_plain"]:
                installed_version["plain"] = addon["detected_version_plain"]
        else:
            failed.append(label)
            log.warning("Update failed to apply, continuing with remaining addons: %s", label)

    # ── Installable DLC — order doesn't matter between DLC items ──────────
    dlc_items = [a for a in db.get_addons_for_game(game_id, "installable_dlc")
                if _selected(a) and not a["installed"]]
    for addon in dlc_items:
        label = addon["archive_name"] or Path(addon["nas_path"]).name
        if progress_cb:
            progress_cb("Installing DLC", 0, f"Installing DLC: {label}…")
        ok = _run_addon_installer(addon, prefix_path, wine_bin, tmp_base, progress_cb)
        if ok:
            db.mark_addon_installed(addon["id"])
        else:
            failed.append(label)
            log.warning("DLC failed to install, continuing with remaining addons: %s", label)

    # ── Crackfix — always applies if selected, no version gating ──────────
    crackfixes = [a for a in db.get_addons_for_game(game_id, "crackfix")
                 if _selected(a) and not a["installed"]]
    for addon in crackfixes:
        label = addon["archive_name"] or Path(addon["nas_path"]).name
        if progress_cb:
            progress_cb("Applying crackfix", 0, f"Applying: {label}…")
        ok = _run_addon_installer(addon, prefix_path, wine_bin, tmp_base, progress_cb)
        if ok:
            db.mark_addon_installed(addon["id"])
        else:
            failed.append(label)
            log.warning("Crackfix failed to apply, continuing: %s", label)

    return {"failed": failed, "installed_version": installed_version}


def run_install(game_id: int, options: dict, progress_cb: Callable = None) -> dict:
    """
    Full install pipeline.

    options = {
        'wine_prefix_name': str,
        'game_path':        str,
        'launcher_type':    str,
        'redists':          list[str],
        'cleanup_tmp':      bool,
        'proton_version':   str,
        'install_tag':      str,
    }

    All result dicts include 'tmp_path' so the caller can offer cleanup on
    failure, and 'cancelled' flag when Wine exited without installing anything.
    """

    def prog(stage, pct, msg):
        if progress_cb:
            progress_cb(stage, pct, msg)
        log.info("[%s] %d%% — %s", stage, pct, msg)

    def fail(error, cancelled=False):
        return {
            "success":   False,
            "error":     error,
            "tmp_path":  str(tmp_path) if 'tmp_path' in dir() else "",
            "cancelled": cancelled,
        }

    game = db.get_game(game_id)
    if not game:
        return {"success": False, "error": "Game not found",
                "tmp_path": "", "cancelled": False}

    title        = game["title"] or game["display_name"] or game["folder_name"]
    nas_path     = game["nas_path"]
    file_type    = game["file_type"]
    archive_name = game["archive_name"] or ""
    install_tag  = options.get("install_tag") or game["install_tag"]

    # ── Paths ─────────────────────────────────────────────────────────────────
    prefix_name = options.get("wine_prefix_name") or make_prefix_name(game["folder_name"])
    prefix_path = _wine_prefix_root() / prefix_name
    tmp_base    = Path(db.get_setting("tmp_path",
                                      str(Path.home() / "Games" / ".tmp")))
    tmp_path    = tmp_base / game["folder_name"]
    game_path   = Path(options.get("game_path") or
                       (db.get_install_paths()[0] if db.get_install_paths() else
                        str(Path.home() / "Games")))
    game_dest   = game_path / game["folder_name"]

    # ── Resolve Wine/Proton binary ────────────────────────────────────────────
    import protondb as protondb_mod
    proton_value = options.get("proton_version", "")
    _version_path = protondb_mod.get_version_path(proton_value)
    wine_bin = _resolve_wine_bin(_version_path) if _version_path else "wine"
    log.info("Using Wine/Proton binary: %s (version key: %s)", wine_bin, proton_value)

    # ── Extract (skip if tmp already populated from a previous attempt) ───────
    source_path = None
    if file_type in ("rar", "7zip", "zip"):
        if tmp_has_contents(tmp_path, archive_name):
            prog("Extracting", 100,
                 f"Using existing extracted files in {tmp_path.name} — skipping extraction.")
            log.info("Skipping extraction — tmp already populated: %s", tmp_path)
        else:
            prog("Extracting", 0, f"Extracting {archive_name}…")
            ok = extract_archive(nas_path, archive_name, file_type, tmp_path, prog)
            if not ok:
                return fail(f"Extraction failed for {archive_name}")
        source_path = tmp_path
    else:
        source_path = Path(nas_path)

    # ── ISO flow ──────────────────────────────────────────────────────────────
    if install_tag == "iso":
        prog("Finding ISO", 10, "Locating .iso file…")
        iso_file = _find_iso(source_path)
        if not iso_file:
            return fail("No .iso file found after extraction")

        # Forcibly detach any stale loop device from a previous failed attempt
        force_unmount_iso(iso_file)

        prog("Mounting ISO", 20, f"Mounting {iso_file.name}…")
        mount_point, mount_method, loop_dev = mount_iso(iso_file)
        if not mount_point:
            return fail("Could not mount ISO. Make sure udisksctl is available.")

        prog("Finding setup", 30, "Looking for installer in ISO…")
        installer_exe = find_installer_exe(mount_point)
        if not installer_exe:
            unmount_iso(mount_point, mount_method, loop_dev)
            return fail("No installer .exe found in ISO")

        prog("Creating Wine prefix", 40, f"Prefix: {prefix_name}")
        create_wine_prefix(prefix_path, prog, wine_bin=wine_bin)

        # Map drives based on user preference
        if db.get_setting("wine_drive_mapping", "auto") == "auto":
            actual_prefix = _resolve_actual_prefix(prefix_path, wine_bin)
            mapped = map_all_drives(actual_prefix)
            if mapped:
                letters = ", ".join(
                    f"{l.upper()}: → {p.name}" for l, p in mapped.items()
                )
                prog("Mapping drives", 42, f"Mapped: {letters}")
            else:
                log.info("No external mount points found to map")
        # else: winetricks_default — leave dosdevices as Wine created it

        failed_redists = []
        if options.get("redists"):
            failed_redists = install_redists(prefix_path, options["redists"], prog,
                            force=options.get("force_redists", False))

        prog("Installing", 60, f"Running {installer_exe.name} via Wine…")
        env = _build_wine_env(wine_bin, prefix_path)

        # Snapshot ALL configured install paths before Wine runs
        all_install_paths = [Path(p) for p in db.get_install_paths()]
        before_snapshots = {p: _snapshot_dir(p) for p in all_install_paths}
        log.info("Pre-install snapshot: watching %d install paths", len(all_install_paths))

        try:
            cmd = ([wine_bin, "run", str(installer_exe)]
                   if Path(wine_bin).name == "proton"
                   else [wine_bin, str(installer_exe)])
            subprocess.run(cmd, env=env, timeout=1800)
        except subprocess.TimeoutExpired:
            unmount_iso(mount_point, mount_method, loop_dev)
            return fail("Installer timed out")
        except FileNotFoundError:
            unmount_iso(mount_point, mount_method, loop_dev)
            return fail("wine not found")

        prog("Unmounting ISO", 90, "Cleaning up ISO mount…")
        unmount_iso(mount_point, mount_method, loop_dev)

        # Find new exe — check snapshot paths first, then fall back to
        # searching the Wine prefix (drive_c) and mapped drive locations
        exe_path = None
        actual_install_path = None
        for watch_path, before in before_snapshots.items():
            found = _find_new_exe(watch_path, before)
            if found:
                exe_path = found
                actual_install_path = str(found.parent)
                log.info("Install detected in %s → %s", watch_path, found)
                break

        if not exe_path:
            log.info("Snapshot found nothing — searching Wine prefix for game exe…")
            actual_prefix = _resolve_actual_prefix(prefix_path, wine_bin)
            exe_path = (
                find_game_exe(actual_prefix / "drive_c" / "Program Files") or
                find_game_exe(actual_prefix / "drive_c" / "Program Files (x86)") or
                find_game_exe(actual_prefix / "drive_c")
            )
            # Also search any mapped drive locations
            if not exe_path:
                try:
                    mounts = _discover_mount_points()
                    for _, mount_path in mounts.items():
                        found = find_game_exe(mount_path)
                        if found:
                            exe_path = found
                            log.info("Found exe on mapped drive: %s", found)
                            break
                except Exception as e:
                    log.debug("Drive search failed: %s", e)
            if exe_path:
                actual_install_path = str(exe_path.parent)
                log.info("Found game exe via prefix search: %s", exe_path)
            else:
                log.info("Could not auto-detect game exe — recording install without exe_path")

        # Re-fetch game row here — metadata may have arrived since install started
        fresh_game = db.get_game(game_id) or game
        icon_path  = ""
        try:
            # Icon priority: exe extraction first (correct aspect ratio + authentic),
            # fall back to SGDB cover art if extraction fails or exe not found yet
            if exe_path and Path(str(exe_path)).exists():
                icon_cache = Path.home() / ".config" / "vaultplay" / "icons"
                safe = re.sub(r"[/\x00]", "_", title).strip()
                extracted = extract_exe_icon(Path(str(exe_path)), icon_cache / safe)
                if extracted:
                    icon_path = extracted
            if not icon_path:
                icon_url = fresh_game["cover_url"] or ""
                if icon_url:
                    cached = db.get_cached_art(icon_url)
                    if cached:
                        icon_path = cached
        except Exception:
            pass

        launch_cmd = launch_cwd = launch_icon = ""
        if exe_path:
            game_dir   = str(actual_install_path) if actual_install_path else ""
            launch_cmd, launch_cwd, _ = _extract_launch_info(
                title, prefix_path, str(exe_path), wine_bin, game_dir)
            launch_icon = icon_path   # use our resolved icon, not _extract_launch_info's

        desktop_path = create_direct_desktop(title, launch_cmd, launch_cwd, launch_icon)

        if options.get("cleanup_tmp", True):
            _cleanup(tmp_path, True, prog)

        db.record_install(
            game_id=game_id,
            install_path=actual_install_path or str(prefix_path),
            wine_prefix=str(prefix_path),
            exe_path=str(exe_path) if exe_path else "",
            desktop_path=desktop_path,
            launch_cmd=launch_cmd,
            launch_cwd=launch_cwd,
            launch_icon=launch_icon,
        )

        # ── Update & DLC Install Support — apply pending updates/DLC/crackfix ──
        addon_result = _install_pending_addons(
            game_id, prefix_path, wine_bin,
            options.get("selected_addon_ids"), tmp_base, prog)
        final_version = addon_result["installed_version"]
        if not (final_version["dotted"] or final_version["plain"] or final_version["date"]):
            # No update applied — seed installed_version from the base
            # archive's own detected version instead, if it has one.
            final_version = scanner.detect_addon_version(
                archive_name, [exe_path.name] if exe_path else [], game["folder_name"])
        if final_version["dotted"] or final_version["plain"] or final_version["date"]:
            db.set_installed_version(game_id, **final_version)

        prog("Done", 100, f"'{title}' installed!")
        return {"success": True, "error": None,
                "exe_path": str(exe_path) if exe_path else "",
                "desktop_path": desktop_path,
                "tmp_path": str(tmp_path),
                "cancelled": False,
                "failed_redists": failed_redists,
                "failed_addons": addon_result["failed"]}

    # ── Installer flow ────────────────────────────────────────────────────────
    elif install_tag == "installer":
        prog("Finding installer", 10, "Locating setup .exe…")
        installer_exe = find_installer_exe(source_path)
        if not installer_exe:
            return fail("No installer .exe found")

        prog("Creating Wine prefix", 20, f"Prefix: {prefix_name}")
        create_wine_prefix(prefix_path, prog, wine_bin=wine_bin)

        if db.get_setting("wine_drive_mapping", "auto") == "auto":
            map_all_drives(_resolve_actual_prefix(prefix_path, wine_bin))

        failed_redists = []
        if options.get("redists"):
            failed_redists = install_redists(prefix_path, options["redists"], prog, force=options.get("force_redists", False))

        prog("Installing", 50, f"Running {installer_exe.name} via Wine…")
        env = _build_wine_env(wine_bin, prefix_path)
        all_install_paths = [Path(p) for p in db.get_install_paths()]
        before_snapshots = {p: _snapshot_dir(p) for p in all_install_paths}
        log.info("Pre-install snapshot: watching %d install paths", len(all_install_paths))
        try:
            cmd = ([wine_bin, "run", str(installer_exe)]
                   if Path(wine_bin).name == "proton"
                   else [wine_bin, str(installer_exe)])
            subprocess.run(cmd, env=env, timeout=1800)
        except subprocess.TimeoutExpired:
            return fail("Installer timed out")
        except FileNotFoundError:
            return fail("wine not found")

        exe_path = None
        actual_install_path = None
        for watch_path, before in before_snapshots.items():
            found = _find_new_exe(watch_path, before)
            if found:
                exe_path = found
                actual_install_path = str(found.parent)
                log.info("Install detected in %s → %s", watch_path, found)
                break

        if not exe_path:
            log.info("Snapshot found nothing — searching Wine prefix for game exe…")
            actual_prefix = _resolve_actual_prefix(prefix_path, wine_bin)
            exe_path = (
                find_game_exe(actual_prefix / "drive_c" / "Program Files") or
                find_game_exe(actual_prefix / "drive_c" / "Program Files (x86)") or
                find_game_exe(actual_prefix / "drive_c")
            )
            if not exe_path:
                try:
                    mounts = _discover_mount_points()
                    for _, mount_path in mounts.items():
                        found = find_game_exe(mount_path)
                        if found:
                            exe_path = found
                            log.info("Found exe on mapped drive: %s", found)
                            break
                except Exception as e:
                    log.debug("Drive search failed: %s", e)
            if exe_path:
                actual_install_path = str(exe_path.parent)
            else:
                log.info("Could not auto-detect game exe — recording install without exe_path")

        fresh_game = db.get_game(game_id) or game
        icon_path  = ""
        try:
            if exe_path and Path(str(exe_path)).exists():
                icon_cache = Path.home() / ".config" / "vaultplay" / "icons"
                safe = re.sub(r"[/\x00]", "_", title).strip()
                extracted = extract_exe_icon(Path(str(exe_path)), icon_cache / safe)
                if extracted:
                    icon_path = extracted
            if not icon_path:
                icon_url = fresh_game["cover_url"] or ""
                if icon_url:
                    cached = db.get_cached_art(icon_url)
                    if cached:
                        icon_path = cached
        except Exception:
            pass

        launch_cmd = launch_cwd = launch_icon = ""
        if exe_path:
            game_dir   = str(actual_install_path) if actual_install_path else ""
            launch_cmd, launch_cwd, _ = _extract_launch_info(
                title, prefix_path, str(exe_path), wine_bin, game_dir)
            launch_icon = icon_path

        desktop_path = create_direct_desktop(title, launch_cmd, launch_cwd, launch_icon)

        if options.get("cleanup_tmp", True):
            _cleanup(tmp_path, True, prog)
        db.record_install(
            game_id=game_id,
            install_path=actual_install_path or str(prefix_path),
            wine_prefix=str(prefix_path),
            exe_path=str(exe_path) if exe_path else "",
            desktop_path=desktop_path,
            launch_cmd=launch_cmd,
            launch_cwd=launch_cwd,
            launch_icon=launch_icon,
        )

        # ── Update & DLC Install Support — apply pending updates/DLC/crackfix ──
        addon_result = _install_pending_addons(
            game_id, prefix_path, wine_bin,
            options.get("selected_addon_ids"), tmp_base, prog)
        final_version = addon_result["installed_version"]
        if not (final_version["dotted"] or final_version["plain"] or final_version["date"]):
            final_version = scanner.detect_addon_version(
                archive_name, [exe_path.name] if exe_path else [], game["folder_name"])
        if final_version["dotted"] or final_version["plain"] or final_version["date"]:
            db.set_installed_version(game_id, **final_version)

        prog("Done", 100, f"'{title}' installed!")
        return {"success": True, "error": None,
                "exe_path": str(exe_path) if exe_path else "",
                "desktop_path": desktop_path,
                "tmp_path": str(tmp_path),
                "cancelled": False,
                "failed_redists": failed_redists,
                "failed_addons": addon_result["failed"]}

    # ── Portable flow ─────────────────────────────────────────────────────────
    elif install_tag == "portable":
        prog("Creating Wine prefix", 10, f"Prefix: {prefix_name}")
        create_wine_prefix(prefix_path, prog, wine_bin=wine_bin)

        if db.get_setting("wine_drive_mapping", "auto") == "auto":
            map_all_drives(_resolve_actual_prefix(prefix_path, wine_bin))

        failed_redists = []
        if options.get("redists"):
            failed_redists = install_redists(prefix_path, options["redists"], prog, force=options.get("force_redists", False))

        prog("Copying files", 40, f"Copying to {game_dest}…")
        try:
            if game_dest.exists():
                shutil.rmtree(game_dest)
            shutil.copytree(str(source_path), str(game_dest))
        except Exception as e:
            return fail(f"Copy failed: {e}")

        prog("Finding game exe", 70, "Locating launch executable…")
        exe_path = find_game_exe(game_dest)
        if not exe_path:
            return fail("No game executable found in game files")

        launcher_type = "direct"
        script_path   = ""
        desktop_path  = ""

        fresh_game = db.get_game(game_id) or game
        icon_path  = ""
        try:
            if exe_path and Path(str(exe_path)).exists():
                icon_cache = Path.home() / ".config" / "vaultplay" / "icons"
                safe = re.sub(r"[/\x00]", "_", title).strip()
                extracted = extract_exe_icon(Path(str(exe_path)), icon_cache / safe)
                if extracted:
                    icon_path = extracted
            if not icon_path:
                icon_url = fresh_game["cover_url"] or ""
                if icon_url:
                    cached = db.get_cached_art(icon_url)
                    if cached:
                        icon_path = cached
        except Exception:
            pass

        prog("Creating launcher", 85, "Generating .desktop launcher…")
        launch_cmd, launch_cwd, _ = _extract_launch_info(
            title, prefix_path, str(exe_path), wine_bin, str(game_dest))
        launch_icon  = icon_path
        desktop_path = create_direct_desktop(title, launch_cmd, launch_cwd, launch_icon)

        if options.get("cleanup_tmp", True):
            _cleanup(tmp_path, True, prog)

        db.record_install(
            game_id=game_id,
            install_path=str(game_dest),
            wine_prefix=str(prefix_path),
            exe_path=str(exe_path),
            game_path=str(game_dest),
            launcher_type=launcher_type,
            desktop_path=desktop_path,
            script_path=script_path,
            launch_cmd=launch_cmd,
            launch_cwd=launch_cwd,
            launch_icon=launch_icon,
        )

        # ── Update & DLC Install Support — apply pending updates/DLC/crackfix ──
        addon_result = _install_pending_addons(
            game_id, prefix_path, wine_bin,
            options.get("selected_addon_ids"), tmp_base, prog)
        final_version = addon_result["installed_version"]
        if not (final_version["dotted"] or final_version["plain"] or final_version["date"]):
            final_version = scanner.detect_addon_version(
                archive_name, [exe_path.name] if exe_path else [], game["folder_name"])
        if final_version["dotted"] or final_version["plain"] or final_version["date"]:
            db.set_installed_version(game_id, **final_version)

        prog("Done", 100, f"'{title}' installed!")
        return {
            "success":      True,
            "error":        None,
            "exe_path":     str(exe_path),
            "desktop_path": desktop_path,
            "script_path":  script_path,
            "tmp_path":     str(tmp_path),
            "cancelled":    False,
            "failed_redists": failed_redists,
            "failed_addons": addon_result["failed"],
        }

    return fail(f"Unknown install_tag: {install_tag}")


def _cleanup(tmp_path: Path, do_cleanup: bool, prog: Callable):
    if do_cleanup and tmp_path.exists():
        prog("Cleanup", 95, "Removing temp files…")
        try:
            shutil.rmtree(tmp_path)
        except Exception as e:
            log.warning("Cleanup failed: %s", e)
