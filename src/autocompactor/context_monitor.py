#!/usr/bin/env python3
"""
context_monitor.py — Claude Code UserPromptSubmit hook.

Watches context occupancy and "compaction opportunity" signals, and tells
you (and Claude) to compact at cheap, natural boundaries instead of letting
autocompact fire near the context ceiling.

Why UserPromptSubmit: it fires on every human turn, gets transcript_path,
and its stdout/additionalContext is injected into context — so the
recommendation is visible to both you and the model. Hooks cannot run
/compact themselves; this is an advisor, not an actuator. (In SDK/headless
automation you can act on the same signal programmatically.)

Decision model
--------------
  occupancy = context_tokens / CONTEXT_WINDOW
  Recommend /compact when:
    occupancy >= HARD_PCT                                   (always), or
    occupancy >= SOFT_PCT and a boundary signal is present:
        * git commit just made
        * test suite just passed
        * all TodoWrite items completed
        * large fraction of context is stale tool output
  Cooldown: never re-recommend within COOLDOWN_TOKENS of the last
  recommendation (state kept per-session in /tmp).

When it recommends, it also pre-computes tailored preservation
instructions and stages them in a state file that precompact_analyzer.py
picks up — so the /compact you run is automatically the smart one.

Tunables via env (set in settings.json "env" or your shell):
  AUTOCOMPACTOR_WINDOW        context window tokens   (default 200000)
  AUTOCOMPACTOR_SOFT_PCT      boundary threshold      (default 0.40)
  AUTOCOMPACTOR_HARD_PCT      unconditional threshold (default 0.65)
  AUTOCOMPACTOR_COOLDOWN      tokens between nags     (default 25000)
  AUTOCOMPACTOR_STALE_FRAC    stale-tool-output frac  (default 0.50)
  AUTOCOMPACTOR_POST_FLOOR    est. post-compaction context (default 70000)
  AUTOCOMPACTOR_MIN_SAVINGS   min est. reclaim to recommend (default 30000)
  MAX_FULL_PARSE_MB  above this transcript size, parse only
                              the post-boundary active segment (default 8)
"""

import json
import os
import sys

from autocompactor import config_lib, artifacts, policy, window_resolver  # noqa: E402
from autocompactor.transcript_lib import (analyze, active_signals,  # noqa: E402
                                          build_preservation_instructions,
                                          context_composition,
                                          current_context_tokens,
                                          detect_phase,
                                          find_last_boundary_offset,
                                          observe_only)
from autocompactor.stats import log_event, run_hook  # noqa: E402

STATE_DIR = os.path.expanduser("~/.claude/autocompactor")


def _run_posttooluse(data: dict, transcript: str, session_id: str) -> int:
    """Mid-burst occupancy watchdog.

    PostToolUse fires after every tool, including during long autonomous
    runs that produce NO UserPromptSubmit — the only chance to catch a
    hard-limit crossing before native autocompact fires unwarned (see
    docs/masterplan/simplify-compaction-model/miss-attribution.md).

    Cheap by design: a tail-only current_context_tokens() read gates
    everything; the full analyze() runs ONLY when occupancy is already at
    or above the hard line. Below it the hook returns in ~1ms with no
    output and no telemetry (no per-tool spam). Cooldown debounces
    re-recommendation exactly as on the UserPromptSubmit path.
    """
    ctx = current_context_tokens(transcript)
    if ctx <= 0:
        return 0

    os.makedirs(STATE_DIR, exist_ok=True)
    state_file = os.path.join(STATE_DIR, f"{session_id}.state.json")
    state = {}
    try:
        with open(state_file) as fh:
            state = json.load(fh)
    except Exception:
        pass
    # First-of-either one-shot compaction notice (owner Q1: "first of either,
    # one-shot"): after a compaction, BOTH the next UserPromptSubmit and the next
    # PostToolUse are armed; whichever fires first renders the single notice via
    # systemMessage, then disarms. This is the PostToolUse arm — it matters during
    # autonomous bursts that produce no UserPromptSubmit, where the compaction
    # would otherwise leave no visible trace at all (G2/G3).
    if state.get("pending_notice"):
        notice = policy.compaction_notice(
            count=state.get("compaction_count"),
            pre_tokens=state.get("pre_compact_tokens"),
            after_tokens=ctx,
            comp=state.get("pre_comp") or {},
            pre_ledger=state.get("pre_ledger"))
        state["pending_notice"] = False
        state["last_milestone_tokens"] = 0   # fresh context -> reset burst ladder
        try:
            with open(state_file, "w") as fh:
                json.dump(state, fh)
        except OSError:
            pass
        if notice:                            # already prefixed "autocompactor: "
            print(json.dumps({"systemMessage": notice}))
        return 0
    # A compaction just happened -> let the next UserPromptSubmit own the
    # reinject; don't speak on the first post-compaction tool result.
    if state.get("pending_reinject"):
        return 0

    configured_window = config_lib.cfg.float("WINDOW", default=200_000)
    peak = max(ctx, int(state.get("peak_ctx", 0)))
    if peak != state.get("peak_ctx"):
        state["peak_ctx"] = peak
        try:
            with open(state_file, "w") as fh:
                json.dump(state, fh)
        except OSError:
            pass
    resolution = window_resolver.resolve_window(
        configured_window=configured_window, observed_peak=peak,
        harness="claude",
        native_ceiling=window_resolver.native_ceiling_from_settings())
    window = resolution.effective_window
    occupancy = ctx / window

    # Escalating-milestone readout (owner Q2: "escalating thresholds only"): emit
    # the mid-burst readout on the FIRST cross of the soft line, the FIRST cross of
    # the hard line, and each further +BURST_MILESTONE_STEP above hard — NOT once
    # per qualifying tool call (which read as spam). `last_milestone_tokens` is the
    # highest milestone already announced this burst; it resets to 0 when context
    # drops back below soft (a fresh burst can re-announce).
    pcfg = policy.resolve_policy_config("claude", int(window))
    soft_t, hard_t = policy.advisory_band(pcfg)
    step = int(config_lib.cfg.float("BURST_MILESTONE_STEP",
                                    default=100_000)) or 100_000
    last_milestone = int(state.get("last_milestone_tokens", 0) or 0)
    est_reclaim = int(ctx - pcfg.post_floor)
    if ctx < soft_t:
        if last_milestone:                    # dropped below soft -> reset ladder
            state["last_milestone_tokens"] = 0
            try:
                with open(state_file, "w") as fh:
                    json.dump(state, fh)
            except OSError:
                pass
        return 0
    milestone = soft_t if ctx < hard_t else hard_t + ((ctx - hard_t) // step) * step
    crossed = milestone > last_milestone and est_reclaim >= pcfg.min_savings
    if not crossed:
        # Gated (off by default) coverage telemetry. PostToolUse otherwise logs a
        # monitor_eval ONLY on the recommend branch below, so non-recommends are
        # invisible and PostToolUse warning-coverage can't be measured (WI-1 gap).
        # When AUTOCOMPACTOR_LOG_WATCHDOG_SKIPS is set, log a CHEAP skip eval (no
        # full analyze()) at/above the soft line — enough for nightly to compute
        # true coverage without per-tool spam. (We are already >= soft_t here.)
        if config_lib.cfg.str("LOG_WATCHDOG_SKIPS", default="0") not in (
                "", "0", "false", "False", "no", "off"):
            log_event({
                "type": "monitor_eval", "session_id": session_id,
                "context_tokens": ctx, "occupancy": round(occupancy, 4),
                "signals": [], "phase": None,
                "est_reclaim": est_reclaim, "tail_parse": True,
                "recommended": False,
                "suppressed_by_cooldown": milestone <= last_milestone,
                "hook_event": "PostToolUse", "watchdog_skip": True,
                **resolution.event_fields(),
            })
        return 0   # below soft / milestone already announced / nothing to reclaim

    # At/above the hard line mid-burst: do ONE bounded full analyze for the
    # reason + signals, stage instructions, and surface a recommendation.
    max_full_mb = config_lib.cfg.float("MAX_FULL_PARSE_MB", default=8)
    offset = 0
    try:
        if (os.path.getsize(os.path.expanduser(transcript))
                > max_full_mb * 1_000_000):
            offset = find_last_boundary_offset(transcript)
    except OSError:
        pass
    st = analyze(transcript, start_offset=offset)
    stale_frac_thr = config_lib.cfg.float("STALE_FRAC", default=0.50)
    sig_pairs = active_signals(st, prompt="", window=window,
                               stale_frac_thr=stale_frac_thr,
                               hard_tokens=hard_t)
    signals = [desc for _, desc in sig_pairs]
    observe = observe_only()
    gating = [desc for name, desc in sig_pairs if name not in observe]

    state.update({"last_reco_tokens": ctx,
                  "last_milestone_tokens": milestone,
                  "staged_instructions":
                      build_preservation_instructions(st, data.get("cwd") or "")})
    try:
        with open(state_file, "w") as fh:
            json.dump(state, fh)
    except OSError:
        pass

    stale_frac = (st.stale_tool_chars / st.total_tool_chars
                  if st.total_tool_chars else 0.0)
    log_event({
        "type": "monitor_eval", "session_id": session_id,
        "context_tokens": ctx, "occupancy": round(occupancy, 4),
        "signals": signals, "stale_frac": round(stale_frac, 4),
        "phase": detect_phase(st), "est_reclaim": est_reclaim,
        "tail_parse": bool(offset), "recommended": True,
        "suppressed_by_cooldown": False, "hook_event": "PostToolUse",
        **resolution.event_fields(),
    })
    native_auto, model_window = window_resolver.readout_anchors(resolution)
    reason = policy.readout_line(ctx, soft_t, hard_t, native_auto, model_window)
    reason += " (mid-burst, no user prompt since the last turn)"
    if gating:
        reason += " — triggered by: " + "; ".join(gating)
    comp = context_composition(st, ctx)
    comp_line = policy.composition_line(comp)
    if comp_line:
        reason += "\n  └ " + comp_line
    skill_warn = policy.skill_warning(comp)
    if skill_warn:
        reason += "\n  " + skill_warn
    # The readout is shown to the user VERBATIM via systemMessage — the reliable,
    # visible channel in Claude Code (owner: "show the useful context info during
    # a suggestion, like before"). additionalContext is Claude-only, carries NO
    # numbers, and exists ONLY to keep Claude from ALSO restating the readout in
    # prose (which read as "double"). Net: the user sees exactly one rich readout.
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": (
                "[autocompactor] Optional early-compaction suggestion "
                "(mid-burst): a full context readout (token anchors + "
                "composition) has just been shown to the user via a system "
                "message — they can see it. Do NOT restate it, summarize it, or "
                "suggest /compact in your reply unless the user asks; never "
                "interrupt mid-tool work. Continue.")},
        "systemMessage": f"autocompactor: {reason}",
    }))
    return 0


def _run() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0

    transcript = data.get("transcript_path") or ""
    session_id = data.get("session_id") or "unknown"
    cwd = data.get("cwd") or ""
    if not transcript or not os.path.exists(os.path.expanduser(transcript)):
        return 0

    if data.get("hook_event_name") == "PostToolUse":
        return _run_posttooluse(data, transcript, session_id)

    configured_window = config_lib.cfg.float("WINDOW", default=200_000)
    window = configured_window
    cooldown = config_lib.cfg.float("COOLDOWN", default=25_000)
    stale_frac_thr = config_lib.cfg.float("STALE_FRAC", default=0.50)
    post_floor = config_lib.cfg.float("POST_FLOOR", default=70_000)
    min_savings = config_lib.cfg.float("MIN_SAVINGS", default=30_000)
    max_full_mb = config_lib.cfg.float("MAX_FULL_PARSE_MB", default=8)

    # Per-session state (cooldown, staged instructions, carried peak).
    os.makedirs(STATE_DIR, exist_ok=True)
    state_file = os.path.join(STATE_DIR, f"{session_id}.state.json")
    state = {}
    try:
        with open(state_file) as fh:
            state = json.load(fh)
    except Exception:
        pass
    last_reco_tokens = state.get("last_reco_tokens", -10**9)

    # State is written at most once per prompt: mutate `state` freely and set
    # state_dirty; _save_state() flushes before each return. (Every-turn
    # cheapness — the previous code wrote the state file up to four times.)
    state_dirty = False

    def _save_state() -> None:
        if not state_dirty:
            return
        try:
            with open(state_file, "w") as fh:
                json.dump(state, fh)
        except OSError:
            pass

    # Bound worst-case parse cost: past a size threshold, parse only the
    # active segment (after the last compaction boundary). The active
    # segment is capped by the autocompact ceiling; the dead prefix grows
    # without bound. Cross-segment aggregates degrade gracefully: the
    # session peak is carried in the state file, and mechanically
    # extracted artifacts were already persisted by earlier prompts.
    offset = 0
    try:
        if (os.path.getsize(os.path.expanduser(transcript))
                > max_full_mb * 1_000_000):
            offset = find_last_boundary_offset(transcript)
    except OSError:
        pass
    st = analyze(transcript, start_offset=offset)
    if st.context_tokens <= 0:
        return 0

    # Per-session effective window. AUTOCOMPACTOR_WINDOW is tuned for the
    # largest model in use (e.g. 400k ceiling on 1M models), but many
    # sessions run 200k-window models whose autocompact fires from ~135k —
    # percentage thresholds against the big window would stay silent for
    # their entire life. Transcripts don't record the effective window, so
    # estimate: until a session's observed context exceeds what a 200k
    # model could reach, assume the tighter ceiling. The peak is carried in
    # state so tail-only parses and post-compaction shrinkage don't forget.
    peak = max(st.usage_series) if st.usage_series else st.context_tokens
    peak = max(peak, int(state.get("peak_ctx", 0)))
    if peak != state.get("peak_ctx"):
        state["peak_ctx"] = peak
        # Persist the peak IMMEDIATELY, not via the batched flush: it is the
        # durability anchor for tail-only parses (a later mid-prompt exception
        # would otherwise lose it, since run_hook swallows the raise). The
        # other state writes (cooldown reset, staged instructions) are not
        # durability-critical mid-prompt and stay batched in _save_state().
        try:
            with open(state_file, "w") as fh:
                json.dump(state, fh)
        except OSError:
            pass
    resolution = window_resolver.resolve_window(
        configured_window=configured_window,
        observed_peak=peak,
        harness="claude",
        native_ceiling=window_resolver.native_ceiling_from_settings())
    window = resolution.effective_window
    occupancy = st.context_tokens / window

    # Window-aware SOFT line: the target(W) curve (policy.target_tokens), not
    # a flat fraction. Small windows aren't starved (target rides near the
    # window); large windows target lower occupancy (keep ~150k, expand only
    # on a boundary signal). See docs/masterplan/simplify-compaction-model/
    # window-aware.md. A deprecated SOFT_PCT override (if set) still wins.
    pcfg = policy.resolve_policy_config("claude", int(window))
    soft = pcfg.soft

    # First post-compaction prompt: two one-shots may both be armed here — the
    # single combined notice (pending_notice, owner Q1 "first of either") rendered
    # via systemMessage, and the artifact-digest re-injection (pending_reinject)
    # via additionalContext. Render whichever are armed in ONE output, then return.
    if state.get("pending_notice") or state.get("pending_reinject"):
        out_obj = {}
        if state.get("pending_notice"):
            notice = policy.compaction_notice(
                count=state.get("compaction_count"),
                pre_tokens=state.get("pre_compact_tokens"),
                after_tokens=st.context_tokens,
                comp=state.get("pre_comp") or {},
                pre_ledger=state.get("pre_ledger"))
            state["pending_notice"] = False
            state["last_milestone_tokens"] = 0   # fresh context -> reset ladder
            state_dirty = True
            if notice:                           # already "autocompactor: "-prefixed
                out_obj["systemMessage"] = notice
        if state.get("pending_reinject"):
            arts = artifacts.load(session_id)
            budget = int(config_lib.cfg.float("ARTIFACT_BUDGET", default=1500))
            digest = artifacts.build_digest(
                arts, budget_tokens=budget,
                stats_line=state.get("last_compaction_stats", ""))
            state["pending_reinject"] = False
            state["last_reco_tokens"] = -10**9   # fresh context, reset cooldown
            state_dirty = True
            if digest:
                ev = {"type": "reinject", "session_id": session_id,
                      "digest_tokens": len(digest) // 4,
                      "artifact_keys": list(arts.keys())}
                # Realized post-compaction floor: this is the first prompt after a
                # compaction, so st.context_tokens is the compacted size. Stamp it
                # (and the pre-compaction size PreCompact stashed) ONLY when it is
                # genuinely a reduction — if the transcript reads stale here, omit
                # the fields and let the nightly precompact→eval join reconstruct
                # it rather than log a wrong number. Content-free; never raises.
                pre_ct = state.get("pre_compact_tokens")
                after_ct = st.context_tokens
                if (isinstance(pre_ct, (int, float)) and after_ct
                        and after_ct < pre_ct):
                    ev["pre_tokens"] = int(pre_ct)
                    ev["after_tokens"] = int(after_ct)
                log_event(ev)
                out_obj["hookSpecificOutput"] = {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": digest}
        _save_state()
        if out_obj:
            print(json.dumps(out_obj))
        return 0
    # Continuous artifact extraction: merge-persist so mechanically extracted
    # facts survive compactions that arrive with no warning (autocompact,
    # crash, another tool's /compact). Write only when the merge actually
    # changed something — a steady-state prompt adds nothing new and skips the
    # disk write entirely (the on-disk content is identical either way).
    try:
        existing = artifacts.load(session_id)
        merged = artifacts.merge(existing, artifacts.extract(st))
        if merged != existing:
            artifacts.save(session_id, merged)
    except Exception:
        pass

    # Cooldown debounces RISING context only (see pi_bridge.py for the full
    # rationale): a context that has shrunk below the last staging point has
    # more room, not less, so reset the baseline. Persist the reset so a
    # bricked state file self-heals on the next prompt.
    if st.context_tokens < last_reco_tokens:
        last_reco_tokens = -10**9
        state["last_reco_tokens"] = last_reco_tokens
        state_dirty = True
    suppressed = 0 <= (st.context_tokens - last_reco_tokens) < cooldown

    stale_frac = (st.stale_tool_chars / st.total_tool_chars
                  if st.total_tool_chars else 0.0)
    prompt = data.get("prompt") or ""
    soft_t, hard_t = policy.advisory_band(pcfg)
    sig_pairs = active_signals(st, prompt=prompt, window=window,
                               stale_frac_thr=stale_frac_thr,
                               hard_tokens=hard_t)
    signals = [desc for _, desc in sig_pairs]
    # Observe-only signals are logged (telemetry keeps measuring them)
    # but never justify a recommendation — they tested anti-predictive.
    observe = observe_only()
    gating = [desc for name, desc in sig_pairs if name not in observe]

    recommend = (occupancy >= pcfg.hard or (occupancy >= soft and bool(gating)))
    # Min-savings guard: a compaction can only reclaim what sits above the
    # post-compaction floor (system prompt + tools + CLAUDE.md + summary —
    # measured ~69k median on this machine). Below that margin a compaction
    # stalls 30-60s to reclaim almost nothing; never recommend it.
    est_reclaim = int(st.context_tokens - post_floor)
    if est_reclaim < min_savings:
        recommend = False
    log_event({
        "type": "monitor_eval", "session_id": session_id,
        "context_tokens": st.context_tokens,
        "occupancy": round(occupancy, 4),
        "signals": signals, "stale_frac": round(stale_frac, 4),
        "phase": detect_phase(st),
        "est_reclaim": est_reclaim,
        "tail_parse": bool(offset),
        "recommended": recommend and not suppressed,
        "suppressed_by_cooldown": recommend and suppressed,
        "hook_event": "UserPromptSubmit",
        **resolution.event_fields(),
    })
    if not recommend or suppressed:
        _save_state()
        return 0

    # Stage tailored preservation instructions for the PreCompact hook.
    instructions = build_preservation_instructions(st, cwd)
    state.update({"last_reco_tokens": st.context_tokens,
                  "staged_instructions": instructions})
    state_dirty = True
    _save_state()

    # Absolute-anchor readout (not a bare occupancy %, which read >100% against
    # the aggressive configured target on large-window models — owner #4).
    native_auto, model_window = window_resolver.readout_anchors(resolution)
    reason = policy.readout_line(st.context_tokens, soft_t, hard_t,
                                 native_auto, model_window)
    if gating:
        reason += " — triggered by: " + "; ".join(gating)
    comp = context_composition(st, st.context_tokens)
    comp_line = policy.composition_line(comp)
    if comp_line:
        reason += "\n  └ " + comp_line
    skill_warn = policy.skill_warning(comp)
    if skill_warn:
        reason += "\n  " + skill_warn

    # The readout is shown to the user VERBATIM via systemMessage — the reliable,
    # visible channel in Claude Code. additionalContext is Claude-only, carries NO
    # numbers, and exists ONLY to keep Claude from restating the readout (the
    # "double"). Net: the user sees exactly one rich readout, the systemMessage.
    out = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": (
                "[autocompactor] Optional early-compaction suggestion: a full "
                "context readout (token anchors + composition) has just been "
                "shown to the user via a system message — they can see it. Do "
                "NOT restate it, summarize it, or suggest /compact in your reply "
                "unless the user asks. Just address the prompt."
            ),
        },
        "systemMessage": f"autocompactor: {reason}",
    }
    print(json.dumps(out))
    return 0


def main() -> int:
    """Never-raise wrapper (hook contract): any failure degrades to exit 0."""
    return run_hook("context_monitor", _run)


if __name__ == "__main__":
    sys.exit(main())
