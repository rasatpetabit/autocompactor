"""Waiting-state open_work extraction + resolve_next_step priority."""

from __future__ import annotations

import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(REPO, "tests", "fixtures")
sys.path.insert(0, os.path.join(REPO, "src"))
sys.path.insert(0, REPO)

from autocompactor import pi_session_lib, transcript_lib  # noqa: E402


WAITING_FIXTURE = os.path.join(FIX, "pi", "waiting_build_session.jsonl")


def _write_jsonl(path, entries):
    path.write_text(
        "".join(json.dumps(entry) + "\n" for entry in entries),
        encoding="utf-8",
    )
    return str(path)


def test_extract_open_work_from_text_requires_handle():
    bare = transcript_lib.extract_open_work_from_text(
        "I can poll later when it succeeds.")
    assert bare == []

    hits = transcript_lib.extract_open_work_from_text(
        "Y260717-114448 is running.\n"
        "Monitor:\n```bash\nyanos-builder show Y260717-114448\n```\n"
        "When it succeeds I'll fill rebuild-artifact.txt and run task 11.")
    kinds = {h["kind"] for h in hits}
    assert "waiting_monitor" in kinds
    wait = next(h for h in hits if h["kind"] == "waiting_monitor")
    assert "Y260717-114448" in wait.get("resource_ids", [])
    assert any("yanos-builder show" in c for c in wait.get("monitor_cmds", []))
    assert wait.get("next_on_success")


def test_extract_waiting_monitor_from_assistant_fixture():
    st = pi_session_lib.analyze(WAITING_FIXTURE)
    assert any(w.get("kind") == "waiting_monitor" for w in st.open_work)
    blob = json.dumps(st.open_work)
    assert "Y260717-114448" in blob
    assert "yanos-builder" in blob


def test_resolve_next_step_prefers_wait_over_stale_user_task():
    st = pi_session_lib.analyze(WAITING_FIXTURE)
    step, src = transcript_lib.resolve_next_step(st)
    assert src == "open_work:waiting_monitor"
    assert "Y260717-114448" in step
    assert "real session" not in step
    assert "iVBORw0KGgo" not in step
    assert "WAITING:" in step
    assert "On success:" in step


def test_last_user_task_strips_base64_and_ignores_status_ping(tmp_path):
    path = _write_jsonl(tmp_path / "hygiene.jsonl", [
        {
            "type": "message",
            "id": "h1",
            "parentId": None,
            "message": {
                "role": "user",
                "content": [{
                    "type": "text",
                    "text": (
                        "Please fix the grok headers.\n\n"
                        "iVBORw0KGgoAAAANSUhEUgAAAqcAAADXCAYAAAAnWPjs"
                        "AAAMTWlDQ1BJQ0MgUHJvZmlsZQAASImVVwdYU8kWnltS"
                        "IQQIREBK6E0QkRJASggt9I4gKiEJEEqMCUHFjiy7gmsX"
                        "EazoKkXR1RWQxYa6NhbF3hcLKsq6uC525U0IoMu8r35v"
                        "rnz33O/HPOuXPvnQGA3sWXSnNRTQDyJPmy2GB"
                    ),
                }],
            },
        },
        {
            "type": "message",
            "id": "h2",
            "parentId": "h1",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Working on it."}],
                "usage": {
                    "input": 100, "output": 5, "cacheRead": 0,
                    "cacheWrite": 0, "totalTokens": 105,
                },
            },
        },
        {
            "type": "message",
            "id": "h3",
            "parentId": "h2",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "status?"}],
            },
        },
        {
            "type": "message",
            "id": "h4",
            "parentId": "h3",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Still going."}],
                "usage": {
                    "input": 120, "output": 5, "cacheRead": 0,
                    "cacheWrite": 0, "totalTokens": 125,
                },
            },
        },
    ])
    st = pi_session_lib.analyze(path)
    assert "iVBORw0KGgo" not in st.last_user_task
    assert "fix the grok headers" in st.last_user_task
    assert st.last_user_task.strip().lower() != "status?"


def test_preservation_instructions_include_open_work():
    st = pi_session_lib.analyze(WAITING_FIXTURE)
    instr = transcript_lib.build_preservation_instructions(st)
    assert "OPEN WORK" in instr
    assert "Y260717-114448" in instr


def test_wait_beats_todo_when_both_present():
    """Wait mode always outranks coding progress/todos (progress-ledger A2)."""
    st = pi_session_lib.analyze(WAITING_FIXTURE)
    st.todos = [
        {"content": "finish packaging", "status": "pending"},
        {"content": "done item", "status": "completed"},
    ]
    step, src = transcript_lib.resolve_next_step(st)
    assert src == "open_work:waiting_monitor"
    assert "Y260717-114448" in step


def test_sanitize_user_task_text_unit():
    cleaned = transcript_lib.sanitize_user_task_text(
        "Fix auth.\n\niVBORw0KGgoAAAANSUhEUgAAAqcAAADXCAYAAAAnWPjs"
        "AAAMTWlDQ1BJQ0MgUHJvZmlsZQAASImVVwdYU8kWnltSIQQI"
        "REBK6E0QkRJASggt9I4gKiEJEEqMCUHFjiy7gmsXEazoKkXR"
    )
    assert "Fix auth." in cleaned
    assert "iVBORw0KGgo" not in cleaned
    assert transcript_lib.is_trivial_user_ping("status?")
    assert transcript_lib.is_trivial_user_ping("ok")
    assert not transcript_lib.is_trivial_user_ping("continue the rebuild")


def test_bridge_prepare_reinject_surfaces_wait_next_step(tmp_path):
    """End-to-end: prepare on waiting fixture → reinject carries wait brief."""
    import pathlib
    import subprocess
    bridge = pathlib.Path(REPO) / "src" / "pi_bridge.py"
    state_dir = tmp_path / "state"
    env = {k: v for k, v in os.environ.items() if not k.startswith("AUTOCOMPACTOR_")}
    env["AUTOCOMPACTOR_STATE_DIR"] = str(state_dir)
    prep = subprocess.run(
        [sys.executable, str(bridge), "prepare", "--session", WAITING_FIXTURE],
        capture_output=True, text=True, cwd=REPO, env=env,
    )
    assert prep.returncode == 0, prep.stderr
    reinj = subprocess.run(
        [sys.executable, str(bridge), "reinject", "--session", WAITING_FIXTURE],
        capture_output=True, text=True, cwd=REPO, env=env,
    )
    assert reinj.returncode == 0, reinj.stderr
    data = json.loads(reinj.stdout.strip())
    assert data.get("nextStepSource") == "open_work:waiting_monitor"
    assert data.get("nextStepWait") is True
    assert "Y260717-114448" in (data.get("nextStep") or "")
    assert data.get("nextStepWaitMode") in ("poll", "advisory", "off")
    assert int(data.get("waitPollS") or 0) == 60
    assert int(data.get("waitPollMax") or 0) == 20
    assert isinstance(data.get("openWork"), list) and data["openWork"]


# ---------------------------------------------------------------------------
# F1: terminal success invalidates waiting_monitor (optic-5c compact failure)
# ---------------------------------------------------------------------------

def test_extract_terminal_resource_ids_from_status_line():
    text = (
        "Build Y260728-170946\n"
        "  status  : succeeded\n"
        "  duration: 29m\n"
    )
    ids = transcript_lib.extract_terminal_resource_ids(text)
    assert "Y260728-170946" in ids

    still = transcript_lib.extract_terminal_resource_ids(
        "Build Y260728-170946\n  status  : running\n")
    assert "Y260728-170946" not in still

    failed = transcript_lib.extract_terminal_resource_ids(
        "Y260728-170946 status : failed (catalog)")
    assert "Y260728-170946" in failed


def test_invalidate_terminal_open_work_drops_waiting_monitor():
    wait = {
        "kind": "waiting_monitor",
        "summary": "Y260728-170946 is running",
        "monitor_cmds": ["yanos-builder show Y260728-170946"],
        "resource_ids": ["Y260728-170946"],
        "next_on_success": "flash the WIC",
        "confidence": "high",
    }
    other = {
        "kind": "next_on_success",
        "summary": "flash the WIC",
        "resource_ids": ["Y260728-170946"],
        "next_on_success": "flash the WIC",
        "confidence": "medium",
    }
    kept_wait = {
        "kind": "waiting_monitor",
        "summary": "Y260729-000001 still building",
        "monitor_cmds": ["yanos-builder show Y260729-000001"],
        "resource_ids": ["Y260729-000001"],
        "confidence": "high",
    }
    pruned = transcript_lib.invalidate_terminal_open_work(
        [wait, other, kept_wait], {"Y260728-170946"})
    kinds_ids = [(w.get("kind"), w.get("resource_ids")) for w in pruned]
    assert ("waiting_monitor", ["Y260728-170946"]) not in kinds_ids
    assert ("waiting_monitor", ["Y260729-000001"]) in kinds_ids
    assert ("next_on_success", ["Y260728-170946"]) in kinds_ids


def test_terminal_success_invalidates_waiting_monitor_in_transcript(tmp_path):
    """Wait language for build X, then later status : succeeded for X
    → no waiting_monitor; resolve_next_step is not open_work:waiting_monitor.
    """
    path = _write_jsonl(tmp_path / "terminal_success.jsonl", [
        {
            "type": "message",
            "id": "t1",
            "parentId": None,
            "message": {
                "role": "user",
                "content": [{
                    "type": "text",
                    "text": "Build a working optic-5c image and flash it.",
                }],
            },
        },
        {
            "type": "message",
            "id": "t2",
            "parentId": "t1",
            "message": {
                "role": "assistant",
                "content": [{
                    "type": "text",
                    "text": (
                        "Y260728-170946 is running via yanos-builder.\n"
                        "Monitor:\n```bash\n"
                        "yanos-builder show Y260728-170946\n```\n"
                        "When it succeeds I'll flash the WIC. Leaving the "
                        "build running; I can poll."
                    ),
                }],
                "usage": {
                    "input": 100, "output": 40, "cacheRead": 0,
                    "cacheWrite": 0, "totalTokens": 140,
                },
            },
        },
        {
            "type": "message",
            "id": "t3",
            "parentId": "t2",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Checking build status."},
                    {
                        "type": "toolCall",
                        "id": "bash_show",
                        "name": "bash",
                        "arguments": {
                            "command": "yanos-builder show Y260728-170946",
                        },
                    },
                ],
                "usage": {
                    "input": 120, "output": 20, "cacheRead": 0,
                    "cacheWrite": 0, "totalTokens": 140,
                },
            },
        },
        {
            "type": "message",
            "id": "t4",
            "parentId": "t3",
            "message": {
                "role": "toolResult",
                "toolCallId": "bash_show",
                "isError": False,
                "content": [{
                    "type": "text",
                    "text": (
                        "Build Y260728-170946\n"
                        "  status  : succeeded\n"
                        "  image  : /srv/builds/Y260728-170946/optic-5c.wic\n"
                        "Tasks Summary: all succeeded\n"
                        "catalog: succeeded\n"
                    ),
                }],
            },
        },
        {
            "type": "message",
            "id": "t5",
            "parentId": "t4",
            "message": {
                "role": "assistant",
                "content": [{
                    "type": "text",
                    "text": (
                        "Build Y260728-170946 succeeded. Flash path is "
                        "/srv/builds/Y260728-170946/optic-5c.wic."
                    ),
                }],
                "usage": {
                    "input": 140, "output": 20, "cacheRead": 0,
                    "cacheWrite": 0, "totalTokens": 160,
                },
            },
        },
    ])
    st = pi_session_lib.analyze(path)
    assert "Y260728-170946" in (st.terminal_resource_ids or set())
    assert not any(
        w.get("kind") == "waiting_monitor" for w in (st.open_work or [])
    ), st.open_work
    step, src = transcript_lib.resolve_next_step(st)
    assert src != "open_work:waiting_monitor", (step, src)
    assert "WAITING:" not in (step or "")
    instr = transcript_lib.build_preservation_instructions(st)
    assert "WAITING: Y260728-170946" not in instr


def test_resolve_next_step_skips_terminal_waiting_even_if_unpruned():
    """Belt: resolve_next_step ignores waiting_monitor for terminal ids
    even when open_work was not pruned (defensive)."""
    st = transcript_lib.TranscriptStats()
    st.open_work = [{
        "kind": "waiting_monitor",
        "summary": "Y260728-170946 is running",
        "monitor_cmds": ["yanos-builder show Y260728-170946"],
        "resource_ids": ["Y260728-170946"],
        "next_on_success": "flash the WIC",
        "confidence": "high",
    }]
    st.terminal_resource_ids = {"Y260728-170946"}
    st.last_user_task = "Build a working optic-5c image and flash it."
    step, src = transcript_lib.resolve_next_step(st)
    assert src != "open_work:waiting_monitor"
    assert "WAITING:" not in (step or "")
    # Falls through to last_user_task (or next_on_success if present).
    assert src in ("last_user_task", "open_work:next_on_success", "")
