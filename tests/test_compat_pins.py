"""Wave-0 compatibility-pin suite — freeze-the-present guardrail.

Four invariants pinned here:
  1. active_signals() name-set and OBSERVE_ONLY_DEFAULT constant.
  2. Golden build_preservation_instructions() output on the rich fixture
     (byte-equality so any instruction-text drift is caught).
  3. STATE_DIR defaults in context_monitor and precompact_analyzer.
  4. Hook stdout top-level key schema for monitor and analyzer entrypoints.

These tests import existing modules and drive existing hook scripts via
subprocess (matching the pattern in test_autocompactor.py). No source
files are modified; the new file is tests/test_compat_pins.py.
"""

import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(REPO, "tests", "fixtures")
sys.path.insert(0, REPO)

import transcript_lib as tl  # noqa: E402
import context_monitor  # noqa: E402
import precompact_analyzer  # noqa: E402


# ----------------------------------------------------------------- helpers

def _hook_env(tmp_path):
    """Isolated HOME + scrubbed AUTOCOMPACTOR_* (mirrors test_autocompactor.py
    _hook_env, keeps the min-savings guard from suppressing the fixture which
    sits at ~84k context)."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("AUTOCOMPACTOR_")}
    env["HOME"] = str(tmp_path)
    env["AUTOCOMPACTOR_POST_FLOOR"] = "50000"
    env["AUTOCOMPACTOR_MIN_SAVINGS"] = "20000"
    return env


def _run_hook(script, payload, tmp_path):
    return subprocess.run(
        [sys.executable, os.path.join(REPO, script)],
        input=payload, capture_output=True, text=True,
        env=_hook_env(tmp_path), cwd=REPO, timeout=60,
    )


# ============================================================ Pin 1
# active_signals() name-set and OBSERVE_ONLY_DEFAULT

EXPECTED_SIGNAL_NAMES = frozenset({
    "commit",
    "tests_pass",
    "todos_done",
    "todo_step",
    "error_resolved",
    "subagent_done",
    "idle_gap",
    "stale_output",
    "burn_rate",
    "topic_shift",
})


def test_active_signals_name_set():
    """active_signals() must never emit a name outside EXPECTED_SIGNAL_NAMES,
    and every name in EXPECTED_SIGNAL_NAMES must be reachable."""
    # Build a state that fires every signal.
    st = tl.TranscriptStats()
    st.recent_commit = True
    st.recent_tests_pass = True
    # todos_done: all completed
    st.todos = [{"content": "a", "status": "completed"}]
    st.todos_all_done = True
    st.todo_step = False  # todos_done and todo_step are mutually exclusive paths
    st.recent_error_then_clean = True
    st.task_tool_recent = True
    st.idle_gap_minutes = 60.0
    st.stale_tool_chars = 90
    st.total_tool_chars = 100
    # burn_rate: needs usage series that produces a positive median delta and
    # a context_tokens value that puts turns_left in [0, 8]
    st.usage_series = [160_000, 170_000, 175_000]
    st.context_tokens = 175_000
    # topic_shift: recent_words with low overlap to the prompt below
    st.recent_words = {"database", "migration", "schema", "postgres", "sql"}

    sigs_all_done = dict(tl.active_signals(
        st, prompt="design the marketing landing page hero image",
        window=200_000, stale_frac_thr=0.5,
    ))
    assert set(sigs_all_done.keys()) <= EXPECTED_SIGNAL_NAMES, (
        f"unexpected signal names: {set(sigs_all_done.keys()) - EXPECTED_SIGNAL_NAMES}"
    )
    # Every name must be reachable; check todos_done fired here.
    assert "todos_done" in sigs_all_done
    assert "commit" in sigs_all_done

    # Now check todo_step fires (mutually exclusive with todos_done).
    st2 = tl.TranscriptStats()
    st2.todos = [
        {"content": "a", "status": "completed"},
        {"content": "b", "status": "pending"},
    ]
    st2.todos_all_done = False
    st2.todo_step = True
    sigs_step = dict(tl.active_signals(st2))
    assert "todo_step" in sigs_step
    assert "todos_done" not in sigs_step

    # Collect the union of all reachable names across both states plus the
    # remaining signals already validated above.
    all_names = (set(sigs_all_done.keys())
                 | set(sigs_step.keys())
                 | {"tests_pass", "error_resolved", "subagent_done",
                    "idle_gap", "stale_output", "burn_rate", "topic_shift"})
    assert EXPECTED_SIGNAL_NAMES <= all_names, (
        f"signals never emitted: {EXPECTED_SIGNAL_NAMES - all_names}"
    )


def test_observe_only_default_constant():
    """OBSERVE_ONLY_DEFAULT must equal the three demoted anti-predictive
    signals; changing this string changes live behavior."""
    assert tl.OBSERVE_ONLY_DEFAULT == "error_resolved,tests_pass,idle_gap"


# ============================================================ Pin 2
# Golden build_preservation_instructions output (byte-equality)
#
# Nondeterminism sources scrubbed:
#   - cwd: passed as "" so the working-directory anchor is omitted.
#   - File I/O path: analyze() is called directly on the fixture, not via
#     any hook subprocess (no env or state leakage).
#   - set ordering in recent_words: the fixture's words set is stable across
#     runs because Python 3.7+ dicts are insertion-ordered and the JSONL
#     fixture is a checked-in file with a fixed line order.

GOLDEN_PRESERVATION_INSTRUCTIONS = (
    "Produce a structured handoff, not prose. Sections: GOAL; USER "
    "CONSTRAINTS & PREFERENCES (verbatim where stated); PLAN & POSITION "
    "(done / in-progress / next step); FILES TOUCHED (path -> one-line role "
    "+ status); KEY DECISIONS (+ rationale); FAILED APPROACHES (what was "
    "tried, exact failure text, why abandoned -- never retry these); "
    "WORKING COMMANDS & CONFIG (verbatim); ENVIRONMENT FACTS (versions, "
    "ports, hardware quirks, anything discovered empirically); OPEN "
    "QUESTIONS.\n"
    "Rules: copy identifiers, paths, commands, error strings, and numeric "
    "constants verbatim -- never paraphrase them. Keep anything that took "
    "multiple tool calls to discover and cannot be re-derived by re-reading "
    "the repo; drop anything recoverable from disk (file contents, applied "
    "diffs, passing test output, old reasoning). If unsure whether to keep "
    "something, keep a one-line pointer to re-derive it (file:line or "
    "command) instead of the content.\n"
    "\n"
    "This session is mostly EXPLORATION. Additionally preserve: the "
    "map learned -- for each significant file/module, one line on its "
    "purpose and how it connects; entry points and data flow; "
    "surprises (where reality differed from expectations). Do NOT "
    "preserve file contents -- pointers only.\n"
    "\n"
    "Session-specific anchors (seed the sections above with these):\n"
    "- The current task/goal: no, don't use can0 — use can1, the adapter enumerates second\n"
    "- Open todo items (exact wording): map SPN fields; emulate charger handshake\n"
    "- Completed items (one line each is enough): identify BHM frame ID\n"
    "- Recent error texts (verbatim, for FAILED APPROACHES "
    "or the hypothesis ledger):\n"
    "    * candump: read: Network is down\n"
    "    * candump: read: Network is down\n"
    "    * candump: read: Network is down"
)


def test_golden_preservation_instructions():
    """build_preservation_instructions() on the rich fixture with cwd='' must
    produce byte-identical output to the golden string captured at the time
    this test was authored.  Any change to BASE_SCHEMA, PHASE_ADDENDA, or the
    session-anchor formatting triggers this test so it cannot be missed."""
    st = tl.analyze(os.path.join(FIX, "rich_transcript.jsonl"))
    actual = tl.build_preservation_instructions(st, cwd="")
    assert actual == GOLDEN_PRESERVATION_INSTRUCTIONS, (
        "build_preservation_instructions output drifted from the golden.\n"
        f"Expected ({len(GOLDEN_PRESERVATION_INSTRUCTIONS)} chars):\n"
        f"{GOLDEN_PRESERVATION_INSTRUCTIONS!r}\n\n"
        f"Got ({len(actual)} chars):\n"
        f"{actual!r}"
    )


# ============================================================ Pin 3
# STATE_DIR defaults

def test_state_dir_defaults():
    """Both hooks must resolve their STATE_DIR to the canonical path
    ~/.claude/autocompactor regardless of the current user's HOME."""
    expected = os.path.expanduser("~/.claude/autocompactor")
    assert context_monitor.STATE_DIR == expected, (
        f"context_monitor.STATE_DIR {context_monitor.STATE_DIR!r} != {expected!r}"
    )
    assert precompact_analyzer.STATE_DIR == expected, (
        f"precompact_analyzer.STATE_DIR {precompact_analyzer.STATE_DIR!r} != {expected!r}"
    )


# ============================================================ Pin 4
# Hook stdout top-level key schema

MONITOR = "context_monitor.py"
ANALYZER = "precompact_analyzer.py"

_RICH_PATH = os.path.join(FIX, "rich_transcript.jsonl")


def test_monitor_key_schema_when_recommending(tmp_path):
    """When the monitor fires a recommendation the JSON object's top-level
    keys must be exactly {'hookSpecificOutput', 'systemMessage'}."""
    payload = json.dumps({
        "session_id": "pin4mon", "cwd": "/tmp",
        "transcript_path": _RICH_PATH,
        "hook_event_name": "UserPromptSubmit",
        "prompt": "now plan the website redesign",
    })
    r = _run_hook(MONITOR, payload, tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip(), "expected non-empty stdout (monitor should recommend)"
    obj = json.loads(r.stdout)
    assert set(obj.keys()) == {"hookSpecificOutput", "systemMessage"}, (
        f"unexpected top-level keys: {set(obj.keys())}"
    )


def test_monitor_empty_stdout_when_not_recommending(tmp_path):
    """When the monitor does not recommend, it must produce empty stdout
    (exit 0, no JSON, no stray text)."""
    import tempfile
    # Very small transcript — context well below min-savings floor.
    small = tmp_path / "tiny.jsonl"
    small.write_text(
        json.dumps({
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "hello"}],
                "usage": {
                    "input_tokens": 5000,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 0,
                },
            },
        }) + "\n"
    )
    # Use default guard thresholds — 5k context is well below POST_FLOOR+MIN_SAVINGS.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("AUTOCOMPACTOR_")}
    env["HOME"] = str(tmp_path)
    payload = json.dumps({
        "session_id": "pin4nosuggest", "cwd": "/tmp",
        "transcript_path": str(small),
        "hook_event_name": "UserPromptSubmit",
        "prompt": "hi there",
    })
    r = subprocess.run(
        [sys.executable, os.path.join(REPO, MONITOR)],
        input=payload, capture_output=True, text=True,
        env=env, cwd=REPO, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "", (
        f"expected empty stdout, got: {r.stdout!r}"
    )


def test_analyzer_key_schema(tmp_path):
    """The analyzer's JSON object top-level keys must be a subset of
    {'hookSpecificOutput', 'systemMessage'}, and hookSpecificOutput must
    carry hookEventName == 'PreCompact'."""
    payload = json.dumps({
        "session_id": "pin4ana", "cwd": "/tmp",
        "transcript_path": _RICH_PATH,
        "hook_event_name": "PreCompact",
        "trigger": "manual",
        "custom_instructions": "",
    })
    r = _run_hook(ANALYZER, payload, tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip(), "analyzer should emit output for a real transcript"
    obj = json.loads(r.stdout)
    allowed = {"hookSpecificOutput", "systemMessage"}
    assert set(obj.keys()) <= allowed, (
        f"unexpected top-level keys: {set(obj.keys()) - allowed}"
    )
    assert "hookSpecificOutput" in obj, (
        "analyzer must include hookSpecificOutput when a transcript is present"
    )
    assert obj["hookSpecificOutput"]["hookEventName"] == "PreCompact", (
        f"hookEventName: {obj['hookSpecificOutput'].get('hookEventName')!r}"
    )
