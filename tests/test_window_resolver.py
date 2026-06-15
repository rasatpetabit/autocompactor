import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import window_resolver  # noqa: E402


def test_observed_peak_classifies_candidate_windows():
    cases = [
        (180_000, 200_000, "200k", "small_session_clamp"),
        (250_000, 300_000, "300k", "observed_peak"),
        (341_000, 512_000, "512k", "observed_peak"),
        (490_000, 1_000_000, "1m", "observed_peak"),
    ]
    for peak, learned_window, tier, source in cases:
        res = window_resolver.resolve_window(
            configured_window=300_000,
            observed_peak=peak,
        )
        assert res.learned_window == learned_window
        assert res.learned_tier == tier
        assert res.window_source == source


def test_pi_runtime_context_window_is_authoritative():
    res = window_resolver.resolve_window(
        harness="pi",
        configured_window=200_000,
        observed_peak=120_000,
        runtime_context_window=512_000,
        reserve=40_000,
    )
    assert res.effective_window == 472_000
    assert res.learned_window == 512_000
    assert res.learned_tier == "512k"
    assert res.window_source == "runtime"
    assert res.runtime_context_window == 512_000
    assert res.reserve == 40_000


def test_native_ceiling_warning_for_learned_large_window():
    res = window_resolver.resolve_window(
        configured_window=300_000,
        observed_peak=341_000,
        native_ceiling=300_000,
    )
    assert res.learned_window == 512_000
    assert res.native_ceiling == 300_000
    assert res.native_ceiling_blocks_learned_window is True


def test_no_observed_peak_uses_configured_window_as_source():
    res = window_resolver.resolve_window(
        configured_window=512_000,
        observed_peak=0,
        native_ceiling=300_000,
    )
    assert res.effective_window == 512_000
    assert res.learned_window == 512_000
    assert res.learned_tier == "512k"
    assert res.window_source == "configured"
    assert res.native_ceiling_blocks_learned_window is True


def test_observe_mode_keeps_effective_window_on_current_live_path():
    claude = window_resolver.resolve_window(
        configured_window=300_000,
        observed_peak=341_000,
    )
    assert claude.effective_window == 300_000
    assert claude.learned_window == 512_000

    small = window_resolver.resolve_window(
        configured_window=300_000,
        observed_peak=180_000,
    )
    assert small.effective_window == 200_000
    assert small.learned_window == 200_000

    pi = window_resolver.resolve_window(
        harness="pi",
        configured_window=512_000,
        observed_peak=180_000,
        reserve=40_000,
    )
    assert pi.effective_window == 472_000
    assert pi.learned_window == 200_000
