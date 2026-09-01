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
import uuid
from pathlib import Path
from typing import Callable, Optional

import db

log = logging.getLogger(__name__)

WISHLIST_FILENAME = "wishlist.json"
ART_SUBDIR         = "wishlist_art"
CURRENT_VERSION    = 1


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
        data.setdefault("version", CURRENT_VERSION)
        data.setdefault("items", [])
        data.setdefault("ignored_matches", [])
        if not isinstance(data["items"], list):
            data["items"] = []
        if not isinstance(data["ignored_matches"], list):
            data["ignored_matches"] = []
        return data
    except Exception as e:
        log.warning("wishlist_store: could not load %s: %s", path, e)
        return _empty_data()


def _save(data: dict) -> bool:
    """Atomic write: temp file + os.replace(). Returns True on success,
    False if wishlist_path isn't configured or the write failed for any
    reason (never raises)."""
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
            cover_url: str = "") -> Optional[dict]:
    """
    Create a new wishlist item, appended to the bottom of the priority
    list — sort_order = max(existing) + 1, or 0 if the list is empty.
    Same "new items append, never disrupt existing order" convention
    Collections already uses for both new collections and new members.

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
                cover_url: Optional[str] = None) -> bool:
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
                found["ok"] = True
                break

    result = _read_modify_write(_apply)
    return found["ok"] and result is not None


def remove_item(item_id: str) -> bool:
    """Delete an item. Does NOT touch ignored_matches rows referencing it
    — a harmless orphan (never looked up again once the item is gone) is
    simpler and safer than trying to prune in lockstep across a shared
    file two machines might be editing."""
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

def _normalize_title_key(text: str) -> str:
    """Lowercase, strip everything except alphanumerics — same
    normalization idea save_backup.scan_known_locations() already uses
    for its name_key matching. Not a new algorithm, per spec."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def find_acquired_matches(scanned_titles: dict) -> list:
    """
    scanned_titles: {folder_name: display_title} for newly-scanned games.

    Match rule: normalized title keys are equal, OR one is a substring of
    the other — identical containment rule to save_backup.py's
    known-location matching, deliberately reused rather than inventing a
    new one. Skips any (wishlist_id, folder_name) pair already recorded
    in ignored_matches.

    Returns a list of dicts, one per un-ignored match found, each:
        {"wishlist_id", "wishlist_title", "folder_name", "scanned_title"}
    A single wishlist item or a single scanned game may appear in more
    than one result if it fuzzy-matches multiple counterparts — the
    caller (the one-at-a-time confirmation flow) handles that the same
    way it handles any other queued match.
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
        wkey = _normalize_title_key(item.get("title", ""))
        if not wkey:
            continue
        for folder_name, scanned_title in scanned_titles.items():
            skey = _normalize_title_key(scanned_title)
            if not skey:
                continue
            if wkey == skey or wkey in skey or skey in wkey:
                if _ignored(item["id"], folder_name):
                    continue
                results.append({
                    "wishlist_id":    item["id"],
                    "wishlist_title": item.get("title", ""),
                    "folder_name":    folder_name,
                    "scanned_title":  scanned_title,
                })
    return results
