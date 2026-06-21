#!/usr/bin/env python3
"""Observe-only context-window inference helpers."""

from __future__ import annotations

from dataclasses import dataclass

from autocompactor import config_lib


DEFAULT_TIERS = [200_000, 300_000, 512_000, 1_000_000]


@dataclass
class WindowResolution:
    effective_window: float
    configured_window: float
    learned_window: int
    learned_tier: str
    window_source: str
    runtime_context_window: int | None = None
    reserve: int = 0

    def event_fields(self) -> dict:
        return {
            "effective_window": int(self.effective_window),
            "configured_window": int(self.configured_window),
            "learned_window": int(self.learned_window),
            "learned_tier": self.learned_tier,
            "window_source": self.window_source,
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
            "AUTO_WINDOW_TIERS", default=DEFAULT_TIERS):
        try:
            val = int(float(raw))
        except (TypeError, ValueError):
            continue
        if val > 0:
            vals.append(val)
    return sorted(set(vals)) or list(DEFAULT_TIERS)


def tier_label(window: int) -> str:
    if window >= 1_000_000 and window % 1_000_000 == 0:
        return f"{window // 1_000_000}m"
    return f"{window // 1000}k"


def _nearest_tier(window: int, tier_values: list[int]) -> int:
    for tier in tier_values:
        if window <= tier:
            return tier
    return tier_values[-1]


def resolve_window(configured_window: float, observed_peak: int,
                   runtime_context_window: int | None = None,
                   reserve: int = 0) -> WindowResolution:
    configured = max(float(configured_window or 0), 1.0)
    reserve = max(_int_or_none(reserve) or 0, 0)
    runtime = _int_or_none(runtime_context_window)
    tier_values = tiers("pi")
    if runtime:
        learned, source = _nearest_tier(runtime, tier_values), "runtime"
        effective = float(max(runtime - reserve, 1))
    else:
        learned, source = _nearest_tier(int(configured), tier_values), "configured"
        effective = float(max(configured - reserve, 1))
    return WindowResolution(
        effective_window=effective, configured_window=configured,
        learned_window=int(learned), learned_tier=tier_label(int(learned)),
        window_source=source, runtime_context_window=runtime, reserve=reserve)
