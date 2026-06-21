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
