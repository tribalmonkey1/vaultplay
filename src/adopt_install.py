"""
adopt_install.py — "Already installed? Locate Game" logic for VaultPlay

Re-creates a game's install record (installs row + .desktop launcher) from
whatever the user still has on disk: an install folder, a Wine/Proton prefix
and/or a VaultPlay .desktop file. Anything missing is rebuilt from the same
recommended settings a normal install uses (ProtonDB-recommended Proton
version, auto-detected redistributables, ...), or from the user's choices.

Qt-free on purpose: ui/locate_game_dialog.py drives it, but it can be tested
and called from anywhere.

    info = inspect(game, install_path, wine_prefix, desktop_path)   # fast, read-only
    result = adopt(game_id, plan, progress_cb)                      # does the work
"""

import logging
import os
import re
from pathlib import Path
from typing import Callable, Optional

import db
import dbsafety
import installer
import protondb

log = logging.getLogger(__name__)


# ── Small helpers ────────────────────────────────────────────────────────────

def _g(game, key, default=None):
    try:
        v = game[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if v is None else v


def normalize_prefix_root(p) -> str:
    """Accept either a prefix root or a Proton <root>/pfx path; return the root.
    '' stays ''."""
    if not p:
        return ""
    path = Path(str(p).strip()).expanduser()
    if path.name == "pfx" and (path / "system.reg").exists():
        path = path.parent
    return str(path)


def prefix_initialized(root) -> bool:
    if not root:
        return False
    r = Path(root)
    return (r / "pfx" / "system.reg").exists() or (r / "system.reg").exists()


def prefix_is_proton(root) -> bool:
    if not root:
        return False
    r = Path(root)
    return (r / "pfx").is_dir() or (r / "version").exists()


def candidate_exes(install_path, limit: int = 60) -> list:
    """Likely game executables under install_path (setup/redist/crash helpers
    skipped), shallowest first. Never raises."""
    out = []
    if not install_path or not Path(install_path).is_dir():
        return out
    try:
        for root, dirs, files in os.walk(install_path):
            dirs[:] = [d for d in dirs if not installer.SKIP_EXE_RE.search(d)]
            for f in files:
                if f.lower().endswith(".exe") and not installer.SKIP_EXE_RE.search(f):
                    out.append(Path(root) / f)
            if len(out) > 500:
                break
    except OSError:
        pass
    out.sort(key=lambda p: (len(p.parts), p.name.lower()))
    return [str(p) for p in out[:limit]]


def _recommended_proton_value(game) -> tuple:
    """(value, source) — the same choice a fresh install would pre-select:
    ProtonDB's top-voted version if installed, else the user's default."""
    installed = protondb.get_versions_for_ui()
    rec = _g(game, "recommended_proton", "") or ""
    if rec:
        m = protondb.match_to_installed(rec, installed)
        if m:
            return m, "recommended"
    default = db.get_setting("default_proton_version", "") or ""
    if default and any(v == default for _, v in installed):
        return default, "default"
    if installed:
        return installed[0][1], "first-installed"
    return "", ""


def _desktop_exec_exe(exe_token: str, cwd: str) -> str:
    if not exe_token:
        return ""
    p = Path(exe_token)
    if not p.is_absolute() and cwd:
        p = Path(cwd) / p
    return str(p) if p.exists() else ""


# ── Inspection (read-only) ───────────────────────────────────────────────────

def inspect(game, install_path: str = "", wine_prefix: str = "",
            desktop_path: str = "") -> dict:
    """
    Work out everything that can be learned from the paths the user gave.
    Returns:
      install_path, wine_prefix (root, '' = none yet), prefix_exists,
      prefix_is_proton, desktop (parsed dict or {}), exe, exe_candidates,
      proton_version, proton_source ('desktop'|'prefix'|recommended|...|''),
      installed_redists, will_create_prefix, notes (list[str])
    """
    notes = []
    d = {}
    parsed_exec = {}
    if desktop_path:
        dp = Path(desktop_path).expanduser()
        if dp.is_file():
            d = dbsafety.parse_desktop(dp)
            parsed_exec = dbsafety.parse_exec(d.get("Exec", ""))
            if not d.get("Exec"):
                notes.append("That .desktop file has no Exec= line — it will be ignored.")
                d, parsed_exec = {}, {}
        else:
            notes.append("The .desktop file doesn't exist.")

    # Prefix: explicit > desktop's compat dir > desktop's WINEPREFIX
    root = normalize_prefix_root(wine_prefix)
    if not root and parsed_exec:
        root = normalize_prefix_root(parsed_exec.get("compat") or parsed_exec.get("wineprefix"))
    exists = prefix_initialized(root)
    is_proton = prefix_is_proton(root)

    # Install folder: explicit > desktop's Path= > exe's folder
    cwd = d.get("Path", "")
    exe = _desktop_exec_exe(parsed_exec.get("exe", ""), cwd)
    inst = (install_path or "").strip()
    if not inst:
        inst = cwd or (str(Path(exe).parent) if exe else "")
    if inst and not Path(inst).is_dir():
        notes.append("The install folder doesn't exist.")

    candidates = []
    if not exe:
        candidates = candidate_exes(inst)
        if len(candidates) == 1:
            exe = candidates[0]
    elif inst:
        candidates = candidate_exes(inst)
        if exe not in candidates:
            candidates.insert(0, exe)

    # Proton / Wine version
    version, source = "", ""
    wb = parsed_exec.get("wine_bin", "") if parsed_exec else ""
    if wb:
        v = protondb.find_version_value_for_binary(wb)
        if v:
            version, source = v, "desktop"
    if not version:
        version, source = _recommended_proton_value(game)
        if is_proton and source in ("recommended", "default", "first-installed"):
            pass   # keep, a Proton prefix needs a Proton build
    if version and source != "desktop":
        # Prefer a Proton build for a Proton-layout prefix.
        if is_proton and version == "system-wine":
            for _, val in protondb.get_versions_for_ui():
                if val != "system-wine":
                    version, source = val, "first-installed"
                    break

    redists_present = []
    if exists:
        redists_present = sorted(installer.get_installed_redists(Path(root)))

    return {
        "install_path": inst,
        "wine_prefix": root,
        "prefix_exists": exists,
        "prefix_is_proton": is_proton,
        "desktop": d,
        "desktop_exec": parsed_exec,
        "exe": exe,
        "exe_candidates": candidates,
        "proton_version": version,
        "proton_source": source,
        "installed_redists": redists_present,
        "will_create_prefix": not exists,
        "notes": notes,
    }


def default_prefix_path(game) -> str:
    name = installer.make_prefix_name(_g(game, "folder_name", "") or _g(game, "title", "game"))
    return str(installer._wine_prefix_root() / name)


def suggest_redists(game) -> tuple:
    """(verbs, source) — same detection the Install dialog uses."""
    try:
        import steamdb as steamdb_mod
        base = steamdb_mod.BASELINE_REDISTS[:]
        if db.get_setting("auto_detect_redists", "true") == "true" and _g(game, "steam_app_id"):
            title = _g(game, "title", "") or _g(game, "display_name", "")
            res = steamdb_mod.get_redists_for_install(
                _g(game, "steam_app_id"), title, auto_detect=True)
            return installer.normalize_redists(res["auto"]), res["source"]
        return installer.normalize_redists(base), "baseline"
    except Exception as e:
        log.warning("suggest_redists failed: %s", e)
        return [], "none"


# ── Adoption ─────────────────────────────────────────────────────────────────

def adopt(game_id: int, plan: dict, progress_cb: Optional[Callable] = None) -> dict:
    """
    plan = {
      'install_path': str, 'wine_prefix': str ('' → default for this game),
      'exe_path': str (required), 'desktop_path': str ('' = none given),
      'proton_version': str, 'redists': list[str],
    }
    Returns {"success", "error", "desktop_path", "launch_cmd", "warnings": []}
    """
    def prog(stage, pct, msg=""):
        if progress_cb:
            progress_cb(stage, pct, msg)
        log.info("[LOCATE] %s %d%% %s", stage, pct, msg)

    def fail(msg):
        log.warning("[LOCATE] failed: %s", msg)
        return {"success": False, "error": msg, "desktop_path": "",
                "launch_cmd": "", "warnings": []}

    warnings = []
    game = db.get_game(game_id)
    if not game:
        return fail("Game not found in the library.")

    exe_path = (plan.get("exe_path") or "").strip()
    if not exe_path or not Path(exe_path).exists():
        return fail("Pick the game's executable first.")
    install_path = (plan.get("install_path") or "").strip() or str(Path(exe_path).parent)
    title = _g(game, "title") or _g(game, "display_name") or _g(game, "folder_name", "")
    proton_value = plan.get("proton_version", "") or ""

    # Safety net: snapshot the DB before writing anything.
    dbsafety.backup_now("before_locate")

    version_path = protondb.get_version_path(proton_value) if proton_value else None
    wine_bin = installer._resolve_wine_bin(version_path) if version_path else "wine"
    is_proton = Path(wine_bin).name == "proton"

    # ── Prefix ────────────────────────────────────────────────────────────
    prefix_root = normalize_prefix_root(plan.get("wine_prefix")) or default_prefix_path(game)
    prefix_path = Path(prefix_root)
    created = False
    if not prefix_initialized(prefix_path):
        prog("Wine prefix", 5, f"Creating {prefix_path.name}…")
        ok = installer.create_wine_prefix(
            prefix_path, lambda s, p, m: prog(s, 5 + int(p * 0.25), m), wine_bin)
        if not ok:
            return fail("Could not create the Wine prefix. Check that the "
                        "selected Proton/Wine version works.")
        created = True
        try:
            installer.map_all_drives(installer._resolve_actual_prefix(prefix_path, wine_bin))
        except Exception as e:
            warnings.append(f"Could not map drives: {e}")
    else:
        prog("Wine prefix", 30, f"Using existing prefix {prefix_path.name}")

    # ── Redistributables ──────────────────────────────────────────────────
    redists = installer.normalize_redists(plan.get("redists") or [])
    failed = []
    if redists:
        prog("Redistributables", 32, f"Installing {len(redists)} redistributable(s)…")
        failed = installer.install_redists(
            prefix_path, redists,
            lambda s, p, m: prog(s, 32 + int(p * 0.45), m)) or []
        if failed:
            warnings.append("Some redistributables failed: " + ", ".join(failed))
    else:
        prog("Redistributables", 77, "None requested")

    # ── Launcher ──────────────────────────────────────────────────────────
    prog("Launcher", 80, "Writing launch settings…")
    launch_cmd = launch_cwd = launch_icon = ""
    desktop_path = ""

    given_desktop = (plan.get("desktop_path") or "").strip()
    keep = False
    if given_desktop and Path(given_desktop).is_file() and not created:
        dd = dbsafety.parse_desktop(Path(given_desktop))
        pe = dbsafety.parse_exec(dd.get("Exec", ""))
        same_prefix = str(prefix_path) in dd.get("Exec", "")
        desk_ver = protondb.find_version_value_for_binary(pe.get("wine_bin", "")) \
            if pe.get("wine_bin") else None
        if dd.get("Exec") and same_prefix and (desk_ver == proton_value or not proton_value):
            keep = True
            launch_cmd = dd["Exec"]
            launch_cwd = dd.get("Path", "") or install_path
            launch_icon = dd.get("Icon", "")
            desktop_path = given_desktop
            if "X-VaultPlay" not in dd:
                warnings.append("The .desktop file wasn't written by VaultPlay; "
                                "it was kept as-is.")

    if not keep:
        icon_path = ""
        try:
            icon_cache = Path.home() / ".config" / "vaultplay" / "icons"
            safe = re.sub(r"[/\x00]", "_", title).strip()
            extracted = installer.extract_exe_icon(Path(exe_path), icon_cache / safe)
            if extracted:
                icon_path = extracted
            if not icon_path:
                url = _g(game, "cover_url", "")
                if url:
                    icon_path = db.get_cached_art(url) or ""
        except Exception as e:
            log.debug("icon lookup failed: %s", e)
        try:
            launch_cmd, launch_cwd, _ = installer._extract_launch_info(
                title, prefix_path, exe_path, wine_bin, install_path,
                game_id=game_id, engine=installer._game_engine(game),
                use_shortcuts=False)
            launch_icon = icon_path
            desktop_path = installer.create_direct_desktop(
                title, launch_cmd, launch_cwd, launch_icon)
        except Exception as e:
            return fail(f"Could not create the launcher: {e}")

    # ── Record ────────────────────────────────────────────────────────────
    prog("Saving", 92, "Recording install…")
    try:
        db.record_install(
            game_id=game_id,
            install_path=install_path,
            wine_prefix=str(prefix_path),
            exe_path=exe_path,
            game_path=install_path,
            desktop_path=desktop_path,
            launch_cmd=launch_cmd,
            launch_cwd=launch_cwd,
            launch_icon=launch_icon,
            proton_version=proton_value,
        )
        installer._store_redist_result(
            game_id, sorted(set(redists) | set(installer.get_installed_redists(prefix_path))),
            failed)
    except Exception as e:
        return fail(f"Could not save the install record: {e}")

    # Installed version: best effort from the archive/exe name (no addons known).
    try:
        import scanner
        v = scanner.detect_addon_version(
            _g(game, "archive_name", "") or "", [Path(exe_path).name],
            _g(game, "folder_name", ""))
        if v.get("dotted") or v.get("plain") or v.get("date"):
            db.set_installed_version(game_id, **v)
    except Exception as e:
        log.debug("installed-version seed skipped: %s", e)

    prog("Done", 100, f"'{title}' added to your installed games.")
    return {"success": True, "error": None, "desktop_path": desktop_path,
            "launch_cmd": launch_cmd, "warnings": warnings}
