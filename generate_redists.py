#!/usr/bin/env python3
"""
generate_redists.py — VaultPlay redistributable data generator

Reads all steam_app_id values from VaultPlay's SQLite database, queries
SteamCMD for each game's declared redistributable depots, and writes the
results to assets/redists.json in the project folder.

Run this on your machine whenever you want to update the bundled redist data.
The output file gets bundled into the AppImage automatically via install.sh.

Usage:
    python3 generate_redists.py                    # auto-find DB + write to assets/
    python3 generate_redists.py --db /path/to/db   # explicit DB path
    python3 generate_redists.py --dry-run           # print results, don't write file
    python3 generate_redists.py --app-id 1017900   # single game (for testing)

Requirements:
    steamcmd must be installed (pacman -S steamcmd)
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

# ── Depot ID → winetricks verbs ──────────────────────────────────────────────
KNOWN_REDIST_DEPOTS = {
    228981: ["vcrun2005"],
    228982: ["vcrun2008"],
    228983: ["vcrun2010"],
    228984: ["vcrun2012"],
    228985: ["vcrun2013"],
    228986: ["vcrun2015"],
    228987: ["vcrun2017"],
    228988: ["vcrun2019"],
    228989: ["vcrun2022"],
    228990: ["d3dx9", "d3dx10", "d3dcompiler_47"],
    228991: ["openal"],
    228992: ["xact"],
    228993: ["physx"],
    1826330: ["dotnet48"],
    1826331: ["dotnet48"],
}

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR   = Path(__file__).parent.resolve()
ASSETS_DIR   = SCRIPT_DIR / "assets"
OUTPUT_FILE  = ASSETS_DIR / "redists.json"
DEFAULT_DB   = Path.home() / ".config" / "vaultplay" / "vaultplay.db"


# ── SteamCMD ──────────────────────────────────────────────────────────────────

def find_steamcmd() -> str | None:
    return shutil.which("steamcmd")


def query_steamcmd(app_id: int, steamcmd_path: str, timeout: int = 30) -> str | None:
    """
    Run steamcmd +login anonymous +app_info_print <app_id> +quit
    and return stdout as a string, or None on failure.

    Note: steamcmd on Linux sometimes needs app_info_update before
    app_info_print to force a fresh fetch. We include it here.
    """
    cmd = [
        steamcmd_path,
        "+login", "anonymous",
        "+app_info_update", "1",
        "+app_info_print", str(app_id),
        "+quit"
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        # steamcmd writes app_info_print to stdout
        return result.stdout
    except subprocess.TimeoutExpired:
        print("  TIMEOUT after " + str(timeout) + "s")
        return None
    except Exception as e:
        print("  ERROR: " + str(e))
        return None


def parse_redist_depots(steamcmd_output: str) -> list[str]:
    """
    Parse steamcmd app_info_print output and extract redistributable verbs.

    We look for depot IDs that have:
        "depotfromapp"    "228980"   (Steam Common Redistributables app)
        "sharedinstall"   "1"

    These are the depots Steam declares as common redistributables for this game.
    """
    if not steamcmd_output:
        return []

    # Find all depot ID blocks that reference app 228980 (Steam Common Redist)
    # The output format is:
    #   "228987"
    #   {
    #       ...
    #       "depotfromapp"    "228980"
    #       "sharedinstall"   "1"
    #   }
    #
    # Strategy: find lines that look like depot IDs (purely numeric),
    # then check if the following block contains depotfromapp 228980.

    found_verbs = []
    found_depot_ids = []

    lines = steamcmd_output.splitlines()

    # Walk through lines looking for numeric depot IDs
    for i, line in enumerate(lines):
        stripped = line.strip().strip('"')
        try:
            depot_id = int(stripped)
        except ValueError:
            continue

        if depot_id not in KNOWN_REDIST_DEPOTS:
            continue

        # Look ahead in the next 15 lines for depotfromapp 228980
        block = "\n".join(lines[i:i+15])
        if '228980' in block and 'depotfromapp' in block:
            verbs = KNOWN_REDIST_DEPOTS[depot_id]
            for v in verbs:
                if v not in found_verbs:
                    found_verbs.append(v)
            found_depot_ids.append(depot_id)

    if found_depot_ids:
        print("  Depots found: " + str(found_depot_ids) + " -> " + str(found_verbs))
    else:
        print("  No common redist depots declared")

    return found_verbs


# ── Database ──────────────────────────────────────────────────────────────────

def load_games_from_db(db_path: Path) -> list[dict]:
    """
    Return list of {game_id, folder_name, display_name, title, steam_app_id}
    for all games that have a steam_app_id.
    Games without a steam_app_id are included with steam_app_id=None
    so we can report on them.
    """
    if not db_path.exists():
        print("ERROR: Database not found at " + str(db_path))
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT
            g.id            AS game_id,
            g.folder_name,
            g.display_name,
            COALESCE(m.title, g.display_name, g.folder_name) AS title,
            m.steam_app_id
        FROM games g
        LEFT JOIN metadata m ON m.game_id = g.id
        ORDER BY COALESCE(m.title, g.display_name, g.folder_name) COLLATE NOCASE
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db",      type=Path, default=DEFAULT_DB,
                        help="Path to vaultplay.db (default: " + str(DEFAULT_DB) + ")")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print results without writing the output file")
    parser.add_argument("--app-id",  type=int, default=None,
                        help="Query a single Steam app ID (for testing)")
    parser.add_argument("--delay",        type=float, default=1.5,
                        help="Seconds to wait between SteamCMD calls (default: 1.5)")
    parser.add_argument("--retry-missing", action="store_true",
                        help="Only query app IDs in DB that are completely absent from redists.json "
                             "(i.e. errored out in a previous run). Skips already-processed entries.")
    args = parser.parse_args()

    print("\n=== VaultPlay Redistributable Data Generator ===\n")

    # Check SteamCMD
    steamcmd = find_steamcmd()
    if not steamcmd:
        print("ERROR: steamcmd not found in PATH.")
        print("Install with: sudo pacman -S steamcmd")
        sys.exit(1)
    print("SteamCMD: " + steamcmd)
    print("Database: " + str(args.db))
    print("Output:   " + str(OUTPUT_FILE))
    print()

    # Single app ID test mode
    if args.app_id:
        print("=== Single app ID test: " + str(args.app_id) + " ===\n")
        output = query_steamcmd(args.app_id, steamcmd)
        verbs = parse_redist_depots(output)
        print("\nResult: " + str(verbs))
        return

    # Load existing file so we can merge/update rather than clobber
    existing_data = {}
    if OUTPUT_FILE.exists():
        try:
            with open(OUTPUT_FILE) as f:
                existing_data = json.load(f)
            print("Loaded existing redists.json (" + str(len(existing_data)) + " entries)")
        except Exception as e:
            print("Warning: could not load existing file: " + str(e))
    else:
        print("No existing redists.json — creating fresh")
    print()

    # Load games from DB
    games = load_games_from_db(args.db)
    with_steam_id    = [g for g in games if g["steam_app_id"]]
    without_steam_id = [g for g in games if not g["steam_app_id"]]

    print("Games in database:        " + str(len(games)))
    print("With Steam App ID:        " + str(len(with_steam_id)))
    print("Without Steam App ID:     " + str(len(without_steam_id)) + " (skipped)")
    print()

    if without_steam_id:
        print("Skipped (no Steam App ID):")
        for g in without_steam_id[:10]:
            print("  - " + g["title"])
        if len(without_steam_id) > 10:
            print("  ... and " + str(len(without_steam_id) - 10) + " more")
        print()

    # Query SteamCMD for each game
    results = dict(existing_data)  # start from existing data

    # --retry-missing: filter to only app IDs completely absent from the file
    # (empty list [] means we checked and found nothing — that's not a missing entry)
    if args.retry_missing:
        before = len(with_steam_id)
        with_steam_id = [
            g for g in with_steam_id
            if str(g["steam_app_id"]) not in results
        ]
        print("--retry-missing: " + str(before) + " games with Steam IDs, " +
              str(len(with_steam_id)) + " absent from redists.json, " +
              str(before - len(with_steam_id)) + " already processed (skipped)")
        print()
        if not with_steam_id:
            print("Nothing to retry — all Steam App IDs are already in redists.json")
            print("(Empty lists [] mean we checked and found no common redist depots)")
            return

    new_count     = 0
    updated_count = 0
    no_data_count = 0
    error_count   = 0

    for i, game in enumerate(with_steam_id):
        app_id    = game["steam_app_id"]
        app_id_str = str(app_id)
        title     = game["title"]

        progress = "[" + str(i+1) + "/" + str(len(with_steam_id)) + "]"
        print(progress + " " + title + " (app_id=" + str(app_id) + ")")

        output = query_steamcmd(app_id, steamcmd)
        if output is None:
            print("  FAILED — keeping existing data if any")
            error_count += 1
        else:
            verbs = parse_redist_depots(output)
            if verbs:
                old = results.get(app_id_str, [])
                if sorted(old) != sorted(verbs):
                    if old:
                        print("  Updated: " + str(old) + " -> " + str(verbs))
                        updated_count += 1
                    else:
                        new_count += 1
                results[app_id_str] = verbs
            else:
                # Game has no common redist depots — store empty list
                # so we know we checked it (not the same as never checked)
                if app_id_str not in results:
                    results[app_id_str] = []
                no_data_count += 1

        # Respect SteamCMD — don't hammer it
        if i < len(with_steam_id) - 1:
            time.sleep(args.delay)

    print()
    print("=" * 55)
    print("SUMMARY")
    print("=" * 55)
    print("New entries:     " + str(new_count))
    print("Updated entries: " + str(updated_count))
    print("No redist data:  " + str(no_data_count) + " (stored as empty list)")
    print("Errors:          " + str(error_count))
    print("Total in file:   " + str(len(results)))
    print()

    # Show a sample of what we found
    with_verbs = {k: v for k, v in results.items() if v}
    print("Sample entries with redists (" + str(len(with_verbs)) + " total):")
    for app_id_str, verbs in list(with_verbs.items())[:10]:
        print("  " + app_id_str + ": " + str(verbs))
    if len(with_verbs) > 10:
        print("  ... and " + str(len(with_verbs) - 10) + " more")
    print()

    if args.dry_run:
        print("DRY RUN — not writing file")
        return

    # Write output
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(results, f, indent=2, sort_keys=True)
    print("Written: " + str(OUTPUT_FILE))
    size_kb = OUTPUT_FILE.stat().st_size / 1024
    print("Size: " + str(round(size_kb, 1)) + " KB")
    print()
    print("Commit this file and rebuild the AppImage to bundle the updated data.")


if __name__ == "__main__":
    main()
