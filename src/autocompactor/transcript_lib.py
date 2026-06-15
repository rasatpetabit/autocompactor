#!/usr/bin/env python3
"""
transcript_lib.py — shared parsing utilities for Claude Code session
transcripts (~/.claude/projects/<project>/<session-id>.jsonl).

Transcript format notes (observed, not a stable public API — pin your
Claude Code version and re-verify after upgrades):

  * One JSON object per line.
  * Entries of interest have "type": "user" | "assistant" | "system".
  * Assistant entries carry message.usage with input_tokens,
    cache_creation_input_tokens, cache_read_input_tokens, output_tokens.
    The most recent assistant usage block is the best available estimate
    of current context occupancy:
        context ≈ input + cache_read + cache_creation (+ output)
  * Tool calls appear as assistant content blocks {"type":"tool_use",
    "name": ..., "input": {...}}; results come back as user entries with
    content blocks {"type":"tool_result", ...}.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field


@dataclass
class TranscriptStats:
    entries: list = field(default_factory=list)
    context_tokens: int = 0          # estimated current context occupancy
    last_usage: dict = field(default_factory=dict)
    edited_files: list = field(default_factory=list)   # ordered, deduped
    read_files: list = field(default_factory=list)
    todos: list = field(default_factory=list)          # latest TodoWrite state
    recent_errors: list = field(default_factory=list)  # last few error strings
    last_user_task: str = ""
    initial_user_prompts: list = field(default_factory=list)  # founding prompts, verbatim
    recent_commit: bool = False      # git commit in recent window
    recent_tests_pass: bool = False  # test-success marker in recent window
    todos_all_done: bool = False
    todo_step: bool = False          # >=1 completed AND >=1 pending in latest TodoWrite
    stale_tool_chars: int = 0        # chars of tool_result older than window
    total_tool_chars: int = 0
    compaction_count: int = 0        # number of compact_boundary entries seen
    # timing-signal raw material
    usage_series: list = field(default_factory=list)   # context estimate per assistant turn
    recent_error_then_clean: bool = False  # debug loop concluded
    idle_gap_minutes: float = 0.0    # largest gap between recent entries
    task_tool_recent: bool = False   # subagent (Task) burst finished recently
    corrections: list = field(default_factory=list)    # verbatim user corrections
    working_commands: list = field(default_factory=list)
    error_ledger: dict = field(default_factory=dict)   # error text -> count
    hex_constants: list = field(default_factory=list)  # constants w/ context
    recent_words: set = field(default_factory=set)     # for topic-shift


def _content_blocks(entry: dict) -> list:
    msg = entry.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content if isinstance(content, list) else []


def _block_text(block: dict) -> str:
    """Flatten a content block (or tool_result) to text.

    Shared by both harnesses (pi_session_lib aliases this). The `thinking`
    branch is unreachable on the Claude path — analyze() only calls this on
    user/tool_result blocks, never assistant thinking — but Pi's _message_text
    relies on it, so the registry stays single-source."""
    if isinstance(block, str):
        return block
    if not isinstance(block, dict):
        return ""
    if block.get("type") == "text":
        return block.get("text", "") or ""
    if block.get("type") == "thinking":
        return block.get("thinking", "") or ""
    inner = block.get("content")
    if isinstance(inner, str):
        return inner
    if isinstance(inner, list):
        return "\n".join(_block_text(b) for b in inner)
    return ""


TEST_PASS_RE = re.compile(
    r"(\b\d+ passed\b|\ball tests? pass|(?<!not )\bok\b.*\b\d+ tests?\b|test suite passed)",
    re.IGNORECASE,
)

CORRECTION_RE = re.compile(
    r"^\s*(no\b|don'?t\b|stop\b|wait\b|actually\b|instead\b|not what\b"
    r"|that'?s wrong|i (said|meant|prefer|want)\b|use .+ not\b)",
    re.IGNORECASE,
)
TASK_CREATED_RE = re.compile(r"Task #(\d+) created")
# Founding-goal capture: the first few genuine human prompts, kept verbatim
# so every compaction pass can restate what the session was started to do.
INITIAL_PROMPTS_MAX = 3
INITIAL_PROMPT_CHARS = 2000
HEX_RE = re.compile(r"0x[0-9A-Fa-f]{2,16}\b")
WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_STOPWORDS = frozenset(
    "the and for with that this from have what your can you are not but all "
    "was were will would should could about into over under just like make "
    "made need want use using used file files code run running".split())


def _content_words(text: str) -> set:
    return {w.lower() for w in WORD_RE.findall(text)} - _STOPWORDS


def _entry_ts(entry: dict):
    ts = entry.get("timestamp")
    if not ts:
        return None
    try:
        import datetime as _dt
        return _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def load_transcript(path: str, start_offset: int = 0) -> list:
    """Parse JSONL entries, optionally starting at a byte offset.

    start_offset must point at a line start (e.g. from
    find_last_boundary_offset) — JSONL lines begin on clean UTF-8
    boundaries, so seeking there is safe.
    """
    entries = []
    try:
        with open(os.path.expanduser(path), "rb") as fh:
            if start_offset > 0:
                fh.seek(start_offset)
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue
                # Only dict entries are usable; a valid-JSON-but-non-object
                # line (bare string/number) would crash the .get() calls in
                # analyze(). Skip it rather than poison the whole parse.
                if isinstance(obj, dict):
                    entries.append(obj)
    except OSError:
        pass
    return entries


def find_last_boundary_offset(path: str, needle: bytes = b'"compact_boundary"',
                              chunk: int = 1 << 20) -> int:
    """Byte offset of the line containing the LAST compaction boundary
    marker, scanning backwards in chunks; 0 if none (or on any error).

    Lets callers parse only the active (post-compaction) segment of huge
    transcripts instead of the whole file: between compactions the active
    segment is bounded by the autocompact ceiling, the dead prefix is not.
    """
    def _is_boundary_line(fh, line_off: int) -> bool:
        # A transcript can *mention* the marker string (e.g. a session
        # about this very tool), so verify the line is the real thing.
        try:
            fh.seek(line_off)
            entry = json.loads(fh.readline().decode("utf-8", "replace"))
            return (entry.get("type") == "system"
                    and entry.get("subtype") == "compact_boundary")
        except Exception:
            return False

    try:
        with open(os.path.expanduser(path), "rb") as fh:
            fh.seek(0, 2)
            pos = fh.tell()
            tail = b""
            while pos > 0:
                step = min(chunk, pos)
                pos -= step
                fh.seek(pos)
                buf = fh.read(step) + tail
                i = len(buf)
                while True:
                    i = buf.rfind(needle, 0, i)
                    if i == -1:
                        break
                    j = buf.rfind(b"\n", 0, i)
                    if j == -1 and pos > 0:
                        continue   # line start in an earlier chunk; the
                                   # overlap below re-exposes it next pass
                    line_off = pos + (j + 1 if j != -1 else 0)
                    if _is_boundary_line(fh, line_off):
                        return line_off
                # overlap so needles/line-starts straddling chunks survive
                tail = buf[:4096]
    except OSError:
        pass
    return 0


def _finalize_stats(st: TranscriptStats, edited: dict, read: dict,
                    task_state: dict, recent_result_flags: list) -> None:
    """Derive summary fields from the raw material the analyze() loop
    accumulated: live context size, ordered file lists, trimmed ledgers,
    synthesized todo state, and the concluded-debug-loop flag. Split out of
    analyze() as a self-contained finalize step — no control flow, just
    projection of the loop's accumulators onto `st`."""
    u = st.last_usage
    st.context_tokens = (
        int(u.get("input_tokens", 0))
        + int(u.get("cache_read_input_tokens", 0))
        + int(u.get("cache_creation_input_tokens", 0))
        + int(u.get("output_tokens", 0))
    )
    st.edited_files = [fp for fp, _ in sorted(edited.items(), key=lambda kv: kv[1])]
    st.read_files = [fp for fp, _ in sorted(read.items(), key=lambda kv: kv[1])
                     if fp not in edited]
    st.recent_errors = st.recent_errors[-3:]
    st.working_commands = st.working_commands[-15:]
    st.corrections = st.corrections[-20:]
    st.hex_constants = st.hex_constants[-20:]
    if not st.todos and task_state:
        # No TodoWrite in transcript: synthesize from TaskCreate/TaskUpdate
        st.todos = list(task_state.values())
    if st.todos:
        st.todos_all_done = all(t.get("status") == "completed" for t in st.todos)
        st.todo_step = (any(t.get("status") == "completed" for t in st.todos)
                        and any(t.get("status") != "completed" for t in st.todos))
    # debug-loop concluded: errors occurred in the window, but the trailing
    # results are a clean streak
    if recent_result_flags:
        had_err = any(recent_result_flags)
        tail = recent_result_flags[-3:]
        st.recent_error_then_clean = (had_err and len(tail) == 3
                                      and not any(tail))


def analyze(path: str = "", recent_window: int = 30,
            entries: list = None, start_offset: int = 0) -> TranscriptStats:
    st = TranscriptStats()
    raw_entries = (entries if entries is not None
                   else load_transcript(path, start_offset=start_offset))
    st.entries = raw_entries

    last_boundary = -1
    for idx, entry in enumerate(raw_entries):
        if (entry.get("type") == "system"
                and entry.get("subtype") == "compact_boundary"):
            last_boundary = idx
            st.compaction_count += 1
    if last_boundary >= 0:
        for entry in raw_entries:
            if len(st.initial_user_prompts) >= INITIAL_PROMPTS_MAX:
                break
            if entry.get("isCompactSummary") or entry.get("isMeta"):
                continue
            if entry.get("type") != "user":
                continue
            blocks = _content_blocks(entry)
            if any(isinstance(b, dict) and b.get("type") == "tool_result"
                   for b in blocks):
                continue
            text = "\n".join(_block_text(b) for b in blocks).strip()
            if text and not text.startswith("/") and "<command-name>" not in text:
                st.initial_user_prompts.append(text[:INITIAL_PROMPT_CHARS])
        st.entries = raw_entries[last_boundary + 1:]

    n = len(st.entries)
    edited, read = {}, {}
    pending_bash = {}            # tool_use_id -> command (for working/error pairing)
    pending_task_create = {}     # tool_use_id -> subject (TaskCreate pairing)
    task_state = {}              # taskId -> {"content", "status"} (TaskCreate/Update)
    recent_result_flags = []     # ordered is_error flags in recent window
    prev_ts = None

    for idx, entry in enumerate(st.entries):
        etype = entry.get("type")
        is_recent = idx >= n - recent_window

        ts = _entry_ts(entry)
        if is_recent and ts and prev_ts:
            gap = (ts - prev_ts).total_seconds() / 60.0
            st.idle_gap_minutes = max(st.idle_gap_minutes, gap)
        if ts:
            prev_ts = ts

        if etype == "assistant":
            usage = (entry.get("message") or {}).get("usage") or {}
            if isinstance(usage, dict) and usage:
                st.last_usage = usage
                st.usage_series.append(
                    int(usage.get("input_tokens", 0))
                    + int(usage.get("cache_read_input_tokens", 0))
                    + int(usage.get("cache_creation_input_tokens", 0))
                    + int(usage.get("output_tokens", 0)))
            for block in _content_blocks(entry):
                if block.get("type") != "tool_use":
                    continue
                name = block.get("name", "")
                tin = block.get("input") or {}
                if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                    fp = tin.get("file_path") or tin.get("notebook_path")
                    if fp:
                        edited[fp] = idx
                elif name == "Read":
                    fp = tin.get("file_path")
                    if fp:
                        read[fp] = idx
                elif name == "TodoWrite":
                    todos = tin.get("todos")
                    if isinstance(todos, list):
                        st.todos = todos
                elif name == "Bash":
                    cmd = str(tin.get("command", ""))
                    pending_bash[block.get("id")] = cmd
                    if is_recent and "git commit" in cmd:
                        st.recent_commit = True
                elif name in ("Task", "Agent") and is_recent:
                    st.task_tool_recent = True
                elif name == "TaskCreate":
                    pending_task_create[block.get("id")] = str(
                        tin.get("subject", ""))[:200]
                elif name == "TaskUpdate":
                    tid = str(tin.get("taskId", ""))
                    status = tin.get("status")
                    if tid in task_state and status in (
                            "pending", "in_progress", "completed"):
                        task_state[tid]["status"] = status
                    elif tid in task_state and status == "deleted":
                        task_state.pop(tid, None)

        elif etype == "user":
            blocks = _content_blocks(entry)
            tool_results = [b for b in blocks
                            if isinstance(b, dict) and b.get("type") == "tool_result"]
            if tool_results:
                for tr in tool_results:
                    text = _block_text(tr)
                    st.total_tool_chars += len(text)
                    if not is_recent:
                        st.stale_tool_chars += len(text)
                    is_err = bool(tr.get("is_error"))
                    if is_err:
                        key = text[:160].strip()
                        st.error_ledger[key] = st.error_ledger.get(key, 0) + 1
                    subj = pending_task_create.pop(tr.get("tool_use_id"), None)
                    if subj is not None and not is_err:
                        m = TASK_CREATED_RE.search(text[:200])
                        if m:
                            task_state[m.group(1)] = {
                                "content": subj, "status": "pending"}
                    cmd = pending_bash.pop(tr.get("tool_use_id"), None)
                    if cmd and not is_err:
                        if cmd not in st.working_commands:
                            st.working_commands.append(cmd)
                    if is_recent:
                        recent_result_flags.append(is_err)
                        st.recent_words |= _content_words(text[:1500])
                        for m in HEX_RE.finditer(text[:4000]):
                            a, b = max(0, m.start() - 30), m.end() + 30
                            st.hex_constants.append(
                                text[a:b].replace("\n", " ").strip())
                        if is_err:
                            st.recent_errors.append(text[:300])
                        if not is_err and TEST_PASS_RE.search(text[:2000]):
                            st.recent_tests_pass = True
            else:
                # A genuine human turn. Track the last substantive one.
                # Harness-injected entries (compaction summaries, meta
                # caveats) are not the user's voice — never capture them.
                if entry.get("isCompactSummary") or entry.get("isMeta"):
                    continue
                text = "\n".join(_block_text(b) for b in blocks).strip()
                if text and not text.startswith("/") and "<command-name>" not in text:
                    st.last_user_task = text[:500]
                    if len(st.initial_user_prompts) < INITIAL_PROMPTS_MAX:
                        st.initial_user_prompts.append(
                            text[:INITIAL_PROMPT_CHARS])
                    if is_recent:
                        st.recent_words |= _content_words(text)
                    if CORRECTION_RE.search(text):
                        st.corrections.append(text[:200])

    _finalize_stats(st, edited, read, task_state, recent_result_flags)
    return st


def burn_rate(st: TranscriptStats, lookback: int = 10) -> float:
    """Median tokens-of-context growth per assistant turn, recent window."""
    s = st.usage_series[-lookback:]
    deltas = [b - a for a, b in zip(s, s[1:]) if b > a]
    if not deltas:
        return 0.0
    deltas.sort()
    return float(deltas[len(deltas) // 2])


def topic_shift(prompt: str, st: TranscriptStats) -> bool:
    """Current prompt shares little vocabulary with the recent window."""
    pw = _content_words(prompt or "")
    if len(pw) < 4 or not st.recent_words:
        return False
    overlap = len(pw & st.recent_words) / len(pw)
    return overlap < 0.2


def active_signals(st: TranscriptStats, prompt: str = "",
                   window: float = 200_000.0,
                   stale_frac_thr: float = 0.5) -> list:
    """Single registry of boundary signals (monitor + backtester share it).
    Returns [(name, human_description)]."""
    sigs = []
    if st.recent_commit:
        sigs.append(("commit", "a git commit was just made"))
    if st.recent_tests_pass:
        sigs.append(("tests_pass", "tests just passed"))
    if st.todos and st.todos_all_done:
        sigs.append(("todos_done", "all todo items are complete"))
    elif st.todo_step:
        sigs.append(("todo_step", "a plan step was just completed"))
    if st.recent_error_then_clean:
        sigs.append(("error_resolved", "a debug loop just concluded"))
    if st.task_tool_recent:
        sigs.append(("subagent_done", "a subagent task just returned"))
    if st.idle_gap_minutes >= 30:
        sigs.append(("idle_gap",
                     f"session resumed after {st.idle_gap_minutes:.0f}m idle"))
    stale = (st.stale_tool_chars / st.total_tool_chars
             if st.total_tool_chars else 0.0)
    if stale >= stale_frac_thr:
        sigs.append(("stale_output",
                     f"{stale:.0%} of tool output in context is stale"))
    br = burn_rate(st)
    if br > 0 and st.context_tokens > 0:
        turns_left = (window * 0.85 - st.context_tokens) / br
        if 0 <= turns_left <= 8:
            sigs.append(("burn_rate",
                         f"~{turns_left:.0f} turns from autocompact at the "
                         f"current burn rate ({br:,.0f} tok/turn)"))
    if prompt and topic_shift(prompt, st):
        sigs.append(("topic_shift", "the new prompt starts a different topic"))
    return sigs


# Signals measured as anti-predictive on BOTH the 14-day backtest and the
# day-one nightly (precision at or below the signal-agnostic baseline:
# error_resolved 3%/0.1x, tests_pass 9%/0.4x, idle_gap 0 firings).
# They stay in the registry so telemetry and the backtester keep
# measuring them, but neither the monitor nor the backtester's
# recommendation replay may GATE a recommendation on them. Tunable:
# AUTOCOMPACTOR_OBSERVE_ONLY (comma-separated signal names).
OBSERVE_ONLY_DEFAULT = "error_resolved,tests_pass,idle_gap"


def observe_only(harness: str = "claude") -> frozenset:
    from autocompactor import config_lib
    raw = config_lib.cfg.str("OBSERVE_ONLY", harness=harness,
                             default=OBSERVE_ONLY_DEFAULT)
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


BASE_SCHEMA = (
    "Produce a structured handoff, not prose. Sections: GOAL; USER "
    "CONSTRAINTS & PREFERENCES (verbatim where stated); PLAN & POSITION "
    "(done / in-progress / next step); FILES TOUCHED (path -> one-line role "
    "+ status); KEY DECISIONS (+ rationale); FAILED APPROACHES (what was "
    "tried, exact failure text, why abandoned -- never retry these); "
    "WORKING COMMANDS & CONFIG (verbatim); ENVIRONMENT FACTS (versions, "
    "ports, hardware quirks, anything discovered empirically); OPEN "
    "QUESTIONS.\n"
    "Rules: preserve the user's own input prompts VERBATIM -- especially "
    "the initial ones that framed the task; quote them in full, never "
    "compress or paraphrase them (later instructions may be shortened only "
    "if purely procedural, but anything stating intent, scope, constraints, "
    "or preferences is quoted exactly). Copy identifiers, paths, commands, "
    "error strings, and numeric "
    "constants verbatim -- never paraphrase them. If the transcript already "
    "contains a prior compaction summary, treat its GOAL and USER "
    "CONSTRAINTS & PREFERENCES sections as canonical: carry them forward "
    "UNCHANGED (including every quoted user prompt) rather than "
    "re-summarizing them -- each re-summarization pass decays the founding "
    "intent. Keep anything that took "
    "multiple tool calls to discover and cannot be re-derived by re-reading "
    "the repo; drop anything recoverable from disk (file contents, applied "
    "diffs, passing test output, old reasoning). If unsure whether to keep "
    "something, keep a one-line pointer to re-derive it (file:line or "
    "command) instead of the content."
)

PHASE_ADDENDA = {
    "debugging": (
        "This session is mid-DEBUG. Additionally preserve: the exact "
        "reproduction steps; every distinct error message seen (verbatim, "
        "deduplicated); the hypothesis ledger -- each hypothesis, the test "
        "performed, and whether it was ruled out; the current leading "
        "hypothesis and the next diagnostic step."
    ),
    "implementation": (
        "This session is mid-IMPLEMENTATION. Additionally preserve: the "
        "agreed design/plan and which step is in progress; signatures and "
        "contracts of functions/interfaces created or changed (names, "
        "params, types); invariants the new code must maintain; any "
        "TODO/FIXME items deliberately deferred."
    ),
    "exploration": (
        "This session is mostly EXPLORATION. Additionally preserve: the "
        "map learned -- for each significant file/module, one line on its "
        "purpose and how it connects; entry points and data flow; "
        "surprises (where reality differed from expectations). Do NOT "
        "preserve file contents -- pointers only."
    ),
    "wrapup": (
        "This session just reached a milestone (commit/tests green). Bias "
        "heavily toward brevity: the next task needs the goal, the final "
        "state, decisions with rationale, and environment facts. History "
        "of how we got here can be one short paragraph."
    ),
}


def detect_phase(st: TranscriptStats) -> str:
    """Cheap heuristic over the recent window."""
    if st.recent_commit or (st.recent_tests_pass and st.todos_all_done):
        return "wrapup"
    if len(st.recent_errors) >= 2 and not st.recent_error_then_clean:
        return "debugging"
    if st.edited_files and not st.recent_errors:
        return "implementation"
    return "exploration"


def build_context_state(st: TranscriptStats, window: float = 0.0,
                        harness: str = "claude") -> str:
    """Produce a concise before-compaction context overview for human display
    and/or inclusion in compaction instructions.

    Returns a multi-line string with token counts, occupancy, phase, active
    signals, and key session facts.  Same function used by both the Claude
    precompact hook (embedded in instructions) and the Pi bridge (emitted as
    a separate JSON field so the TS shim can post it as a visible message).
    """
    phase = detect_phase(st)
    signal_window = window or 200_000.0
    stale_frac_thr = 0.5
    try:
        from autocompactor import config_lib
        if window <= 0:
            signal_window = config_lib.cfg.float(
                "WINDOW", harness=harness, default=200_000)
        stale_frac_thr = config_lib.cfg.float(
            "STALE_FRAC", harness=harness, default=0.50)
    except Exception:
        pass
    signals = active_signals(st, window=signal_window,
                             stale_frac_thr=stale_frac_thr)
    lines = [
        f"Context: {st.context_tokens:,} tokens",
        f"Phase: {phase}",
    ]
    if window > 0:
        occ = st.context_tokens / window
        lines.append(f"Occupancy: {occ:.0%}")
    stale_frac = (st.stale_tool_chars / st.total_tool_chars
                  if st.total_tool_chars else 0.0)
    lines.append(f"Stale tool output: {stale_frac:.0%}")
    lines.append(f"Edited files: {len(st.edited_files)}")
    lines.append(f"Read files: {len(st.read_files)}")
    if signals:
        gating = [desc for name, desc in signals
                  if name not in observe_only(harness=harness)]
        lines.append(f"Active signals: {', '.join(gating) if gating else 'none'}")
    else:
        lines.append("Active signals: none")
    lines.append(f"Compaction count: {getattr(st, 'compaction_count', 0)}")
    return " | ".join(lines)


def build_preservation_instructions(st: TranscriptStats, cwd: str = "") -> str:
    """Compose compaction custom instructions from transcript analysis:
    structured-handoff schema + phase addendum + session-specific facts."""
    phase = detect_phase(st)
    lines = [
        BASE_SCHEMA,
        "",
        PHASE_ADDENDA[phase],
        "",
        "Session-specific anchors (seed the sections above with these):",
    ]
    if st.initial_user_prompts:
        lines.append("- The ORIGINAL user request(s) that framed this "
                     "session (quote these VERBATIM in GOAL; never "
                     "paraphrase):")
        for p in st.initial_user_prompts:
            lines.append("    * " + p.replace("\n", " "))
    if st.last_user_task:
        lines.append(f"- The current task/goal: {st.last_user_task[:1500]}")
    if st.edited_files:
        lines.append("- Full paths of all files modified this session: "
                     + ", ".join(st.edited_files[-15:]))
    if st.todos:
        pending = [t.get("content", "") for t in st.todos
                   if t.get("status") != "completed"]
        done = [t.get("content", "") for t in st.todos
                if t.get("status") == "completed"]
        if pending:
            lines.append("- Open todo items (exact wording): " + "; ".join(pending))
        if done:
            lines.append("- Completed items (one line each is enough): "
                         + "; ".join(done[-8:]))
    if st.recent_errors:
        lines.append("- Recent error texts (verbatim, for FAILED APPROACHES "
                     "or the hypothesis ledger):")
        for err in st.recent_errors:
            lines.append("    * " + err.replace("\n", " ")[:200])
    if cwd:
        lines.append(f"- Working directory: {cwd}")
    return "\n".join(lines)


def append_artifact_restatement(instructions: str, arts: dict) -> str:
    """Append the founding-goal restatement + the on-disk-artifacts NOTE.

    Shared by both PreCompact paths (Claude precompact_analyzer + Pi bridge
    prepare): every compaction pass must restate the session's original
    prompts verbatim — staged instructions built from a tail-only parse may
    predate their capture, but the old-wins artifact merge always carries
    them — and must tell the summarizer not to spend space duplicating the
    mechanically-extracted artifacts that are re-injected post-compaction.
    """
    founding = [p.replace("\n", " ") for p in arts.get("initial_prompts") or []]
    if founding and founding[0][:200] not in instructions:
        instructions += (
            "\n\nThe ORIGINAL user request(s) that framed this session "
            "(quote these VERBATIM in GOAL; never paraphrase):\n"
            + "\n".join("    * " + p for p in founding))
    instructions += (
        "\n\nNOTE: the following are preserved on disk and will be "
        "re-injected after compaction -- do NOT spend summary space "
        "duplicating them: user corrections, error texts, working "
        "commands, discovered constants, file lists. Focus the summary "
        "on what regexes cannot extract: decisions and rationale, plan "
        "position, failed approaches, open questions.")
    return instructions
