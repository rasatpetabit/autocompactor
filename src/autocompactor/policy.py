#!/usr/bin/env python3
"""
policy.py — unified compaction decision rule (harness-agnostic).

Two kinds of helper live here:

1. LIVE (consumed by pi_bridge): ``readout_line``, ``composition_line``,
   ``skill_warning`` — the anchored, content-free display strings.

2. RESERVED / NOT WIRED: the ``decide()`` decision API plus ``target_tokens``,
   ``resolve_policy_config``, ``PolicyConfig``/``PolicyInput``/``PolicyDecision``.
   These are the foundation of the three-knob masterplan (see
   docs/masterplan/simplify-compaction-model/spec.md): a single ``decide()`` the
   Pi bridge would call instead of computing soft/hard inline. They encode the
   CURRENT semantics (binary signal gating, hard/soft thresholds, min-savings
   guard, rising-only cooldown) at parity, but NO live path calls them yet —
   pi_bridge.cmd_evaluate still derives SOFT/HARD via cfg.float_windowed inline.
   Kept deliberately as staged work; the gated rewrite (highest-risk step) lands
   after parity tests. Do not mistake this for accidental dead code.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from autocompactor import config_lib


# Profile -> derived constants (internal; surfaced in --status). "balanced"
# mirrors today's code defaults so the migration starts at exact parity.
# `soft` here is the FALLBACK fraction used only when the window-aware
# target curve cannot apply (or an explicit SOFT_PCT override is set); the
# live SOFT line is target_tokens/effective_limit (see target_tokens()).
_PROFILES = {
    "economy":  {"soft": 0.32, "hard": 0.50, "cooldown": 15_000, "stale_frac": 0.40, "a": 130},
    "balanced": {"soft": 0.40, "hard": 0.65, "cooldown": 25_000, "stale_frac": 0.50, "a": 188},
    "lazy":     {"soft": 0.55, "hard": 0.80, "cooldown": 40_000, "stale_frac": 0.60, "a": 266},
}


# Window-size-aware SOFT (boundary-gated) target curve. See
# docs/masterplan/simplify-compaction-model/window-aware.md.
#
# The post-compaction floor F (~69k, measured) is WINDOW-INDEPENDENT: it is
# system prompt + tool schemas + the injected summary, which do not shrink
# for a smaller model. So a small window is almost entirely floor (little to
# reclaim) while a large window is mostly headroom. target(W) therefore grows
# sub-linearly: a coding task's live working set does not grow 4x just because
# the window did. Reserve-independent -> safe to adopt before ceiling(W)
# (which needs the measured native-auto reserve; see window-aware.md caveat).
#
# The interim ceiling is the current HARD line (hard_pct * W): it keeps the
# SOFT target strictly below HARD so the SOFT->HARD band never inverts until
# the proper ceiling(W) lands.
def target_tokens(effective_window, profile: str,
                  post_floor: float, min_savings: float,
                  hard_pct: float) -> int:
    """Absolute SOFT-line target in tokens for this window + profile."""
    W = max(int(effective_window or 0), 0)
    F = max(float(post_floor), 0.0)
    MS = max(float(min_savings), 0.0)
    if W <= F:
        return W                       # at/below the floor: target = window
    a = _PROFILES.get(profile, _PROFILES["balanced"])["a"]
    curve = F + a * math.sqrt(W - F)
    interim_ceiling = hard_pct * W - MS   # stay below the HARD line
    target = min(curve, interim_ceiling)
    target = max(target, F + MS)          # actionable floor
    return int(target)


def _fmt_tokens(n) -> str:
    """Compact token count for human-facing readouts: 270000 -> '270k'."""
    n = int(n or 0)
    if n >= 1_000_000:
        v = n / 1_000_000
        return f"{v:.0f}m" if abs(v - round(v)) < 0.05 else f"{v:.1f}m"
    if n >= 1000:
        return f"{round(n / 1000)}k"
    return str(n)


def readout_line(used: int, soft: int = 0, hard: int = 0,
                 model_window: int | None = None) -> str:
    """Human-facing context readout with absolute anchors instead of a single
    occupancy % (which, computed against the aggressive configured target,
    routinely read >100% on large-window models — owner complaint #4).

    Distinguishes the two things that are easily — and damagingly — conflated:

    The autocompactor ADVISORY band (soft..hard) is the token range above which
    this tool *suggests* compacting early to keep cached-read cost low. These
    are recommendations, NOT walls — being above them is expected and fine. Pi
    actuates at its own hard line, so there is no separate forced native wall to
    show; "near the soft limit" can never be misread as "one turn from
    auto-compacting" because no forced-wall anchor is rendered.

    model_window is shown only when the caller supplies a confident estimate,
    else omitted."""
    parts = [f"{_fmt_tokens(used)} in context"]
    if soft and hard and abs(int(hard) - int(soft)) >= 1000:
        parts.append(f"compact advised ~{_fmt_tokens(soft)}–{_fmt_tokens(hard)}")
    elif hard or soft:
        parts.append(f"compact advised ~{_fmt_tokens(hard or soft)}")
    if model_window:
        parts.append(f"model window {_fmt_tokens(model_window)}")
    return " · ".join(parts)


def composition_line(comp: dict) -> str:
    """Format a transcript_lib.context_composition() dict as the one-line
    breakdown shown UNDER readout_line — "what is actually in the window".

    Answers owner request (a): not just how full context is, but *where* the
    tokens are, so the reclaimable part (stale tool output) is visible at a
    glance. Returns "" when there is nothing meaningful to show.

    The numbers are reconciled to the true total by context_composition (the
    residual `base` absorbs estimation error), so the parts always sum to the
    readout's "in context" figure; the leading "≈" flags that per-category
    splits are a chars/4 estimate, not exact accounting."""
    if not comp or not comp.get("total"):
        return ""
    parts = []
    skills = int(comp.get("skills", 0) or 0)
    if skills > 0:
        names = [n for n in (comp.get("skill_names") or []) if n]
        label = ", ".join(names[:3]) if names else "loaded"
        if len(names) > 3:
            label += f", +{len(names) - 3}"
        parts.append(f"{_fmt_tokens(skills)} skills ({label})")
    base = int(comp.get("base", 0) or 0)
    if base > 0:
        # Once big skill injections are pulled out, the residual really is just
        # the system prompt + tool schemas — don't keep calling that "floor".
        parts.append(f"{_fmt_tokens(base)} {'system+tools' if skills else 'floor'}")
    summary = int(comp.get("summary", 0) or 0)
    if summary > 0:
        parts.append(f"{_fmt_tokens(summary)} summary")
    tool = int(comp.get("tool", 0) or 0)
    if tool > 0:
        sf = float(comp.get("tool_stale_frac", 0.0) or 0.0)
        seg = f"{_fmt_tokens(tool)} tool"
        if sf > 0:
            seg += f" ({sf:.0%} stale)"
        parts.append(seg)
    asst = int(comp.get("assistant", 0) or 0)
    if asst > 0:
        parts.append(f"{_fmt_tokens(asst)} asst")
    prompts = int(comp.get("prompts", 0) or 0)
    if prompts > 0:
        parts.append(f"{_fmt_tokens(prompts)} prompts")
    if not parts:
        return ""
    return "≈ " + " · ".join(parts)


def _fmt_pct(part: int, total: int) -> str:
    if not total:
        return "0%"
    return f"{(part / total):.0%}"


def _tool_topline(comp: dict, max_items: int = 3) -> str:
    items = comp.get("tool_breakdown") or []
    if not items:
        return ""
    segs = []
    for item in items[:max_items]:
        name = str(item.get("name") or "tool")
        tokens = int(item.get("tokens", 0) or 0)
        if tokens <= 0:
            continue
        stale = float(item.get("stale_frac", 0.0) or 0.0)
        piece = f"{name} {_fmt_tokens(tokens)}"
        if stale > 0:
            piece += f"/{stale:.0%} stale"
        segs.append(piece)
    if not segs:
        return ""
    extra = len(items) - len(segs)
    suffix = f", +{extra}" if extra > 0 else ""
    return "top tools: " + ", ".join(segs) + suffix


def composition_detail_lines(comp: dict) -> list[str]:
    """Multiline, content-free context accounting for UI notices.

    This is intentionally more explicit than composition_line(): it names which
    buckets are likely reclaimed by compaction (stale tool output), which are
    lossy-summary material, and which are fixed/persistent. Counts remain
    estimates, reconciled to the authoritative total by context_composition().
    """
    if not comp or not comp.get("total"):
        return []
    total = int(comp.get("total", 0) or 0)
    lines = []
    skills = int(comp.get("skills", 0) or 0)
    if skills > 0:
        names = [n for n in (comp.get("skill_names") or []) if n]
        label = ", ".join(names[:3]) if names else "loaded skills"
        if len(names) > 3:
            label += f", +{len(names) - 3}"
        lines.append(f"loaded skills: ~{_fmt_tokens(skills)} "
                     f"({_fmt_pct(skills, total)}; {label}; persists across /compact)")
    base = int(comp.get("base", 0) or 0)
    if base > 0:
        label = "system+tool schemas" if skills else "floor (system+tool schemas)"
        lines.append(f"{label}: ~{_fmt_tokens(base)} "
                     f"({_fmt_pct(base, total)}; not compactable)")
    summary = int(comp.get("summary", 0) or 0)
    if summary > 0:
        lines.append(f"prior summary: ~{_fmt_tokens(summary)} "
                     f"({_fmt_pct(summary, total)}; already compacted)")
    tool = int(comp.get("tool", 0) or 0)
    if tool > 0:
        sf = float(comp.get("tool_stale_frac", 0.0) or 0.0)
        stale_tokens = int(round(tool * sf))
        details = [f"{_fmt_pct(tool, total)} of context"]
        if sf > 0:
            details.append(f"{sf:.0%} stale")
            details.append(f"~{_fmt_tokens(stale_tokens)} likely reclaimable")
        top = _tool_topline(comp)
        if top:
            details.append(top)
        lines.append(f"tool output: ~{_fmt_tokens(tool)} (" + "; ".join(details) + ")")
    asst = int(comp.get("assistant", 0) or 0)
    if asst > 0:
        lines.append(f"assistant text/thinking: ~{_fmt_tokens(asst)} "
                     f"({_fmt_pct(asst, total)}; summarized lossily)")
    prompts = int(comp.get("prompts", 0) or 0)
    if prompts > 0:
        lines.append(f"user prompts: ~{_fmt_tokens(prompts)} "
                     f"({_fmt_pct(prompts, total)}; constraints to preserve)")

    # --- ContextInventory additive fields (Task 6) ---
    # Floor decomposition (readout only; CONSUME the comp dict's inventory_floor
    # verbatim - never re-rank/recompute). Per-package tool-schema breakdown
    # labeled 'measured <date>' when probe data exists, else the honest single
    # 'tools+system (fixed)' bucket (true_residual absorbs it).
    inv_floor = comp.get("inventory_floor")
    if isinstance(inv_floor, dict):
        cf = int(inv_floor.get("context_files", 0) or 0)
        sm = int(inv_floor.get("skills_meta", 0) or 0)
        ts = int(inv_floor.get("tools_system", 0) or 0)
        tr = int(inv_floor.get("true_residual", 0) or 0)
        per_pkg = inv_floor.get("per_package") or {}
        measured = inv_floor.get("measured_at", "") or ""
        if cf > 0:
            lines.append(f"context files: ~{_fmt_tokens(cf)} "
                         f"({_fmt_pct(cf, total)}; measured live; persists)")
        if sm > 0:
            lines.append(f"skills meta: ~{_fmt_tokens(sm)} "
                         f"({_fmt_pct(sm, total)}; measured live; persists)")
        if ts > 0 and per_pkg:
            date_label = measured.split("T")[0] if measured else ""
            pkg_str = ", ".join(f"{k} {_fmt_tokens(int(v))}" for k, v in
                                sorted(per_pkg.items(),
                                       key=lambda kv: int(kv[1]), reverse=True)
                                [:4])
            lines.append(f"tool schemas (measured {date_label}): "
                         f"~{_fmt_tokens(ts)} ({pkg_str})")
        elif tr > 0:
            lines.append(f"tools+system (fixed): ~{_fmt_tokens(tr)} "
                         f"({_fmt_pct(tr, total)}; not compactable; "
                         "no probe data)")
    # Dynamic per-item highlights + dormant rollup (CONSUME verbatim).
    ledger = comp.get("dynamic_ledger")
    if isinstance(ledger, list) and ledger:
        top = sorted(ledger, key=lambda it: int(it.get("tokens", 0) or 0),
                     reverse=True)[:5]
        for it in top:
            kind = str(it.get("kind", ""))
            tn = str(it.get("tool_name", "") or "")
            tok = int(it.get("tokens", 0) or 0)
            age = int(it.get("age_turns", 0) or 0)
            tags = []
            if it.get("dormant"):
                tags.append("dormant")
            if it.get("redundant"):
                tags.append("redundant")
            if it.get("reclaimable"):
                tags.append("reclaimable")
            name = f" ({tn})" if tn else ""
            tagstr = (" [" + ",".join(tags) + "]") if tags else ""
            lines.append(f"{kind}{name}: ~{_fmt_tokens(tok)} "
                         f"(age {age} turns{tagstr})")
        dormant = int(comp.get("dormant_tokens", 0) or 0)
        if dormant > 0:
            lines.append(f"dormant items: ~{_fmt_tokens(dormant)} "
                         f"({_fmt_pct(dormant, total)}; "
                         "/compact reclaims if unreferenced)")
    return lines


# Fraction of the window above which loaded skills are called out as the
# dominant (and persistent) consumer. Loaded skills do NOT shrink on /compact.
SKILL_DOMINANCE_FRAC = 0.40

def reducible_floor_advisory(comp: dict, *, min_tokens: int = 1000,
                            max_lines: int = 4) -> list[str]:
    """Render ReclaimEstimate.ranking as user-actionable guidance (spec §5/§7).

    Surfaces [{bucket, tokens, reducible_by}] items from the inventory's
    reclaim.ranking as lines like 'unload pi-subagents ~= 11k'. CONSUMES the
    ranking verbatim — never re-ranks, never recomputes reclaim figures.
    Content-free: token counts and bucket/package names only.

    The advisory frames /compact cannot unload packages — the lever is not
    loading them. Returns [] when no ranking/reclaim data is present or every
    row is below `min_tokens` (noise floor)."""
    reclaim = comp.get("reclaim") if isinstance(comp, dict) else None
    if not isinstance(reclaim, dict):
        return []
    ranking = reclaim.get("ranking")
    if not isinstance(ranking, list) or not ranking:
        return []
    lines = []
    shown = 0
    for r in ranking:
        if shown >= max_lines:
            break
        try:
            tokens = int(r.get("tokens", 0) or 0)
        except (TypeError, ValueError):
            continue
        if tokens < min_tokens:
            continue
        bucket = str(r.get("bucket", "") or "").strip()
        lever = str(r.get("reducible_by", "") or "").strip()
        if not bucket:
            continue
        # Frame so the user understands /compact cannot unload packages: the
        # lever is 'unload package' (not loading them), not /compact.
        hint = ""
        if lever == "unload package":
            hint = " (unload the package; /compact cannot do this)"
        elif lever == "--exclude-tools":
            hint = " (--exclude-tools; /compact cannot do this)"
        lines.append(f"reducible: {bucket} ~= {_fmt_tokens(tokens)}{hint}")
        shown += 1
    return lines
def skill_warning(comp: dict, threshold: float = SKILL_DOMINANCE_FRAC) -> str:
    """When loaded skills dominate the window, name them and warn that they are
    NOT reclaimable by /compact (skill bodies persist across compaction) — the
    real lever is not loading the skill. Returns "" below the threshold.
    Content-free: token counts + skill identifiers only."""
    total = int(comp.get("total", 0) or 0)
    skills = int(comp.get("skills", 0) or 0)
    if not total or skills <= 0:
        return ""
    frac = skills / total
    if frac < threshold:
        return ""
    names = [n for n in (comp.get("skill_names") or []) if n]
    label = ", ".join(names[:3]) if names else "loaded skills"
    if len(names) > 3:
        label += f", +{len(names) - 3}"
    return (f"⚠ {_fmt_tokens(skills)} ({frac:.0%}) of context is loaded skills "
            f"({label}) — /compact won't reclaim these; unload the skill to "
            f"drop them")


def compaction_notice(count, pre_tokens, after_tokens, comp,
                      pre_ledger) -> str:
    """The single user-visible compaction notice (Q1: rendered exactly once, on
    the first of the next UserPromptSubmit / PostToolUse after a compaction, via
    systemMessage — the two channels Claude Code actually renders to the user).

    Why here and not in the PreCompact/PostCompact hooks: CC 2.1.x rejects a
    PreCompact `hookSpecificOutput` outright (PreCompact/PostCompact are not in
    the hook-output union; emitting one fails JSON-output validation and the
    user sees an error), and a PostCompact `systemMessage` is swallowed by the
    compaction redraw (never surfaced). The monitor's UserPromptSubmit /
    PostToolUse outputs DO render, so the notice rides those.

    `comp` is the BEFORE-compaction composition (stashed by PreCompact): its
    category breakdown sums to `pre_tokens` and shows WHERE the reclaim came
    from (typically stale tool output), pairing with the before→after head.

    Returns "" when there is nothing meaningful to say. Content-free: token
    counts / category names only, never transcript text."""
    pre = int(pre_tokens or 0)
    after = int(after_tokens or 0)
    head = f"compaction #{count} complete" if count else "compaction complete"
    if pre and after and after < pre:
        head += (f" — context {_fmt_tokens(pre)} → {_fmt_tokens(after)} "
                 f"(reclaimed ~{_fmt_tokens(pre - after)})")
    elif pre:
        head += f" — {_fmt_tokens(pre)} in context before compaction"
    lines = ["autocompactor: " + head]
    comp_line = composition_line(comp) if comp else ""
    if comp_line:
        lines.append("  └ " + comp_line)
    warn = skill_warning(comp) if comp else ""
    if warn:
        lines.append("  " + warn)
    if pre_ledger:
        lines.append(pre_ledger)
    # Suppress a contentless "compaction complete" with nothing to add.
    if len(lines) > 1 or pre:
        return "\n".join(lines)
    return ""


def advisory_band(cfg: "PolicyConfig") -> tuple[int, int]:
    """(soft, hard) advisory thresholds in tokens, for readout_line(). soft is
    the window-aware target (or soft fraction × window if an explicit SOFT_PCT
    override is set); hard is hard_pct × window. Both are recommendation points,
    not walls."""
    eff = max(int(cfg.effective_limit), 1)
    soft = int(cfg.target_tokens) if cfg.target_tokens else int(cfg.soft * eff)
    hard = int(cfg.hard * eff)
    return soft, hard


def burst_milestone(cfg: "PolicyConfig", ctx: int,
                    occ_pcts: "tuple[float, ...] | None" = None,
                    tail_pct: float = 0.10) -> int:
    """Highest burst-watchdog milestone (a token threshold) that ``ctx`` has
    crossed, or 0 below the soft line.

    Window-INVARIANT cadence. The old ladder advanced by a fixed +100k token
    step above the hard line; with hard ~= 0.55*W and the effective window at
    W, the danger band [hard, W] is only ~0.45*W wide -- narrower than one 100k
    step at a 200k window -- so after the single hard-line announce the next
    step (hard+100k) landed past W and the watchdog went silent across the whole
    danger zone (observed: a skynet session climbed 114k->203k unwarned).

    The rungs are now fractions of the effective window: soft, hard, then each
    ``occ_pcts`` fraction that lies above hard, then +``tail_pct``*W steps above
    the top rung so an over-window burst keeps being nagged toward the native
    wall. Because the rungs scale with W, no rung-to-rung gap can exceed the
    spacing of ``occ_pcts``, whatever the window."""
    eff = max(int(cfg.effective_limit), 1)
    soft, hard = advisory_band(cfg)
    if ctx < soft:
        return 0
    if occ_pcts is None:
        occ_pcts = (0.70, 0.80, 0.90, 0.97)
    rungs = sorted({soft, hard,
                    *(int(p * eff) for p in occ_pcts if int(p * eff) > hard)})
    top = rungs[-1]
    if ctx <= top:
        return max(r for r in rungs if r <= ctx)
    tail = max(int(tail_pct * eff), 1)
    return top + ((ctx - top) // tail) * tail


def is_ctx_spike(ctx: int, prev_ctx: int, window: int) -> bool:
    """True when ``ctx`` is an implausible single-sample jump above the previous
    reading. Tail-parse can momentarily double-count (observed: 303k for one
    eval, back to 155k 4s later). Such a spike must not advance the burst ladder
    (it would mute the watchdog for the rest of the burst) nor update peak_ctx
    (it inflates the learned-window tier). Requires BOTH a >1.5x ratio and a
    >0.5*window absolute jump, so steady growth and a genuine large jump
    (corroborated on the next eval, once prev catches up) pass through."""
    if prev_ctx <= 0:
        return False
    return ctx > prev_ctx * 1.5 and (ctx - prev_ctx) > 0.5 * max(int(window), 1)


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
    target_tokens: int = 0     # window-aware SOFT target (0 = legacy soft fraction)


@dataclass
class PolicyInput:
    context_tokens: int
    effective_limit: int
    gating: bool                     # >=1 non-observe boundary signal present
    last_reco_tokens: int = -10**9   # cooldown baseline (adapter-owned, passed in)


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

    SOFT line: when no explicit SOFT_PCT override is set, the SOFT threshold is
    the window-aware target curve (target_tokens/effective_limit) rather than a
    flat fraction — small windows are not starved, large windows target lower
    occupancy. An explicit SOFT_PCT (deprecated) still wins (migration safety).
    """
    requested = profile or config_lib.cfg.str(
        "PROFILE", default="balanced") or "balanced"
    base = _PROFILES.get(requested, _PROFILES["balanced"])
    pname = requested if requested in _PROFILES else "balanced"
    hard = config_lib.cfg.float("HARD_PCT", default=base["hard"])
    cooldown = config_lib.cfg.float("COOLDOWN", default=base["cooldown"])
    stale_frac = config_lib.cfg.float("STALE_FRAC", default=base["stale_frac"])
    post_floor = config_lib.cfg.float("POST_FLOOR", default=70_000)
    min_savings = config_lib.cfg.float("MIN_SAVINGS", default=30_000)
    mode = config_lib.cfg.str("MODE", default="advise") or "advise"
    eff = max(int(effective_limit), 1)

    # Window-aware SOFT target, unless a deprecated SOFT_PCT override is set.
    raw_soft = config_lib.cfg.raw("SOFT_PCT")
    if raw_soft is not None:
        soft = config_lib.cfg.float("SOFT_PCT", default=base["soft"])
        tgt = 0
    else:
        tgt = target_tokens(eff, pname, post_floor, min_savings, hard)
        soft = tgt / eff

    return PolicyConfig(pname, mode, soft, hard, cooldown, stale_frac,
                        post_floor, min_savings, eff, target_tokens=tgt)


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
              f"(profile={cfg.profile}")
    if cfg.target_tokens:
        reason += f", target={cfg.target_tokens:,}t"
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
