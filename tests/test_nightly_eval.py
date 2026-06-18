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


# --- WI-A: epoch_auto_trigger (deviation watch is sourced from this) ----------

def _auto_tok(sid, ts, ceiling, tokens):
    return {"type": "precompact", "trigger": "auto", "session_id": sid,
            "ts": ts, "native_ceiling": ceiling, "context_tokens": tokens}


def test_epoch_auto_trigger_selects_current_epoch_only():
    """Two-epoch event set: old-epoch autos (~254k, ceiling 300k) plus
    current-epoch autos at the raised ceiling. epoch_auto_trigger must take the
    median of ONLY the requested epoch's autos, so the deviation watch can't
    phantom-flag drift off a mixed-epoch median when the ceiling moves."""
    pre = [
        _auto_tok("s1", "t1", 300000, 250000),   # old epoch
        _auto_tok("s2", "t2", 300000, 258000),   # old epoch
        _auto_tok("s3", "t3", 900000, 800000),   # current epoch
        _auto_tok("s4", "t4", 900000, 810000),   # current epoch
        _auto_tok("s5", "t5", 900000, 820000),   # current epoch
        {"type": "precompact", "trigger": "manual", "session_id": "s6",
         "ts": "t6", "native_ceiling": 900000, "context_tokens": 700000},  # manual: excluded
    ]
    med, n = nightly_eval.epoch_auto_trigger(pre, 900000)
    assert n == 3
    assert med == 810000             # median of 800k/810k/820k, current epoch only
    old_med, old_n = nightly_eval.epoch_auto_trigger(pre, 300000)
    assert (old_n, old_med) == (2, 254000)   # old epoch still selectable
    _, all_n = nightly_eval.epoch_auto_trigger(pre, None)
    assert all_n == 5                # epoch=None pools every auto; manual still out


def test_epoch_auto_trigger_defers_below_three():
    """<3 current-epoch autos -> n<3, which gates main()'s deviation note to the
    'deferred' branch instead of a phantom retune issue. No autos -> (None, 0)."""
    pre = [
        _auto_tok("s1", "t1", 900000, 800000),
        _auto_tok("s2", "t2", 900000, 820000),
        _auto_tok("s3", "t3", 300000, 250000),   # off-epoch -> excluded
    ]
    med, n = nightly_eval.epoch_auto_trigger(pre, 900000)
    assert n == 2 and med == 810000              # <3 -> caller defers
    assert nightly_eval.epoch_auto_trigger([], 900000) == (None, 0)


def test_nightly_breaker_is_session_anchored(tmp_path, monkeypatch):
    """WI-A: the rapid-refill-breaker suspect anchors to each session's OWN max
    auto-trigger, not the global ceiling estimate. A session with >=2 autos whose
    post-compaction peak climbs >CEILING_SLACK past its own max auto-pre is a
    suspect; an otherwise-identical session whose peak stays within slack is not —
    so a ceiling change can't phantom-flag old-epoch sessions."""
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
                json.dump({"sessions": [
                    {   # suspect: 2 autos, peak 60k past its own 254k max
                        "path": "a.jsonl", "learned_tier": "300k",
                        "post_last_compaction_peak": 314000,
                        "compactions": [
                            {"trigger": "auto", "before": 250000},
                            {"trigger": "auto", "before": 254000},
                        ],
                    },
                    {   # NOT a suspect: 2 autos, peak within slack of its own max
                        "path": "b.jsonl", "learned_tier": "300k",
                        "post_last_compaction_peak": 270000,
                        "compactions": [
                            {"trigger": "auto", "before": 250000},
                            {"trigger": "auto", "before": 252000},
                        ],
                    },
                ]}, fh)
            return 0, "Sessions analyzed: 2"
        return 0, ""

    monkeypatch.setattr(nightly_eval, "run", fake_run)
    assert nightly_eval.main() == 0
    record = json.loads((reports / "nightly_history.jsonl").read_text().splitlines()[-1])
    assert record["breaker_suspects"] == 1
    assert any("rapid-refill-breaker" in i for i in record["issues"])


# --- WI-B: realized_reductions (close the reclaim telemetry gap) --------------

def _pre_tok(sid, ts, tokens):
    return {"type": "precompact", "session_id": sid, "ts": ts,
            "context_tokens": tokens}


def _mon_tok(sid, ts, tokens):
    return {"type": "monitor_eval", "session_id": sid, "ts": ts,
            "context_tokens": tokens}


def test_realized_reductions_pairs_precompact_with_first_smaller_eval():
    """WI-B: PostCompact can't log after_tokens; reconstruct the realized floor
    by joining each precompact to the FIRST later, smaller same-session
    monitor_eval. reclaim = before - after."""
    pre = [_pre_tok("s1", "2026-06-17T10:00:00", 200000)]
    mon = [
        _mon_tok("s1", "2026-06-17T10:05:00", 80000),    # first smaller -> realized after
        _mon_tok("s1", "2026-06-17T10:10:00", 95000),    # later, ignored (first wins)
    ]
    r = nightly_eval.realized_reductions(pre, mon)
    assert r["reclaim_n"] == 1
    assert r["reclaim_median"] == 120000             # 200k - 80k


def test_realized_reductions_ignores_earlier_larger_or_cross_session():
    """Only a LATER, same-session eval with context_tokens < before pairs. An
    earlier eval, a same-or-larger eval, and a different session must not."""
    pre = [_pre_tok("s1", "2026-06-17T10:00:00", 200000)]
    mon = [
        _mon_tok("s1", "2026-06-17T09:00:00", 50000),    # earlier -> excluded
        _mon_tok("s1", "2026-06-17T10:05:00", 210000),   # later but larger -> excluded
        _mon_tok("s2", "2026-06-17T10:05:00", 40000),    # other session -> excluded
    ]
    r = nightly_eval.realized_reductions(pre, mon)
    assert r["reclaim_n"] == 0
    assert r["reclaim_median"] is None


def test_realized_reductions_median_over_multiple():
    """Median across several compactions, each paired to its own first smaller
    eval within the same session."""
    pre = [
        _pre_tok("s1", "2026-06-17T10:00:00", 200000),
        _pre_tok("s1", "2026-06-17T12:00:00", 180000),
        _pre_tok("s2", "2026-06-17T10:00:00", 160000),
    ]
    mon = [
        _mon_tok("s1", "2026-06-17T10:05:00", 100000),   # s1 #1 -> 100k reclaim
        _mon_tok("s1", "2026-06-17T12:05:00", 60000),    # s1 #2 -> 120k reclaim
        _mon_tok("s2", "2026-06-17T10:05:00", 80000),    # s2    -> 80k reclaim
    ]
    r = nightly_eval.realized_reductions(pre, mon)
    assert r["reclaim_n"] == 3
    assert r["reclaim_median"] == 100000             # median(100k, 120k, 80k)
