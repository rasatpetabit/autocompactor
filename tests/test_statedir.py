import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from autocompactor import artifacts, statedir, stats  # noqa: E402

DEFAULT_ART_DIR = os.path.expanduser("~/.claude/autocompactor/artifacts")
DEFAULT_STATS_DIR = os.path.expanduser("~/.claude/autocompactor/stats")


def _events(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_state_root_harness_defaults_and_env_override(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTOCOMPACTOR_STATE_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert statedir.state_root("claude") == str(
        tmp_path / ".claude" / "autocompactor"
    )
    assert statedir.state_root("pi") == str(
        tmp_path / ".autocompactor" / "pi"
    )

    override = tmp_path / "override-state"
    monkeypatch.setenv("AUTOCOMPACTOR_STATE_DIR", str(override))
    assert statedir.state_root("claude") == str(override)
    assert statedir.state_root("pi") == str(override)


def test_import_time_constants_stay_at_claude_defaults():
    assert artifacts.ART_DIR == DEFAULT_ART_DIR
    assert stats.STATS_DIR == DEFAULT_STATS_DIR


def test_log_event_defaults_harness_and_routes_to_resolved_state_dir(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AUTOCOMPACTOR_STATE_DIR", str(tmp_path))

    stats.log_event({"type": "monitor_eval"})
    stats.log_event({"type": "monitor_eval"}, harness="pi")

    events_path = tmp_path / "stats" / "events.jsonl"
    rows = _events(events_path)
    assert [row["harness"] for row in rows] == ["claude", "pi"]
