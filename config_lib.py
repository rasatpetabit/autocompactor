#!/usr/bin/env python3
"""
config_lib.py — unified autocompactor configuration.

Source of truth: config.json in this repo. Env vars (AUTOCOMPACTOR_*,
AUTOCOMPACTOR_PI_*) still override for runtime tuning. Defaults are the
last fallback.

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

# Cached config (loaded once at first use)
_config_cache = None


def _load_config() -> dict:
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    try:
        with open(_CONFIG) as fh:
            _config_cache = json.load(fh)
            # Strip comment keys
            _config_cache = {k: v for k, v in _config_cache.items()
                            if not k.startswith("_")}
    except Exception:
        _config_cache = {}
    return _config_cache


def _env_chain(name: str, harness: str) -> list[str]:
    """Ordered list of env var names to check for a given setting."""
    keys = []
    if harness == "pi":
        keys.append(f"AUTOCOMPACTOR_PI_{name}")
    keys.append(f"AUTOCOMPACTOR_{name}")
    return keys


def _env_chain_windowed(name: str, harness: str, ctx_window: int) -> list[str]:
    """Env chain with _WIDE suffix for large windows."""
    is_wide = ctx_window >= 300_000
    keys = []
    # Window-specific variants first
    for prefix in (f"AUTOCOMPACTOR_{harness.upper()}_", "AUTOCOMPACTOR_"):
        if is_wide:
            keys.append(f"{prefix}{name}_WIDE")
        keys.append(f"{prefix}{name}")
    # De-duplicate while preserving order
    seen = set()
    out = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


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
    # 2. Config file: top-level, then harness-specific overrides
    if name in cfg:
        try:
            return float(cfg[name])
        except (TypeError, ValueError):
            pass
    if harness and harness in cfg:
        hvals = cfg[harness]
        if isinstance(hvals, dict) and name in hvals:
            try:
                return float(hvals[name])
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
        cfg = _load_config()
        # Config file
        if name in cfg and isinstance(cfg[name], str):
            return cfg[name]
        if harness and harness in cfg:
            hvals = cfg[harness]
            if isinstance(hvals, dict) and name in hvals and isinstance(hvals[name], str):
                return hvals[name]
        # Env vars
        for key in _env_chain(name, harness):
            raw = os.environ.get(key)
            if raw:
                return raw
        return default

    @property
    def path(self) -> str:
        return _CONFIG


# Module-level singleton
cfg = Config()
