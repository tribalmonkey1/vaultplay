"""
gamematch.py — shared helpers for matching a VaultPlay game to an entry in an
outside metadata database (IGDB, RAWG): title normalisation and the mapping
from a library category folder ("PS2", "3ds", "Switch"…) to each service's
notion of a platform.

Platform IDs / names below were written from memory of IGDB's and RAWG's
platform lists, NOT checked against the live APIs (no network access when
this was written). They are all in the one PLATFORMS table so a wrong entry
is a one-line fix — and every caller falls back to an unfiltered search when
a platform-filtered one finds nothing, so a wrong ID costs accuracy, never a
missing result. "PC" is intentionally not mapped: PC games are searched
unfiltered (Windows/DOS/Mac/Linux releases are too varied to filter on).
"""

import re
from typing import Optional

# canonical key -> {"igdb": IGDB platform id, "rawg": [acceptable RAWG platform names]}
PLATFORMS = {
    "ps1":       {"igdb": 7,   "rawg": ["PlayStation"]},
    "ps2":       {"igdb": 8,   "rawg": ["PlayStation 2"]},
    "ps3":       {"igdb": 9,   "rawg": ["PlayStation 3"]},
    "ps4":       {"igdb": 48,  "rawg": ["PlayStation 4"]},
    "ps5":       {"igdb": 167, "rawg": ["PlayStation 5"]},
    "psp":       {"igdb": 38,  "rawg": ["PSP"]},
    "vita":      {"igdb": 46,  "rawg": ["PS Vita"]},
    "3ds":       {"igdb": 37,  "rawg": ["Nintendo 3DS"]},
    "nds":       {"igdb": 20,  "rawg": ["Nintendo DS"]},
    "switch":    {"igdb": 130, "rawg": ["Nintendo Switch"]},
    "gamecube":  {"igdb": 21,  "rawg": ["GameCube"]},
    "wii":       {"igdb": 5,   "rawg": ["Wii"]},
    "wiiu":      {"igdb": 41,  "rawg": ["Wii U"]},
    "n64":       {"igdb": 4,   "rawg": ["Nintendo 64"]},
    "snes":      {"igdb": 19,  "rawg": ["SNES"]},
    "nes":       {"igdb": 18,  "rawg": ["NES"]},
    "gba":       {"igdb": 24,  "rawg": ["Game Boy Advance"]},
    "gb":        {"igdb": 33,  "rawg": ["Game Boy"]},
    "gbc":       {"igdb": 22,  "rawg": ["Game Boy Color"]},
    "genesis":   {"igdb": 29,  "rawg": ["Genesis", "SEGA Genesis", "SEGA Mega Drive"]},
    "dreamcast": {"igdb": 23,  "rawg": ["Dreamcast", "SEGA Dreamcast"]},
    "saturn":    {"igdb": 32,  "rawg": ["SEGA Saturn", "Saturn"]},
    "xbox":      {"igdb": 11,  "rawg": ["Xbox"]},
    "x360":      {"igdb": 12,  "rawg": ["Xbox 360"]},
    "xone":      {"igdb": 49,  "rawg": ["Xbox One"]},
}

# normalised category-folder spellings -> canonical key
_ALIASES = {
    "ps1": "ps1", "psx": "ps1", "psone": "ps1", "playstation": "ps1", "playstation1": "ps1",
    "ps2": "ps2", "playstation2": "ps2",
    "ps3": "ps3", "playstation3": "ps3",
    "ps4": "ps4", "playstation4": "ps4",
    "ps5": "ps5", "playstation5": "ps5",
    "psp": "psp", "playstationportable": "psp",
    "vita": "vita", "psvita": "vita", "playstationvita": "vita",
    "3ds": "3ds", "n3ds": "3ds", "nintendo3ds": "3ds",
    "ds": "nds", "nds": "nds", "nintendods": "nds",
    "switch": "switch", "nsw": "switch", "nintendoswitch": "switch",
    "gamecube": "gamecube", "gc": "gamecube", "ngc": "gamecube", "nintendogamecube": "gamecube",
    "wii": "wii", "wiiu": "wiiu",
    "n64": "n64", "nintendo64": "n64",
    "snes": "snes", "sfc": "snes", "supernintendo": "snes",
    "nes": "nes", "famicom": "nes",
    "gba": "gba", "gameboyadvance": "gba",
    "gb": "gb", "gameboy": "gb",
    "gbc": "gbc", "gameboycolor": "gbc",
    "genesis": "genesis", "megadrive": "genesis", "segagenesis": "genesis",
    "dreamcast": "dreamcast", "dc": "dreamcast",
    "saturn": "saturn", "segasaturn": "saturn",
    "xbox": "xbox", "originalxbox": "xbox",
    "xbox360": "x360", "x360": "x360",
    "xboxone": "xone", "xone": "xone",
}

_EDITION_WORDS = {
    "edition", "goty", "remastered", "remaster", "definitive", "complete",
    "deluxe", "ultimate", "enhanced", "gold", "anniversary", "hd",
}


def norm_title(t: str) -> str:
    """Loose comparison key: lowercase, drop (..)/[..] tags, '&'→'and',
    punctuation out, leading 'the' and edition words ('GOTY', 'Remastered'…)
    out. Two titles that normalise equal are treated as the same game."""
    t = (t or "").lower()
    t = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", t)
    t = t.replace("&", " and ").replace("game of the year", " ")
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    words = [w for w in t.split() if w not in _EDITION_WORDS]
    if words and words[0] == "the":
        words = words[1:]
    return " ".join(words)


def platform_key(category: Optional[str]) -> Optional[str]:
    """Library category folder name → canonical platform key, or None
    ("PC", unknown folders, blank)."""
    if not category:
        return None
    return _ALIASES.get(re.sub(r"[^a-z0-9]+", "", category.lower()))


def igdb_platform_id(category: Optional[str]) -> Optional[int]:
    key = platform_key(category)
    return PLATFORMS[key]["igdb"] if key else None


def rawg_platform_names(category: Optional[str]) -> list:
    key = platform_key(category)
    return list(PLATFORMS[key]["rawg"]) if key else []
