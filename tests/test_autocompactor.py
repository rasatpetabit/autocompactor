"""Pytest suite for autocompactor (open item #8 — maturity-gap closure).

Covers the harness-agnostic unit surface: transcript_lib / artifacts /
policy / llm_digest. The Claude-hook contract tests (context_monitor /
precompact_analyzer subprocess behavior) were removed in the Pi-only pivot
(Task 3) along with those modules; the Claude backtester (analyze_corpus) and
its tests were removed in Task 4. This file now holds only the shared-brain
tests, re-fixtured for Pi (TranscriptStats sourced from
pi_session_lib.analyze(tests/fixtures/pi/*.jsonl)).

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

from autocompactor import artifacts, policy  # noqa: E402
from autocompactor import llm_digest as ld, transcript_lib as tl  # noqa: E402
from autocompactor import pi_session_lib as psl  # noqa: E402

PI_FIX = os.path.join(REPO, "tests", "fixtures", "pi")


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


# ------------------------------------------------------ transcript parsing


def test_analyze_rich_fixture_core_fields():
    # Re-fixtured onto the Pi parser (Task 4); the `st.todos` sub-assertion was
    # dropped — Pi never populates todos (todo signals removed). The Pi
    # with_compaction fixture carries a Bash working-command and a concluded
    # error-then-clean debug loop so the remaining field coverage survives.
    st = psl.analyze(os.path.join(PI_FIX, "with_compaction.jsonl"))
    assert st.context_tokens > 0
    assert st.working_commands  # successful Bash commands recorded
    assert st.recent_error_then_clean  # fixture encodes a concluded debug loop


# ------------------------------------------------- task-tool state tracking


def test_todowrite_shape_still_supported():
    entries = [_assistant("TodoWrite", {"todos": [
        {"content": "a", "status": "completed"},
        {"content": "b", "status": "pending"}]})]
    st = tl.analyze(entries=entries)
    assert st.todo_step and not st.todos_all_done


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
    st = psl.analyze(os.path.join(PI_FIX, "with_compaction.jsonl"))
    out = tl.build_preservation_instructions(st, cwd="/tmp/x")
    assert "structured handoff" in out
    assert "Session-specific anchors" in out
    assert "/tmp/x" in out


# ---------------------------------------------------------------- artifacts


def test_artifact_roundtrip_and_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    st = psl.analyze(os.path.join(PI_FIX, "with_compaction.jsonl"))
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


# ------------------------------------------------------- compaction events


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
    monkeypatch.setattr(ld.subprocess, "run", fake_run)
    assert ld.llm_digest(str(t)) == "- keep task"
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
    monkeypatch.setattr(ld.urllib.request, "urlopen", fake_urlopen)
    assert ld.llm_digest(str(t)) == "- local model fact"
    assert seen["url"] == "http://local.test:8000/v1/chat/completions"
    assert seen["body"]["model"] == "local-test-model"


def test_public_config_does_not_pin_site_local_llm_defaults():
    cfg_text = open(os.path.join(REPO, "config.json"), encoding="utf-8").read().lower()
    assert "llm_model" not in cfg_text
    assert "llm_base_url" not in cfg_text
    assert "192.168." not in cfg_text


# ---------------------------------- single-sample spike guard (shared policy)


def test_is_ctx_spike_detects_transient_jump():
    assert policy.is_ctx_spike(303_000, 114_000, 200_000) is True   # the report
    assert policy.is_ctx_spike(186_000, 184_000, 200_000) is False  # steady growth
    assert policy.is_ctx_spike(160_000, 0, 200_000) is False        # first eval
    assert policy.is_ctx_spike(303_000, 303_000, 200_000) is False  # corroborated


def test_burst_milestone_fills_danger_band():
    """Every occupancy rung between the hard line and the window is reachable —
    no rung-to-rung gap leaves a silent zone (the old 100k-step bug)."""
    cfg = policy.resolve_policy_config("claude", 200_000)
    _, hard = policy.advisory_band(cfg)
    reached = {policy.burst_milestone(cfg, c)
               for c in range(hard, 200_001, 2_000)}
    for pct in (0.70, 0.80, 0.90, 0.97):
        assert int(pct * 200_000) in reached
    # below the soft line -> no milestone
    assert policy.burst_milestone(cfg, 10_000) == 0
