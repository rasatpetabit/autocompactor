"""
Parity test: context_composition() (context-window-analysis Task 4).

The EXISTING 55cdfef keys (total/base/skills/skill_names/summary/tool/
tool_stale_frac/tool_breakdown/assistant/prompts) must remain byte-identical
after the adapter refactor — pinned against a GOLDEN BASELINE captured from
main BEFORE the refactor (NOT a vacuous new-vs-new self-comparison). The new
inventory fields (inventory_floor/dynamic_ledger/dormant_tokens/reclaim) are
additive on top.

Golden baseline captured 2026-06-25 from the pre-refactor main, over all 7
fixture sessions at context_tokens=60000. If this baseline drifts, the adapter
broke parity and the readout regressed.
"""
import json
import os
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from autocompactor import pi_session_lib, transcript_lib  # noqa: E402

FIX = REPO_ROOT / "tests" / "fixtures" / "pi"

# Golden baseline: pre-refactor main output, context_tokens=60000, every
# fixture. Captured 2026-06-25 before the Task-4 adapter landed. Any change
# here is a parity regression that must be justified, not silent.
GOLDEN_BASELINE = {
    "branched.jsonl": {"assistant": 107, "base": 59813, "prompts": 30,
                       "skill_names": [], "skills": 0, "summary": 0, "tool": 50,
                       "tool_breakdown": [
                           {"name": "grep", "stale_frac": 0.0, "tokens": 17},
                           {"name": "read", "stale_frac": 0.0, "tokens": 12},
                           {"name": "find", "stale_frac": 0.0, "tokens": 9},
                           {"name": "write", "stale_frac": 0.0, "tokens": 9},
                           {"name": "edit", "stale_frac": 0.0, "tokens": 4}],
                       "tool_stale_frac": 0.0, "total": 60000},
    "initial_prompts.jsonl": {"assistant": 2, "base": 59974, "prompts": 24,
                              "skill_names": [], "skills": 0, "summary": 0,
                              "tool": 0, "tool_breakdown": [],
                              "tool_stale_frac": 0.0, "total": 60000},
    "linear.jsonl": {"assistant": 46, "base": 59892, "prompts": 14,
                     "skill_names": [], "skills": 0, "summary": 0, "tool": 48,
                     "tool_breakdown": [
                         {"name": "read", "stale_frac": 0.0, "tokens": 25},
                         {"name": "grep", "stale_frac": 0.0, "tokens": 23}],
                     "tool_stale_frac": 0.0, "total": 60000},
    "no_usage.jsonl": {"assistant": 2, "base": 59998, "prompts": 0,
                      "skill_names": [], "skills": 0, "summary": 0, "tool": 0,
                      "tool_breakdown": [], "tool_stale_frac": 0.0,
                      "total": 60000},
    "parallel_tools.jsonl": {"assistant": 4, "base": 59991, "prompts": 1,
                             "skill_names": [], "skills": 0, "summary": 0,
                             "tool": 4,
                             "tool_breakdown": [
                                 {"name": "bash", "stale_frac": 0.0, "tokens": 2},
                                 {"name": "read", "stale_frac": 0.0, "tokens": 2}],
                             "tool_stale_frac": 0.0, "total": 60000},
    "real_shapes.jsonl": {"assistant": 99, "base": 59834, "prompts": 15,
                          "skill_names": [], "skills": 0, "summary": 0,
                          "tool": 52,
                          "tool_breakdown": [
                              {"name": "read", "stale_frac": 0.0, "tokens": 26},
                              {"name": "grep", "stale_frac": 0.0, "tokens": 14},
                              {"name": "bash", "stale_frac": 0.0, "tokens": 11}],
                          "tool_stale_frac": 0.0, "total": 60000},
    "with_compaction.jsonl": {"assistant": 81, "base": 59818, "prompts": 11,
                              "skill_names": [], "skills": 0, "summary": 46,
                              "tool": 44,
                              "tool_breakdown": [
                                  {"name": "bash", "stale_frac": 0.0, "tokens": 31},
                                  {"name": "write", "stale_frac": 0.0, "tokens": 10},
                                  {"name": "edit", "stale_frac": 0.0, "tokens": 4}],
                              "tool_stale_frac": 0.0, "total": 60000},
}

LEGACY_KEYS = ["total", "base", "skills", "skill_names", "summary", "tool",
               "tool_stale_frac", "tool_breakdown", "assistant", "prompts"]
NEW_KEYS = ["inventory_floor", "dynamic_ledger", "dormant_tokens",
            "reclaim", "inventory_degraded"]


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """Hermetic state dir so a real floor-probe.json does not leak into the
    floor decomposition (the readout probe is environment-coupled)."""
    monkeypatch.setenv("AUTOCOMPACTOR_STATE_DIR", str(tmp_path))


@pytest.mark.parametrize("name", sorted(GOLDEN_BASELINE))
def test_legacy_keys_match_golden_baseline_byte_identical(name):
    """The pre-refactor keys reproduce the golden baseline EXACTLY (real
    regression guard, not a vacuous self-comparison)."""
    fp, active, cc = pi_session_lib.active_path(str(FIX / name))
    st = pi_session_lib.analyze_active_prefix(fp, active, 30, cc)
    comp = transcript_lib.context_composition(st, 60000)
    expected = GOLDEN_BASELINE[name]
    for k in LEGACY_KEYS:
        assert k in comp, f"{name}: missing legacy key {k}"
        assert comp[k] == expected[k], (f"{name}: legacy key {k} drifted: "
                                        f"{comp[k]!r} != {expected[k]!r}")


@pytest.mark.parametrize("name", sorted(GOLDEN_BASELINE))
def test_new_inventory_keys_are_additive(name):
    """The Task-4 adapter adds the inventory fields on top of the legacy keys,
    without removing or renaming any legacy key."""
    fp, active, cc = pi_session_lib.active_path(str(FIX / name))
    st = pi_session_lib.analyze_active_prefix(fp, active, 30, cc)
    comp = transcript_lib.context_composition(st, 60000)
    # New keys present
    for k in NEW_KEYS:
        assert k in comp, f"{name}: missing new key {k}"
    # inventory_floor shape
    fl = comp["inventory_floor"]
    for k in ("context_files", "skills_meta", "tools_system", "true_residual"):
        assert k in fl, f"{name}: inventory_floor missing {k}"
    # Floor reconciles to total: parts sum back (probe clamped when over-report).
    assert (fl["context_files"] + fl["skills_meta"] + fl["tools_system"]
            + fl["true_residual"] == comp["total"]), name
    # dynamic_ledger entries have the stable named fields
    for it in comp["dynamic_ledger"]:
        for k in ("kind", "tool_name", "tokens", "age_turns", "dormant",
                  "redundant", "reclaimable"):
            assert k in it, f"{name}: dynamic_ledger item missing {k}"
    # reclaim shape
    r = comp["reclaim"]
    for k in ("reclaimable_now", "post_floor_estimate", "ranking"):
        assert k in r, f"{name}: reclaim missing {k}"


def test_adapter_falls_back_to_legacy_on_inventory_failure(monkeypatch):
    """Spec §8 recursion break: if build_inventory raises, the adapter returns
    the legacy keys (its own frozen _legacy_composition) and NEVER re-enters the
    inventory. The fallback is observable via inventory_degraded=True."""
    import autocompactor.context_inventory as ci
    fp, active, cc = pi_session_lib.active_path(str(FIX / "linear.jsonl"))
    st = pi_session_lib.analyze_active_prefix(fp, active, 30, cc)

    def boom(*a, **k):
        raise RuntimeError("forced inventory failure")

    monkeypatch.setattr(ci, "build_inventory", boom)
    comp = transcript_lib.context_composition(st, 60000)
    assert comp["inventory_degraded"] is True
    # Legacy keys still present and correct (parity holds even on fallback)
    assert comp["total"] == 60000
    assert comp["tool"] == 48
    # No new inventory figures synthesized on the degraded path
    assert "dynamic_ledger" not in comp
    assert "inventory_floor" not in comp


def test_legacy_composition_helper_is_frozen():
    """_legacy_composition is the preserved pre-refactor body, kept for the
    never-raise fallback. It produces exactly the legacy keys (no new keys)."""
    fp, active, cc = pi_session_lib.active_path(str(FIX / "linear.jsonl"))
    st = pi_session_lib.analyze_active_prefix(fp, active, 30, cc)
    comp = transcript_lib._legacy_composition(st, 60000)
    assert set(comp) == set(LEGACY_KEYS)
    assert comp["tool"] == 48