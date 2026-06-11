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

import analyze_corpus  # noqa: E402
import artifacts  # noqa: E402
import precompact_analyzer as pa  # noqa: E402
import transcript_lib as tl  # noqa: E402


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


# ------------------------------------------------------------ hook contract

MONITOR = "context_monitor.py"
ANALYZER = "precompact_analyzer.py"


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
    assert "Good moment to compact" in r.stdout  # clamped to 200k, not mute


def test_monitor_recommends_on_rich_fixture(tmp_path):
    payload = json.dumps({
        "session_id": "py", "cwd": "/tmp",
        "transcript_path": os.path.join(FIX, "rich_transcript.jsonl"),
        "hook_event_name": "UserPromptSubmit",
        "prompt": "now plan the website redesign"})
    r = _run_hook(MONITOR, payload, tmp_path)
    assert r.returncode == 0
    assert "Good moment to compact" in r.stdout


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
    env["AUTOCOMPACTOR_POST_FLOOR"] = "70000"   # fixture ctx ~84k ->
    env["AUTOCOMPACTOR_MIN_SAVINGS"] = "30000"  # est. reclaim ~14k < 30k
    r = subprocess.run(
        [sys.executable, os.path.join(REPO, MONITOR)],
        input=payload, capture_output=True, text=True, env=env,
        cwd=REPO, timeout=60)
    assert r.returncode == 0
    assert "Good moment to compact" not in r.stdout
    ev = json.loads((tmp_path / ".claude" / "autocompactor" / "stats"
                     / "events.jsonl").read_text().splitlines()[-1])
    assert ev["recommended"] is False
    assert 0 < ev["est_reclaim"] < 30_000


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
    assert "Good moment to compact" in r.stdout       # gates on todo_step etc.
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


def _isolate_config(monkeypatch):
    """Hermetic in-process config: ignore repo config.json/config.local.json."""
    import config_lib
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


def test_analyzer_systemmessage_summary(tmp_path):
    """Every compaction (manual or auto) must surface a quick analysis
    summary to the user via systemMessage — trigger, context, phase,
    artifact accounting, instruction source. Content-free: no transcript
    text beyond signal descriptions."""
    payload = json.dumps({
        "session_id": "sum1", "cwd": "/tmp",
        "transcript_path": os.path.join(FIX, "rich_transcript.jsonl"),
        "hook_event_name": "PreCompact", "trigger": "auto",
        "custom_instructions": "user note"})
    r = _run_hook(ANALYZER, payload, tmp_path)
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert "hookSpecificOutput" in out          # instructions still emitted
    msg = out["systemMessage"]
    assert msg.startswith("autocompactor: compaction #1 (auto)")
    assert "context ~" in msg
    assert "phase: " in msg
    assert "artifacts to disk: " in msg
    assert "instructions: fresh analysis" in msg
    assert "user notes kept" in msg


def test_analyzer_summary_reports_staged_instructions(tmp_path):
    """When the monitor staged instructions moments earlier, the summary
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
    out = json.loads(r.stdout)
    assert "compaction #3 (manual)" in out["systemMessage"]
    assert "instructions: staged by monitor" in out["systemMessage"]


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
