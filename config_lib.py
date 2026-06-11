#!/usr/bin/env python3
"""
config_lib.py — unified autocompactor configuration.

Source of truth: config.json in this repo, with site-local values
(LLM endpoints, model names — anything that should not be versioned)
merged over it from config.local.json (gitignored). Env vars
(AUTOCOMPACTOR_*, AUTOCOMPACTOR_PI_*) still override for runtime
tuning. Defaults are the last fallback.

Usage in any module:
    from config_lib import cfg
    window = cfg.float("WINDOW")                # auto-detects harness
    hard  = cfg.float("HARD_PCT", harness="pi") # or explicit
    hard  = cfg.float_windowed("HARD_PCT", ctx_window, harness="pi")
"""

from __future__ import annotations

import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_CONFIG = os.path.join(_HERE, "config.json")
_CONFIG_LOCAL = os.path.join(_HERE, "config.local.json")

# Cached config (loaded once at first use)
_config_cache = None


def _read_json(path: str) -> dict:
    try:
        with open(path) as fh:
            data = json.load(fh)
        return {k: v for k, v in data.items() if not k.startswith("_")}
    except Exception:
        return {}


def _config_paths() -> list[str]:
    """AUTOCOMPACTOR_CONFIG overrides the file set: a path to use instead
    of config.json(+local), or empty string for no config files at all
    (pure env + code defaults — used by hermetic tests)."""
    override = os.environ.get("AUTOCOMPACTOR_CONFIG")
    if override is not None:
        return [override] if override else []
    return [_CONFIG, _CONFIG_LOCAL]


def _load_config() -> dict:
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    # Later files overlay earlier ones (config.local.json over
    # config.json); harness sections merge key-by-key so a local file
    # can override one value without clobbering the whole section.
    merged: dict = {}
    for path in _config_paths():
        for key, val in _read_json(path).items():
            if isinstance(val, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **val}
            else:
                merged[key] = val
    _config_cache = merged
    return _config_cache


def _env_chain(name: str, harness: str) -> list[str]:
    """Ordered list of env var names to check for a given setting."""
    keys = []
    if harness == "pi":
        keys.append(f"AUTOCOMPACTOR_PI_{name}")
    keys.append(f"AUTOCOMPACTOR_{name}")
    return keys


def _env_chain_windowed(name: str, harness: str, ctx_window: int) -> list[str]:
    """Env chain with _WIDE suffix for large windows.

    Same prefixes as _env_chain (only "pi" gets a harness prefix; claude
    uses the bare AUTOCOMPACTOR_ namespace), with the _WIDE variant of
    each key checked first when the window is wide.
    """
    is_wide = ctx_window >= 300_000
    prefixes = []
    if harness == "pi":
        prefixes.append("AUTOCOMPACTOR_PI_")
    prefixes.append("AUTOCOMPACTOR_")
    keys = []
    for prefix in prefixes:
        if is_wide:
            keys.append(f"{prefix}{name}_WIDE")
        keys.append(f"{prefix}{name}")
    return keys


def _try_float(name: str, harness: str, ctx_window: int = 0) -> float | None:
    """Try env chain first (runtime override), then config.json, return None if not found."""
    cfg = _load_config()
    # 1. Env vars (highest priority — runtime tuning, test overrides)
    envs = _env_chain_windowed(name, harness, ctx_window) if ctx_window else _env_chain(name, harness)
    for key in envs:
        raw = os.environ.get(key)
        if raw:
            try:
                return float(raw)
            except ValueError:
                pass
    # 2. Config file: harness-specific overrides win over top-level.
    #    With a wide window (>=300k) the _WIDE variant of the key wins
    #    over the bare name at each level, mirroring the env chain.
    names = [name]
    if ctx_window >= 300_000:
        names.insert(0, f"{name}_WIDE")
    if harness and harness in cfg and isinstance(cfg[harness], dict):
        hvals = cfg[harness]
        for n in names:
            if n in hvals:
                try:
                    return float(hvals[n])
                except (TypeError, ValueError):
                    pass
    for n in names:
        if n in cfg:
            try:
                return float(cfg[n])
            except (TypeError, ValueError):
                pass
    return None


class Config:
    """Unified config reader.

    Usage:
        from config_lib import cfg
        window = cfg.float("WINDOW")
        hard   = cfg.float("HARD_PCT", harness="pi", default=0.65)
    """

    def float(self, name: str, harness: str = "claude",
              default: float = 0.0, ctx_window: int = 0) -> float:
        val = _try_float(name, harness, ctx_window)
        return val if val is not None else default

    def float_windowed(self, name: str, ctx_window: int,
                       harness: str = "claude", default: float = 0.0) -> float:
        """Float with _WIDE suffix auto-selection for ctx_window >= 300k."""
        return self.float(name, harness, default, ctx_window)

    def str(self, name: str, harness: str = "claude", default: str = "") -> str:
        # Env vars first (runtime override), matching the float path.
        # Presence wins, not truthiness: an empty string is a deliberate
        # override (e.g. AUTOCOMPACTOR_OBSERVE_ONLY="" clears the list).
        for key in _env_chain(name, harness):
            raw = os.environ.get(key)
            if raw is not None:
                return raw
        cfg = _load_config()
        # Harness-specific overrides win over top-level.
        if harness and harness in cfg:
            hvals = cfg[harness]
            if isinstance(hvals, dict) and name in hvals and isinstance(hvals[name], str):
                return hvals[name]
        if name in cfg and isinstance(cfg[name], str):
            return cfg[name]
        return default

    @property
    def path(self) -> str:
        return _CONFIG


# Module-level singleton
cfg = Config()
