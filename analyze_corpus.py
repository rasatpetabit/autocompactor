#!/usr/bin/env python3
"""
analyze_corpus.py — backtest autocompactor heuristics against real Claude
Code transcripts and report tuning recommendations.

Usage:
    python3 analyze_corpus.py [--days 4] [--root ~/.claude/projects] \
                              [--window 200000] [--json report.json]

Runs entirely locally; reads transcripts read-only; emits aggregate
statistics only (no transcript content is included in the report unless
--verbose-errors is passed).

What it measures per session
----------------------------
  * context-token trajectory (from assistant usage blocks)
  * inferred compaction events: a drop of >30% in context tokens between
    consecutive assistant messages (the JSONL has no stable explicit
    compaction marker across versions, so we infer from the trajectory)
  * occupancy at each compaction vs. the earliest point autocompactor
    would have recommended (the backtest), and the token-delta between
    those two points = estimated waste per late compaction
  * boundary-signal availability: at autocompactor's recommended moments,
    which signals fired (commit/tests/todos/stale) — measures whether the
    soft threshold ever gets a chance to act, or sessions blow straight
    past it
  * phase classification at each compaction (validates detect_phase
    against what you remember of those sessions)
  * stale tool-output fraction distribution

Tuning outputs
--------------
  * recommended SOFT_PCT / HARD_PCT given observed compaction occupancies
  * cooldown sanity check (recommendation density per session)
  * per-signal hit rates so dead signals can be cut and missing ones spotted
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from transcript_lib import (analyze, detect_phase,  # noqa: E402
                            load_transcript, observe_only)
from transcript_lib import active_signals as _registry_signals  # noqa: E402

DROP_FRAC = 0.30   # context drop that we treat as a compaction event
_OBSERVE = observe_only()   # anti-predictive signals: measured, never gating


def usage_tokens(entry: dict) -> int:
    u = (entry.get("message") or {}).get("usage") or {}
    if not u:
        return 0
    return (int(u.get("input_tokens", 0))
            + int(u.get("cache_read_input_tokens", 0))
            + int(u.get("cache_creation_input_tokens", 0))
            + int(u.get("output_tokens", 0)))


def trajectory(entries: list) -> list:
    """[(entry_index, context_tokens)] for assistant messages with usage."""
    out = []
    for i, e in enumerate(entries):
        if e.get("type") == "assistant":
            t = usage_tokens(e)
            if t:
                out.append((i, t))
    return out


def find_compactions(traj: list, entries: list = None) -> list:
    """Compaction events as trajectory indices.

    Prefers explicit markers (type=system/subtype=compact_boundary or
    isCompactSummary — present in current Claude Code transcripts); falls
    back to the >DROP_FRAC context-drop heuristic for older transcripts."""
    if entries:
        markers = [(i, e.get("compactMetadata") or {})
                   for i, e in enumerate(entries)
                   if (e.get("type") == "system"
                       and e.get("subtype") == "compact_boundary")
                   or e.get("isCompactSummary")]
        if markers:
            events = []
            for mi, cm in markers:
                k = next((k for k in range(len(traj))
                          if traj[k][0] >= mi), None)
                if k is None or k == 0:
                    continue
                # adjacent markers (boundary + summary) collapse to one event
                if events and events[-1]["traj_idx"] == k:
                    continue
                # compactMetadata.preTokens is the CLI's own measurement —
                # authoritative when present
                before = cm.get("preTokens") or traj[k - 1][1]
                after = cm.get("postTokens") or traj[k][1]
                events.append({"traj_idx": k, "entry_idx": mi,
                               "before": before, "after": after,
                               "trigger": cm.get("trigger", "unknown"),
                               "explicit": True})
            return events
    events = []
    for k in range(1, len(traj)):
        prev, cur = traj[k - 1][1], traj[k][1]
        if prev > 20_000 and cur < prev * (1 - DROP_FRAC):
            events.append({"traj_idx": k, "entry_idx": traj[k][0],
                           "before": prev, "after": cur,
                           "trigger": "inferred"})
    return events


def backtest_session(path: str, window: float, soft: float, hard: float,
                     min_eval: float = 90_000, lead_tokens: float = 50_000):
    entries = load_transcript(path)
    traj = trajectory(entries)
    if len(traj) < 5:
        return {"path": __import__("os").path.basename(path),
                "turns": len(traj), "skipped": "too_short"}
    peak = max(t for _, t in traj)
    compactions = find_compactions(traj, entries)

    # Replay autocompactor at sampled assistant steps. One pass computes
    # both the first-recommendation point and per-signal precision
    # observations (does a compaction follow within lead_tokens of
    # context growth from each firing?).
    first_reco = None
    reco_signals = []
    signal_obs = []
    sample_points = [traj[k] for k in range(0, len(traj), max(1, len(traj) // 40))]
    for entry_idx, tokens in sample_points:
        occ = tokens / window
        want_reco = first_reco is None and occ >= soft
        want_prec = tokens >= min_eval
        if not (want_reco or want_prec):
            continue
        st = analyze_prefix(entries, entry_idx)
        sigs = active_signals(st)
        # Mirror the monitor exactly: observe-only signals are measured
        # for precision below but never justify a replayed recommendation.
        gating = [s for s in sigs if s not in _OBSERVE]
        if want_reco and (occ >= hard or gating):
            first_reco = {"tokens": tokens, "occupancy": occ, "signals": sigs}
            reco_signals = sigs
        if want_prec:
            nxt = next((c for c in compactions
                        if c["entry_idx"] > entry_idx), None)
            lead = (nxt["before"] - tokens) if nxt else None
            hit = lead is not None and 0 <= lead <= lead_tokens
            trig = nxt["trigger"] if nxt else None
            # "_baseline" tracks the signal-agnostic hit rate at evaluated
            # points, so each signal's lift over chance is visible
            for s in ["_baseline"] + sigs:
                signal_obs.append({"signal": s, "hit": hit, "lead": lead,
                                   "next_trigger": trig})

    results = []
    for c in compactions:
        entry_idx = traj[c["traj_idx"]][0]
        st = analyze_prefix(entries, entry_idx)
        rec = {
            "occupancy_at_compact": c["before"] / window,
            "before": c["before"],
            "after": c["after"],
            "reduction": 1 - c["after"] / c["before"],
            "trigger": c.get("trigger", "unknown"),
            "phase": detect_phase(st),
            "signals_at_compact": active_signals(st),
        }
        if first_reco and first_reco["tokens"] < c["before"]:
            rec["late_by_tokens"] = c["before"] - first_reco["tokens"]
        results.append(rec)

    # Peak context AFTER the last compaction — feeds the nightly
    # rapid-refill-breaker watch (did autocompact silently stop firing
    # while context kept growing?).
    post_peak = None
    if compactions:
        tail_vals = [t for _, t in traj[compactions[-1]["traj_idx"]:]]
        post_peak = max(tail_vals) if tail_vals else None

    full = analyze(entries=entries)
    return {
        "path": os.path.basename(path),
        "turns": len(traj),
        "peak_tokens": peak,
        "peak_occupancy": peak / window,
        "post_last_compaction_peak": post_peak,
        "compactions": results,
        "first_recommendation": first_reco,
        "recommendation_signals": reco_signals,
        "signal_observations": signal_obs,
        "stale_frac": (full.stale_tool_chars / full.total_tool_chars
                       if full.total_tool_chars else 0.0),
    }


def analyze_prefix(entries: list, upto: int):
    """Run transcript analysis over entries[:upto]."""
    return analyze(entries=entries[:upto])


def active_signals(st) -> list:
    return [name for name, _ in _registry_signals(st)]



def _dist(vals):
    import statistics
    if not vals:
        return {}
    return {"n": len(vals), "min": min(vals),
            "median": round(statistics.median(vals), 3), "max": max(vals)}


def aggregate_events(stats_dir=None):
    """Aggregate live telemetry written by stats.log_event."""
    if stats_dir is None:
        stats_dir = "~/.claude/autocompactor/stats"
    path = os.path.join(os.path.expanduser(stats_dir), "events.jsonl")
    evs = load_transcript(path)
    mon = [e for e in evs if e.get("type") == "monitor_eval"]
    pre = [e for e in evs if e.get("type") == "precompact"]
    by = lambda lst, k: {v: sum(1 for e in lst if e.get(k) == v)
                         for v in {e.get(k) for e in lst}}
    out = {
        "monitor_evals": len(mon),
        "recommendations": sum(1 for e in mon if e.get("recommended")),
        "cooldown_suppressions": sum(1 for e in mon
                                     if e.get("suppressed_by_cooldown")),
        "occupancy_seen": _dist([e.get("occupancy", 0) for e in mon]),
        "compactions": {"total": len(pre), "by_trigger": by(pre, "trigger"),
                        "by_phase": by(pre, "phase"),
                        "staged_hit_rate_pct": round(100 * sum(
                            1 for e in pre if e.get("had_staged"))
                            / max(len(pre), 1), 1)},
    }
    reductions = []
    for p in pre:
        later = [m for m in mon if m.get("session_id") == p.get("session_id")
                 and m.get("ts", "") > p.get("ts", "") and m.get("context_tokens")]
        if later and p.get("context_tokens"):
            reductions.append(round(1 - later[0]["context_tokens"]
                                    / p["context_tokens"], 3))
    out["compaction_reduction_ratio"] = _dist(reductions)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=4)
    ap.add_argument("--root", default="~/.claude/projects")
    ap.add_argument("--window", type=float, default=200_000)
    ap.add_argument("--soft", type=float, default=0.40)
    ap.add_argument("--hard", type=float, default=0.65)
    ap.add_argument("--min-eval-tokens", type=float, default=90_000,
                    help="evaluate signal precision at sampled points above this")
    ap.add_argument("--lead-tokens", type=float, default=50_000,
                    help="a signal firing counts as a hit if a compaction "
                         "follows within this much context growth")
    ap.add_argument("--events", action="store_true",
                    help="aggregate live telemetry from stats.py instead of backtesting")
    ap.add_argument("--stats-dir", default="~/.claude/autocompactor/stats",
                    help="directory containing stats.py events.jsonl for --events")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    if args.events:
        print(json.dumps(aggregate_events(args.stats_dir), indent=2))
        return

    cutoff = time.time() - args.days * 86400
    paths = [p for p in glob.glob(
                 os.path.join(os.path.expanduser(args.root), "**", "*.jsonl"),
                 recursive=True)
             if os.path.getmtime(p) >= cutoff]
    print(f"Found {len(paths)} transcript(s) modified in last "
          f"{args.days:g} day(s) under {args.root}")

    sessions, all_compactions = [], []
    for p in sorted(paths):
        try:
            r = backtest_session(p, args.window, args.soft, args.hard,
                                 args.min_eval_tokens, args.lead_tokens)
        except Exception as exc:  # schema drift tolerance
            print(f"  ! skipped {os.path.basename(p)}: {exc}")
            continue
        if r and r.get("skipped"):
            print(f"  - {r['path']}: skipped ({r['skipped']}, {r['turns']} turns)")
        elif r:
            sessions.append(r)
            all_compactions.extend(r["compactions"])

    if not sessions:
        print("No analyzable sessions.")
        return

    occs = [c["occupancy_at_compact"] for c in all_compactions]
    lates = [c["late_by_tokens"] for c in all_compactions
             if "late_by_tokens" in c]
    reductions = [c["reduction"] for c in all_compactions]
    phases = {}
    sig_counts = {}
    sig_counts_manual = {}
    trigger_counts = {}
    pre_by_trigger = {}
    for c in all_compactions:
        phases[c["phase"]] = phases.get(c["phase"], 0) + 1
        trig = c.get("trigger", "unknown")
        trigger_counts[trig] = trigger_counts.get(trig, 0) + 1
        pre_by_trigger.setdefault(trig, []).append(c["before"])
        for s in c["signals_at_compact"]:
            sig_counts[s] = sig_counts.get(s, 0) + 1
            if trig == "manual":
                sig_counts_manual[s] = sig_counts_manual.get(s, 0) + 1
    reco_sig_counts = {}
    for s in sessions:
        for sig in s["recommendation_signals"]:
            reco_sig_counts[sig] = reco_sig_counts.get(sig, 0) + 1

    print(f"\nSessions analyzed:        {len(sessions)}")
    print(f"Compaction events found:  {len(all_compactions)}")
    if occs:
        print(f"Occupancy at compaction:  median {statistics.median(occs):.0%}, "
              f"p90 {sorted(occs)[int(0.9 * (len(occs) - 1))]:.0%}")
        print(f"Reduction per compaction: median "
              f"{statistics.median(reductions):.0%}")
    if lates:
        print(f"Late compactions:         {len(lates)} "
              f"(median {statistics.median(lates):,.0f} tokens past the "
              f"first viable recommendation; total waste "
              f"~{sum(lates):,.0f} tokens re-paid as input)")
    print(f"Phase at compaction:      {phases}")
    print(f"Compactions by trigger:   {trigger_counts}")
    for trig, pres in sorted(pre_by_trigger.items()):
        print(f"  {trig}: preTokens median {statistics.median(pres):,.0f}, "
              f"p90 {sorted(pres)[int(0.9 * (len(pres) - 1))]:,.0f}, "
              f"max {max(pres):,.0f}")
    print(f"Signals at compaction:    {sig_counts}")
    print(f"  at MANUAL compactions:  {sig_counts_manual}")
    print(f"Signals at recommendation:{reco_sig_counts}")

    # Per-signal precision: of the points where each signal fired, how
    # often did a compaction actually follow within the lead window?
    # "_baseline" = hit rate over ALL evaluated points (lift comparator).
    prec = {}
    for s in sessions:
        for ob in s.get("signal_observations", []):
            d = prec.setdefault(ob["signal"],
                                {"fires": 0, "hits": 0, "leads": [],
                                 "manual_next": 0})
            d["fires"] += 1
            if ob["hit"]:
                d["hits"] += 1
                d["leads"].append(ob["lead"])
            if ob.get("next_trigger") == "manual" and ob["hit"]:
                d["manual_next"] += 1
    if prec:
        print("\nPer-signal precision (compaction within lead window "
              "after firing):")
        base = prec.get("_baseline", {"fires": 0, "hits": 0})
        base_rate = base["hits"] / base["fires"] if base["fires"] else 0.0
        print(f"  {'signal':<15} {'fires':>6} {'prec':>6} {'lift':>6} "
              f"{'med lead':>9} {'manual':>6}")
        for name, d in sorted(prec.items(),
                              key=lambda kv: -(kv[1]["hits"] / kv[1]["fires"]
                                               if kv[1]["fires"] else 0)):
            p = d["hits"] / d["fires"] if d["fires"] else 0.0
            lift = p / base_rate if base_rate else 0.0
            med_lead = (f"{statistics.median(d['leads']):,.0f}"
                        if d["leads"] else "-")
            print(f"  {name:<15} {d['fires']:>6} {p:>6.0%} {lift:>5.1f}x "
                  f"{med_lead:>9} {d['manual_next']:>6}")

    # Tuning suggestions
    print("\n--- tuning suggestions ---")
    if occs and statistics.median(occs) > args.hard:
        print(f"* Compactions cluster above HARD_PCT ({args.hard:.0%}): "
              "mostly autocompact. Lower HARD_PCT or shorten COOLDOWN.")
    no_signal_sessions = sum(1 for s in sessions
                             if s["peak_occupancy"] >= args.soft
                             and not s["recommendation_signals"]
                             and (s["first_recommendation"] or {}).get(
                                 "occupancy", 1) >= args.hard)
    if no_signal_sessions:
        print(f"* {no_signal_sessions} session(s) crossed SOFT_PCT with no "
              "boundary signal firing -> add signals (e.g. topic-shift "
              "detection, PR/push, long idle gaps) or lower STALE_FRAC.")
    # Full registry minus topic_shift (needs a prompt, never evaluated here)
    dead = [s for s in ("commit", "tests_pass", "todos_done", "todo_step",
                        "error_resolved", "subagent_done", "idle_gap",
                        "stale_output", "burn_rate")
            if s not in sig_counts and s not in reco_sig_counts]
    if dead:
        print(f"* Signals that never fired on this corpus: {dead} — check "
              "their regexes/heuristics against this workflow's actual "
              "tool output.")

    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"sessions": sessions}, fh, indent=1)
        print(f"\nFull per-session detail written to {args.json}")


if __name__ == "__main__":
    main()
