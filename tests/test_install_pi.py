"""
Task 10 (context-window-analysis): install_pi --status surfaces floor-probe
freshness/staleness as a read-only consumer of floor-probe.json (the frozen T9
artifact). The line reports fresh/stale/missing against the artifact's
staleness_budget; it never flips the --status exit code (informational only).
"""
import datetime
import json
import os
import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from autocompactor import install_pi  # noqa: E402


def _write_probe(state_dir, *, measured_at, budget=86400,
                  per_package=None):
    os.makedirs(state_dir, exist_ok=True)
    probe = {"per_package": per_package or {}, "measured_at": measured_at,
             "pi_version": "test", "staleness_budget": budget}
    with open(os.path.join(state_dir, "floor-probe.json"), "w") as fh:
        json.dump(probe, fh)


def _status_output(state_dir):
    env = dict(os.environ, AUTOCOMPACTOR_STATE_DIR=str(state_dir),
               PYTHONPATH=str(REPO_ROOT / "src"))
    proc = subprocess.run(
        [sys.executable, "src/autocompactor/install_pi.py", "--status"],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT))
    return proc.returncode, proc.stdout + proc.stderr


def test_status_renders_missing_when_no_probe(tmp_path):
    rc, out = _status_output(tmp_path)
    assert "floor probe" in out
    assert "missing" in out


def test_status_renders_fresh_when_probe_recent(tmp_path):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    _write_probe(tmp_path, measured_at=now, budget=86400)
    rc, out = _status_output(tmp_path)
    assert "floor probe: fresh" in out


def test_status_renders_stale_when_probe_aged(tmp_path):
    old = (datetime.datetime.now(datetime.timezone.utc)
           - datetime.timedelta(days=30)).isoformat()
    _write_probe(tmp_path, measured_at=old, budget=86400)
    rc, out = _status_output(tmp_path)
    assert "floor probe: stale" in out


def test_floor_probe_status_line_is_read_only_and_never_raises(tmp_path):
    """The helper must never raise and must not write anything."""
    before = set(os.listdir(tmp_path)) if os.path.isdir(tmp_path) else set()
    line = install_pi._floor_probe_status_line(str(tmp_path))
    assert isinstance(line, str)
    assert "floor probe" in line
    after = set(os.listdir(tmp_path)) if os.path.isdir(tmp_path) else set()
    assert before == after  # read-only; no files created


def test_floor_probe_status_line_missing_on_unreadable_dir(tmp_path):
    line = install_pi._floor_probe_status_line(str(tmp_path / "nope"))
    assert "missing" in line or "unavailable" in line