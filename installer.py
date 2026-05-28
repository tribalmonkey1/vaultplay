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

log = logging.getLogger(__name__)

WINE_PREFIX_ROOT = Path.home() / ".local" / "share" / "wineprefixes"

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
    try:
        import py7zr
        with py7zr.SevenZipFile(str(archive_path), mode="r") as z:
            names  = z.getnames()
            total  = len(names)
            done   = [0]

            def _cb(x):
                done[0] += 1
                if progress_cb:
                    progress_cb("Extracting", int(done[0] / total * 100),
                                f"Extracting {x.filename}…")

            z.extractall(path=str(dest), callback=_cb)
        return True
    except Exception as e:
        log.error("7z extraction failed: %s", e)
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


def extract_archive(nas_path: str, archive_name: str, file_type: str,
                    dest: Path, progress_cb=None) -> bool:
    dest.mkdir(parents=True, exist_ok=True)
    archive_path = Path(nas_path) / archive_name
    if file_type == "7zip":
        return _extract_7z(archive_path, dest, progress_cb)
    elif file_type == "rar":
        return _extract_rar(archive_path, dest, progress_cb)
    return False


# ── ISO mounting ──────────────────────────────────────────────────────────────

def _find_iso(search_root: Path) -> Optional[Path]:
    for p in search_root.rglob("*.iso"):
        return p
    return None


def mount_iso_udisks(iso_path: Path) -> Optional[Path]:
    try:
        result = subprocess.run(
            ["udisksctl", "loop-setup", "-f", str(iso_path), "--no-user-interaction"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            log.warning("udisksctl loop-setup failed: %s", result.stderr.strip())
            return None
        # Parse loop device from output: "Mapped file … as /dev/loop0."
        m = re.search(r"(/dev/loop\d+)", result.stdout + result.stderr)
        if not m:
            return None
        loop_dev = m.group(1)

        mount_result = subprocess.run(
            ["udisksctl", "mount", "-b", loop_dev, "--no-user-interaction"],
            capture_output=True, text=True, timeout=30
        )
        if mount_result.returncode != 0:
            log.warning("udisksctl mount failed: %s", mount_result.stderr.strip())
            _unmount_loop_udisks(loop_dev)
            return None

        # Parse mount point: "Mounted /dev/loop0 at /run/media/user/LABEL."
        mp = re.search(r"at\s+(\S+?)\.?\s*$", mount_result.stdout.strip())
        if mp:
            return Path(mp.group(1))
        return None
    except FileNotFoundError:
        log.warning("udisksctl not found, trying fuseiso")
        return None
    except Exception as e:
        log.error("udisksctl mount error: %s", e)
        return None


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


def mount_iso(iso_path: Path) -> tuple[Optional[Path], str]:
    """
    Try udisksctl first, fall back to fuseiso.
    Returns (mount_point, method) where method is 'udisksctl' or 'fuseiso'.
    """
    mp = mount_iso_udisks(iso_path)
    if mp:
        return mp, "udisksctl"
    mp = mount_iso_fuse(iso_path)
    if mp:
        return mp, "fuseiso"
    return None, ""


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


# ── Wine prefix ───────────────────────────────────────────────────────────────

def create_wine_prefix(prefix_path: Path, progress_cb=None) -> bool:
    prefix_path.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "WINEPREFIX": str(prefix_path)}
    if progress_cb:
        progress_cb("Creating Wine prefix", 0, f"Initialising {prefix_path.name}…")
    try:
        subprocess.run(["wineboot", "--init"], env=env,
                       capture_output=True, timeout=120)
        if progress_cb:
            progress_cb("Creating Wine prefix", 100, "Prefix ready.")
        return True
    except Exception as e:
        log.error("wineboot failed: %s", e)
        return False


def install_redists(prefix_path: Path, redists: list, progress_cb=None) -> list:
    env    = {**os.environ, "WINEPREFIX": str(prefix_path)}
    failed = []
    for i, verb in enumerate(redists):
        if progress_cb:
            progress_cb("Redistributables",
                        int(i / len(redists) * 100),
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


# ── Desktop / launcher generation ────────────────────────────────────────────

APPS_DIR = Path.home() / ".local" / "share" / "applications"


def _safe_name(title: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", title).strip("_").lower()


def create_direct_desktop(title: str, exe_path: str,
                           prefix_path: Path, icon_path: str = "") -> str:
    """Create a .desktop file that launches the game directly via wine."""
    APPS_DIR.mkdir(parents=True, exist_ok=True)
    slug    = _safe_name(title)
    desktop = APPS_DIR / f"vaultplay_{slug}.desktop"
    content = f"""[Desktop Entry]
Name={title}
Exec=env WINEPREFIX="{prefix_path}" wine "{exe_path}"
Type=Application
Categories=Game;
"""
    if icon_path and Path(icon_path).exists():
        content += f"Icon={icon_path}\n"
    desktop.write_text(content)
    log.info("Created direct .desktop: %s", desktop)
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


# ── Main install pipeline ─────────────────────────────────────────────────────

def run_install(game_id: int, options: dict, progress_cb: Callable = None) -> dict:
    """
    Full install pipeline.

    options = {
        'wine_prefix_name': str,        # e.g. 'thewitcher3'
        'game_path':        str,        # destination for portable games
        'launcher_type':    str,        # 'direct' | 'script'  (portables only)
        'redists':          list[str],
        'cleanup_tmp':      bool,
        'proton_version':   str,        # passed to wineboot env
    }

    progress_cb(stage: str, percent: int, message: str)
    """

    def prog(stage, pct, msg):
        if progress_cb:
            progress_cb(stage, pct, msg)
        log.info("[%s] %d%% — %s", stage, pct, msg)

    game = db.get_game(game_id)
    if not game:
        return {"success": False, "error": "Game not found"}

    title        = game["title"] or game["display_name"] or game["folder_name"]
    nas_path     = game["nas_path"]
    file_type    = game["file_type"]
    archive_name = game["archive_name"]
    install_tag  = game["install_tag"]

    # ── Paths ─────────────────────────────────────────────────────────────────
    prefix_name  = options.get("wine_prefix_name") or make_prefix_name(game["folder_name"])
    prefix_path  = WINE_PREFIX_ROOT / prefix_name
    tmp_base     = Path(db.get_setting("tmp_path",
                                       str(Path.home() / "Games" / ".tmp")))
    tmp_path     = tmp_base / game["folder_name"]
    game_path    = Path(options.get("game_path") or
                        (db.get_install_paths()[0] if db.get_install_paths() else
                         str(Path.home() / "Games")))
    game_dest    = game_path / game["folder_name"]

    # ── Extract ───────────────────────────────────────────────────────────────
    source_path = None
    if file_type in ("rar", "7zip"):
        prog("Extracting", 0, f"Extracting {archive_name}…")
        ok = extract_archive(nas_path, archive_name, file_type, tmp_path, prog)
        if not ok:
            return {"success": False, "error": f"Extraction failed for {archive_name}"}
        source_path = tmp_path
    else:
        # Loose: work directly from NAS path
        source_path = Path(nas_path)

    # ── ISO flow ──────────────────────────────────────────────────────────────
    if install_tag == "iso":
        prog("Finding ISO", 10, "Locating .iso file…")
        iso_file = _find_iso(source_path)
        if not iso_file:
            return {"success": False, "error": "No .iso file found after extraction"}

        prog("Mounting ISO", 20, f"Mounting {iso_file.name}…")
        mount_point, mount_method = mount_iso(iso_file)
        if not mount_point:
            return {"success": False,
                    "error": "Could not mount ISO. Install udisksctl or fuseiso."}

        loop_dev = ""
        if mount_method == "udisksctl":
            # Try to parse loop dev for clean unmount later
            try:
                result = subprocess.run(
                    ["udisksctl", "info", "-b", "/dev/loop0"],
                    capture_output=True, text=True
                )
                m = re.search(r"(/dev/loop\d+)", result.stdout)
                if m:
                    loop_dev = m.group(1)
            except Exception:
                pass

        prog("Finding setup", 30, "Looking for installer in ISO…")
        installer_exe = find_installer_exe(mount_point)
        if not installer_exe:
            unmount_iso(mount_point, mount_method, loop_dev)
            return {"success": False,
                    "error": "No installer .exe found in ISO"}

        prog("Creating Wine prefix", 40, f"Prefix: {prefix_name}")
        create_wine_prefix(prefix_path, prog)

        if options.get("redists"):
            install_redists(prefix_path, options["redists"], prog)

        prog("Installing", 60, f"Running {installer_exe.name} via Wine…")
        env = {**os.environ, "WINEPREFIX": str(prefix_path)}
        try:
            subprocess.run(["wine", str(installer_exe)], env=env, timeout=1800)
        except subprocess.TimeoutExpired:
            unmount_iso(mount_point, mount_method, loop_dev)
            return {"success": False, "error": "Installer timed out"}
        except FileNotFoundError:
            unmount_iso(mount_point, mount_method, loop_dev)
            return {"success": False, "error": "wine not found"}

        prog("Unmounting ISO", 90, "Cleaning up ISO mount…")
        unmount_iso(mount_point, mount_method, loop_dev)

        exe_path = find_game_exe(
            prefix_path / "drive_c" / "Program Files"
        ) or find_game_exe(
            prefix_path / "drive_c" / "Program Files (x86)"
        )

        _cleanup(tmp_path, options.get("cleanup_tmp", True), prog)
        db.record_install(game_id=game_id, install_path=str(prefix_path),
                          wine_prefix=str(prefix_path), exe_path=str(exe_path or ""))
        prog("Done", 100, f"'{title}' installed!")
        return {"success": True, "error": None, "exe_path": str(exe_path or "")}

    # ── Installer flow ────────────────────────────────────────────────────────
    elif install_tag == "installer":
        prog("Finding installer", 10, "Locating setup .exe…")
        installer_exe = find_installer_exe(source_path)
        if not installer_exe:
            return {"success": False, "error": "No installer .exe found"}

        prog("Creating Wine prefix", 20, f"Prefix: {prefix_name}")
        create_wine_prefix(prefix_path, prog)

        if options.get("redists"):
            install_redists(prefix_path, options["redists"], prog)

        prog("Installing", 50, f"Running {installer_exe.name} via Wine…")
        env = {**os.environ, "WINEPREFIX": str(prefix_path)}
        try:
            subprocess.run(["wine", str(installer_exe)], env=env, timeout=1800)
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Installer timed out"}
        except FileNotFoundError:
            return {"success": False, "error": "wine not found"}

        exe_path = find_game_exe(
            prefix_path / "drive_c" / "Program Files"
        ) or find_game_exe(
            prefix_path / "drive_c" / "Program Files (x86)"
        )

        _cleanup(tmp_path, options.get("cleanup_tmp", True), prog)
        db.record_install(game_id=game_id, install_path=str(prefix_path),
                          wine_prefix=str(prefix_path), exe_path=str(exe_path or ""))
        prog("Done", 100, f"'{title}' installed!")
        return {"success": True, "error": None, "exe_path": str(exe_path or "")}

    # ── Portable flow ─────────────────────────────────────────────────────────
    elif install_tag == "portable":
        prog("Creating Wine prefix", 10, f"Prefix: {prefix_name}")
        create_wine_prefix(prefix_path, prog)

        if options.get("redists"):
            install_redists(prefix_path, options["redists"], prog)

        prog("Copying files", 40, f"Copying to {game_dest}…")
        try:
            if game_dest.exists():
                shutil.rmtree(game_dest)
            shutil.copytree(str(source_path), str(game_dest))
        except Exception as e:
            return {"success": False, "error": f"Copy failed: {e}"}

        prog("Finding game exe", 70, "Locating launch executable…")
        exe_path = find_game_exe(game_dest)
        if not exe_path:
            return {"success": False,
                    "error": "No game executable found in game files"}

        launcher_type = options.get("launcher_type", "direct")
        script_path   = ""
        desktop_path  = ""

        # Fetch icon from cache if available
        icon_path = ""
        try:
            icon_url = game.get("cover_url") or ""
            if icon_url:
                cached = db.get_cached_art(icon_url)
                if cached:
                    icon_path = cached
        except Exception:
            pass

        prog("Creating launcher", 85, f"Generating {launcher_type} launcher…")
        if launcher_type == "script":
            script_path, desktop_path = create_script_launcher(
                title, str(exe_path), prefix_path, icon_path
            )
        else:
            desktop_path = create_direct_desktop(
                title, str(exe_path), prefix_path, icon_path
            )

        _cleanup(tmp_path, options.get("cleanup_tmp", True), prog)

        db.record_install(
            game_id=game_id,
            install_path=str(game_dest),
            wine_prefix=str(prefix_path),
            exe_path=str(exe_path),
            game_path=str(game_dest),
            launcher_type=launcher_type,
            desktop_path=desktop_path,
            script_path=script_path,
        )
        prog("Done", 100, f"'{title}' installed!")
        return {
            "success":      True,
            "error":        None,
            "exe_path":     str(exe_path),
            "desktop_path": desktop_path,
            "script_path":  script_path,
        }

    return {"success": False, "error": f"Unknown install_tag: {install_tag}"}


def _cleanup(tmp_path: Path, do_cleanup: bool, prog: Callable):
    if do_cleanup and tmp_path.exists():
        prog("Cleanup", 95, "Removing temp files…")
        try:
            shutil.rmtree(tmp_path)
        except Exception as e:
            log.warning("Cleanup failed: %s", e)
