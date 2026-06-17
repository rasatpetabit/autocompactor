import json
import os
import sys
import datetime

import pytest  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from autocompactor import nightly_eval  # noqa: E402


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
        if len(cmd) >= 2 and cmd[1] == "src/analyze_corpus.py":
            seen["backtest_cmd"] = cmd
            report = cmd[cmd.index("--json") + 1]
            with open(report, "w", encoding="utf-8") as fh:
                json.dump({"sessions": []}, fh)
            return 0, "No analyzable sessions."
        return 0, ""

    monkeypatch.setattr(nightly_eval, "run", fake_run)

    assert nightly_eval.main() == 0
    cmd = seen["backtest_cmd"]
    assert cmd[cmd.index("--window") + 1] == "200000.0"
    assert cmd[cmd.index("--soft") + 1] == "0.5"
    assert cmd[cmd.index("--hard") + 1] == "0.55"
    assert cmd[cmd.index("--stale-frac") + 1] == "0.9"
    record = json.loads((reports / "nightly_history.jsonl").read_text().splitlines()[-1])
    assert record["hard_tokens"] == pytest.approx(110000.0)


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
        if len(cmd) >= 2 and cmd[1] == "src/analyze_corpus.py":
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


def _auto(sid, ts, ceiling):
    return {"type": "precompact", "trigger": "auto", "session_id": sid,
            "ts": ts, "native_ceiling": ceiling}


def _eval(sid, ts, recommended):
    return {"type": "monitor_eval", "session_id": sid, "ts": ts,
            "recommended": recommended}


def test_auto_warning_coverage_epoch_filter_and_classification():
    """WI-1: epoch filter (old-config/None autos excluded), cold-start separated
    (unwarnable), session-level warned, genuine miss = unwarned."""
    pre = [
        _auto("s1", "2026-06-17T10:00:00", 300000),   # warned
        _auto("s2", "2026-06-17T11:00:00", 300000),   # cold-start (no eval)
        _auto("s3", "2026-06-17T12:00:00", 300000),   # unwarned (eval, no rec)
        _auto("s4", "2026-06-17T09:00:00", None),      # off-epoch (old config)
        _auto("s5", "2026-06-17T09:30:00", 150000),    # off-epoch (old ceiling)
    ]
    mon = [
        _eval("s1", "2026-06-17T09:55:00", True),
        _eval("s3", "2026-06-17T11:55:00", False),
    ]
    cov = nightly_eval.auto_warning_coverage(pre, mon, live_ceiling=300000)
    assert cov["epoch"] == 300000
    assert cov["warned"] == 1            # s1
    assert cov["unwarned"] == 1          # s3
    assert cov["cold_start"] == 1        # s2
    assert cov["off_epoch"] == 2         # s4, s5
    assert cov["auto_seen"] == 5


def test_auto_warning_coverage_is_session_level():
    """The naive per-interval metric marked the 2nd auto unwarned because the
    only recommendation fell before the 1st precompact. Session-level counts
    both autos warned."""
    pre = [
        _auto("s1", "2026-06-17T10:00:00", 300000),
        _auto("s1", "2026-06-17T12:00:00", 300000),
    ]
    mon = [_eval("s1", "2026-06-17T09:30:00", True)]
    cov = nightly_eval.auto_warning_coverage(pre, mon, live_ceiling=300000)
    assert cov["warned"] == 2
    assert cov["unwarned"] == 0
    assert cov["cold_start"] == 0


def test_auto_warning_coverage_epoch_falls_back_to_modal():
    """With no live ceiling, the epoch is the modal observed native_ceiling;
    minority-ceiling autos are treated as off-epoch."""
    pre = [
        _auto("a", "t1", 300000),
        _auto("b", "t2", 300000),
        _auto("c", "t3", 150000),       # minority -> off-epoch
    ]
    cov = nightly_eval.auto_warning_coverage(pre, [], live_ceiling=None)
    assert cov["epoch"] == 300000
    assert cov["off_epoch"] == 1
    assert cov["cold_start"] == 2       # the two 300k autos, no evals
