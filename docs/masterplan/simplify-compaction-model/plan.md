# Plan: simplify compaction policy to three knobs

Date: 2026-06-16
Status: brainstorm/masterplan draft (revised after GPT-5.5 advisor pass — see review.md)

> **Revision summary:** reordered so investigation/validation precede any behavior or docs change. Added Workstream 0 (miss attribution), an implementation-traps workstream, and explicit notes on the Pi TS pre-gate and `policy.py` scope. Success is measured by `auto_unwarned`/lead tokens, not key count.

## Workstream 0 — miss attribution (do this first)  ★

> **Advisor finding #1 (CRITICAL).** The actual pain is not config verbosity; it is Claude auto-compactions arriving with no advance recommendation. A profile rename will not fix it. Diagnose first.

1. For every unwarned auto-compaction in recent transcripts, record:
   - last prior `monitor_eval`: context, occupancy, effective window, suppression state, `recommended`, `suppressed_by_cooldown`;
   - the time gap and token gap between that eval and the native auto;
   - whether a `UserPromptSubmit` (Claude) / `agent_end` (Pi) fired at all in between;
   - whether the hook ran and exited 0 (vs absent/crashed);
   - the effective window used vs the real auto trigger.
2. Bucket misses: cooldown-suppressed, wrong-effective-window, hook-not-fired, hook-crashed, one-turn-context-jump (no prompt between crossing and auto), below-min-reclaim.
3. Decide per bucket whether a *policy* fix, a *timing* fix, or a *hook-reliability* fix is needed.

Deliverable: a short report (`docs/masterplan/simplify-compaction-model/miss-attribution.md`) naming the dominant bucket(s) and the smallest fix for each.

## Workstream A — policy inventory and naming

1. List every config key read by runtime code (Python **and** the Pi TS shim) and classify it: public intent / advanced operational / deprecated override / internal derived constant.
2. Add a docs table mapping old keys → new model.
3. Canonical names: `PROFILE`, `MAX_CONTEXT_TOKENS`, `MODE`. Default profile: `balanced`.
4. Decide the versioned profile→constants table that `--status` will surface.

Deliverable: a single map from which `config.json` and docs are derived.

## Workstream B — central `policy.py` (rule, not adapters)

> **Advisor finding #4.** Centralize the decision rule only. Adapters keep state, staging, actuation.

1. Create `src/autocompactor/policy.py`.
2. Move decision inputs/outputs into dataclasses (`PolicyInput`, `PolicyDecision`).
3. `resolve_policy_config(harness, runtime_window=None)`: read `PROFILE`/`MAX_CONTEXT_TOKENS`/`MODE`; apply deprecated old-key overrides if present; derive thresholds/cooldown/min-reclaim/stale settings.
4. `decide_compaction(input)`: the rule in spec.md. **Keep binary gating for now** (advisor finding #3). Correct `burn_rate` to medium (it is predictive).
5. Return all telemetry fields existing reports need.

Deliverable: policy logic unit-testable without Claude/Pi plumbing.

## Workstream C — parity tests before any adapter switch

> **Advisor finding #5.** Validate before swapping.

1. Fixture-based parity tests: given identical resolved inputs, `policy.decide()` matches the *current* `context_monitor` decision and the *current* `pi_bridge cmd_evaluate` decision, across a matrix of (context_tokens, window, signals, cooldown state, min-reclaim).
2. Freeze current behavior as golden tests so the migration cannot drift silently.

Deliverable: green parity suite; `policy.py` proven equivalent to today.

## Workstream D — switch adapters one at a time

1. `context_monitor.py`: gather inputs → call policy → handle Claude staging/reinject. **Highest-risk step** — Claude's only advisory moment is `UserPromptSubmit`; if it misses that boundary, no policy can recover. Add the Workstream-0 fixes first.
2. `pi_bridge.py cmd_evaluate`: gather Pi runtime facts → call the same policy → return Pi JSON.
3. **Pi TS pre-gate (advisor finding #6):** `src/pi/autocompactor.ts:55-58,124-127` reads `SOFT_PCT/MIN_SAVINGS/POST_FLOOR/COOLDOWN` locally and bypasses the bridge. Either export the policy constants in a form the TS pre-gate can consume, or have the pre-gate delegate to the bridge. A Python-only `policy.py` does not centralize this path on its own.
4. Keep adapter-specific capabilities (Claude advisory-only; Pi actuate) outside policy.

Deliverable: one decision rule, two adapters, both switched safely.

## Workstream E — implementation traps (explicit)

> **Advisor finding #6.** These have tests and incident history; handle deliberately.

1. **State schema:** `last_reco_tokens`, `pending_reinject`, `peak_ctx` — preserve semantics; migration must not bricked-cooldown-reset or lose the peak durability anchor (`tests/test_autocompactor.py:679`).
2. **`_WIDE` compatibility:** `HARD_PCT_WIDE`/windowed reads (`float_windowed`) must remain honored during deprecation.
3. **`native_ceiling_blocks_learned_window`:** preserve the Claude/Pi asymmetry that is intentional (`window_resolver.py`).
4. **Pi reserve semantics:** `contextWindow - reserve` is authoritative; cap-only.
5. **Hidden policy constants inside signals:** e.g. `burn_rate` hardcodes `0.85` at `transcript_lib.py:441`. Move such constants into the versioned profile table or document them.
6. **Pi TS pre-gate:** see Workstream D step 3.

Deliverable: a trap checklist ticked before each adapter switch.

## Workstream F — old-vs-new backtest (gate before docs/defaults)

1. Extend `analyze_corpus.py` with old-vs-new comparison: old recommendation token vs new recommendation token, lead time, false-positive proxy, **`auto_unwarned` count**.
2. Run over recent transcripts; require `auto_unwarned` not to regress vs Workstream-0 baseline.
3. Update nightly report: profile, effective limit, decision source (hard-limit vs boundary vs suppressed), deprecated-override usage.

Deliverable: measured, non-regressing behavior before flipping defaults.

## Workstream G — config and docs migration (after behavior is validated)

1. Rewrite README tunables: primary = three knobs; advanced = operational limits + compatibility overrides; deprecated = old percentage/tier knobs.
2. Simplify `config.json` around the public model; keep old defaults available in code.
3. Installer/`--status` prints the effective-policy block.
4. Update `HANDOFF.md` only where it would mislead a future session.

Deliverable: a normal user sees only `PROFILE`, `MAX_CONTEXT_TOKENS`, `MODE` first.

## Workstream H — boundary scoring (optional, last, small)

> **Advisor finding #3.** Only if backtest shows binary gating is insufficient. If added: strong=1, medium=0.5, observe=0; one threshold per profile; all weights surfaced in `--status`. Do not introduce per-signal public knobs.

Deliverable: only if it measurably improves lead time / `auto_unwarned`.

## Workstream I — window-size-aware target & ceiling (Opus-4.8 advisor — see window-aware.md)

The owner's directive (handle 64k→1m differently) requires replacing flat `SOFT`/`HARD` percentages with absolute `target(W)`/`ceiling(W)` curves derived from window + floor + profile.

1. Add `target(W)` / `ceiling(W)` to `policy.py` (reserve-independent target first; ceiling gated on the reserve re-measurement).
2. `a[profile]` replaces the existing `soft` fractions in `_PROFILES` (`{economy:130, balanced:188, lazy:266}`).
3. `decide()`: SOFT line = `context >= target(W) AND boundary`; HARD line = `context >= ceiling(W)`; gate expansion on `est_reclaim ≥ MS` + predictive signals, **never `stale_output`**.
4. Promote `native_ceiling` to Claude's effective window when present (`window_resolver`); add 64k/128k to `AUTO_WINDOW_TIERS` as fallback only.
5. Force `MODE→observe` below `W_min (~110–130k)`; surface in `--status`.
6. Retire `_WIDE` (degenerate 1-breakpoint version of this curve) — inert-deprecate then delete.
7. **Pi actuation safety:** SOFT-band actuation requires a *strong* signal (subagent_done/commit), not just any boundary — real tokens are spent.
8. Backtest `target/ceiling` per window tier before trusting `ceiling(W)` at the extremes.

Deliverable: the three regimes (small=physics-protected, medium=target+expand, large=efficient-but-grow) realized with **zero new public knobs**.

## Proposed execution order

1. **Workstream 0** — miss attribution (report + smallest fixes).
2. **Workstream A** — inventory + naming.
3. **Workstream B** — `policy.py` (rule only, binary gating, corrected `burn_rate`).
4. **Workstream C** — parity tests (golden freeze).
5. **Workstream D** — switch adapters one at a time (Claude last, after Workstream-0 fixes).
6. **Workstream E** — trap checklist throughout D.
7. **Workstream F** — old-vs-new backtest (gate).
8. **Workstream G** — docs/config rewrite.
9. **Workstream H** — scoring, only if backtest demands it.

## Risk register

| Risk | Mitigation |
|---|---|
| Treating this as a cosmetic refactor while the real bug (late autos) stays | Workstream 0 first; success metric is `auto_unwarned`, not key count |
| Behavior changes silently for existing users | old keys override derived policy during migration; `--status` reports deprecated overrides |
| Pi actuates too aggressively | default Pi `MODE` explicit; adapter enforces capability/mode |
| `policy.py` over-reaches into adapter concerns | strict rule-only scope; adapters own state/staging/actuation |
| Boundary score becomes hidden magic | defer to last phase; brutally small table; surfaced in `--status` |
| Claude miss has no recovery once `UserPromptSubmit` is skipped | Workstream 0 fixes timing/reliability before policy swap |
| Pi TS pre-gate diverges from Python policy | export constants for the shim or delegate to bridge (Workstream D step 3) |

## Open decisions

- Should `MAX_CONTEXT_TOKENS` default to unset (pure derivation) for the `balanced` profile? (advisor: cap-only; likely yes.)
- Should this owner's default profile be `economy` (cached-read spend dominates) while the public default stays `balanced`?
- Old-key status: warnings in `--status` only, or also telemetry events?
- `MAX_FULL_PARSE_MB` / `ARTIFACT_BUDGET`: README advanced config, or developer docs only?
