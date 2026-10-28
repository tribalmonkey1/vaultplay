"""
scan_reconcile.py — Non-destructive NAS scan wrapper for VaultPlay

A scan never clears the library. After scanner.scan_nas() has upserted every
game it found (keeping user state, keyed on folder_name), this flags games
whose NAS folder no longer exists (games.missing_from_nas) and un-flags ones
that are back. Switching NAS path therefore keeps shared games, adds new ones
and flags the missing ones.

Nothing is flagged when the NAS looks unreachable or the scan reported errors,
and games in blacklisted categories are never touched.
"""

import logging
from pathlib import Path

import db
import scanner

log = logging.getLogger(__name__)


def reconcile_missing(nas_path: str, result: dict) -> dict:
    out = {"flagged": 0, "cleared": 0, "skipped_reason": ""}
    root = Path(nas_path) if nas_path else None
    if not root or not root.is_dir():
        out["skipped_reason"] = "NAS root not reachable"
        return out
    if result.get("errors"):
        out["skipped_reason"] = "scan reported errors"
        return out
    if result.get("total", 0) == 0:
        # An empty scan of a reachable root is more likely a flaky mount than
        # a genuinely emptied library; don't mass-flag on it.
        out["skipped_reason"] = "scan found no games"
        return out
    try:
        blacklisted = set(db.get_blacklisted_categories())
        games = db.get_all_games()
    except Exception as e:
        out["skipped_reason"] = f"db error: {e}"
        return out
    for g in games:
        if g["category"] in blacklisted:
            continue
        p = g["nas_path"] or ""
        present = bool(p) and Path(p).exists()
        was_missing = bool(g["missing_from_nas"]) if "missing_from_nas" in g.keys() else False
        try:
            if not present and not was_missing:
                db.set_game_missing_from_nas(g["id"], True)
                out["flagged"] += 1
            elif present and was_missing:
                db.set_game_missing_from_nas(g["id"], False)
                out["cleared"] += 1
        except Exception as e:
            log.warning("reconcile: could not update %s: %s", g["folder_name"], e)
    log.info("[SCAN] reconcile: %d flagged missing, %d restored",
             out["flagged"], out["cleared"])
    return out


def scan_and_reconcile(nas_path: str, progress_callback=None) -> dict:
    result = scanner.scan_nas(nas_path, progress_callback)
    try:
        result["reconcile"] = reconcile_missing(nas_path, result)
    except Exception as e:
        log.warning("reconcile failed: %s", e)
    return result
