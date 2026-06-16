#!/usr/bin/env python3
"""
policy.py — unified compaction decision rule (harness-agnostic).

Centralizes the decision that the Claude monitor (context_monitor.py) and the
Pi bridge (pi_bridge.py) both compute inline today. Adapters gather resolved
inputs (context_tokens, effective_limit, whether a non-observe boundary signal
is present, the cooldown baseline) and call ``decide()``; they keep their own
state, instruction staging, and actuation.

This is the foundation of the three-knob masterplan (see
docs/masterplan/simplify-compaction-model/spec.md). It encodes the CURRENT
semantics — binary signal gating, hard/soft thresholds, the min-savings guard,
and the rising-only cooldown — so behavior is at parity. ``PROFILE``-derived
constants are introduced but currently mirror today's defaults; per-adapter
old-key overrides still win (no silent behavior change during migration).

Out of scope here (later gated steps): rewriting the two existing adapter
paths to call ``decide()`` (highest-risk step — done after parity tests and
the Workstream-0 fixes), and weighted boundary scoring.
"""

from __future__ import annotations

from dataclasses import dataclass

from autocompactor import config_lib


# Profile -> derived constants (internal; surfaced in --status). "balanced"
# mirrors today's code defaults so the migration starts at exact parity.
_PROFILES = {
    "economy":  {"soft": 0.32, "hard": 0.50, "cooldown": 15_000, "stale_frac": 0.40},
    "balanced": {"soft": 0.40, "hard": 0.65, "cooldown": 25_000, "stale_frac": 0.50},
    "lazy":     {"soft": 0.55, "hard": 0.80, "cooldown": 40_000, "stale_frac": 0.60},
}


@dataclass
class PolicyConfig:
    profile: str
    mode: str
    soft: float
    hard: float
    cooldown: float
    stale_frac: float
    post_floor: float
    min_savings: float
    effective_limit: int


@dataclass
class PolicyInput:
    context_tokens: int
    effective_limit: int
    gating: bool                     # >=1 non-observe boundary signal present
    last_reco_tokens: int = -10**9   # cooldown baseline (adapter-owned, passed in)
    harness: str = "claude"


@dataclass
class PolicyDecision:
    recommend: bool
    suppressed_by_cooldown: bool
    occupancy: float
    est_reclaim: int
    reason: str
    config: PolicyConfig


def profile_names() -> list:
    return sorted(_PROFILES)


def resolve_policy_config(harness: str, effective_limit: int,
                          profile: str = "") -> PolicyConfig:
    """Build a PolicyConfig for this harness + effective limit.

    PROFILE selects the derived-constant base; deprecated per-key overrides
    (SOFT_PCT/HARD_PCT/COOLDOWN/STALE_FRAC/POST_FLOOR/MIN_SAVINGS) win over
    the profile base via the standard config_lib precedence (env > harness
    section > top-level > default), so existing tuned installs are unchanged.
    """
    requested = profile or config_lib.cfg.str(
        "PROFILE", harness=harness, default="balanced") or "balanced"
    base = _PROFILES.get(requested, _PROFILES["balanced"])
    pname = requested if requested in _PROFILES else "balanced"
    soft = config_lib.cfg.float("SOFT_PCT", harness=harness, default=base["soft"])
    hard = config_lib.cfg.float("HARD_PCT", harness=harness, default=base["hard"])
    cooldown = config_lib.cfg.float("COOLDOWN", harness=harness, default=base["cooldown"])
    stale_frac = config_lib.cfg.float("STALE_FRAC", harness=harness, default=base["stale_frac"])
    post_floor = config_lib.cfg.float("POST_FLOOR", harness=harness, default=70_000)
    min_savings = config_lib.cfg.float("MIN_SAVINGS", harness=harness, default=30_000)
    mode = config_lib.cfg.str("MODE", harness=harness, default="advise") or "advise"
    return PolicyConfig(pname, mode, soft, hard, cooldown, stale_frac,
                        post_floor, min_savings, max(int(effective_limit), 1))


def decide(inp: PolicyInput, cfg: PolicyConfig) -> PolicyDecision:
    """The unified recommendation rule.

    recommend when:
      est_reclaim >= min_savings
      AND (occupancy >= hard OR (occupancy >= soft AND a gating signal))
      AND not cooldown-suppressed (rising-only; a shrink resets the baseline)
    """
    eff = max(int(cfg.effective_limit), 1)
    occupancy = inp.context_tokens / eff

    # Cooldown debounces RISING context only (issue #1): a context that has
    # shrunk below the last staging point has more room, so reset it.
    last = inp.last_reco_tokens
    if inp.context_tokens < last:
        last = -10**9
    suppressed = 0 <= (inp.context_tokens - last) < cfg.cooldown

    est_reclaim = int(inp.context_tokens - cfg.post_floor)
    want = (occupancy >= cfg.hard
            or (occupancy >= cfg.soft and bool(inp.gating)))
    blocked_by_savings = est_reclaim < cfg.min_savings
    recommend = bool(want and not blocked_by_savings)

    reason = (f"context at {occupancy:.0%} of ~{eff:,} tokens "
              f"(profile={cfg.profile})")
    if inp.gating and occupancy < cfg.hard:
        reason += " with a boundary signal"
    if blocked_by_savings:
        reason += (f"; est. reclaim {est_reclaim:,} below the "
                   f"{int(cfg.min_savings):,}-token minimum")
    if recommend and suppressed:
        reason += " (suppressed: cooldown)"

    return PolicyDecision(
        recommend=recommend and not suppressed,
        suppressed_by_cooldown=bool(recommend and suppressed),
        occupancy=occupancy,
        est_reclaim=est_reclaim,
        reason=reason,
        config=cfg,
    )
