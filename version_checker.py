"""
version_checker.py — Qt worker threads for version tracking in VaultPlay

Four entry points, all sharing ONE system-wide running guard:
  1. VersionAutoTrackWorker  — fires after scan_nas(), checks new games against
                               all formula (auto_track_new_games=1) sites
  2. VersionCheckWorker      — recurring background recheck of ALL existing trackers,
                               also used for manual "Check All Now"
  3. VersionBackfillWorker   — per-site backfill of the existing library
  4. (Timer wiring)          — handled by arm_version_check_timer() / tick, called
                               from MainWindow at startup and after each run

Only one of these four workers should ever be in-flight at a time.
MainWindow owns a single `_version_worker_running: bool` flag and checks it
before starting any of the four. This module's workers set it to False when done.

Delay between individual checks: 0.5 seconds (same spirit as metadata.py's 0.4s).
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

import logging
import time
import datetime
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal

import db
import version_check as vc

log = logging.getLogger(__name__)

CHECK_DELAY_SECONDS = 0.5   # between individual HTTP checks in any batch


def _safe_get(row, key, default=None):
    """
    Safely read a value from a sqlite3.Row.
    sqlite3.Row supports row["key"] indexing but has no .get() method —
    calling .get() on one raises AttributeError. This was crashing
    VersionCheckWorker unconditionally the first time it ran against any
    real trackers (db.get_all_trackers() returns sqlite3.Row objects).
    """
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


# ── Shared progress/result signal shapes ─────────────────────────────────────
# All three workers use the same signal signatures so MainWindow can connect
# a single set of slots regardless of which worker is running.

# progress(current, total, message)
# finished(found_count, checked_count, error_count)


# ── VersionAutoTrackWorker ────────────────────────────────────────────────────

class VersionAutoTrackWorker(QThread):
    """
    Checks new games against all formula (auto_track_new_games=1) sites.
    Fired by MainWindow right after scan_nas() returns its new_game_ids list.

    Check-before-store: a tracker row is only created when the check succeeds
    (status == 'ok'). Games that return 'no_match' are logged in
    version_autotrack_log so they're not re-queried on future scans.
    'error' results are NOT logged — they will be retried on the next scan.

    Progress is reported per-operation (game × site), not per-game, because
    multiple formula sites mean a 700-game library × 3 sites = 2,100 operations.
    """
    progress = pyqtSignal(int, int, str)    # current, total, message
    finished = pyqtSignal(int, int, int)    # found, checked, errors

    def __init__(self, new_game_ids: list):
        super().__init__()
        self._new_game_ids = list(new_game_ids)

    def run(self):
        formula_sites = [s for s in db.get_version_sites()
                         if s["auto_track_new_games"]]
        if not formula_sites:
            log.debug("VersionAutoTrackWorker: no formula sites — nothing to do")
            self.finished.emit(0, 0, 0)
            return

        found   = 0
        checked = 0
        errors  = 0

        # Build full operation list: (game_row, site_row) for every
        # game that hasn't been confirmed absent for this site.
        ops: list[tuple] = []
        for site in formula_sites:
            candidates = db.get_unattempted_new_games_for_site(
                site["id"], self._new_game_ids)
            for game in candidates:
                ops.append((game, site))

        total = len(ops)
        log.info("VersionAutoTrackWorker: %d new games × %d formula sites = %d ops",
                 len(self._new_game_ids), len(formula_sites), total)

        for i, (game, site) in enumerate(ops):
            title = game["title"] or game["display_name"] or game["folder_name"]
            self.progress.emit(
                i + 1, total,
                f"Auto-track {i+1}/{total} — {title} @ {site['label']}"
            )

            # Build URL from slug
            slug = vc.slugify_game_name(
                game["display_name"] or game["folder_name"])
            path    = f"/{slug}"
            url     = vc.build_url(site["base_url"], path, site["suffix"] or "")
            fake_row = {"id": None, "source_url": url}

            result  = vc.check_tracker(fake_row, is_formula_site=True)
            checked += 1
            status   = result["status"]

            if status == "ok":
                # Create tracker and store result
                tracker = db.add_version_tracker(game["id"], site["id"], path)
                db.update_version_tracker_result(
                    tracker_id     = tracker["id"],
                    status         = "ok",
                    dotted_version = result["dotted_version"],
                    plain_version  = result["plain_version"],
                    date_version   = result["date_version"],
                )
                found += 1
                log.info("Auto-track OK: %s @ %s → dotted=%s plain=%s date=%s",
                         title, site["label"],
                         result["dotted_version"], result["plain_version"],
                         result["date_version"])
            elif status == "no_match":
                # Log as no_match — won't be retried for this game/site pair
                db.log_autotrack_attempt(game["id"], site["id"], "no_match")
                log.debug("Auto-track no_match: %s @ %s", title, site["label"])
            else:
                # error — do NOT log, will be retried next scan
                errors += 1
                log.warning("Auto-track error: %s @ %s — %s",
                            title, site["label"], result.get("error_msg"))

            if i < total - 1:
                time.sleep(CHECK_DELAY_SECONDS)

        log.info("VersionAutoTrackWorker done: found=%d checked=%d errors=%d",
                 found, checked, errors)
        self.finished.emit(found, checked, errors)


# ── VersionCheckWorker ────────────────────────────────────────────────────────

class VersionCheckWorker(QThread):
    """
    Rechecks ALL existing version_trackers rows (excluding blacklisted categories).
    Used for:
      - Recurring background timer
      - Manual "Check All Now" button in Settings → Version Tracking

    Unlike VersionAutoTrackWorker, this always rechecks regardless of previous
    no_match results — an existing tracker that returns no_match on recheck just
    updates last_status/last_error while preserving stored version values.

    Monotonic rule: dotted_version/plain_version only advance, never regress.
    Enforced inside db.update_version_tracker_result().
    """
    progress = pyqtSignal(int, int, str)    # current, total, message
    finished = pyqtSignal(int, int, int)    # found (updated), checked, errors

    def run(self):
        trackers = db.get_all_trackers()
        total    = len(trackers)

        if total == 0:
            log.info("VersionCheckWorker: no trackers to check")
            self.finished.emit(0, 0, 0)
            return

        log.info("VersionCheckWorker: checking %d trackers", total)

        found   = 0
        checked = 0
        errors  = 0

        for i, tracker in enumerate(trackers):
            title = _safe_get(tracker, "title") or _safe_get(tracker, "display_name") or ""
            site_label = _safe_get(tracker, "label") or ""
            self.progress.emit(
                i + 1, total,
                f"Checking {i+1}/{total} — {title} @ {site_label}"
            )

            result  = vc.check_tracker(tracker, is_formula_site=False)
            checked += 1
            status   = result["status"]

            db.update_version_tracker_result(
                tracker_id     = tracker["id"],
                status         = status,
                dotted_version = result["dotted_version"],
                plain_version  = result["plain_version"],
                date_version   = result["date_version"],
                error_msg      = result.get("error_msg"),
            )

            if status == "ok":
                found += 1
            elif status == "error":
                errors += 1

            if i < total - 1:
                time.sleep(CHECK_DELAY_SECONDS)

        # Stamp last_run_at
        db.set_setting("version_check_last_run_at",
                       datetime.datetime.utcnow().isoformat())

        log.info("VersionCheckWorker done: found=%d checked=%d errors=%d",
                 found, checked, errors)
        self.finished.emit(found, checked, errors)


# ── VersionBackfillWorker ─────────────────────────────────────────────────────

class VersionBackfillWorker(QThread):
    """
    Runs the auto-track check-before-store pass against all existing library
    games for ONE specific formula site.

    Used by the "Backfill Existing Library" button in Settings → Version Tracking.
    Same check-before-store / log-no-match logic as VersionAutoTrackWorker.

    Candidate games:
      - Not already in version_trackers for this site
      - Not logged as no_match in version_autotrack_log for this site
        (error-logged games ARE retried)
      - Not in a blacklisted category

    Progress is per-game (only one site), so the total count is straightforward.
    """
    progress = pyqtSignal(int, int, str)    # current, total, message
    finished = pyqtSignal(int, int, int)    # found, checked, errors

    def __init__(self, site_id: int):
        super().__init__()
        self._site_id = site_id

    def run(self):
        # Look up the site
        sites = db.get_version_sites()
        site  = next((s for s in sites if s["id"] == self._site_id), None)
        if not site:
            log.warning("VersionBackfillWorker: site_id=%d not found", self._site_id)
            self.finished.emit(0, 0, 0)
            return

        candidates = db.get_backfill_candidates_for_site(self._site_id)
        total      = len(candidates)

        log.info("VersionBackfillWorker: site=%s  %d candidate games",
                 site["label"], total)

        if total == 0:
            self.finished.emit(0, 0, 0)
            return

        found   = 0
        checked = 0
        errors  = 0

        for i, game in enumerate(candidates):
            title = game["title"] or game["display_name"] or game["folder_name"]
            self.progress.emit(
                i + 1, total,
                f"Backfill {i+1}/{total} — {title}"
            )

            slug    = vc.slugify_game_name(game["display_name"] or game["folder_name"])
            path    = f"/{slug}"
            url     = vc.build_url(site["base_url"], path, site["suffix"] or "")
            fake_row = {"id": None, "source_url": url}

            result  = vc.check_tracker(fake_row, is_formula_site=True)
            checked += 1
            status   = result["status"]

            if status == "ok":
                tracker = db.add_version_tracker(game["id"], site["id"], path)
                db.update_version_tracker_result(
                    tracker_id     = tracker["id"],
                    status         = "ok",
                    dotted_version = result["dotted_version"],
                    plain_version  = result["plain_version"],
                    date_version   = result["date_version"],
                )
                found += 1
                log.info("Backfill OK: %s → dotted=%s plain=%s date=%s",
                         title, result["dotted_version"], result["plain_version"],
                         result["date_version"])
            elif status == "no_match":
                db.log_autotrack_attempt(game["id"], self._site_id, "no_match")
                log.debug("Backfill no_match: %s", title)
            else:
                errors += 1
                log.warning("Backfill error: %s — %s",
                            title, result.get("error_msg"))

            if i < total - 1:
                time.sleep(CHECK_DELAY_SECONDS)

        log.info("VersionBackfillWorker done: found=%d checked=%d errors=%d",
                 found, checked, errors)
        self.finished.emit(found, checked, errors)


# ── Timer helpers ─────────────────────────────────────────────────────────────

def get_next_check_delay_ms() -> Optional[int]:
    """
    Compute how many milliseconds until the next background recheck should fire.

    Returns:
        None         — version_check_auto is disabled, don't arm any timer
        0            — overdue or first-ever run (but NOT a true first-ever run
                       with no last_run_at — see note), fire immediately
        positive int — arm a QTimer for this many milliseconds

    First-ever-run rule (from spec):
        If last_run_at is blank AND this is the app's first launch after a scan
        has run (i.e. there ARE trackers in the DB), stamp last_run_at to now
        and return the full interval — don't fire immediately, because the
        new-game auto-track pass already ran on the first scan and we don't
        want to hammer sites twice in quick succession.

        If last_run_at is blank AND there are NO trackers yet, return None —
        no point arming a timer when there's nothing to check.
    """
    if db.get_setting("version_check_auto", "true") != "true":
        return None

    try:
        interval_hours = int(
            db.get_setting("version_check_interval_hours", "24"))
    except (ValueError, TypeError):
        interval_hours = 24

    interval_ms = interval_hours * 3600 * 1000

    last_run_str = db.get_setting("version_check_last_run_at", "")

    if not last_run_str:
        # First-ever run
        tracker_count = _count_all_trackers()
        if tracker_count == 0:
            return None  # nothing to check yet
        # Stamp now and arm for the full interval
        db.set_setting("version_check_last_run_at",
                       datetime.datetime.utcnow().isoformat())
        return interval_ms

    try:
        last_run = datetime.datetime.fromisoformat(
            last_run_str.replace("T", " ")[:19])
    except (ValueError, TypeError):
        # Unparseable timestamp — fire immediately
        return 0

    elapsed_ms = int(
        (datetime.datetime.utcnow() - last_run).total_seconds() * 1000)
    remaining  = interval_ms - elapsed_ms

    return max(0, remaining)


def stamp_last_run_at():
    """
    Update version_check_last_run_at to now.
    Called by VersionCheckWorker (already done inside its run()) but also
    exposed here so MainWindow can stamp it before arming the next timer cycle.
    """
    db.set_setting("version_check_last_run_at",
                   datetime.datetime.utcnow().isoformat())


def _count_all_trackers() -> int:
    """Return the total number of version_trackers rows (for first-run guard)."""
    try:
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM version_trackers"
            ).fetchone()
            return row["n"] if row else 0
    except Exception:
        return 0


def estimate_check_seconds(op_count: int) -> int:
    """
    Estimate total seconds for op_count individual HTTP checks.
    Uses CHECK_DELAY_SECONDS between checks plus a ~2s average fetch time.
    """
    return int(op_count * (CHECK_DELAY_SECONDS + 2.0))
