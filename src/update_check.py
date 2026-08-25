"""
update_check.py — AppImage Self-Update Check for VaultPlay

Spec: Notion → Features → Fully Planned → AppImage Self-Update Check.

Pure-logic module — no PyQt here, same split as version_check.py/version_checker.py:
this module does the GitHub API call, version comparison, download, and self-replace
script; the QThread wrappers that call it live in ui/settings_view.py (manual check +
download) and ui/main_window.py (silent launch check), matching how version_check.py
stays Qt-free while version_checker.py owns the QThread workers.

Repo: tribalmonkey1/vaultplay — public, no auth token needed for the releases API,
but GitHub's unauthenticated rate limit is 60 req/hour/IP. This module only ever
calls the API once per launch plus once per manual click, so that's not a practical
concern — noted here in case a future change (e.g. polling) needs to account for it.

Five responsibilities:
  1. fetch_latest_release()   — hit the GitHub releases API, return the parsed result
  2. is_update_available()    — semantic version compare, current APP_VERSION vs. tag
  3. download_update()        — resumable (HTTP Range) download of the .AppImage asset
  4. launch_self_update()     — write + launch the detached updater shell script
  5. extract_version_from_title() — used only so dismissing the notification can
     record it as skipped_app_version (see ui/library_view.py's NotificationPanel)

Version source of truth stays SettingsView.APP_VERSION in ui/settings_view.py (per
the Project Log's standing rule — NOT duplicated here). Every function below that
needs "the current version" takes it as a parameter from the caller instead.
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
# ─────────────────────────────────────────────────────────────────────────────

import datetime
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Callable, Optional

import requests
import requests.adapters

log = logging.getLogger(__name__)

GITHUB_REPO                = "tribalmonkey1/vaultplay"
GITHUB_API_LATEST_RELEASE  = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
GITHUB_RELEASES_PAGE       = f"https://github.com/{GITHUB_REPO}/releases"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "VaultPlay-SelfUpdater/1.0",
    "Accept":     "application/vnd.github+json",
})
_adapter = requests.adapters.HTTPAdapter(pool_connections=2, pool_maxsize=2, max_retries=1)
SESSION.mount("https://", _adapter)
SESSION.mount("http://", _adapter)


# ── AppImage runtime helpers ──────────────────────────────────────────────────

def is_running_as_appimage() -> bool:
    """True if this process is actually running from a mounted AppImage (the
    AppImage runtime sets $APPIMAGE to the .AppImage file's own path — see
    main.py's _print_system_info(), which already reports this at startup)."""
    return bool(os.environ.get("APPIMAGE"))


def current_appimage_path() -> Optional[str]:
    """Path to the currently-running .AppImage file, or None if not running
    as one (e.g. launched via `python3 main.py` during development)."""
    return os.environ.get("APPIMAGE") or None


# ── Version parsing / comparison ──────────────────────────────────────────────

def normalize_tag(tag: str) -> str:
    """Strip a leading 'v' from a GitHub release tag: 'v0.4.0' -> '0.4.0'."""
    tag = (tag or "").strip()
    return tag[1:] if tag[:1].lower() == "v" else tag


def normalize_app_version(app_version: str) -> str:
    """Strip a trailing '-dev' (or any '-suffix') from APP_VERSION: '0.3.0-dev' -> '0.3.0'."""
    v = (app_version or "").strip()
    return v.split("-", 1)[0]


def _version_tuple(v: str) -> tuple:
    """Parse a dotted version string into a tuple of ints. Non-numeric/empty
    input becomes (0,) rather than raising, so a malformed string just sorts
    lowest instead of crashing the comparison."""
    parts = re.findall(r"\d+", v)
    return tuple(int(p) for p in parts) if parts else (0,)


def is_update_available(current_app_version: str, latest_tag: str) -> bool:
    """
    True if latest_tag (a raw GitHub release tag, e.g. 'v0.4.0') is a newer
    semantic version than current_app_version (the raw APP_VERSION string,
    e.g. '0.3.0-dev'). Tuples are padded to equal length before comparing so
    '1.0' and '1.0.0' compare equal rather than '1.0' looking "older" purely
    because it has fewer segments.
    """
    current = _version_tuple(normalize_app_version(current_app_version))
    latest  = _version_tuple(normalize_tag(latest_tag))
    length  = max(len(current), len(latest))
    current = current + (0,) * (length - len(current))
    latest  = latest  + (0,) * (length - len(latest))
    return latest > current


def format_release_date(published_at: str) -> str:
    """GitHub ISO8601 'published_at' -> 'July 17, 2026'. Returns the raw string
    unchanged if it doesn't parse, rather than raising."""
    if not published_at:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return published_at
    try:
        return dt.strftime("%B %-d, %Y")   # no leading zero on day — fine on Linux/glibc
    except ValueError:
        return dt.strftime("%B %d, %Y")    # portable fallback


def extract_version_from_title(title: str) -> Optional[str]:
    """
    Pull the 'X.Y.Z' version token back out of an app_update notification's
    title (see main_window.py's f"Update Available: v{version}" text) — used
    only so dismissing the notification can record it as the skipped version
    (see ui/library_view.py's NotificationPanel._on_dismiss). Returns None if
    no version-shaped token is found.
    """
    if not title:
        return None
    m = re.search(r"v(\d+\.\d+\.\d+)", title)
    return m.group(1) if m else None


# ── GitHub API ─────────────────────────────────────────────────────────────────

def _find_appimage_asset(release: dict) -> Optional[dict]:
    """Return the .AppImage asset dict from a GitHub release's 'assets' list."""
    for asset in release.get("assets", []) or []:
        if asset.get("name", "").lower().endswith(".appimage"):
            return asset
    return None


def fetch_latest_release() -> Optional[dict]:
    """
    Fetch the latest GitHub release for VaultPlay.

    Returns:
        {
            "tag":          str,        # raw tag, e.g. "v0.4.0"
            "published_at": str,        # raw ISO8601 timestamp
            "release_date": str,        # human-formatted, e.g. "July 17, 2026"
            "html_url":     str,        # GitHub releases page for this release
            "download_url": str | None, # direct .AppImage asset URL, None if
                                         # the release has no AppImage attached
            "asset_size":   int,        # bytes, 0 if unknown
        }
    or None on any failure (network error, non-200, malformed JSON, no tag) —
    never raises. Per spec, a failed check does nothing silently, so callers
    should treat None as "couldn't check — don't show or change anything."
    """
    try:
        resp = SESSION.get(GITHUB_API_LATEST_RELEASE, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.info("[UPDATE CHECK] Could not reach GitHub releases API: %s", e)
        return None

    tag = data.get("tag_name") or ""
    if not tag:
        return None

    asset = _find_appimage_asset(data)
    published_at = data.get("published_at") or ""

    return {
        "tag":           tag,
        "published_at":  published_at,
        "release_date":  format_release_date(published_at),
        "html_url":      data.get("html_url") or GITHUB_RELEASES_PAGE,
        "download_url":  asset.get("browser_download_url") if asset else None,
        "asset_size":    asset.get("size", 0) if asset else 0,
    }


# ── Download (resumable via HTTP Range) ───────────────────────────────────────

def download_update(download_url: str, dest_path, progress_cb: Optional[Callable[[int, int], None]] = None):
    """
    Download an AppImage asset to dest_path, resuming from an existing partial
    file at dest_path if one is already present (HTTP Range request) — this is
    what makes Settings → About's "Retry" button resume instead of restarting.

    progress_cb(downloaded_bytes, total_bytes) is called after every chunk.
    total_bytes is 0 if the server didn't report Content-Length — callers
    should treat that as "unknown total" (no percentage), not zero remaining.

    Returns (success: bool, error_msg: str). Never raises — network/IO errors
    are caught and reported via the return tuple. A failed download leaves
    whatever partial bytes were already written on disk (per spec: Retry
    resumes from there, Cancel is the caller's job to delete it).
    """
    dest_path = Path(dest_path)
    resume_from = dest_path.stat().st_size if dest_path.exists() else 0

    headers = {"Range": f"bytes={resume_from}-"} if resume_from else {}

    try:
        resp = SESSION.get(download_url, headers=headers, stream=True, timeout=30)

        if resume_from and resp.status_code == 200:
            # Server doesn't support Range and sent the whole file from byte 0 —
            # start the local file over rather than corrupting it by appending.
            resume_from = 0
        elif resp.status_code not in (200, 206):
            resp.raise_for_status()

        content_length = int(resp.headers.get("content-length", 0) or 0)
        total = resume_from + content_length
        mode  = "ab" if resume_from else "wb"
        downloaded = resume_from

        with open(dest_path, mode) as f:
            for chunk in resp.iter_content(chunk_size=262144):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                if progress_cb:
                    progress_cb(downloaded, total)
        resp.close()
        return True, ""
    except Exception as e:
        log.warning("[UPDATE CHECK] Download failed: %s", e)
        return False, str(e)


# ── Self-replacement ───────────────────────────────────────────────────────────
# Flow (per spec): VaultPlay launches this script detached, then exits. The
# script polls for VaultPlay's PID to disappear, renames the downloaded
# AppImage over the running one, launches it, then deletes itself. This is
# what keeps two instances from ever running at once — the rename+relaunch
# only happens after confirming the old process is fully gone.

_UPDATER_SCRIPT = """#!/bin/bash
# VaultPlay self-updater — written and launched by update_check.py.
# Args: $1 = PID of the VaultPlay process to wait for
#       $2 = path to the newly-downloaded AppImage
#       $3 = path to the AppImage to replace (the one currently installed)
PID="$1"
NEW_APPIMAGE="$2"
TARGET_APPIMAGE="$3"

# Wait for the running VaultPlay process to fully exit before touching its
# own file — this is the guarantee that two instances never run at once.
while kill -0 "$PID" 2>/dev/null; do
    sleep 0.3
done

mv -f "$NEW_APPIMAGE" "$TARGET_APPIMAGE"
chmod +x "$TARGET_APPIMAGE"

nohup "$TARGET_APPIMAGE" >/dev/null 2>&1 &
disown

# Bash has already read this whole script into memory before running it, so
# it's safe to delete it out from under itself here.
rm -f "$0"
"""


def _updater_script_path() -> Path:
    env = os.environ.get("VAULTPLAY_CONFIG_DIR")
    base = Path(env) if env else (Path.home() / ".config" / "vaultplay")
    base.mkdir(parents=True, exist_ok=True)
    return base / "vaultplay_updater.sh"


def launch_self_update(new_appimage_path: str, target_appimage_path: str) -> bool:
    """
    Write the updater shell script and launch it detached (so it survives
    this process exiting), then return. The CALLER must quit the application
    immediately after this returns True — the script is already polling for
    this process's PID to disappear before it touches anything.

    Returns False (and starts nothing) if the script couldn't be written or
    launched — the caller should show an error rather than quit the app.
    """
    try:
        script_path = _updater_script_path()
        script_path.write_text(_UPDATER_SCRIPT)
        script_path.chmod(0o755)

        subprocess.Popen(
            [str(script_path), str(os.getpid()), str(new_appimage_path), str(target_appimage_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        log.info("[UPDATE CHECK] Updater script launched: %s -> %s",
                 new_appimage_path, target_appimage_path)
        return True
    except Exception as e:
        log.error("[UPDATE CHECK] Could not launch updater script: %s", e)
        return False
