"""
launch_options.py — Per-game launch option overrides for VaultPlay

Spec: Notion → Features → Fully Planned → Launch Options.

Storage model
-------------
game_state.launch_options holds a JSON blob containing ONLY the keys that
differ from the STATIC/computed defaults below — never a full snapshot.
NULL = no overrides at all. On read, get_effective_options() computes the
real defaults (some of which are hardware-dependent — GameMode's default
is "ON if installed"), then merges the stored overrides on top.

This module owns three responsibilities:
  1. The option schema + static/computed defaults (OPTION_DEFAULTS,
     resolve_defaults(), get_effective_options())
  2. Hardware/tooling detection used both to compute defaults and to grey
     out / hide options in the UI (gamemode_installed(), etc.)
  3. Turning a resolved options dict into actual launch-command fragments
     (build_command()) — consumed by installer.py at install time and by
     regenerate_launch() when the Cogwheel changes something.

Nothing in here talks to Qt — this is pure data/logic so both
ui/cogwheel_menu.py and ui/install_dialog.py can share it.
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

import logging
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

import db

log = logging.getLogger(__name__)


# ── Option keys ────────────────────────────────────────────────────────────────
# Every key that can appear in the overrides JSON. Kept as a frozenset so
# get_effective_options() can drop any stale/unknown key from an older
# build without raising, and so set_effective_options() can validate input.

OPTION_KEYS = frozenset({
    "dxvk", "vkd3d",
    "dxvk_hud", "mangohud",
    "gamemode",
    "fsr", "fsr_sharpness",
    "disable_sync",
    "audio_latency_fix", "audio_latency_ms",
    "nvidia_prime", "dri_prime",
    "large_address_aware", "use_wined3d",
    "engine_args",      # dict: {"-nointro": bool, ...}
    "env_vars",          # free text: "KEY=VALUE KEY2=VALUE2"
    "launch_args",       # free text: "-arg1 -arg2"
})

# Static defaults — everything EXCEPT gamemode, which is computed (see
# resolve_defaults()) because its default depends on whether it's installed.
_STATIC_DEFAULTS = {
    "dxvk":                 False,
    "vkd3d":                False,
    "dxvk_hud":              False,
    "mangohud":              False,
    "fsr":                   False,
    "fsr_sharpness":         2,
    "disable_sync":          False,
    "audio_latency_fix":     False,
    "audio_latency_ms":      60,
    "nvidia_prime":          False,
    "dri_prime":             False,
    "large_address_aware":   False,
    "use_wined3d":           False,
    "engine_args":           {},
    "env_vars":              "",
    "launch_args":           "",
}

# Mutually-exclusive groups — enforced by the UI (checking one unchecks the
# other), documented here so command-building has a single source of truth
# for "what happens if both somehow ended up ON" (first-listed wins).
RENDERER_GROUP = ("dxvk", "vkd3d")
OVERLAY_GROUP  = ("dxvk_hud", "mangohud")

# Engine argument matrix — flag → set of engines it applies to.
# Mirrors the spec's table (Unreal 4/5, Source/Source 2, CryEngine).
ENGINE_ARG_MATRIX = {
    "-nointro":    {"unreal", "source"},
    "-dx9":        {"unreal", "source", "cryengine"},
    "-dx11":       {"unreal", "cryengine"},
    "-dx12":       {"unreal", "cryengine"},
    "-vulkan":     {"unreal", "source"},
    "-windowed":   {"unreal", "source"},
    "-fullscreen": {"unreal"},
    "-noborder":   {"source"},
    "-gl":         {"source"},
}

_ENGINE_ALIASES = {
    "unreal engine": "unreal", "unreal 4": "unreal", "unreal 5": "unreal",
    "unreal engine 4": "unreal", "unreal engine 5": "unreal", "ue4": "unreal", "ue5": "unreal",
    "source": "source", "source engine": "source", "source 2": "source",
    "cryengine": "cryengine", "cry engine": "cryengine",
}


def normalize_engine(raw: Optional[str]) -> Optional[str]:
    """Map an IGDB-style free-text engine name to one of our matrix keys, or None."""
    if not raw:
        return None
    key = raw.strip().lower()
    return _ENGINE_ALIASES.get(key)


def engine_args_for(engine: Optional[str]) -> list[str]:
    """Return the ordered list of -flag names applicable to this engine."""
    if not engine:
        return []
    return [flag for flag, engines in ENGINE_ARG_MATRIX.items() if engine in engines]


# ── Hardware / tooling detection ─────────────────────────────────────────────
# Cached briefly (process lifetime is short-lived per dialog open, so a
# simple TTL cache avoids re-shelling-out on every widget rebuild without
# risking a stale answer across app restarts).

_detect_cache: dict = {}
_DETECT_TTL = 60.0


def _cached(key: str, fn):
    entry = _detect_cache.get(key)
    if entry and (time.monotonic() - entry[1]) < _DETECT_TTL:
        return entry[0]
    value = fn()
    _detect_cache[key] = (value, time.monotonic())
    return value


def gamemode_installed() -> bool:
    return _cached("gamemode", lambda: shutil.which("gamemoderun") is not None)


def mangohud_installed() -> bool:
    return _cached("mangohud", lambda: shutil.which("mangohud") is not None)


def detect_gpus() -> list[dict]:
    """
    Return a list of {'vendor': 'nvidia'|'amd'|'intel'|'other', 'name': str}
    for detected GPUs, via `lspci`. Best-effort — an empty list just means
    the GPU-selection options stay hidden, never a crash.
    """
    def _scan():
        lspci = shutil.which("lspci")
        if not lspci:
            return []
        try:
            out = subprocess.run([lspci, "-nn"], capture_output=True,
                                 text=True, timeout=5).stdout
        except Exception:
            return []
        gpus = []
        for line in out.splitlines():
            if not re.search(r"\b(VGA compatible controller|3D controller|Display controller)\b", line):
                continue
            low = line.lower()
            if "nvidia" in low:
                vendor = "nvidia"
            elif "amd" in low or "ati" in low or "advanced micro devices" in low:
                vendor = "amd"
            elif "intel" in low:
                vendor = "intel"
            else:
                vendor = "other"
            name = line.split(": ", 1)[-1].strip()
            gpus.append({"vendor": vendor, "name": name})
        return gpus
    return _cached("gpus", _scan)


def has_dual_gpu_nvidia() -> bool:
    """True when an Nvidia GPU AND at least one other GPU are both present."""
    gpus = detect_gpus()
    vendors = {g["vendor"] for g in gpus}
    return "nvidia" in vendors and len(gpus) >= 2


def has_dual_gpu_amd_intel() -> bool:
    """True when a discrete AMD GPU AND an Intel integrated GPU are both present."""
    gpus = detect_gpus()
    vendors = {g["vendor"] for g in gpus}
    return "amd" in vendors and "intel" in vendors


def detect_audio_server() -> str:
    """Return 'pipewire', 'pulseaudio', or 'unknown', via a process scan."""
    def _scan():
        pgrep = shutil.which("pgrep")
        if not pgrep:
            return "unknown"
        try:
            for name, label in (("pipewire", "pipewire"), ("pulseaudio", "pulseaudio")):
                r = subprocess.run([pgrep, "-x", name], capture_output=True,
                                   text=True, timeout=3)
                if r.returncode == 0:
                    return label
        except Exception:
            pass
        return "unknown"
    return _cached("audio_server", _scan)


# ── Defaults resolution + merge ──────────────────────────────────────────────

def resolve_defaults() -> dict:
    """
    Build the actual default dict for THIS machine, right now — static
    defaults plus the one hardware-computed default (GameMode ON iff
    installed). Never mutate the module-level _STATIC_DEFAULTS.
    """
    defaults = dict(_STATIC_DEFAULTS)
    defaults["gamemode"] = gamemode_installed()
    return defaults


def get_effective_options(game_id: int) -> dict:
    """
    Return the fully-resolved options dict for a game: defaults with the
    game's stored overrides merged on top. Unknown/stale keys in the
    stored JSON (e.g. from a removed option in a future version) are
    silently dropped rather than polluting the effective dict.
    """
    effective = resolve_defaults()
    overrides = db.get_launch_option_overrides(game_id)
    for key, value in overrides.items():
        if key in OPTION_KEYS:
            effective[key] = value
    return effective


def save_effective_options(game_id: int, effective: dict):
    """
    Diff `effective` against this machine's current defaults and persist
    ONLY the differing keys — this is what keeps the stored blob a true
    "overrides only" diff rather than a full-state dump. Called by the
    Cogwheel/Install dialog's Apply/Save action with whatever the widgets
    currently show.
    """
    defaults = resolve_defaults()
    overrides = {}
    for key in OPTION_KEYS:
        if key not in effective:
            continue
        value = effective[key]
        default_value = defaults.get(key)
        if key == "engine_args":
            # Only store checked flags — an engine_args dict of all-False
            # is equivalent to "no overrides" for this key.
            checked = {k: v for k, v in (value or {}).items() if v}
            if checked:
                overrides[key] = checked
            continue
        if value != default_value:
            overrides[key] = value
    db.set_launch_option_overrides(game_id, overrides)


def reset_to_defaults(game_id: int):
    """Wipe all stored overrides for a game — Cogwheel's Reset to Defaults button."""
    db.set_launch_option_overrides(game_id, {})


# ── Command construction ─────────────────────────────────────────────────────

def _resolve_mutual_exclusion(options: dict, group: tuple, keep_first_if_both: bool = True) -> dict:
    """Return a shallow copy of options with at most one of `group` truthy."""
    out = dict(options)
    truthy = [k for k in group if out.get(k)]
    if len(truthy) > 1:
        winner = truthy[0] if keep_first_if_both else truthy[-1]
        for k in truthy:
            if k != winner:
                out[k] = False
    return out


def build_command(options: dict, *, is_proton: bool, engine: Optional[str] = None) -> dict:
    """
    Translate a resolved options dict into command fragments:

        {
            "env":            {KEY: VALUE, ...},   # extra env vars to set
            "wrap_gamemode":  bool,                 # prefix with gamemoderun
            "args":           [str, ...],           # appended after the exe
        }

    Rules from spec, all enforced here so callers never have to re-derive
    them:
      - Renderer group (dxvk/vkd3d) and overlay group (dxvk_hud/mangohud)
        are mutually exclusive — if both are somehow ON, the first wins.
      - DXVK_HUD only takes effect if DXVK is also on (spec: "only works
        when DXVK is also enabled") — silently omitted otherwise.
      - MangoHud/GameMode are only included if actually installed on this
        machine, regardless of what's stored as ON in the JSON.
      - Proton-only options (Large Address Aware, Use WineD3D) are
        silently omitted when is_proton is False.
      - Engine-detected args only include flags valid for the resolved
        engine, and only the ones checked ON.
    """
    options = _resolve_mutual_exclusion(options, RENDERER_GROUP)
    options = _resolve_mutual_exclusion(options, OVERLAY_GROUP)

    env: dict = {}
    args: list = []

    # ── Renderer ──────────────────────────────────────────────────────────
    # Proton ships DXVK/VKD3D-Proton by default — these are best-effort
    # signals for the rarer case of an explicit toggle (e.g. a plain-Wine
    # prefix with DXVK/VKD3D installed separately via winetricks/DXVK's
    # own installer). No env var is strictly required on Proton for
    # either; the flags mainly matter for the "Use Software D3D" opt-out
    # below and for plain Wine.
    if options.get("vkd3d") and not is_proton:
        env["VKD3D_CONFIG"] = "dxr"  # best-effort plain-Wine VKD3D-Proton hint

    # ── Overlay ───────────────────────────────────────────────────────────
    if options.get("dxvk_hud") and options.get("dxvk"):
        env["DXVK_HUD"] = "fps,gpuload,version"
    if options.get("mangohud") and mangohud_installed():
        env["MANGOHUD"] = "1"

    # ── GameMode ──────────────────────────────────────────────────────────
    wrap_gamemode = bool(options.get("gamemode")) and gamemode_installed()

    # ── FSR ───────────────────────────────────────────────────────────────
    if options.get("fsr"):
        env["WINE_FULLSCREEN_FSR"] = "1"
        sharpness = options.get("fsr_sharpness", 2)
        env["WINE_FULLSCREEN_FSR_STRENGTH"] = str(sharpness)

    # ── Sync ──────────────────────────────────────────────────────────────
    if options.get("disable_sync"):
        env["WINEFSYNC"] = "0"
        env["WINEESYNC"] = "0"
        if is_proton:
            env["PROTON_NO_FSYNC"] = "1"
            env["PROTON_NO_ESYNC"] = "1"

    # ── Audio latency ─────────────────────────────────────────────────────
    if options.get("audio_latency_fix"):
        ms = options.get("audio_latency_ms", 60)
        server = detect_audio_server()
        if server == "pipewire":
            env["PIPEWIRE_LATENCY"] = f"{ms}/48000"
        else:
            env["STAGING_AUDIO_LATENCY"] = str(ms)

    # ── GPU selection ─────────────────────────────────────────────────────
    if options.get("nvidia_prime") and has_dual_gpu_nvidia():
        env["__NV_PRIME_RENDER_OFFLOAD"] = "1"
        env["__GLX_VENDOR_LIBRARY_NAME"] = "nvidia"
    if options.get("dri_prime") and has_dual_gpu_amd_intel():
        env["DRI_PRIME"] = "1"

    # ── Proton-only ───────────────────────────────────────────────────────
    if is_proton:
        if options.get("large_address_aware"):
            env["PROTON_FORCE_LARGE_ADDRESS_AWARE"] = "1"
        if options.get("use_wined3d"):
            env["PROTON_USE_WINED3D"] = "1"

    # ── Engine args ───────────────────────────────────────────────────────
    engine_args = options.get("engine_args") or {}
    for flag in engine_args_for(engine):  # preserve matrix order
        if engine_args.get(flag):
            args.append(flag)

    # ── Free-text env vars ────────────────────────────────────────────────
    raw_env = (options.get("env_vars") or "").strip()
    if raw_env:
        try:
            for token in shlex.split(raw_env):
                if "=" in token:
                    k, _, v = token.partition("=")
                    if k:
                        env[k] = v
        except ValueError:
            log.warning("launch_options: could not parse env_vars free-text: %r", raw_env)

    # ── Free-text launch arguments (always last) ─────────────────────────
    raw_args = (options.get("launch_args") or "").strip()
    if raw_args:
        try:
            args.extend(shlex.split(raw_args))
        except ValueError:
            args.extend(raw_args.split())

    return {"env": env, "wrap_gamemode": wrap_gamemode, "args": args}


def apply_to_launch_cmd(base_cmd: str, options: dict, *, is_proton: bool,
                        engine: Optional[str] = None) -> str:
    """
    Take an already-constructed shell launch command (either the raw
    installer.py-built `env WINEPREFIX=... wine/proton "exe"` string, or a
    winemenubuilder-derived Exec= line) and weave in this game's launch
    options, per the spec's construction order:

        [env vars from checkboxes] [env vars free-text]
        wine/proton [exe path]
        [game arg checkboxes] [launch arguments free-text]

    with GameMode wrapping the entire result:  gamemoderun [rest]

    Both known base_cmd shapes already end in the exe invocation with
    nothing after it, so appending args at the very end of the string is
    equivalent to appending them right after the exe token in both cases.
    """
    frags = build_command(options, is_proton=is_proton, engine=engine)

    cmd = base_cmd

    # Extra args go at the very end.
    if frags["args"]:
        quoted = " ".join(shlex.quote(a) if not a.startswith("-") else a
                          for a in frags["args"])
        cmd = f"{cmd} {quoted}"

    # Extra env vars: fold into an existing leading "env ..." prefix if
    # present, otherwise add our own "env ..." prefix.
    if frags["env"]:
        env_pairs = " ".join(f'{k}="{v}"' for k, v in frags["env"].items())
        if cmd.lstrip().startswith("env "):
            cmd = re.sub(r"^\s*env\s+", f"env {env_pairs} ", cmd, count=1)
        else:
            cmd = f"env {env_pairs} {cmd}"

    if frags["wrap_gamemode"]:
        cmd = f"gamemoderun {cmd}"

    return cmd
