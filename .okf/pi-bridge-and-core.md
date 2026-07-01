---
type: reference
title: Pi bridge and harness-agnostic core
timestamp: 2026-07-01T00:00:00Z
privacy: private
tags: [autocompactor, pi, bridge, config, signals]
---

# Pi bridge and harness-agnostic core

## Data flow

```
agent_end ─────────→ pi_bridge evaluate (watches occupancy + boundary
                      │ signals, advises or actuates ctx.compact())
                      ▼
session_before_compact → pi_bridge prepare (backs up transcript, extracts
                      │ artifacts, restates founding goal)
                      ▼
session_compact ────→ pi_bridge reinject (one-shot budgeted artifact
                      digest delivered on the next turn)
```

`transcript_lib.py` is the shared signal registry, phase detector, and
instruction builder consumed by the bridge. `stats.py` appends local
telemetry to `events.jsonl`. `nightly_eval.py` is the cron self-evaluation
(tests, telemetry health, dated reports).

## Module map (`src/autocompactor/`)

| module | role |
|---|---|
| `transcript_lib.py` | signal registry (`active_signals()`), phase detection, instruction builder — single source of truth for which signals fire |
| `config_lib.py` | unified config reader: `config.json` (+ `config.local.json`) + `AUTOCOMPACTOR_*` env overrides, single namespace |
| `artifacts.py` | mechanical extraction → disk → budgeted digest |
| `llm_digest.py` | optional cheap-model "must-survive" digest (harness-agnostic; consumed by `pi_bridge`) |
| `stats.py` | telemetry appender |
| `statedir.py` | state root resolution (`~/.autocompactor/pi`) |
| `window_resolver.py` | effective-window resolution (`contextWindow − reserveTokens`) |
| `nightly_eval.py` | cron self-evaluation: tests, telemetry health, dated reports, retention pruning |
| `pi_session_lib.py` | parses Pi v3 tree-format JSONL into `TranscriptStats` |
| `pi_bridge.py` | never-raise JSON CLI bridging the Pi extension to the Python core (`evaluate`/`prepare`/`reinject`) |
| `install_pi.py` | Pi harness adapter installer (copy-with-rewrite TS shim, version pin) |
| `policy.py`, `turn_profile.py`, `context_inventory.py`, `md_inventory.py`, `chonkie_lib.py`, `chonkie_chunk_runner.py` | supporting policy/profile/inventory/chunking helpers |

Thin entrypoint shims at `src/*.py` (`pi_bridge.py`, `nightly_eval.py`,
`install_pi.py`) put `src/` on `sys.path` and call the matching
`autocompactor.<module>.main()`. `src/pi/autocompactor.ts` is the Pi
TypeScript extension; its Node test suite lives at `src/pi/test/*.test.mjs`
and transpiles the shim with esbuild on the fly — no Pi install required.

## Compaction decision model

- **Occupancy** is derived free of model calls from the last assistant
  message's usage block: `input + cache_read + cache_creation + output`.
- **Boundary signals** (`transcript_lib.active_signals()`): `todo_step`,
  `error_resolved`, `idle_gap`, `subagent_done`, `burn_rate`,
  `topic_shift`, `commit`, `tests_pass`, `stale_output`. Pi's
  `OBSERVE_ONLY` set (`error_resolved,tests_pass,idle_gap` by default) is
  logged for telemetry but never justifies a recommendation — because Pi
  *actuates* (`ctx.compact()`) rather than only advising, it keeps
  `subagent_done`/`commit` as strong gates.
- **Thresholds** (`config.json`): `SOFT_PCT` 0.50 / `HARD_PCT` 0.90 for
  normal windows, `SOFT_PCT_WIDE` 0.25 / `HARD_PCT_WIDE` 0.40 for windows
  at or above the `AUTO_WINDOW_TIERS` breakpoints
  (`[200000,300000,512000,1000000]`). `MIN_SAVINGS` (30000) vs
  `POST_FLOOR` (70000) is the min-savings guard: no recommendation once
  `context − POST_FLOOR < MIN_SAVINGS`.
- **Window resolution** (`window_resolver.py`): effective window is
  `contextWindow − reserveTokens` when Pi reports both; `WINDOW` (200000)
  and `RESERVE` (40000, mirroring this host's `~/.pi/agent/settings.json`
  pin) are fallbacks. `native_ceiling` is a cap
  (`effective = min(resolved, native_ceiling)`), not a window replacement.
- **Large transcripts**: above `MAX_FULL_PARSE_MB` (8 MB), only the active
  segment since the last verified `compact_boundary` is parsed, bounding
  worst-case latency.
- **Instructions** are three-layered: base structured-handoff schema
  (verbatim-identifier rule; keep what can't be re-derived from disk, drop
  what can) + phase addendum (debugging/implementation/exploration/wrapup)
  + session anchors. The founding-goal directive
  (`TranscriptStats.initial_user_prompts`, first 3 genuine human prompts)
  leads every post-compaction digest and survives unlimited passes via an
  old-wins merge.
- **MODE**: `advise` only posts `autocompactor.advice`; `actuate` (default
  for Pi) lets the shim call `ctx.compact()` directly — Pi is the only
  harness that can actuate; a removed Claude adapter could only advise.

## Config surface

`config.json` (versioned) + `config.local.json` (gitignored, site-local
overlay) at repo root; `config_lib.py` reads env-first, with any
`AUTOCOMPACTOR_<NAME>` variable overriding the matching key. Key tunables:
`MODE`, `NEXTSTEP` (`autonomous`/`advisory`/`off`), `PROFILE` (`economy` by
default), `WINDOW`, `RESERVE`, `SOFT_PCT`/`HARD_PCT` (+ `_WIDE` variants),
`COOLDOWN`, `ARTIFACT_BUDGET` (1500), `AUTOCOMPACTOR_LLM` (opt-in cheap-model
digest via `llm_digest.py`), `AUTOCOMPACTOR_STATE_DIR`. Full table with
current defaults: `README.md` "Tunables".

## Verified ground-truth pins (do not re-derive; see `HANDOFF.md`)

- Validated against Pi `0.79.9`; `install_pi.py` re-pins the version
  observed at install time.
- This host pins `reserveTokens` to 40,000 in `~/.pi/agent/settings.json`
  (Pi default is 16,384); `RESERVE_FALLBACK` mirrors that pin.
- `pi.exec` has no stdin channel — bridge inputs are flags only.
- Signal precision backtests (14-day, 1,684 sessions) found `todo_step`
  best-performing (1.7x lift); a later re-run reversed some thin-sample
  signals (`idle_gap` 7.5x, `tests_pass` 2.7x) — treat as needing re-check
  before trusting as load-bearing, per `HANDOFF.md`.

## Open items (from `HANDOFF.md`, as of 2026-06-25)

- `~/.claude/settings.json` still has now-deleted Claude hooks
  (`context_monitor`, `precompact_analyzer`) registered — owner-held,
  outside the repo tree, non-fatal but should be deregistered.
- Several inert/dormant nits (dead `harness` param on
  `transcript_lib.observe_only`, dormant `TranscriptStats.todo_step` /
  `todos_all_done` fields, stale docstring/import references) are listed
  as a safe post-merge sweep with no runtime effect.
