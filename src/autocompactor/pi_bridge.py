#!/usr/bin/env python3
"""
pi_bridge.py — never-raise JSON CLI bridging the Pi coding agent to the
autocompactor Python core.

The Pi TypeScript shim (pi/autocompactor.ts) shells out here via
pi.exec("python3", [...]) — there is no stdin channel, so every input
arrives as a CLI flag. Contract: ALWAYS exit 0 and emit at most ONE JSON
object on stdout (or nothing); every exception is swallowed. A broken
bridge must never break a Pi compaction.

Subcommands (flags mirror the shim's bridge() calls):

  evaluate --session <path> [--tokens N] [--context-window N] [--reserve N]
      The recommendation model, judged against the Pi effective window
      (contextWindow - reserveTokens):
      recommend when occupancy >= HARD_PCT, or >= SOFT_PCT with a gating
      signal (active_signals minus observe-only); min-savings guard and
      per-session cooldown identical to the Claude monitor. Emits
      {"recommend": bool, "reason": str, "context_tokens": int} and logs a
      monitor_eval telemetry row (harness "pi", Pi state dir).

  prepare --session <path> [--cwd <dir>] [--trigger <s>]
      PreCompact analog: backup the session JSONL, merge-persist artifacts,
      stage/refresh preservation instructions (founding-goal restatement
      included). Emits {"customInstructions": str}.

  reinject --session <path>
      Post-compaction digest, shaped for pi.sendMessage. Emits
      {"text": str, "customType": "autocompactor.digest"} when artifacts
      exist, nothing otherwise. One-shot: clears pending state and resets
      the cooldown (fresh context).

Thresholds read AUTOCOMPACTOR_<NAME> env overrides, else config.json,
else code defaults. State lives under statedir.state_root()
(~/.autocompactor/pi unless AUTOCOMPACTOR_STATE_DIR overrides).
"""

import datetime
import json
import os
import shutil
import sys

from autocompactor import (artifacts, pi_session_lib, policy,  # noqa: E402
                           statedir, transcript_lib, window_resolver)
from autocompactor.config_lib import cfg                          # noqa: E402
from autocompactor.llm_digest import llm_digest                 # noqa: E402
from autocompactor.stats import log_event                         # noqa: E402

HARNESS = "pi"
DIGEST_CUSTOM_TYPE = "autocompactor.digest"
RESERVE_FALLBACK = 40_000


def _parse_args(argv: list) -> dict:
    """Tolerant --flag value parser; unknown flags ignored, never raises."""
    opts = {}
    i = 0
    while i < len(argv):
        tok = argv[i]
        if isinstance(tok, str) and tok.startswith("--"):
            key = tok[2:].replace("-", "_")
            if i + 1 < len(argv) and not str(argv[i + 1]).startswith("--"):
                opts[key] = argv[i + 1]
                i += 2
                continue
            opts[key] = ""
        i += 1
    return opts


def _to_int(value, default=None):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _session_id(session_path: str) -> str:
    base = os.path.basename(session_path or "")
    return os.path.splitext(base)[0] or "unknown"


def _state_path(session_id: str) -> str:
    return os.path.join(statedir.state_root(HARNESS),
                        session_id + ".state.json")


def _load_state(session_id: str) -> dict:
    try:
        with open(_state_path(session_id)) as fh:
            state = json.load(fh)
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def _save_state(session_id: str, state: dict) -> None:
    try:
        os.makedirs(statedir.state_root(HARNESS), exist_ok=True)
        with open(_state_path(session_id), "w") as fh:
            json.dump(state, fh)
    except Exception:
        pass


def _analyze(session: str):
    if session and os.path.exists(os.path.expanduser(session)):
        return pi_session_lib.analyze(session)
    return transcript_lib.TranscriptStats()


def cmd_evaluate(opts: dict) -> dict:
    session = opts.get("session", "")
    session_id = _session_id(session)
    st = _analyze(session)

    context_tokens = _to_int(opts.get("tokens"))
    if context_tokens is None:
        context_tokens = st.context_tokens
    configured_window = int(cfg.float("WINDOW", default=200_000))
    runtime_context_window = _to_int(opts.get("context_window"))
    context_window = runtime_context_window or configured_window
    reserve = _to_int(opts.get("reserve"),
                      int(cfg.float("RESERVE", default=RESERVE_FALLBACK)))
    observed_peak = max(
        [context_tokens] + [int(v) for v in getattr(st, "usage_series", []) or []])
    resolution = window_resolver.resolve_window(
        configured_window=configured_window,
        observed_peak=observed_peak,
        runtime_context_window=runtime_context_window,
        reserve=reserve)
    window = resolution.effective_window
    occupancy = context_tokens / window

    # Window-aware thresholds: WIDE suffix for large windows (>=300k)
    soft = cfg.float_windowed("SOFT_PCT", context_window, HARNESS, 0.40)
    hard = cfg.float_windowed("HARD_PCT", context_window, HARNESS, 0.65)
    soft_t, hard_t = int(soft * window), int(hard * window)
    cooldown = cfg.float("COOLDOWN", default=25_000)
    stale_frac_thr = cfg.float("STALE_FRAC", default=0.50)
    post_floor = cfg.float("POST_FLOOR", default=70_000)
    min_savings = cfg.float("MIN_SAVINGS", default=30_000)

    state = _load_state(session_id)
    last_reco = state.get("last_reco_tokens", -10**9)
    # Cooldown debounces RISING context only: don't re-recommend every turn
    # as tokens creep up past the last staging point. A context that has
    # SHRUNK below the last staging point has more room, not less, so reset
    # the baseline. Without this a reco staged at a high token count that
    # never reached the reinject reset (native compaction, crash, race) would
    # deadlock the session permanently: a negative delta is always < cooldown.
    # Persist the reset so a bricked state file self-heals on the next eval.
    if context_tokens < last_reco:
        last_reco = -10**9
        state["last_reco_tokens"] = last_reco
        _save_state(session_id, state)
    suppressed = 0 <= (context_tokens - last_reco) < cooldown

    stale_frac = (st.stale_tool_chars / st.total_tool_chars
                  if st.total_tool_chars else 0.0)
    sig_pairs = transcript_lib.active_signals(
        st, window=window, stale_frac_thr=stale_frac_thr, hard_tokens=hard_t)
    signals = [desc for _, desc in sig_pairs]
    observe = transcript_lib.observe_only()
    gating = [desc for name, desc in sig_pairs if name not in observe]

    recommend = (occupancy >= hard or (occupancy >= soft and bool(gating)))
    est_reclaim = int(context_tokens - post_floor)
    if est_reclaim < min_savings:
        recommend = False

    log_event({
        "type": "monitor_eval", "session_id": session_id,
        "context_tokens": context_tokens,
        "occupancy": round(occupancy, 4),
        "signals": signals, "stale_frac": round(stale_frac, 4),
        "phase": transcript_lib.detect_phase(st),
        "est_reclaim": est_reclaim,
        "tail_parse": False,
        "recommended": recommend and not suppressed,
        "suppressed_by_cooldown": recommend and suppressed,
        **resolution.event_fields(),
    })

    # Pre-compaction context overview — for the TS shim to display and
    # for the compaction instructions to embed.
    ctx_state = transcript_lib.build_context_state(
        st, window=window)

    if recommend and not suppressed:
        # Stage instructions for prepare and start the cooldown.
        state.update({
            "last_reco_tokens": context_tokens,
            "staged_instructions":
                transcript_lib.build_preservation_instructions(
                    st, opts.get("cwd", "")),
            "context_state": ctx_state,
        })
        _save_state(session_id, state)

    # Anchored readout (shared with Claude) instead of a bare occupancy % that
    # gave no denominator. Pi ACTUATES at its own hard line, so there is no
    # separate native wall to show; the true model window
    # (runtime contextWindow) is the ceiling anchor when the runtime reported
    # it. Composition ("what's in the window") rides on contextState below.
    reason = policy.readout_line(context_tokens, soft_t, hard_t,
                                 model_window=(runtime_context_window or None))
    if gating:
        reason += " — triggered by: " + "; ".join(gating)
    if recommend and suppressed:
        reason += " (suppressed: cooldown)"
    elif not recommend and est_reclaim < min_savings:
        reason = (f"est. reclaim ~{max(est_reclaim, 0):,} tokens is below "
                  f"the {int(min_savings):,}-token minimum")

    return {"recommend": bool(recommend and not suppressed),
            "reason": reason,
            "mode": cfg.str("MODE", default="advise"),
            "context_tokens": context_tokens,
            "contextState": ctx_state}


def cmd_prepare(opts: dict) -> dict:
    session = opts.get("session", "")
    session_id = _session_id(session)
    trigger = opts.get("trigger") or "pi"
    st = _analyze(session)

    # Backup: compaction is lossy, the session JSONL is the audit trail.
    if session and os.path.exists(os.path.expanduser(session)):
        backup_dir = os.path.join(statedir.state_root(HARNESS), "backups")
        try:
            os.makedirs(backup_dir, exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            shutil.copy2(os.path.expanduser(session), os.path.join(
                backup_dir, f"{session_id}-{ts}-{trigger}.jsonl"))
        except OSError:
            pass

    state = _load_state(session_id)
    staged = state.get("staged_instructions", "")
    instructions = staged or transcript_lib.build_preservation_instructions(
        st, opts.get("cwd", ""))

    arts = artifacts.merge(artifacts.load(session_id),
                           artifacts.extract(st))
    art_sizes = artifacts.save(session_id, arts)
    # --skip-llm: the native (non-intercept) path discards customInstructions
    # (Pi's own summarizer runs), so the shim awaits prepare only for the cheap
    # on-disk artifacts/state — skip the up-to-45s LLM digest there.
    if cfg.str("LLM") == "1" and session and opts.get("skip_llm") != "1":
        extra = llm_digest(session)
        if extra:
            instructions += "\n\nAdditional must-preserve facts:\n" + extra

    # Founding-goal restatement + on-disk-artifacts NOTE (shared with the
    # Claude precompact_analyzer so the two paths can't drift).
    instructions = transcript_lib.append_artifact_restatement(
        instructions, arts)

    state["pending_reinject"] = True
    state["compaction_count"] = state.get("compaction_count", 0) + 1
    phase = transcript_lib.detect_phase(st)
    comp = transcript_lib.context_composition(st, st.context_tokens)
    art_kept, art_dropped = artifacts.budget_plan(arts)
    state["last_compaction_stats"] = " | ".join([
        f"compaction #{state['compaction_count']} ({trigger})",
        f"context ~{st.context_tokens:,}t", f"phase: {phase}",
        f"artifacts to disk: {len(art_sizes)} classes, "
        f"{sum(art_sizes.values()):,}B",
        "instructions: " + ("staged" if staged else "fresh analysis")
        + f" ({len(instructions):,} chars)",
    ])
    # Composition (a) + preservation ledger (b), mirroring the Claude path.
    comp_line = policy.composition_line(comp)
    if comp_line:
        state["last_compaction_stats"] += "\n  └ " + comp_line
    skill_warn = policy.skill_warning(comp)
    if skill_warn:
        state["last_compaction_stats"] += "\n  " + skill_warn
    ledger = artifacts.preservation_ledger(
        arts, art_sizes, lossy_tokens=comp.get("assistant", 0))
    if ledger:
        state["last_compaction_stats"] += "\n" + ledger
    _save_state(session_id, state)

    # Pre-compaction context overview — produced at prepare time so it
    # reflects the exact state when compaction starts (not when evaluate
    # fired, which may be several turns earlier).
    prepare_resolution = window_resolver.resolve_window(
        configured_window=cfg.float("WINDOW", default=200_000),
        observed_peak=max(st.usage_series) if st.usage_series else st.context_tokens,
        reserve=int(cfg.float("RESERVE", default=RESERVE_FALLBACK)))
    effective_window = prepare_resolution.effective_window
    ctx_state = transcript_lib.build_context_state(
        st, window=effective_window)

    log_event({
        "type": "precompact", "session_id": session_id, "trigger": trigger,
        "context_tokens": st.context_tokens, "phase": phase,
        "had_staged": bool(staged), "had_user_instructions": False,
        "instr_chars": len(instructions), "artifact_chars": art_sizes,
        "composition": comp or None,
        "artifacts_kept": art_kept, "artifacts_dropped": art_dropped,
        **prepare_resolution.event_fields(),
    })

    return {"customInstructions": instructions,
            "contextState": ctx_state}


def cmd_reinject(opts: dict) -> dict:
    session = opts.get("session", "")
    session_id = _session_id(session)
    arts = artifacts.load(session_id)
    state = _load_state(session_id)
    digest = artifacts.build_digest(
        arts, budget_tokens=int(cfg.float("ARTIFACT_BUDGET", default=1500)),
        stats_line=state.get("last_compaction_stats", ""))

    state["pending_reinject"] = False
    state["last_reco_tokens"] = -10**9   # fresh context, reset cooldown
    _save_state(session_id, state)

    if not digest:
        return {}

    # Post-compaction context overview: re-analyze the session
    # (which now has the compaction entry + truncated active segment)
    # to get the post-compaction token count, phase, occupancy.
    post_st = _analyze(session)
    # Resolve the effective window the SAME way evaluate/prepare do (runtime
    # window when the shim reports it), so the post-compaction occupancy
    # readout is consistent rather than computed off a stale config window.
    runtime_context_window = _to_int(opts.get("context_window"))
    observed_peak = max(
        [post_st.context_tokens]
        + [int(v) for v in getattr(post_st, "usage_series", []) or []])
    resolution = window_resolver.resolve_window(
        configured_window=int(cfg.float("WINDOW", default=200_000)),
        observed_peak=observed_peak,
        runtime_context_window=runtime_context_window,
        reserve=int(cfg.float("RESERVE", default=RESERVE_FALLBACK)))
    post_state = transcript_lib.build_context_state(
        post_st, window=resolution.effective_window)

    log_event({"type": "reinject", "session_id": session_id,
               "digest_tokens": len(digest) // 4,
               "artifact_keys": list(arts.keys()),
               "post_tokens": post_st.context_tokens,
               "post_phase": transcript_lib.detect_phase(post_st)},
              )
    return {"text": digest, "customType": DIGEST_CUSTOM_TYPE,
            "contextState": post_state}


def main(argv: list) -> int:
    if not argv:
        return 0
    cmd, opts = argv[0], _parse_args(argv[1:])
    handler = {"evaluate": cmd_evaluate,
               "prepare": cmd_prepare,
               "reinject": cmd_reinject}.get(cmd)
    if handler is None:
        return 0
    out = handler(opts)
    if out:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception:
        pass
    sys.exit(0)
