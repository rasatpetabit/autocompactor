"""Pytest suite for autocompactor (open item #8 — maturity-gap closure).

Covers the unit surface of transcript_lib / artifacts / analyze_corpus and
the hook contract of context_monitor / precompact_analyzer, including the
error paths the smoke test skips: hooks must NEVER raise into the hook
path — malformed input, missing transcripts, and unreadable paths all
degrade to a clean exit.

Run from the repo root:  python3 -m pytest tests/ -q
"""

import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(REPO, "tests", "fixtures")
sys.path.insert(0, REPO)

from autocompactor import analyze_corpus, artifacts, policy  # noqa: E402
from autocompactor import precompact_analyzer as pa, transcript_lib as tl  # noqa: E402


# ---------------------------------------------------------------- helpers

def _assistant(tool_name=None, tool_input=None, tool_id="t1", usage=None,
               text="", ts=None):
    content = []
    if text:
        content.append({"type": "text", "text": text})
    if tool_name:
        content.append({"type": "tool_use", "name": tool_name,
                        "input": tool_input or {}, "id": tool_id})
    e = {"type": "assistant", "message": {"content": content}}
    if usage:
        e["message"]["usage"] = usage
    if ts:
        e["timestamp"] = ts
    return e


def _tool_result(text, tool_id="t1", is_error=False, ts=None):
    e = {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tool_id,
         "content": text, "is_error": is_error}]}}
    if ts:
        e["timestamp"] = ts
    return e


def _human(text, ts=None):
    e = {"type": "user", "message": {"content": [
        {"type": "text", "text": text}]}}
    if ts:
        e["timestamp"] = ts
    return e


def _usage(total):
    return {"input_tokens": total, "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0, "output_tokens": 0}


def _hook_env(tmp_path):
    """Isolated HOME + scrubbed AUTOCOMPACTOR_* (live settings.json env
    leaks into child processes and must not steer test expectations).

    The min-savings guard defaults (POST_FLOOR 70k + MIN_SAVINGS 30k)
    suppress recommendations below ~100k context; the rich fixture sits at
    ~84k, so recommendation tests pin guard values that keep it eligible.
    test_monitor_min_savings_guard_suppresses overrides these to exercise
    the guard itself."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("AUTOCOMPACTOR_")}
    env["HOME"] = str(tmp_path)
    env["AUTOCOMPACTOR_CONFIG"] = ""  # hermetic: no repo config files
    env["AUTOCOMPACTOR_POST_FLOOR"] = "50000"
    env["AUTOCOMPACTOR_MIN_SAVINGS"] = "20000"
    return env


def _run_hook(script, payload, tmp_path):
    return subprocess.run(
        [sys.executable, os.path.join(REPO, script)],
        input=payload, capture_output=True, text=True,
        env=_hook_env(tmp_path), cwd=REPO, timeout=60)


# ------------------------------------------------------ transcript parsing

def test_load_transcript_skips_malformed_lines(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text('{"type":"user"}\nnot json\n\n{"type":"assistant"}\n')
    entries = tl.load_transcript(str(p))
    assert [e["type"] for e in entries] == ["user", "assistant"]


def test_load_transcript_missing_file_returns_empty():
    assert tl.load_transcript("/nonexistent/nope.jsonl") == []


def test_analyze_rich_fixture_core_fields():
    st = tl.analyze(os.path.join(FIX, "rich_transcript.jsonl"))
    assert st.context_tokens > 0
    assert st.todos  # fixture carries a TodoWrite state
    assert st.working_commands  # successful Bash commands recorded
    assert st.recent_error_then_clean  # fixture encodes a concluded debug loop


# ------------------------------------------------- task-tool state tracking

def test_todowrite_shape_still_supported():
    entries = [_assistant("TodoWrite", {"todos": [
        {"content": "a", "status": "completed"},
        {"content": "b", "status": "pending"}]})]
    st = tl.analyze(entries=entries)
    assert st.todo_step and not st.todos_all_done


def test_taskcreate_taskupdate_synthesis():
    entries = [
        _assistant("TaskCreate", {"subject": "ship it"}, tool_id="c1"),
        _tool_result("Task #1 created successfully: ship it", tool_id="c1"),
        _assistant("TaskCreate", {"subject": "test it"}, tool_id="c2"),
        _tool_result("Task #2 created successfully: test it", tool_id="c2"),
        _assistant("TaskUpdate", {"taskId": "1", "status": "completed"}),
    ]
    st = tl.analyze(entries=entries)
    assert len(st.todos) == 2
    assert st.todo_step and not st.todos_all_done
    sigs = dict(tl.active_signals(st))
    assert "todo_step" in sigs


def test_taskupdate_all_completed_and_deleted():
    entries = [
        _assistant("TaskCreate", {"subject": "a"}, tool_id="c1"),
        _tool_result("Task #1 created successfully: a", tool_id="c1"),
        _assistant("TaskCreate", {"subject": "b"}, tool_id="c2"),
        _tool_result("Task #2 created successfully: b", tool_id="c2"),
        _assistant("TaskUpdate", {"taskId": "1", "status": "completed"}),
        _assistant("TaskUpdate", {"taskId": "2", "status": "deleted"}),
    ]
    st = tl.analyze(entries=entries)
    assert st.todos_all_done
    assert dict(tl.active_signals(st)).get("todos_done")


def test_taskcreate_failed_result_not_tracked():
    entries = [
        _assistant("TaskCreate", {"subject": "a"}, tool_id="c1"),
        _tool_result("error: tasks unavailable", tool_id="c1", is_error=True),
    ]
    st = tl.analyze(entries=entries)
    assert st.todos == []


def test_agent_and_task_tools_both_set_subagent_signal():
    for name in ("Task", "Agent"):
        st = tl.analyze(entries=[_assistant(name, {"prompt": "x"})])
        assert st.task_tool_recent, name


# ------------------------------------------------------------ signals/phase

def test_topic_shift_detection():
    st = tl.TranscriptStats()
    st.recent_words = {"database", "migration", "schema", "postgres"}
    assert tl.topic_shift("design the marketing landing page hero", st)
    assert not tl.topic_shift("alter the postgres schema migration", st)
    assert not tl.topic_shift("hi", st)  # too few content words


def test_burn_rate_median_of_positive_deltas():
    st = tl.TranscriptStats()
    st.usage_series = [100, 200, 250, 400]  # deltas 100, 50, 150
    assert tl.burn_rate(st) == 100.0
    st.usage_series = [500, 400]  # shrinking: no positive deltas
    assert tl.burn_rate(st) == 0.0


def test_stale_output_threshold_respected():
    st = tl.TranscriptStats()
    st.stale_tool_chars, st.total_tool_chars = 60, 100
    assert "stale_output" in dict(tl.active_signals(st))
    assert "stale_output" not in dict(
        tl.active_signals(st, stale_frac_thr=0.9))


def test_active_signals_empty_and_degenerate_transcripts():
    """active_signals must not crash and fires nothing on empty / single-entry
    transcripts (no usage series, no boundary signals to gate on)."""
    assert tl.active_signals(tl.analyze(entries=[])) == []
    assert tl.active_signals(tl.analyze(entries=[_human("hi")])) == []
    one = tl.analyze(entries=[_assistant(usage=_usage(40_000))])
    assert isinstance(tl.active_signals(one), list)


def test_topic_shift_degenerate_prompts():
    st = tl.TranscriptStats()
    st.recent_words = {"database", "migration", "schema"}
    assert not tl.topic_shift("", st)                 # empty prompt
    assert not tl.topic_shift("the and for with", st)  # all stopwords


def test_failed_tap_output_does_not_count_as_tests_passed():
    entries = [
        _assistant("Bash", {"command": "node --test"}, tool_id="t1"),
        _tool_result("not ok 1 test\n# failed", tool_id="t1", is_error=True),
    ]
    st = tl.analyze(entries=entries)
    assert not st.recent_tests_pass
    assert "tests_pass" not in dict(tl.active_signals(st))


def test_analyze_resets_active_signals_after_compact_boundary():
    entries = [
        _human("Original goal"),
        _assistant("Bash", {"command": "git commit -m done"}, tool_id="c1",
                   usage=_usage(120_000)),
        _tool_result("[main abc123] done", tool_id="c1"),
        {"type": "system", "subtype": "compact_boundary",
         "compactMetadata": {"preTokens": 120_000, "postTokens": 70_000}},
        {"type": "user", "isCompactSummary": True,
         "message": {"content": [{"type": "text", "text": "summary"}]}},
        _assistant(usage=_usage(110_000)),
    ]
    st = tl.analyze(entries=entries)
    assert st.initial_user_prompts == ["Original goal"]
    assert st.context_tokens == 110_000
    assert not st.recent_commit
    assert "commit" not in dict(tl.active_signals(st, window=300_000,
                                                  stale_frac_thr=0.9))


def test_analyze_counts_compaction_boundaries():
    entries = [
        _human("Original goal"),
        _assistant(usage=_usage(100_000)),
        {"type": "system", "subtype": "compact_boundary",
         "compactMetadata": {"preTokens": 100_000, "postTokens": 60_000}},
        _assistant(usage=_usage(150_000)),
        {"type": "system", "subtype": "compact_boundary",
         "compactMetadata": {"preTokens": 150_000, "postTokens": 70_000}},
        _assistant(usage=_usage(80_000)),
    ]
    st = tl.analyze(entries=entries)
    assert st.compaction_count == 2
    # build_context_state surfaces the real count, not a stuck-at-0 default
    assert "Compaction count: 2" in tl.build_context_state(st, window=300_000)
    # a transcript with no compactions reports 0
    st0 = tl.analyze(entries=[_human("hi"), _assistant(usage=_usage(50_000))])
    assert st0.compaction_count == 0


def test_detect_phase_variants():
    st = tl.TranscriptStats()
    st.recent_commit = True
    assert tl.detect_phase(st) == "wrapup"
    st = tl.TranscriptStats()
    st.recent_errors = ["e1", "e2"]
    assert tl.detect_phase(st) == "debugging"
    st = tl.TranscriptStats()
    st.edited_files = ["a.py"]
    assert tl.detect_phase(st) == "implementation"
    assert tl.detect_phase(tl.TranscriptStats()) == "exploration"


def test_instructions_contain_schema_phase_and_anchors():
    st = tl.analyze(os.path.join(FIX, "rich_transcript.jsonl"))
    out = tl.build_preservation_instructions(st, cwd="/tmp/x")
    assert "structured handoff" in out
    assert "Session-specific anchors" in out
    assert "/tmp/x" in out


# ---------------------------------------------------------------- artifacts

def test_artifact_roundtrip_and_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    st = tl.analyze(os.path.join(FIX, "rich_transcript.jsonl"))
    arts = artifacts.extract(st)
    artifacts.save("pytest-sess", arts)
    loaded = artifacts.load("pytest-sess")
    assert loaded
    digest = artifacts.build_digest(loaded, budget_tokens=100)
    assert digest
    assert len(digest) <= 100 * 6  # budget is approximate, never wildly over


def test_build_digest_does_not_return_header_only_for_large_single_section():
    arts = {"files": {"edited": ["/" + "x" * 5000 + ".py"], "read": []}}
    digest = artifacts.build_digest(arts, budget_tokens=20)
    assert "FILES:" in digest
    assert len(digest.split("\n\n", 1)[1].strip()) > 0


def test_merge_unions_and_supersedes():
    old = {"corrections": ["use tabs"], "working_commands": ["make test"],
           "error_ledger": [{"error": "E1", "count": 3}],
           "hex_constants": [], "files": {"edited": ["a.py"], "read": []}}
    new = {"corrections": ["use tabs", "no emoji"],
           "working_commands": ["make lint"],
           "error_ledger": [{"error": "E1", "count": 1},
                            {"error": "E2", "count": 2}],
           "hex_constants": [], "files": {"edited": ["b.py"], "read": []}}
    m = artifacts.merge(old, new)
    assert m["corrections"] == ["use tabs", "no emoji"]  # deduped, ordered
    assert m["working_commands"] == ["make test", "make lint"]
    led = {e["error"]: e["count"] for e in m["error_ledger"]}
    assert led == {"E1": 3, "E2": 2}  # max, not sum
    assert m["files"]["edited"] == ["a.py", "b.py"]


def test_merge_handles_empty_sides():
    full = {"corrections": ["x"], "error_ledger": [],
            "working_commands": [], "hex_constants": [],
            "files": {"edited": [], "read": []}}
    assert artifacts.merge({}, full) == full
    assert artifacts.merge(full, {}) == full


def test_initial_prompts_captured_verbatim():
    entries = [
        _human("Build the frobnicator with 100% compat"),
        _assistant(text="ok"),
        _human("also add tests"),
        _human("third message"),
        _human("fourth message -- beyond the cap"),
    ]
    st = tl.analyze(entries=entries)
    assert st.initial_user_prompts == [
        "Build the frobnicator with 100% compat",
        "also add tests",
        "third message",
    ]
    assert st.last_user_task == "fourth message -- beyond the cap"
    instr = tl.build_preservation_instructions(st, cwd="")
    assert "Build the frobnicator with 100% compat" in instr


def test_initial_prompts_skip_harness_injected():
    summary = _human("This session is being continued from a previous "
                     "conversation that ran out of context...")
    summary["isCompactSummary"] = True
    meta = _human("Caveat: the messages below were generated by the user...")
    meta["isMeta"] = True
    entries = [summary, meta, _human("real founding prompt"),
               _human("/compact")]
    st = tl.analyze(entries=entries)
    assert st.initial_user_prompts == ["real founding prompt"]
    assert st.last_user_task == "real founding prompt"


def test_initial_prompts_merge_old_wins():
    old = {"initial_prompts": ["the founding goal"]}
    new = {"initial_prompts": ["post-compaction first message"],
           "corrections": ["c"]}
    m = artifacts.merge(old, new)
    assert m["initial_prompts"] == ["the founding goal"]
    # absent on the old side -> the fresh extraction fills it in
    m2 = artifacts.merge({"corrections": ["x"]}, new)
    assert m2["initial_prompts"] == ["post-compaction first message"]


def test_digest_founding_goal_survives_budget_trim():
    arts = {
        "initial_prompts": ["build X exactly as specified"],
        "corrections": ["use tabs"] * 30,
        "error_ledger": [{"error": f"E{i}", "count": 1} for i in range(30)],
        "working_commands": ["cmd --flag"] * 20,
        "hex_constants": [],
        "files": {"edited": ["a.py"], "read": []},
    }
    digest = artifacts.build_digest(arts, budget_tokens=60)
    assert "FOUNDING GOAL" in digest
    assert "build X exactly as specified" in digest
    # the tight budget trims lower-priority sections first
    assert "KNOWN-WORKING COMMANDS" not in digest


def test_monitor_persists_artifacts_every_prompt(tmp_path):
    """Continuous extraction: artifacts on disk after a plain monitor run,
    and facts survive even when later extractions no longer contain them
    (post-compaction transcript)."""
    payload = json.dumps({
        "session_id": "cont", "cwd": "/tmp",
        "transcript_path": os.path.join(FIX, "rich_transcript.jsonl"),
        "hook_event_name": "UserPromptSubmit", "prompt": "carry on"})
    r = _run_hook(MONITOR, payload, tmp_path)
    assert r.returncode == 0
    art_file = (tmp_path / ".claude" / "autocompactor" / "artifacts"
                / "cont.json")
    assert art_file.exists()
    before = json.loads(art_file.read_text())
    assert before["working_commands"]
    # simulate a post-compaction prompt: tiny transcript, same session
    small = tmp_path / "small.jsonl"
    small.write_text(json.dumps({
        "type": "assistant", "message": {
            "usage": {"input_tokens": 50_000},
            "content": [{"type": "text", "text": "fresh start"}]}}) + "\n")
    payload2 = json.dumps({
        "session_id": "cont", "cwd": "/tmp",
        "transcript_path": str(small),
        "hook_event_name": "UserPromptSubmit", "prompt": "next"})
    r2 = _run_hook(MONITOR, payload2, tmp_path)
    assert r2.returncode == 0
    after = json.loads(art_file.read_text())
    assert after["working_commands"] == before["working_commands"]


# --------------------------------------------------- boundary-offset finder

def test_find_last_boundary_offset_real_vs_mention(tmp_path):
    """The finder must skip lines that merely MENTION the marker string
    (e.g. a session about this very tool) and land on the real boundary."""
    p = tmp_path / "t.jsonl"
    lines = [
        json.dumps(_assistant(usage=_usage(150_000))),
        json.dumps({"type": "system", "subtype": "compact_boundary",
                    "compactMetadata": {"trigger": "auto",
                                        "preTokens": 150_000,
                                        "postTokens": 20_000}}),
        json.dumps(_assistant(
            usage=_usage(25_000),
            text='we grep for "compact_boundary" markers in transcripts')),
    ]
    p.write_text("\n".join(lines) + "\n")
    off = tl.find_last_boundary_offset(str(p))
    assert off == len(lines[0]) + 1  # start of the real boundary line
    entries = tl.load_transcript(str(p), start_offset=off)
    assert entries[0].get("subtype") == "compact_boundary"
    assert len(entries) == 2


def test_find_last_boundary_offset_none(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps(_assistant(usage=_usage(1000))) + "\n")
    assert tl.find_last_boundary_offset(str(p)) == 0
    assert tl.find_last_boundary_offset("/nonexistent/nope.jsonl") == 0


def test_find_last_boundary_offset_across_chunks(tmp_path):
    """A boundary far from EOF, with many chunks of trailing data and a
    tiny chunk size, must still be found (chunk-overlap path)."""
    p = tmp_path / "t.jsonl"
    head = json.dumps(_assistant(usage=_usage(90_000)))
    boundary = json.dumps({"type": "system", "subtype": "compact_boundary",
                           "compactMetadata": {"trigger": "manual",
                                               "preTokens": 100_000,
                                               "postTokens": 30_000}})
    filler = json.dumps(_tool_result("x" * 1000))
    p.write_text("\n".join([head, boundary] + [filler] * 200) + "\n")
    off = tl.find_last_boundary_offset(str(p), chunk=4096)
    assert off == len(head) + 1


# ------------------------------------------------------- compaction events

def test_find_compactions_prefers_explicit_markers():
    entries = [
        _assistant(usage=_usage(150_000)),
        {"type": "system", "subtype": "compact_boundary",
         "compactMetadata": {"trigger": "auto", "preTokens": 180_000,
                             "postTokens": 20_000}},
        _assistant(usage=_usage(25_000)),
    ]
    traj = analyze_corpus.trajectory(entries)
    events = analyze_corpus.find_compactions(traj, entries)
    assert len(events) == 1
    ev = events[0]
    assert ev["before"] == 180_000 and ev["after"] == 20_000
    assert ev["trigger"] == "auto" and ev["explicit"]


def test_find_compactions_drop_heuristic_fallback():
    entries = [_assistant(usage=_usage(t), tool_id=f"t{i}")
               for i, t in enumerate([100_000, 150_000, 40_000])]
    traj = analyze_corpus.trajectory(entries)
    events = analyze_corpus.find_compactions(traj, entries)
    assert len(events) == 1
    assert events[0]["trigger"] == "inferred"
    assert events[0]["before"] == 150_000


def test_backtest_session_on_fixture_corpus():
    p = os.path.join(FIX, "corpus", "projA", "sessA.jsonl")
    r = analyze_corpus.backtest_session(p, 200_000, 0.40, 0.65)
    assert not r.get("skipped")
    assert "signal_observations" in r
    # feeds the nightly rapid-refill-breaker watch
    assert "post_last_compaction_peak" in r
    assert "learned_window" in r
    assert "peak_learned_occupancy" in r


def test_analyze_prefix_includes_sampled_entry():
    entries = [
        _assistant(usage=_usage(90_000)),
        _assistant("Bash", {"command": "git commit -m x"}, usage=_usage(160_000)),
    ]
    st = analyze_corpus.analyze_prefix(entries, 1)
    assert st.context_tokens == 160_000
    assert st.recent_commit


def test_analyze_prefix_boundary_cases():
    """Backtester fidelity (fix 6d4a533 uses entries[:upto+1]): upto=0 keeps
    exactly the first entry; upto<0 yields an empty analysis, never an error."""
    entries = [_human("start"), _assistant(usage=_usage(50_000))]
    st0 = analyze_corpus.analyze_prefix(entries, 0)
    assert len(st0.entries) == 1 and st0.context_tokens == 0
    st_neg = analyze_corpus.analyze_prefix(entries, -1)
    assert st_neg.entries == [] and st_neg.context_tokens == 0


def test_backtester_signal_registry_matches_monitor():
    """Invariant: the live monitor and the backtester MUST share one signal
    registry. Pin it — analyze_corpus.active_signals (names) equals the names
    transcript_lib yields for the same state, so the two cannot silently
    diverge (the backtester is just the name-projection of the registry)."""
    st = tl.TranscriptStats()
    st.recent_commit = True
    st.todos = [{"content": "a", "status": "completed"}]
    st.todos_all_done = True
    st.stale_tool_chars, st.total_tool_chars = 80, 100
    st.usage_series = [100_000, 130_000, 160_000]
    st.context_tokens = 160_000
    registry = [name for name, _ in tl.active_signals(
        st, window=200_000, stale_frac_thr=0.5)]
    backtester = analyze_corpus.active_signals(
        st, window=200_000, stale_frac_thr=0.5)
    assert backtester == registry
    assert "commit" in backtester  # sanity: this state actually fires signals


def test_backtest_replay_uses_configured_signal_thresholds(tmp_path):
    entries = [_assistant(usage=_usage(t)) for t in (
        120_000, 130_000, 140_000, 150_000, 160_000, 200_000, 160_000)]
    p = tmp_path / "wide.jsonl"
    p.write_text("".join(json.dumps(e) + "\n" for e in entries))
    report = analyze_corpus.backtest_session(
        str(p), window=400_000, soft=0.4, hard=0.65,
        min_eval=90_000, lead_tokens=50_000)
    observed = {ob["signal"] for ob in report["signal_observations"]}
    assert "burn_rate" not in observed
    assert not report["first_recommendation"]


def test_backtest_reports_learned_window_occupancy_for_large_peak(tmp_path):
    entries = [_assistant(usage=_usage(t)) for t in (
        120_000, 200_000, 341_000, 320_000, 330_000)]
    p = tmp_path / "large-peak.jsonl"
    p.write_text("".join(json.dumps(e) + "\n" for e in entries))
    report = analyze_corpus.backtest_session(
        str(p), window=300_000, soft=0.5, hard=0.62)
    assert report["learned_window"] == 512_000
    assert report["learned_tier"] == "512k"
    assert report["peak_learned_occupancy"] == 341_000 / 512_000


def test_backtest_late_by_tokens_resets_after_each_compaction(tmp_path):
    def boundary(pre, post=80_000):
        return {"type": "system", "subtype": "compact_boundary",
                "compactMetadata": {
                    "preTokens": pre, "postTokens": post, "trigger": "auto"}}

    entries = [
        _assistant(usage=_usage(100_000)),
        _assistant(usage=_usage(130_000)),
        _assistant("Bash", {"command": "git commit -m first"},
                   usage=_usage(160_000)),
        _assistant(usage=_usage(210_000)),
        boundary(210_000),
        _assistant(usage=_usage(80_000)),
        _assistant(usage=_usage(120_000)),
        _assistant(usage=_usage(170_000)),
        _assistant(usage=_usage(210_000)),
        _assistant(usage=_usage(250_000)),
        boundary(250_000),
        _assistant(usage=_usage(80_000)),
    ]
    p = tmp_path / "two-compactions.jsonl"
    p.write_text("".join(json.dumps(e) + "\n" for e in entries))
    report = analyze_corpus.backtest_session(
        str(p), window=300_000, soft=0.5, hard=0.65, min_eval=0)
    # Cycle 1 has a git commit at 160k (commit stays a gating signal), so the
    # first viable SOFT recommendation is at 160k -> 50k before the 210k compaction.
    assert report["compactions"][0]["late_by_tokens"] == 50_000
    # Cycle 2 has no gating signal: burn_rate was demoted to observe-only in the
    # 2026-06-17 recalibration (0.9x lift, sub-baseline), so it no longer gates a
    # SOFT recommendation at 170k. The first viable recommendation is now the
    # unconditional HARD line (0.65*300k = 195k, first crossed at 210k) -> 40k
    # before the 250k compaction. This is the precision/early-warning tradeoff of
    # the recalibration; the burst-lateness root cause (pending_reinject) is a
    # separate lever tracked in WORKLOG WI-1.
    assert report["compactions"][1]["late_by_tokens"] == 40_000


def test_build_context_state_uses_window_harness_and_default_count(monkeypatch):
    monkeypatch.setenv("AUTOCOMPACTOR_PI_OBSERVE_ONLY", "stale_output")
    st = tl.TranscriptStats(context_tokens=160_000,
                            usage_series=[120_000, 130_000, 140_000,
                                          150_000, 160_000],
                            stale_tool_chars=60, total_tool_chars=100)
    out = tl.build_context_state(st, window=400_000, harness="pi")
    assert "Occupancy: 40%" in out
    assert "Active signals: none" in out
    assert "Compaction count: 0" in out


# ------------------------------------------- dual-anchor readout + composition

def test_readout_line_separates_advisory_band_from_forced_wall():
    """The advisory soft–hard band and the forced native wall are different
    things; the readout must show both with headroom so 'near the soft limit'
    can't be misread as 'one turn from auto-compacting' (owner feedback)."""
    line = policy.readout_line(209_000, 100_000, 110_000, 270_000, 1_000_000)
    assert "209k in context" in line
    assert "compact advised ~100k–110k" in line
    assert "forced auto-compact ~270k" in line
    assert "61k away" in line              # headroom to the forced wall is shown
    assert "%" not in line                 # never a bare occupancy %


def test_readout_line_flags_when_forced_wall_reached():
    """Edge case: context at/over the forced wall must not silently drop the
    headroom clause and read as 'comfortably below'."""
    line = policy.readout_line(285_000, 100_000, 110_000, 270_000)
    assert "reached" in line and "imminent" in line


def test_readout_line_pi_shape_omits_absent_forced_wall():
    """Pi actuates at its own hard line — there is no separate native wall, so
    forced_auto is None and only the advisory band + true model window show."""
    line = policy.readout_line(210_000, 225_000, 405_000, None, 450_000)
    assert "forced auto-compact" not in line
    assert "model window 450k" in line


def test_context_composition_reconciles_to_true_total():
    """Per-category estimates (chars/4) are reconciled so the parts always sum
    to the authoritative context_tokens; the residual 'base' absorbs error."""
    st = tl.TranscriptStats(context_tokens=209_000, total_tool_chars=480_000,
                            stale_tool_chars=437_000,
                            assistant_text_chars=60_000,
                            user_prompt_chars=16_000)
    comp = tl.context_composition(st, st.context_tokens)
    assert comp["base"] + comp["tool"] + comp["assistant"] + comp["prompts"] \
        == comp["total"] == 209_000
    assert 0.90 <= comp["tool_stale_frac"] <= 0.92
    line = policy.composition_line(comp)
    assert "floor" in line and "tool" in line and "stale" in line


def test_context_composition_scales_when_estimate_overshoots():
    """If chars/4 exceeds the true total, content categories scale down to fit
    and base goes to 0 — never a breakdown that sums past the real total."""
    st = tl.TranscriptStats(context_tokens=50_000, total_tool_chars=400_000,
                            stale_tool_chars=200_000,
                            assistant_text_chars=200_000,
                            user_prompt_chars=40_000)
    comp = tl.context_composition(st, st.context_tokens)
    assert comp["base"] == 0
    assert comp["tool"] + comp["assistant"] + comp["prompts"] == 50_000


def test_context_composition_surfaces_loaded_skills():
    """Owner finding: a loaded skill body (isMeta injection) can be ~80% of the
    window and is RECLAIMABLE — it must be surfaced as its own category, not
    buried in 'floor', and the residual relabelled system+tools."""
    st = tl.TranscriptStats(context_tokens=200_000,
                            skill_chars=600_000,        # ~150k tok loaded skill
                            skill_names=["claude-api"],
                            summary_chars=16_000,       # ~4k tok carried summary
                            total_tool_chars=8_000,
                            stale_tool_chars=4_000,
                            assistant_text_chars=4_000)
    comp = tl.context_composition(st, st.context_tokens)
    assert comp["skills"] == 150_000 and comp["summary"] == 4_000
    # every part (incl. skills + summary) reconciles exactly to the true total
    assert (comp["base"] + comp["skills"] + comp["summary"]
            + comp["tool"] + comp["assistant"] + comp["prompts"]) == 200_000
    assert comp["base"] < 50_000        # 'floor' no longer absorbs the skill
    line = policy.composition_line(comp)
    assert "skills (claude-api)" in line
    assert "system+tools" in line and "floor" not in line
    assert "summary" in line


def test_context_composition_skill_free_session_keeps_floor_label():
    """No loaded skills -> skills=0, residual still reads 'floor' (unchanged
    for ordinary sessions; the relabel only kicks in when a skill dominates)."""
    st = tl.TranscriptStats(context_tokens=120_000, total_tool_chars=40_000,
                            stale_tool_chars=20_000, assistant_text_chars=8_000)
    comp = tl.context_composition(st, st.context_tokens)
    assert comp["skills"] == 0 and comp["summary"] == 0
    line = policy.composition_line(comp)
    assert "floor" in line and "loaded skills" not in line


def test_skill_warning_fires_when_skills_dominate():
    """When loaded skills exceed the dominance threshold, the advisor calls
    them out by name and states /compact won't reclaim them."""
    comp = {"total": 200_000, "skills": 150_000,
            "skill_names": ["claude-api", "systematic-debugging"]}
    w = policy.skill_warning(comp)
    assert w.startswith("⚠")
    assert "150k (75%)" in w
    assert "claude-api" in w and "won't reclaim" in w and "unload" in w


def test_skill_warning_silent_below_threshold_and_when_empty():
    assert policy.skill_warning({"total": 200_000, "skills": 20_000,
                                 "skill_names": ["x"]}) == ""   # 10% < 40%
    assert policy.skill_warning({"total": 0, "skills": 0}) == ""


def test_preservation_ledger_names_preserved_lossy_and_dropped():
    """Owner request (b): the compaction ledger names what's kept verbatim,
    what's left to the lossy summarizer, and what was trimmed for budget."""
    arts = {
        "initial_prompts": ["do the thing"],
        "corrections": ["c1", "c2", "c3"],
        "error_ledger": [{"error": "e%d" % i, "count": 1} for i in range(5)],
        "working_commands": ["cmd%d" % i for i in range(12)],
        "hex_constants": ["0xAB ctx"],
        "files": {"edited": ["a", "b"], "read": list("cdefgh")},
    }
    full = artifacts.preservation_ledger(arts, lossy_tokens=15_000)
    assert "preserved verbatim" in full
    assert "3 corrections" in full and "12 commands" in full
    assert "left to summarizer (lossy)" in full and "15k" in full
    # A tight budget forces low-priority categories out of the digest, and the
    # ledger must say so — sharing budget_plan() with build_digest so they
    # can never disagree about what was kept.
    trimmed = artifacts.preservation_ledger(arts, budget_tokens=20)
    _, dropped = artifacts.budget_plan(arts, budget_tokens=20)
    assert dropped and "dropped for budget" in trimmed


def test_burn_rate_signal_does_not_claim_forced_autocompact():
    """The burn_rate description approaches the ADVISORY compact line, not the
    forced native wall — the two were conflated (owner feedback)."""
    st = tl.TranscriptStats(context_tokens=100_000,
                            usage_series=[70_000, 80_000, 90_000, 100_000])
    sigs = dict(tl.active_signals(st, window=200_000, hard_tokens=110_000))
    assert "burn_rate" in sigs              # within the horizon at this burn
    assert "autocompact" not in sigs["burn_rate"]
    assert "compact line" in sigs["burn_rate"]


# ------------------------------------------------------------ hook contract

MONITOR = "src/context_monitor.py"
ANALYZER = "src/precompact_analyzer.py"


def test_monitor_clamps_window_for_small_sessions(tmp_path):
    """A session that never exceeded 200k-model reach must be evaluated
    against a 200k effective window even when AUTOCOMPACTOR_WINDOW is
    tuned for 1M models — otherwise the monitor is silent for the whole
    life of small-window sessions."""
    payload = json.dumps({
        "session_id": "clamp", "cwd": "/tmp",
        "transcript_path": os.path.join(FIX, "rich_transcript.jsonl"),
        "hook_event_name": "UserPromptSubmit",
        "prompt": "now plan the website redesign"})
    env = _hook_env(tmp_path)
    env["AUTOCOMPACTOR_WINDOW"] = "400000"  # 1M-model tuning
    r = subprocess.run(
        [sys.executable, os.path.join(REPO, MONITOR)],
        input=payload, capture_output=True, text=True, env=env,
        cwd=REPO, timeout=60)
    assert r.returncode == 0
    assert "early-compaction suggestion" in r.stdout  # clamped to 200k, not mute


def test_monitor_recommends_on_rich_fixture(tmp_path):
    payload = json.dumps({
        "session_id": "py", "cwd": "/tmp",
        "transcript_path": os.path.join(FIX, "rich_transcript.jsonl"),
        "hook_event_name": "UserPromptSubmit",
        "prompt": "now plan the website redesign"})
    r = _run_hook(MONITOR, payload, tmp_path)
    assert r.returncode == 0
    assert "early-compaction suggestion" in r.stdout
    ev = json.loads((tmp_path / ".claude" / "autocompactor" / "stats"
                     / "events.jsonl").read_text().splitlines()[-1])
    for key in (
            "effective_window", "configured_window", "learned_window",
            "learned_tier", "window_source",
            "native_ceiling_blocks_learned_window"):
        assert key in ev
    assert ev["effective_window"] == 200_000
    assert ev["configured_window"] == 200_000


def test_monitor_min_savings_guard_suppresses(tmp_path):
    """Below POST_FLOOR + MIN_SAVINGS a compaction can't reclaim enough to
    pay for its 30-60s stall — the monitor must stay quiet even when
    occupancy and signals say go."""
    payload = json.dumps({
        "session_id": "guard", "cwd": "/tmp",
        "transcript_path": os.path.join(FIX, "rich_transcript.jsonl"),
        "hook_event_name": "UserPromptSubmit",
        "prompt": "now plan the website redesign"})
    env = _hook_env(tmp_path)
    env["AUTOCOMPACTOR_POST_FLOOR"] = "160000"  # fixture ctx ~170k ->
    env["AUTOCOMPACTOR_MIN_SAVINGS"] = "30000"  # est. reclaim ~10k < 30k
    r = subprocess.run(
        [sys.executable, os.path.join(REPO, MONITOR)],
        input=payload, capture_output=True, text=True, env=env,
        cwd=REPO, timeout=60)
    assert r.returncode == 0
    assert "early-compaction suggestion" not in r.stdout
    ev = json.loads((tmp_path / ".claude" / "autocompactor" / "stats"
                     / "events.jsonl").read_text().splitlines()[-1])
    assert ev["recommended"] is False
    assert 0 < ev["est_reclaim"] < 30_000


def test_monitor_cooldown_deadlock_breaks_on_shrink(tmp_path):
    """Regression for issue #1 (Claude harness): a reco staged at a high token
    count that reinject never reset used to pin last_reco_tokens above the
    live context forever — a shrunken context's negative delta was always
    < cooldown, so the monitor stayed mute even far past HARD_PCT.

    Fix: cooldown debounces rising context only; a shrunken context resets
    the baseline and the bricked state self-heals.
    """
    state_dir = tmp_path / ".claude" / "autocompactor"
    state_dir.mkdir(parents=True)
    # Bricked state: a reco staged at 494339 that reinject never reset.
    # (pending_reinject is False here: the monitor clears it on the prompt
    # after compaction, then the cooldown logic runs — that is the state in
    # which the deadlock actually persists across turns.)
    (state_dir / "deadlock.state.json").write_text(json.dumps({
        "last_reco_tokens": 494339,
        "pending_reinject": False,
        "compaction_count": 2,
    }))
    payload = json.dumps({
        "session_id": "deadlock", "cwd": "/tmp",
        "transcript_path": os.path.join(FIX, "rich_transcript.jsonl"),
        "hook_event_name": "UserPromptSubmit",
        "prompt": "now plan the website redesign"})
    env = _hook_env(tmp_path)
    # Fixture context ~84k; force a hard-threshold occupancy against a tiny
    # window so occupancy >= HARD_PCT regardless of signals.
    env["AUTOCOMPACTOR_WINDOW"] = "100000"   # 84k/100k = 84%
    env["AUTOCOMPACTOR_HARD_PCT"] = "0.5"
    env["AUTOCOMPACTOR_COOLDOWN"] = "20000"
    env["AUTOCOMPACTOR_POST_FLOOR"] = "0"      # don't let min_savings mask
    env["AUTOCOMPACTOR_MIN_SAVINGS"] = "0"
    r = subprocess.run(
        [sys.executable, os.path.join(REPO, MONITOR)],
        input=payload, capture_output=True, text=True, env=env,
        cwd=REPO, timeout=60)
    assert r.returncode == 0
    # Past HARD_PCT must recommend despite the stale high baseline.
    assert "early-compaction suggestion" in r.stdout
    ev = json.loads((state_dir / "stats" / "events.jsonl")
                    .read_text().splitlines()[-1])
    assert ev["recommended"] is True
    # Bricked baseline self-healed.
    healed = json.loads((state_dir / "deadlock.state.json").read_text())
    assert healed["last_reco_tokens"] != 494339


def test_observe_only_signals_never_gate(tmp_path):
    """Anti-predictive signals (error_resolved et al.) keep flowing into
    telemetry but must not appear in — or justify — a recommendation."""
    payload = json.dumps({
        "session_id": "obsonly", "cwd": "/tmp",
        "transcript_path": os.path.join(FIX, "rich_transcript.jsonl"),
        "hook_event_name": "UserPromptSubmit",
        "prompt": "now plan the website redesign"})
    r = _run_hook(MONITOR, payload, tmp_path)
    assert r.returncode == 0
    assert "early-compaction suggestion" in r.stdout       # gates on todo_step etc.
    assert "debug loop just concluded" not in r.stdout
    ev = json.loads((tmp_path / ".claude" / "autocompactor" / "stats"
                     / "events.jsonl").read_text().splitlines()[-1])
    assert "a debug loop just concluded" in ev["signals"]  # still observed


def test_observe_only_env_override(tmp_path):
    """AUTOCOMPACTOR_OBSERVE_ONLY= (empty) restores full gating, so the
    demotion is a tunable, not a hardcode."""
    payload = json.dumps({
        "session_id": "obsoverride", "cwd": "/tmp",
        "transcript_path": os.path.join(FIX, "rich_transcript.jsonl"),
        "hook_event_name": "UserPromptSubmit",
        "prompt": "now plan the website redesign"})
    env = _hook_env(tmp_path)
    env["AUTOCOMPACTOR_OBSERVE_ONLY"] = ""
    r = subprocess.run(
        [sys.executable, os.path.join(REPO, MONITOR)],
        input=payload, capture_output=True, text=True, env=env,
        cwd=REPO, timeout=60)
    assert r.returncode == 0
    assert "debug loop just concluded" in r.stdout


def test_monitor_tail_parse_after_boundary(tmp_path):
    """Past MAX_FULL_PARSE_MB the monitor parses only the active segment
    after the last compaction boundary; telemetry records tail_parse and
    context reflects the post-boundary segment."""
    big = tmp_path / "big.jsonl"
    pre = [json.dumps(_assistant(usage=_usage(150_000), text="x" * 2000))
           for _ in range(10)]
    boundary = json.dumps({"type": "system", "subtype": "compact_boundary",
                           "compactMetadata": {"trigger": "auto",
                                               "preTokens": 150_000,
                                               "postTokens": 60_000}})
    post = json.dumps(_assistant(usage=_usage(60_000)))
    big.write_text("\n".join(pre + [boundary, post]) + "\n")
    payload = json.dumps({
        "session_id": "tailp", "cwd": "/tmp", "transcript_path": str(big),
        "hook_event_name": "UserPromptSubmit", "prompt": "hi"})
    env = _hook_env(tmp_path)
    env["AUTOCOMPACTOR_MAX_FULL_PARSE_MB"] = "0.001"  # force tail mode
    r = subprocess.run(
        [sys.executable, os.path.join(REPO, MONITOR)],
        input=payload, capture_output=True, text=True, env=env,
        cwd=REPO, timeout=60)
    assert r.returncode == 0
    ev = json.loads((tmp_path / ".claude" / "autocompactor" / "stats"
                     / "events.jsonl").read_text().splitlines()[-1])
    assert ev["tail_parse"] is True
    assert ev["context_tokens"] == 60_000


def test_monitor_carried_peak_prevents_misclamp(tmp_path):
    """A 1M-model session whose >190k peak happened before the last
    compaction must not be clamped to a 200k effective window when later
    parses only see the small active segment — peak_ctx is carried in the
    per-session state file."""
    state_dir = tmp_path / ".claude" / "autocompactor"
    state_dir.mkdir(parents=True)
    (state_dir / "carry.state.json").write_text(
        json.dumps({"peak_ctx": 350_000}))
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps(_assistant(usage=_usage(100_000))) + "\n")
    payload = json.dumps({
        "session_id": "carry", "cwd": "/tmp", "transcript_path": str(t),
        "hook_event_name": "UserPromptSubmit", "prompt": "hi"})
    env = _hook_env(tmp_path)
    env["AUTOCOMPACTOR_WINDOW"] = "400000"
    r = subprocess.run(
        [sys.executable, os.path.join(REPO, MONITOR)],
        input=payload, capture_output=True, text=True, env=env,
        cwd=REPO, timeout=60)
    assert r.returncode == 0
    ev = json.loads((state_dir / "stats" / "events.jsonl")
                    .read_text().splitlines()[-1])
    assert ev["occupancy"] == 0.25  # 100k/400k — not 100k/200k


@pytest.mark.parametrize("script", [MONITOR, ANALYZER])
@pytest.mark.parametrize("payload", [
    "", "not json", "{}",
    '{"session_id":"x","transcript_path":"/nonexistent/t.jsonl",'
    '"hook_event_name":"UserPromptSubmit","prompt":"hi"}',
])
def test_hooks_never_raise(script, payload, tmp_path):
    """Hooks must degrade silently on any input — never break the hook path."""
    r = _run_hook(script, payload, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr


# --------------------------- never-raise hardening (malformed transcript body)

def test_analyze_survives_non_dict_usage():
    """A non-dict message.usage (corruption / producer-version skew) reaches
    usage.get(...) and used to crash analyze(); the turn is now skipped and a
    later well-formed turn is still counted."""
    entries = [
        _human("hi"),
        {"type": "assistant", "message": {"usage": "not-a-dict", "content": []}},
        _assistant(usage=_usage(50_000)),
    ]
    st = tl.analyze(entries=entries)
    assert st.context_tokens == 50_000


def test_load_transcript_skips_non_object_json_lines(tmp_path):
    """Valid JSON that isn't an object (bare string/number) would crash the
    .get() calls in analyze(); load_transcript drops it so the parse holds."""
    p = tmp_path / "t.jsonl"
    p.write_text('"a bare string"\n42\n[1,2,3]\n{"type":"assistant"}\n')
    entries = tl.load_transcript(str(p))
    assert [e.get("type") for e in entries] == ["assistant"]


def test_hooks_survive_malformed_transcript_content(tmp_path):
    """End-to-end: a transcript with a bare-string line and a non-dict usage
    block must not crash either hook — they parse what they can and exit 0."""
    t = tmp_path / "t.jsonl"
    t.write_text(
        '"a bare string line"\n'
        + json.dumps({"type": "assistant",
                      "message": {"usage": "not-a-dict", "content": []}}) + "\n"
        + json.dumps(_assistant(usage=_usage(120_000))) + "\n")
    for script in (MONITOR, ANALYZER):
        payload = json.dumps({
            "session_id": "malformed", "cwd": "/tmp", "transcript_path": str(t),
            "hook_event_name": "UserPromptSubmit", "prompt": "hi",
            "trigger": "manual"})
        r = _run_hook(script, payload, tmp_path)
        assert r.returncode == 0, r.stderr
        assert "Traceback" not in r.stderr


def test_run_hook_backstops_exceptions(tmp_path, monkeypatch):
    """run_hook is the belt-and-suspenders backstop: an exception escaping the
    inner function becomes exit 0 plus a content-free hook_skip breadcrumb
    (error class name only — never the exception message / transcript text)."""
    from autocompactor import stats
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AUTOCOMPACTOR_STATE_DIR", raising=False)

    def boom():
        raise RuntimeError("secret transcript text")

    assert stats.run_hook("unit_probe", boom) == 0
    ev = json.loads((tmp_path / ".claude" / "autocompactor" / "stats"
                     / "events.jsonl").read_text().splitlines()[-1])
    assert ev["type"] == "hook_skip" and ev["hook"] == "unit_probe"
    assert ev["error"] == "RuntimeError"
    assert "secret transcript text" not in json.dumps(ev)


def test_run_hook_passes_through_return_code(tmp_path, monkeypatch):
    """A clean inner run returns its own exit code unchanged."""
    from autocompactor import stats
    monkeypatch.setenv("HOME", str(tmp_path))
    assert stats.run_hook("ok_probe", lambda: 0) == 0


# ------------------------------------------------ every-prompt I/O cheapness

def test_monitor_skips_redundant_writes_on_unchanged_prompt(tmp_path):
    """C1+C2: a second identical prompt with no new transcript activity must
    rewrite neither the artifact file nor the state file (an identical-content
    rewrite still bumps mtime, so mtime is the right witness)."""
    t = tmp_path / "t.jsonl"
    # Low context (no recommendation) but with extractable activity, so the
    # artifact file exists after the first run.
    t.write_text(
        json.dumps(_human("build the thing")) + "\n"
        + json.dumps(_assistant("Edit", {"file_path": "/a.py"},
                                usage=_usage(40_000))) + "\n")
    payload = json.dumps({
        "session_id": "cheap", "cwd": "/tmp", "transcript_path": str(t),
        "hook_event_name": "UserPromptSubmit", "prompt": "build the thing"})

    assert _run_hook(MONITOR, payload, tmp_path).returncode == 0
    base = tmp_path / ".claude" / "autocompactor"
    art = base / "artifacts" / "cheap.json"
    state = base / "cheap.state.json"
    assert art.exists() and state.exists()
    m_art, m_state = art.stat().st_mtime_ns, state.stat().st_mtime_ns

    # Second identical run: nothing changed -> neither file is rewritten.
    assert _run_hook(MONITOR, payload, tmp_path).returncode == 0
    assert art.stat().st_mtime_ns == m_art, "artifact rewritten with no change"
    assert state.stat().st_mtime_ns == m_state, "state rewritten with no change"


def _isolate_config(monkeypatch):
    """Hermetic in-process config: ignore repo config.json/config.local.json."""
    from autocompactor import config_lib
    monkeypatch.setenv("AUTOCOMPACTOR_CONFIG", "")
    monkeypatch.setattr(config_lib, "_config_cache", None)


def test_llm_digest_default_uses_claude_haiku(tmp_path, monkeypatch):
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps(_human("keep this task")) + "\n")
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout="- keep task\n", stderr="")

    monkeypatch.delenv("AUTOCOMPACTOR_LLM_CMD", raising=False)
    monkeypatch.delenv("AUTOCOMPACTOR_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("AUTOCOMPACTOR_LLM_MODEL", raising=False)
    _isolate_config(monkeypatch)
    monkeypatch.setattr(pa.subprocess, "run", fake_run)
    assert pa.llm_digest(str(t)) == "- keep task"
    assert seen["cmd"][:4] == ["claude", "-p", "--model", "haiku"]


def test_llm_digest_openai_override_is_env_only(tmp_path, monkeypatch):
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps(_human("keep this task")) + "\n")
    seen = {}

    class FakeResponse:
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            return False
        def read(self):
            return json.dumps({"choices": [{"message": {"content": "- local model fact"}}]}).encode()

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["timeout"] = timeout
        seen["body"] = json.loads(req.data.decode())
        return FakeResponse()

    monkeypatch.setenv("AUTOCOMPACTOR_LLM_PROVIDER", "openai")
    monkeypatch.setenv("AUTOCOMPACTOR_LLM_BASE_URL", "http://local.test:8000/v1")
    monkeypatch.setenv("AUTOCOMPACTOR_LLM_MODEL", "local-test-model")
    _isolate_config(monkeypatch)
    monkeypatch.setattr(pa.urllib.request, "urlopen", fake_urlopen)
    assert pa.llm_digest(str(t)) == "- local model fact"
    assert seen["url"] == "http://local.test:8000/v1/chat/completions"
    assert seen["body"]["model"] == "local-test-model"


def test_public_config_does_not_pin_site_local_llm_defaults():
    cfg_text = open(os.path.join(REPO, "config.json"), encoding="utf-8").read().lower()
    assert "llm_model" not in cfg_text
    assert "llm_base_url" not in cfg_text
    assert "192.168." not in cfg_text


def test_analyzer_emits_instructions_and_artifacts(tmp_path):
    payload = json.dumps({
        "session_id": "py2", "cwd": "/tmp",
        "transcript_path": os.path.join(FIX, "rich_transcript.jsonl"),
        "hook_event_name": "PreCompact", "trigger": "manual",
        "custom_instructions": "user note"})
    r = _run_hook(ANALYZER, payload, tmp_path)
    assert r.returncode == 0
    assert "structured handoff" in r.stdout
    assert "user note" in r.stdout
    assert (tmp_path / ".claude" / "autocompactor" / "artifacts"
            / "py2.json").exists()
    ev = json.loads((tmp_path / ".claude" / "autocompactor" / "stats"
                     / "events.jsonl").read_text().splitlines()[-1])
    assert ev["type"] == "precompact"
    for key in ("effective_window", "configured_window", "learned_window",
                "learned_tier", "window_source"):
        assert key in ev


def test_analyzer_systemmessage_summary(tmp_path):
    """PreCompact emits NO user-facing systemMessage now (combined into the single
    PostCompact notice). It still emits customInstructions and stashes a
    content-free analysis summary — trigger, context, phase, artifact accounting,
    instruction source — for telemetry / the PostCompact notice. Content-free:
    no transcript text beyond signal descriptions."""
    payload = json.dumps({
        "session_id": "sum1", "cwd": "/tmp",
        "transcript_path": os.path.join(FIX, "rich_transcript.jsonl"),
        "hook_event_name": "PreCompact", "trigger": "auto",
        "custom_instructions": "user note"})
    r = _run_hook(ANALYZER, payload, tmp_path)
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert "hookSpecificOutput" in out          # instructions still emitted
    assert "systemMessage" not in out           # combined into PostCompact
    stashed = json.loads((tmp_path / ".claude" / "autocompactor"
                          / "sum1.state.json").read_text())["last_compaction_stats"]
    assert stashed.startswith("compaction #1 (auto)")
    # Absolute-anchor readout (WI-2): "<used> in context · compact advised
    # ~<soft>–<hard> · forced auto-compact ~<auto> (~<headroom> away)" replaced
    # the old bare "context ~Xt (Y% of Zk)" occupancy string.
    assert "in context" in stashed
    assert "compact advised ~" in stashed
    assert "phase: " in stashed
    assert "artifacts to disk: " in stashed
    assert "instructions: fresh analysis" in stashed
    assert "user notes kept" in stashed


def test_analyzer_summary_reports_staged_instructions(tmp_path):
    """When the monitor staged instructions moments earlier, the stashed summary
    must say so (and the count must increment across compactions)."""
    state_dir = tmp_path / ".claude" / "autocompactor"
    state_dir.mkdir(parents=True)
    (state_dir / "sum2.state.json").write_text(json.dumps(
        {"staged_instructions": "STAGED", "compaction_count": 2}))
    payload = json.dumps({
        "session_id": "sum2", "cwd": "/tmp",
        "transcript_path": os.path.join(FIX, "rich_transcript.jsonl"),
        "hook_event_name": "PreCompact", "trigger": "manual"})
    r = _run_hook(ANALYZER, payload, tmp_path)
    assert r.returncode == 0
    stashed = json.loads(
        (state_dir / "sum2.state.json").read_text())["last_compaction_stats"]
    assert "compaction #3 (manual)" in stashed
    assert "instructions: staged by monitor" in stashed


def test_postcompact_notice_surfaces_skills_from_pre_state(tmp_path, monkeypatch,
                                                           capsys):
    """PostCompact (new surface): a PreCompact systemMessage is swallowed by the
    compaction redraw, so the user-visible notice fires here. With no parseable
    new transcript it renders from the stashed pre-compaction composition —
    including the loaded-skill warning."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(pa, "STATE_DIR", str(tmp_path))
    (tmp_path / "s1.state.json").write_text(json.dumps({
        "pre_compact_tokens": 244_000,
        "pre_comp": {"total": 244_000, "base": 52_000, "skills": 156_000,
                     "skill_names": ["claude-api"], "summary": 4_000,
                     "tool": 7_000, "tool_stale_frac": 0.8,
                     "assistant": 2_000, "prompts": 12}}))
    rc = pa._run_postcompact({"hook_event_name": "PostCompact",
                              "session_id": "s1",
                              "transcript_path": "/nonexistent/x.jsonl",
                              "trigger": "auto"})
    assert rc == 0
    msg = json.loads(capsys.readouterr().out)["systemMessage"]
    assert msg.startswith("autocompactor: compaction complete")
    assert "244k in context before compaction" in msg
    assert "skills (claude-api)" in msg
    assert "won't reclaim" in msg          # skill warning rendered


def test_postcompact_shows_before_after_delta(tmp_path, monkeypatch, capsys):
    """When the compacted transcript parses to a smaller total, show the
    before -> after delta the user just gained."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(pa, "STATE_DIR", str(tmp_path))
    (tmp_path / "d1.state.json").write_text(json.dumps({
        "pre_compact_tokens": 244_000,
        "pre_comp": {"total": 244_000, "base": 80_000, "tool": 150_000,
                     "tool_stale_frac": 0.9, "assistant": 14_000,
                     "prompts": 5, "skills": 0, "summary": 0}}))
    t = tmp_path / "compacted.jsonl"
    t.write_text("\n".join(json.dumps(e) for e in [
        _human("resume"), _assistant(text="ok", usage=_usage(8000))]))
    rc = pa._run_postcompact({"hook_event_name": "PostCompact",
                              "session_id": "d1", "transcript_path": str(t),
                              "trigger": "auto"})
    assert rc == 0
    msg = json.loads(capsys.readouterr().out)["systemMessage"]
    assert "→" in msg and "reclaimed" in msg and "244k" in msg


def test_postcompact_hook_reads_precompact_state(tmp_path):
    """End-to-end: the real entrypoint branches on hook_event_name. PreCompact
    stashes pre_compact_tokens; PostCompact reads it and emits a notice-only
    systemMessage (no hookSpecificOutput/customInstructions)."""
    pre = json.dumps({"session_id": "pc1", "cwd": "/tmp",
                      "transcript_path": os.path.join(FIX, "rich_transcript.jsonl"),
                      "hook_event_name": "PreCompact", "trigger": "manual"})
    assert _run_hook(ANALYZER, pre, tmp_path).returncode == 0
    post = json.dumps({"session_id": "pc1", "cwd": "/tmp",
                       "transcript_path": "/nonexistent/none.jsonl",
                       "hook_event_name": "PostCompact", "trigger": "auto"})
    r = _run_hook(ANALYZER, post, tmp_path)
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert "hookSpecificOutput" not in out        # PostCompact: notice only
    # PreCompact stashed compaction_count=1 -> single combined notice carries it
    assert out["systemMessage"].startswith("autocompactor: compaction #1 complete")
    assert "in context before compaction" in out["systemMessage"]
    # Combined notice carries the preservation ledger (owner request (b)): the two
    # compaction-time outputs are merged into this single PostCompact notice.
    assert "preserved verbatim" in out["systemMessage"]


def test_precompact_probe_arms_sentinel_when_enabled(tmp_path):
    """customInstructions live-confirm scaffolding: with
    AUTOCOMPACTOR_CUSTOMINSTR_PROBE=1 the PreCompact emit appends the sentinel to
    customInstructions and stashes it in session state for PostCompact to verify.
    Off by default (the other PreCompact tests run without the env and never
    emit the sentinel)."""
    env = _hook_env(tmp_path)
    env["AUTOCOMPACTOR_CUSTOMINSTR_PROBE"] = "1"
    pre = json.dumps({"session_id": "arm1", "cwd": "/tmp",
                      "transcript_path": os.path.join(FIX, "rich_transcript.jsonl"),
                      "hook_event_name": "PreCompact", "trigger": "manual"})
    r = subprocess.run([sys.executable, os.path.join(REPO, ANALYZER)],
                       input=pre, capture_output=True, text=True,
                       env=env, cwd=REPO, timeout=60)
    assert r.returncode == 0
    ci = json.loads(r.stdout)["hookSpecificOutput"]["customInstructions"]
    assert pa.CUSTOMINSTR_PROBE_SENTINEL in ci
    st = json.loads((tmp_path / ".claude" / "autocompactor"
                     / "arm1.state.json").read_text())
    assert st["custominstr_probe"] == pa.CUSTOMINSTR_PROBE_SENTINEL


def test_postcompact_probe_reports_honored_when_sentinel_in_summary(
        tmp_path, monkeypatch, capsys):
    """The summarizer honored our instructions iff the generated summary
    (compact_summary input) contains the sentinel. Verdict surfaces to the user
    and the one-shot stash is cleared."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(pa, "STATE_DIR", str(tmp_path))
    (tmp_path / "pr1.state.json").write_text(json.dumps({
        "pre_compact_tokens": 120_000, "pre_comp": {},
        "custominstr_probe": pa.CUSTOMINSTR_PROBE_SENTINEL}))
    rc = pa._run_postcompact({
        "hook_event_name": "PostCompact", "session_id": "pr1",
        "transcript_path": "/nonexistent/x.jsonl", "trigger": "manual",
        "compact_summary": pa.CUSTOMINSTR_PROBE_SENTINEL + "\nRest of summary."})
    assert rc == 0
    msg = json.loads(capsys.readouterr().out)["systemMessage"]
    assert "customInstructions probe: HONORED" in msg
    assert json.loads(
        (tmp_path / "pr1.state.json").read_text())["custominstr_probe"] is None


def test_postcompact_probe_reports_noop_when_sentinel_absent(
        tmp_path, monkeypatch, capsys):
    """The expected outcome on Claude Code: the summarizer ignores our
    customInstructions, so the sentinel never appears -> NO-OP verdict."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(pa, "STATE_DIR", str(tmp_path))
    (tmp_path / "pr2.state.json").write_text(json.dumps({
        "pre_compact_tokens": 120_000, "pre_comp": {},
        "custominstr_probe": pa.CUSTOMINSTR_PROBE_SENTINEL}))
    rc = pa._run_postcompact({
        "hook_event_name": "PostCompact", "session_id": "pr2",
        "transcript_path": "/nonexistent/x.jsonl", "trigger": "manual",
        "compact_summary": "A normal summary with no marker in it."})
    assert rc == 0
    msg = json.loads(capsys.readouterr().out)["systemMessage"]
    assert "customInstructions probe: NO-OP" in msg


def test_analyzer_restates_founding_goal_when_staged_lacks_it(tmp_path):
    """Owner directive: every compaction pass must restate the founding
    goal verbatim. Staged instructions built from a tail-only parse can
    miss the original prompts; the analyzer must append them from the
    merged artifacts (old-wins) so they cannot decay across passes."""
    state_dir = tmp_path / ".claude" / "autocompactor"
    (state_dir / "artifacts").mkdir(parents=True)
    (state_dir / "sum4.state.json").write_text(json.dumps(
        {"staged_instructions": "STAGED (tail-parsed, no founding goal)"}))
    (state_dir / "artifacts" / "sum4.json").write_text(json.dumps(
        {"initial_prompts": ["build the frobnicator with 100% compat"]}))
    payload = json.dumps({
        "session_id": "sum4", "cwd": "/tmp",
        "transcript_path": os.path.join(FIX, "rich_transcript.jsonl"),
        "hook_event_name": "PreCompact", "trigger": "auto"})
    r = _run_hook(ANALYZER, payload, tmp_path)
    assert r.returncode == 0
    instr = json.loads(r.stdout)["hookSpecificOutput"]["customInstructions"]
    assert "build the frobnicator with 100% compat" in instr
    assert "ORIGINAL user request" in instr


def test_analyzer_summary_feeds_digest_header(tmp_path):
    """The same summary must arrive on the second surface: the header of
    the one-shot artifact digest re-injected on the first post-compaction
    prompt."""
    payload = json.dumps({
        "session_id": "sum3", "cwd": "/tmp",
        "transcript_path": os.path.join(FIX, "rich_transcript.jsonl"),
        "hook_event_name": "PreCompact", "trigger": "auto"})
    assert _run_hook(ANALYZER, payload, tmp_path).returncode == 0
    payload2 = json.dumps({
        "session_id": "sum3", "cwd": "/tmp",
        "transcript_path": os.path.join(FIX, "rich_transcript.jsonl"),
        "hook_event_name": "UserPromptSubmit", "prompt": "continue"})
    r = _run_hook(MONITOR, payload2, tmp_path)
    assert r.returncode == 0
    digest = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "Durable artifacts recovered" in digest
    assert "compaction #1 (auto)" in digest
    assert "phase: " in digest


# ---------------------------------------------- PostToolUse mid-burst watchdog

def test_current_context_tokens_returns_last_usage(tmp_path):
    """The cheap tail read sums the LAST assistant usage block — the same
    input+cache_read+cache_creation+output formula analyze() uses."""
    p = tmp_path / "ctx.jsonl"
    p.write_text("".join(json.dumps(e) + "\n" for e in [
        _assistant(usage=_usage(40_000)),
        _tool_result("result"),
        _assistant(usage=_usage(180_000)),
        _tool_result("result"),
    ]))
    assert tl.current_context_tokens(str(p)) == 180_000


def test_current_context_tokens_missing_file_is_zero():
    assert tl.current_context_tokens("/nonexistent/zz.jsonl") == 0


def _ptu_payload(session, transcript_path):
    return json.dumps({
        "session_id": session, "cwd": "/tmp",
        "transcript_path": transcript_path,
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash", "tool_input": {}})


def test_posttooluse_watchdog_recommends_above_hard(tmp_path):
    """The whole point of Fix #1: with NO UserPromptSubmit, a context that
    has crossed the hard line mid-burst still gets a recommendation and a
    telemetry row tagged hook_event=PostToolUse."""
    high = tmp_path / "high.jsonl"
    high.write_text("".join(json.dumps(e) + "\n" for e in [
        _assistant(usage=_usage(180_000)),
        _tool_result("x"),
    ]))
    env = _hook_env(tmp_path)               # WINDOW default 200k, HARD 0.65
    env["AUTOCOMPACTOR_WINDOW"] = "200000"  # hard line = 130k
    r = subprocess.run(
        [sys.executable, os.path.join(REPO, MONITOR)],
        input=_ptu_payload("ptu1", str(high)),
        capture_output=True, text=True, env=env, cwd=REPO, timeout=60)
    assert r.returncode == 0
    assert "early-compaction suggestion" in r.stdout
    assert "mid-burst" in r.stdout
    ev = json.loads((tmp_path / ".claude" / "autocompactor" / "stats"
                     / "events.jsonl").read_text().splitlines()[-1])
    assert ev["recommended"] is True
    assert ev["hook_event"] == "PostToolUse"


def test_posttooluse_watchdog_emits_user_visible_systemmessage(tmp_path):
    """Mid-burst recommendations must reach the USER as a verbatim, visible
    readout — that is the systemMessage (the reliable channel in Claude Code;
    additionalContext is Claude-only and Claude relays it unreliably). The full
    readout (anchors + composition) rides systemMessage; additionalContext is the
    Claude-only 'don't restate it' note and carries NO numbers (no double).
    Gated to fire only when a recommendation actually fires."""
    high = tmp_path / "high.jsonl"
    high.write_text("".join(json.dumps(e) + "\n" for e in [
        _assistant(usage=_usage(180_000)),
        _tool_result("x"),
    ]))
    env = _hook_env(tmp_path)
    env["AUTOCOMPACTOR_WINDOW"] = "200000"  # hard line = 130k; 180k > it
    r = subprocess.run(
        [sys.executable, os.path.join(REPO, MONITOR)],
        input=_ptu_payload("ptu-sm", str(high)),
        capture_output=True, text=True, env=env, cwd=REPO, timeout=60)
    assert r.returncode == 0
    out = json.loads(r.stdout)
    msg = out["systemMessage"]
    assert msg.startswith("autocompactor:")
    assert "mid-burst" in msg
    assert "in context" in msg          # full readout is user-visible verbatim
    assert "compact advised ~" in msg
    # additionalContext is Claude-only awareness, no numbers -> no double
    ac = out["hookSpecificOutput"]["additionalContext"]
    assert "in context" not in ac


def test_posttooluse_watchdog_silent_below_hard(tmp_path):
    """Below the hard line the watchdog must be cheap AND quiet: no output,
    and no per-tool telemetry spam (no monitor_eval row written)."""
    low = tmp_path / "low.jsonl"
    low.write_text("".join(json.dumps(e) + "\n" for e in [
        _assistant(usage=_usage(50_000)),
        _tool_result("x"),
    ]))
    env = _hook_env(tmp_path)
    env["AUTOCOMPACTOR_WINDOW"] = "200000"  # hard line = 130k; 50k < it
    r = subprocess.run(
        [sys.executable, os.path.join(REPO, MONITOR)],
        input=_ptu_payload("ptu2", str(low)),
        capture_output=True, text=True, env=env, cwd=REPO, timeout=60)
    assert r.returncode == 0
    assert r.stdout == ""
    assert not (tmp_path / ".claude" / "autocompactor" / "stats"
                / "events.jsonl").exists()


def test_posttooluse_watchdog_defers_when_reinject_pending(tmp_path):
    """Right after a compaction (pending_reinject set) the watchdog must not
    speak on the first tool result — the next UserPromptSubmit owns the
    reinject."""
    high = tmp_path / "high2.jsonl"
    high.write_text("".join(json.dumps(e) + "\n" for e in [
        _assistant(usage=_usage(180_000)),
        _tool_result("x"),
    ]))
    state_dir = tmp_path / ".claude" / "autocompactor"
    state_dir.mkdir(parents=True)
    (state_dir / "ptu3.state.json").write_text(json.dumps(
        {"pending_reinject": True, "compaction_count": 1}))
    env = _hook_env(tmp_path)
    env["AUTOCOMPACTOR_WINDOW"] = "200000"
    r = subprocess.run(
        [sys.executable, os.path.join(REPO, MONITOR)],
        input=_ptu_payload("ptu3", str(high)),
        capture_output=True, text=True, env=env, cwd=REPO, timeout=60)
    assert r.returncode == 0
    assert r.stdout == ""   # quiet: reinject owns the next prompt


def test_userpromptsubmit_eval_tagged_hook_event(tmp_path):
    """WI-1 instrumentation: UserPromptSubmit monitor_eval rows must carry
    hook_event=UserPromptSubmit so coverage metrics can tell them apart from
    PostToolUse (previously this field was absent -> '?' in telemetry)."""
    high = tmp_path / "ups.jsonl"
    high.write_text("".join(json.dumps(e) + "\n" for e in [
        _assistant(usage=_usage(180_000)),
        _tool_result("x"),
    ]))
    env = _hook_env(tmp_path)
    env["AUTOCOMPACTOR_WINDOW"] = "200000"
    payload = json.dumps({
        "session_id": "ups1", "cwd": "/tmp", "transcript_path": str(high),
        "hook_event_name": "UserPromptSubmit", "prompt": "carry on"})
    r = subprocess.run(
        [sys.executable, os.path.join(REPO, MONITOR)],
        input=payload, capture_output=True, text=True,
        env=env, cwd=REPO, timeout=60)
    assert r.returncode == 0
    ev = json.loads((tmp_path / ".claude" / "autocompactor" / "stats"
                     / "events.jsonl").read_text().splitlines()[-1])
    assert ev["hook_event"] == "UserPromptSubmit"


def test_posttooluse_skip_log_gated(tmp_path):
    """WI-1 instrumentation: PostToolUse otherwise logs a monitor_eval ONLY on
    the recommend branch, so non-recommends are invisible and coverage can't be
    measured. With AUTOCOMPACTOR_LOG_WATCHDOG_SKIPS set, an at/above-soft
    non-recommend logs a cheap skip eval; off by default it stays silent."""
    # 120k: in the hermetic-env band (soft 110k, hard 130k) -> above soft so the
    # skip-log is eligible, below hard so PostToolUse does NOT recommend.
    mid = tmp_path / "mid.jsonl"
    mid.write_text("".join(json.dumps(e) + "\n" for e in [
        _assistant(usage=_usage(120_000)),
        _tool_result("x"),
    ]))
    statsf = (tmp_path / ".claude" / "autocompactor" / "stats" / "events.jsonl")

    # OFF (default): no watchdog_skip telemetry
    env = _hook_env(tmp_path)
    env["AUTOCOMPACTOR_WINDOW"] = "200000"
    r = subprocess.run(
        [sys.executable, os.path.join(REPO, MONITOR)],
        input=_ptu_payload("sk1", str(mid)),
        capture_output=True, text=True, env=env, cwd=REPO, timeout=60)
    assert r.returncode == 0
    assert (not statsf.exists()) or "watchdog_skip" not in statsf.read_text()

    # ON: a cheap skip eval is logged, tagged + recommended False
    env["AUTOCOMPACTOR_LOG_WATCHDOG_SKIPS"] = "1"
    r = subprocess.run(
        [sys.executable, os.path.join(REPO, MONITOR)],
        input=_ptu_payload("sk2", str(mid)),
        capture_output=True, text=True, env=env, cwd=REPO, timeout=60)
    assert r.returncode == 0
    ev = json.loads(statsf.read_text().splitlines()[-1])
    assert ev["watchdog_skip"] is True
    assert ev["hook_event"] == "PostToolUse"
    assert ev["recommended"] is False
