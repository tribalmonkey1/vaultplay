"""
wishlist_store.py — Shared NAS-file wishlist storage for VaultPlay

Spec: Notion → Features → Fully Planned → Wishlist.

Wishlist data does NOT live in vaultplay.db. SQLite (even in WAL mode,
which db.py already uses locally) is unsafe over SMB/NFS network shares —
locking doesn't work reliably there, and two machines writing at once
risks corrupting the file. Instead the wishlist is a JSON file stored on
a shared NAS folder, following the same "flat file on disk, atomic write"
pattern redists.py already uses for redists.json.

Layout:
    <wishlist_path>/
        wishlist.json          # the data itself
        wishlist_art/          # uploaded cover images
            <uuid>.<ext>
        wishlist_backups/      # automatic point-in-time copies of wishlist.json
            wishlist-<timestamp>.json

wishlist.json shape:
    {
        "version": 1,
        "items": [
            {
                "id":           "<uuid4 hex>",   # not autoincrement — two
                                                  # machines adding within
                                                  # the same second can't
                                                  # collide
                "title":        str,
                "sort_order":   int,
                "release_date": str,              # freetext, may be blank
                "notes":        str,
                "cover_url":    str,               # URL, or a path RELATIVE
                                                    # to wishlist_path for
                                                    # uploaded art — never
                                                    # absolute, since each
                                                    # machine may mount the
                                                    # shared folder at a
                                                    # different location
                "youtube_url":  str,               # legacy manual trailer
                                                    # link field — superseded
                                                    # by trailer_url/
                                                    # trailer_source/
                                                    # trailer_manual_override
                                                    # below; kept only so
                                                    # existing files still
                                                    # parse. See load()'s
                                                    # backfill shim.
                "trailer_url":              str,   # playable/external video
                                                     # reference (any source)
                "trailer_source":           str,   # 'steam' | 'gog' |
                                                     # 'youtube' | 'manual'
                "trailer_manual_override":  bool,  # True once the user has
                                                     # explicitly set/pinned
                                                     # a trailer — reserved
                                                     # for when automatic
                                                     # detection exists for
                                                     # wishlist items too;
                                                     # today every trailer on
                                                     # a wishlist item is
                                                     # manual, so this is
                                                     # always True whenever
                                                     # trailer_url is set.
                "developer":    str,               # optional, freetext
                "publisher":    str,               # optional, freetext
                "genres":       list[str],         # optional, freetext CSV
                                                    # split into a list at
                                                    # save time
                "created_at":   ISO8601 str,
            }
        ],
        "ignored_matches": [
            # keyed on folder_name (the stable, NAS-derived identity
            # games.folder_name already uses), NOT game_id — a local
            # SQLite autoincrement id is meaningless on another machine's
            # own vaultplay.db.
            {"wishlist_id": str, "folder_name": str}
        ]
    }

Write safety
------------
Every write (add/edit/delete/reorder) re-reads wishlist.json fresh
immediately beforehand and applies just that one change on top of the
latest on-disk state, rather than writing back a whole in-memory copy
that might be stale (_read_modify_write() below). Writes themselves go to
a temp file then os.replace() over the real one — never a partial/torn
write, even if interrupted. This isn't distributed-lock-grade safety —
there's no real cross-machine file lock, because that's not reliable over
SMB/NFS either — but it keeps two near-simultaneous edits from silently
clobbering each other in the common case, the same "good enough for two
people" bar redists.json import/export already operates at.

Backups
-------
Right before every write replaces wishlist.json, the file as it was is
copied to wishlist_backups/ (skipped when identical to the newest backup).
The newest BACKUPS_KEEP_RECENT copies are always kept; older ones are
thinned to one per day for BACKUPS_KEEP_DAYS days. get_deleted_from_backups()
diffs backups against the live wishlist and restore_items() puts chosen
items back — see ui/wishlist_restore_dialog.py.

WishlistView is responsible for polling get_mtime() every few seconds
while it's the visible page and reloading if it changed, so an addition
from another machine shows up without restarting the app — this module
itself does no polling or caching of its own; every read hits disk fresh.
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
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

import db

log = logging.getLogger(__name__)

WISHLIST_FILENAME = "wishlist.json"
ART_SUBDIR         = "wishlist_art"
BACKUP_SUBDIR      = "wishlist_backups"
CURRENT_VERSION    = 1
BACKUPS_KEEP_RECENT = 40
BACKUPS_KEEP_DAYS   = 60


# ── Path helpers ────────────────────────────────────────────────────────────

def get_wishlist_path() -> str:
    return db.get_setting("wishlist_path", "")


def is_configured() -> bool:
    return bool(get_wishlist_path().strip())


def _wishlist_root() -> Optional[Path]:
    raw = get_wishlist_path().strip()
    if not raw:
        return None
    return Path(_os.path.expanduser(raw))


def _wishlist_file() -> Optional[Path]:
    root = _wishlist_root()
    return (root / WISHLIST_FILENAME) if root else None


def get_art_dir() -> Optional[Path]:
    """Return <wishlist_path>/wishlist_art/, creating it if needed. None
    if wishlist_path isn't configured."""
    root = _wishlist_root()
    if not root:
        return None
    d = root / ART_SUBDIR
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("wishlist_store: could not create art dir %s: %s", d, e)
    return d


def location_status() -> str:
    """
    'unconfigured' | 'missing' | 'ok' — for the Wishlist page's inline
    status message. 'missing' means wishlist_path is set but the folder
    doesn't currently exist/isn't reachable (e.g. NAS unmounted) —
    distinct from 'unconfigured' (never set at all), mirroring
    scanner.scan_nas()'s treatment of an unconfigured nas_path.
    """
    root = _wishlist_root()
    if root is None:
        return "unconfigured"
    if not root.exists() or not root.is_dir():
        return "missing"
    return "ok"


# ── Load / save ──────────────────────────────────────────────────────────────

def _empty_data() -> dict:
    return {"version": CURRENT_VERSION, "items": [], "ignored_matches": []}


def _backfill_item_defaults(item: dict) -> dict:
    """
    Normalize a loaded item dict in place (returns it for convenience) so
    every caller — old files and new alike — can read the same set of
    keys without needing to know which schema version wrote them.

    Trailer field migration: earlier versions of this file only ever
    wrote `youtube_url`. Every item is read through here, so a legacy
    `youtube_url` value is copied into the new trailer_url/trailer_source/
    trailer_manual_override triplet the first time it's loaded — purely
    in memory, nothing is rewritten on disk until this item is next
    explicitly saved. `youtube_url` itself is left untouched (dead key,
    harmless) rather than deleted, so nothing breaks if an older build of
    the app ever reads this same file.
    """
    item.setdefault("title", "")
    item.setdefault("sort_order", 0)
    item.setdefault("release_date", "")
    item.setdefault("notes", "")
    item.setdefault("cover_url", "")
    item.setdefault("youtube_url", "")
    item.setdefault("developer", "")
    item.setdefault("publisher", "")
    item.setdefault("genres", [])
    if not isinstance(item.get("genres"), list):
        item["genres"] = []
    item.setdefault("created_at", "")

    if not item.get("trailer_url") and item.get("youtube_url"):
        item["trailer_url"] = item["youtube_url"]
        item.setdefault("trailer_source", "manual")
        item["trailer_manual_override"] = True
    else:
        item.setdefault("trailer_url", "")
        item.setdefault("trailer_source", "")
        item.setdefault("trailer_manual_override", False)

    return item


def _parse_data(data) -> dict:
    """Normalize a decoded JSON document into the standard structure."""
    if not isinstance(data, dict):
        return _empty_data()
    data.setdefault("version", CURRENT_VERSION)
    data.setdefault("items", [])
    data.setdefault("ignored_matches", [])
    if not isinstance(data["items"], list):
        data["items"] = []
    if not isinstance(data["ignored_matches"], list):
        data["ignored_matches"] = []
    data["items"] = [_backfill_item_defaults(i) for i in data["items"]
                      if isinstance(i, dict)]
    return data


def load() -> dict:
    """
    Load wishlist.json fresh from disk. Returns an empty structure if the
    file doesn't exist yet, wishlist_path isn't configured, or the file is
    unreadable/corrupt — never raises. Always reads from disk, no caching,
    since this may be read from a share another machine just wrote to.
    """
    path = _wishlist_file()
    if not path or not path.exists():
        return _empty_data()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            log.warning("wishlist_store: %s did not contain a JSON object — "
                       "treating as empty", path)
            return _empty_data()
        return _parse_data(data)
    except Exception as e:
        log.warning("wishlist_store: could not load %s: %s", path, e)
        return _empty_data()


# ── Backups ──────────────────────────────────────────────────────────────────

def _backup_dir() -> Optional[Path]:
    root = _wishlist_root()
    return (root / BACKUP_SUBDIR) if root else None


def _backup_files() -> list:
    d = _backup_dir()
    if not d or not d.is_dir():
        return []
    try:
        return sorted(d.glob("wishlist-*.json"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []


def _snapshot_before_write(live: Path):
    """Copy the current wishlist.json into the backups folder just before
    it's replaced. Never raises — a failed backup must not block a save."""
    try:
        if not live.exists() or live.stat().st_size == 0:
            return
        d = _backup_dir()
        if d is None:
            return
        d.mkdir(parents=True, exist_ok=True)
        existing = _backup_files()
        content = live.read_bytes()
        if existing:
            try:
                if existing[0].read_bytes() == content:
                    return
            except OSError:
                pass
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = d / f"wishlist-{stamp}-{int(time.time() * 1000) % 1000:03d}.json"
        dest.write_bytes(content)
        _rotate_backups()
    except Exception as e:
        log.warning("wishlist_store: backup before write failed: %s", e)


def _rotate_backups():
    files = _backup_files()   # newest first
    keep = set(files[:BACKUPS_KEEP_RECENT])
    cutoff = time.time() - BACKUPS_KEEP_DAYS * 86400
    seen_days = set()
    for p in files[BACKUPS_KEEP_RECENT:]:
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        if m < cutoff:
            continue
        day = time.strftime("%Y%m%d", time.localtime(m))
        if day in seen_days:
            continue
        seen_days.add(day)
        keep.add(p)
    for p in files:
        if p not in keep:
            try:
                p.unlink()
            except OSError:
                pass


def list_backups() -> list:
    """Newest first: [{"path", "mtime", "count"}] (count = items in it)."""
    out = []
    for p in _backup_files():
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = _parse_data(json.load(f))
            out.append({"path": str(p), "mtime": p.stat().st_mtime,
                        "count": len(data["items"])})
        except Exception:
            continue
    return out


def load_backup(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return _parse_data(json.load(f))
    except Exception as e:
        log.warning("wishlist_store: could not read backup %s: %s", path, e)
        return _empty_data()


def get_deleted_from_backups(backup_path: Optional[str] = None) -> list:
    """
    Diff backups against the live wishlist: items that exist in a backup but
    not (by id) in the current wishlist. With backup_path, diff that single
    backup; with None, combine every backup (newest copy of each item wins).

    Returns [{"item": dict, "backup_mtime": float}] ordered by when the
    containing backup was taken (newest first), then by title.
    """
    current_ids = {i.get("id") for i in load()["items"]}
    found = {}
    paths = [Path(backup_path)] if backup_path else _backup_files()
    for p in paths:   # newest first, so the first sighting is the newest copy
        data = load_backup(str(p))
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        for item in data["items"]:
            iid = item.get("id")
            if iid and iid not in current_ids and iid not in found:
                found[iid] = {"item": item, "backup_mtime": mtime}
    return sorted(found.values(),
                  key=lambda r: (-r["backup_mtime"], r["item"].get("title", "").lower()))


def restore_items(items: list) -> int:
    """Put items (dicts from get_deleted_from_backups) back, keeping their
    original ids and details, appended to the bottom of the priority list.
    Items whose id is already present are skipped. Returns the number
    restored."""
    restored = {"n": 0}

    def _apply(data):
        existing_ids = {i.get("id") for i in data["items"]}
        order = max((i.get("sort_order", 0) for i in data["items"]), default=-1)
        for item in items:
            if not item.get("id") or item["id"] in existing_ids:
                continue
            order += 1
            new_item = dict(item)
            new_item["sort_order"] = order
            data["items"].append(new_item)
            existing_ids.add(item["id"])
            restored["n"] += 1

    if not items:
        return 0
    result = _read_modify_write(_apply)
    return restored["n"] if result is not None else 0


def _save(data: dict) -> bool:
    """Atomic write: temp file + os.replace(). Returns True on success,
    False if wishlist_path isn't configured or the write failed for any
    reason (never raises). The previous file is snapshotted into
    wishlist_backups/ first."""
    root = _wishlist_root()
    if not root:
        log.warning("wishlist_store: wishlist_path not configured — cannot save")
        return False
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.error("wishlist_store: could not create %s: %s", root, e)
        return False

    path = root / WISHLIST_FILENAME
    tmp  = path.with_suffix(".tmp")
    _snapshot_before_write(path)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp.replace(path)   # atomic on POSIX — matches redists.py's save_redists()
        return True
    except Exception as e:
        log.error("wishlist_store: save failed for %s: %s", path, e)
        try:
            tmp.unlink()
        except Exception:
            pass
        return False


def _read_modify_write(fn: Callable[[dict], None]) -> Optional[dict]:
    """
    Read-fresh-then-patch (see module docstring's Write safety section):
    reload wishlist.json from disk right now, apply fn(data) in place,
    save, and return the saved data — or None if the save failed (e.g.
    wishlist_path unset/unreachable), in which case the caller's edit was
    NOT persisted.
    """
    data = load()
    fn(data)
    ok = _save(data)
    return data if ok else None


def get_mtime() -> Optional[float]:
    """Return wishlist.json's mtime, or None if unset/missing on this
    machine. WishlistView polls this every few seconds to detect a change
    made from another machine and reload."""
    path = _wishlist_file()
    if not path or not path.exists():
        return None
    try:
        return path.stat().st_mtime
    except OSError:
        return None


# ── Item CRUD ────────────────────────────────────────────────────────────────

def get_items() -> list:
    """Return all items ordered by sort_order ascending."""
    data = load()
    return sorted(data.get("items", []), key=lambda i: i.get("sort_order", 0))


def get_item(item_id: str) -> Optional[dict]:
    for item in load().get("items", []):
        if item.get("id") == item_id:
            return item
    return None


def get_count() -> int:
    return len(load().get("items", []))


def add_item(title: str, release_date: str = "", notes: str = "",
            cover_url: str = "",
            trailer_url: str = "", trailer_source: str = "",
            trailer_manual_override: bool = False,
            developer: str = "", publisher: str = "",
            genres: Optional[list] = None) -> Optional[dict]:
    """
    Create a new wishlist item, appended to the bottom of the priority
    list — sort_order = max(existing) + 1, or 0 if the list is empty.
    Same "new items append, never disrupt existing order" convention
    Collections already uses for both new collections and new members.

    trailer_url / trailer_source / trailer_manual_override: see the
    module docstring's item shape. Every trailer set through the Add/Edit
    dialog today is user-entered, so callers should pass
    trailer_source="manual", trailer_manual_override=True whenever
    trailer_url is non-blank — this module doesn't assume that for you,
    since a future automatic-detection pass will want to call add_item()
    (or update_item()) with a non-manual source.

    developer / publisher: optional freetext, shown on the Game Detail
    page the same way a real game's info card shows them.
    genres: optional list of freetext genre strings (already split from
    the dialog's comma-separated field — this module stores it as-is).

    Returns the new item dict on success, or None if the write failed
    (e.g. wishlist_path unset/unreachable — caller should show an error,
    the item was NOT saved).
    """
    title = (title or "").strip()
    new_item = {
        "id":           uuid.uuid4().hex,
        "title":        title,
        "sort_order":   0,
        "release_date": (release_date or "").strip(),
        "notes":        (notes or "").strip(),
        "cover_url":    cover_url or "",
        "youtube_url":  trailer_url or "",   # legacy mirror — see module docstring
        "trailer_url":             trailer_url or "",
        "trailer_source":          trailer_source or "",
        "trailer_manual_override": bool(trailer_manual_override),
        "developer":    (developer or "").strip(),
        "publisher":    (publisher or "").strip(),
        "genres":       list(genres) if genres else [],
        "created_at":   datetime.datetime.utcnow().isoformat(),
    }

    def _apply(data):
        existing  = data["items"]
        max_order = max((i.get("sort_order", 0) for i in existing), default=-1)
        new_item["sort_order"] = max_order + 1
        existing.append(new_item)

    result = _read_modify_write(_apply)
    return new_item if result is not None else None


def update_item(item_id: str, title: Optional[str] = None,
                release_date: Optional[str] = None,
                notes: Optional[str] = None,
                cover_url: Optional[str] = None,
                trailer_url: Optional[str] = None,
                trailer_source: Optional[str] = None,
                trailer_manual_override: Optional[bool] = None,
                developer: Optional[str] = None,
                publisher: Optional[str] = None,
                genres: Optional[list] = None) -> bool:
    """
    Partial update — only fields passed as not-None are changed, matching
    the partial-update contract used throughout db.py (e.g.
    set_installed_version()). Returns True if the item was found and the
    write succeeded.
    """
    found = {"ok": False}

    def _apply(data):
        for item in data["items"]:
            if item.get("id") == item_id:
                if title is not None:
                    item["title"] = title.strip()
                if release_date is not None:
                    item["release_date"] = release_date.strip()
                if notes is not None:
                    item["notes"] = notes.strip()
                if cover_url is not None:
                    item["cover_url"] = cover_url
                if trailer_url is not None:
                    item["trailer_url"]  = trailer_url
                    item["youtube_url"]  = trailer_url   # keep legacy mirror in sync
                if trailer_source is not None:
                    item["trailer_source"] = trailer_source
                if trailer_manual_override is not None:
                    item["trailer_manual_override"] = bool(trailer_manual_override)
                if developer is not None:
                    item["developer"] = developer.strip()
                if publisher is not None:
                    item["publisher"] = publisher.strip()
                if genres is not None:
                    item["genres"] = list(genres)
                found["ok"] = True
                break

    result = _read_modify_write(_apply)
    return found["ok"] and result is not None


def remove_item(item_id: str) -> bool:
    """Delete an item. Does NOT touch ignored_matches rows referencing it
    — a harmless orphan (never looked up again once the item is gone) is
    simpler and safer than trying to prune in lockstep across a shared
    file two machines might be editing. The previous file state is kept in
    wishlist_backups/, so the item can be brought back via restore_items()."""
    removed = {"ok": False}

    def _apply(data):
        before = len(data["items"])
        data["items"] = [i for i in data["items"] if i.get("id") != item_id]
        removed["ok"] = len(data["items"]) < before

    result = _read_modify_write(_apply)
    return removed["ok"] and result is not None


def reorder_items(ordered_ids: list) -> bool:
    """
    Persist a new full priority order. ordered_ids is the FULL list of
    item ids in their new display order — same contract as
    db.reorder_games_in_collection()/db.reorder_collections(). Any id in
    the current data but missing from ordered_ids (shouldn't normally
    happen) keeps its existing sort_order rather than being dropped.
    """
    def _apply(data):
        order_index = {iid: i for i, iid in enumerate(ordered_ids)}
        for item in data["items"]:
            if item.get("id") in order_index:
                item["sort_order"] = order_index[item["id"]]

    result = _read_modify_write(_apply)
    return result is not None


# ── Ignored matches ──────────────────────────────────────────────────────────

def is_match_ignored(wishlist_id: str, folder_name: str) -> bool:
    data = load()
    return any(
        e.get("wishlist_id") == wishlist_id and e.get("folder_name") == folder_name
        for e in data.get("ignored_matches", [])
    )


def add_ignored_match(wishlist_id: str, folder_name: str) -> bool:
    """'Keep — don't ask again' — the wishlist item stays, but this
    (wishlist_id, folder_name) pair is never re-prompted."""
    def _apply(data):
        existing = data["ignored_matches"]
        if not any(e.get("wishlist_id") == wishlist_id and e.get("folder_name") == folder_name
                   for e in existing):
            existing.append({"wishlist_id": wishlist_id, "folder_name": folder_name})

    result = _read_modify_write(_apply)
    return result is not None


# ── Cover art ────────────────────────────────────────────────────────────────

def copy_art_into_wishlist(src_path: str) -> Optional[str]:
    """
    Copy a user-picked local file into <wishlist_path>/wishlist_art/ under
    a unique generated name. Returns the path stored in cover_url — always
    RELATIVE to wishlist_path (e.g. "wishlist_art/<uuid>.png"), never
    absolute, since each machine may mount the shared folder at a
    different location (see module docstring). Returns None on failure
    (wishlist_path not configured, or the copy itself raised).
    """
    art_dir = get_art_dir()
    root    = _wishlist_root()
    if not art_dir or not root:
        return None
    try:
        ext       = Path(src_path).suffix or ".png"
        dest_name = f"{uuid.uuid4().hex}{ext}"
        dest      = art_dir / dest_name
        shutil.copy2(src_path, dest)
        return f"{ART_SUBDIR}/{dest_name}"
    except Exception as e:
        log.warning("wishlist_store: could not copy art %s: %s", src_path, e)
        return None


def is_local_art_path(cover_url: str) -> bool:
    """True for a stored wishlist_art/... relative path, False for a URL
    or blank. Used by the UI to decide which loader (URL download vs.
    local-file resolve) applies without re-deriving the regex everywhere."""
    return bool(cover_url) and not re.match(r"^https?://", cover_url, re.IGNORECASE)


def resolve_cover_path(cover_url: str) -> Optional[str]:
    """
    Resolve a stored cover_url to something the UI can actually load:
      - blank                          → None
      - looks like a URL (http/https)  → returned as-is; caller downloads
                                          it the normal way (e.g.
                                          metadata.download_art())
      - a wishlist_art/... relative path → resolved against THIS
                                          machine's current wishlist_path,
                                          so art uploaded on the other
                                          machine still resolves correctly
                                          here once the share is visible

    Returns None if it can't be resolved right now (wishlist_path unset,
    or the referenced file genuinely isn't there yet — e.g. share not
    mounted/synced) — caller should fall back to the placeholder, not
    treat this as an error.
    """
    if not cover_url:
        return None
    if re.match(r"^https?://", cover_url, re.IGNORECASE):
        return cover_url
    root = _wishlist_root()
    if not root:
        return None
    local = root / cover_url
    return str(local) if local.exists() else None


# ── Acquired-game auto-match ───────────────────────────────────────────────
# Fires after every scan, alongside (not instead of) Version Tracking's
# auto-track pass — both are independent consumers of scan_nas()'s
# new_game_ids list. See MainWindow._start_auto_track() for the sibling
# pattern this hooks next to.
#
# Matching is WORD-based, never raw character-substring. The old rule
# (squash to alphanumerics, then substring either way) matched "Ys I" with
# "Physint" ("ysi" appears inside "physint") and "Rage" with "Spyro the
# Dragon: Ripto's Rage". Now:
#   exact  — same words after dropping edition noise ("Deluxe Edition",
#            "GOTY", "Remastered", release-group junk, "the"…).
#   close  — one title is the START or END of the other on whole-word
#            boundaries, the shorter side is distinctive (2+ words, or a
#            single word of 6+ letters — "Exodus" in "Metro Exodus"), and
#            the leftover words contain no number/roman numeral (so "Doom"
#            never matches "Doom 3", "Ys" never matches "Ys II").
#   short single words ("Rage", "Ys") only ever match an exact title.

_NOISE_WORDS = frozenset({
    "the", "a", "an", "of", "and",
    "edition", "goty", "remastered", "remaster", "remake", "deluxe",
    "definitive", "complete", "ultimate", "enhanced", "directors", "cut",
    "anniversary", "collectors", "premium", "standard", "gold", "hd",
    "multi", "repack", "proper", "dlc", "dlcs", "all", "update", "build",
    "win", "pc", "gog", "codex", "plaza", "rune", "cpy", "empress", "flt",
    "elamigos", "dodi", "fitgirl", "goldberg", "tenoke", "skidrow",
})
_ROMAN = frozenset({"ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x",
                    "xi", "xii", "xiii", "xiv", "xv", "xx"})
_MIN_SINGLE_WORD_LEN = 6
_MAX_EXTRA_WORDS = 3


def _title_words(text: str) -> list:
    """Lowercased alphanumeric words with apostrophes folded away
    ("Ripto's" -> "riptos") and edition/release noise removed."""
    t = (text or "").lower().replace("'", "").replace("’", "")
    t = re.sub(r"\(\d{3,}\)", " ", t)                      # store ids: (12345)
    t = re.sub(r"\bv\s?\d+(?:[ ._]\d+)*[a-z]?\b", " ", t)  # v1.2 / v1 12 3
    t = re.sub(r"\bmulti\d*\b", " ", t)                    # MULTi10
    t = re.sub(r"\bbuild\s*\d+\b", " ", t)
    words = re.findall(r"[a-z0-9]+", t)
    return [w for w in words if w not in _NOISE_WORDS]


def _is_numeral(word: str) -> bool:
    return word.isdigit() or word in _ROMAN


def _normalize_title_key(text: str) -> str:
    """Kept for any other callers: words joined, no separators."""
    return "".join(_title_words(text))


def match_confidence(wishlist_title: str, scanned_title: str) -> Optional[str]:
    """'exact' | 'close' | None for a pair of titles (see block comment)."""
    w = _title_words(wishlist_title)
    s = _title_words(scanned_title)
    if not w or not s:
        return None
    if w == s:
        return "exact"
    if len(w) == len(s):
        return None
    short, long_ = (w, s) if len(w) < len(s) else (s, w)
    # the shorter title must be distinctive enough to count at all
    if len(short) < 2 and len(short[0]) < _MIN_SINGLE_WORD_LEN:
        return None
    extra = len(long_) - len(short)
    if extra > _MAX_EXTRA_WORDS:
        return None
    if long_[:len(short)] == short:
        leftover = long_[len(short):]
    elif long_[-len(short):] == short:
        leftover = long_[:-len(short)]
    else:
        return None
    if any(_is_numeral(x) for x in leftover):
        return None   # a sequel / different entry, not the same game
    return "close"


def find_acquired_matches(scanned_titles: dict) -> list:
    """
    scanned_titles: {folder_name: display_title} for newly-scanned games.

    Uses match_confidence() — word-based, see the block comment above.
    Skips any (wishlist_id, folder_name) pair already recorded in
    ignored_matches.

    Returns a list of dicts, one per un-ignored match found, each:
        {"wishlist_id", "wishlist_title", "folder_name", "scanned_title",
         "confidence"}            # confidence: "exact" | "close"
    Exact matches come first. A single wishlist item or scanned game may
    appear more than once if it matches multiple counterparts — the
    caller (the one-at-a-time confirmation flow) queues them all.
    """
    items = get_items()
    if not items or not scanned_titles:
        return []

    data    = load()
    ignored = data.get("ignored_matches", [])

    def _ignored(wid: str, fname: str) -> bool:
        return any(e.get("wishlist_id") == wid and e.get("folder_name") == fname
                  for e in ignored)

    results = []
    for item in items:
        for folder_name, scanned_title in scanned_titles.items():
            conf = match_confidence(item.get("title", ""), scanned_title)
            if not conf or _ignored(item["id"], folder_name):
                continue
            results.append({
                "wishlist_id":    item["id"],
                "wishlist_title": item.get("title", ""),
                "folder_name":    folder_name,
                "scanned_title":  scanned_title,
                "confidence":     conf,
            })
    results.sort(key=lambda r: 0 if r["confidence"] == "exact" else 1)
    return results
