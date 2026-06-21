import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from autocompactor import artifacts, statedir, stats  # noqa: E402

DEFAULT_ART_DIR = os.path.expanduser("~/.autocompactor/pi/artifacts")
DEFAULT_STATS_DIR = os.path.expanduser("~/.autocompactor/pi/stats")


def _events(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_state_root_defaults_and_env_override(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTOCOMPACTOR_STATE_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    expected = str(tmp_path / ".autocompactor" / "pi")
    # Single namespace: state_root ignores any (legacy) harness argument.
    assert statedir.state_root() == expected
    assert statedir.state_root("pi") == expected
    assert statedir.state_root("anything") == expected

    override = tmp_path / "override-state"
    monkeypatch.setenv("AUTOCOMPACTOR_STATE_DIR", str(override))
    assert statedir.state_root() == str(override)
    assert statedir.state_root("pi") == str(override)


def test_import_time_constants_point_at_pi_state():
    assert artifacts.ART_DIR == DEFAULT_ART_DIR
    assert stats.STATS_DIR == DEFAULT_STATS_DIR


def test_log_event_routes_to_resolved_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOCOMPACTOR_STATE_DIR", str(tmp_path))

    # `harness` kwarg is accepted but inert; no `harness` field is written.
    stats.log_event({"type": "monitor_eval"})
    stats.log_event({"type": "monitor_eval"}, harness="pi")

    events_path = tmp_path / "stats" / "events.jsonl"
    rows = _events(events_path)
    assert len(rows) == 2
    assert all("harness" not in row for row in rows)
    assert all(row["type"] == "monitor_eval" for row in rows)
