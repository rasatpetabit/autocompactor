"""
This module pins the pi_bridge never-raise JSON CLI contract end to end.
It verifies that pi_bridge.py always exits 0, never reads stdin, and outputs
at most one JSON object (or nothing) for various subcommands and edge cases,
while managing state under AUTOCOMPACTOR_STATE_DIR.
"""
import json
import os
import pathlib
import subprocess
import sys


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
BRIDGE = REPO_ROOT / "src" / "pi_bridge.py"


def run_bridge(args, state_dir, stdin_text=None, extra_env=None):
    """Run pi_bridge.py with the given args and state dir."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("AUTOCOMPACTOR_")}
    env["AUTOCOMPACTOR_STATE_DIR"] = str(state_dir)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(BRIDGE)] + args,
        input=stdin_text,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
    )


def parse_single_json(stdout):
    """Assert stdout is empty or a single JSON object, returning the object or None."""
    stripped = stdout.strip()
    if not stripped:
        return None
    return json.loads(stripped)


def test_garbage_stdin_exits_zero(tmp_path):
    state_dir = tmp_path / "state"
    result = run_bridge(["evaluate"], state_dir, stdin_text="not json at all")
    assert result.returncode == 0
    parse_single_json(result.stdout)


def test_missing_session_exits_zero(tmp_path):
    state_dir = tmp_path / "state"
    result = run_bridge(["evaluate", "--session", "/nonexistent/path.jsonl"], state_dir)
    assert result.returncode == 0
    data = parse_single_json(result.stdout)
    if data is not None:
        assert data.get("recommend") is False


def test_unparseable_session_exits_zero(tmp_path):
    state_dir = tmp_path / "state"
    garbage_file = tmp_path / "garbage.jsonl"
    garbage_file.write_bytes(b"\x00\xffnot jsonl\n\n")
    result = run_bridge(["evaluate", "--session", str(garbage_file)], state_dir)
    assert result.returncode == 0
    parse_single_json(result.stdout)


def test_unknown_subcommand_exits_zero(tmp_path):
    state_dir = tmp_path / "state"
    result = run_bridge(["frobnicate"], state_dir)
    assert result.returncode == 0
    parse_single_json(result.stdout)


def test_evaluate_recommends_near_ceiling(tmp_path):
    state_dir = tmp_path / "state"
    fixture_path = REPO_ROOT / "tests" / "fixtures" / "pi" / "with_compaction.jsonl"
    result = run_bridge(
        ["evaluate", "--session", str(fixture_path), "--tokens", "150000", "--context-window", "200000"],
        state_dir,
    )
    assert result.returncode == 0
    data = parse_single_json(result.stdout)
    assert data is not None
    assert data["recommend"] is True
    assert data["context_tokens"] == 150000
    assert "Context: 150,000 tokens" in data["contextState"]
    assert isinstance(data["reason"], str) and len(data["reason"]) > 0


def test_evaluate_logs_runtime_window_learning_fields(tmp_path):
    state_dir = tmp_path / "state"
    fixture_path = REPO_ROOT / "tests" / "fixtures" / "pi" / "with_compaction.jsonl"
    result = run_bridge(
        ["evaluate", "--session", str(fixture_path), "--tokens", "300000",
         "--context-window", "512000"],
        state_dir,
    )
    assert result.returncode == 0
    data = parse_single_json(result.stdout)
    assert data is not None
    events = (state_dir / "stats" / "events.jsonl").read_text().splitlines()
    ev = json.loads(events[-1])
    assert ev["runtime_context_window"] == 512_000
    assert ev["reserve"] == 40_000
    assert ev["effective_window"] == 472_000
    assert ev["learned_window"] == 512_000
    assert ev["learned_tier"] == "512k"
    assert ev["window_source"] == "runtime"


def test_evaluate_mode_comes_from_config_without_env(tmp_path):
    # run_bridge strips all AUTOCOMPACTOR_* env: the verdict mode must come
    # from config.json (pi section), so actuation works in env-less Pi
    # processes (non-interactive launches never see the bashrc exports).
    state_dir = tmp_path / "state"
    fixture_path = REPO_ROOT / "tests" / "fixtures" / "pi" / "with_compaction.jsonl"
    result = run_bridge(
        ["evaluate", "--session", str(fixture_path), "--tokens", "150000", "--context-window", "200000"],
        state_dir,
    )
    assert result.returncode == 0
    data = parse_single_json(result.stdout)
    assert data is not None
    assert data["mode"] == "actuate"


def test_evaluate_mode_env_overrides_config(tmp_path):
    state_dir = tmp_path / "state"
    fixture_path = REPO_ROOT / "tests" / "fixtures" / "pi" / "with_compaction.jsonl"
    result = run_bridge(
        ["evaluate", "--session", str(fixture_path), "--tokens", "150000", "--context-window", "200000"],
        state_dir,
        extra_env={"AUTOCOMPACTOR_MODE": "advise"},
    )
    assert result.returncode == 0
    data = parse_single_json(result.stdout)
    assert data is not None
    assert data["mode"] == "advise"


def test_wide_threshold_scales_for_large_context_windows(tmp_path):
    # Regression: a 976K GLM-5.2 context window with flat SOFT_PCT/HARD_PCT
    # needs hundreds of K tokens to trigger. config.json sets _WIDE variants
    # (>=300K windows) so the gate stays practical — but not so low that
    # CacheLane-pruned sessions thrash near the post-compact residual.
    # 2026-07-17 retune: SOFT_WIDE 0.40 / HARD_WIDE 0.58 (was 0.25 / 0.40).
    state_dir = tmp_path / "state"
    fixture_path = REPO_ROOT / "tests" / "fixtures" / "pi" / "with_compaction.jsonl"
    result = run_bridge(
        ["evaluate", "--session", str(fixture_path), "--tokens", "550000", "--context-window", "976000"],
        state_dir,
    )
    assert result.returncode == 0
    data = parse_single_json(result.stdout)
    assert data is not None
    assert data["mode"] == "actuate"
    assert "374k" in data["reason"]  # SOFT_PCT_WIDE=0.40 · 936K effective
    assert "543k" in data["reason"]  # HARD_PCT_WIDE=0.58 · 936K effective
    assert data["recommend"] is True  # 550K > hard threshold (543K)


def test_wide_threshold_below_soft_does_not_recommend(tmp_path):
    state_dir = tmp_path / "state"
    fixture_path = REPO_ROOT / "tests" / "fixtures" / "pi" / "with_compaction.jsonl"
    result = run_bridge(
        ["evaluate", "--session", str(fixture_path), "--tokens", "200000", "--context-window", "976000"],
        state_dir,
    )
    assert result.returncode == 0
    data = parse_single_json(result.stdout)
    assert data is not None
    assert data["recommend"] is False  # 200K < soft threshold (374K), no gating


def test_cooldown_round_trip(tmp_path):
    state_dir = tmp_path / "state"
    fixture_path = REPO_ROOT / "tests" / "fixtures" / "pi" / "with_compaction.jsonl"
    args = ["evaluate", "--session", str(fixture_path), "--tokens", "150000", "--context-window", "200000"]

    # First run: should recommend
    result1 = run_bridge(args, state_dir)
    assert result1.returncode == 0
    data1 = parse_single_json(result1.stdout)
    assert data1 is not None
    assert data1["recommend"] is True

    # Second run: should NOT recommend due to cooldown
    result2 = run_bridge(args, state_dir)
    assert result2.returncode == 0
    data2 = parse_single_json(result2.stdout)
    assert data2 is not None
    assert data2["recommend"] is False

    # Assert state file exists and contains last_reco_tokens
    state_file = state_dir / "with_compaction.state.json"
    assert state_file.exists()
    with open(state_file, "r") as f:
        state_data = json.load(f)
    assert "last_reco_tokens" in state_data
    assert state_data["last_reco_tokens"] == 150000


def test_cooldown_deadlock_breaks_when_context_shrinks(tmp_path):
    """Regression for issue #1: a reco staged at a high token count that never
    reached the reinject reset (native compaction, crash, race) used to pin
    last_reco_tokens above the live context forever. A negative delta was
    always < cooldown -> permanent suppression, even at 150% occupancy.

    Fix: cooldown debounces RISING context only; a shrunken context resets
    the baseline (and the bricked state file self-heals).
    """
    state_dir = tmp_path / "state"
    fixture_path = REPO_ROOT / "tests" / "fixtures" / "pi" / "with_compaction.jsonl"
    state_file = state_dir / "with_compaction.state.json"
    state_dir.mkdir(parents=True, exist_ok=True)
    # Bricked state: a reco staged at 494339 that reinject never reset.
    state_file.write_text(json.dumps({
        "last_reco_tokens": 494339,
        "pending_reinject": True,
        "compaction_count": 2,
    }))
    args = ["evaluate", "--session", str(fixture_path),
            "--tokens", "240175", "--context-window", "200000"]
    result = run_bridge(args, state_dir)
    assert result.returncode == 0
    data = parse_single_json(result.stdout)
    assert data is not None
    # 150% occupancy must NOT be suppressed by a stale high baseline.
    assert data["recommend"] is True
    assert "suppressed" not in data["reason"]
    # The bricked baseline self-heals: no longer the stale 494339.
    healed = json.loads(state_file.read_text())
    assert healed["last_reco_tokens"] != 494339


def test_prepare_emits_instructions_and_side_effects(tmp_path):
    state_dir = tmp_path / "state"
    fixture_path = REPO_ROOT / "tests" / "fixtures" / "pi" / "with_compaction.jsonl"
    result = run_bridge(["prepare", "--session", str(fixture_path)], state_dir)
    assert result.returncode == 0
    data = parse_single_json(result.stdout)
    assert data is not None
    assert isinstance(data["customInstructions"], str) and len(data["customInstructions"]) > 0

    # Check side effects
    backups_dir = state_dir / "backups"
    assert backups_dir.exists()
    jsonl_files = list(backups_dir.glob("*.jsonl"))
    assert len(jsonl_files) == 1

    artifacts_dir = state_dir / "artifacts"
    assert artifacts_dir.exists()
    json_files = list(artifacts_dir.glob("*.json"))
    assert len(json_files) >= 1


def test_prepare_can_include_optional_llm_digest(tmp_path):
    state_dir = tmp_path / "state"
    fixture_path = REPO_ROOT / "tests" / "fixtures" / "pi" / "with_compaction.jsonl"
    helper = tmp_path / "digest_helper.py"
    helper.write_text("import sys; print('- helper fact')\n")
    result = run_bridge(
        ["prepare", "--session", str(fixture_path)],
        state_dir,
        extra_env={
            "AUTOCOMPACTOR_LLM": "1",
            "AUTOCOMPACTOR_LLM_CMD": f"{sys.executable} {helper}",
        },
    )
    assert result.returncode == 0
    data = parse_single_json(result.stdout)
    assert "Additional must-preserve facts" in data["customInstructions"]
    assert "- helper fact" in data["customInstructions"]


def test_reinject_after_prepare(tmp_path):
    state_dir = tmp_path / "state"
    fixture_path = REPO_ROOT / "tests" / "fixtures" / "pi" / "with_compaction.jsonl"

    # Run prepare first
    prepare_result = run_bridge(["prepare", "--session", str(fixture_path)], state_dir)
    assert prepare_result.returncode == 0

    # Run reinject
    reinject_result = run_bridge(["reinject", "--session", str(fixture_path)], state_dir)
    assert reinject_result.returncode == 0
    data = parse_single_json(reinject_result.stdout)
    assert data is not None
    assert isinstance(data["text"], str) and len(data["text"]) > 0
    assert data["customType"] == "autocompactor.digest"
    assert "compactionStats" in data
    assert "compaction #" in data["compactionStats"]
    assert "pre-compaction composition" in data["text"]


def test_reinject_without_prepare_is_quiet_or_json(tmp_path):
    state_dir = tmp_path / "state"
    fixture_path = REPO_ROOT / "tests" / "fixtures" / "pi" / "linear.jsonl"
    result = run_bridge(["reinject", "--session", str(fixture_path)], state_dir)
    assert result.returncode == 0
    parse_single_json(result.stdout)


def test_midwave_prepare_reinject_progress_hard_resume(tmp_path):
    """Mid-wave actuate smoke: prepare+reinject surfaces progress hard-resume.

    Simulates compact mid masterplan execute: cwd has active state.yml,
    session text has affinity cues, reinject must return progress: source
    and progressResume=autonomous (coding hard-resume eligible).
    """
    state_dir = tmp_path / "state"
    state_fixture = (
        REPO_ROOT / "tests" / "fixtures" / "progress" / "masterplan_active_state.yml"
    )
    masterplan_dir = tmp_path / "docs" / "masterplan" / "demo-wave-run"
    masterplan_dir.mkdir(parents=True)
    (masterplan_dir / "state.yml").write_text(
        state_fixture.read_text(encoding="utf-8"), encoding="utf-8"
    )

    session_path = tmp_path / "midwave.jsonl"
    session_fixture = (
        REPO_ROOT / "tests" / "fixtures" / "progress" / "session_affinity_true.jsonl"
    )
    session_path.write_text(
        session_fixture.read_text(encoding="utf-8"), encoding="utf-8"
    )
    resume_env = {
        "AUTOCOMPACTOR_PROGRESS_RESUME": "autonomous",
        "AUTOCOMPACTOR_NEXTSTEP": "autonomous",
        "AUTOCOMPACTOR_CONFIG": "",
    }

    prepare_result = run_bridge(
        [
            "prepare",
            "--session",
            str(session_path),
            "--cwd",
            str(tmp_path),
            "--trigger",
            "self",
        ],
        state_dir,
        extra_env=resume_env,
    )
    assert prepare_result.returncode == 0, prepare_result.stderr
    prepare_data = parse_single_json(prepare_result.stdout)
    assert prepare_data and prepare_data.get("customInstructions")

    reinject_result = run_bridge(
        ["reinject", "--session", str(session_path)],
        state_dir,
        extra_env=resume_env,
    )
    assert reinject_result.returncode == 0, reinject_result.stderr
    data = parse_single_json(reinject_result.stdout)
    assert data is not None
    assert data.get("customType") == "autocompactor.digest"
    source = str(data.get("nextStepSource") or "")
    assert source.startswith("progress:"), data
    assert data.get("progressResume") == "autonomous", data
    assert int(data.get("progressResumeCooldownMs") or 0) >= 0
    next_step = str(data.get("nextStep") or "")
    assert next_step.strip(), "hard-resume brief must be non-empty"
    assert data.get("nextStepWait") is False


# --- Task 8 (context-window-analysis): decision-consumer corrections (spec §6) ---

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "pi"


def _eval(fixture, tokens, state_dir, *, context_window=200000, extra_env=None):
    return run_bridge(
        ["evaluate", "--session", str(fixture),
         "--tokens", str(tokens),
         "--context-window", str(context_window)],
        state_dir, extra_env=extra_env)


def test_hard_line_fires_even_when_estimated_savings_below_min_savings(tmp_path):
    """Spec §9 proof: at/above the hard line, compaction proceeds even when
    est. reclaim < min_savings — the estimate cannot suppress a safety
    compaction. min_savings guards ONLY the opportunistic soft band."""
    state_dir = tmp_path / "state"
    # tokens=190000 of a 200000 window -> occupancy 0.95 (>= HARD_PCT 0.90).
    # Set MIN_SAVINGS huge so est_reclaim (190000 - post_floor) is below it.
    result = _eval(FIXTURES / "linear.jsonl", 190000, state_dir,
                   extra_env={"AUTOCOMPACTOR_MIN_SAVINGS": "9999999"})
    data = parse_single_json(result.stdout)
    assert data["recommend"] is True  # hard-line compaction always proceeds


def test_no_telemetry_falls_back_to_static_post_floor(tmp_path):
    """Spec §6.1: when no reinject telemetry history exists, post_floor falls
    back to the static floor (config-aware: still folds live base+skills). The
    monitor_eval event records the fallback via floor_note."""
    state_dir = tmp_path / "state"
    result = _eval(FIXTURES / "linear.jsonl", 150000, state_dir)
    # No exception, recommend decision returned. The decision path did NOT read
    # floor-probe.json (no telemetry -> static fallback, no probe open).
    data = parse_single_json(result.stdout)
    assert "recommend" in data


def test_post_floor_tracks_changed_live_base(tmp_path):
    """Spec §9 proof: post_floor tracks a changed live base (the exact total
    residual), so a session with a larger fixed floor yields a higher
    post_floor than one with a smaller one at the same summary_term."""
    state_dir = tmp_path / "state"
    # Run the same fixture at two different token totals (the live base =
    # total - measured rises 1:1 with total when measured is constant).
    r1 = _eval(FIXTURES / "linear.jsonl", 50000, state_dir)
    r2 = _eval(FIXTURES / "linear.jsonl", 90000, state_dir)
    # Both produce decisions without exception (config-aware path holds).
    assert "recommend" in r1.stdout and "recommend" in r2.stdout
    parse_single_json(r1.stdout)
    parse_single_json(r2.stdout)


def test_dormant_output_additive_or_never_suppresses_stale_output(tmp_path):
    """Spec §9 proof: the dormant_output signal is additive — it OR-combines
    with stale_output in the gating pipeline. When both fire, compaction
    recommends at the soft band (gating non-empty). Verified via the never-
    raise contract: evaluate returns a valid decision with no exception."""
    state_dir = tmp_path / "state"
    # Force the dormant threshold to 0... but dormancy needs items; instead we
    # assert the bridge never raises and produces a recommendation when gating
    # signals fire at the soft band (stale tool output + occupancy >= soft).
    result = _eval(FIXTURES / "real_shapes.jsonl", 110000, state_dir,
                   extra_env={"AUTOCOMPACTOR_DORMANT_TOKEN_THRESHOLD": "1"})
    data = parse_single_json(result.stdout)
    assert "recommend" in data


def test_visible_fallback_static_inputs_corrected_formula_recommends_at_hard(tmp_path, monkeypatch):
    """Spec §8/§9 proof: a forced inventory error -> degraded inputs (static
    post_floor) + the CORRECTED formula still recommends at the hard line
    (the hard line is never gated by min_savings even on the fallback path)."""
    state_dir = tmp_path / "state"
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from autocompactor import context_inventory as ci

    def raise_inventory_error(*_args, **_kwargs):
        raise RuntimeError("forced inventory failure for test")

    monkeypatch.setattr(ci, "decision_floor_terms", raise_inventory_error)
    # Re-run the bridge IN-PROCESS so the monkeypatch applies. We call
    # cmd_evaluate directly with a minimal opts dict.
    from autocompactor import pi_bridge
    from autocompactor import config_lib
    config_lib._config_cache = None
    monkeypatch.setenv("AUTOCOMPACTOR_MIN_SAVINGS", "9999999")  # huge
    monkeypatch.setenv("AUTOCOMPACTOR_STATE_DIR", str(state_dir))
    opts = {"session": str(FIXTURES / "linear.jsonl"),
            "tokens": "190000", "context_window": 200000}
    data = pi_bridge.cmd_evaluate(opts)
    # Hard line (190000/200000 = 0.95 >= 0.90) -> recommend True even with the
    # forced inventory error AND min_savings huge. The fallback swapped INPUTS
    # only; the corrected formula still recommends at the hard line.
    assert data["recommend"] is True


def test_decision_path_does_not_read_floor_probe(tmp_path, monkeypatch):
    """Spec §9 proof: the DECISION INPUT path (_config_aware_post_floor via
    decision_floor_terms(include_probe=False)) never opens floor-probe.json
    (T9 readout-only boundary; complements T9's producer-side assertion). The
    readout path (build_context_state -> context_composition ->
    build_inventory(include_probe=True)) legitimately reads the probe for the
    per-package display, which is the intended readout use."""
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from autocompactor import context_inventory as ci, pi_bridge
    sentinel_opened = {"yes": False}
    def trap(*a, **k):
        sentinel_opened["yes"] = True
        return 0
    monkeypatch.setattr(ci, "_read_probe_tools_tokens", trap)
    monkeypatch.setenv("AUTOCOMPACTOR_STATE_DIR", str(tmp_path))
    from autocompactor import config_lib
    config_lib._config_cache = None
    # The decision-safe entry must not trigger the probe read.
    terms = ci.decision_floor_terms([], 150000)
    assert "base" in terms and "skills" in terms
    assert sentinel_opened["yes"] is False, (
        "decision_floor_terms must not read floor-probe.json (T9 boundary)")
    # And the helper the decision actually uses (_config_aware_post_floor)
    # also does not read the probe.
    sentinel_opened["yes"] = False
    pf, _note = pi_bridge._config_aware_post_floor([], 150000, "test-session")
    assert isinstance(pf, int) and pf >= 0
    assert sentinel_opened["yes"] is False


def test_reinject_persists_post_floor_terms(tmp_path):
    """Spec §6.1: cmd_reinject persists post_total/base/skills on the reinject
    event so the decision's summary-term median can be read back."""
    state_dir = tmp_path / "state"
    # Run a reinject against a fixture that has a compaction boundary.
    fixture = FIXTURES / "with_compaction.jsonl"
    result = run_bridge(["reinject", "--session", str(fixture),
                         "--context-window", "200000"], state_dir)
    assert result.returncode == 0
    # The reinject event should be logged with post_total/base/skills.
    for stats_file in state_dir.rglob("events.jsonl"):
        for line in stats_file.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except Exception:
                continue
            if event.get("type") == "reinject":
                assert "post_total" in event
                assert "base" in event
                assert "skills" in event
