import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from autocompactor import nightly_eval  # noqa: E402


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


# --- night canary smoke-path regression -----------------------------------------

def test_nightly_smoke_command_targets_existing_file(tmp_path, monkeypatch):
    """The schema-drift smoke canary must invoke a file that actually exists
    and must be gated on (PI_SMOKE=1). A wrong path (the generic
    tests/smoke_test.sh, which does not exist) made the canary fail every
    night on a missing-file error; a missing PI_SMOKE gate made it silently
    skip (false PASS). Pin both behaviourally, not by source-text scan.

    We monkeypatch `run` to record each invocation (and short-circuit the
    pytest/smoke calls so the full nightly doesn't actually execute), point
    the state dir at a tmp dir, and run main(); then assert the smoke call's
    argv and env. A source-text assertion would pass even with the fix
    reverted (the explanatory comment still contains both strings)."""
    calls = []

    def fake_run(cmd, timeout=1800, env=None):
        calls.append((list(cmd), env))
        return 0, ""

    monkeypatch.setattr(nightly_eval, "run", fake_run)
    monkeypatch.setenv("AUTOCOMPACTOR_STATE_DIR", str(tmp_path))
    nightly_eval.main()

    # The smoke call is the second run() invocation (after pytest).
    smoke_cmd, smoke_env = next(
        ((c, e) for c, e in calls if c[:2] == ["bash", "tests/smoke_test_pi.sh"]),
        (None, None))
    assert smoke_cmd is not None, (
        "nightly smoke canary must call bash tests/smoke_test_pi.sh")
    assert smoke_env and smoke_env.get("PI_SMOKE") == "1", (
        "nightly smoke canary must set PI_SMOKE=1 so it actually runs")
    # And the target file must exist (guards against the path being renamed
    # again without updating nightly_eval).
    assert os.path.isfile(os.path.join(REPO, "tests", "smoke_test_pi.sh")), (
        "nightly smoke canary target tests/smoke_test_pi.sh is missing")


# --- Task 9 (context-window-analysis): readout-only per-package floor probe ---

import json as _json
import datetime as _dt


PI_JSONL_SAMPLE = (
    '{"type":"session","version":3,"id":"x","timestamp":"2026-06-25T00:00:00Z","cwd":"/tmp"}\n'
    '{"type":"agent_start"}\n'
    '{"type":"turn_start"}\n'
    '{"type":"message_start","message":{"role":"user","content":[{"type":"text","text":"hi"}]}}\n'
    '{"type":"message_end","message":{"role":"assistant","usage":{"input":5388,"output":11,"totalTokens":5399}}}\n'
    '{"type":"turn_end","message":{"role":"assistant","usage":{"input":5388,"output":11}}}\n'
    '{"type":"agent_end","messages":[{"role":"assistant","usage":{"input":5388,\"output\":11}}]}\n'
)


def _strip_env(monkeypatch):
    for k in list(__import__("os").environ):
        if k.startswith("AUTOCOMPACTOR"):
            monkeypatch.delenv(k, raising=False)


def test_parse_first_request_input_reads_usage_input():
    n = nightly_eval._parse_first_request_input(PI_JSONL_SAMPLE)
    assert n == 5388


def test_parse_first_request_input_none_on_empty():
    assert nightly_eval._parse_first_request_input("") is None
    assert nightly_eval._parse_first_request_input("not json\n") is None


def test_run_floor_probe_uses_injected_spawn_and_diffs():
    calls = []

    def fake_spawn(label, *, env_overrides=None, provider=None, model=None,
                    extra_args=None, timeout=None):
        calls.append(label)
        # full run sees everything; per-package runs see the floor minus that
        # package's tool schemas, so the diff = the package cost.
        if label == "full":
            return 50000
        if label == "pi-subagents":
            return 50000 - 11229
        if label == "context-mode":
            return 50000 - 10973
        return None

    per = nightly_eval.run_floor_probe(
        spawn=fake_spawn,
        packages=[("pi-subagents", None, None),
                  ("context-mode", None, None)])
    assert per == {"pi-subagents": 11229, "context-mode": 10973}
    assert calls == ["full", "pi-subagents", "context-mode"]


def test_run_floor_probe_returns_empty_when_full_missing():
    def fake_spawn(label, **kw):
        return None
    assert nightly_eval.run_floor_probe(spawn=fake_spawn) == {}


def test_run_floor_probe_never_raises_on_spawn_exception():
    def fake_spawn(label, **kw):
        raise RuntimeError("boom")
    assert nightly_eval.run_floor_probe(spawn=fake_spawn) == {}


def test_write_floor_probe_frozen_schema(tmp_path, monkeypatch):
    _strip_env(monkeypatch)
    monkeypatch.setattr(nightly_eval, "PI_BASE", str(tmp_path))
    monkeypatch.setattr(nightly_eval, "FLOOR_PROBE_PATH",
                        str(tmp_path / "floor-probe.json"))
    monkeypatch.setattr(nightly_eval, "_detect_pi_version",
                        lambda: "pi 0.80.2")
    nightly_eval.write_floor_probe({"pi-subagents": 11229,
                                     "context-mode": 10973})
    data = _json.load(open(tmp_path / "floor-probe.json"))
    # Frozen schema exact keys
    assert set(data) == {"per_package", "measured_at", "pi_version",
                         "staleness_budget"}
    assert data["per_package"] == {"pi-subagents": 11229,
                                    "context-mode": 10973}
    assert data["pi_version"] == "pi 0.80.2"
    assert data["staleness_budget"] == 14 * 86400  # config default
    # measured_at is parseable ISO-8601
    _dt.datetime.fromisoformat(data["measured_at"].replace("Z", "+00:00"))


def test_floor_probe_is_fresh_states(tmp_path, monkeypatch):
    _strip_env(monkeypatch)
    p = tmp_path / "floor-probe.json"
    monkeypatch.setattr(nightly_eval, "FLOOR_PROBE_PATH", str(p))
    # missing
    assert nightly_eval.floor_probe_is_fresh() == ("missing", "")
    # fresh (measured now, default budget)
    fresh = {"per_package": {}, "measured_at":
             _dt.datetime.now(_dt.timezone.utc).isoformat(),
             "pi_version": "", "staleness_budget": 86400}
    p.write_text(_json.dumps(fresh))
    st, _ = nightly_eval.floor_probe_is_fresh()
    assert st == "fresh"
    # stale (measured 30 days ago, 1-day budget)
    old = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=30)
    stale = {"per_package": {}, "measured_at": old.isoformat(),
             "pi_version": "", "staleness_budget": 86400}
    p.write_text(_json.dumps(stale))
    st, _ = nightly_eval.floor_probe_is_fresh()
    assert st == "stale"


def test_floor_probe_is_observational_only_decision_never_reads_it():
    """The frozen artifact is readout-only: no DECISION/POLICY module may OPEN
    or READ floor-probe.json (the readout path - context_inventory + nightly_eval
    - is the only reader). Comments documenting the T9 boundary are allowed;
    this asserts no READ access (open/json.load of the probe artifact) in the
    decision/policy modules. transcript_lib hosts the READOUT adapter
    (context_composition -> build_inventory(include_probe=True)) which is the
    INTENDED readout reader, so it is excluded from this scan; the T9 boundary
    is that the DECISION INPUTS must not read the probe (also asserted
    behaviorally by test_decision_path_does_not_read_floor_probe in T8)."""
    import inspect
    import re
    from autocompactor import pi_bridge, policy
    read_patterns = [
        r"open\s*\([^)]*floor-probe\.json",
        r"json\.load\s*\([^)]*floor-probe\.json",
        r"_read_probe_tools_tokens",
    ]
    for mod in (pi_bridge, policy):
        src = inspect.getsource(mod)
        for pat in read_patterns:
            assert not re.search(pat, src), (
                f"{mod.__name__} reads floor-probe.json ({pat}) - T9 boundary "
                f"violation: decision/policy modules must not open the probe")


def test_main_runs_floor_probe_best_effort(tmp_path, monkeypatch):
    """main() invokes run_floor_probe fail-soft; a spawn failure must NOT break
    the nightly eval."""
    _strip_env(monkeypatch)
    monkeypatch.setattr(nightly_eval, "BASE", str(tmp_path))
    monkeypatch.setattr(nightly_eval, "REPORTS", str(tmp_path / "reports"))
    monkeypatch.setattr(nightly_eval, "HISTORY",
                        str(tmp_path / "reports" / "nightly_history.jsonl"))
    monkeypatch.setattr(nightly_eval, "PI_BASE", str(tmp_path))
    monkeypatch.setattr(nightly_eval, "FLOOR_PROBE_PATH",
                        str(tmp_path / "floor-probe.json"))

    def fake_run(cmd, timeout=1800, env=None):
        return 0, ""  # pytest + smoke both "pass"
    monkeypatch.setattr(nightly_eval, "run", fake_run)
    monkeypatch.setattr(nightly_eval, "run_floor_probe",
                        lambda **kw: {"pi-subagents": 11229})
    monkeypatch.setattr(nightly_eval, "write_floor_probe",
                        lambda per, **kw: nightly_eval.FLOOR_PROBE_PATH)
    monkeypatch.setattr(nightly_eval, "_detect_pi_version", lambda: "x")
    rc = nightly_eval.main()
    assert rc == 0  # nightly eval never breaks on the probe step
