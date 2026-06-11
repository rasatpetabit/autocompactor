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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config_lib  # noqa: E402
from transcript_lib import (analyze, active_signals,  # noqa: E402
                            build_preservation_instructions, detect_phase,
                            find_last_boundary_offset, observe_only)
import artifacts  # noqa: E402
from stats import log_event  # noqa: E402

STATE_DIR = os.path.expanduser("~/.claude/autocompactor")



def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0

    transcript = data.get("transcript_path") or ""
    session_id = data.get("session_id") or "unknown"
    cwd = data.get("cwd") or ""
    if not transcript or not os.path.exists(os.path.expanduser(transcript)):
        return 0

    window = config_lib.cfg.float("WINDOW", default=200_000)
    soft = config_lib.cfg.float("SOFT_PCT", default=0.40)
    hard = config_lib.cfg.float("HARD_PCT", default=0.65)
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
        try:
            with open(state_file, "w") as fh:
                json.dump(state, fh)
        except OSError:
            pass
    if peak < 190_000:
        window = min(window, 200_000)
    occupancy = st.context_tokens / window

    # One-shot artifact re-injection on the first prompt after a compaction.
    if state.get("pending_reinject"):
        arts = artifacts.load(session_id)
        budget = int(config_lib.cfg.float("ARTIFACT_BUDGET", default=1500))
        digest = artifacts.build_digest(
            arts, budget_tokens=budget,
            stats_line=state.get("last_compaction_stats", ""))
        state["pending_reinject"] = False
        state["last_reco_tokens"] = -10**9   # fresh context, reset cooldown
        with open(state_file, "w") as fh:
            json.dump(state, fh)
        if digest:
            log_event({"type": "reinject", "session_id": session_id,
                       "digest_tokens": len(digest) // 4,
                       "artifact_keys": list(arts.keys())})
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": digest}}))
        return 0
    # Continuous artifact extraction: merge-persist on every prompt so
    # mechanically extracted facts survive compactions that arrive with
    # no warning (autocompact, crash, another tool's /compact). Analysis
    # is already done above — this adds one small JSON read+write.
    try:
        artifacts.save(session_id, artifacts.merge(
            artifacts.load(session_id), artifacts.extract(st)))
    except Exception:
        pass

    suppressed = st.context_tokens - last_reco_tokens < cooldown

    stale_frac = (st.stale_tool_chars / st.total_tool_chars
                  if st.total_tool_chars else 0.0)
    prompt = data.get("prompt") or ""
    sig_pairs = active_signals(st, prompt=prompt, window=window,
                               stale_frac_thr=stale_frac_thr)
    signals = [desc for _, desc in sig_pairs]
    # Observe-only signals are logged (telemetry keeps measuring them)
    # but never justify a recommendation — they tested anti-predictive.
    observe = observe_only()
    gating = [desc for name, desc in sig_pairs if name not in observe]

    recommend = (occupancy >= hard or (occupancy >= soft and bool(gating)))
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
    })
    if not recommend or suppressed:
        return 0

    # Stage tailored preservation instructions for the PreCompact hook.
    instructions = build_preservation_instructions(st, cwd)
    state.update({"last_reco_tokens": st.context_tokens,
                  "staged_instructions": instructions})
    with open(state_file, "w") as fh:
        json.dump(state, fh)

    reason = ("context is at "
              f"{occupancy:.0%} (~{st.context_tokens:,} tokens)")
    if gating:
        reason += " and " + "; ".join(gating)

    out = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": (
                "[autocompactor] Good moment to compact: " + reason + ". "
                "If the user's current request starts a new task or this is a "
                "natural breakpoint, briefly suggest they run /compact before "
                "proceeding. Do not interrupt mid-task work for this."
            ),
        },
        "systemMessage": (
            f"autocompactor: {reason}. Running /compact now is much cheaper "
            "than waiting for autocompact (tailored preservation "
            "instructions are staged)."
        ),
    }
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
