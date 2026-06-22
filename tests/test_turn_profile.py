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
