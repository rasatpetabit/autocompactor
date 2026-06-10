import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(REPO, "tests", "fixtures")
sys.path.insert(0, REPO)

import pi_session_lib  # noqa: E402
import transcript_lib  # noqa: E402


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


def test_pi_and_claude_transcript_signal_parity_for_same_logical_session(tmp_path):
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

    claude_entries = [
        {
            "type": "user",
            "message": {"content": [{"type": "text", "text": "Commit and verify."}]},
        },
        {
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "id": "claude-test",
                    "name": "Bash",
                    "input": {"command": "pytest -q"},
                }],
                "usage": {
                    "input_tokens": 100,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 10,
                },
            },
        },
        {
            "type": "user",
            "message": {
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "claude-test",
                    "content": "1 passed in 0.01s",
                    "is_error": False,
                }]
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "id": "claude-commit",
                    "name": "Bash",
                    "input": {"command": "git commit -am 'ship tests'"},
                }],
                "usage": {
                    "input_tokens": 100,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 10,
                },
            },
        },
        {
            "type": "user",
            "message": {
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "claude-commit",
                    "content": "[main abc123] ship tests",
                    "is_error": False,
                }]
            },
        },
    ]

    assert _signal_names(pi_session_lib.analyze(pi_path)) == _signal_names(
        transcript_lib.analyze(entries=claude_entries)
    )


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
