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


def pct_override_from_settings(path: str = "~/.claude/settings.json") -> int | None:
    """CLAUDE_AUTOCOMPACT_PCT_OVERRIDE — the % of the native ceiling at which
    Claude's own autocompact fires. Used only to estimate the native-auto
    trigger for the human-facing readout; never feeds the decision."""
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as fh:
            env = (json.load(fh).get("env") or {})
    except Exception:
        return None
    pct = _int_or_none(env.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"))
    if pct is None or not (0 < pct <= 100):
        return None
    return pct


def native_auto_estimate(native_ceiling: int | None,
                         pct_override: int | None) -> int | None:
    """Best estimate of where native autocompact actually fires:
    native_ceiling × pct_override%. Returns None when the ceiling is unknown
    (then the readout simply omits the native-auto anchor)."""
    if not native_ceiling or native_ceiling <= 0:
        return None
    pct = pct_override if (pct_override and 0 < pct_override <= 100) else 90
    return int(native_ceiling * pct / 100)


def readout_anchors(resolution: "WindowResolution",
                    settings_path: str = "~/.claude/settings.json"
                    ) -> tuple[int | None, int | None]:
    """(native_auto_estimate, confident_model_window) for the human readout.

    native_auto: where Claude's own autocompact will force a compaction.
    model_window: shown ONLY when the tier was learned from an observed peak
    that exceeds the native ceiling — i.e. we have empirically seen context
    larger than the enforced wall, proving a bigger model. Otherwise omitted,
    because Claude cannot see the live model window and a guessed tier (e.g.
    the 200k small-session clamp on a 1M model) would itself misstate the
    limit — the exact failure this readout exists to fix."""
    native_auto = native_auto_estimate(
        resolution.native_ceiling, pct_override_from_settings(settings_path))
    model_window = None
    if (resolution.window_source in ("observed_peak", "runtime")
            and resolution.learned_window > (resolution.native_ceiling or 0)):
        model_window = resolution.learned_window
    return native_auto, model_window


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
            # Claude/Pi asymmetry here is BY DESIGN, not a bug. Claude cannot
            # see the live model window, so it infers the tier from observed
            # peak and clamps a 1M-tuned configured window down to the 200k
            # tier for a small session. Pi's effective window is the *exact*
            # contextWindow - reserve (HANDOFF: "Pi effective window"); the
            # live window is authoritative on the evaluate path, so the tier
            # clamp is neither needed nor wanted. This branch is only reached
            # on Pi's fire-and-forget prepare overview (no runtime window
            # passed), where configured - reserve is the right fallback.
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

    # Claude blind spot: it cannot see the live model window, so it inferred a
    # tier from observed peak. CLAUDE_CODE_AUTO_COMPACT_WINDOW (native_ceiling)
    # is the one hard fact about the enforced wall — and the tier inference was
    # provably wrong (miss-attribution.md: inferred 512k when native was 500k).
    # Cap the effective window at native_ceiling so it can never exceed the
    # enforced reality. This is a CAP, not a replacement: an owner who set a
    # tighter WINDOW (aggressive) keeps it; native_ceiling only binds when the
    # inferred/configured window would exceed the enforced ceiling (over-
    # inference, or a small model: a 64k/128k ceiling correctly caps down).
    # Pi is unaffected (its runtime context window is authoritative).
    if harness != "pi" and native and effective > native:
        effective = float(native)
        if source in ("configured", "observed_peak", "small_session_clamp"):
            source = "native_ceiling_capped"
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
