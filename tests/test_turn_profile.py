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
