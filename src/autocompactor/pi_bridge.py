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

from autocompactor import (artifacts, context_inventory, pi_session_lib, policy,  # noqa: E402
                           statedir, transcript_lib, window_resolver)
from autocompactor.config_lib import cfg                          # noqa: E402
from autocompactor.llm_digest import llm_digest                 # noqa: E402
from autocompactor.stats import log_event                         # noqa: E402

HARNESS = "pi"
DIGEST_CUSTOM_TYPE = "autocompactor.digest"
RESERVE_FALLBACK = 40_000

PROBE_NEVER_READ_NOTE = "decision path never reads floor-probe.json (T9 boundary)"


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

def _summary_term_median(session_id, *, window_size=None):
    """Telemetry median of historical (post_total - (base + skills)) — the
    SUMMARY-TERM only, far more config-stable than the whole post-total (spec
    §6.1). Read by parsing the raw events.jsonl under statedir.state_root()/stats
    (no stats.py read API exists — raw-log read is the intended path; we do NOT
    edit stats.py). Returns None when no telemetry history exists (caller falls
    back to the static POST_FLOOR_FALLBACK). Never raises."""
    import json
    import os
    try:
        if window_size is None:
            window_size = int(cfg.float("POST_FLOOR_CALIBRATION", default=10))
        # The reinject event carries post_total/base/skills (persisted by
        # cmd_reinject). events.jsonl lives under the stats subdir.
        root = statedir.state_root()
        stats_path = os.path.join(os.path.dirname(root), "stats",
                                  "events.jsonl")
        # Fall back to the legacy <state_root>/stats path if the layout differs.
        if not os.path.isfile(stats_path):
            stats_path = os.path.join(root, "stats", "events.jsonl")
        if not os.path.isfile(stats_path):
            return None
        terms = []
        with open(stats_path) as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("type") != "reinject":
                    continue
                post_total = obj.get("post_total")
                base = obj.get("base")
                skills = obj.get("skills")
                if (isinstance(post_total, (int, float))
                        and isinstance(base, (int, float))
                        and isinstance(skills, (int, float))):
                    term = post_total - (base + skills)
                    if term >= 0:
                        terms.append(int(term))
        if not terms:
            return None
        terms = terms[-window_size:] if window_size > 0 else terms
        terms_sorted = sorted(terms)
        n = len(terms_sorted)
        mid = n // 2
        if n % 2:
            return int(terms_sorted[mid])
        return int((terms_sorted[mid - 1] + terms_sorted[mid]) // 2)
    except Exception:
        return None


def _config_aware_post_floor(active_prefix, context_tokens, session_id):
    """post_floor = live_fixed_floor + summary_term (spec §6.1).

    live_fixed_floor = base + skills for THIS session, from
    context_inventory.decision_floor_terms() (DECISION-SAFE: include_probe=False
    by construction; this path NEVER opens floor-probe.json — T9 boundary).
    base = total - measured already reflects whatever tool schemas/packages are
    loaded NOW (no telemetry, no probe, no staleness).

    summary_term = telemetry median of historical post_total - (base + skills)
    (the summary size only); static POST_FLOOR_FALLBACK (70000) when no history.

    Returns (post_floor, degraded_note). degraded_note is '' on the config-
    aware path; on inventory failure it is the observable note AND post_floor
    falls back to the static POST_FLOOR (the INPUT only — the corrected policy
    formula still applies: the hard line is never gated by min_savings)."""
    static_floor = int(cfg.float("POST_FLOOR", default=70_000))
    try:
        terms = context_inventory.decision_floor_terms(active_prefix,
                                                        context_tokens)
        if terms.get("note"):
            # Decision inventory degraded — swap INPUTS only (static floor),
            # never the formula. Visible via the note.
            return static_floor, terms["note"]
        base = int(terms.get("base", 0))
        skills = int(terms.get("skills", 0))
        summary_term = _summary_term_median(session_id)
        if summary_term is None:
            summary_term = int(cfg.float("POST_FLOOR_FALLBACK",
                                          default=static_floor))
            # When there is no telemetry, fold the live base+skills into the
            # static floor so post_floor is config-aware even on the no-history
            # path: post_floor = max(base + skills + summary_term, static).
            live_floor = base + skills + summary_term
            return max(live_floor, static_floor), "no telemetry: static fallback"
        live = base + skills + summary_term
        return live, ""
    except Exception as exc:
        # Inventory failure — degraded INPUTS only; the corrected rule still
        # applies (hard line never gated by min_savings even on the fallback).
        return static_floor, f"inventory degraded: {exc}"

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
    # The runtime's getContextUsage() is the authoritative total at the moment
    # the TS shim asks for a readout. The transcript can lag or contain only an
    # older per-message usage entry, so reconcile composition to this number.
    st.context_tokens = context_tokens
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
    min_savings = cfg.float("MIN_SAVINGS", default=30_000)
    # Config-aware post_floor (spec §6.1): live_fixed_floor (base + skills for
    # THIS session, via the DECISION-SAFE entry — never opens floor-probe.json,
    # T9 boundary) + summary_term (telemetry median of historical
    # post_total - (base + skills); static fallback when no telemetry).
    # On inventory failure the INPUT swaps to static floor only; the
    # CORRECTED policy formula still applies (hard line never gated by
    # min_savings even on the fallback path, spec §8). The visible fallback
    # note is emitted in the telemetry event below.
    active_prefix = list(getattr(st, "entries", []) or [])
    post_floor, floor_note = _config_aware_post_floor(
        active_prefix, context_tokens, session_id)
    fallback_inputs = bool(floor_note)

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
    # GUARD CORRECTION (spec §6.1, the cross-vendor-fought line): min_savings
    # suppresses ONLY the opportunistic soft path (occupancy < hard) and is
    # NEVER applied at/above the hard line — a hard-line compaction always
    # proceeds. (Today's code blanket-suppressed at all bands; that could let
    # an estimate suppress a needed safety compaction. Estimating error in the
    # soft band only shifts opportunistic timing, never misses the safety one.)
    if occupancy < hard and est_reclaim < min_savings:
        recommend = False

    log_event({
        "type": "monitor_eval", "session_id": session_id,
        "context_tokens": context_tokens,
        "occupancy": round(occupancy, 4),
        "signals": signals, "stale_frac": round(stale_frac, 4),
        "phase": transcript_lib.detect_phase(st),
        "est_reclaim": est_reclaim,
        "post_floor": int(post_floor),
        "floor_degraded": fallback_inputs,
        "floor_note": floor_note,
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
    elif not recommend and occupancy < hard and est_reclaim < min_savings:
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
    # Capture the next step from the RICH pre-compaction transcript. The
    # post-compaction transcript reinject sees is truncated to an opaque
    # summary, so resolving there would return empty. Persist for reinject.
    next_step, next_step_src = transcript_lib.resolve_next_step(st)
    state["staged_next_step"] = next_step
    state["staged_next_step_src"] = next_step_src
    # Stage open_work for reinject / telemetry (counts + kinds only in events).
    open_work = list(getattr(st, "open_work", None) or [])[:5]
    state["staged_open_work"] = open_work
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
    detail_lines = policy.composition_detail_lines(comp)
    if detail_lines:
        state["last_compaction_stats"] += "\npre-compaction composition:"
        for line in detail_lines:
            state["last_compaction_stats"] += "\n  • " + line
    else:
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
        # Content-free next-step / open-work telemetry (no brief text).
        "next_step_src": next_step_src or "",
        "next_step_wait": bool(
            next_step_src.startswith("open_work:waiting")),
        "open_work_n": len(open_work),
        "open_work_kinds": [w.get("kind") for w in open_work
                            if isinstance(w, dict) and w.get("kind")],
        **prepare_resolution.event_fields(),
    })

    return {"customInstructions": instructions,
            "contextState": ctx_state}


def cmd_reinject(opts: dict) -> dict:
    session = opts.get("session", "")
    session_id = _session_id(session)
    arts = artifacts.load(session_id)
    state = _load_state(session_id)
    stats_line = state.get("last_compaction_stats", "")
    digest = artifacts.build_digest(
        arts, budget_tokens=int(cfg.float("ARTIFACT_BUDGET", default=1500)),
        stats_line=stats_line)

    state["pending_reinject"] = False
    state["last_reco_tokens"] = -10**9   # fresh context, reset cooldown
    _save_state(session_id, state)

    if not digest and not stats_line:
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

    # Persist post_total/base/skills on the reinject event so the decision's
    # telemetry summary-term median (post_total - (base + skills)) can be read
    # back by future cmd_evaluate calls (spec §6.1). Schema-free log_event (no
    # stats.py edit). The decision-floor terms are computed on the
    # POST-compaction active prefix via the DECISION-SAFE entry that never
    # opens floor-probe.json (T9 boundary).
    post_total = int(post_st.context_tokens)
    post_active_prefix = list(getattr(post_st, "entries", []) or [])
    try:
        floor_terms = context_inventory.decision_floor_terms(
            post_active_prefix, post_total)
        reinject_base = int(floor_terms.get("base", post_total))
        reinject_skills = int(floor_terms.get("skills", 0))
        floor_note = str(floor_terms.get("note", "") or "")
    except Exception:
        reinject_base, reinject_skills, floor_note = post_total, 0, "reinject floor terms degraded"

    log_event({"type": "reinject", "session_id": session_id,
               "digest_tokens": len(digest) // 4,
               "artifact_keys": list(arts.keys()),
               "post_tokens": post_st.context_tokens,
               "post_total": post_total,
               "base": reinject_base,
               "skills": reinject_skills,
               "floor_note": floor_note,
               "post_phase": transcript_lib.detect_phase(post_st)},
              )
    out = {"contextState": post_state}
    if stats_line:
        out["compactionStats"] = stats_line
    if digest:
        out.update({"text": digest, "customType": DIGEST_CUSTOM_TYPE})
    # Recover the next step staged at prepare time (pre-compaction transcript
    # was rich; post-compaction is truncated). Surface for an optional
    # next-step extension to act on. Kept (not popped) so it survives a
    # reinject race where the next-step listener runs after this reinject and
    # re-reads state; it is overwritten on the next prepare.
    next_step = state.get("staged_next_step", "")
    next_step_src = state.get("staged_next_step_src", "")
    if next_step:
        out["nextStep"] = next_step[:1500]
        out["nextStepSource"] = next_step_src
    out["nextStepWait"] = bool(
        (next_step_src or "").startswith("open_work:waiting"))
    open_work = state.get("staged_open_work") or []
    if open_work:
        out["openWork"] = open_work[:5]
    # Surface the configured NEXTSTEP mode so the TS shim gates surfacing
    # consistently with config.json (single source of truth).
    out["nextStepMode"] = cfg.str("NEXTSTEP", default="autonomous").lower()
    out["nextStepWaitMode"] = cfg.str(
        "NEXTSTEP_WAIT", default="poll").lower()
    out["waitPollS"] = int(cfg.float("WAIT_POLL_S", default=60))
    out["waitPollMax"] = int(cfg.float("WAIT_POLL_MAX", default=20))
    return out


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
