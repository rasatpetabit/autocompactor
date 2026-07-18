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
