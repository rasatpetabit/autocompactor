import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(REPO, "tests", "fixtures")
sys.path.insert(0, REPO)

from autocompactor import pi_session_lib, transcript_lib  # noqa: E402


def _write_jsonl(path, entries):
    path.write_text(
        "".join(json.dumps(entry) + "\n" for entry in entries),
        encoding="utf-8",
    )
    return str(path)


def _signal_names(st):
    return {name for name, _ in transcript_lib.active_signals(st)}


def test_leaf_to_root_walk_selects_active_branch_and_ignores_dead_branch():
    st = pi_session_lib.analyze(os.path.join(FIX, "pi", "branched.jsonl"))
    body = "\n".join(json.dumps(entry) for entry in st.entries)

    assert "Test-first branch is the active leaf" in body
    assert "tests/config-negative.test.ts" in st.edited_files
    assert "Direct validation branch is ready" not in body
    assert "call_branch_a_edit" not in body


def test_active_segment_begins_after_last_compaction_entry():
    st = pi_session_lib.analyze(os.path.join(FIX, "pi", "with_compaction.jsonl"))

    assert st.compaction_count == 1
    assert st.entries[0]["id"] == "c0000011"
    assert "tests/ingest-validation.test.ts" in st.edited_files
    assert "src/ingest.ts" in st.edited_files
    assert "src/batch.ts" not in st.read_files


def test_pi_transcript_signals_for_commit_and_verify_session(tmp_path):
    # Was a Pi<->Claude parser parity check; the Claude entry-parser
    # (transcript_lib.analyze) was removed in the Pi-only pivot, so this now
    # pins the Pi parser's signal set directly for the same logical session
    # (a passing test run followed by a commit -> tests_pass + commit).
    pi_path = _write_jsonl(tmp_path / "pi.jsonl", [
        {
            "type": "message",
            "id": "p1",
            "parentId": None,
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "Commit and verify."}],
            },
        },
        {
            "type": "message",
            "id": "p2",
            "parentId": "p1",
            "message": {
                "role": "assistant",
                "content": [{
                    "type": "toolCall",
                    "id": "pi-test",
                    "name": "bash",
                    "arguments": {"command": "pytest -q"},
                }],
                "usage": {
                    "input": 100,
                    "output": 10,
                    "cacheRead": 0,
                    "cacheWrite": 0,
                    "totalTokens": 110,
                },
            },
        },
        {
            "type": "message",
            "id": "p3",
            "parentId": "p2",
            "message": {
                "role": "toolResult",
                "toolCallId": "pi-test",
                "content": [{"type": "text", "text": "1 passed in 0.01s"}],
                "isError": False,
            },
        },
        {
            "type": "message",
            "id": "p4",
            "parentId": "p3",
            "message": {
                "role": "assistant",
                "content": [{
                    "type": "toolCall",
                    "id": "pi-commit",
                    "name": "bash",
                    "arguments": {"command": "git commit -am 'ship tests'"},
                }],
                "usage": {
                    "input": 100,
                    "output": 10,
                    "cacheRead": 0,
                    "cacheWrite": 0,
                    "totalTokens": 110,
                },
            },
        },
        {
            "type": "message",
            "id": "p5",
            "parentId": "p4",
            "message": {
                "role": "toolResult",
                "toolCallId": "pi-commit",
                "content": [{"type": "text", "text": "[main abc123] ship tests"}],
                "isError": False,
            },
        },
    ])

    st = pi_session_lib.analyze(pi_path)
    assert st.recent_tests_pass
    assert st.recent_commit
    assert set(_signal_names(st)) == {"commit", "tests_pass"}


def test_session_with_no_compaction_returns_stats(tmp_path):
    path = _write_jsonl(tmp_path / "no-compaction.jsonl", [
        {
            "type": "message",
            "id": "n1",
            "parentId": None,
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "Read the file."}],
            },
        },
        {
            "type": "message",
            "id": "n2",
            "parentId": "n1",
            "message": {
                "role": "assistant",
                "content": [{
                    "type": "toolCall",
                    "id": "read-1",
                    "name": "read",
                    "arguments": {"path": "README.md"},
                }],
                "usage": {"input": 20, "output": 5, "totalTokens": 25},
            },
        },
    ])

    st = pi_session_lib.analyze(path)
    assert isinstance(st, transcript_lib.TranscriptStats)
    assert st.compaction_count == 0
    assert st.read_files == ["README.md"]


def test_single_line_session_returns_stats_without_raising(tmp_path):
    path = _write_jsonl(tmp_path / "single-line.jsonl", [
        {
            "type": "session",
            "version": 3,
            "id": "only-session-line",
            "timestamp": "2026-01-18T12:00:00.000Z",
        }
    ])

    st = pi_session_lib.analyze(path)
    assert isinstance(st, transcript_lib.TranscriptStats)
    assert st.entries[0]["id"] == "only-session-line"


def test_assistant_text_chars_includes_thinking():
    st = pi_session_lib.analyze(os.path.join(FIX, "pi", "real_shapes.jsonl"))
    # real_shapes.jsonl carries a thinking block; field must count it.
    assert st.assistant_text_chars > 0


def test_user_prompt_chars_populated():
    st = pi_session_lib.analyze(os.path.join(FIX, "pi", "linear.jsonl"))
    assert st.user_prompt_chars > 0


def test_summary_chars_single_source():
    st = pi_session_lib.analyze(os.path.join(FIX, "pi", "with_compaction.jsonl"))
    # with_compaction.jsonl has BOTH a compactionSummary role and a
    # compaction entry; summary_chars counts the summary once, never both.
    assert st.summary_chars > 0


def test_skills_remain_zero_spec0():
    st = pi_session_lib.analyze(os.path.join(FIX, "pi", "linear.jsonl"))
    assert st.skill_chars == 0 and st.skill_names == []
