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


def analyze(path: str = "", recent_window: int = 30) -> transcript_lib.TranscriptStats:
    st = transcript_lib.TranscriptStats()
    full_path = _leaf_path(_load_jsonl(path))
    active, compaction_count = _active_segment(full_path)
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
            st.assistant_text_chars += len(_message_text(msg, include_thinking=True))
            usage = msg.get("usage")
            if isinstance(usage, dict):
                st.last_usage = _usage_compat(usage)
                st.usage_series.append(_usage_context(usage))

            for call in _tool_calls(msg):
                name = call["name"]
                lname = name.lower()
                args = call["arguments"]

                if lname in ("edit", "write"):
                    _remember_path(edited, args.get("path"), idx)
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
                elif lname in ("task", "agent") and is_recent:
                    st.task_tool_recent = True

        elif role == "toolResult":
            text = _tool_result_text(msg)
            is_error = bool(msg.get("isError"))
            _record_result_text(st, text, is_error, is_recent)
            cmd = pending_bash.pop(msg.get("toolCallId"), None)
            if cmd and not is_error and cmd not in st.working_commands:
                st.working_commands.append(cmd)
            if is_recent:
                recent_result_flags.append(is_error)

        elif role == "bashExecution":
            cmd = str(msg.get("command", "") or "")
            text = str(msg.get("output", "") or "")
            is_error = bool(msg.get("cancelled")) or (
                msg.get("exitCode") not in (None, 0)
            )
            if is_recent and "git commit" in cmd:
                st.recent_commit = True
            _record_result_text(st, text, is_error, is_recent)
            if cmd and not is_error and cmd not in st.working_commands:
                st.working_commands.append(cmd)
            if is_recent:
                recent_result_flags.append(is_error)

        elif role == "user":
            text = _message_text(msg).strip()
            if text and not text.startswith("/") and "<command-name>" not in text:
                st.user_prompt_chars += len(text)
                st.last_user_task = text[:500]
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
