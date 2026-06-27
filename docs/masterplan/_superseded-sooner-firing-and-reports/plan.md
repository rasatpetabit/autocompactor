# Sooner Firing + Compaction Reports — Plan

**Spec:** `docs/masterplan/sooner-firing-and-reports/spec.md`
**Date:** 2026-06-26

## Tasks

### T1: Lower SOFT_PCT_WIDE to 0.10
**Files:** `config.json`, `tests/test_pi_bridge.py`
- Change `SOFT_PCT_WIDE` from 0.25 to 0.10 in `config.json`
- Update `test_wide_threshold_below_soft_does_not_recommend` — 200K was below the old soft (234K), now 200K > new soft (96K) so it will recommend. Rewrite to assert 200K is above soft and produces a recommendation (when gating signal present at hard line via hard threshold).
- Update `test_wide_threshold_scales_for_large_context_windows` — reason string contains "234k" (old soft) and "374k" (old hard). New soft is "96k" so the string match needs updating.
**Verification:** `python3 -m pytest tests/ -q`

### T2: Build compactionReport in prepare/reinject
**Files:** `src/autocompactor/pi_bridge.py`
- In `cmd_prepare()`: persist `pre_report` dict in session state alongside `last_compaction_stats`. Fields: ts, trigger, pre_tokens, effective_window, phase, thresholds, occupancy, stale_frac, composition, artifacts, instructions_chars, compaction_count.
- In `cmd_reinject()`: read `pre_report`, compute post state, build `compactionReport` with before/after tokens, reclaimed tokens, pre/post phase, post occupancy, thresholds, composition, artifacts, instructions_chars, next_step, next_step_source, reinject_digest_chars, ts. Return it alongside existing fields. Log a `compaction_report` telemetry event.
**Verification:** `python3 -m pytest tests/test_pi_bridge.py::test_prepare_and_reinject_roundtrip -q` (existing test) + manual bridge invocation

### T3: Surface compactionReport in TS session_compact
**Files:** `src/pi/autocompactor.ts`
- In `session_compact` handler: consume `inj?.compactionReport` and extend the existing post-compaction message builder to include report lines (before/after tokens, phase, stale frac, artifacts, instructions, next step, thresholds).
- Use `withStatsBlock` to append the report to the existing message.
**Verification:** `node --test src/pi/test/extension.test.mjs` (existing TS test suite)

### T4: Detail notice verification (no code change)
- Verify that `DETAIL_MIN_TOKENS` (100k) sits above the new soft boundary (~96k for 1M) so the existing detail notice fires at the right boundary.
- Verify the TS pre-gate comment is updated if needed (no code change, but the comment at line 87-88 says `~0.25` which should update to `~0.10`).
**Verification:** Visual inspection, comment update

## Execution strategy
- T1 first (config change + test fixes) — validates the threshold immediately
- T2 second (report in bridge) — the data plumbing
- T3 third (TS surface) — the UI layer
- T4 last (comment cleanup + verification)
