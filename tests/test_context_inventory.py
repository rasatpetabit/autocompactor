"""
Pins context_inventory.py (context-window-analysis Task 3):
 - the ContextInventory model + stable named fields (the contract T4/T5/T8 build against)
 - the decision-safe entry decision_floor_terms() never opens floor-probe.json
 - the never-raise fallback is self-contained (does NOT call
   transcript_lib.context_composition — would infinitely recurse via T4)
 - the standalone --inventory report renders a token-count line over a fixture
   session (not just the argparse --help smoke)
 - ReclaimEstimate.ranking consumed verbatim, never re-ranked/recomputed
"""
import json
import os
import pathlib
import sys
import tempfile

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from autocompactor import context_inventory as ci  # noqa: E402
from autocompactor import pi_session_lib  # noqa: E402

FIX = REPO_ROOT / "tests" / "fixtures" / "pi"


def _active(name="linear.jsonl"):
    full, active, cc = pi_session_lib.active_path(str(FIX / name))
    return full, active, cc


# --- API contract: dataclasses have the stable named fields ----------------

def test_context_item_fields():
    it = ci.ContextItem(kind="tool_result", tool_name="read", tokens=10,
                        age_turns=3, last_read_turn=1)
    for f in ("kind", "tool_name", "tokens", "age_turns", "last_read_turn",
              "dormant", "redundant", "reclaimable"):
        assert hasattr(it, f), f


def test_floor_breakdown_fields():
    for f in ("context_files", "skills_meta", "tools_system", "true_residual"):
        assert hasattr(ci.FloorBreakdown(), f)


def test_context_inventory_fields():
    for f in ("total_tokens", "window", "occupancy", "floor", "dynamic",
              "dynamic_dormant_tokens", "categories", "reclaim",
              "degraded", "note"):
        assert hasattr(ci.ContextInventory(), f)


def test_reclaim_estimate_fields():
    r = ci.ReclaimEstimate()
    assert hasattr(r, "reclaimable_now") and hasattr(r, "post_floor_estimate")
    assert hasattr(r, "ranking") and r.ranking == []


# --- build_inventory end-to-end over a fixture ------------------------------

def test_build_inventory_non_degraded_over_fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOCOMPACTOR_STATE_DIR", str(tmp_path))
    _, active, _ = _active()
    inv = ci.build_inventory(active, 50000, 200000)
    assert inv.degraded is False
    assert inv.total_tokens == 50000
    assert inv.window == 200000
    assert 0.0 < inv.occupancy < 1.0
    assert isinstance(inv.floor, ci.FloorBreakdown)
    assert inv.dynamic and all(isinstance(it, ci.ContextItem) for it in inv.dynamic)
    assert inv.dynamic_dormant_tokens >= 0
    assert set(inv.categories) == {"tool", "assistant", "prompts", "summary"}
    # Floor reconciles to the total: context_files + skills_meta + tools_system
    # + true_residual == total (when no probe data, tools_system=0).
    f = inv.floor
    assert f.context_files + f.skills_meta + f.tools_system + f.true_residual == inv.total_tokens


def test_build_inventory_with_probe_reads_tools_system(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOCOMPACTOR_STATE_DIR", str(tmp_path))
    probe = {"per_package": {"pi-subagents": 11229, "context-mode": 10973},
             "measured_at": "2026-06-09T00:00:00Z", "pi_version": "x",
             "staleness_budget": 1209600}
    (tmp_path / "floor-probe.json").write_text(json.dumps(probe))
    _, active, _ = _active()
    inv = ci.build_inventory(active, 50000, 200000, include_probe=True)
    assert inv.floor.tools_system == 11229 + 10973


def test_include_probe_false_never_reads_probe(tmp_path, monkeypatch):
    """The decision-safe path must NOT open floor-probe.json (T9 boundary)."""
    monkeypatch.setenv("AUTOCOMPACTOR_STATE_DIR", str(tmp_path))
    sentinel = tmp_path / "floor-probe.json"
    sentinel.write_text(json.dumps({"per_package": {"x": 999999}}))
    _, active, _ = _active()
    inv = ci.build_inventory(active, 50000, 200000, include_probe=False)
    assert inv.floor.tools_system == 0  # probe never read
    # And the dedicated decision entry returns live base+skills without the probe.
    terms = ci.decision_floor_terms(active, 50000)
    assert "base" in terms and "skills" in terms and terms["base"] >= 0
    assert terms["skills"] >= 0


def test_decision_floor_terms_base_is_live_residual(tmp_path, monkeypatch):
    """base = total - measured(skills + context_files) tracks the live floor,
    config-correct for THIS session (spec §6). Changing the total moves base 1:1."""
    monkeypatch.setenv("AUTOCOMPACTOR_STATE_DIR", str(tmp_path))
    _, active, _ = _active()
    t1 = ci.decision_floor_terms(active, 50000)
    t2 = ci.decision_floor_terms(active, 72000)
    # base rises 1:1 with total when measured is constant (no telemetry/probe)
    assert (t2["base"] - t1["base"]) == (72000 - 50000)


# --- never-raise fallback is self-contained (no recursion into context_composition) --

def test_never_raise_on_bad_prefix_returns_degraded():
    inv = ci.build_inventory("not-a-list", 50000, 200000)  # bad prefix type
    assert inv.degraded is True
    assert inv.total_tokens == 50000
    assert inv.floor.true_residual == 50000
    assert inv.dynamic == []


def test_fallback_does_not_call_context_composition(monkeypatch):
    """Self-contained fallback MUST NOT call transcript_lib.context_composition
    (T4 turns that into an adapter over this module — would infinitely recurse)."""
    import autocompactor.transcript_lib as tl
    called = {"n": 0}
    orig = tl.context_composition

    def trap(*a, **k):
        called["n"] += 1
        return {}

    monkeypatch.setattr(tl, "context_composition", trap)
    inv = ci.build_inventory("not-a-list", 50000, 200000)
    assert inv.degraded is True
    assert called["n"] == 0
    # restore (monkeypatch does this automatically, but be explicit for clarity)
    tl.context_composition = orig


# --- ReclaimEstimate.ranking consumed verbatim, no re-rank/recompute --------

def test_render_report_consumes_ranking_verbatim():
    inv = ci.ContextInventory(
        total_tokens=50000, window=200000, occupancy=0.25,
        floor=ci.FloorBreakdown(true_residual=50000),
        dynamic=[], dynamic_dormant_tokens=0,
        categories={"tool": 0, "assistant": 0, "prompts": 0, "summary": 0},
        reclaim=ci.ReclaimEstimate(
            reclaimable_now=1234, post_floor_estimate=70000,
            ranking=[{"bucket": "unload pi-subagents", "tokens": 11229,
                      "reducible_by": "unload package"},
                     {"bucket": "stale tool output", "tokens": 900,
                      "reducible_by": "/compact"}],
        ),
    )
    out = ci.render_report(inv)
    assert "Context inventory" in out
    assert "50kt" in out  # token-count line renders (compact size)
    assert "unload pi-subagents" in out
    assert "11k t" in out.replace("\n", " ") or "11k" in out  # ranking figures
    assert "70k t" in out.replace("\n", " ") or "70k" in out  # post_floor
    # ranking order preserved (pi-subagents 11229 before stale 900)
    assert out.index("unload pi-subagents") < out.index("stale tool output")


def test_report_over_fixture_renders_token_count_line():
    """The standalone --inventory report OUTPUT (not just --help): runs the
    report over a fixture session and asserts a token-count line renders."""
    _, active, _ = _active("with_compaction.jsonl")
    inv = ci.build_inventory(active, 60000, 200000)
    out = ci.render_report(inv)
    assert "Context inventory" in out
    assert "60kt" in out  # token-count line present (compact size)
    assert "FLOOR" in out
    assert "DYNAMIC" in out


def test_main_runs_report_over_fixture(tmp_path, capsys, monkeypatch):
    """main() over a fixture session prints the report (not just --help)."""
    monkeypatch.setenv("AUTOCOMPACTOR_STATE_DIR", str(tmp_path))
    rc = ci.main(["--session=" + str(FIX / "linear.jsonl"), "--total=50000",
                  "--window=200000"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "Context inventory" in captured.out
    assert "50kt" in captured.out


# --- main()/CLI smoke (argparse path) ---------------------------------------

def test_main_help_returns_zero(capsys):
    rc = ci.main(["--help"])
    assert rc == 0


# --- no-probe fallback bucket (honest single 'tools+system (fixed)') --------

def test_no_probe_falls_back_to_honest_bucket(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOCOMPACTOR_STATE_DIR", str(tmp_path))
    # no floor-probe.json in the state dir
    _, active, _ = _active()
    inv = ci.build_inventory(active, 50000, 200000, include_probe=True)
    assert inv.floor.tools_system == 0
    # everything unattributed lives in true_residual (the honest bucket)
    assert inv.floor.true_residual > 0
    assert inv.floor.context_files + inv.floor.skills_meta + inv.floor.true_residual == 50000


def test_probe_over_reports_clamps_to_total(tmp_path, monkeypatch):
    """When a stale probe reports tools_system LARGER than what fits under the
    live total, the readout floor clamps tools_system so the parts still sum
    to the total and true_residual stays >= 0 (the honesty bucket never hides
    estimate error). The decision side is unaffected (it uses base = total -
    measured, never the probe)."""
    monkeypatch.setenv("AUTOCOMPACTOR_STATE_DIR", str(tmp_path))
    probe = {"per_package": {"heavy": 999999},
             "measured_at": "2026-06-09T00:00:00Z",
             "pi_version": "x", "staleness_budget": 1209600}
    (tmp_path / "floor-probe.json").write_text(json.dumps(probe))
    _, active, _ = _active()
    inv = ci.build_inventory(active, 50000, 200000, include_probe=True)
    f = inv.floor
    assert f.context_files + f.skills_meta + f.tools_system + f.true_residual == 50000
    assert f.true_residual >= 0
    # tools_system clamped down from 999999 to fit the headroom
    assert f.tools_system < 999999