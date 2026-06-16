"""Unit tests for the unified policy decision rule (autocompactor.policy).

These pin the rule's semantics independent of any adapter: hard line, soft
+signal, min-savings guard, rising-only cooldown, profile defaults, and
old-key override precedence. The rule is intended to be at parity with the
current inline logic in context_monitor.py / pi_bridge.py.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "src"))

from autocompactor import config_lib, policy  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_config_cache():
    # config_lib caches the loaded config at module level; earlier in-process
    # tests may have populated it from the real config.json. Reset so each
    # test reads under its own AUTOCOMPACTOR_CONFIG env.
    config_lib._config_cache = None
    yield
    config_lib._config_cache = None


def _cfg(eff=200_000, **over):
    base = dict(profile="balanced", mode="advise", soft=0.40, hard=0.65,
                cooldown=25_000, stale_frac=0.50, post_floor=50_000,
                min_savings=20_000, effective_limit=eff)
    base.update(over)
    return policy.PolicyConfig(**base)


def test_hard_line_recommends_without_signal():
    d = policy.decide(policy.PolicyInput(180_000, 200_000, gating=False), _cfg())
    assert d.recommend is True
    assert d.occupancy == 0.9


def test_soft_without_signal_is_quiet():
    # 0.50 occupancy >= soft(0.40) but < hard(0.65) and no signal -> no
    d = policy.decide(policy.PolicyInput(100_000, 200_000, gating=False), _cfg())
    assert d.recommend is False


def test_soft_with_signal_recommends():
    d = policy.decide(policy.PolicyInput(100_000, 200_000, gating=True), _cfg())
    assert d.recommend is True


def test_below_soft_is_quiet_even_with_signal():
    d = policy.decide(policy.PolicyInput(60_000, 200_000, gating=True), _cfg())
    assert d.recommend is False   # 0.30 < soft


def test_min_savings_guard_blocks():
    # context 140k (occ 0.7 >= hard), post_floor 50k -> reclaim 90k >= min 20k: ok.
    d = policy.decide(policy.PolicyInput(140_000, 200_000, gating=False),
                      _cfg(post_floor=50_000, min_savings=20_000))
    assert d.recommend is True
    # raise the floor so reclaim < min -> blocked even at hard occupancy
    d2 = policy.decide(policy.PolicyInput(140_000, 200_000, gating=False),
                       _cfg(post_floor=130_000, min_savings=20_000))
    assert d2.recommend is False
    assert d2.est_reclaim == 10_000


def test_cooldown_suppresses_rising_only():
    # staged at 180k; a small rise stays within cooldown -> suppressed
    d = policy.decide(
        policy.PolicyInput(185_000, 200_000, gating=False, last_reco_tokens=180_000),
        _cfg(cooldown=25_000))
    assert d.recommend is False
    assert d.suppressed_by_cooldown is True
    # a rise past cooldown -> recommends again
    d2 = policy.decide(
        policy.PolicyInput(210_000, 200_000, gating=False, last_reco_tokens=180_000),
        _cfg(cooldown=25_000))
    assert d2.recommend is True


def test_cooldown_resets_on_shrink():
    # Regression guard for issue #1: a shrunken context must not stay muted.
    d = policy.decide(
        policy.PolicyInput(150_000, 200_000, gating=False, last_reco_tokens=180_000),
        _cfg(cooldown=25_000))
    assert d.recommend is True          # negative delta -> baseline reset
    assert d.suppressed_by_cooldown is False


def test_resolve_policy_config_profile_defaults(monkeypatch):
    monkeypatch.setenv("AUTOCOMPACTOR_CONFIG", "")   # hermetic
    cfg = policy.resolve_policy_config("claude", 200_000, profile="balanced")
    assert cfg.profile == "balanced"
    # SOFT is now the window-aware target curve (no SOFT_PCT override):
    # balanced 200k -> target 100k (interim ceiling = hard 0.65*200k - MS 30k),
    # so soft = 100k/200k = 0.50. hard/cooldown come from the profile base.
    assert cfg.hard == 0.65 and cfg.cooldown == 25_000
    assert cfg.target_tokens == 100_000
    assert cfg.soft == 0.50


def test_resolve_policy_config_unknown_profile_falls_back(monkeypatch):
    monkeypatch.setenv("AUTOCOMPACTOR_CONFIG", "")
    cfg = policy.resolve_policy_config("claude", 200_000, profile="nope")
    assert cfg.profile == "balanced"


def test_resolve_policy_config_old_key_overrides_profile(monkeypatch):
    # A deprecated explicit key must win over the profile-derived default
    # (no silent behavior change for tuned installs).
    monkeypatch.setenv("AUTOCOMPACTOR_CONFIG", "")
    monkeypatch.setenv("AUTOCOMPACTOR_HARD_PCT", "0.90")
    cfg = policy.resolve_policy_config("claude", 200_000, profile="economy")
    assert cfg.hard == 0.90            # override wins, not economy's 0.50
    assert cfg.profile == "economy"


def test_economy_profile_compacts_earlier_than_lazy(monkeypatch):
    monkeypatch.setenv("AUTOCOMPACTOR_CONFIG", "")   # hermetic: no old-key overrides
    eff = 200_000
    cfg_e = policy.resolve_policy_config("claude", eff, profile="economy")
    cfg_l = policy.resolve_policy_config("claude", eff, profile="lazy")
    # same mid context, no signal: economy (hard 0.50) recommends, lazy (0.80) doesn't
    d_e = policy.decide(policy.PolicyInput(110_000, eff, gating=False), cfg_e)
    d_l = policy.decide(policy.PolicyInput(110_000, eff, gating=False), cfg_l)
    assert d_e.recommend is True
    assert d_l.recommend is False


# -------------------------------------- window-size-aware target curve

def test_target_curve_small_window_target_equals_window():
    """64k regime: the floor (~70k) exceeds the window, so there is nothing
    to reclaim — target rides up at the window (compact only on stale data,
    never to hit a percentage). target_tokens returns the window itself."""
    t = policy.target_tokens(64_000, "balanced", 70_000, 30_000, 0.65)
    assert t == 64_000


def test_target_curve_large_window_targets_low_occupancy():
    """1m regime: a large window is mostly reclaimable headroom (the ~69k
    floor is window-independent), so target sits far below the window.
    balanced 1m -> ~251k (25%), matching the advisor's table."""
    t = policy.target_tokens(1_000_000, "balanced", 70_000, 30_000, 0.65)
    assert 245_000 < t < 258_000
    assert t / 1_000_000 < 0.30          # well below the window


def test_target_curve_medium_window_around_150k():
    """256k/512k regime: keep ~150k for efficiency. balanced 512k ~195k."""
    t512 = policy.target_tokens(512_000, "balanced", 70_000, 30_000, 0.65)
    assert 190_000 < t512 < 200_000


def test_target_curve_never_exceeds_hard_line():
    """The interim ceiling keeps SOFT strictly below HARD so the SOFT->HARD
    band never inverts (target < hard_tokens - min_savings)."""
    for W in (200_000, 300_000, 512_000, 1_000_000):
        hard = 0.65
        t = policy.target_tokens(W, "balanced", 70_000, 30_000, hard)
        assert t < hard * W - 30_000 + 1   # strictly below the hard line


def test_target_curve_never_below_actionable_floor():
    """target is floored at F + MS so a recommendation is always actionable."""
    for W in (200_000, 512_000):
        t = policy.target_tokens(W, "balanced", 70_000, 30_000, 0.65)
        assert t >= 100_000


def test_resolve_uses_target_curve_when_no_soft_override(monkeypatch):
    monkeypatch.setenv("AUTOCOMPACTOR_CONFIG", "")
    # 512k balanced -> target ~195k, soft ~0.38 (NOT the flat 0.40 fallback)
    cfg = policy.resolve_policy_config("claude", 512_000, profile="balanced")
    assert cfg.target_tokens > 0
    assert 0.36 < cfg.soft < 0.40


def test_resolve_soft_pct_override_disables_curve(monkeypatch):
    """A deprecated explicit SOFT_PCT wins and zeroes target_tokens (the curve
    is not used) — no silent behavior change for tuned installs."""
    monkeypatch.setenv("AUTOCOMPACTOR_CONFIG", "")
    monkeypatch.setenv("AUTOCOMPACTOR_SOFT_PCT", "0.35")
    cfg = policy.resolve_policy_config("claude", 512_000, profile="balanced")
    assert cfg.soft == 0.35
    assert cfg.target_tokens == 0
