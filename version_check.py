"""
version_check.py — Version tracking fetch-and-parse logic for VaultPlay

Answers one question per tracker: "what is the newest version of this game
according to this web page?"  Nothing is compared to an installed version here.

Public API
----------
slugify_game_name(name)          str  → URL slug for formula sites
build_url(base_url, path, suffix) str  → full URL to fetch
split_url_into_base_and_rest(url) (base_url, rest)  → for paste-a-URL flow
find_versions(html_text)         [(raw_str, format)]  → all version strings found
highest_per_format(matches)      {format: raw_str}   → best per bucket
classify(raw)                    "dotted" | "plain"
sort_key(raw)                    comparable key for sorting
check_tracker(tracker_row)       CheckResult

CheckResult is a dict:
    {
        "status":         "ok" | "no_match" | "error",
        "dotted_version": str | None,
        "plain_version":  str | None,
        "error_msg":      str | None,
        "source_url":     str,
    }

Design notes
------------
- Browser-like User-Agent by default (some sites block the requests default UA).
- 15-second timeout, matching the timeouts used in protondb.py / steamdb.py.
- Response body is capped at MAX_RESPONSE_BYTES before parsing — avoid reading
  an unbounded page into memory when the user pastes a misconfigured URL.
- 0.5s delay between checks in batch operations is the CALLER's responsibility
  (version_check.py never sleeps — it only checks one URL per call).
- Uses BeautifulSoup to strip HTML before regex matching, so patterns don't
  accidentally match version numbers embedded inside tag attributes or JS code.
- EXCLUDE_KEYWORDS: lines that contain these tokens are stripped before matching
  to avoid common false positives (e.g. "DirectX version 12 required").
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
import re
import time
from typing import Optional
from urllib.parse import urlparse, urljoin

import requests
import requests.adapters

log = logging.getLogger(__name__)

# ── HTTP session ──────────────────────────────────────────────────────────────
# Browser-like UA: some wiki/info sites block the default 'python-requests/x.y'
# UA — same lesson learned from protondb.py.

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent":      "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.9",
    "Accept-Language": "en-US,en;q=0.5",
})
_adapter = requests.adapters.HTTPAdapter(
    pool_connections=2, pool_maxsize=4, max_retries=1
)
SESSION.mount("https://", _adapter)
SESSION.mount("http://",  _adapter)

TIMEOUT_SECONDS  = 15
MAX_RESPONSE_BYTES = 4 * 1024 * 1024   # 4 MB cap — exceeding this → error status

# ── Version patterns ──────────────────────────────────────────────────────────
# Ordered most-specific to least-specific.
# Each entry: (compiled_regex, format_bucket)
# format_bucket: "dotted" for semantic versions, "plain" for build/revision
# numbers, "date" for calendar-date-based versioning.
#
# "dotted" bucket — canonical form: one or more numeric segments separated by dots,
# optionally prefixed with 'v', optionally suffixed with a short label.
# Examples: "1.0.4", "v2.11.0", "1.0.0-rc1", "Version 3.5.2"
#
# "plain" bucket — long numeric build IDs with no dots.
# Examples: "Build 11442480", "build 2024031501"
#
# "date" bucket — DD.MM.YYYY calendar-date versioning, confirmed common in
# real NAS release naming (see scanner.py's detect_version() for the same
# pattern applied to filenames). Kept as a genuinely separate bucket from
# "dotted" rather than folded in — a date like "14.08.2023" would otherwise
# get misread as semantic version 14.8.2023 and sort in the wrong order
# entirely (day-first instead of chronological). See date_sort_key() below.
#
# Patterns are applied to individual text lines AFTER HTML stripping and AFTER
# EXCLUDE_KEYWORDS filtering — see find_versions() for the pipeline.

VERSION_PATTERNS = [
    # DD.MM.YYYY date-based versioning — checked before the generic dotted
    # pattern below, since a date is shaped like a 3-segment dotted version
    # and find_versions() explicitly excludes date-shaped matches from the
    # dotted bucket (see _looks_like_date()) to avoid double-counting the
    # same substring into both buckets.
    (re.compile(r"\b(\d{1,2}\.\d{1,2}\.\d{4})\b"), "date"),

    # "updated to version X.Y.Z" / "updated to X.Y.Z"
    (re.compile(
        r"updated\s+to\s+(?:version\s+)?v?(\d+\.\d+(?:\.\d+)*(?:-[a-zA-Z0-9]+)?)(?!\d)(?![.\d])",
        re.IGNORECASE
    ), "dotted"),

    # "version X.Y.Z" / "Version: X.Y.Z" (colon optional)
    (re.compile(
        r"\bversion\s*[:\-]?\s*v?(\d+\.\d+(?:\.\d+)*(?:-[a-zA-Z0-9]+)?)(?!\d)(?![.\d])",
        re.IGNORECASE
    ), "dotted"),

    # Bare "vX.Y.Z" with word-boundary — common on wikis ("v1.0.4 (2024-...)")
    (re.compile(
        r"\bv(\d+\.\d+(?:\.\d+)*(?:-[a-zA-Z0-9]+)?)(?!\d)(?![.\d])\b",
        re.IGNORECASE
    ), "dotted"),

    # Bare "X.Y.Z" with at least two segments and word-boundary on both sides
    # Requires at least major.minor to avoid matching lone integers.
    # Negative lookahead/behind on \d prevents matching inside longer numbers.
    (re.compile(
        r"(?<!\d)(\d+\.\d+(?:\.\d+)*(?:-[a-zA-Z0-9]+)?)(?!\d)(?![.\d])",
    ), "dotted"),

    # Build NNNNN — GOG-style long build numbers (3 or more digits, no dots)
    # "Build 11442480", "build: 2024031501"
    (re.compile(
        r"\bbuild\s*[:\-]?\s*(\d{3,})\b",
        re.IGNORECASE
    ), "plain"),
]

# Lines containing any of these tokens (case-insensitive, word-boundary match)
# are stripped before pattern matching — common sources of false positives.
EXCLUDE_KEYWORDS = [
    "directx",
    "opengl",
    "openal",
    "requires",
    "minimum",
    "recommended",
    "system requirements",
    "operating system",
    "os:",
    "windows ",
    "processor",
    "memory",
    "storage",
    "graphics",
    "copyright",
    "engine version",
    "unity",
    "unreal",
    "dx11",
    "dx12",
    "build tools",
    "sdk",
    "api",
    "python",
    "java",
    "framework",
    ".net",
    "visual c++",
    "vcredist",
    "patch notes",    # suppress lines that say "patch notes for version X" (list headers)
]

_EXCLUDE_RE = re.compile(
    "|".join(re.escape(kw) for kw in EXCLUDE_KEYWORDS),
    re.IGNORECASE
)


# ── Slugify ───────────────────────────────────────────────────────────────────

def slugify_game_name(name: str) -> str:
    """
    Convert a cleaned display_name to a URL slug for formula sites.

    Rules (from spec worked examples):
      - Lowercase
      - Spaces → dashes
      - STRIP (don't dash) punctuation: apostrophes, colons, periods, commas,
        exclamation marks, question marks, ampersands
      - Multiple consecutive dashes → single dash
      - Strip leading/trailing dashes

    Examples:
      "Baldur's Gate 3"               → "baldurs-gate-3"
      "Tom Clancy's Splinter Cell"     → "tom-clancys-splinter-cell"
      "Resident Evil: Village"         → "resident-evil-village"
      "Sons of the Forest"             → "sons-of-the-forest"
    """
    s = name.lower()
    # Strip punctuation that should disappear entirely (not become a dash)
    s = re.sub(r"[''':.,!?&]", "", s)
    # Replace remaining non-alphanumeric runs with a single dash
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


# ── URL helpers ───────────────────────────────────────────────────────────────

def build_url(base_url: str, path: str, suffix: str) -> str:
    """
    Construct the full URL to fetch for a tracker.
    Never stores the result — always computed on the fly.
    """
    # base_url has already had its trailing slash stripped at creation time.
    # path may or may not start with '/'; suffix may or may not start with '/'.
    return base_url.rstrip("/") + path + (suffix or "")


def split_url_into_base_and_rest(pasted_url: str) -> tuple[str, str]:
    """
    Split a user-pasted URL into (base_url, rest) for the paste-a-URL flow.

    base_url = scheme + host (no trailing slash, no www.)
    rest     = everything after the host, starting with '/'

    Examples:
      "https://www.pcgamingwiki.com/wiki/Cyberpunk_2077"
        → ("https://pcgamingwiki.com", "/wiki/Cyberpunk_2077")
      "https://steamdb.info/app/730/info/"
        → ("https://steamdb.info", "/app/730/info/")
      "not a url"
        → raises ValueError

    The returned base_url strips 'www.' so it matches the normalization
    used by db._normalize_base_url() and db.get_or_create_version_site_by_base_url().
    """
    pasted_url = pasted_url.strip()
    if not pasted_url:
        raise ValueError("URL is empty")
    parsed = urlparse(pasted_url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"Not a valid absolute URL: {pasted_url!r} "
            "(must start with http:// or https://)"
        )
    if not parsed.netloc:
        raise ValueError(f"No host found in URL: {pasted_url!r}")

    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    base_url = f"{parsed.scheme.lower()}://{host}"
    # rest = path + query + fragment
    rest = parsed.path
    if parsed.query:
        rest += "?" + parsed.query
    if parsed.fragment:
        rest += "#" + parsed.fragment

    if not rest:
        rest = "/"

    return base_url, rest


def validate_url(url: str) -> Optional[str]:
    """
    Return None if url is a valid absolute HTTP/HTTPS URL, else return
    a human-readable error string.  Used by VersionTrackerDialog before
    attempting to parse/create a site from pasted input.
    """
    if not url or not url.strip():
        return "URL is empty"
    try:
        parsed = urlparse(url.strip())
        if parsed.scheme not in ("http", "https"):
            return f"URL must start with http:// or https:// (got {parsed.scheme!r})"
        if not parsed.netloc:
            return "URL has no host"
        return None
    except Exception as e:
        return str(e)


# ── Version extraction ────────────────────────────────────────────────────────

def _strip_html(html: str) -> str:
    """
    Use BeautifulSoup to extract visible text from HTML.
    Falls back to a simple regex strip if bs4 isn't available.
    """
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        # Remove script and style tags entirely (their content isn't visible text)
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        return soup.get_text(separator="\n")
    except ImportError:
        log.debug("beautifulsoup4 not installed — falling back to regex HTML strip")
        return re.sub(r"<[^>]+>", " ", html)


def _filter_lines(text: str) -> list[str]:
    """
    Split text into lines, strip each, and remove lines that contain
    EXCLUDE_KEYWORDS — the primary false-positive suppression mechanism.
    Also strips lines that are pure whitespace or very short (< 3 chars).
    """
    lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if len(line) < 3:
            continue
        if _EXCLUDE_RE.search(line):
            continue
        lines.append(line)
    return lines


def _looks_like_date(raw: str) -> bool:
    """
    True if a dotted-shaped 3-segment string is actually a plausible
    DD.MM.YYYY calendar date rather than a semantic version — e.g.
    "21.08.2023" (a date) vs "2.11.0" (a version). Used to keep the
    generic bare-dotted pattern from double-counting a date match into
    the "dotted" bucket when the dedicated date pattern already claims it.
    """
    parts = raw.split(".")
    if len(parts) != 3:
        return False
    day, month, year = parts
    if not (day.isdigit() and month.isdigit() and year.isdigit()):
        return False
    if len(year) != 4:
        return False
    d, m, y = int(day), int(month), int(year)
    return 1 <= d <= 31 and 1 <= m <= 12 and 1900 <= y <= 2099


def find_versions(html_text: str) -> list[tuple[str, str]]:
    """
    Extract all version strings from an HTML page.

    Pipeline:
      1. Strip HTML → visible text (BeautifulSoup if available, regex fallback)
      2. Split into lines, filter EXCLUDE_KEYWORDS lines
      3. Apply VERSION_PATTERNS to each line
      4. Deduplicate (preserve first occurrence of each raw string)

    A date-shaped 3-segment match (see _looks_like_date()) is excluded from
    the "dotted" bucket even if the generic bare-dotted pattern would also
    match it — it belongs in "date" only, never both, since the two buckets
    use incompatible comparison logic (semver-style vs. chronological).

    Returns list of (raw_version_string, format_bucket) tuples,
    in order of first appearance.
    """
    text  = _strip_html(html_text)
    lines = _filter_lines(text)
    seen: set[str] = set()
    results: list[tuple[str, str]] = []

    for line in lines:
        for pattern, fmt in VERSION_PATTERNS:
            for m in pattern.finditer(line):
                raw = m.group(1).strip()
                if not raw or raw in seen:
                    continue
                if fmt == "dotted" and _looks_like_date(raw):
                    continue
                seen.add(raw)
                results.append((raw, fmt))

    return results


def classify(raw: str) -> str:
    """
    Return "dotted" or "plain" for a version string.
    "dotted" = contains at least one dot (semantic version shape).
    "plain"  = no dots (build number / long integer shape).
    """
    return "dotted" if "." in raw else "plain"


def sort_key(raw: str) -> tuple:
    """
    Return a comparable tuple for ordering version strings within their bucket.

    For dotted versions:  each segment as an integer, with non-numeric segments
      converted to 0 and appended at the end as a string for tie-breaking.
      "1.0.4"   → (1, 0, 4)
      "1.0.4-rc1" → (1, 0, 4)   (suffix stripped for numeric comparison)
      "2.11.0"  → (2, 11, 0)
      "v1.2"    → (1, 2)

    For plain versions: the string as a zero-padded integer (if numeric) so
      "Build 11442480" and "11442480" both sort as integers.
      Non-numeric plain versions sort lexicographically.

    Deliberately avoids cross-bucket comparison — callers always compare
    within one bucket, never dotted vs. plain.
    """
    if not raw:
        return (0,)

    # Strip leading 'v' prefix
    s = raw.lstrip("vV")

    if "." in s:
        # Dotted: split on dots, strip trailing label suffix
        # "1.0.4-rc1" → ["1", "0", "4-rc1"] → strip after dash/space
        parts = re.split(r"\.", s)
        nums = []
        for part in parts:
            # Take only the numeric prefix of each segment
            m = re.match(r"(\d+)", part)
            nums.append(int(m.group(1)) if m else 0)
        return tuple(nums)
    else:
        # Plain: try pure integer
        m = re.match(r"(\d+)", s)
        if m:
            return (int(m.group(1)),)
        return (s,)


def date_sort_key(raw: str) -> tuple:
    """
    Return a comparable (year, month, day) tuple for a DD.MM.YYYY date
    version string — chronological ordering, deliberately NOT the same
    logic as sort_key(). A plain numeric-tuple compare of "14.08.2023" as
    if it were dotted would sort by day first (14 vs 21) and get the order
    completely wrong; this reorders to (year, month, day) so comparisons
    are actually chronological.
    Returns (0, 0, 0) for anything that doesn't parse as DD.MM.YYYY, so a
    malformed value always sorts lowest rather than raising.
    """
    parts = raw.split(".")
    if len(parts) != 3:
        return (0, 0, 0)
    day, month, year = parts
    if not (day.isdigit() and month.isdigit() and year.isdigit()):
        return (0, 0, 0)
    return (int(year), int(month), int(day))


def highest_per_format(matches: list[tuple[str, str]]) -> dict[str, str]:
    """
    Given a list of (raw_str, format_bucket) tuples from find_versions(),
    return the highest version per bucket.

    Returns dict with keys "dotted", "plain", and "date" (any may be None
    if no match of that type was found):
        {"dotted": "2.11.0", "plain": None, "date": None}

    "date" is compared using date_sort_key() (chronological), not sort_key()
    (which "dotted" and "plain" use) — the two are intentionally different,
    non-interchangeable comparison schemes.
    """
    best: dict[str, Optional[str]] = {"dotted": None, "plain": None, "date": None}

    for raw, fmt in matches:
        if fmt not in best:
            continue
        current_best = best[fmt]
        if current_best is None:
            best[fmt] = raw
        else:
            try:
                key_fn = date_sort_key if fmt == "date" else sort_key
                if key_fn(raw) > key_fn(current_best):
                    best[fmt] = raw
            except TypeError:
                # Incomparable types (mixed str/int tuple) — keep current
                pass

    return best


# ── Main check function ───────────────────────────────────────────────────────

def check_tracker(tracker_row, is_formula_site: bool = False) -> dict:
    """
    Fetch the tracker's source URL and extract the current version.

    tracker_row must expose: source_url (precomputed), id, last_status.
    Compute source_url as base_url + path + suffix — pass the joined row
    from db.get_trackers_for_game() or db.get_all_trackers(), which already
    includes this as a computed column.

    is_formula_site:
        True  → a 404 is treated as 'no_match' (auto-guessed slug may not exist)
        False → a 404 is treated as 'error' (user confirmed the URL at some point)

    Returns CheckResult dict:
        {
            "status":         "ok" | "no_match" | "error",
            "dotted_version": str | None,
            "plain_version":  str | None,
            "date_version":   str | None,
            "error_msg":      str | None,
            "source_url":     str,
        }
    """
    try:
        source_url = tracker_row["source_url"]
    except (KeyError, TypeError) as e:
        return {
            "status":         "error",
            "dotted_version": None,
            "plain_version":  None,
            "date_version":   None,
            "error_msg":      f"tracker_row missing source_url: {e}",
            "source_url":     "",
        }

    result_base = {
        "dotted_version": None,
        "plain_version":  None,
        "date_version":   None,
        "source_url":     source_url,
    }

    # ── Fetch ─────────────────────────────────────────────────────────────────
    try:
        resp = SESSION.get(source_url, timeout=TIMEOUT_SECONDS, stream=True)
    except requests.exceptions.Timeout:
        log.warning("[VERSION] Timeout fetching %s", source_url)
        return {**result_base, "status": "error",
                "error_msg": "Request timed out"}
    except requests.exceptions.RequestException as e:
        log.warning("[VERSION] Network error fetching %s: %s", source_url, e)
        return {**result_base, "status": "error",
                "error_msg": f"Network error: {e}"}

    # ── HTTP status classification ────────────────────────────────────────────
    if resp.status_code == 404:
        resp.close()
        status = "no_match" if is_formula_site else "error"
        log.info("[VERSION] 404 for %s → %s", source_url, status)
        return {**result_base, "status": status,
                "error_msg": "HTTP 404 Not Found"}

    if resp.status_code != 200:
        resp.close()
        log.warning("[VERSION] HTTP %d for %s", resp.status_code, source_url)
        return {**result_base, "status": "error",
                "error_msg": f"HTTP {resp.status_code}"}

    # ── Read with size cap ────────────────────────────────────────────────────
    try:
        body_bytes = b""
        for chunk in resp.iter_content(chunk_size=65536):
            body_bytes += chunk
            if len(body_bytes) > MAX_RESPONSE_BYTES:
                resp.close()
                log.warning("[VERSION] Response too large (>%d bytes) for %s",
                            MAX_RESPONSE_BYTES, source_url)
                return {**result_base, "status": "error",
                        "error_msg": f"Response exceeded {MAX_RESPONSE_BYTES // 1024 // 1024} MB cap"}
        resp.close()
    except requests.exceptions.RequestException as e:
        log.warning("[VERSION] Read error for %s: %s", source_url, e)
        return {**result_base, "status": "error",
                "error_msg": f"Read error: {e}"}

    # ── Decode ────────────────────────────────────────────────────────────────
    try:
        encoding = resp.encoding or "utf-8"
        html_text = body_bytes.decode(encoding, errors="replace")
    except Exception as e:
        log.warning("[VERSION] Decode error for %s: %s", source_url, e)
        return {**result_base, "status": "error",
                "error_msg": f"Decode error: {e}"}

    # ── Parse ─────────────────────────────────────────────────────────────────
    matches = find_versions(html_text)
    best    = highest_per_format(matches)

    dotted = best.get("dotted")
    plain  = best.get("plain")
    date   = best.get("date")

    if dotted or plain or date:
        log.info("[VERSION] %s → dotted=%s plain=%s date=%s",
                 source_url, dotted, plain, date)
        return {
            **result_base,
            "status":         "ok",
            "dotted_version": dotted,
            "plain_version":  plain,
            "date_version":   date,
            "error_msg":      None,
        }
    else:
        log.info("[VERSION] %s → no version found (no_match)", source_url)
        return {**result_base, "status": "no_match", "error_msg": None}


def check_and_store(tracker_row, is_formula_site: bool = False) -> dict:
    """
    Convenience wrapper: check_tracker() + db.update_version_tracker_result().

    Returns the same CheckResult dict as check_tracker().
    Caller is still responsible for the 0.5s inter-request delay between
    batch calls (version_check.py never sleeps).
    """
    import db
    result = check_tracker(tracker_row, is_formula_site=is_formula_site)
    db.update_version_tracker_result(
        tracker_id      = tracker_row["id"],
        status          = result["status"],
        dotted_version  = result["dotted_version"],
        plain_version   = result["plain_version"],
        date_version    = result["date_version"],
        error_msg       = result["error_msg"],
    )
    return result
