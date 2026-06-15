import json
import os
import sys
import datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import nightly_eval  # noqa: E402


def test_nightly_passes_live_config_to_backtester(tmp_path, monkeypatch):
    base = tmp_path / "state"
    reports = base / "reports"
    monkeypatch.setattr(nightly_eval, "BASE", str(base))
    monkeypatch.setattr(nightly_eval, "REPORTS", str(reports))
    monkeypatch.setattr(nightly_eval, "HISTORY", str(reports / "nightly_history.jsonl"))
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "300000"}
    }))
    monkeypatch.setattr(nightly_eval, "SETTINGS", str(settings))
    monkeypatch.setattr(nightly_eval.glob, "glob", lambda pattern: [])

    seen = {}

    def fake_run(cmd, timeout=1800):
        if cmd[:3] == [sys.executable, "-m", "pytest"]:
            return 0, "pytest ok"
        if cmd[:2] == ["bash", "tests/smoke_test.sh"]:
            return 0, "smoke ok"
        if cmd[-1:] == ["--version"]:
            return 0, "Claude Code test\n"
        if len(cmd) >= 2 and cmd[1] == "analyze_corpus.py":
            seen["backtest_cmd"] = cmd
            report = cmd[cmd.index("--json") + 1]
            with open(report, "w", encoding="utf-8") as fh:
                json.dump({"sessions": []}, fh)
            return 0, "No analyzable sessions."
        return 0, ""

    monkeypatch.setattr(nightly_eval, "run", fake_run)

    assert nightly_eval.main() == 0
    cmd = seen["backtest_cmd"]
    assert cmd[cmd.index("--window") + 1] == "300000.0"
    assert cmd[cmd.index("--soft") + 1] == "0.5"
    assert cmd[cmd.index("--hard") + 1] == "0.62"
    assert cmd[cmd.index("--stale-frac") + 1] == "0.9"
    record = json.loads((reports / "nightly_history.jsonl").read_text().splitlines()[-1])
    assert record["hard_tokens"] == 186000.0


def test_nightly_reports_learned_tiers_and_native_cap_blocks(tmp_path, monkeypatch):
    base = tmp_path / "state"
    reports = base / "reports"
    monkeypatch.setattr(nightly_eval, "BASE", str(base))
    monkeypatch.setattr(nightly_eval, "REPORTS", str(reports))
    monkeypatch.setattr(nightly_eval, "HISTORY", str(reports / "nightly_history.jsonl"))
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "300000"}
    }))
    monkeypatch.setattr(nightly_eval, "SETTINGS", str(settings))
    monkeypatch.setattr(nightly_eval.glob, "glob", lambda pattern: [])

    def fake_run(cmd, timeout=1800):
        if cmd[:3] == [sys.executable, "-m", "pytest"]:
            return 0, "pytest ok"
        if cmd[:2] == ["bash", "tests/smoke_test.sh"]:
            return 0, "smoke ok"
        if cmd[-1:] == ["--version"]:
            return 0, "Claude Code test\n"
        if len(cmd) >= 2 and cmd[1] == "analyze_corpus.py":
            report = cmd[cmd.index("--json") + 1]
            with open(report, "w", encoding="utf-8") as fh:
                json.dump({"sessions": [{
                    "path": "large.jsonl",
                    "learned_tier": "512k",
                    "learned_window": 512000,
                    "native_ceiling": 300000,
                    "native_ceiling_blocks_learned_window": True,
                    "compactions": [{
                        "trigger": "auto",
                        "before": 341000,
                        "late_by_tokens": 41000,
                        "learned_occupancy_at_compact": 0.666,
                    }],
                }]}, fh)
            return 0, "Sessions analyzed:        1"
        return 0, ""

    monkeypatch.setattr(nightly_eval, "run", fake_run)

    assert nightly_eval.main() == 0
    record = json.loads((reports / "nightly_history.jsonl").read_text().splitlines()[-1])
    assert record["learned_tiers"]["512k"]["sessions"] == 1
    assert record["learned_tiers"]["512k"]["compactions"] == 1
    assert record["native_ceiling_blocked_sessions"] == 1
    md = (reports / f"nightly-{datetime.date.today().isoformat()}.md").read_text()
    assert "512k: sessions 1, compactions 1" in md
    assert "native ceiling blocks learned window: 1 session(s)" in md
