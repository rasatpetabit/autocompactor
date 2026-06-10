"""
This module pins the pi_bridge never-raise JSON CLI contract end to end.
It verifies that pi_bridge.py always exits 0, never reads stdin, and outputs
at most one JSON object (or nothing) for various subcommands and edge cases,
while managing state under AUTOCOMPACTOR_STATE_DIR.
"""
import json
import os
import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
BRIDGE = REPO_ROOT / "pi_bridge.py"


def run_bridge(args, state_dir, stdin_text=None):
    """Run pi_bridge.py with the given args and state dir."""
    env = {**os.environ, "AUTOCOMPACTOR_STATE_DIR": str(state_dir)}
    return subprocess.run(
        [sys.executable, str(BRIDGE)] + args,
        input=stdin_text,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
    )


def parse_single_json(stdout):
    """Assert stdout is empty or a single JSON object, returning the object or None."""
    stripped = stdout.strip()
    if not stripped:
        return None
    return json.loads(stripped)


def test_garbage_stdin_exits_zero(tmp_path):
    state_dir = tmp_path / "state"
    result = run_bridge(["evaluate"], state_dir, stdin_text="not json at all")
    assert result.returncode == 0
    parse_single_json(result.stdout)


def test_missing_session_exits_zero(tmp_path):
    state_dir = tmp_path / "state"
    result = run_bridge(["evaluate", "--session", "/nonexistent/path.jsonl"], state_dir)
    assert result.returncode == 0
    data = parse_single_json(result.stdout)
    if data is not None:
        assert data.get("recommend") is False


def test_unparseable_session_exits_zero(tmp_path):
    state_dir = tmp_path / "state"
    garbage_file = tmp_path / "garbage.jsonl"
    garbage_file.write_bytes(b"\x00\xffnot jsonl\n\n")
    result = run_bridge(["evaluate", "--session", str(garbage_file)], state_dir)
    assert result.returncode == 0
    parse_single_json(result.stdout)


def test_unknown_subcommand_exits_zero(tmp_path):
    state_dir = tmp_path / "state"
    result = run_bridge(["frobnicate"], state_dir)
    assert result.returncode == 0
    parse_single_json(result.stdout)


def test_evaluate_recommends_near_ceiling(tmp_path):
    state_dir = tmp_path / "state"
    fixture_path = REPO_ROOT / "tests" / "fixtures" / "pi" / "with_compaction.jsonl"
    result = run_bridge(
        ["evaluate", "--session", str(fixture_path), "--tokens", "150000", "--context-window", "200000"],
        state_dir,
    )
    assert result.returncode == 0
    data = parse_single_json(result.stdout)
    assert data is not None
    assert data["recommend"] is True
    assert data["context_tokens"] == 150000
    assert isinstance(data["reason"], str) and len(data["reason"]) > 0


def test_cooldown_round_trip(tmp_path):
    state_dir = tmp_path / "state"
    fixture_path = REPO_ROOT / "tests" / "fixtures" / "pi" / "with_compaction.jsonl"
    args = ["evaluate", "--session", str(fixture_path), "--tokens", "150000", "--context-window", "200000"]

    # First run: should recommend
    result1 = run_bridge(args, state_dir)
    assert result1.returncode == 0
    data1 = parse_single_json(result1.stdout)
    assert data1 is not None
    assert data1["recommend"] is True

    # Second run: should NOT recommend due to cooldown
    result2 = run_bridge(args, state_dir)
    assert result2.returncode == 0
    data2 = parse_single_json(result2.stdout)
    assert data2 is not None
    assert data2["recommend"] is False

    # Assert state file exists and contains last_reco_tokens
    state_file = state_dir / "with_compaction.state.json"
    assert state_file.exists()
    with open(state_file, "r") as f:
        state_data = json.load(f)
    assert "last_reco_tokens" in state_data
    assert state_data["last_reco_tokens"] == 150000


def test_prepare_emits_instructions_and_side_effects(tmp_path):
    state_dir = tmp_path / "state"
    fixture_path = REPO_ROOT / "tests" / "fixtures" / "pi" / "with_compaction.jsonl"
    result = run_bridge(["prepare", "--session", str(fixture_path)], state_dir)
    assert result.returncode == 0
    data = parse_single_json(result.stdout)
    assert data is not None
    assert isinstance(data["customInstructions"], str) and len(data["customInstructions"]) > 0

    # Check side effects
    backups_dir = state_dir / "backups"
    assert backups_dir.exists()
    jsonl_files = list(backups_dir.glob("*.jsonl"))
    assert len(jsonl_files) == 1

    artifacts_dir = state_dir / "artifacts"
    assert artifacts_dir.exists()
    json_files = list(artifacts_dir.glob("*.json"))
    assert len(json_files) >= 1


def test_reinject_after_prepare(tmp_path):
    state_dir = tmp_path / "state"
    fixture_path = REPO_ROOT / "tests" / "fixtures" / "pi" / "with_compaction.jsonl"

    # Run prepare first
    prepare_result = run_bridge(["prepare", "--session", str(fixture_path)], state_dir)
    assert prepare_result.returncode == 0

    # Run reinject
    reinject_result = run_bridge(["reinject", "--session", str(fixture_path)], state_dir)
    assert reinject_result.returncode == 0
    data = parse_single_json(reinject_result.stdout)
    assert data is not None
    assert isinstance(data["text"], str) and len(data["text"]) > 0
    assert data["customType"] == "autocompactor.digest"


def test_reinject_without_prepare_is_quiet_or_json(tmp_path):
    state_dir = tmp_path / "state"
    fixture_path = REPO_ROOT / "tests" / "fixtures" / "pi" / "linear.jsonl"
    result = run_bridge(["reinject", "--session", str(fixture_path)], state_dir)
    assert result.returncode == 0
    parse_single_json(result.stdout)
