# autocompactor — handoff notes

Project: a **Pi context compactor** — smarter, earlier, instruction-tailored
compaction for the Pi coding agent. The core is harness-agnostic by design; Pi
is the sole adapter that ships.

> **NOTE (2026-06-21, updated 2026-06-25):** the dated history below is the
> decision record — including the original Claude Code adapter and the
> rationale for removing it (the Pi-only pivot). For the *current* module
> layout and entrypoints, see `AGENTS.md` (Architecture); the canonical
> components are `transcript_lib.py`, `artifacts.py`, `llm_digest.py`,
> `pi_session_lib.py`, `pi_bridge.py`, `nightly_eval.py`, `install_pi.py`, and
> `src/pi/autocompactor.ts`.


## Post-compact plan position (2026-07-18)

Mechanical **progress ledger** (`progress_lib.py`) stages masterplan/coord/todo
plan position into artifacts + next-step. Hard resume is gated by session
**affinity**, **confidence**, `PROGRESS_RESUME`, and wait-mode supremacy
(waiting-state path unchanged). See
`docs/masterplan/post-compact-task-continuity/spec.md`.

## Post-pivot follow-ups (open items, 2026-06-21)

Deferred, owner-held cleanup from the Pi-only pivot merge (branch
`docs/spec0-pi-only-pivot`, merged to `main` @ `495088e`). None block the merge.

**Owner-held (touch uncommitted/user-owned or out-of-tree state):**
1. **WORKLOG entry** — **CLOSED (2026-06-25, this change set).** The verbatim
   2026-06-21 Pi-only-pivot entry was folded into `WORKLOG.md` (compressed)
   during the handoff/worklog compression pass.
2. ~~**TS-shim dead-branch cleanup**~~ — **CLOSED (2026-06-22).** The dead
   `CFG?.pi?.[key]` lookups in `cfgNum` and the `AUTOCOMPACTOR_PI_*` env reads
   were dropped from `src/pi/autocompactor.ts:50-86`.
3. ~~**`~/.claude/settings.json`**~~ — **CLOSED (2026-07-18).** Live
   `~/.claude/settings.json` / `settings.local.json` no longer register
   `context_monitor` / `precompact_analyzer` / `PreCompact` hooks (verified
   scan). No deregistration left to do on this host.

**Inert nits (safe post-merge sweep, no runtime effect):**
- `transcript_lib.observe_only(harness="claude")` — inert dead `harness` param;
  both callers already call it arg-less.
- `TranscriptStats.todo_step` / `todos_all_done` — dataclass fields the Pi
  parser never sets (default `False`); read under guards, dormant-by-construction.
- F401 unused imports in `tests/test_llm_digest.py`; stale `analyze()` reference
  in the `_block_text` docstring.
- `resolve_window` `cmd_prepare`/configured branch has no direct test pinning
  `effective_window == configured − reserve` (assertion still true; the hot
  `cmd_evaluate` path is covered). A 1-line regression pin would close it.

## Historical decision record (pre-Pi-only-pivot, preserved)

The Claude Code adapter was removed in the 2026-06-21 Pi-only pivot (branch
`docs/spec0-pi-only-pivot` → `main` @ `495088e`). Its history is preserved
here as the decision record. Full per-session detail lives in `WORKLOG.md`
and `git log`; this section keeps only what cannot be re-derived from disk.

### Claude adapter removal rationale

The Claude adapter's history was channel-fighting (systemMessage redraw,
additionalContext relay, PreCompact hookSpecificOutput rejection, cooldown
starvation) and Claude only ever advised — it cannot invoke `/compact`. Pi
actuates via `ctx.compact()`. Extracted `llm_digest` to a kept module;
completed the Pi parser's assistant/user/summary field-completion before any
removal. Full scaffolding flatten: single-namespace config, state under
`~/.autocompactor/pi`.

### Design decisions (and why) — still load-bearing on Pi

1. **Advise/actuate split.** Hooks/bridges that cannot invoke compaction
   advise at cheap boundaries; Pi actuates via `ctx.compact()` at its hard
   line.
2. **Occupancy from usage blocks.** Last assistant message's
   `input + cache_read + cache_creation + output` ≈ live context. Free to
   compute, no model calls.
3. **Boundary signals**: `git commit`, test-pass markers in tool output,
   all-TodoWrite-completed, stale tool-output fraction. Extended registry:
   `todo_step`, `error_resolved`, `idle_gap`, `subagent_done`, `burn_rate`,
   `topic_shift`. `active_signals()` in `transcript_lib.py` is the single
   registry consumed by the Pi bridge.
4. **Instructions are three-layered**: base structured-handoff schema
   (verbatim-identifier rule + recoverability: keep what cannot be
   re-derived from disk, drop what can, pointer when unsure) + phase addendum
   (debugging / implementation / exploration / wrapup) + session anchors.
5. **Telemetry is local-only** and content-free (counts, ratios, phases —
   no transcript text).
6. **Mechanical extraction → disk artifacts** (`artifacts.py`). Facts a
   regex can extract should never depend on a summarizer's goodwill; re-inject
   a priority-trimmed, budgeted digest on the first prompt after compaction.
   Adopted (adapted) from the `@davidorex/pi-custom-compactor` evaluation.

### `pi-custom-compactor` evaluation (2026-06-09) — conclusion

**Complementary, not duplicative** — they own summary durability, we own
timing and evaluation. Adopted: mechanical extraction → disk artifacts; per-
artifact cost accounting + stats visibility. Not adopted: their YAML spec
system (our phases are inferred from transcript behavior). Kept ours: boundary-
timing engine and offline backtester (no counterpart exists; this is the
differentiation).

### Signal precision results (14-day backtest, 1,684 sessions / 1,568 compactions)

True median reduction per compaction: **88%** (from postTokens). Measured
late-compaction waste: ~80.7M tokens / 14 days. Per-signal lift (vs baseline):
`todo_step` 1.7× (best), `commit` 1.3×, `idle_gap` 1.3×, `subagent_done` 1.2×,
`todos_done` 1.2×, `burn_rate` 1.1×, `stale_output` 1.0× (NO lift — hence
`STALE_FRAC` 0.50→0.90), `tests_pass` 0.9×, `error_resolved` 0.6×
(anti-predictive). A later (2026-06-17) corpus re-run reversed some thin-
sample signals: `idle_gap` 7.5×, `tests_pass` 2.7× re-promoted; `burn_rate`
0.9×, `subagent_done` 0.8× demoted; re-check before trusting as load-bearing.

**Pi signal gating** (`OBSERVE_ONLY`) is the conservative set: because Pi
actuates, it retains `subagent_done`/`commit` as strong gates (design trap
#4). `error_resolved`/`tests_pass`/`idle_gap` are observe-only (anti-
predictive on real corpora); `active_signals()` still reports them for
telemetry, but they never justify a recommendation.

### Verified ground-truth pins (do not re-derive)

- Validated against `@earendil-works/pi-coding-agent` **0.79.9**
  (`~/.npm-global/lib/node_modules/`); every API name in the shim was checked
  against its `dist/core/extensions/types.d.ts`. `install_pi.py` re-pins the
  version observed at install time.
- This host pins Pi `reserveTokens` to **40,000** in
  `~/.pi/agent/settings.json` (Pi default is 16,384). The bridge's
  `RESERVE_FALLBACK = 40_000` mirrors the host pin; effective window is
  `contextWindow − reserve`.
- `pi.exec` has NO stdin channel — bridge inputs are flags only.
- The ~69k post-compaction floor is WINDOW-INDEPENDENT (small windows are
  physics-protected; the target curve lands at ≥256k).
- `native_ceiling` is a CAP (`effective=min(resolved, native_ceiling)`), not a
  full window replacement — chose cap over replacement so a deliberate
  aggressive WINDOW is not loosened.

### Founding-goal directive (commits 0fc80d3 + 94ee3a8)

Compaction instructions preserve user input prompts VERBATIM (especially
initial ones). `TranscriptStats.initial_user_prompts` captures the first 3
genuine human prompts; the FOUNDING GOAL section leads every post-compaction
artifact digest (top of PRIORITY, old-wins merge so it survives unlimited
passes); `BASE_SCHEMA` carries prior summaries' GOAL/CONSTRAINTS forward
unchanged; Pi capture walks the full leaf path pre-compaction. Owner
directive: restate and reinforce the founding goal during compression to
prevent high-end models from forgetting their original purpose after many
passes.

### Config: single source of truth

`config.json` owns all tuning (single namespace; `config.local.json` is the
gitignored site-local overlay). `config_lib.py` reads env-first with
`AUTOCOMPACTOR_*` overrides; harness sections wholly outrank top-level keys.
`AUTOCOMPACTOR_CONFIG` env var: alternate config path, or empty for none
(tests/smokes use it for hermeticity). The `target(W)` SOFT curve
(`F + a[profile]·sqrt(W − F)`, `_A={economy:130, balanced:188, lazy:266}`)
replaces flat `SOFT_PCT` on the Claude main path; Pi stays on pinned flat
soft (actuate, conservative).

## Known limitations

* Transcript JSONL schema is not a public API; re-run smoke tests after
  Claude Code / Pi upgrades.
* Occupancy estimate ignores the fixed system-prompt share; thresholds are
  approximate by design — `/context` is ground truth.
* Compaction detection in the backtester is heuristic (usage-drop); eyeball a
  few detections against raw JSONL before trusting aggregates.
* On subscription billing this saves quota, not dollars; on API billing both.

## Smoke test (run after any change)

```bash
python3 -m pytest tests/ -q              # Python core (100 cases)
PI_SMOKE=1 bash tests/smoke_test_pi.sh   # Pi bridge contract, isolated HOME
node --test 'src/pi/test/*.test.mjs'     # Pi TS shim against stubbed pi/ctx
```