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
