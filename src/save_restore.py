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
    user = detect_user(drive_c, proton)
    out = {"main": None, "extras": [], "legacy_main": False}

    canonical = save_root / "PC" / folder_name
    if canonical.is_dir():
        files, newest = _count_files(canonical)
        if files:
            man = read_manifest(save_root, folder_name)
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
