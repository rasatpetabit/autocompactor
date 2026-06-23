import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(REPO, "tests", "fixtures")
sys.path.insert(0, os.path.join(REPO, "src"))

from autocompactor import pi_session_lib, transcript_lib  # noqa: E402


def _write_jsonl(path, entries):
    path.write_text(
        "".join(json.dumps(entry) + "\n" for entry in entries),
        encoding="utf-8",
    )
    return str(path)


def test_active_path_matches_analyze_segment(tmp_path):
    """active_path() returns the same active segment analyze() walks."""
    path = _write_jsonl(tmp_path / "ap.jsonl", [
        {"type": "session", "id": "s0", "timestamp": "2026-01-01T00:00:00.000Z"},
        {"type": "message", "id": "u1", "parentId": None,
         "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}},
        {"type": "message", "id": "a1", "parentId": "u1",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "ok"}],
                     "usage": {"input": 10, "cacheRead": 0, "cacheWrite": 0,
                               "output": 2, "totalTokens": 12}}},
    ])
    full, active, compaction_count = pi_session_lib.active_path(path)
    st = pi_session_lib.analyze(path)
    assert [e.get("id") for e in active] == [e.get("id") for e in st.entries]
    assert compaction_count == st.compaction_count
    assert len(full) >= len(active)


def test_analyze_active_prefix_equivalent_to_analyze(tmp_path):
    """analyze_active_prefix on the full active segment == analyze()."""
    path = _write_jsonl(tmp_path / "eq.jsonl", [
        {"type": "session", "id": "s0", "timestamp": "2026-01-01T00:00:00.000Z"},
        {"type": "message", "id": "u1", "parentId": None,
         "message": {"role": "user", "content": [{"type": "text", "text": "do thing"}]}},
        {"type": "message", "id": "a1", "parentId": "u1",
         "message": {"role": "assistant",
                     "content": [{"type": "thinking", "thinking": "plan"},
                                 {"type": "toolCall", "id": "c1", "name": "read",
                                  "arguments": {"path": "README.md"}}],
                     "usage": {"input": 100, "cacheRead": 0, "cacheWrite": 0,
                               "output": 10, "totalTokens": 110}}},
        {"type": "message", "id": "t1", "parentId": "a1",
         "message": {"role": "toolResult", "toolCallId": "c1", "isError": False,
                     "content": [{"type": "text", "text": "file body"}]}},
        {"type": "message", "id": "a2", "parentId": "t1",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "done"}],
                     "usage": {"input": 50, "cacheRead": 110, "cacheWrite": 0,
                               "output": 5, "totalTokens": 165}}},
    ])
    full, active, compaction_count = pi_session_lib.active_path(path)
    st_full = pi_session_lib.analyze_active_prefix(
        full, active, recent_window=30, compaction_count=compaction_count)
    st_analyze = pi_session_lib.analyze(path)
    assert st_full.context_tokens == st_analyze.context_tokens
    assert st_full.user_prompt_chars == st_analyze.user_prompt_chars
    assert st_full.assistant_text_chars == st_analyze.assistant_text_chars
    assert st_full.total_tool_chars == st_analyze.total_tool_chars
    assert st_full.edited_files == st_analyze.edited_files
    assert st_full.compaction_count == st_analyze.compaction_count


from autocompactor import turn_profile  # noqa: E402
from autocompactor import policy  # noqa: E402


def test_turn_attribution_exact_occupancy_and_pre_call(tmp_path):
    path = _write_jsonl(tmp_path / "at.jsonl", [
        {"type": "session", "id": "s0", "timestamp": "2026-01-01T00:00:00.000Z"},
        {"type": "message", "id": "u1", "parentId": None,
         "message": {"role": "user", "content": [{"type": "text", "text": "x"}]}},
        {"type": "message", "id": "a1", "parentId": "u1",
         "message": {"role": "assistant",
                     "content": [{"type": "toolCall", "id": "c1", "name": "read",
                                  "arguments": {"path": "f.py"}}],
                     "usage": {"input": 100, "cacheRead": 0, "cacheWrite": 0,
                               "output": 10, "totalTokens": 110,
                               "cost": {"total": 0.01}}}},
        {"type": "message", "id": "t1", "parentId": "a1",
         "message": {"role": "toolResult", "toolCallId": "c1", "isError": False,
                     "content": [{"type": "text", "text": "abcd" * 100}]}},
        {"type": "message", "id": "a2", "parentId": "t1",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "ok"}],
                     "usage": {"input": 60, "cacheRead": 110, "cacheWrite": 0,
                               "output": 5, "totalTokens": 175,
                               "cost": {"total": 0.02}}}},
    ])
    res = turn_profile.profile_turns(path)
    assert len(res.turns) == 2
    t0, t1 = res.turns
    # occupancy == totalTokens (autocompactor-consistent), exact
    assert t0.occupancy == 110 and t1.occupancy == 175
    # pre_call == input + cacheRead + cacheWrite, exact
    assert t0.pre_call_tokens == 100 and t1.pre_call_tokens == 170
    # delta since previous call
    assert t0.delta_occupancy == 0 and t1.delta_occupancy == 175 - 110
    # cache hit ratio of pre_call
    assert t0.cache_hit_ratio == 0.0
    assert t1.cache_hit_ratio == 110 / 170
    # tools emitted by t0 feed t1; t0 emits read
    assert t0.tools_called == ["read"] and t1.tools_called == []
    # fed-by for t1 includes the tool result (interval a1..a2 exclusive of a2)
    assert t1.fed_by_tokens > 0
    assert any(fb["tool"] == "read" for fb in t1.fed_by)


def test_flags_parallel_and_error_and_think_bloat(tmp_path):
    res = turn_profile.profile_turns(os.path.join(FIX, "pi", "parallel_tools.jsonl"))
    t0 = res.turns[0]
    assert "parallel-tools" in t0.flags          # two toolCalls in one turn
    t1 = res.turns[1]
    assert t1.fed_by_tokens > 0


def test_flag_redundant_read(tmp_path):
    entries = [{"type": "session", "id": "s0", "timestamp": "2026-01-01T00:00:00.000Z"},
               {"type": "message", "id": "u1", "parentId": None,
                "message": {"role": "user", "content": [{"type": "text", "text": "go"}]}}]
    for n in range(3):
        a = {"type": "message", "id": f"a{n}", "parentId": entries[-1]["id"],
             "message": {"role": "assistant",
                         "content": [{"type": "toolCall", "id": f"c{n}",
                                      "name": "read", "arguments": {"path": "same.py"}}],
                         "usage": {"input": 10, "cacheRead": 0, "cacheWrite": 0,
                                   "output": 1, "totalTokens": 11}}}
        t = {"type": "message", "id": f"t{n}", "parentId": f"a{n}",
             "message": {"role": "toolResult", "toolCallId": f"c{n}",
                         "isError": False, "content": [{"type": "text", "text": "x"}]}}
        entries += [a, t]
    path = _write_jsonl(tmp_path / "rr.jsonl", entries)
    res = turn_profile.profile_turns(path)
    assert "redundant-read" in res.turns[1].flags
    assert "redundant-read" in res.turns[2].flags


def test_summary_aggregates(tmp_path):
    res = turn_profile.profile_turns(os.path.join(FIX, "pi", "parallel_tools.jsonl"))
    s = turn_profile.summarize(res, post_floor=70000)
    assert s.turn_count == 2
    assert s.has_usage is True
    assert s.peak_ctx == 284 and s.peak_turn_index == 1
    assert s.start_ctx == 220 and s.final_ctx == 284
    assert abs(s.total_cost - (0.016 + 0.0152)) < 1e-9
    assert s.tool_frequency["read"] == 2
    assert s.reclaimable_tokens == max(284 - 70000, 0)   # below floor -> 0
    assert s.sparkline                  # non-empty
    names = [r["tool"] for r in s.per_tool_result_tokens]
    assert "read" in names
    # review P2#4: no blended per-tool cost, only result-token share
    assert all("share" in r and "result_tokens" in r
               for r in s.per_tool_result_tokens)


def test_degenerate_no_usage(tmp_path):
    res = turn_profile.profile_turns(os.path.join(FIX, "pi", "no_usage.jsonl"))
    turn_profile.summarize(res, full_path=None)
    s = res.summary
    assert s.has_usage is False
    assert s.peak_turn_index is None
    assert s.biggest_growth_turn is None
    assert "no usage blocks found" in " ".join(s.warnings)
    assert res.turns and all(not t.has_usage for t in res.turns)


def test_empty_active_segment(tmp_path):
    path = _write_jsonl(tmp_path / "empty.jsonl", [
        {"type": "session", "id": "e0", "timestamp": "2026-01-01T00:00:00.000Z"}])
    res = turn_profile.profile_turns(path)
    turn_profile.summarize(res, full_path=None)
    assert res.turns == []
    assert res.summary.turn_count == 0
    assert res.summary.warnings


def test_text_report_uses_si_formatting_and_reuses_policy_fmt(tmp_path):
    res = turn_profile.profile_turns(os.path.join(FIX, "pi", "parallel_tools.jsonl"))
    turn_profile.summarize(res, full_path=None)
    txt = turn_profile.format_text(res)
    # line 1 carries SI occupancy (reuse policy._fmt_tokens)
    assert policy._fmt_tokens(220) in txt
    assert "T  0" in txt or "T   0" in txt or "T 0" in txt
    # fed-by line present for turns with fed content
    assert "fed by" in txt
    # review P2#4: result-token table, not per-tool cost
    assert "result-tok" in txt and "est-cost" not in txt


def test_rollup_collapses_under_user_prompts(tmp_path):
    res = turn_profile.profile_turns(os.path.join(FIX, "pi", "parallel_tools.jsonl"))
    roll = turn_profile.rollup(res)
    assert len(roll) == 1              # one user prompt -> one human turn
    assert roll[0].loop_len == 2


def test_main_json_never_raises_on_missing_session(capsys):
    rc = turn_profile.main(["--session", "/nonexistent/path.jsonl", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    import json as _json
    obj = _json.loads(out)              # valid JSON even on failure
    assert obj["summary"]["warnings"]


def test_main_text_and_json_flags(tmp_path, capsys):
    path = _write_jsonl(tmp_path / "cli.jsonl", [
        {"type": "session", "id": "s0", "timestamp": "2026-01-01T00:00:00.000Z"},
        {"type": "message", "id": "u1", "parentId": None,
         "message": {"role": "user", "content": [{"type": "text", "text": "x"}]}},
        {"type": "message", "id": "a1", "parentId": "u1",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "y"}],
                     "usage": {"input": 10, "cacheRead": 0, "cacheWrite": 0,
                               "output": 1, "totalTokens": 11,
                               "cost": {"total": 0.001}}}}])
    rc = turn_profile.main(["--session", path])
    assert rc == 0 and "SESSION PROFILE" in capsys.readouterr().out
    rc = turn_profile.main(["--session", path, "--json"])
    import json as _json
    assert rc == 0
    assert _json.loads(capsys.readouterr().out)["turns"]


def test_mixed_usage_bounds_use_usage_bearing_turns(tmp_path):
    """start_ctx/final_ctx bound to usage-bearing turns, not no-usage head/tail."""
    path = _write_jsonl(tmp_path / "mixed.jsonl", [
        {"type": "session", "id": "s0", "timestamp": "2026-01-01T00:00:00.000Z"},
        {"type": "message", "id": "u1", "parentId": None,
         "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}},
        {"type": "message", "id": "a1", "parentId": "u1",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "no usage"}]}},
        {"type": "message", "id": "a2", "parentId": "a1",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "usage"}],
                     "usage": {"input": 100, "cacheRead": 0, "cacheWrite": 0,
                               "output": 10, "totalTokens": 110,
                               "cost": {"total": 1}}}},
        {"type": "message", "id": "a3", "parentId": "a2",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "tail no usage"}]}}])
    res = turn_profile.profile_turns(path)
    s = turn_profile.summarize(res)
    assert s.has_usage is True
    assert s.start_ctx == 110 and s.final_ctx == 110   # NOT 0
    assert s.peak_ctx == 110


def test_repeated_parallel_tool_args_preserved(tmp_path):
    """parallel reads with different paths keep ALL args (no overwrite)."""
    path = _write_jsonl(tmp_path / "par.jsonl", [
        {"type": "session", "id": "s0", "timestamp": "2026-01-01T00:00:00.000Z"},
        {"type": "message", "id": "u1", "parentId": None,
         "message": {"role": "user", "content": [{"type": "text", "text": "go"}]}},
        {"type": "message", "id": "a1", "parentId": "u1",
         "message": {"role": "assistant",
                     "content": [{"type": "toolCall", "id": "c1", "name": "read",
                                  "arguments": {"path": "a.py"}},
                                 {"type": "toolCall", "id": "c2", "name": "read",
                                  "arguments": {"path": "b.py"}}],
                     "usage": {"input": 50, "cacheRead": 0, "cacheWrite": 0,
                               "output": 2, "totalTokens": 52}}},
        {"type": "message", "id": "t1", "parentId": "a1",
         "message": {"role": "toolResult", "toolCallId": "c1", "isError": False,
                     "content": [{"type": "text", "text": "a"}]}},
        {"type": "message", "id": "t2", "parentId": "a1",
         "message": {"role": "toolResult", "toolCallId": "c2", "isError": False,
                     "content": [{"type": "text", "text": "b"}]}}])
    res = turn_profile.profile_turns(path)
    t0 = res.turns[0]
    paths = sorted(ca["arguments"].get("path") for ca in t0.tool_call_args)
    assert paths == ["a.py", "b.py"]   # both preserved, not overwritten


def test_unpriced_provider_flagged_not_shown_as_zero(tmp_path):
    """An unpriced provider (cost.total==0 but tokens>0) must not read as $0.00."""
    path = _write_jsonl(tmp_path / "unpriced.jsonl", [
        {"type": "session", "id": "s0", "timestamp": "2026-01-01T00:00:00.000Z"},
        {"type": "message", "id": "u1", "parentId": None,
         "message": {"role": "user", "content": [{"type": "text", "text": "go"}]}},
        {"type": "message", "id": "a1", "parentId": "u1",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "y"}],
                     "usage": {"input": 1000, "cacheRead": 0, "cacheWrite": 0,
                               "output": 50, "totalTokens": 1050,
                               "cost": {"input": 0, "output": 0, "cacheRead": 0,
                                        "cacheWrite": 0, "total": 0}}}}])
    res = turn_profile.profile_turns(path)
    turn_profile.summarize(res)
    t0 = res.turns[0]
    assert t0.cost == 0.0 and t0.cost_known is False   # detected, not trusted
    assert res.summary.unpriced_turn_count == 1
    assert res.summary.total_cost == 0.0                # excluded from known sum
    txt = turn_profile.format_text(res)
    assert "$—" in txt and "$0.00" not in txt           # shows $—, not $0.00
    assert "unpriced" in txt
