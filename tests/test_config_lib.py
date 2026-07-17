"""
Pins the config_lib resolution order (single namespace, Pi sole adapter):
env (with _WIDE preference on wide windows) > config.local.json >
config.json > default. Harness sections and the AUTOCOMPACTOR_PI_* env
prefix were removed in the Pi-only flatten; this suite pins the flat
semantics plus an effective-value equivalence check against the shipped
config.json.
"""
import json
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from autocompactor import config_lib  # noqa: E402


@pytest.fixture
def temp_config(tmp_path, monkeypatch):
    """Point config_lib at a throwaway config.json and clear ambient env."""
    cfg_path = tmp_path / "config.json"

    def write(data, local=None):
        cfg_path.write_text(json.dumps(data))
        local_path = tmp_path / "config.local.json"
        if local is not None:
            local_path.write_text(json.dumps(local))
        monkeypatch.setattr(config_lib, "_CONFIG", str(cfg_path))
        monkeypatch.setattr(config_lib, "_CONFIG_LOCAL", str(local_path))
        monkeypatch.setattr(config_lib, "_config_cache", None)

    for key in list(__import__("os").environ):
        if key.startswith("AUTOCOMPACTOR"):
            monkeypatch.delenv(key, raising=False)
    return write


def test_float_env_beats_config(temp_config, monkeypatch):
    temp_config({"HARD_PCT": 0.65})
    monkeypatch.setenv("AUTOCOMPACTOR_HARD_PCT", "0.62")
    assert config_lib.cfg.float("HARD_PCT") == 0.62


def test_float_top_level(temp_config):
    temp_config({"HARD_PCT": 0.90})
    assert config_lib.cfg.float("HARD_PCT") == 0.90


def test_float_wide_key_reachable_in_config(temp_config):
    temp_config({"HARD_PCT": 0.90, "HARD_PCT_WIDE": 0.60})
    assert config_lib.cfg.float_windowed("HARD_PCT", 400_000) == 0.60
    assert config_lib.cfg.float_windowed("HARD_PCT", 200_000) == 0.90


def test_float_wide_env_beats_wide_config(temp_config, monkeypatch):
    temp_config({"HARD_PCT_WIDE": 0.60})
    monkeypatch.setenv("AUTOCOMPACTOR_HARD_PCT_WIDE", "0.55")
    assert config_lib.cfg.float_windowed("HARD_PCT", 400_000) == 0.55


def test_str_env_beats_config(temp_config, monkeypatch):
    temp_config({"MODE": "advise"})
    monkeypatch.setenv("AUTOCOMPACTOR_MODE", "actuate")
    assert config_lib.cfg.str("MODE") == "actuate"


def test_str_top_level(temp_config):
    temp_config({"MODE": "actuate"})
    assert config_lib.cfg.str("MODE") == "actuate"


def test_str_default_when_absent(temp_config):
    temp_config({})
    assert config_lib.cfg.str("MODE", default="advise") == "advise"


def test_local_overlay_merges_over_config(temp_config):
    temp_config(
        {"HARD_PCT": 0.65, "MODE": "actuate", "RESERVE": 40000},
        local={"LLM_MODEL": "site-model", "RESERVE": 50000},
    )
    assert config_lib.cfg.str("LLM_MODEL") == "site-model"
    # local overlay wins key-by-key: RESERVE overridden, MODE survives
    assert config_lib.cfg.float("RESERVE") == 50000
    assert config_lib.cfg.str("MODE") == "actuate"
    assert config_lib.cfg.float("HARD_PCT") == 0.65


def test_str_empty_env_is_deliberate_override(temp_config, monkeypatch):
    """An empty-string env var is a deliberate override (presence wins, not
    truthiness) — e.g. AUTOCOMPACTOR_OBSERVE_ONLY="" clears the configured
    list rather than falling through to it."""
    temp_config({"OBSERVE_ONLY": "error_resolved,tests_pass"})
    monkeypatch.setenv("AUTOCOMPACTOR_OBSERVE_ONLY", "")
    assert config_lib.cfg.str("OBSERVE_ONLY") == ""


def test_list_json_array_env(temp_config, monkeypatch):
    temp_config({})
    monkeypatch.setenv("AUTOCOMPACTOR_TIERS", '["a", "b", "c"]')
    assert config_lib.cfg.list("TIERS") == ["a", "b", "c"]


def test_list_malformed_json_falls_back_to_comma_split(temp_config, monkeypatch):
    """Non-JSON env values degrade to a comma-split list, never raise."""
    temp_config({})
    monkeypatch.setenv("AUTOCOMPACTOR_TIERS", "alpha, beta ,gamma")
    assert config_lib.cfg.list("TIERS") == ["alpha", "beta", "gamma"]
    monkeypatch.setenv("AUTOCOMPACTOR_TIERS", "[not valid json")
    assert config_lib.cfg.list("TIERS") == ["[not valid json"]
    monkeypatch.setenv("AUTOCOMPACTOR_TIERS", "")
    assert config_lib.cfg.list("TIERS") == []


def test_repo_config_ships_pi_actuate(monkeypatch):
    # The shipped config.json must keep Pi in actuate mode: this is the
    # actual regression fix (advise-only behavior in env-less processes).
    for key in list(__import__("os").environ):
        if key.startswith("AUTOCOMPACTOR"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(config_lib, "_CONFIG",
                        str(REPO_ROOT / "config.json"))
    monkeypatch.setattr(config_lib, "_config_cache", None)
    assert config_lib.cfg.str("MODE") == "actuate"


# --- Pi-only flatten: effective-value equivalence + flat-config invariant ---

# Every config key the Pi path reads (pi_bridge, policy, config_lib, TS
# shim). Values are the effective Pi config BEFORE the flatten (pi.*
# overlaid on top-level), captured 2026-06-21 via the Step-2 snapshot.
# The flat config.json must reproduce these exactly — behavior-preserving.
PI_KEYS_FLOAT = {
    "WINDOW": 200000, "RESERVE": 40000,
    "SOFT_PCT": 0.50, "SOFT_PCT_WIDE": 0.40,
    "HARD_PCT": 0.90, "HARD_PCT_WIDE": 0.58,
    "COOLDOWN": 30000, "STALE_FRAC": 0.90,
    "POST_FLOOR": 70000, "MIN_SAVINGS": 30000,
    "DETAIL_MIN_TOKENS": 100000, "DETAIL_COOLDOWN": 75000,
    "ARTIFACT_BUDGET": 1500, "MAX_FULL_PARSE_MB": 8,
}
PI_KEYS_STR = {"MODE": "actuate", "PROFILE": "economy",
               "OBSERVE_ONLY": "error_resolved,tests_pass,idle_gap"}


def _shipped_config(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("AUTOCOMPACTOR"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(config_lib, "_CONFIG", str(REPO_ROOT / "config.json"))
    monkeypatch.setattr(config_lib, "_config_cache", None)


def test_flat_config_preserves_effective_pi_values(monkeypatch):
    _shipped_config(monkeypatch)
    # STALE_FRAC canary: the pi section omitted it, so top-level 0.90 was
    # the effective Pi value; the flat config must carry 0.90 (not revert
    # to pi_bridge's 0.50 default).
    assert config_lib.cfg.float("STALE_FRAC", default=0.50) == 0.90
    # HARD_PCT_WIDE canary: 2026-07-17 CacheLane retune raised wide hard
    # from 0.40 → 0.58 so actuate does not thrash near post-compact residual
    # while CacheLane K-prunes tool bulk on the Pi/:7332 path.
    assert config_lib.cfg.float("HARD_PCT_WIDE", default=-1) == 0.58
    for k, v in PI_KEYS_FLOAT.items():
        assert config_lib.cfg.float(k, default=-1) == v, k
    for k, v in PI_KEYS_STR.items():
        assert config_lib.cfg.str(k, default="") == v, k


def test_no_harness_sections_or_pi_env_prefix():
    data = json.load(open(config_lib._CONFIG))
    assert "claude" not in data and "pi" not in data


# --- Inventory config surface (context-window-analysis bundle) ---
# Pins the new keys added in spec §10: inventory enable flag, dormancy
# age + token thresholds with a deadband/hysteresis margin, probe-staleness
# window, post_floor calibration window, and the static 70000 fallback.
INVENTORY_FLOAT_DEFAULTS = {
    "DORMANT_AGE_TURNS": 20,
    "DORMANT_MIN_TOKENS": 500,
    "DORMANT_TOKEN_THRESHOLD": 30000,
    "DORMANT_DEADBAND": 0.2,
    "PROBE_STALENESS_SECONDS": 14 * 86400,
    "POST_FLOOR_CALIBRATION": 10,
    "POST_FLOOR_FALLBACK": 70000,
}


def test_bool_truthy_env(temp_config, monkeypatch):
    temp_config({"INVENTORY_ENABLED": False})
    for truthy in ("1", "true", "True", "yes", "ON"):
        monkeypatch.setenv("AUTOCOMPACTOR_INVENTORY_ENABLED", truthy)
        assert config_lib.cfg.bool("INVENTORY_ENABLED") is True, truthy
    for falsy in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("AUTOCOMPACTOR_INVENTORY_ENABLED", falsy)
        assert config_lib.cfg.bool("INVENTORY_ENABLED") is False, falsy


def test_bool_config_bool_value_honored(temp_config):
    temp_config({"INVENTORY_ENABLED": True})
    assert config_lib.cfg.bool("INVENTORY_ENABLED") is True
    temp_config({"INVENTORY_ENABLED": False})
    assert config_lib.cfg.bool("INVENTORY_ENABLED") is False


def test_bool_default_when_absent(temp_config):
    temp_config({})
    assert config_lib.cfg.bool("INVENTORY_ENABLED", default=True) is True
    assert config_lib.cfg.bool("INVENTORY_ENABLED", default=False) is False


def test_inventory_float_defaults_in_shipped_config(monkeypatch):
    _shipped_config(monkeypatch)
    for k, v in INVENTORY_FLOAT_DEFAULTS.items():
        assert config_lib.cfg.float(k, default=-1) == v, k


def test_inventory_enabled_defaults_true_in_shipped_config(monkeypatch):
    _shipped_config(monkeypatch)
    assert config_lib.cfg.bool("INVENTORY_ENABLED", default=False) is True


def test_inventory_keys_do_not_collide_with_existing_pi_values(monkeypatch):
    _shipped_config(monkeypatch)
    # Existing compaction thresholds unchanged alongside the new keys.
    assert config_lib.cfg.float("POST_FLOOR") == 70000
    assert config_lib.cfg.float("MIN_SAVINGS") == 30000


def test_inventory_env_overrides_shipped(monkeypatch):
    _shipped_config(monkeypatch)
    monkeypatch.setenv("AUTOCOMPACTOR_DORMANT_AGE_TURNS", "40")
    assert config_lib.cfg.float("DORMANT_AGE_TURNS") == 40
    monkeypatch.setenv("AUTOCOMPACTOR_INVENTORY_ENABLED", "false")
    assert config_lib.cfg.bool("INVENTORY_ENABLED") is False
