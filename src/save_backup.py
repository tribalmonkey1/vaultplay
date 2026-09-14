"""
save_backup.py — Save Backup detection & move/symlink logic for VaultPlay

Implements Flow 1 ("first play after fresh install") from the Save Backup
feature spec: snapshot the Wine prefix's drive_c before launch, diff it
after the game closes, rank the changed folders as save-location
candidates, and move the chosen folder to a canonical path with a symlink
left behind so the game keeps reading/writing normally.

Flow 2 ("subsequent plays" — is the link still good) is implemented as a
detection-only check: check_link_status() tells the caller whether an
already-linked game's symlink is still intact. Flow 3 ("prefix was
deleted and recreated — re-link automatically") is implemented for the
common case: game_state still has save_source_path/save_path recorded,
but the symlink at save_source_path is gone, replaced with a plain
folder, or points somewhere unexpected — diagnose_source_path() tells
the caller which, and repair_link() fixes the first two automatically
(a plain folder's contents are merged into the canonical backup rather
than discarded — see repair_link()'s docstring for why that's safe).
A symlink pointing somewhere unexpected is NOT auto-repaired; per spec
that's surprising enough to ask the user first (see
ui/game_detail.py's _maybe_snapshot_before_launch()).

NOT implemented: the "game_state itself was wiped" sub-case (the spec's
fallback to a vaultplay-library.json backup file, since that export/
import feature doesn't exist) and its "ask the user to make one test
save, diff, then delete just the test file" recovery flow. If a game's
save_source_path/save_path are both unset, Flow 1 just runs fresh, same
as a first install — there is no attempt to recover a prior link from
disk alone.

Cardinal rule: this module NEVER deletes or overwrites anything at the
canonical save path without explicit caller-confirmed permission.
move_and_link() only ever moves files INTO the canonical path, and raises
SaveMoveConflict instead of silently overwriting an existing backup.
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

import datetime
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


# ── Pending snapshot storage ───────────────────────────────────────────────────
# A snapshot is taken right before a game launches and written to disk
# immediately (not just kept in memory) so it survives the app being
# closed before the user responds to the post-play prompt — the prompt
# can then simply be re-shown next session using the same persisted
# snapshot, rather than losing the baseline entirely.

def _config_dir() -> Path:
    env = os.environ.get("VAULTPLAY_CONFIG_DIR")
    if env:
        return Path(env)
    p = Path.home() / ".config" / "vaultplay"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _pending_saves_dir() -> Path:
    p = _config_dir() / "pending_saves"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _safe_filename(game_folder_name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", game_folder_name)


def _pending_snapshot_path(game_folder_name: str) -> Path:
    return _pending_saves_dir() / f"{_safe_filename(game_folder_name)}.json"


def save_pending_snapshot(game_folder_name: str, snapshot: dict,
                          actual_prefix_path: Path) -> None:
    """Persist a pre-launch snapshot to disk. Never raises — a failure here
    just means the post-play diff will find nothing and do nothing, which
    is safe (no data loss, just a missed backup opportunity for this
    session)."""
    data = {
        "taken_at":    datetime.datetime.utcnow().isoformat(),
        "prefix_path": str(actual_prefix_path),
        "snapshot":    snapshot,
    }
    path = _pending_snapshot_path(game_folder_name)
    try:
        path.write_text(json.dumps(data))
    except OSError as e:
        log.warning("[SAVE BACKUP] Could not persist pending snapshot for %s: %s",
                   game_folder_name, e)


def load_pending_snapshot(game_folder_name: str) -> Optional[dict]:
    """Return the persisted {taken_at, prefix_path, snapshot} dict, or None
    if no snapshot was taken for this game (e.g. the feature was off at
    launch time, or the game isn't installed via a tracked wine_prefix)."""
    path = _pending_snapshot_path(game_folder_name)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.warning("[SAVE BACKUP] Could not read pending snapshot for %s: %s",
                   game_folder_name, e)
        return None


def delete_pending_snapshot(game_folder_name: str) -> None:
    path = _pending_snapshot_path(game_folder_name)
    try:
        if path.exists():
            path.unlink()
    except OSError as e:
        log.debug("[SAVE BACKUP] Could not delete pending snapshot for %s: %s",
                  game_folder_name, e)


# ── Snapshot & diff ────────────────────────────────────────────────────────────

def snapshot_prefix(actual_prefix_path: Path) -> dict:
    """
    Record {relative_path_str: mtime} for every file under
    actual_prefix_path/drive_c. Scoped to drive_c/ only — the other
    top-level prefix dirs (dosdevices, system.reg, etc.) never contain
    game saves and would only add noise to the diff.
    """
    snapshot: dict = {}
    drive_c = Path(actual_prefix_path) / "drive_c"
    if not drive_c.exists():
        return snapshot
    for f in drive_c.rglob("*"):
        if f.is_file():
            try:
                snapshot[str(f.relative_to(drive_c))] = f.stat().st_mtime
            except OSError:
                pass
    return snapshot


def diff_snapshot(actual_prefix_path: Path, old_snapshot: dict) -> list:
    """Return absolute Paths for files under drive_c/ that are new or have
    a newer mtime than what was recorded in old_snapshot."""
    changed = []
    drive_c = Path(actual_prefix_path) / "drive_c"
    if not drive_c.exists():
        return changed
    for f in drive_c.rglob("*"):
        if not f.is_file():
            continue
        try:
            rel   = str(f.relative_to(drive_c))
            mtime = f.stat().st_mtime
        except OSError:
            continue
        old_mtime = old_snapshot.get(rel)
        if old_mtime is None or mtime > old_mtime:
            changed.append(f)
    return changed


# ── Noise filtering ────────────────────────────────────────────────────────────
# Filters out changes that are real but never saves — shader/driver caches
# and Windows system files churn constantly during play and would otherwise
# dominate the candidate list.

_NOISE_PATH_TOKENS = (
    "shadercache", "shader_cache", "dxcache", "dx_cache",
    "gscache", "gs_cache", "vulkancache", "vulkan_cache",
    "d3dcompiler", "nvidia", "amd shader",
)
_NOISE_SUFFIXES = (".log",)


def _is_noise(path: Path, drive_c: Path) -> bool:
    try:
        rel_parts = [p.lower() for p in path.relative_to(drive_c).parts]
    except ValueError:
        return False
    if rel_parts and rel_parts[0] == "windows":
        return True
    collapsed = "".join(rel_parts)
    if any(token in collapsed for token in _NOISE_PATH_TOKENS):
        return True
    if path.suffix.lower() in _NOISE_SUFFIXES:
        return True
    return False


def filter_noise(paths: list, drive_c: Path) -> tuple:
    """
    Split paths into (kept, filtered). Filtered paths are never silently
    discarded from the log — the caller is expected to log them (see
    ui/main_window.py's _maybe_run_save_backup_flow) so a real save that
    ever gets misclassified as noise is visible for debugging, not just
    invisibly dropped.
    """
    kept, filtered = [], []
    for p in paths:
        (filtered if _is_noise(p, drive_c) else kept).append(p)
    return kept, filtered


# ── Always-Backed-Up Files (Achievements & Persistent Progress) ────────────
# Some files hold progress data that lives OUTSIDE whatever single folder
# the user picks in Flow 1 — most commonly a Goldberg-emulated Steam
# crack's achievements/stats/leaderboards files, which routinely sit in
# their own directory entirely separate from wherever the game's actual
# save data lands (confirmed real: Vampire Survivors' achievements.ini —
# see Bug History #21 — and reproduced again during Save Backup testing,
# where achievements.ini showed up as its own separate ranked candidate
# with no way to back it up alongside the real save, since the picker is
# single-select — see "One Save Folder Per Game (PC)" in the spec).
#
# Files matching this pattern are pulled OUT of the normal candidate pool
# before folders are ranked (see partition_always_backup_files()) and are
# instead detected/linked automatically and unconditionally — see
# find_extra_files() below — never competing with the main save folder
# for the single selection slot.
#
# Deliberately NOT included: settings.ini (Goldberg's local/hardware
# config — language, offline name, etc. — not progress data, and
# re-generated fresh in a new prefix anyway) and DLC/config files that
# ship with the crack itself (static, never change at runtime, so they'd
# never show up in a diff regardless).
ALWAYS_BACKUP_FILENAME_RE = re.compile(
    r"^(achievements?|stats?|leaderboards?)\.(ini|json|dat|bin)$",
    re.IGNORECASE
)


def partition_always_backup_files(changed_files: list) -> tuple:
    """
    Split a list of changed file Paths into (always_backup_files, remaining)
    based on ALWAYS_BACKUP_FILENAME_RE matching the filename alone,
    regardless of which folder it's in. Called BEFORE rank_candidate_folders()
    so these files never get folded into, or compete as, a normal
    save-folder candidate — they're handled separately (see
    find_extra_files() / move_and_link_file() below) and always backed up,
    not offered as a pick.
    """
    always_backup, remaining = [], []
    for f in changed_files:
        (always_backup if ALWAYS_BACKUP_FILENAME_RE.match(f.name) else remaining).append(f)
    return always_backup, remaining


def find_extra_files(drive_c: Path) -> list:
    """
    Scan drive_c for any file matching ALWAYS_BACKUP_FILENAME_RE, anywhere
    in the tree — no snapshot/diff needed, since these files are
    unambiguous by name alone and should be tracked the moment they exist,
    whether that's session one or session one hundred. This is what lets
    achievements get linked even for a game whose main save was already
    linked before this feature existed (Flow 1's one-time diff has already
    been consumed for the save folder, but this scan needs no diff at all).
    Cheap relative to a full snapshot diff — just a filtered rglob. Returns
    absolute Paths; may be empty. Never raises.
    """
    found = []
    if not drive_c.exists():
        return found
    try:
        for f in drive_c.rglob("*"):
            try:
                if f.is_file() and ALWAYS_BACKUP_FILENAME_RE.match(f.name):
                    found.append(f)
            except OSError:
                pass
    except Exception:
        pass
    return found


# ── Move + symlink + diagnose + repair for individual "always backup"
# files ─────────────────────────────────────────────────────────────────
# File-level equivalents of move_and_link()/diagnose_source_path()/
# repair_link() above, since achievements/stats/leaderboards are lone
# files, not folders. Same cardinal rule: canonical is only ever written
# to via an explicit move (never silently discarded), and a conflict at
# the canonical path without confirmation raises rather than overwrites.

def move_and_link_file(source_file: Path, save_root: Path, game_folder_name: str,
                       drive_c: Path, overwrite_confirmed: bool = False) -> Path:
    """
    Move a single always-backup file (e.g. achievements.ini) to
    <save_root>/_extras/PC/<game_folder_name>/<path relative to drive_c>,
    then replace it with a symlink so the game keeps reading/writing it
    from the same place. The relative-to-drive_c path is preserved
    (rather than flattening to just the filename) so two files that
    happen to share a name in different subfolders — e.g. one game with
    both a Goldberg achievements.ini AND an unrelated stats.ini in a
    different directory — never collide at the canonical path.

    IMPORTANT: this canonical location lives under its OWN top-level
    <save_root>/_extras/ tree, deliberately outside
    <save_root>/PC/<game_folder_name>/ (the main save's own canonical
    folder — see move_and_link() above). It must never be nested inside
    the main save's folder: move_and_link()'s conflict check treats ANY
    existing content under the main save's canonical folder as "a
    previous save is already backed up here", and its confirmed-overwrite
    path does a full shutil.rmtree() of that folder — which would have
    silently destroyed every tracked extra file the first time a user
    linked their main save after this feature had already backed up
    their achievements. Fixed 2026-09-13 after exactly that scenario was
    reported; see the module-level migration note on sync_extra_files().

    Raises SaveMoveConflict if the canonical file already exists and
    overwrite_confirmed is False — same guarantee move_and_link() gives
    for folders.
    """
    source_file = Path(source_file)
    try:
        rel = source_file.relative_to(drive_c)
    except ValueError:
        rel = Path(source_file.name)
    canonical = Path(save_root) / "_extras" / "PC" / game_folder_name / rel

    if canonical.exists() and not overwrite_confirmed:
        raise SaveMoveConflict(str(canonical))

    canonical.parent.mkdir(parents=True, exist_ok=True)

    if canonical.exists():
        # Only reached with overwrite_confirmed=True.
        canonical.unlink()

    shutil.move(str(source_file), str(canonical))

    if source_file.is_symlink() or source_file.exists():
        source_file.unlink()
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.symlink_to(canonical)

    log.info("[SAVE BACKUP] Extra file moved %s → %s (symlink left behind)",
             source_file, canonical)
    return canonical


def diagnose_extra_file(source_path, canonical_path) -> str:
    """
    File-level equivalent of diagnose_source_path(). Returns one of:
      "unset"             — source_path/canonical_path not recorded yet.
      "canonical_missing" — the backed-up file itself is gone (user
                             deleted it directly) — nothing to restore
                             from, leave alone.
      "ok"                — source_path is a symlink correctly pointing
                             at canonical_path.
      "missing"            — nothing at all exists at source_path (e.g.
                             right after a Wine prefix was deleted and
                             recreated). Safe to auto-repair.
      "plain_file"         — a real file exists at source_path instead of
                             a symlink (the game already wrote a fresh
                             one in the recreated prefix). Safe to
                             auto-repair — the fresh file is moved over
                             the canonical copy before symlinking, so
                             newer progress data is never discarded.
      "wrong_symlink"      — a symlink exists but resolves somewhere else
                             (including a dangling target). NOT
                             auto-repaired.
    """
    if not source_path or not canonical_path:
        return "unset"
    canonical = Path(canonical_path)
    if not canonical.exists():
        return "canonical_missing"
    source = Path(source_path)
    if source.is_symlink():
        try:
            if source.resolve() == canonical.resolve():
                return "ok"
        except OSError:
            pass
        return "wrong_symlink"
    if not source.exists():
        return "missing"
    return "plain_file"


def repair_extra_file_link(source_path: str, canonical_path: str) -> bool:
    """
    File-level equivalent of repair_link(), for the "missing" and
    "plain_file" diagnose_extra_file() states (call only after explicit
    user confirmation for "wrong_symlink", same contract as repair_link()).

    "plain_file" is handled by MOVING the fresh file over the canonical
    copy before symlinking — mirrors Flow 2's "move file to canonical,
    replace with symlink silently" contract for the main save: newer
    progress data always wins, nothing is silently discarded. There's no
    merge step (unlike a save folder) since this is a single file with
    nothing else to preserve alongside it.

    Returns True on success.
    """
    source = Path(source_path)
    canonical = Path(canonical_path)
    try:
        if source.exists() and not source.is_symlink():
            canonical.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(canonical))
        elif source.is_symlink():
            source.unlink()
        source.parent.mkdir(parents=True, exist_ok=True)
        if not source.exists() and not source.is_symlink():
            source.symlink_to(canonical)
        log.info("[SAVE BACKUP] Extra file relinked %s → %s", source, canonical)
        return True
    except Exception as e:
        log.error("[SAVE BACKUP] repair_extra_file_link failed for %s → %s: %s",
                  source, canonical, e)
        return False


# ── One-time migration: legacy nested _extras/ location ─────────────────────
# The very first version of this feature (2026-09-12) put the canonical
# extras location at <save_root>/PC/<game_folder_name>/_extras/... — NESTED
# inside the main save's own canonical folder. That was a real bug, not
# just untidy: move_and_link() (used by the main save flow) treats ANY
# existing content under <save_root>/PC/<game_folder_name>/ as "a previous
# save is already backed up here" and raises SaveMoveConflict — so a user
# who had achievements auto-linked before ever linking their main save
# would see a false "a backed-up save already exists" warning the first
# time they tried to. Worse: if they confirmed the overwrite, move_and_link()
# does a full shutil.rmtree() of that folder, which would have silently
# destroyed every already-linked achievements/stats/leaderboards file along
# with it. Reported and fixed 2026-09-13 — see Bug History.
#
# Fixed going forward by move_and_link_file() writing to a completely
# separate <save_root>/_extras/PC/<game_folder_name>/... tree instead (see
# its docstring). This function transparently relocates anything already
# linked at the old nested location the first time sync_extra_files() sees
# it again, so an affected install self-heals on its next play session or
# manual "Back Up Save Now" click — no separate migration script needed.

def _migrate_legacy_extra_canonical(canonical_path: Path, source_path: str,
                                    save_root: Path, game_folder_name: str) -> Path:
    """
    If canonical_path points at the old, buggy nested location, physically
    move the file to the new <save_root>/_extras/PC/... location and
    recreate the in-prefix symlink to point at it (the old symlink would
    otherwise be left dangling, pointing at a path that no longer exists).
    Returns the (possibly unchanged) canonical Path the caller should use
    from here on. Never raises — a failed migration just leaves the entry
    at its old location for a future attempt, logged as a warning.
    """
    old_root = Path(save_root) / "PC" / game_folder_name / "_extras"
    try:
        rel = canonical_path.relative_to(old_root)
    except ValueError:
        return canonical_path   # already new-style, or something else entirely

    new_canonical = Path(save_root) / "_extras" / "PC" / game_folder_name / rel
    try:
        if canonical_path.exists() and not new_canonical.exists():
            new_canonical.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(canonical_path), str(new_canonical))
            log.info("[SAVE BACKUP] Migrated legacy extra-file canonical "
                     "path %s → %s", canonical_path, new_canonical)

        if source_path:
            source = Path(source_path)
            if source.is_symlink() or source.exists():
                source.unlink()
            source.parent.mkdir(parents=True, exist_ok=True)
            source.symlink_to(new_canonical)
    except Exception as e:
        log.warning("[SAVE BACKUP] Legacy extra-file migration failed for "
                   "%s → %s: %s", canonical_path, new_canonical, e)
        return canonical_path

    return new_canonical


def _cleanup_legacy_extras_root(save_root: Path, game_folder_name: str):
    """
    Best-effort removal of the old <save_root>/PC/<game_folder_name>/_extras/
    directory once every entry that lived under it has been migrated —
    only removes it if it's now empty of files (a leftover empty directory
    tree), never a forced/recursive delete of anything unexpected. This is
    what clears move_and_link()'s false-conflict check for good, rather
    than just leaving an empty _extras/ folder that still makes
    `any(canonical.iterdir())` true.
    """
    old_root = Path(save_root) / "PC" / game_folder_name / "_extras"
    if not old_root.exists():
        return
    try:
        if any(p.is_file() for p in old_root.rglob("*")):
            return   # something un-migrated is still in there — leave it
        shutil.rmtree(old_root, ignore_errors=True)
        log.info("[SAVE BACKUP] Cleaned up empty legacy _extras/ folder "
                 "under %s", old_root.parent)
    except Exception as e:
        log.debug("[SAVE BACKUP] Legacy _extras/ cleanup skipped for %s: %s",
                 old_root, e)


# ── Shared orchestration: scan + link + repair, one call site ──────────────
# The actual DB-touching orchestration for Achievements/Stats/Leaderboards
# Auto-Backup lives HERE (not duplicated in ui/main_window.py and
# ui/cogwheel_menu.py) specifically so the automatic post-session flow
# (MainWindow._on_session_ended → _sync_save_extras()) and the manual
# "Back Up Save Now" trigger (CogwheelButton._manual_backup_save) always
# behave identically — same detection, same repair rules, same canonical
# location — no matter which of those two moments triggered it. This is
# the one function in this module that imports db, since there's no way
# to track "what's already linked" without it; every other function above
# stays a pure Path-in/Path-out helper.

def sync_extra_files(game_id: int) -> list:
    """
    Scan this game's Wine prefix for achievements/stats/leaderboards files
    (see find_extra_files()) and link/repair any that aren't already
    correctly linked. No dialog, no snapshot/diff needed — scans the
    CURRENT state of drive_c directly, so this also works retroactively
    for a game whose main save was already linked before this feature
    existed.

    Returns the list of canonical Paths newly linked by THIS call (empty
    if nothing new was found, the feature is disabled in Settings, or the
    prefix can't be resolved). Never raises — callers can call this
    fire-and-forget from either a background post-session flow or a
    direct UI button click.

    Also transparently migrates any extra file still linked at the old,
    buggy nested canonical location from before 2026-09-13 (see
    _migrate_legacy_extra_canonical()) — this runs unconditionally on
    every call, independent of whether the scan below finds anything new.
    """
    import db

    if db.get_setting("save_backup_enabled", "false") != "true":
        return []

    game = db.get_game(game_id)
    if not game:
        return []

    wine_prefix = (game["wine_prefix"] or "").strip()
    if not wine_prefix or not Path(wine_prefix).exists():
        return []

    try:
        import installer as install_mod
        wine_bin = install_mod.parse_wine_bin_from_cmd(game["launch_cmd"] or "")
        actual_prefix = install_mod._resolve_actual_prefix(Path(wine_prefix), wine_bin)
        drive_c = actual_prefix / "drive_c"

        existing   = db.get_save_extra_paths(game_id)
        save_root  = Path(db.get_setting(
            "save_backup_root", str(Path.home() / "Documents" / "Game Saves")))

        # One-time migration for anything still linked at the old, buggy
        # nested location — see _migrate_legacy_extra_canonical()'s
        # docstring. Runs BEFORE the scan-based early-return below (a
        # record's symlink target may already be broken at the old
        # location, in which case find_extra_files() below won't discover
        # it at all — migration must not depend on that scan succeeding)
        # and before diagnose_extra_file() further down, so diagnosis is
        # always checked against the correct, current canonical path.
        migrated = False
        for entry in existing:
            old_canonical = entry.get("canonical_path")
            if not old_canonical:
                continue
            new_canonical = _migrate_legacy_extra_canonical(
                Path(old_canonical), entry.get("source_path"),
                save_root, game["folder_name"])
            if str(new_canonical) != old_canonical:
                entry["canonical_path"] = str(new_canonical)
                db.add_save_extra_path(game_id, entry.get("source_path"), str(new_canonical))
                migrated = True
        if migrated:
            _cleanup_legacy_extras_root(save_root, game["folder_name"])

        found = find_extra_files(drive_c)
        if not found:
            return []

        linked_now = []

        for f in found:
            record = next((e for e in existing if e.get("source_path") == str(f)), None)

            if record:
                diag = diagnose_extra_file(
                    record.get("source_path"), record.get("canonical_path"))
                if diag == "ok":
                    continue
                if diag in ("missing", "plain_file"):
                    if repair_extra_file_link(
                            record["source_path"], record["canonical_path"]):
                        continue
                    # Repair failed — fall through and try a fresh link below.
                elif diag == "wrong_symlink":
                    # Surprising enough to leave alone automatically — same
                    # caution repair_link() uses for the main save's
                    # folder-level equivalent. No per-file UI exists yet to
                    # ask the user about it.
                    continue
                elif diag == "canonical_missing":
                    continue  # user deleted their own backup — leave it alone

            # Not tracked yet at all, or its repair above failed — do the
            # initial move+symlink now.
            try:
                canonical = move_and_link_file(f, save_root, game["folder_name"], drive_c)
                db.add_save_extra_path(game_id, str(f), str(canonical))
                linked_now.append(canonical)
            except SaveMoveConflict:
                log.warning(
                    "[SAVE BACKUP] Extra file conflict at canonical path for "
                    "%s (game_id=%d) — leaving unlinked rather than silently "
                    "overwriting", f, game_id)
            except Exception as e:
                log.warning("[SAVE BACKUP] Could not link extra file %s "
                           "(game_id=%d): %s", f, game_id, e)

        if linked_now:
            names = ", ".join(p.name for p in linked_now)
            log.info("[SAVE BACKUP] Auto-linked %d extra file(s) for "
                     "game_id=%d: %s", len(linked_now), game_id, names)
        return linked_now
    except Exception as e:
        log.warning("[SAVE BACKUP] Extras sync failed for game_id=%d: %s",
                   game_id, e)
        return []


# ── Known-location fast path ──────────────────────────────────────────────────
# Checked before falling back to a full diff — if the game's save folder
# is sitting in one of these conventional spots, there's no need to make
# the user pick from a diff-derived candidate list at all.

_KNOWN_LOCATION_GLOBS = [
    "AppData/LocalLow/*/*",
    "AppData/Local/*/*",
    "AppData/Roaming/*/*",
    "Saved Games/*",
    "Documents/My Games/*",
]
_PUBLIC_ONLY_GLOBS = ["Documents/*/*"]


def scan_known_locations(actual_prefix_path: Path, game_display_name: str) -> list:
    """
    Fast-path scan across every user directory under drive_c/users/ (not
    hardcoded to one username — covers <username>, Public, steamuser, and
    anything else present) for a folder whose name looks like this game.
    Returns a list of matching Path candidates (may be empty) — no
    ranking needed here, these are already high-confidence by construction.
    """
    users_dir = Path(actual_prefix_path) / "drive_c" / "users"
    if not users_dir.exists():
        return []

    name_key = re.sub(r"[^a-z0-9]", "", game_display_name.lower())
    if not name_key:
        return []

    matches = []
    try:
        user_dirs = [d for d in users_dir.iterdir() if d.is_dir()]
    except OSError:
        return []

    for user_dir in user_dirs:
        globs = list(_KNOWN_LOCATION_GLOBS)
        if user_dir.name.lower() == "public":
            globs = globs + _PUBLIC_ONLY_GLOBS
        for pattern in globs:
            try:
                for candidate in user_dir.glob(pattern):
                    if not candidate.is_dir():
                        continue
                    candidate_key = re.sub(r"[^a-z0-9]", "", candidate.name.lower())
                    if candidate_key and (name_key in candidate_key or candidate_key in name_key):
                        matches.append(candidate)
            except OSError:
                continue

    seen, unique = set(), []
    for m in matches:
        s = str(m)
        if s not in seen:
            seen.add(s)
            unique.append(m)
    return unique


def candidates_from_known_locations(matches: list) -> list:
    """Build ranked-dialog-shaped candidate dicts from known-location matches."""
    results = []
    for folder in matches:
        try:
            files = [f for f in folder.rglob("*") if f.is_file()]
        except OSError:
            files = []
        results.append({
            "path":         folder,
            "file_count":   len(files),
            "sample_files": [f.name for f in files[:5]],
            "score":        10,   # known-location matches are highest confidence
        })
    results.sort(key=lambda r: -r["file_count"])
    return results


# ── Ranking (diff fallback path) ──────────────────────────────────────────────

_CRACKER_NAMES   = ("empress", "goldberg", "onlinefix", "hoodlum")
_SAVE_EXTENSIONS = (".sav", ".dat", ".bin")


def rank_candidate_folders(changed_files: list, drive_c: Path,
                           game_display_name: str) -> list:
    """
    Group changed files by containing directory and score each group.
    Higher score = more likely to be the actual save folder. Scoring
    signals: folder path contains the game's own name, a known cracker
    name (EMPRESS, Goldberg, OnlineFix, HOODLUM), or the group contains a
    file with a save-like extension (.sav/.dat/.bin).
    Returns candidate dicts sorted by score, then file count, descending.
    """
    groups: dict = {}
    for f in changed_files:
        groups.setdefault(f.parent, []).append(f)

    name_key = re.sub(r"[^a-z0-9]", "", game_display_name.lower())
    results = []
    for folder, files in groups.items():
        score = 0
        folder_key = re.sub(r"[^a-z0-9]", "", str(folder).lower())
        if name_key and name_key in folder_key:
            score += 5
        if any(cracker in folder_key for cracker in _CRACKER_NAMES):
            score += 3
        if any(f.suffix.lower() in _SAVE_EXTENSIONS for f in files):
            score += 2
        results.append({
            "path":         folder,
            "file_count":   len(files),
            "sample_files": [f.name for f in files[:5]],
            "score":        score,
        })
    results.sort(key=lambda r: (-r["score"], -r["file_count"]))
    return results


# ── Move + symlink ────────────────────────────────────────────────────────────

class SaveMoveConflict(Exception):
    """
    Raised by move_and_link() when the canonical save path already has
    files in it and overwrite_confirmed was not set — the caller must
    warn the user and re-call with overwrite_confirmed=True. VaultPlay
    never silently overwrites a previously backed-up save.
    """
    def __init__(self, canonical_path: str):
        super().__init__(canonical_path)
        self.canonical_path = canonical_path


def move_and_link(source_folder: Path, save_root: Path, game_folder_name: str,
                  overwrite_confirmed: bool = False) -> Path:
    """
    Move source_folder's contents to the canonical save path
    (<save_root>/PC/<game_folder_name>/), then replace source_folder with
    a symlink pointing at the canonical path so the game keeps reading
    and writing saves from the same location it always has.

    Raises SaveMoveConflict if the canonical path already contains files
    and overwrite_confirmed is False.

    Cardinal rule (see module docstring): only ever moves files INTO the
    canonical path. The canonical path is never deleted or touched outside
    of an explicitly-confirmed overwrite.
    """
    source_folder = Path(source_folder)
    canonical = Path(save_root) / "PC" / game_folder_name

    if canonical.exists() and any(canonical.iterdir()) and not overwrite_confirmed:
        raise SaveMoveConflict(str(canonical))

    canonical.parent.mkdir(parents=True, exist_ok=True)

    if canonical.exists():
        # Only reached with overwrite_confirmed=True — the caller already
        # obtained explicit user confirmation before this point.
        shutil.rmtree(canonical)

    shutil.move(str(source_folder), str(canonical))

    # Replace the original location with a symlink to the canonical path
    if source_folder.is_symlink() or source_folder.is_file():
        source_folder.unlink()
    elif source_folder.exists():
        shutil.rmtree(source_folder)
    source_folder.parent.mkdir(parents=True, exist_ok=True)
    source_folder.symlink_to(canonical, target_is_directory=True)

    log.info("[SAVE BACKUP] Moved %s → %s (symlink left behind)",
             source_folder, canonical)
    return canonical


def symlink_points_to(source_path: Path, canonical_path: Path) -> bool:
    """
    True if source_path is a symlink resolving to canonical_path.
    """
    try:
        return (Path(source_path).is_symlink()
                and Path(source_path).resolve() == Path(canonical_path).resolve())
    except OSError:
        return False


def diagnose_source_path(save_source_path: Optional[str],
                         save_path: Optional[str]) -> str:
    """
    Fine-grained diagnosis of a linked game's source path. Used by Flow 3
    to decide whether a broken link can be auto-repaired or needs to ask
    the user first. Returns one of:

      "unset"             — not linked yet (Flow 1 territory).
      "canonical_missing" — save_path itself no longer exists on disk
                             (e.g. the user deleted the backup folder
                             directly). Nothing to restore from — per
                             spec this means "launch normally," not a
                             fresh Flow 1 re-detection.
      "ok"                — save_source_path is a symlink correctly
                             pointing at save_path. Nothing to do.
      "missing"            — nothing at all exists at save_source_path.
                             Most common right after a Wine prefix is
                             deleted and recreated, before the game has
                             run again in the new prefix. Safe to
                             auto-repair (see repair_link()).
      "plain_folder"       — a real file/folder exists at save_source_path
                             instead of a symlink — the game already ran
                             once in the recreated prefix and wrote a
                             fresh, unlinked save there. Safe to
                             auto-repair; its contents are merged into
                             the canonical backup rather than discarded.
      "wrong_symlink"      — a symlink exists at save_source_path but
                             resolves somewhere other than save_path
                             (including a dangling symlink to a target
                             that no longer exists). NOT auto-repaired —
                             per spec this is surprising enough to ask
                             the user before touching it.
    """
    if not save_source_path or not save_path:
        return "unset"

    canonical = Path(save_path)
    if not canonical.exists():
        return "canonical_missing"

    source = Path(save_source_path)
    if source.is_symlink():
        try:
            if source.resolve() == canonical.resolve():
                return "ok"
        except OSError:
            pass
        return "wrong_symlink"

    if not source.exists():
        return "missing"

    return "plain_folder"


def check_link_status(save_source_path: Optional[str],
                      save_path: Optional[str]) -> str:
    """
    Flow 2 — cheap sanity check for display purposes (e.g. the Save
    Backup row on the game detail page). A simplified three-state view
    over diagnose_source_path(): "unset", "ok", or "broken" (collapsing
    canonical_missing/missing/plain_folder/wrong_symlink, all of which
    read as "something's wrong" at this level of detail). For the
    fine-grained states Flow 3 needs to decide how to react, call
    diagnose_source_path() directly instead.
    """
    diag = diagnose_source_path(save_source_path, save_path)
    if diag in ("unset", "ok"):
        return diag
    return "broken"


def current_symlink_target(path: str) -> Optional[str]:
    """
    Return the resolved target of a symlink at path, or None if path
    isn't a symlink. Used only for building a clear message when asking
    the user about a "wrong_symlink" state — never used for logic.
    """
    p = Path(path)
    if not p.is_symlink():
        return None
    try:
        return str(p.resolve())
    except OSError:
        return None


def merge_into_canonical(source_folder: Path, canonical_path: Path) -> dict:
    """
    Copy source_folder's files into canonical_path, overwriting any file
    at the same relative path but never deleting anything already in
    canonical_path that this merge isn't itself replacing. Then removes
    source_folder (repair_link() replaces it with a symlink afterward).

    Used by repair_link() for the "plain_folder" Flow 3 case: an
    already-linked game whose in-prefix copy became a real folder again
    (most commonly because the Wine prefix was deleted and recreated,
    and the game wrote a fresh default save there). This is different
    from move_and_link()'s first-link conflict handling — canonical
    already legitimately holds this game's data, so folding in whatever
    the game just wrote is a normal resync, not a surprising conflict
    requiring confirmation, the same way a normal linked play session
    naturally overwrites older save files with newer ones.

    Returns {"new_files": [...], "overwritten_files": [...]} — relative
    paths, so the caller can log exactly what happened. overwritten_files
    is specifically the list of pre-existing canonical files that got
    replaced — the one thing in this whole module that can actually look
    like data loss if the incoming version turns out to be worse than
    what was already backed up, so it needs to be visible, not silent.
    """
    canonical_path.mkdir(parents=True, exist_ok=True)
    new_files = []
    overwritten_files = []
    for item in source_folder.rglob("*"):
        if item.is_file():
            rel = item.relative_to(source_folder)
            dest = canonical_path / rel
            if dest.exists():
                overwritten_files.append(str(rel))
            else:
                new_files.append(str(rel))
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(item), str(dest))
    shutil.rmtree(source_folder)
    return {"new_files": new_files, "overwritten_files": overwritten_files}


def repair_link(save_source_path: str, save_path: str) -> bool:
    """
    Flow 3 — recreate the symlink at save_source_path pointing to
    save_path. Handles all three repairable diagnose_source_path()
    states:
      "missing"       — nothing there, just create the symlink. Never
                        touches canonical.
      "plain_folder"  — merge its contents into canonical first (see
                        merge_into_canonical()), then symlink. The ONLY
                        one of these three that can overwrite existing
                        canonical files — logged explicitly by name so
                        it's never ambiguous after the fact which repair
                        path actually ran.
      "wrong_symlink" — remove the existing (mis-pointed or dangling)
                        symlink, then create a fresh one. Only call this
                        for "wrong_symlink" after the user has explicitly
                        confirmed — this function itself doesn't ask.

    Never touches save_path except to add/update files into it (via
    merge_into_canonical) — same cardinal rule as move_and_link().
    Returns True on success, False if anything went wrong (the caller
    should fall back to surfacing a broken-link warning rather than
    assuming the launch will now find the right save).
    """
    source = Path(save_source_path)
    canonical = Path(save_path)
    try:
        merge_summary = None
        if source.exists() and not source.is_symlink():
            merge_summary = merge_into_canonical(source, canonical)
        if source.exists() or source.is_symlink():
            source.unlink()
        source.parent.mkdir(parents=True, exist_ok=True)
        source.symlink_to(canonical, target_is_directory=True)

        if merge_summary is not None:
            log.info(
                "[SAVE BACKUP] Flow 3: relinked %s → %s "
                "(plain_folder repair — merged %d new file(s), "
                "OVERWROTE %d existing canonical file(s)%s)",
                source, canonical,
                len(merge_summary["new_files"]),
                len(merge_summary["overwritten_files"]),
                (": " + ", ".join(merge_summary["overwritten_files"])
                 if merge_summary["overwritten_files"] else ""))
        else:
            log.info(
                "[SAVE BACKUP] Flow 3: relinked %s → %s "
                "(missing repair — nothing existed there, no canonical "
                "files touched)", source, canonical)
        return True
    except Exception as e:
        log.error("[SAVE BACKUP] Flow 3: repair_link failed for %s → %s: %s",
                  source, canonical, e)
        return False
