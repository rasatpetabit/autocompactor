"""Pins for window_resolver.resolve_window (configured vs runtime paths)."""

from autocompactor import window_resolver


def test_resolve_window_configured_minus_reserve():
    """cmd_prepare path: no runtime window → effective = configured − reserve."""
    res = window_resolver.resolve_window(
        configured_window=512_000,
        observed_peak=100_000,
        reserve=40_000,
    )
    assert res.effective_window == 512_000 - 40_000
    assert res.configured_window == 512_000
    assert res.reserve == 40_000
    assert res.window_source == "configured"
    assert res.runtime_context_window is None


def test_resolve_window_runtime_minus_reserve():
    """cmd_evaluate hot path: runtime contextWindow wins over configured."""
    res = window_resolver.resolve_window(
        configured_window=512_000,
        observed_peak=100_000,
        runtime_context_window=200_000,
        reserve=40_000,
    )
    assert res.effective_window == 200_000 - 40_000
    assert res.window_source == "runtime"
    assert res.runtime_context_window == 200_000


def test_resolve_window_reserve_floor():
    """effective never drops below 1 even if reserve ≥ window."""
    res = window_resolver.resolve_window(
        configured_window=10_000,
        observed_peak=0,
        reserve=50_000,
    )
    assert res.effective_window == 1.0
