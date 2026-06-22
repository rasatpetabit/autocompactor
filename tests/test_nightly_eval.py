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
