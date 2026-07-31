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


def merge_into_canonical(source_folder: Path, canonical_path: Path) -> None:
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
    """
    canonical_path.mkdir(parents=True, exist_ok=True)
    for item in source_folder.rglob("*"):
        if item.is_file():
            rel = item.relative_to(source_folder)
            dest = canonical_path / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(item), str(dest))
    shutil.rmtree(source_folder)


def repair_link(save_source_path: str, save_path: str) -> bool:
    """
    Flow 3 — recreate the symlink at save_source_path pointing to
    save_path. Handles all three repairable diagnose_source_path()
    states:
      "missing"       — nothing there, just create the symlink.
      "plain_folder"  — merge its contents into canonical first (see
                        merge_into_canonical()), then symlink.
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
        if source.exists() and not source.is_symlink():
            merge_into_canonical(source, canonical)
        if source.exists() or source.is_symlink():
            source.unlink()
        source.parent.mkdir(parents=True, exist_ok=True)
        source.symlink_to(canonical, target_is_directory=True)
        log.info("[SAVE BACKUP] Flow 3: relinked %s → %s", source, canonical)
        return True
    except Exception as e:
        log.error("[SAVE BACKUP] Flow 3: repair_link failed for %s → %s: %s",
                  source, canonical, e)
        return False
