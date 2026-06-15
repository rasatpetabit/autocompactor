"""
Pins the config_lib resolution order: env (with _WIDE preference on wide
windows) > config.json harness section > config.json top-level > default.
These orderings broke in a73b3a5 (str checked config before env; the
harness section could never override a top-level key; _WIDE keys in
config.json were unreachable) and silently discarded user tuning.
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


def test_float_harness_section_beats_top_level(temp_config):
    temp_config({"HARD_PCT": 0.65, "pi": {"HARD_PCT": 0.90}})
    assert config_lib.cfg.float("HARD_PCT", harness="pi") == 0.90
    assert config_lib.cfg.float("HARD_PCT", harness="claude") == 0.65


def test_float_wide_key_reachable_in_config(temp_config):
    temp_config({"HARD_PCT": 0.90, "HARD_PCT_WIDE": 0.60})
    assert config_lib.cfg.float_windowed("HARD_PCT", 400_000) == 0.60
    assert config_lib.cfg.float_windowed("HARD_PCT", 200_000) == 0.90


def test_float_wide_env_beats_wide_config(temp_config, monkeypatch):
    temp_config({"HARD_PCT_WIDE": 0.60})
    monkeypatch.setenv("AUTOCOMPACTOR_HARD_PCT_WIDE", "0.55")
    assert config_lib.cfg.float_windowed("HARD_PCT", 400_000) == 0.55


def test_str_env_beats_config(temp_config, monkeypatch):
    temp_config({"MODE": "advise", "pi": {"MODE": "actuate"}})
    monkeypatch.setenv("AUTOCOMPACTOR_PI_MODE", "advise")
    assert config_lib.cfg.str("MODE", harness="pi") == "advise"


def test_str_harness_section_beats_top_level(temp_config):
    temp_config({"MODE": "advise", "pi": {"MODE": "actuate"}})
    assert config_lib.cfg.str("MODE", harness="pi") == "actuate"
    assert config_lib.cfg.str("MODE", harness="claude") == "advise"


def test_str_default_when_absent(temp_config):
    temp_config({})
    assert config_lib.cfg.str("MODE", harness="pi", default="advise") == "advise"


def test_local_overlay_merges_over_config(temp_config):
    temp_config(
        {"HARD_PCT": 0.65, "pi": {"MODE": "actuate", "RESERVE": 40000}},
        local={"LLM_MODEL": "site-model", "pi": {"RESERVE": 50000}},
    )
    assert config_lib.cfg.str("LLM_MODEL") == "site-model"
    # harness sections merge key-by-key: local RESERVE wins, MODE survives
    assert config_lib.cfg.float("RESERVE", harness="pi") == 50000
    assert config_lib.cfg.str("MODE", harness="pi") == "actuate"
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


def test_float_wide_and_harness_section_compose(temp_config):
    """_WIDE selection and harness-section precedence compose: a wide window
    picks the harness section's _WIDE key; a narrow one picks its bare key."""
    temp_config({"HARD_PCT": 0.65,
                 "pi": {"HARD_PCT": 0.90, "HARD_PCT_WIDE": 0.60}})
    assert config_lib.cfg.float_windowed("HARD_PCT", 400_000, harness="pi") == 0.60
    assert config_lib.cfg.float_windowed("HARD_PCT", 200_000, harness="pi") == 0.90
    # claude (no section) still reads the top-level bare key
    assert config_lib.cfg.float_windowed("HARD_PCT", 400_000) == 0.65


def test_repo_config_ships_pi_actuate(monkeypatch):
    # The shipped config.json must keep Pi in actuate mode: this is the
    # actual regression fix (advise-only behavior in env-less processes).
    for key in list(__import__("os").environ):
        if key.startswith("AUTOCOMPACTOR"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(config_lib, "_CONFIG",
                        str(REPO_ROOT / "config.json"))
    monkeypatch.setattr(config_lib, "_config_cache", None)
    assert config_lib.cfg.str("MODE", harness="pi") == "actuate"
    assert config_lib.cfg.str("MODE", harness="claude") == "advise"
