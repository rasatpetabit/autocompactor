# autocompactor

A **Pi context compactor**: smarter, earlier, cheaper compaction for the Pi
coding agent. The Pi extension shells out to a harness-agnostic Python core that
watches occupancy and task boundaries, decides when a compaction is worthwhile,
and tailors the instructions the summarizer is given.

```
agent_end ─────────→ pi_bridge evaluate  (watches occupancy + boundaries,
                     │                     advises or actuates ctx.compact())
                     ▼
session_before_compact → pi_bridge prepare  (backs up transcript, extracts
                     │                        artifacts, restates founding goal)
                     ▼
session_compact ────→ pi_bridge reinject  (one-shot budgeted artifact digest
                     │                       delivered as the next turn)
                     │
   transcript_lib.py  (shared signal registry, phase detection, instruction builder)
   stats.py           (local telemetry -> events.jsonl)
   nightly_eval.py    (cron self-evaluation: tests, telemetry health, dated reports)
```

## Why this shape

Compaction cost scales with the context summarized, and every token of bloat is
re-paid as input on every subsequent turn. Compacting at a task boundary, at
moderate occupancy, is dramatically cheaper than an autocompact firing near the
ceiling — and the summary is better, because the instructions are generated from
what actually happened in the session (files edited, recent errors, last task
statement). Pi is the first harness where we hold an actuator: the bridge can
either advise (`autocompactor.advice`) or call `ctx.compact()` itself
(the shipped default — see MODE below).

## Install

Clone anywhere and run the installer — the extension is copied into
`~/.pi/agent/extensions/` with the bridge path baked in (a symlink can't carry
the path), the Pi package version observed at install time is pinned, and
nothing else under `~/.pi` is touched:

```bash
git clone <repo-url> autocompactor && cd autocompactor
python3 src/install_pi.py            # extension -> ~/.pi/agent/extensions/ (bridge path baked in)
python3 src/install_pi.py --status   # doctor: placeholder rewritten? bridge reachable? version drift?
python3 src/install_pi.py --remove   # delete only our shim + version pin
```

Restart Pi to load the extension. State and telemetry live under
`~/.autocompactor/pi/`.

## Tunables

Tuning lives in `config.json` at the repo root (bare key names, e.g.
`HARD_PCT`), read at runtime via `config_lib` — no env-plumbing sync step
needed. `config.local.json` is the gitignored site-local overlay, and any
`AUTOCOMPACTOR_<NAME>` environment variable overrides the corresponding key.

| Key | Default | Meaning |
|---|---|---|
| `MODE` | `actuate` | `advise` only posts an `autocompactor.advice` message; `actuate` lets the shim call `ctx.compact()` itself. Overridden by `AUTOCOMPACTOR_PI_MODE` env var. |
| `PROFILE` | `economy` | Compaction profile passed to the summarizer. |
| `WINDOW` | 200000 | Fallback context window in tokens. Pi normally derives the exact effective window from `ctx` (`contextWindow − reserveTokens`), better than this static value. |
| `RESERVE` | 40000 | Reserve tokens subtracted from `contextWindow` when Pi does not report one. Mirrors the Pi host pin in `~/.pi/agent/settings.json`. |
| `SOFT_PCT` | 0.50 | Recommend at this occupancy *if* a boundary signal is present. |
| `SOFT_PCT_WIDE` | 0.25 | Lower soft threshold used when the window tier is wide (e.g. ≥300k). |
| `HARD_PCT` | 0.90 | Recommend unconditionally at this occupancy. |
| `HARD_PCT_WIDE` | 0.40 | Lower hard threshold used when the window tier is wide. |
| `COOLDOWN` | 20000 | Min token growth between recommendations. |
| `STALE_FRAC` | 0.90 | Stale-tool-output fraction that counts as a boundary signal. |
| `POST_FLOOR` | 70000 | Estimated post-compaction context (measured ~69k median here). A compaction can only reclaim what sits above this. |
| `MIN_SAVINGS` | 30000 | Min estimated reclaim (context − POST_FLOOR) to recommend; below it a compaction stalls 30–60s for almost nothing. |
| `DETAIL_MIN_TOKENS` | 100000 | Start showing composition-only monitoring notices once context is large enough to make compaction meaningfully reclaimable, even before the compaction gate. |
| `DETAIL_COOLDOWN` | 75000 | Min token growth between pre-gate composition monitoring notices. |
| `MAX_FULL_PARSE_MB` | 8 | Above this transcript size, parse only the active segment after the last compaction boundary (bounds worst-case latency). |
| `OBSERVE_ONLY` | `error_resolved,tests_pass,idle_gap` | Signals logged for telemetry but never allowed to justify a recommendation (defaults measured anti-predictive on real corpora). Set empty to restore full gating. |
| `ARTIFACT_BUDGET` | 1500 | Token budget for the post-compaction artifact digest. |
| `AUTO_WINDOW_TIERS` | `[200000,300000,512000,1000000]` | Ordered token-count breakpoints used to select wide-threshold variants (`SOFT_PCT_WIDE`, `HARD_PCT_WIDE`) for large-window sessions. |
| `AUTOCOMPACTOR_LLM` | unset | `1` = `prepare` also runs a configured cheap-model LLM over the transcript tail for a smarter must-preserve digest. Adds latency and its own token cost. |
| `AUTOCOMPACTOR_LLM_PROVIDER` | `claude` | Optional digest provider for the `AUTOCOMPACTOR_LLM` path. This is a *model provider* choice (Anthropic API), independent of the harness. |
| `AUTOCOMPACTOR_PI_MODE` | `actuate` | Env override for `MODE`. `advise` only posts an `autocompactor.advice` message; `actuate` lets the shim call `ctx.compact()` itself. |
| `AUTOCOMPACTOR_PI_INTERCEPT` | unset | `1` = cancel a native auto-compaction and re-trigger it with our enriched instructions. Default off; auto-disabled when `pi-custom-compactor` is configured. |
| `AUTOCOMPACTOR_STATE_DIR` | `~/.autocompactor/pi` | Override the state root (used by tests). Pi is the sole adapter. |

## Boundary signals detected

- `git commit` executed in the recent window
- a subagent task just returned
- ≥50% of tool-result bytes in context are older than the recent window
- burn-rate projection (within ~8 turns of autocompact)
- topic shift in the incoming prompt

Observe-only (telemetry, never gating — measured anti-predictive on real
corpora; see `OBSERVE_ONLY`): test-suite success markers, debug-loop
conclusion, long idle gap.

## Billing note

The recommendation path costs nothing — it is local Python parsing of the Pi
transcript JSONL (context occupancy read from the `usage` block of the last
assistant message). The compaction itself is still a model call, billed/quota'd
normally; this system just makes each one smaller, earlier, and better. The
occupancy estimate ignores the fixed system-prompt share, so treat thresholds as
approximate and tune to taste — `/context` is ground truth. On subscription
plans this saves *quota*, not dollars; on API billing it saves both.

## Test matrix

```bash
python3 -m pytest tests/ -q              # Python core (100 cases)
PI_SMOKE=1 bash tests/smoke_test_pi.sh   # Pi bridge contract, isolated HOME
node --test 'src/pi/test/*.test.mjs'     # Pi TS shim against stubbed pi/ctx
```

`smoke_test_pi.sh` is a no-op (exit 0) unless `PI_SMOKE=1`, so plain CI runs are
unaffected. The `node --test` suite transpiles `src/pi/autocompactor.ts` with
esbuild on the fly and needs no Pi installation (pass the glob, not a bare
directory — node 22 resolves a positional directory as a module and fails). Run
the full matrix before any commit that touches `pi_bridge.py`,
`pi_session_lib.py`, `statedir.py`, or `src/pi/`.
