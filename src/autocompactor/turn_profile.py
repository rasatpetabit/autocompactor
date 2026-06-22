#!/usr/bin/env python3
"""
turn_profile.py -- per-turn transcript profiler (harness-agnostic core).

Profiles each assistant LLM call in a Pi session: context-window size (occupancy,
autocompactor-consistent), pre-call tokens, growth delta, output, cost, cache-hit
ratio, tools emitted, and an interval "fed by" composition. Read-only diagnostic;
NOT wired into the pi_bridge compaction flow. See
docs/masterplan/turn-profiler/spec.md.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

from autocompactor import pi_session_lib, policy, transcript_lib


@dataclass
class TurnRecord:
    index: int
    role: str = "assistant"
    timestamp: _dt.datetime | None = None
    has_usage: bool = False
    occupancy: int = 0
    pre_call_tokens: int = 0
    delta_occupancy: int = 0
    input_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    cache_hit_ratio: float = 0.0
    fed_by_tokens: int = 0
    assistant_text_tokens: int = 0
    thinking_tokens: int = 0
    tools_called: list = field(default_factory=list)
    tool_call_args: list = field(default_factory=list)  # [{name, arguments}] per call
    fed_by: list = field(default_factory=list)
    is_error_turn: bool = False
    flags: list = field(default_factory=list)
    wall_seconds: float | None = None
    active_pos: int = -1      # position in the active segment (for rollup)


@dataclass
class HumanTurnRollup:
    index: int
    start_turn: int
    end_turn: int
    loop_len: int
    start_ctx: int
    end_ctx: int
    growth: int
    total_cost: float
    total_output: int
    tools: dict
    wall_seconds: float | None


@dataclass
class ProfileSummary:
    turn_count: int = 0
    human_turn_count: int = 0
    has_usage: bool = False
    peak_ctx: int = 0
    peak_turn_index: int | None = None
    start_ctx: int = 0
    final_ctx: int = 0
    total_cost: float = 0.0
    cost_split: dict = field(default_factory=dict)
    overall_cache_hit_ratio: float = 0.0
    avg_cache_write_per_turn: float = 0.0
    composition_at_peak: dict | None = None
    reclaimable_tokens: int = 0
    redundant_read_count: int = 0
    oversized_output_count: int = 0
    tool_frequency: dict = field(default_factory=dict)
    biggest_growth_turn: tuple | None = None
    biggest_tool_output_turn: tuple | None = None
    total_wall_seconds: float | None = None
    sparkline: str = ""
    per_tool_result_tokens: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


@dataclass
class ProfileResult:
    session_id: str = ""
    turns: list = field(default_factory=list)
    human_turns: list = field(default_factory=list)
    user_active_positions: list = field(default_factory=list)  # active idxs of user prompts
    summary: ProfileSummary = field(default_factory=ProfileSummary)


# ---- per-turn walk -------------------------------------------------------

def _interval_tokens(entries, tool_name_by_id):
    """Sum chars/4 over a slice of active entries (the fed-by interval),
    returning (total_tokens, [{role, tool_or_unknown, tokens, is_error}])."""
    total = 0
    breakdown = []
    for e in entries:
        msg = pi_session_lib._message(e)
        role = msg.get("role", "")
        is_error = bool(msg.get("isError")) or (
            role == "bashExecution"
            and msg.get("exitCode") not in (None, 0))
        if role == "toolResult":
            text = pi_session_lib._tool_result_text(msg)
            tool = tool_name_by_id.get(msg.get("toolCallId"), "unknown")
        elif role == "bashExecution":
            text = str(msg.get("output", "") or "")
            tool = "bash"
        elif role == "custom":
            text = pi_session_lib._message_text(msg)
            tool = "custom"
        elif role == "user":
            text = pi_session_lib._message_text(msg)
            tool = "user"
        elif role == "assistant":
            text = pi_session_lib._message_text(msg, include_thinking=True)
            tool = "assistant"
        else:
            text = pi_session_lib._message_text(msg)
            tool = role or "other"
        if not text:
            continue
        tok = len(text) // transcript_lib.CHARS_PER_TOKEN
        total += tok
        breakdown.append({"role": role, "tool": tool, "tokens": tok,
                          "is_error": is_error})
    return total, breakdown


def _thinking_only(message: dict) -> str:
    parts = []
    for block in pi_session_lib._content_blocks(message):
        if isinstance(block, dict) and block.get("type") == "thinking":
            t = block.get("thinking", "") or ""
            if t:
                parts.append(t)
    return "\n".join(parts)


def profile_turns(session: str, recent_window: int = 30) -> ProfileResult:
    """Build the per-turn profile for a Pi session JSONL. Never raises."""
    import os
    res = ProfileResult(session_id=os.path.splitext(
        os.path.basename(session or ""))[0] or "unknown")
    try:
        full_path, active, compaction_count = pi_session_lib.active_path(session)
    except Exception:
        res.summary.warnings.append("could not read/parse session")
        return res

    # Map toolCall id -> name across the active segment for fed-by resolution.
    tool_name_by_id = {}
    for e in active:
        for call in pi_session_lib._tool_calls(pi_session_lib._message(e)):
            if call.get("id"):
                tool_name_by_id[call["id"]] = call["name"]

    turns = []
    prev_occupancy = None
    prev_assistant_pos = 0      # inclusive start of the next fed-by interval
    prev_ts = None
    for i, e in enumerate(active):
        msg = pi_session_lib._message(e)
        if msg.get("role") == "user":
            res.user_active_positions.append(i)
        if msg.get("role") != "assistant":
            continue
        usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else None
        rec = TurnRecord(index=len(turns), timestamp=pi_session_lib._entry_ts(e),
                         active_pos=i)
        rec.has_usage = bool(usage)
        if usage:
            rec.occupancy = pi_session_lib._usage_context(usage)
            rec.input_tokens = int(usage.get("input", 0) or 0)
            rec.cache_read = int(usage.get("cacheRead", 0) or 0)
            rec.cache_write = int(usage.get("cacheWrite", 0) or 0)
            rec.output_tokens = int(usage.get("output", 0) or 0)
            cost = usage.get("cost")
            rec.cost = float(cost.get("total", 0.0)) if isinstance(cost, dict) else 0.0
            rec.pre_call_tokens = rec.input_tokens + rec.cache_read + rec.cache_write
            rec.cache_hit_ratio = (rec.cache_read / rec.pre_call_tokens
                                   if rec.pre_call_tokens else 0.0)
            rec.delta_occupancy = (rec.occupancy - prev_occupancy
                                   if prev_occupancy is not None else 0)
            prev_occupancy = rec.occupancy

        # tools emitted this turn (feed the NEXT call)
        for call in pi_session_lib._tool_calls(msg):
            rec.tools_called.append(call["name"])
            # per-call list (not name->args dict) so repeated calls with
            # different args (e.g. parallel reads) are all preserved.
            rec.tool_call_args.append(
                {"name": call["name"], "arguments": call.get("arguments", {})})

        # assistant text + thinking this turn (estimated)
        rec.assistant_text_tokens = len(
            pi_session_lib._message_text(msg)) // transcript_lib.CHARS_PER_TOKEN
        rec.thinking_tokens = len(
            _thinking_only(msg)) // transcript_lib.CHARS_PER_TOKEN

        # fed-by = interval [prev_assistant_pos, i) inclusive of prev assistant
        fed_total, fed_breakdown = _interval_tokens(
            active[prev_assistant_pos:i], tool_name_by_id)
        rec.fed_by_tokens = fed_total
        rec.fed_by = fed_breakdown
        rec.is_error_turn = any(fb["is_error"] for fb in fed_breakdown)

        # wall-clock since previous turn
        if rec.timestamp and prev_ts:
            rec.wall_seconds = (rec.timestamp - prev_ts).total_seconds()
        if rec.timestamp:
            prev_ts = rec.timestamp

        prev_assistant_pos = i
        turns.append(rec)

    _compute_flags(turns)
    res.turns = turns
    return res


def _compute_flags(turns, *, large_output=5000, redundant_window=10,
                   think_bloat_x=5, idle_gap_min=30):
    seen_reads = []  # list of (turn_index, path)
    for j, t in enumerate(turns):
        if len(t.tools_called) > 1:
            t.flags.append("parallel-tools")
        if t.thinking_tokens > think_bloat_x * max(t.output_tokens, 1) and t.thinking_tokens > 0:
            t.flags.append("think-bloat")
        if any(fb["is_error"] for fb in t.fed_by):
            t.flags.append("error-retry")
        if any(fb["tokens"] >= large_output for fb in t.fed_by):
            t.flags.append("large-output")
        if t.wall_seconds is not None and t.wall_seconds >= idle_gap_min * 60:
            t.flags.append("idle-gap")
        # redundant read: same path read within the last redundant_window turns
        for ca in t.tool_call_args:
            name = ca.get("name", "")
            args = ca.get("arguments", {})
            if name.lower() in ("read", "grep"):
                p = args.get("path")
                if p and any(p == sp for k, sp in seen_reads
                             if j - k <= redundant_window and k != j):
                    t.flags.append("redundant-read")
                if p:
                    seen_reads.append((j, p))
        # error-retry: 2+ consecutive error turns
        if j >= 1 and t.is_error_turn and turns[j - 1].is_error_turn \
                and "error-retry" not in t.flags:
            t.flags.append("error-retry")


_SPARK = "▁▂▃▄▅▆▇█"


def _sparkline(values):
    if not values:
        return ""
    lo, hi = min(values), max(values)
    if hi <= lo:
        return _SPARK[3] * len(values)
    return "".join(
        _SPARK[min(len(_SPARK) - 1,
                   int((v - lo) / (hi - lo) * (len(_SPARK) - 1)))]
        for v in values)


def summarize(res: ProfileResult, post_floor: float = 70000,
              full_path=None, active=None, compaction_count: int = 0,
              recent_window: int = 30) -> ProfileSummary:
    turns = res.turns
    s = res.summary
    s.turn_count = len(turns)
    used = [t for t in turns if t.has_usage]
    s.has_usage = bool(used)
    # start_ctx/final_ctx bound to usage-bearing turns so a mixed transcript
    # (no-usage turns at head/tail) doesn't report "grew 0→0" while has_usage
    # is true and a nonzero peak was observed.
    if used:
        s.start_ctx = used[0].occupancy
        s.final_ctx = used[-1].occupancy
    elif turns:
        s.start_ctx = turns[0].occupancy
        s.final_ctx = turns[-1].occupancy
    if used:
        occupancies = [t.occupancy for t in turns]
        s.peak_ctx = max(occupancies)
        s.peak_turn_index = occupancies.index(s.peak_ctx)
        s.total_cost = sum(t.cost for t in turns)
        cr = sum(t.cache_read for t in used)
        pre = sum(t.pre_call_tokens for t in used)
        s.overall_cache_hit_ratio = cr / pre if pre else 0.0
        s.avg_cache_write_per_turn = (sum(t.cache_write for t in used) / len(used))
        deltas = [(t.index, t.delta_occupancy) for t in turns if t.delta_occupancy]
        if deltas:
            s.biggest_growth_turn = max(deltas, key=lambda kv: kv[1])
        outs = [(t.index, t.fed_by_tokens) for t in turns if t.fed_by_tokens]
        if outs:
            s.biggest_tool_output_turn = max(outs, key=lambda kv: kv[1])
        s.reclaimable_tokens = max(s.peak_ctx - int(post_floor), 0)

    # tool frequency + per-tool result tokens (review P2#4: result-token share,
    # NOT cost attribution)
    freq = {}
    per_tool = {}
    for t in turns:
        for name in t.tools_called:
            freq[name] = freq.get(name, 0) + 1
        for fb in t.fed_by:
            if fb["role"] in ("toolResult", "bashExecution"):
                n = fb["tool"]
                slot = per_tool.setdefault(
                    n, {"tool": n, "calls": 0, "result_tokens": 0})
                slot["result_tokens"] += fb["tokens"]
                slot["calls"] += 1
    s.tool_frequency = freq
    total_result = sum(v["result_tokens"] for v in per_tool.values()) or 1
    s.per_tool_result_tokens = sorted(
        [{"tool": v["tool"], "calls": v["calls"],
          "result_tokens": v["result_tokens"],
          "share": round(v["result_tokens"] / total_result, 3)}
         for v in per_tool.values()],
        key=lambda d: d["result_tokens"], reverse=True)
    # flag-derived counts for the summary line
    s.redundant_read_count = sum(
        1 for t in turns if "redundant-read" in t.flags)
    s.oversized_output_count = sum(
        1 for t in turns if "large-output" in t.flags)

    # composition @ peak (one prefix analysis; review P2#3)
    if used and full_path is not None and active is not None:
        try:
            st = pi_session_lib.analyze_active_prefix(
                full_path, active, recent_window, compaction_count)
            s.composition_at_peak = transcript_lib.context_composition(
                st, st.context_tokens)
        except Exception:
            s.composition_at_peak = None

    # wall-clock total
    wall = [t.wall_seconds for t in turns if t.wall_seconds is not None]
    s.total_wall_seconds = sum(wall) if wall else None

    s.sparkline = _sparkline([t.occupancy for t in turns])
    if not turns:
        s.warnings.append("no assistant turns in active segment")
    elif not used:
        s.warnings.append("no usage blocks found; exact token/cost fields are 0")
    return s


# ---- human-turn rollup ---------------------------------------------------

def rollup(res: ProfileResult) -> list:
    """Collapse consecutive assistant turns under each user prompt.

    Uses the active positions captured during profile_turns()
    (res.turns[i].active_pos and res.user_active_positions) -- no re-walk,
    no file reload.
    """
    import bisect
    user_positions = res.user_active_positions
    human = []
    groups = {}
    for ti, t in enumerate(res.turns):
        idx = bisect.bisect_right(user_positions, t.active_pos) - 1
        groups.setdefault(idx if idx >= 0 else -1, []).append(ti)
    for key in sorted(groups):
        idxs = groups[key]
        tslice = [res.turns[k] for k in idxs]
        if not tslice:
            continue
        human.append(HumanTurnRollup(
            index=len(human), start_turn=idxs[0], end_turn=idxs[-1],
            loop_len=len(idxs),
            start_ctx=tslice[0].occupancy, end_ctx=tslice[-1].occupancy,
            growth=tslice[-1].occupancy - tslice[0].occupancy,
            total_cost=sum(t.cost for t in tslice),
            total_output=sum(t.output_tokens for t in tslice),
            tools={n: sum(1 for t in tslice if n in t.tools_called)
                   for n in {n for t in tslice for n in t.tools_called}},
            wall_seconds=sum(t.wall_seconds for t in tslice
                             if t.wall_seconds is not None) or None))
    return human


# ---- text report ---------------------------------------------------------

def _counted(names):
    d = {}
    for n in names:
        d[n] = d.get(n, 0) + 1
    return d


def format_text(res: ProfileResult) -> str:
    s = res.summary
    out = []
    for t in res.turns:
        tools = " ".join(
            f"{n}" + (f"×{c}" if c > 1 else "")
            for n, c in _counted(t.tools_called).items()) or "—"
        out.append(
            f"T {t.index:>3} {t.role}  ctx {policy._fmt_tokens(t.occupancy)}"
            + (f" ▲{policy._fmt_tokens(t.delta_occupancy)}"
               if t.delta_occupancy else "")
            + f"  out {policy._fmt_tokens(t.output_tokens)}"
            f"  ${t.cost:.2f}  cache {t.cache_hit_ratio:.0%}  {tools}")
        if t.fed_by_tokens:
            seg = " · ".join(
                f"{policy._fmt_tokens(fb['tokens'])} {fb['tool']}"
                for fb in t.fed_by[:3])
            out.append(f"      └ fed by ≈{policy._fmt_tokens(t.fed_by_tokens)} "
                       f"({seg})")
        if t.flags:
            out.append(f"      ⚠ {' · '.join(t.flags)}")
    out.append("══════════════ SESSION PROFILE ══════════════")
    out.append(f"{s.turn_count} turns · peak {policy._fmt_tokens(s.peak_ctx)} "
               f"· grew {policy._fmt_tokens(s.start_ctx)}→"
               f"{policy._fmt_tokens(s.final_ctx)}")
    if s.sparkline:
        out.append("ctx trend " + s.sparkline)
    if s.has_usage:
        out.append(f"Cost ${s.total_cost:.2f}  ·  cache "
                   f"{s.overall_cache_hit_ratio:.0%} hit overall")
    if s.composition_at_peak:
        line = policy.composition_line(s.composition_at_peak)
        if line:
            out.append("Composition @ peak " + line)
    if s.has_usage:
        out.append(f"Reclaimable ≈{policy._fmt_tokens(s.reclaimable_tokens)} "
                   f"· {s.redundant_read_count} redundant reads · "
                   f"{s.oversized_output_count} oversized outputs")
        if s.per_tool_result_tokens:
            out.append("\nPer-tool result tokens   calls   result-tok   share")
            for r in s.per_tool_result_tokens[:8]:
                out.append(f"  {r['tool']:<18} {r['calls']:<7} "
                           f"{policy._fmt_tokens(r['result_tokens']):<11} "
                           f"{r['share']:.0%}")
    for w in s.warnings:
        out.append(f"⚠ {w}")
    return "\n".join(out)


# ---- CLI ------------------------------------------------------------------

import json as _json
import sys


def _parse(argv):
    opts = {}
    i = 0
    while i < len(argv):
        tok = argv[i]
        if isinstance(tok, str) and tok.startswith("--"):
            key = tok[2:].replace("-", "_")
            if i + 1 < len(argv) and not str(argv[i + 1]).startswith("--"):
                opts[key] = argv[i + 1]; i += 2; continue
            opts[key] = "1"   # valueless flag -> truthy marker
        i += 1
    return opts


def _to_int(v, default=None):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _to_json(res: ProfileResult) -> str:
    def _turn(t):
        return {
            "index": t.index, "role": t.role, "has_usage": t.has_usage,
            "occupancy": t.occupancy, "pre_call_tokens": t.pre_call_tokens,
            "delta_occupancy": t.delta_occupancy, "input_tokens": t.input_tokens,
            "cache_read": t.cache_read, "cache_write": t.cache_write,
            "output_tokens": t.output_tokens, "cost": t.cost,
            "cache_hit_ratio": t.cache_hit_ratio, "fed_by_tokens": t.fed_by_tokens,
            "assistant_text_tokens": t.assistant_text_tokens,
            "thinking_tokens": t.thinking_tokens, "tools_called": t.tools_called,
            "tool_call_args": t.tool_call_args, "fed_by": t.fed_by,
            "is_error_turn": t.is_error_turn, "flags": t.flags,
            "wall_seconds": t.wall_seconds,
            "timestamp": t.timestamp.isoformat() if t.timestamp else None,
        }
    s = res.summary
    return _json.dumps({
        "session_id": res.session_id,
        "turns": [_turn(t) for t in res.turns],
        "human_turns": [vars(h) for h in res.human_turns],
        "summary": {
            **vars(s),
            "biggest_growth_turn": (list(s.biggest_growth_turn)
                                    if s.biggest_growth_turn else None),
            "biggest_tool_output_turn": (list(s.biggest_tool_output_turn)
                                         if s.biggest_tool_output_turn else None),
        },
    }, default=str)


def main(argv=None) -> int:
    opts = _parse(list(sys.argv[1:] if argv is None else argv))
    session = opts.get("session", "")
    try:
        from autocompactor import config_lib
        post_floor = config_lib.cfg.float("POST_FLOOR", default=70000)
    except Exception:
        post_floor = 70000
    recent = _to_int(opts.get("recent"), 30) or 30
    try:
        res = profile_turns(session, recent_window=recent)
        full = active = None; cc = 0
        try:
            full, active, cc = pi_session_lib.active_path(session)
        except Exception:
            pass
        summarize(res, post_floor=post_floor, full_path=full, active=active,
                  compaction_count=cc, recent_window=recent)
        if opts.get("rollup"):
            res.human_turns = rollup(res)
        if opts.get("json"):
            sys.stdout.write(_to_json(res) + "\n")
        else:
            sys.stdout.write(format_text(res) + "\n")
    except Exception as e:  # never-raise
        if opts.get("json"):
            sys.stdout.write(_json.dumps(
                {"session_id": "", "turns": [], "human_turns": [],
                 "summary": {"warnings": [f"profile failed: {e}"]}}) + "\n")
        else:
            sys.stdout.write(f"turn-profile: could not profile session: {e}\n")
    return 0
