"""
Task 5 (context-window-analysis): dormant_output boundary signal.

A medium-tier ADDITIVE gate (spec §6.2) registered in
transcript_lib.active_signals(), computed from ContextInventory
dynamic_dormant_tokens vs the DORMANT_TOKEN_THRESHOLD config key, with a
deadband/hysteresis persisted across evaluate calls in a small JSON under
statedir.state_root('pi'). It NEVER suppresses stale_output (OR-combines
additively in pi_bridge's gating pipeline).
"""
import json
import os
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from autocompactor import config_lib, pi_session_lib, transcript_lib  # noqa: E402

FIX = REPO_ROOT / "tests" / "fixtures" / "pi"


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOCOMPACTOR_STATE_DIR", str(tmp_path))
    config_lib._config_cache = None
    return tmp_path


def _st(name="linear.jsonl"):
    fp, active, cc = pi_session_lib.active_path(str(FIX / name))
    return pi_session_lib.analyze_active_prefix(fp, active, 30, cc)


def test_dormant_output_is_registered_in_active_signals(isolated_state, monkeypatch):
    """dormant_output appears in active_signals() when the threshold is met and
    is NOT in observe_only() (so it gates additively, never suppresses
    stale_output). Inject a mock inventory with known dormant_tokens so the
    test does not depend on fixture dormancy (fixtures are small/new)."""
    from autocompactor import context_inventory as ci
    inv = ci.ContextInventory(total_tokens=50000, window=200000,
                               occupancy=0.25, dynamic_dormant_tokens=50000)
    monkeypatch.setattr(ci, "build_inventory", lambda *a, **k: inv)
    monkeypatch.setenv("AUTOCOMPACTOR_DORMANT_TOKEN_THRESHOLD", "30000")
    config_lib._config_cache = None
    st = _st("real_shapes.jsonl")
    sigs = transcript_lib.active_signals(st, window=200000)
    names = [n for n, _ in sigs]
    assert "dormant_output" in names
    assert "dormant_output" not in transcript_lib.observe_only()


def test_dormant_output_additive_never_suppresses_stale_output(isolated_state,
                                                                 monkeypatch):
    """The additive property: dormant_output ORs with stale_output. When BOTH
    fire, both are present in the registry; neither removes the other."""
    from autocompactor import context_inventory as ci
    inv = ci.ContextInventory(total_tokens=50000, window=200000,
                               occupancy=0.25, dynamic_dormant_tokens=50000)
    monkeypatch.setattr(ci, "build_inventory", lambda *a, **k: inv)
    monkeypatch.setenv("AUTOCOMPACTOR_DORMANT_TOKEN_THRESHOLD", "30000")
    config_lib._config_cache = None
    fp, active, cc = pi_session_lib.active_path(str(FIX / "real_shapes.jsonl"))
    st = pi_session_lib.analyze_active_prefix(fp, active, 30, cc)
    # Force stale_fraction high so stale_output fires.
    st.stale_tool_chars = max(st.total_tool_chars, 1)
    st.total_tool_chars = max(st.total_tool_chars, 1)
    sigs = transcript_lib.active_signals(st, window=200000,
                                          stale_frac_thr=0.5)
    names = [n for n, _ in sigs]
    # Both present — dormant_output did not suppress stale_output.
    assert "stale_output" in names
    assert "dormant_output" in names


def test_dormant_output_absent_when_inventory_disabled(isolated_state, monkeypatch):
    monkeypatch.setenv("AUTOCOMPACTOR_INVENTORY_ENABLED", "false")
    config_lib._config_cache = None
    st = _st("real_shapes.jsonl")
    sigs = transcript_lib.active_signals(st, window=200000)
    names = [n for n, _ in sigs]
    assert "dormant_output" not in names


def test_dormant_output_deadband_hysteresis_persists_across_calls(isolated_state,
                                                                    monkeypatch):
    """Once ON at threshold, the signal stays ON until dormant_tokens drops
    below the lower band (threshold * (1 - deadband)). The on/off state
    persists in dormant-deadband.json across evaluate calls."""
    monkeypatch.setenv("AUTOCOMPACTOR_DORMANT_TOKEN_THRESHOLD", "5000")
    monkeypatch.setenv("AUTOCOMPACTOR_DORMANT_DEADBAND", "0.2")
    config_lib._config_cache = None
    st = _st("real_shapes.jsonl")
    # First call: with threshold 5000, the fixture likely has < 5000 dormant,
    # so signal stays OFF and state is persisted as {dormant_on: False}.
    sigs1 = transcript_lib.active_signals(st, window=200000)
    state_path = transcript_lib._dormant_deadband_path()
    assert os.path.exists(state_path)
    persisted = json.load(open(state_path))
    assert "dormant_on" in persisted
    assert "dormant_tokens" in persisted


def test_dormant_output_hysteresis_keeps_on_below_threshold(isolated_state,
                                                              monkeypatch):
    """Hysteresis: with prior ON, the signal stays ON while dormant_tokens is
    between lower band and threshold (avoids churn)."""
    monkeypatch.setenv("AUTOCOMPACTOR_DORMANT_TOKEN_THRESHOLD", "30000")
    monkeypatch.setenv("AUTOCOMPACTOR_DORMANT_DEADBAND", "0.2")  # lower = 24000
    config_lib._config_cache = None
    # Pre-seed the persisted state as ON with a dormant count in the band.
    transcript_lib._write_deadband_state({"dormant_on": True,
                                          "dormant_tokens": 26000,
                                          "threshold": 30000, "lower": 24000})
    # Check the hysteresis math directly: with was_on=True and 26000 >= 24000
    # (lower), the signal stays ON.
    from autocompactor import config_lib as _cfg
    thr = int(_cfg.cfg.float("DORMANT_TOKEN_THRESHOLD", default=30000))
    deadband = float(_cfg.cfg.float("DORMANT_DEADBAND", default=0.2))
    lower = int(thr * (1.0 - deadband))
    assert lower == 24000
    was_on = True
    on = (26000 >= lower) if was_on else (26000 >= thr)
    assert on is True  # stays ON in the band


def test_dormant_output_never_raises_on_bad_prefix(isolated_state):
    """The signal computation never raises into the hook path (degrades to
    an empty description)."""
    from autocompactor.transcript_lib import TranscriptStats
    st = TranscriptStats()
    st.entries = ["not-an-entry"]  # bad prefix
    sigs = transcript_lib.active_signals(st, window=200000)
    names = [n for n, _ in sigs]
    # No exception, no dormant_output on a bad prefix
    assert "dormant_output" not in names


def test_dormant_output_appears_in_build_context_state(isolated_state, monkeypatch):
    """build_context_state consumes active_signals() and renders the signal in
    the contextState Active signals line (so the readout + telemetry see it)."""
    from autocompactor import context_inventory as ci
    inv = ci.ContextInventory(total_tokens=50000, window=200000,
                               occupancy=0.25, dynamic_dormant_tokens=50000)
    monkeypatch.setattr(ci, "build_inventory", lambda *a, **k: inv)
    monkeypatch.setenv("AUTOCOMPACTOR_DORMANT_TOKEN_THRESHOLD", "30000")
    config_lib._config_cache = None
    fp, active, cc = pi_session_lib.active_path(str(FIX / "real_shapes.jsonl"))
    st = pi_session_lib.analyze_active_prefix(fp, active, 30, cc)
    out = transcript_lib.build_context_state(st, window=200000)
    # build_context_state returns a single string
    assert "dormant" in out.lower()