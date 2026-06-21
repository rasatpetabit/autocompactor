#!/usr/bin/env python3
"""
config_lib.py — autocompactor configuration (single namespace).

Source of truth: config.json in this repo, with site-local values
(LLM endpoints, model names — anything that should not be versioned)
merged over it from config.local.json (gitignored). Env vars
(AUTOCOMPACTOR_*) still override for runtime tuning. Defaults are the
last fallback.

Precedence: env -> config.local.json -> config.json -> default.

Usage in any module:
    from autocompactor.config_lib import cfg
    window = cfg.float("WINDOW")
    hard   = cfg.float("HARD_PCT", default=0.65)
    hard   = cfg.float_windowed("HARD_PCT", ctx_window)

The `harness` keyword on the reader methods is accepted but ignored
(Pi is the sole adapter); it is retained only for call-site compatibility.
"""

from __future__ import annotations

import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))       # .../src/autocompactor
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))        # the checkout root
_CONFIG = os.path.join(_REPO_ROOT, "config.json")
_CONFIG_LOCAL = os.path.join(_REPO_ROOT, "config.local.json")

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
    # Later files overlay earlier ones (config.local.json over config.json).
    merged: dict = {}
    for path in _config_paths():
        for key, val in _read_json(path).items():
            if isinstance(val, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **val}
            else:
                merged[key] = val
    _config_cache = merged
    return _config_cache


def _env_chain(name: str) -> list[str]:
    """Ordered list of env var names to check for a given setting."""
    return [f"AUTOCOMPACTOR_{name}"]


def _env_chain_windowed(name: str, ctx_window: int) -> list[str]:
    """Env chain with _WIDE suffix checked first for wide windows (>=300k)."""
    keys = []
    if ctx_window >= 300_000:
        keys.append(f"AUTOCOMPACTOR_{name}_WIDE")
    keys.append(f"AUTOCOMPACTOR_{name}")
    return keys


def _try_float(name: str, ctx_window: int = 0) -> float | None:
    """Try env chain first (runtime override), then config.json, return None if not found."""
    cfg = _load_config()
    # 1. Env vars (highest priority — runtime tuning, test overrides)
    envs = _env_chain_windowed(name, ctx_window) if ctx_window else _env_chain(name)
    for key in envs:
        raw = os.environ.get(key)
        if raw:
            try:
                return float(raw)
            except ValueError:
                pass
    # 2. Config file. With a wide window (>=300k) the _WIDE variant of the
    #    key wins over the bare name, mirroring the env chain.
    names = [name]
    if ctx_window >= 300_000:
        names.insert(0, f"{name}_WIDE")
    for n in names:
        if n in cfg:
            try:
                return float(cfg[n])
            except (TypeError, ValueError):
                pass
    return None


class Config:
    """Config reader (single namespace).

    Usage:
        from autocompactor.config_lib import cfg
        window = cfg.float("WINDOW")
        hard   = cfg.float("HARD_PCT", default=0.65)

    `harness` is accepted but ignored (Pi is the sole adapter).
    """

    def float(self, name: str, harness: str = "pi",
              default: float = 0.0, ctx_window: int = 0) -> float:
        val = _try_float(name, ctx_window)
        return val if val is not None else default

    def float_windowed(self, name: str, ctx_window: int,
                       harness: str = "pi", default: float = 0.0) -> float:
        """Float with _WIDE suffix auto-selection for ctx_window >= 300k."""
        return self.float(name, default=default, ctx_window=ctx_window)

    def str(self, name: str, harness: str = "pi", default: str = "") -> str:
        # Env vars first (runtime override), matching the float path.
        # Presence wins, not truthiness: an empty string is a deliberate
        # override (e.g. AUTOCOMPACTOR_OBSERVE_ONLY="" clears the list).
        for key in _env_chain(name):
            raw = os.environ.get(key)
            if raw is not None:
                return raw
        cfg = _load_config()
        if name in cfg and isinstance(cfg[name], str):
            return cfg[name]
        return default

    def raw(self, name: str, harness: str = "pi", default=None):
        """Return an uncoerced config value with the standard precedence."""
        for key in _env_chain(name):
            raw = os.environ.get(key)
            if raw is not None:
                return raw
        cfg = _load_config()
        return cfg.get(name, default)

    def list(self, name: str, harness: str = "pi", default=None) -> list:
        raw = self.raw(name, default=default or [])
        if isinstance(raw, list):
            return raw
        if isinstance(raw, str):
            raw = raw.strip()
            if not raw:
                return []
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    return parsed
            except Exception:
                pass
            return [part.strip() for part in raw.split(",") if part.strip()]
        return default or []

    @property
    def path(self) -> str:
        return _CONFIG


# Module-level singleton
cfg = Config()
