#!/usr/bin/env python3
"""
transcript_lib.py — shared parsing utilities for coding-agent session
transcripts (per-session JSONL).

Transcript format notes (observed, not a stable public API — pin your
producer version and re-verify after upgrades):

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
    assistant_text_chars: int = 0    # chars of assistant text+thinking in context
    user_prompt_chars: int = 0       # chars of genuine user turns in context
    summary_chars: int = 0           # chars of carried compaction summary in context
    skill_chars: int = 0             # chars of loaded skill injections (isMeta, persist)
    skill_names: list = field(default_factory=list)  # deduped loaded-skill names
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


def burn_rate(st: TranscriptStats, lookback: int = 10) -> float:
    """Median tokens-of-context growth per assistant turn, recent window."""
    s = st.usage_series[-lookback:]
    deltas = [b - a for a, b in zip(s, s[1:]) if b > a]
    if not deltas:
        return 0.0
    deltas.sort()
    return float(deltas[len(deltas) // 2])


CHARS_PER_TOKEN = 4   # chars ~ tokens*4 heuristic (matches artifacts.py)


def context_composition(st: TranscriptStats, context_tokens: int = -1) -> dict:
    """Estimate the per-category token breakdown of the CURRENT context window,
    reconciled to the authoritative total. Answers owner request (a): not "how
    full" but "where are the tokens, and how much is reclaimable".

    Categories (over the in-context content, plus persistent injections):
      skills     loaded skill / large instruction injections (isMeta) still in
                 context — often the single largest item, and RECLAIMABLE (a
                 loaded skill body, not fixed overhead). Measured exactly from
                 the transcript, so trusted over the chars/4 estimates.
      summary    carried compaction summary still in context.
      base       residual = total - everything measured: system prompt + tool
                 schemas + estimation error. Labelled "floor"/"system+tools"
                 downstream — the part a compaction genuinely cannot shrink.
      tool       tool_result output, plus the stale fraction (prime reclaim).
      assistant  the model's own text/reasoning currently in context.
      prompts    genuine user turns in context.

    skills/summary are exact; the chars/4 content categories are scaled to fit
    whatever they leave, and `base` is the residual, so the parts always sum
    back to the true total. Formatted by policy.composition_line()."""
    total = int(context_tokens if context_tokens >= 0 else st.context_tokens)
    total = max(total, 0)
    skills = max(int(getattr(st, "skill_chars", 0)), 0) / CHARS_PER_TOKEN
    summary = max(int(getattr(st, "summary_chars", 0)), 0) / CHARS_PER_TOKEN
    tool = max(int(st.total_tool_chars), 0) / CHARS_PER_TOKEN
    asst = max(int(getattr(st, "assistant_text_chars", 0)), 0) / CHARS_PER_TOKEN
    prompts = max(int(getattr(st, "user_prompt_chars", 0)), 0) / CHARS_PER_TOKEN
    if total and (skills + summary) > total:
        # Pathological: the exact measurements alone exceed the true total.
        s = total / (skills + summary)
        skills *= s
        summary *= s
        tool = asst = prompts = 0.0
    elif total:
        avail = total - skills - summary
        content = tool + asst + prompts
        if content > avail:              # chars/4 over-estimated — scale to fit
            scale = (avail / content) if content else 0.0
            tool *= scale
            asst *= scale
            prompts *= scale
    r_skills = int(round(skills))
    r_summary = int(round(summary))
    r_tool = int(round(tool))
    r_asst = int(round(asst))
    r_prompts = int(round(prompts))
    # base = residual so the parts always sum back to the true total.
    base = max(total - r_skills - r_summary - r_tool - r_asst - r_prompts, 0)
    stale_frac = (st.stale_tool_chars / st.total_tool_chars
                  if st.total_tool_chars else 0.0)
    return {
        "total": total,
        "base": base,
        "skills": r_skills,
        "skill_names": list(getattr(st, "skill_names", []) or []),
        "summary": r_summary,
        "tool": r_tool,
        "tool_stale_frac": round(stale_frac, 4),
        "assistant": r_asst,
        "prompts": r_prompts,
    }


def topic_shift(prompt: str, st: TranscriptStats) -> bool:
    """Current prompt shares little vocabulary with the recent window."""
    pw = _content_words(prompt or "")
    if len(pw) < 4 or not st.recent_words:
        return False
    overlap = len(pw & st.recent_words) / len(pw)
    return overlap < 0.2


def active_signals(st: TranscriptStats, prompt: str = "",
                   window: float = 200_000.0,
                   stale_frac_thr: float = 0.5,
                   hard_tokens: float = 0.0) -> list:
    """Single registry of boundary signals (monitor + backtester share it).
    Returns [(name, human_description)].

    hard_tokens, when >0, is the caller's advisory HARD line in tokens; the
    burn_rate signal projects turns to *that* line. When 0 it falls back to a
    fraction of `window`. Either way the description says "the compact line",
    never "autocompact" — burn_rate approaches the advisory recommendation,
    not Claude's forced native wall (those were conflated; owner feedback)."""
    sigs = []
    if st.recent_commit:
        sigs.append(("commit", "a git commit was just made"))
    if st.recent_tests_pass:
        sigs.append(("tests_pass", "tests just passed"))
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
        target = hard_tokens if hard_tokens and hard_tokens > 0 else window * 0.85
        turns_left = (target - st.context_tokens) / br
        if 0 <= turns_left <= 8:
            sigs.append(("burn_rate",
                         f"~{turns_left:.0f} turns to the compact line at the "
                         f"current burn rate (~{br:,.0f} tok/turn)"))
    if prompt and topic_shift(prompt, st):
        sigs.append(("topic_shift", "the new prompt starts a different topic"))
    return sigs


# Observe-only signals: measured but never allowed to GATE a recommendation
# (they stay in the registry so telemetry and the backtester keep scoring
# them). The set is re-derived from measured per-signal lift, not fixed.
#
# 2026-06-17 recalibration (backtest 2026-06-17, signal-agnostic baseline 8%):
#   demoted  burn_rate    7%/0.9x  and  subagent_done 6%/0.8x  (now sub-baseline
#            as GATING signals — they were nagging on noise);
#   promoted idle_gap    56%/7.5x  and  tests_pass   20%/2.7x  back to gating —
#            both were genuinely anti-predictive in the smaller 2026-06-10
#            corpus (idle_gap 0 firings, tests_pass 0.4x) but strongly
#            predictive now. NOTE: idle_gap (n=16) and tests_pass (n=30) are
#            thin samples — re-check at the next nightly before trusting them
#            as load-bearing.
#   error_resolved stays observe-only (3%/0.5x, still sub-baseline).
# Tunable: AUTOCOMPACTOR_OBSERVE_ONLY (comma-separated signal names).
OBSERVE_ONLY_DEFAULT = "error_resolved,burn_rate,subagent_done"


def observe_only(harness: str = "claude") -> frozenset:
    from autocompactor import config_lib
    raw = config_lib.cfg.str("OBSERVE_ONLY",
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
                "WINDOW", default=200_000)
        stale_frac_thr = config_lib.cfg.float(
            "STALE_FRAC", default=0.50)
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
        # Anchor the denominator: a bare "Occupancy: 42%" can't be read without
        # knowing what it is 42% OF (clamped tier? learned window? reserve-net?).
        lines.append(f"Occupancy: {occ:.0%} of ~{int(window):,}t effective window")
    # Composition breakdown — where the tokens actually are (owner request a).
    try:
        from autocompactor import policy as _policy
        comp_line = _policy.composition_line(
            context_composition(st, st.context_tokens))
        if comp_line:
            lines.append("Composition: " + comp_line)
    except Exception:
        pass
    stale_frac = (st.stale_tool_chars / st.total_tool_chars
                  if st.total_tool_chars else 0.0)
    lines.append(f"Stale tool output: {stale_frac:.0%}")
    lines.append(f"Edited files: {len(st.edited_files)}")
    lines.append(f"Read files: {len(st.read_files)}")
    if signals:
        gating = [desc for name, desc in signals
                  if name not in observe_only()]
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

    Used by the Pi bridge prepare path: every compaction pass must restate
    the session's original
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
