# Review: advisor pass (GPT-5.5 @ xhigh) + reconciliation

Date: 2026-06-16
Advisor: `[Advisor] simplify-compaction-model masterplan review` — Paseo agent `a406d072`, provider `codex/gpt-5.5`, thinking `xhigh`. Read-only.

## Verdict

"Revise before executing." The three-knob direction is a good public UX layer, but the draft reads as a *simplification* plan while the live failure is a *timing/lead-time* bug. Do not start implementation until the plan adds a "why did Claude miss autos?" attribution pass and gates the migration on old-vs-new backtest metrics.

## Ranked findings (advisor, verified against source this session)

1. **CRITICAL — this does not yet fix late Claude auto-compactions.** All 3 auto-compactions on 2026-06-16 had no advance recommendation (auto trigger ~348k, hard nag ~186k). The current Claude monitor already hard-recommends on `occupancy >= hard` after resolving the effective window (`context_monitor.py:146,207`). So the miss is upstream of config shape: no `UserPromptSubmit` between threshold crossing and native auto, cooldown suppression, wrong effective window, hook absence/crash, or one-turn context jumps. A profile rename will not fix it.
   - **Reconciliation:** add **Workstream 0 — miss attribution** (classify every unwarned auto by last prior `monitor_eval`, context, effective window, suppression state, and time/token gap).

2. **HIGH — `MAX_CONTEXT_TOKENS` must be a cap, not a target.** The spec left it "cap or target" (contradictory). Pi's runtime `contextWindow - reserve` is authoritative; a config value cannot enlarge it (`window_resolver.py:125`).
   - **Reconciliation:** `MAX_CONTEXT_TOKENS` = optional **user cap only**. Runtime/native/inferred limit always wins downward; unset → derive.

3. **HIGH — weighted boundary scoring risks hidden magic.** A single signal registry + observe-only filter already exists (`transcript_lib.py:413,461`); replacing `bool(gating)` with a score is not automatically simpler. **Also the spec wrongly demoted `burn_rate`** — HANDOFF records `burn_rate 54%/2.3x` lift (predictive), not weak (`HANDOFF.md:258`).
   - **Reconciliation:** defer scoring to the **last** phase. If adopted, keep it brutally small (strong=1, medium=0.5, observe=0; one threshold per profile; surfaced in `--status`). Correct `burn_rate` to medium/strong.

4. **HIGH — `policy.py` unifies the rule, not the adapters.** Asymmetry is in inputs and side effects: Claude carries `peak_ctx`, stages instructions, reinjects next prompt (`context_monitor.py:133,155`); Pi trusts live runtime context and can actuate (`pi_bridge.py:123,213`).
   - **Reconciliation:** `policy.py` takes resolved inputs and returns a decision. It does **not** own adapter state, file paths, staging, or actuation.

5. **MEDIUM — phase order is backwards.** The plan put old-vs-new backtest *after* docs/config migration (`plan.md:105`). Move validation forward.
   - **Reconciliation:** revised order — miss-attribution → `policy.py` + parity tests → adapter switch (one at a time) → old-vs-new backtest → docs/config → boundary scoring last.

6. **MEDIUM — missing implementation traps:** state schema (`last_reco_tokens`, `pending_reinject`, `peak_ctx`) with regression tests + incident history (`tests/test_autocompactor.py:679`); `_WIDE` compatibility; `native_ceiling_blocks_learned_window`; Pi reserve semantics; hidden policy constants inside signals (`burn_rate` hardcodes `0.85` at `transcript_lib.py:441`); **and** the Pi TS shim pre-gate reads `SOFT_PCT/MIN_SAVINGS/POST_FLOOR/COOLDOWN` locally (`src/pi/autocompactor.ts:55-58,124-127`) and bypasses the bridge — so a Python-only `policy.py` will not centralize that path.
   - **Reconciliation:** add an explicit "implementation traps" list; policy constants must be exportable in a form the TS pre-gate can consume, or the pre-gate must delegate to the bridge.

## Verification of advisor claims (this session)

- `burn_rate` predictive: `HANDOFF.md:258` → `burn_rate 54%/2.3x`. Spec's demotion was wrong.
- Hardcoded `0.85`: `transcript_lib.py:441` → `turns_left = (window * 0.85 - st.context_tokens) / br`.
- Pi TS pre-gate: `src/pi/autocompactor.ts:55-58` defines `SOFT_PCT/MIN_SAVINGS/POST_FLOOR/COOLDOWN`; `:124-127` implements the zero-spawn pre-gate that uses them directly.

## Revised success criteria

Measure success by: **`auto_unwarned` rate** (advance recommendation present before a native auto), recommendation rate, cooldown suppressions, and **lead tokens** — *not* by "fewer keys in `config.json`". Fewer public keys is a UX goal, not a correctness goal.
