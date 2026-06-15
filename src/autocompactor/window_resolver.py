#!/usr/bin/env python3
"""Observe-only context-window inference helpers."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os

from autocompactor import config_lib


DEFAULT_TIERS = [200_000, 300_000, 512_000, 1_000_000]
DEFAULT_PROMOTE_FRAC = 0.95
SMALL_SESSION_PEAK = 190_000


@dataclass
class WindowResolution:
    effective_window: float
    configured_window: float
    learned_window: int
    learned_tier: str
    window_source: str
    native_ceiling: int | None = None
    native_ceiling_blocks_learned_window: bool = False
    runtime_context_window: int | None = None
    reserve: int = 0

    def event_fields(self) -> dict:
        return {
            "effective_window": int(self.effective_window),
            "configured_window": int(self.configured_window),
            "learned_window": int(self.learned_window),
            "learned_tier": self.learned_tier,
            "window_source": self.window_source,
            "native_ceiling": self.native_ceiling,
            "native_ceiling_blocks_learned_window":
                self.native_ceiling_blocks_learned_window,
            "runtime_context_window": self.runtime_context_window,
            "reserve": self.reserve,
        }


def _int_or_none(value):
    try:
        if value in (None, ""):
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def tiers(harness: str = "claude") -> list[int]:
    vals = []
    for raw in config_lib.cfg.list(
            "AUTO_WINDOW_TIERS", harness=harness, default=DEFAULT_TIERS):
        try:
            val = int(float(raw))
        except (TypeError, ValueError):
            continue
        if val > 0:
            vals.append(val)
    return sorted(set(vals)) or list(DEFAULT_TIERS)


def promote_frac(harness: str = "claude") -> float:
    try:
        frac = config_lib.cfg.float(
            "AUTO_WINDOW_PROMOTE_FRAC", harness=harness,
            default=DEFAULT_PROMOTE_FRAC)
    except Exception:
        return DEFAULT_PROMOTE_FRAC
    if frac <= 0 or frac > 1:
        return DEFAULT_PROMOTE_FRAC
    return frac


def tier_label(window: int) -> str:
    if window >= 1_000_000 and window % 1_000_000 == 0:
        return f"{window // 1_000_000}m"
    return f"{window // 1000}k"


def _nearest_tier(window: int, tier_values: list[int]) -> int:
    for tier in tier_values:
        if window <= tier:
            return tier
    return tier_values[-1]


def _learned_from_peak(peak: int, tier_values: list[int],
                       frac: float) -> tuple[int, str]:
    if peak > 0 and peak < SMALL_SESSION_PEAK:
        return tier_values[0], "small_session_clamp"
    for tier in tier_values:
        if peak <= tier * frac:
            return tier, "observed_peak"
    return tier_values[-1], "observed_peak"


def native_ceiling_from_settings(path: str = "~/.claude/settings.json") -> int | None:
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as fh:
            env = (json.load(fh).get("env") or {})
    except Exception:
        return None
    return _int_or_none(env.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW"))


def resolve_window(configured_window: float, observed_peak: int,
                   harness: str = "claude",
                   runtime_context_window: int | None = None,
                   reserve: int = 0,
                   native_ceiling: int | None = None) -> WindowResolution:
    configured = max(float(configured_window or 0), 1.0)
    observed = _int_or_none(observed_peak) or 0
    reserve = max(_int_or_none(reserve) or 0, 0)
    runtime = _int_or_none(runtime_context_window)
    native = _int_or_none(native_ceiling)
    if native is not None and native <= 0:
        native = None
    tier_values = tiers(harness)

    if runtime:
        learned = _nearest_tier(runtime, tier_values)
        source = "runtime"
        effective = float(max(runtime - reserve, 1))
    else:
        if observed <= 0:
            learned, source = _nearest_tier(int(configured), tier_values), "configured"
        else:
            learned, source = _learned_from_peak(
                observed, tier_values, promote_frac(harness))
        if source == "small_session_clamp":
            configured_effective = configured
            if harness == "pi":
                configured_effective = max(configured_effective - reserve, 1)
                effective = float(configured_effective)
            else:
                effective = float(min(configured_effective, tier_values[0]))
        else:
            effective = configured
            if harness == "pi":
                effective = float(max(effective - reserve, 1))

    blocks = bool(native and native < learned)
    return WindowResolution(
        effective_window=effective,
        configured_window=configured,
        learned_window=int(learned),
        learned_tier=tier_label(int(learned)),
        window_source=source,
        native_ceiling=native,
        native_ceiling_blocks_learned_window=blocks,
        runtime_context_window=runtime,
        reserve=reserve,
    )
