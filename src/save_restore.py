"""
save_restore.py — restore backed-up saves / achievements into a (new) Wine prefix.

Why this exists
---------------
save_backup.py only ever moved saves OUT of a prefix into
<save_root>/PC/<folder_name>/ (and extras into <save_root>/_extras/PC/...).
Nothing ever moved them back. On a fresh install the DB has no link for the
game, so VaultPlay ran "Flow 1" again (as if no backup existed) and, worse,
when the user then picked the save folder it reported a *conflict* and offered
to OVERWRITE the existing backup with the brand-new empty save.

What this module adds (pure logic, no Qt — see ui/save_restore_dialog.py):

  * a small manifest per game, written whenever a save is linked, recording
    WHERE in the prefix the save lives (relative to drive_c, with the Wine
    user name tokenised so a new machine / new prefix still maps correctly):
        <save_root>/_manifests/PC/<folder_name>.json
    (outside the game's own save folder, so the game never sees it and the
    conflict check in move_and_link() is unaffected).
  * find_restorable(): what's in the backup and where it should go now.
        - main save: needs the manifest to know the target. Backups made before
          this module existed have none ("legacy"); those are handled at the
          post-play step instead (see adopt_existing_backup()).
        - extras (achievements/stats/leaderboards): their canonical path under
          _extras/PC/<folder>/ already mirrors the path under drive_c, so no
          manifest is needed.
  * restore_main()/restore_extra(): symlink the backup into the prefix. Anything
    already at the target is MOVED to <save_root>/_replaced/... first — never
    deleted — so a fresh-install default save can never destroy real progress
    and vice-versa.
  * adopt_existing_backup(): "use my existing backup for THIS folder" — the
    non-destructive alternative to the old overwrite prompt.
"""
import datetime
import getpass
import json
import logging
import re
import shutil
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

USER_TOKEN = "{USER}"


# ── helpers ───────────────────────────────────────────────────────────────────

def _safe(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", name)


def rel_to_drive_c(p) -> Optional[Path]:
    """Path relative to the 'drive_c' component of p (prefix-agnostic)."""
    parts = Path(p).parts
    for i in range(len(parts) - 1, -1, -1):
        if parts[i] == "drive_c":
            return Path(*parts[i + 1:]) if i + 1 < len(parts) else Path(".")
    return None


def tokenize_rel(rel: Path) -> str:
    parts = list(Path(rel).parts)
    if len(parts) >= 2 and parts[0].lower() == "users" and parts[1].lower() != "public":
        parts[1] = USER_TOKEN
    return "/".join(parts)


def detect_user(drive_c: Path, proton: bool) -> str:
    """The Wine user name to use inside this prefix."""
    names = []
    try:
        names = [d.name for d in (Path(drive_c) / "users").iterdir()
                 if d.is_dir() and d.name.lower() != "public"]
    except OSError:
        pass
    if "steamuser" in names:
        return "steamuser"
    try:
        me = getpass.getuser()
    except Exception:
        me = ""
    if me in names:
        return me
    if names:
        return sorted(names)[0]
    return "steamuser" if proton else (me or "steamuser")


def expand_rel(tokenized: str, drive_c: Path, user: str) -> Path:
    return Path(drive_c) / Path(tokenized.replace(USER_TOKEN, user))


def _count_files(folder: Path):
    n, newest = 0, 0.0
    try:
        for f in Path(folder).rglob("*"):
            try:
                if f.is_file():
                    n += 1
                    newest = max(newest, f.stat().st_mtime)
            except OSError:
                pass
    except OSError:
        pass
    return n, newest


def _safety_dir(save_root: Path, folder_name: str) -> Path:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    d = Path(save_root) / "_replaced" / "PC" / folder_name / stamp
    d.mkdir(parents=True, exist_ok=True)
    return d


def _same_target(link: Path, canonical: Path) -> bool:
    try:
        return link.is_symlink() and link.resolve() == Path(canonical).resolve()
    except OSError:
        return False


# ── manifest ──────────────────────────────────────────────────────────────────

def manifest_path(save_root, folder_name: str) -> Path:
    return Path(save_root) / "_manifests" / "PC" / f"{_safe(folder_name)}.json"


def _steam_id_for_folder(folder_name: str) -> Optional[int]:
    """Steam App ID for a library game, by NAS folder name. None if unknown."""
    try:
        import db
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT m.steam_app_id AS sid FROM games g "
                "JOIN metadata m ON m.game_id = g.id WHERE g.folder_name = ?",
                (folder_name,)).fetchone()
        return int(row["sid"]) if row and row["sid"] else None
    except Exception:
        return None


def _library_has_folder(folder_name: str) -> bool:
    try:
        import db
        with db.get_connection() as conn:
            return conn.execute("SELECT 1 FROM games WHERE folder_name = ?",
                                (folder_name,)).fetchone() is not None
    except Exception:
        return True   # can't tell — assume yes, which makes callers cautious


def _migrate_alias(save_root, folder_name: str) -> None:
    """
    Backups are keyed by NAS folder name. If this game's folder was renamed,
    find a backup whose manifest carries the SAME Steam App ID and move it
    (backup folder, _extras folder and manifest) to the current folder name.
    Skipped when anything already exists under the current name, or when the
    old name still belongs to a game in the library (e.g. two editions of the
    same Steam game) — never takes another game's backup. Never raises.
    """
    try:
        save_root = Path(save_root)
        sid = _steam_id_for_folder(folder_name)
        mdir = save_root / "_manifests" / "PC"
        if not sid or not mdir.is_dir():
            return
        dest_main = save_root / "PC" / folder_name
        dest_x = save_root / "_extras" / "PC" / folder_name
        if (manifest_path(save_root, folder_name).exists()
                or dest_main.exists() or dest_x.exists()):
            return
        for mf in mdir.glob("*.json"):
            try:
                data = json.loads(mf.read_text())
            except (OSError, ValueError):
                continue
            old = data.get("folder_name")
            if data.get("steam_app_id") != sid or not old or old == folder_name:
                continue
            if _library_has_folder(old):
                continue
            for src, dst in ((save_root / "PC" / old, dest_main),
                             (save_root / "_extras" / "PC" / old, dest_x)):
                if src.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), str(dst))
            data["folder_name"] = folder_name
            newm = manifest_path(save_root, folder_name)
            newm.parent.mkdir(parents=True, exist_ok=True)
            newm.write_text(json.dumps(data, indent=2))
            mf.unlink()
            log.info("[SAVE RESTORE] Backup renamed %s -> %s (matched Steam ID %s)",
                     old, folder_name, sid)
            return
    except Exception as e:
        log.warning("[SAVE RESTORE] alias migration failed for %s: %s", folder_name, e)


def _stamp_manifest_steam_id(save_root, folder_name: str, man: dict) -> None:
    """Add the Steam App ID to an older manifest that lacks one."""
    try:
        if man and not man.get("steam_app_id"):
            sid = _steam_id_for_folder(folder_name)
            if sid:
                man["steam_app_id"] = sid
                manifest_path(save_root, folder_name).write_text(
                    json.dumps(man, indent=2))
    except Exception:
        pass


def write_manifest(save_root, folder_name: str, source_folder) -> bool:
    """Record where the main save lives inside the prefix. Never raises."""
    try:
        rel = rel_to_drive_c(source_folder)
        if rel is None:
            return False
        p = manifest_path(save_root, folder_name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "version": 1,
            "folder_name": folder_name,
            "steam_app_id": _steam_id_for_folder(folder_name),
            "source_rel": tokenize_rel(rel),
            "written": datetime.datetime.now().isoformat(timespec="seconds"),
        }, indent=2))
        return True
    except Exception as e:
        log.warning("[SAVE RESTORE] manifest write failed for %s: %s", folder_name, e)
        return False


def read_manifest(save_root, folder_name: str) -> Optional[dict]:
    try:
        return json.loads(manifest_path(save_root, folder_name).read_text())
    except (OSError, ValueError):
        return None


# ── discovery ─────────────────────────────────────────────────────────────────

def find_restorable(save_root, folder_name: str, drive_c, proton: bool,
                    linked_extra_canonicals=()) -> dict:
    """
    Returns {"main": {...}|None, "extras": [...], "legacy_main": bool}
      main:   canonical, target, files, newest, (target may already be linked → None)
      extras: [{"canonical", "target", "name"}] — backed-up files not yet linked
      legacy_main: a main backup exists but has no manifest (target unknown)
    """
    save_root, drive_c = Path(save_root), Path(drive_c)
    _migrate_alias(save_root, folder_name)      # renamed NAS folder → same backup
    user = detect_user(drive_c, proton)
    out = {"main": None, "extras": [], "legacy_main": False, "skipped_extras": []}

    canonical = save_root / "PC" / folder_name
    if canonical.is_dir():
        files, newest = _count_files(canonical)
        if files:
            man = read_manifest(save_root, folder_name)
            if man:
                _stamp_manifest_steam_id(save_root, folder_name, man)
            if man and man.get("source_rel"):
                target = expand_rel(man["source_rel"], drive_c, user)
                if not _same_target(target, canonical):
                    out["main"] = {"canonical": canonical, "target": target,
                                   "files": files, "newest": newest}
            else:
                out["legacy_main"] = True

    linked = {str(c) for c in linked_extra_canonicals}
    xroot = save_root / "_extras" / "PC" / folder_name
    if xroot.is_dir():
        for f in sorted(xroot.rglob("*")):
            try:
                if not f.is_file() or str(f) in linked:
                    continue
                rel = f.relative_to(xroot)
                tok = tokenize_rel(rel)
                target = expand_rel(tok, drive_c, user)
                under_users = tok.startswith("users/")
                if not under_users and not target.parent.exists():
                    out["skipped_extras"].append(f.name)
                    continue   # game's own folder not there — don't invent it
                if _same_target(target, f):
                    continue
                out["extras"].append({"canonical": f, "target": target, "name": f.name})
            except OSError:
                continue
    return out


# ── restore ───────────────────────────────────────────────────────────────────

def _make_room(target: Path, save_root, folder_name: str) -> Optional[Path]:
    """Move whatever is at target aside (never delete). Returns where it went."""
    if target.is_symlink():
        target.unlink()
        return None
    if not target.exists():
        return None
    if target.is_dir() and not any(target.iterdir()):
        target.rmdir()
        return None
    dest = _safety_dir(save_root, folder_name) / target.name
    shutil.move(str(target), str(dest))
    log.info("[SAVE RESTORE] Moved existing %s aside to %s", target, dest)
    return dest


def restore_main(canonical, target, save_root, folder_name: str) -> dict:
    """Symlink target → canonical. Existing content at target is preserved in _replaced/."""
    canonical, target = Path(canonical), Path(target)
    try:
        if _same_target(target, canonical):
            return {"ok": True, "moved_aside": None}
        target.parent.mkdir(parents=True, exist_ok=True)
        aside = _make_room(target, save_root, folder_name)
        target.symlink_to(canonical, target_is_directory=True)
        log.info("[SAVE RESTORE] Linked %s → %s", target, canonical)
        return {"ok": True, "moved_aside": str(aside) if aside else None}
    except Exception as e:
        log.error("[SAVE RESTORE] restore_main failed %s → %s: %s", target, canonical, e)
        return {"ok": False, "error": str(e)}


def restore_extra(canonical, target, save_root, folder_name: str) -> dict:
    canonical, target = Path(canonical), Path(target)
    try:
        if _same_target(target, canonical):
            return {"ok": True, "moved_aside": None}
        target.parent.mkdir(parents=True, exist_ok=True)
        aside = _make_room(target, save_root, folder_name)
        target.symlink_to(canonical)
        log.info("[SAVE RESTORE] Linked extra %s → %s", target, canonical)
        return {"ok": True, "moved_aside": str(aside) if aside else None}
    except Exception as e:
        log.error("[SAVE RESTORE] restore_extra failed %s → %s: %s", target, canonical, e)
        return {"ok": False, "error": str(e)}


def restore_all(game_id: int, folder_name: str, save_root, info: dict,
                do_main: bool, do_extras: bool) -> dict:
    """Perform the restore chosen in the dialog and record it in the DB."""
    import db
    res = {"main": None, "extras": [], "errors": [], "moved_aside": []}
    m = info.get("main")
    if do_main and m:
        r = restore_main(m["canonical"], m["target"], save_root, folder_name)
        if r["ok"]:
            db.set_save_paths(game_id, save_path=str(m["canonical"]),
                              save_source_path=str(m["target"]))
            res["main"] = str(m["target"])
            if r.get("moved_aside"):
                res["moved_aside"].append(r["moved_aside"])
        else:
            res["errors"].append(f"Save: {r.get('error')}")
    if do_extras:
        for e in info.get("extras", []):
            r = restore_extra(e["canonical"], e["target"], save_root, folder_name)
            if r["ok"]:
                db.add_save_extra_path(game_id, str(e["target"]), str(e["canonical"]))
                res["extras"].append(e["name"])
                if r.get("moved_aside"):
                    res["moved_aside"].append(r["moved_aside"])
            else:
                res["errors"].append(f"{e['name']}: {r.get('error')}")
    return res


def adopt_existing_backup(game_id: int, folder_name: str, chosen_folder, save_root) -> dict:
    """
    The old flow said "a backup already exists — overwrite it with the save you
    just picked?". This is the safe alternative: keep the backup, point the
    chosen folder at it (what the chosen folder holds now is moved aside, not
    deleted), and write the manifest so future installs restore automatically.
    """
    import db
    canonical = Path(save_root) / "PC" / folder_name
    r = restore_main(canonical, chosen_folder, save_root, folder_name)
    if r["ok"]:
        db.set_save_paths(game_id, save_path=str(canonical),
                          save_source_path=str(chosen_folder))
        write_manifest(save_root, folder_name, chosen_folder)
    return r


# ── Automatic restore at the end of an install / Locate Game ─────────────────

def _save_root_path() -> Path:
    import db
    return Path(db.get_setting(
        "save_backup_root", str(Path.home() / "Documents" / "Game Saves")))


def _has_live_data(p: Path) -> bool:
    """True if p holds real (non-symlink) content that a restore would move aside."""
    try:
        if p.is_symlink() or not p.exists():
            return False
        if p.is_dir():
            return any(p.iterdir())
        return True
    except OSError:
        return False


def auto_restore_after_install(game_id: int) -> dict:
    """
    Restore this game's backed-up save AND its _extras (achievements/stats/
    leaderboards) right after an install or Locate Game, when the destination
    is known (manifest for the main save; _extras mirror their own paths).

    Returns a dict; res["status"] is one of:
      "restored"       something was restored (see main/extras/skipped)
      "needs_confirm"  something real already sits at a destination — caller
                       should show SaveRestoreDialog with res["info"]
      "needs_placement" a main-save backup exists but has no manifest, so its
                       destination is unknown (play + save once; the normal
                       post-play flow will offer "Use My Existing Backup")
      "none" / "disabled" / "no_prefix" / "error"
    Never overwrites or deletes: anything in the way is moved to _replaced/.
    """
    import db, save_backup
    res = {"status": "none", "main": None, "extras": [], "skipped": [],
           "errors": [], "moved_aside": [], "info": None,
           "folder_name": None, "legacy_main": False}
    try:
        if db.get_setting("save_backup_enabled", "false") != "true":
            res["status"] = "disabled"
            return res
        game = db.get_game(game_id)
        wine_prefix = (game["wine_prefix"] or "").strip() if game else ""
        if not game or not wine_prefix or not Path(wine_prefix).exists():
            res["status"] = "no_prefix"
            return res
        root = Path(wine_prefix)
        actual = root / "pfx" if (root / "pfx" / "drive_c").exists() else root
        drive_c = actual / "drive_c"
        if not drive_c.exists():
            res["status"] = "no_prefix"
            return res
        proton = actual != root
        folder = game["folder_name"]
        res["folder_name"] = folder
        save_root = _save_root_path()
        res["save_root"] = str(save_root)

        linked = [e.get("canonical_path") for e in db.get_save_extra_paths(game_id)
                  if e.get("canonical_path")]
        info = find_restorable(save_root, folder, drive_c, proton, linked)
        paths = db.get_save_paths(game_id)
        if save_backup.diagnose_source_path(
                paths.get("save_source_path"), paths.get("save_path")) == "ok":
            info["main"] = None
        res["skipped"] = list(info.get("skipped_extras") or [])
        res["legacy_main"] = bool(info.get("legacy_main"))

        if not info["main"] and not info["extras"]:
            res["status"] = "needs_placement" if res["legacy_main"] else "none"
            return res

        conflict = (info["main"] and _has_live_data(Path(info["main"]["target"]))) \
            or any(_has_live_data(Path(e["target"])) for e in info["extras"])
        if conflict:
            res["status"] = "needs_confirm"
            res["info"] = info
            return res

        r = restore_all(game_id, folder, save_root, info, True, True)
        res.update(main=r["main"], extras=r["extras"],
                   errors=r["errors"], moved_aside=r["moved_aside"])
        res["status"] = "restored" if (r["main"] or r["extras"]) else "none"
        return res
    except Exception as e:
        log.warning("[SAVE RESTORE] auto restore failed for game_id=%s: %s", game_id, e)
        res["status"] = "error"
        res["errors"].append(str(e))
        return res


def describe_result(res: dict) -> str:
    """One short human-readable summary of auto_restore_after_install()'s result
    ('' when there is nothing worth telling the user)."""
    lines = []
    if res.get("main"):
        lines.append("✓ Restored your save game.")
    if res.get("extras"):
        lines.append(f"✓ Restored {len(res['extras'])} achievement/stats file(s).")
    if res.get("skipped"):
        names = ", ".join(res["skipped"][:4]) + ("…" if len(res["skipped"]) > 4 else "")
        lines.append(f"{len(res['skipped'])} backed-up file(s) couldn't be placed yet "
                     f"({names}) — their game folder doesn't exist in this prefix until "
                     "the game has run once. They'll be offered again at next launch.")
    if res.get("legacy_main") or res.get("status") == "needs_placement":
        lines.append("A save backup exists, but where it belongs wasn't recorded. "
                     "Play the game and save once — VaultPlay will then offer "
                     "“Use My Existing Backup”.")
    if res.get("status") == "declined":
        lines.append("Backup not restored (you can restore it later from the cogwheel menu).")
    if res.get("errors"):
        lines.append("⚠ " + "; ".join(res["errors"]))
    return "\n".join(lines)
