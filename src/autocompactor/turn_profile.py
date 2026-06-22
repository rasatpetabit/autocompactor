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
    tool_call_args: dict = field(default_factory=dict)
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
            rec.tool_call_args[call["name"]] = call.get("arguments", {})

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

    res.turns = turns
    return res
