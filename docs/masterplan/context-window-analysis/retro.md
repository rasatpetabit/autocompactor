# Retro — context-window-analysis

**Date:** 2026-06-25 · **Bundle:** `context-window-analysis` · **Outcome:** all 10 tasks / 5 waves landed green

## What we built

A **unified ContextInventory** layer (consumer-agnostic) that itemizes what
occupies the Pi context window below the category level, feeding both a richer
readout and the compaction decision. This continues the thread of commit
`55cdfef` (category-level accounting) by going *below* the category level.

- **`context_inventory.py`** (new): the `ContextInventory` model
  (`ContextItem` / `FloorBreakdown` / `ReclaimEstimate`) with `build_inventory()`
  (dynamic per-item ledger with dormancy/redundancy/reclaimability; live-measured
  context_files/skills_meta; probe-decomposed tools_system with no-probe fallback
  + over-report clamping) and the decision-safe `decision_floor_terms()` that
  never opens `floor-probe.json` (T9 boundary). Never-raise fallback is
  self-contained (never recurses into `context_composition`).
- **`transcript_lib.context_composition()`** refactored into a thin adapter over
  the inventory: the existing 55cdfef keys are byte-identical (golden-baseline
  parity over all 7 fixtures), new inventory keys additive on top. Recursion
  break: on inventory failure falls back to frozen `_legacy_composition()` only.
- **`dormant_output` signal** registered in `active_signals()`: medium-tier
  additive gate (never suppresses `stale_output`), computed from
  `dynamic_dormant_tokens` vs `DORMANT_TOKEN_THRESHOLD` with deadband/hysteresis
  persisted across evaluate calls.
- **`policy.composition_detail_lines()`** extended to render the inventory
  fields verbatim (floor decomposition + per-item highlights + dormant rollup);
  **`reducible_floor_advisory()`** surfaces `ReclaimEstimate.ranking` as
  user-actionable guidance (framed "unload the package; /compact cannot do this").
- **`nightly_eval.py` floor probe**: readout-only per-package tool-schema cost
  via the isolation-spawn method (`pi --mode json -p` first-request token diffs),
  writing `floor-probe.json` with the frozen schema. Observational only.
- **`install_pi.py --status`** surfaces floor-probe freshness (read-only consumer).
- **`pi_bridge.py` decision corrections (spec §6)**: config-aware `post_floor`
  (live `base`+`skills` from `decision_floor_terms` + telemetry `summary_term`
  median; persisted on the reinject event); the **guard correction** —
  `min_savings` suppresses ONLY the soft band, never the hard line (the
  cross-vendor-fought safety property); `dormant_output` consumed additively via
  the existing pipeline. Fallback swaps INPUTS only, never the formula.

## What worked

- The adversarial review pass (3 rounds) was load-bearing — it caught the
  recursion path, the suppress-at-hard bug, the stale-median-cache, and the
  "estimates never flip" false claim. The **band-limited** property (exact hard
  line + native ceiling guarantee the safety compaction; estimates only shift
  opportunistic soft-band timing) is the honest, provable guarantee.
- Golden-baseline parity test (baked-in, over all 7 fixtures) caught nothing
  during the refactor but is a real regression guard — not a vacuous new-vs-new
  self-comparison.
- File-disjoint wave packing let the scope guard catch genuine drift (T10 test
  file, T9 test refinement) and revert it, forcing explicit plan-index updates.
- Per-package probe via injected `spawn` kept the heavy isolation-spawn testable
  without spawning `pi` in unit tests.

## What hurt

- The masterplan shell's edit tool kept mangling multi-line Python strings
  through the shell layer (em-dashes, quotes); had to fall back to heredoc-with-
  Python-block insertion repeatedly. Cost time but no correctness impact.
- `python` vs `python3` in the verify commands: `python` isn't on PATH; the
  suite only passes under `python3`. A latent fragility for any operator running
  the smoke canary on a host where `python` aliases python2 or is absent.
- The scope guard reverts are correct but create rework: the T9
  observational-only test had to be refined when T8 added boundary *comments* to
  `pi_bridge` (the strict source-scan was too brittle for explanatory comments).
  Re-applied and committed directly.

## Residual risks (monitor, not block)

- chars/4 soft-band noise in `dormant_tokens` (damped by deadband + additive-only).
- `summary_term` drift by workload/model (the median is workload-coupled).
- `cooldown` semantics at the hard line are untested together at scale.
- The floor probe's default `_default_probe_packages()` recipe is a placeholder
  toggle (yields ~0 diff); a real nightly run needs curated per-package
  `PI_CODING_AGENT_DIR` dirs to actually attribute per-package cost.

## Tests

201 passing (baseline 100 → +101 across the 10 tasks). The 12 verify commands
all green; full suite green.