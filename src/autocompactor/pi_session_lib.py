#!/usr/bin/env python3
"""
pi_session_lib.py -- Pi coding-agent session adapter.

Pi v3 sessions are JSONL trees.  The live conversation is the path from the
active leaf back to the root, not the full file.  This module extracts that
path, cuts it at the latest compaction entry, and fills the same
TranscriptStats object used by transcript_lib's Claude transcript parser.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re

from autocompactor import transcript_lib


PI_TEST_PASS_RE = re.compile(r"(^|\n)\s*PASS\b")
PI_TOOL_ARG_KEYS = {
    "read": ("path", "offset", "limit"),
    "write": ("path", "content"),
    "edit": ("path", "edits"),
    "grep": ("pattern", "path", "glob", "ignoreCase"),
    "find": ("pattern", "path", "limit"),
    "bash": ("command", "timeout"),
}


def _load_jsonl(path: str) -> list:
    entries = []
    try:
        with open(os.path.expanduser(path), "rb") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line.decode("utf-8", "replace"))
                except (TypeError, json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(entry, dict):
                    entries.append(entry)
    except Exception:
        pass
    return entries


def _leaf_path(entries: list) -> list:
    nodes = [e for e in entries if e.get("id")]
    if not nodes:
        return []

    by_id = {}
    parent_ids = set()
    for entry in nodes:
        entry_id = entry.get("id")
        if entry_id:
            by_id[entry_id] = entry
        parent_id = entry.get("parentId")
        if parent_id:
            parent_ids.add(parent_id)

    leaves = [e for e in nodes if e.get("id") not in parent_ids]
    leaf = leaves[-1] if leaves else nodes[-1]

    path = []
    seen = set()
    cur = leaf
    while isinstance(cur, dict):
        entry_id = cur.get("id")
        if not entry_id or entry_id in seen:
            break
        seen.add(entry_id)
        path.append(cur)
        parent_id = cur.get("parentId")
        if not parent_id:
            break
        cur = by_id.get(parent_id)
    path.reverse()
    return path


def _active_segment(path: list) -> tuple[list, int]:
    compaction_indexes = [
        idx for idx, entry in enumerate(path)
        if entry.get("type") == "compaction"
    ]
    if not compaction_indexes:
        return path, 0

    last_idx = compaction_indexes[-1]
    start = last_idx + 1
    first_kept = path[last_idx].get("firstKeptEntryId")
    if first_kept:
        for idx in range(last_idx + 1, len(path)):
            if path[idx].get("id") == first_kept:
                start = idx
                break
    return path[start:], len(compaction_indexes)


def _message(entry: dict) -> dict:
    msg = entry.get("message")
    return msg if isinstance(msg, dict) else {}


def _content_blocks(message: dict) -> list:
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content if isinstance(content, list) else []


# Single-source the block flattener: transcript_lib._block_text already
# handles text / thinking / nested-content blocks identically. Aliasing here
# (rather than re-implementing) keeps the two harnesses from drifting.
_block_text = transcript_lib._block_text


def _message_text(message: dict, include_thinking: bool = False) -> str:
    parts = []
    for block in _content_blocks(message):
        if isinstance(block, dict) and block.get("type") == "toolCall":
            continue
        if not include_thinking and isinstance(block, dict):
            if block.get("type") == "thinking":
                continue
        text = _block_text(block)
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _entry_ts(entry: dict):
    ts = entry.get("timestamp")
    if not ts:
        ts = _message(entry).get("timestamp")
    if not ts:
        return None
    try:
        if isinstance(ts, (int, float)):
            return _dt.datetime.fromtimestamp(ts / 1000.0, _dt.timezone.utc)
        return _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def _usage_context(usage: dict) -> int:
    if not isinstance(usage, dict):
        return 0
    try:
        total = int(usage.get("totalTokens", 0) or 0)
    except (TypeError, ValueError):
        total = 0
    if total:
        return total
    total = 0
    raw_keys = ("input", "cacheRead", "cacheWrite", "output")
    compat_keys = (
        "input_tokens", "cache_read_input_tokens",
        "cache_creation_input_tokens", "output_tokens",
    )
    keys = raw_keys if any(k in usage for k in raw_keys) else compat_keys
    for key in keys:
        try:
            total += int(usage.get(key, 0) or 0)
        except (TypeError, ValueError):
            pass
    return total


def _usage_compat(usage: dict) -> dict:
    if not isinstance(usage, dict):
        return {}
    compat = dict(usage)
    compat.setdefault("input_tokens", usage.get("input", 0))
    compat.setdefault("output_tokens", usage.get("output", 0))
    compat.setdefault("cache_read_input_tokens", usage.get("cacheRead", 0))
    compat.setdefault("cache_creation_input_tokens", usage.get("cacheWrite", 0))
    return compat


def _remember_path(seen: dict, path, idx: int) -> None:
    if isinstance(path, str) and path:
        seen[path] = idx


_DONE_TODO_STATUSES = frozenset({"completed", "done", "cancelled"})


def _apply_todos(st, args) -> None:
    """Fill st.todos + derived todo_step / todos_all_done from a tool call.

    Accepts Claude-style ``{"todos": [{"content", "status"}, ...]}`` args
    (and the same under a bare list). Never raises; leaves prior state on
    unusable input so a partial/malformed call cannot wipe a good list.
    """
    if not isinstance(args, dict):
        return
    raw = args.get("todos")
    if not isinstance(raw, list):
        return
    todos = [t for t in raw if isinstance(t, dict)]
    if not todos:
        return
    st.todos = todos
    statuses = [str(t.get("status") or "").lower() for t in todos]
    pending = [s for s in statuses if s not in _DONE_TODO_STATUSES]
    done = [s for s in statuses if s in ("completed", "done")]
    st.todos_all_done = not pending
    st.todo_step = bool(done) and bool(pending)


def _tool_calls(message: dict) -> list:
    calls = []
    for block in _content_blocks(message):
        if not isinstance(block, dict) or block.get("type") != "toolCall":
            continue
        args = block.get("arguments")
        calls.append({
            "id": block.get("id"),
            "name": str(block.get("name", "")),
            "arguments": args if isinstance(args, dict) else {},
        })
    return calls


def _tool_result_text(message: dict) -> str:
    return "\n".join(
        _block_text(block) for block in _content_blocks(message)
    ).strip()


def _record_tool_breakdown(st, tool_name: str, chars: int, is_recent: bool) -> None:
    name = str(tool_name or "tool").strip().lower() or "tool"
    st.tool_chars_by_name[name] = st.tool_chars_by_name.get(name, 0) + chars
    if not is_recent:
        st.stale_tool_chars_by_name[name] = (
            st.stale_tool_chars_by_name.get(name, 0) + chars
        )


def _record_result_text(st, text: str, is_error: bool, is_recent: bool) -> None:
    st.total_tool_chars += len(text)
    if not is_recent:
        st.stale_tool_chars += len(text)
    if is_error:
        key = text[:160].strip()
        if key:
            st.error_ledger[key] = st.error_ledger.get(key, 0) + 1
    if is_recent:
        st.recent_words |= transcript_lib._content_words(text[:1500])
        for m in transcript_lib.HEX_RE.finditer(text[:4000]):
            a, b = max(0, m.start() - 30), m.end() + 30
            st.hex_constants.append(text[a:b].replace("\n", " ").strip())
        if is_error:
            st.recent_errors.append(text[:300])
        if (not is_error
                and (transcript_lib.TEST_PASS_RE.search(text[:2000])
                     or PI_TEST_PASS_RE.search(text[:2000]))):
            st.recent_tests_pass = True



def _interval_tokens(entries, tool_name_by_id):
    """Sum chars/4 over a slice of active entries (the fed-by interval),
    returning (total_tokens, [{role, tool, tokens, is_error}]).

    Neutral home for the per-interval tool-breakdown family shared by
    turn_profile and (later) context_inventory: it walks a slice of the
    active segment and attributes tokens per role/tool. Never raises -
    callers wrap exceptions as they see fit. (Hoisted from turn_profile.py
    per context-window-analysis Task 2; pi_session_lib <- transcript_lib
    only, so there is no import cycle.)"""
    total = 0
    breakdown = []
    for e in entries:
        msg = _message(e)
        role = msg.get("role", "")
        is_error = bool(msg.get("isError")) or (
            role == "bashExecution"
            and msg.get("exitCode") not in (None, 0))
        if role == "toolResult":
            text = _tool_result_text(msg)
            tool = tool_name_by_id.get(msg.get("toolCallId"), "unknown")
        elif role == "bashExecution":
            text = str(msg.get("output", "") or "")
            tool = "bash"
        elif role == "custom":
            text = _message_text(msg)
            tool = "custom"
        elif role == "user":
            text = _message_text(msg)
            tool = "user"
        elif role == "assistant":
            text = _message_text(msg, include_thinking=True)
            tool = "assistant"
        else:
            text = _message_text(msg)
            tool = role or "other"
        if not text:
            continue
        tok = len(text) // transcript_lib.CHARS_PER_TOKEN
        total += tok
        breakdown.append({"role": role, "tool": tool, "tokens": tok,
                          "is_error": is_error})
    return total, breakdown


def compute_turn_flags(turns, *, large_output=5000, redundant_window=10,
                        think_bloat_x=5, idle_gap_min=30):
    """Per-turn behavior-flag engine (neutral home).

    Operates on a list of duck-typed turn records whose attributes match
    turn_profile.TurnRecord; it never imports TurnRecord, so there is no
    cycle (pi_session_lib <- transcript_lib only). Mutates `t.flags` in
    place. Shared by turn_profile (diagnostics) and context_inventory
    (per-item classification). Renamed from `_compute_flags` so external
    callers can reach the neutral home; turn_profile keeps a back-compat
    alias."""
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
        if j >= 1 and t.is_error_turn and turns[j - 1].is_error_turn                 and "error-retry" not in t.flags:
            t.flags.append("error-retry")


# Back-compat alias matching the original private name in turn_profile.
_compute_flags = compute_turn_flags


def active_path(path: str) -> tuple[list, list, int]:
    """Extract the live conversation path and its post-compaction active segment.

    Returns (full_path, active, compaction_count):
      full_path        — root->leaf path through the tree (founding prompts +
                         carried summary live here, before the compaction cut)
      active           — segment after the last compaction boundary
      compaction_count — number of compaction entries on the path
    Factored from _leaf_path + _active_segment for reuse by turn_profile
    without importing many private helpers.
    """
    full_path = _leaf_path(_load_jsonl(path))
    active, compaction_count = _active_segment(full_path)
    return full_path, active, compaction_count


def analyze_active_prefix(full_path, active, recent_window: int = 30,
                          compaction_count: int = 0):
    """Run the analyze() walk over an explicit (full_path, active prefix).

    `full_path` provides founding prompts + carried summary (pre-cut context);
    `active` is the segment to walk (the post-compaction segment for analyze(),
    or a prefix of it up to some turn for composition-at-peak). Behavior-
    identical to analyze() when called with the full active segment.
    """
    st = transcript_lib.TranscriptStats()
    st.entries = active
    st.compaction_count = compaction_count

    # summary_chars: single-source the carried compaction summary.
    # Prefer an explicit `compactionSummary`-role message; fall back to the
    # `compaction` entry's own summary text. Never count both (double-count
    # guard, spec §9). Scan the full path, not the active segment — the
    # summary lives just before the cut.
    summary_text = ""
    for entry in full_path:
        m = _message(entry)
        if m.get("role") == "compactionSummary":
            # Pi carries the carried summary as a `summary` string field on the
            # message; fall back to content blocks for other shapes.
            summary_text = str(m.get("summary") or "") or _message_text(
                m, include_thinking=True)
            break
    if not summary_text:
        for entry in full_path:
            if entry.get("type") == "compaction":
                summary_text = str(entry.get("summary") or "") or _message_text(
                    _message(entry), include_thinking=True)
                break
    st.summary_chars = len(summary_text)

    # Founding-goal capture walks the FULL path, not the active segment:
    # the prompts that framed the session live before the last compaction.
    for entry in full_path:
        if (len(st.initial_user_prompts)
                >= transcript_lib.INITIAL_PROMPTS_MAX):
            break
        if entry.get("type") == "compaction":
            continue
        msg = _message(entry)
        if msg.get("role") != "user":
            continue
        text = _message_text(msg).strip()
        if text and not text.startswith("/") and "<command-name>" not in text:
            st.initial_user_prompts.append(
                text[:transcript_lib.INITIAL_PROMPT_CHARS])

    edited, read = {}, {}
    pending_bash = {}
    pending_tool_names = {}
    recent_result_flags = []
    prev_ts = None
    n = len(active)

    for idx, entry in enumerate(active):
        is_recent = idx >= n - recent_window

        ts = _entry_ts(entry)
        if is_recent and ts and prev_ts:
            gap = (ts - prev_ts).total_seconds() / 60.0
            st.idle_gap_minutes = max(st.idle_gap_minutes, gap)
        if ts:
            prev_ts = ts

        etype = entry.get("type")
        msg = _message(entry)
        role = msg.get("role")

        if etype == "compaction":
            continue

        if role == "assistant":
            asst_text = _message_text(msg, include_thinking=False)
            st.assistant_text_chars += len(
                _message_text(msg, include_thinking=True))
            usage = msg.get("usage")
            if isinstance(usage, dict):
                st.last_usage = _usage_compat(usage)
                st.usage_series.append(_usage_context(usage))
            # Mechanical open-work extraction (wait monitors / on-success).
            if asst_text:
                hits = transcript_lib.extract_open_work_from_text(asst_text)
                if hits:
                    st.open_work = transcript_lib.merge_open_work(
                        st.open_work, hits)
            # Todos: best-effort. Live Pi sessions (re-scanned 2026-07-18)
            # do not emit a TodoWrite-class tool; when a Claude-shaped or
            # future Pi-shaped call appears, fill st.todos + derived flags.
            # progress_lib.extract_todos / resolve_next_step tolerate empty.

            for call in _tool_calls(msg):
                name = call["name"]
                lname = name.lower()
                args = call["arguments"]
                call_id = call.get("id")
                if call_id:
                    pending_tool_names[call_id] = lname or "tool"

                if lname in ("edit", "write"):
                    _remember_path(edited, args.get("path"), idx)
                elif lname in ("todowrite", "todo_write", "todo"):
                    _apply_todos(st, args)
                elif lname == "read":
                    _remember_path(read, args.get("path"), idx)
                elif lname == "grep":
                    if args.get("path") and not args.get("glob"):
                        _remember_path(read, args.get("path"), idx)
                elif lname == "bash":
                    cmd = str(args.get("command", "") or "")
                    pending_bash[call.get("id")] = cmd
                    if is_recent and "git commit" in cmd:
                        st.recent_commit = True
                    # Bash that enqueues/monitors a background build is also
                    # open-work evidence (often no prose wait declaration yet).
                    # Seed a wait verb so extract_open_work_from_text accepts
                    # monitor commands that already carry a resource handle.
                    if cmd and (
                        "yanos-builder show" in cmd
                        or "yanos-builder logs" in cmd
                        or ("yanos-builder" in cmd and "enqueue" in cmd)
                        or ("BUILD_ID=" in cmd and "yanos-builder" in cmd)
                    ):
                        seeded = cmd
                        if not transcript_lib.WAIT_VERB_RE.search(cmd):
                            seeded = cmd + "\n# monitor; leaving build running"
                        cmd_hits = transcript_lib.extract_open_work_from_text(
                            seeded)
                        if cmd_hits:
                            st.open_work = transcript_lib.merge_open_work(
                                st.open_work, cmd_hits)
                elif lname in ("task", "agent") and is_recent:
                    st.task_tool_recent = True

        elif role == "toolResult":
            text = _tool_result_text(msg)
            is_error = bool(msg.get("isError"))
            tool_call_id = msg.get("toolCallId")
            tool_name = pending_tool_names.pop(tool_call_id, "tool")
            _record_result_text(st, text, is_error, is_recent)
            _record_tool_breakdown(st, tool_name, len(text), is_recent)
            cmd = pending_bash.pop(tool_call_id, None)
            if cmd and not is_error and cmd not in st.working_commands:
                st.working_commands.append(cmd)
            if is_recent:
                recent_result_flags.append(is_error)
            # Tool output often carries the concrete BUILD_ID after enqueue
            # ("enqueued Y260717-…") even when the assistant never restated it.
            # Strict: only live enqueue / single-build running|queued status.
            # Skip multi-build `yanos-builder status` dumps (many historical ids).
            if text:
                multi_dump = (
                    text.count("status  :") >= 2
                    or text.count("Build Y") >= 2
                    or (cmd and re.search(
                        r"yanos-builder\s+status\b", cmd))
                )
                live_wait = (not multi_dump) and bool(
                    re.search(r"\benqueued\b", text, re.I)
                    or re.search(
                        r"status\s*:\s*(running|queued)\b", text, re.I)
                )
                if live_wait:
                    seeded = ((cmd + "\n") if cmd else "") + text
                    if not transcript_lib.WAIT_VERB_RE.search(seeded):
                        seeded += "\n# monitor; leaving build running"
                    hits = transcript_lib.extract_open_work_from_text(seeded)
                    if hits:
                        st.open_work = transcript_lib.merge_open_work(
                            st.open_work, hits)

        elif role == "bashExecution":
            cmd = str(msg.get("command", "") or "")
            text = str(msg.get("output", "") or "")
            is_error = bool(msg.get("cancelled")) or (
                msg.get("exitCode") not in (None, 0)
            )
            if is_recent and "git commit" in cmd:
                st.recent_commit = True
            _record_result_text(st, text, is_error, is_recent)
            _record_tool_breakdown(st, "bash", len(text), is_recent)
            if cmd and not is_error and cmd not in st.working_commands:
                st.working_commands.append(cmd)
            if is_recent:
                recent_result_flags.append(is_error)

        elif role == "user":
            text = _message_text(msg).strip()
            if text and not text.startswith("/") and "<command-name>" not in text:
                st.user_prompt_chars += len(text)
                # Hygiene: ignore trivial pings (status?) and strip base64/
                # image bulk so last_user_task stays a real goal string.
                if not transcript_lib.is_trivial_user_ping(text):
                    cleaned = transcript_lib.sanitize_user_task_text(text)
                    if cleaned and len(cleaned.strip()) >= 8:
                        st.last_user_task = cleaned[:500]
                if is_recent:
                    st.recent_words |= transcript_lib._content_words(text)
                if transcript_lib.CORRECTION_RE.search(text):
                    st.corrections.append(text[:200])

        elif role == "custom":
            text = _message_text(msg).strip()
            if is_recent and text:
                st.recent_words |= transcript_lib._content_words(text)

    st.context_tokens = _usage_context(st.last_usage)
    st.edited_files = [fp for fp, _ in sorted(edited.items(), key=lambda kv: kv[1])]
    st.read_files = [
        fp for fp, _ in sorted(read.items(), key=lambda kv: kv[1])
        if fp not in edited
    ]
    st.recent_errors = st.recent_errors[-3:]
    st.working_commands = st.working_commands[-15:]
    st.corrections = st.corrections[-20:]
    st.hex_constants = st.hex_constants[-20:]

    if recent_result_flags:
        had_err = any(recent_result_flags)
        tail = recent_result_flags[-3:]
        st.recent_error_then_clean = (
            had_err and len(tail) == 3 and not any(tail)
        )

    return st


def analyze(path: str = "", recent_window: int = 30) -> transcript_lib.TranscriptStats:
    full_path, active, compaction_count = active_path(path)
    return analyze_active_prefix(full_path, active, recent_window, compaction_count)
