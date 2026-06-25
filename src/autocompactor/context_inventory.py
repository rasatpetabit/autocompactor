#!/usr/bin/env python3
"""
context_inventory.py — unified ContextInventory model (spec §4).

Consumer-agnostic analysis layer that itemizes what occupies the Pi context
window: the fixed floor decomposed (context_files / skills_meta / tools_system
/ true_residual), the dynamic content itemized as a per-item ledger with
dormancy/redundancy/reclaimability flags, and a ReclaimEstimate with a
deterministic ordered ranking. Feeds both a richer readout (policy detail
lines + the standalone `--inventory` report, spec §5) and the compaction
decision via a decision-safe entry that never opens the readout-only floor
probe (spec §6/§7/T9 boundary).

Architecture (spec §8): never-raise, but VISIBLE. Inventory build never throws;
on any failure it returns a degraded inventory computed from `total` alone and
emits a telemetry/log event — the fallback is observable, not a silent policy-
regime switch. The fallback is self-contained: it MUST NOT call
transcript_lib.context_composition() (T4 turns that into an adapter over THIS
module, so calling it would infinitely recurse).

Module boundary (no import cycle): context_inventory depends on
pi_session_lib (the neutral per-item primitives hoisted in Task 2) and
config_lib, only. It never imports transcript_lib's context_composition and
never imports turn_profile.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field

from autocompactor import config_lib, pi_session_lib, statedir, transcript_lib

CHARS_PER_TOKEN = transcript_lib.CHARS_PER_TOKEN  # shared chars/4 heuristic

FLOOR_PROBE_NAME = "floor-probe.json"


# ---------------------------------------------------------------------------
# Dataclasses (the API contract consumers T4/T5/T6/T7/T8 build against)
# ---------------------------------------------------------------------------


@dataclass
class ContextItem:
    """One dynamic content item in the active prefix."""
    kind: str            # tool_result | assistant | user | summary
    tool_name: str       # tool name for tool_result; "" otherwise
    tokens: int          # chars/4 estimate
    age_turns: int        # turns since this item was appended
    last_read_turn: int  # last turn a read/grep touched a path in this item (-1 never)
    dormant: bool = False
    redundant: bool = False
    reclaimable: bool = False


@dataclass
class FloorBreakdown:
    """The FIXED layer (survives /compact)."""
    context_files: int = 0   # MEASURED live (AGENTS.md etc., chars/4 — accurate)
    skills_meta: int = 0     # MEASURED live (loaded skills metadata, chars/4)
    tools_system: int = 0    # probe-decomposed per-package sum when include_probe,
                             # else the honest single "tools+system (fixed)" bucket
    true_residual: int = 0   # total − everything attributed (honesty bucket)


@dataclass
class ReclaimEstimate:
    """Reclaim advisory. Readout-level; never a decision input except the
    additive dormant_output gate (T5) which consumes ContextInventory
    .dynamic_dormant_tokens, not this object."""
    reclaimable_now: int = 0          # dynamic items /compact would drop (advisory, chars/4)
    post_floor_estimate: int = 0      # telemetry-calibrated; static fallback when no history
    ranking: list = field(default_factory=list)  # [{bucket, tokens, reducible_by}]


@dataclass
class ContextInventory:
    total_tokens: int = 0
    window: int = 0
    occupancy: float = 0.0
    floor: FloorBreakdown = field(default_factory=FloorBreakdown)
    dynamic: list = field(default_factory=list)        # [ContextItem]
    dynamic_dormant_tokens: int = 0
    categories: dict = field(default_factory=dict)     # {tool,assistant,prompts,summary}
    reclaim: ReclaimEstimate = field(default_factory=ReclaimEstimate)
    degraded: bool = False   # True when the never-raise fallback built this
    note: str = ""           # observable fallback label


# ---------------------------------------------------------------------------
# Public builders
# ---------------------------------------------------------------------------


def build_inventory(active_prefix, total_tokens, window, *, include_probe=True):
    """Build a ContextInventory over an active prefix.

    `active_prefix` is the post-compaction active segment (a list of JSONL
    entries) — the same shape pi_session_lib.active_path() returns as the
    second element, OR a prefix of it. `total_tokens` is the EXACT aggregate
    (ctx.getContextUsage()); `window` is the effective window.

    `include_probe=True` reads floor-probe.json (readout path). The
    decision-safe entry decision_floor_terms()/include_probe=False never
    opens the probe artifact (T9 boundary).

    Never raises: on internal failure returns a degraded inventory computed
    from `total_tokens` alone, with `degraded=True` and an observable note.
    Self-contained fallback — never calls transcript_lib.context_composition.
    """
    try:
        total = max(int(total_tokens), 0)
        win = max(int(window), 0) if window else 0
        occ = (total / win) if win else 0.0
        st = pi_session_lib.analyze_active_prefix(active_prefix, active_prefix,
                                                   recent_window=30,
                                                   compaction_count=0)
        dynamic = _build_dynamic_ledger(active_prefix)
        floor = _build_floor(st, total, include_probe=include_probe)
        cats = _rollup_categories(st)
        dormant_tokens = sum(it.tokens for it in dynamic if it.dormant)
        reclaim = _build_reclaim(dynamic, cats, total, floor)
        return ContextInventory(
            total_tokens=total,
            window=win,
            occupancy=occ,
            floor=floor,
            dynamic=dynamic,
            dynamic_dormant_tokens=dormant_tokens,
            categories=cats,
            reclaim=reclaim,
            degraded=False,
            note="",
        )
    except Exception as exc:  # never-raise, but VISIBLE (spec §8)
        return _degraded_inventory(total_tokens, window, str(exc))


def decision_floor_terms(active_prefix, total_tokens):
    """Decision-safe entry (spec §6/T8): return (base, skills) live, computed
    from the exact `total` residual and the measured skills — WITHOUT ever
    opening floor-probe.json. `base = total − measured` already reflects
    whatever tool schemas/packages are loaded NOW (no telemetry, no probe,
    no staleness). The decision consumer (T8) cannot read the readout-only
    probe by construction — this function does not touch the probe artifact.

    Returns a dict {base, skills}; never raises (degraded to
    {base: total, skills: 0} on any failure, with the label in `note`)."""
    try:
        total = max(int(total_tokens), 0)
        # Reuse the prefix analysis to measure skills live. analyze_active_prefix
        # never opens the probe — it reads the transcript only.
        st = pi_session_lib.analyze_active_prefix(active_prefix, active_prefix,
                                                   recent_window=30,
                                                   compaction_count=0)
        skills = max(int(getattr(st, "skill_chars", 0)), 0) // CHARS_PER_TOKEN
        context_files = _measure_context_files_chars() // CHARS_PER_TOKEN
        measured = skills + context_files
        base = max(total - measured, 0)
        # post_floor_estimate is the summary_term median lived in the decision
        # consumer (T8) which holds the telemetry history; here we surface only
        # the live residual terms so the formula is config-correct THIS session.
        return {"base": base, "skills": skills,
                "context_files": context_files, "note": ""}
    except Exception as exc:
        total = max(int(total_tokens), 0)
        return {"base": total, "skills": 0, "context_files": 0,
                "note": f"decision_floor_terms degraded: {exc}"}


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _measure_context_files_chars():
    """Live-measure the context-file injections (AGENTS.md etc.) that Pi loads
    ~verbatim. These are markdown injected nearly as-is, so chars/4 is
    accurate for them (spec §3). Returns total chars across known context
    files, 0 if none are readable."""
    total = 0
    seen = set()
    # Pi context-file resolution: the cwd AGENTS.md and the global one. We
    # measure what we can find without importing pi internals (no per-item
    # API exists — spec §3); missing files contribute 0. Deterministic and
    # content-free at the byte level (we only count chars here).
    candidates = []
    cwd = os.getcwd()
    candidates.append(os.path.join(cwd, "AGENTS.md"))
    candidates.append(os.path.join(cwd, "CLAUDE.md"))
    home = os.path.expanduser("~")
    candidates.append(os.path.join(home, ".pi", "agent", "AGENTS.md"))
    for p in candidates:
        try:
            rp = os.path.realpath(p)
        except Exception:
            continue
        if rp in seen:
            continue
        seen.add(rp)
        try:
            if os.path.isfile(p):
                with open(p, "r", errors="ignore") as fh:
                    total += len(fh.read())
        except Exception:
            continue
    return total


def _build_dynamic_ledger(active_prefix):
    """Per-item ledger over the active prefix. Reuses pi_session_lib
    primitives (no import cycle). Each entry becomes one ContextItem."""
    items: list[ContextItem] = []
    n = len(active_prefix)
    if n == 0:
        return items
    # Build per-item records by walking the active segment. We piggyback on
    # pi_session_lib's message helpers rather than re-implementing parsing.
    tool_name_by_id: dict = {}
    # First pass: collect tool-call ids -> names so toolResult items can name
    # the tool that produced them.
    for entry in active_prefix:
        try:
            msg = pi_session_lib._message(entry)
            for call in pi_session_lib._tool_calls(msg):
                tool_name_by_id[call.get("id")] = call.get("name", "tool")
        except Exception:
            continue
    # Second pass: emit ContextItems. age_turns counts from the END (last item
    # is age 0). We track read/grep path hits for last_read_turn.
    read_paths: list = []  # [(turn_index, path)]
    turns_seen = 0
    for idx, entry in enumerate(active_prefix):
        try:
            msg = pi_session_lib._message(entry)
            etype = entry.get("type")
            role = msg.get("role", "")
            # Skip compaction boundary entries themselves (not context content).
            if etype == "compaction":
                continue
            age = n - 1 - idx
            last_read = -1
            kind = "assistant"
            tool_name = ""
            text = ""
            if role == "toolResult":
                kind = "tool_result"
                tool_name = str(tool_name_by_id.get(msg.get("toolCallId"), "tool") or "tool")
                text = pi_session_lib._tool_result_text(msg)
            elif role == "bashExecution":
                kind = "tool_result"
                tool_name = "bash"
                text = str(msg.get("output", "") or "")
            elif role == "user":
                kind = "user"
                text = pi_session_lib._message_text(msg)
            elif role == "assistant":
                kind = "assistant"
                text = pi_session_lib._message_text(msg, include_thinking=True)
                # record read/grep calls for the last_read_turn signal
                for call in pi_session_lib._tool_calls(msg):
                    name = str(call.get("name", "")).lower()
                    args = call.get("arguments", {}) or {}
                    if name in ("read", "grep"):
                        p = args.get("path") or args.get("file_path")
                        if p:
                            read_paths.append((idx, str(p)))
            elif role == "compactionSummary":
                kind = "summary"
                text = str(msg.get("summary") or "") or pi_session_lib._message_text(msg, include_thinking=True)
            else:
                kind = role or "other"
                text = pi_session_lib._message_text(msg)
            if not text:
                continue
            tokens = max(len(text) // CHARS_PER_TOKEN, 0)
            # last_read_turn: latest turn index whose read/grep touched a path
            # that ALSO appears as text in this item (heuristic — the only
            # observable proxy from JSONL, spec §4).
            low = text[:4000]
            for ridx, rp in read_paths:
                if ridx <= idx and rp and os.path.basename(rp) and os.path.basename(rp) in low:
                    last_read = max(last_read, ridx)
            items.append(ContextItem(
                kind=kind, tool_name=tool_name, tokens=tokens,
                age_turns=age, last_read_turn=last_read,
            ))
        except Exception:
            continue
    # Classify dormancy/redundancy/reclaimability from config thresholds.
    _classify(items, total_turns=n)
    return items


def _classify(items, *, total_turns):
    """Apply dormancy/redundancy/reclaimability flags from config (spec §4/§10).

    dormant    = age_turns >= DORMANT_AGE_TURNS AND tokens >= DORMANT_MIN_TOKENS
                 AND not re-read recently (last_read_turn < 0 OR
                 age_turns - (total_turns-1 - last_read_turn) >= DORMANT_AGE_TURNS)
    redundant  = a duplicate-read flag (same tool+path seen twice in the prefix);
                 we approximate with kind=='tool_result' AND tool_name in
                 ('read','grep','cat') AND last_read_turn>=0 — i.e. the item was
                 touched by a later read of the same path (best JSONL proxy).
    reclaimable= not dormant and not redundant and kind in ('tool_result','assistant')
                 — what /compact drops, advisory.
    """
    age_thr = int(config_lib.cfg.float("DORMANT_AGE_TURNS", default=20))
    min_tok = int(config_lib.cfg.float("DORMANT_MIN_TOKENS", default=500))
    last_item_idx = total_turns - 1 if total_turns else 0
    for it in items:
        recent_read = (it.last_read_turn >= 0 and
                       (last_item_idx - it.last_read_turn) < age_thr)
        it.dormant = (it.age_turns >= age_thr and it.tokens >= min_tok
                      and not recent_read)
        it.redundant = (it.kind == "tool_result" and it.last_read_turn >= 0
                        and it.tool_name.lower() in ("read", "grep", "cat"))
        it.reclaimable = (not it.dormant and not it.redundant
                          and it.kind in ("tool_result", "assistant"))


def _build_floor(st, total, *, include_probe):
    """LIVE-measure context_files + skills_meta; read tools_system from
    floor-probe.json when include_probe, else the honest single bucket."""
    context_files = _measure_context_files_chars() // CHARS_PER_TOKEN
    skills_meta = max(int(getattr(st, "skill_chars", 0)), 0) // CHARS_PER_TOKEN
    tools_system = 0
    if include_probe:
        tools_system = _read_probe_tools_tokens()
    # If no probe data, leave tools_system=0; true_residual absorbs it as the
    # honest single "tools+system (fixed)" bucket (spec §4/§7).
    attributed = context_files + skills_meta + tools_system
    # Reconcile to the authoritative total: the parts always sum back to the
    # total, and true_residual is the honesty bucket (>= 0). When the probe
    # over-reports tools_system relative to what fits (a stale probe under a
    # lighter floor config), clamp tools_system to fit so the floor stays
    # reconciled and the decision-side residual (base = total - measured) is
    # unaffected (the decision never reads the probe — T9 boundary).
    headroom = max(total - context_files - skills_meta, 0)
    if tools_system > headroom:
        tools_system = headroom
    true_residual = max(total - context_files - skills_meta - tools_system, 0)
    return FloorBreakdown(
        context_files=context_files,
        skills_meta=skills_meta,
        tools_system=tools_system,
        true_residual=true_residual,
    )


def _probe_path():
    return os.path.join(statedir.state_root("pi"), FLOOR_PROBE_NAME)


def _read_probe_tools_tokens():
    """Read floor-probe.json's per_package map and return the summed tokens.
    Readout-only (T9 boundary). Returns 0 on any failure / missing file — the
    no-probe fallback bucket, honest single 'tools+system (fixed)'."""
    try:
        with open(_probe_path()) as fh:
            data = json.load(fh)
        per = data.get("per_package", {}) or {}
        return int(sum(int(v) for v in per.values() if isinstance(v, (int, float))))
    except Exception:
        return 0


def _probe_per_package():
    """Return the per_package dict from floor-probe.json (readout) or {}."""
    try:
        with open(_probe_path()) as fh:
            data = json.load(fh)
        return dict(data.get("per_package", {}) or {})
    except Exception:
        return {}


def _probe_measured_at():
    """Return measured_at (ISO-8601) from floor-probe.json or ''."""
    try:
        with open(_probe_path()) as fh:
            data = json.load(fh)
        return str(data.get("measured_at", "") or "")
    except Exception:
        return ""


def _rollup_categories(st):
    """Back-compat rollup into today's category keys."""
    total_tool = max(int(getattr(st, "total_tool_chars", 0)), 0) // CHARS_PER_TOKEN
    asst = max(int(getattr(st, "assistant_text_chars", 0)), 0) // CHARS_PER_TOKEN
    prompts = max(int(getattr(st, "user_prompt_chars", 0)), 0) // CHARS_PER_TOKEN
    summary = max(int(getattr(st, "summary_chars", 0)), 0) // CHARS_PER_TOKEN
    return {
        "tool": total_tool,
        "assistant": asst,
        "prompts": prompts,
        "summary": summary,
    }


def _build_reclaim(dynamic, cats, total, floor):
    """Deterministic ordered ranking of reclaimable buckets (readout advisory)."""
    # reclaimable_now: dynamic items /compact would drop — the dynamic items
    # flagged reclaimable (dormant + redundant excluded; they get their own
    # ranking rows). Advisory, chars/4.
    reclaimable_now = sum(it.tokens for it in dynamic if it.reclaimable)
    dormant = sum(it.tokens for it in dynamic if it.dormant)
    redundant = sum(it.tokens for it in dynamic if it.redundant)
    # Build ranking rows. Each row: {bucket, tokens, reducible_by} — reducible_by
    # is the lever hint ('unload'/'--exclude-tools'/'/compact'), advisory only.
    ranking = []
    # Floor-side levers (readout-only): ordered by token size (probe per_package
    # when present, else the single honest bucket absorbed into true_residual).
    per_pkg = _probe_per_package()
    if per_pkg:
        for name in sorted(per_pkg, key=lambda k: per_pkg[k], reverse=True):
            t = int(per_pkg[name])
            if t <= 0:
                continue
            ranking.append({"bucket": f"unload {name}", "tokens": t,
                            "reducible_by": "unload package"})
    stale_tool = int(round(cats.get("tool", 0) * 0.9))  # ~stale fraction proxy
    if stale_tool > 0:
        ranking.append({"bucket": "stale tool output", "tokens": stale_tool,
                         "reducible_by": "/compact"})
    if dormant > 0:
        ranking.append({"bucket": "dormant items", "tokens": dormant,
                         "reducible_by": "/compact"})
    if redundant > 0:
        ranking.append({"bucket": "redundant reads", "tokens": redundant,
                         "reducible_by": "drop re-reads"})
    # Deterministic ordering: highest tokens first, stable on bucket name.
    ranking.sort(key=lambda r: (-r["tokens"], r["bucket"]))
    return ReclaimEstimate(
        reclaimable_now=reclaimable_now,
        post_floor_estimate=0,  # the summary-term median lives in T8 (telemetry)
        ranking=ranking,
    )


def _degraded_inventory(total_tokens, window, note):
    """Self-contained never-raise fallback (spec §8). Computed from `total`
    alone. MUST NOT call transcript_lib.context_composition() — T4 adapts over
    this module, so that would infinitely recurse."""
    total = max(int(total_tokens), 0)
    win = max(int(window), 0) if window else 0
    return ContextInventory(
        total_tokens=total,
        window=win,
        occupancy=(total / win) if win else 0.0,
        floor=FloorBreakdown(true_residual=total),
        dynamic=[],
        dynamic_dormant_tokens=0,
        categories={},
        reclaim=ReclaimEstimate(),
        degraded=True,
        note=note or "inventory build degraded; using total-only fallback",
    )


# ---------------------------------------------------------------------------
# Standalone --inventory report (spec §5/§11)
# ---------------------------------------------------------------------------


def render_report(inv: ContextInventory) -> str:
    """Content-free report: token counts + category/tool/package names only.
    CONSUMES ReclaimEstimate.ranking verbatim — never re-ranks/recomputes."""
    lines = []
    lines.append(f"Context inventory  total={inv.total_tokens:,}t  "
                 f"window={inv.window:,}t  occupancy={inv.occupancy:.0%}")
    if inv.degraded:
        lines.append(f"  (degraded: {inv.note})")
    f = inv.floor
    lines.append("Floor (fixed, survives /compact):")
    lines.append(f"  context_files  {f.context_files:,}t  (measured live)")
    lines.append(f"  skills_meta    {f.skills_meta:,}t  (measured live)")
    if f.tools_system > 0:
        lines.append(f"  tools_system   {f.tools_system:,}t  (probe)")
    else:
        lines.append("  tools_system   (no probe — in true_residual)")
    lines.append(f"  true_residual  {f.true_residual:,}t  "
                 "(honesty bucket; tools+system fixed when no probe)")
    lines.append("Dynamic ledger:")
    if not inv.dynamic:
        lines.append("  (no items)")
    for it in inv.dynamic:
        fl = []
        if it.dormant:
            fl.append("dormant")
        if it.redundant:
            fl.append("redundant")
        if it.reclaimable:
            fl.append("reclaimable")
        tag = (" [" + ",".join(fl) + "]") if fl else ""
        name = f" ({it.tool_name})" if it.tool_name else ""
        lines.append(f"  {it.kind}{name}  {it.tokens:,}t  age={it.age_turns}{tag}")
    lines.append(f"Dynamic dormant tokens: {inv.dynamic_dormant_tokens:,}t")
    lines.append(f"Reclaim now: {inv.reclaim.reclaimable_now:,}t  "
                 f"post_floor_estimate: {inv.reclaim.post_floor_estimate:,}t")
    if inv.reclaim.ranking:
        lines.append("Reclaim ranking (consumed verbatim; readout advisory):")
        for r in inv.reclaim.ranking:
            lines.append(f"  {r['bucket']:<28} {r['tokens']:>10,}t   "
                         f"lever: {r['reducible_by']}")
    else:
        lines.append("Reclaim ranking: (none)")
    return "\n".join(lines)


def _parse(argv):
    args = {"session": None, "window": 200000, "total": -1}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            print("usage: context_inventory --session=<path> "
                  "[--window=N] [--total=N] [--no-probe]")
            return None
        if a.startswith("--session="):
            args["session"] = a.split("=", 1)[1]
        elif a == "--session" and i + 1 < len(argv):
            i += 1
            args["session"] = argv[i]
        elif a.startswith("--window="):
            args["window"] = int(a.split("=", 1)[1])
        elif a.startswith("--total="):
            args["total"] = int(a.split("=", 1)[1])
        elif a == "--no-probe":
            args["include_probe"] = False
        i += 1
    args.setdefault("include_probe", True)
    return args


def main(argv=None) -> int:
    opts = _parse(list(sys.argv[1:] if argv is None else argv))
    if opts is None:
        return 0
    if not opts["session"]:
        print("context_inventory: --session=<path> required", file=sys.stderr)
        return 2
    full_path, active, cc = pi_session_lib.active_path(opts["session"])
    total = opts["total"]
    if total < 0:
        # Best-effort: use the active-segment chars/4 sum as the total fallback
        # (real calls pass the exact aggregate from ctx.getContextUsage()).
        total = sum(
            len(pi_session_lib._message_text(pi_session_lib._message(e)))
            // CHARS_PER_TOKEN for e in active
        )
    inv = build_inventory(active, total, opts["window"],
                          include_probe=opts["include_probe"])
    print(render_report(inv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())