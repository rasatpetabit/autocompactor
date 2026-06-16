import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from autocompactor import window_resolver  # noqa: E402


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


def test_pi_small_session_uses_exact_window_not_tier_clamp():
    """D4 characterization: the small_session_clamp tier clamp is Claude-ONLY
    by design. Claude infers an unknown model window from observed peak and
    clamps a 1M-tuned configured window down to the 200k tier; Pi's effective
    window is the exact contextWindow - reserve (the live window is
    authoritative on the evaluate path), so Pi must NOT tier-clamp. Pinned so
    the asymmetry can't be 'fixed' into a regression."""
    claude = window_resolver.resolve_window(
        configured_window=400_000, observed_peak=180_000)
    assert claude.window_source == "small_session_clamp"
    assert claude.effective_window == 200_000  # clamped to tier0, not 400k

    pi = window_resolver.resolve_window(
        harness="pi", configured_window=400_000, observed_peak=180_000,
        reserve=40_000)
    assert pi.window_source == "small_session_clamp"
    assert pi.effective_window == 360_000  # 400k - 40k reserve, NO tier clamp
    assert pi.learned_window == 200_000    # tier still recorded as 200k


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
    # native_ceiling CAPS the effective window for Claude: configured 512k is
    # over the enforced 300k ceiling, so effective is capped to 300k (the
    # enforced reality). learned_tier still reflects the configured window.
    res = window_resolver.resolve_window(
        configured_window=512_000,
        observed_peak=0,
        native_ceiling=300_000,
    )
    assert res.effective_window == 300_000
    assert res.learned_window == 512_000
    assert res.learned_tier == "512k"
    assert res.window_source == "native_ceiling_capped"
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


def test_native_ceiling_caps_over_inference_for_claude():
    """The tier inference can overshoot the enforced ceiling (miss-attribution:
    inferred 512k when native was 500k). native_ceiling must cap it down."""
    res = window_resolver.resolve_window(
        configured_window=1_000_000, observed_peak=341_000,
        native_ceiling=500_000)
    assert res.effective_window == 500_000          # capped, not 1m
    assert res.window_source == "native_ceiling_capped"
    assert res.learned_window == 512_000            # tier still recorded


def test_native_ceiling_small_model_caps_down():
    """A 64k/128k model has a small CLAUDE_CODE_AUTO_COMPACT_WINDOW; it must
    cap the effective window down so thresholds track the small wall."""
    res = window_resolver.resolve_window(
        configured_window=200_000, observed_peak=120_000,
        native_ceiling=128_000)
    assert res.effective_window == 128_000
    assert res.window_source == "native_ceiling_capped"


def test_native_ceiling_never_loosens_a_tighter_window():
    """An owner who set an aggressive WINDOW below the ceiling keeps it —
    native_ceiling only binds when effective would EXCEED it. So a 200k
    configured session under a 300k ceiling stays at 200k (not loosened)."""
    res = window_resolver.resolve_window(
        configured_window=200_000, observed_peak=180_000,
        native_ceiling=300_000)
    assert res.effective_window == 200_000          # NOT raised to 300k
    assert res.window_source == "small_session_clamp"  # clamp path, uncapped


def test_native_ceiling_does_not_affect_pi():
    """Pi's runtime context window is authoritative; the cap is Claude-only."""
    res = window_resolver.resolve_window(
        harness="pi", configured_window=1_000_000, observed_peak=490_000,
        runtime_context_window=600_000, reserve=40_000, native_ceiling=300_000)
    assert res.effective_window == 560_000          # 600k - 40k, NOT capped to 300k
    assert res.window_source == "runtime"
