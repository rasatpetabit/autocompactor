# Sooner Firing + Compaction Reports

**Date:** 2026-06-26
**Complexity:** high
**Status:** spec

## Problem

1. **Firing too late on 1M context models.** The current `SOFT_PCT_WIDE = 0.25` sets the soft boundary at ~240k tokens for 1M models (25% of ~960k effective window). On long-running sessions where the working set is small, this means compaction advice arrives well past the point where stale output has accumulated — the soft line should fire at ~100k, not ~240k.

2. **No consolidated compaction report.** Users see a before/after token count in the post-compaction chat line, but lack a structured summary of what the compaction actually did: the state before and after, what was reclaimed, what artifacts were preserved, and whether the next step was recovered. This makes it hard to audit compaction quality across sessions.

3. **Detail notices don't fire near the soft line.** The existing `DETAIL_MIN_TOKENS` (100k) is meant to trigger a composition readout above the soft boundary, but with the current soft at 240k, the detail notice fires at 100k — which is *below* the soft line, so it's not actionable. After the soft threshold drops to ~96k, the detail notice at 100k sits *above* the soft boundary and becomes a meaningful "approaching compaction gate" signal.

## Decision

- **Per-tier absolute soft targets** (`SOFT_TARGET_1M`, `SOFT_TARGET_512K`, `SOFT_TARGET_300K`) replacing the flat `SOFT_PCT_WIDE` percentage — avoids floor-collapse on smaller tiers
- **Full compaction report** surfaced as a persistent chat message + telemetry event
- **Detail notice at soft line** — requires a minor code change (T4) to move the detail gate before the soft check so it fires at/above the soft boundary

## Spec

### 1. Per-tier absolute soft targets

**Problem with a flat percentage:** `SOFT_PCT_WIDE` as a single percentage collapses on smaller tiers. At 300k, `0.10 × 260k = 26k` soft, but `POST_FLOOR + MIN_SAVINGS ≈ 100k` means the soft band never fires — sessions jump straight to hard (104k). The percentage approach cannot adapt to fixed floors.

**Solution:** absolute soft targets per learned tier, resolved when `window_resolver` identifies the tier. The bridge's `cmd_evaluate` and the TS pre-gate resolve the soft line from these targets when the tier matches.

**config.json:**
```jsonc
"SOFT_TARGET_1M": 100000,
"SOFT_TARGET_512K": 80000,
"SOFT_TARGET_300K": 70000,
```

**Effect by model tier:**

| Model tier | Effective window | Old soft (25%) | New soft (absolute) | Hard (40%) |
|---|---|---|---|---|
| 1M | ~960k | ~240k | 100k | ~384k |
| 512k | ~472k | ~118k | 80k | ~189k |
| 300k | ~260k | ~65k | 70k | ~104k |

**Tier resolution:** `window_resolver.tiers()` returns `[200k, 300k, 512k, 1m]`. The `_nearest_tier()` maps the runtime context window to the nearest tier. `cmd_evaluate` computes `soft_t` from `SOFT_TARGET_<TIER>` when the learned tier matches, falling back to `SOFT_PCT_WIDE × window` when no tier-specific key is set.

**Min-savings guard interaction:** At 100k tokens (1M tier), `est_reclaim = context - post_floor ≈ 100k - 70k = 30k`, which meets `MIN_SAVINGS = 30k`. The soft path fires at the right point. For 512k (80k soft), reclaim ≈ 10k — suppressed by min-savings, but the hard line at 189k still compacts safely. For 300k (70k soft), reclaim ≈ 0 — suppressed entirely, but this is acceptable since the soft band on small windows is primarily advisory.

**Changes required:**
- `pi_bridge.py::cmd_evaluate()`: resolve `soft_t` from `SOFT_TARGET_<TIER>` using `resolution.learned_tier` label (e.g. `"1m"`, `"512k"`, `"300k"`). Fallback: `SOFT_PCT_WIDE × window`.
- `autocompactor.ts` pre-gate: add `softTargetFor(tier)` that mirrors the Python tier resolution. Update the pre-gate to use absolute target when available.
- Tests: update `test_wide_threshold_*` assertions to match new thresholds.

### 2. Compaction Report

**Shape:** structured, content-free. Token counts, category names, timestamps — never transcript text.

**Lifecycle:**
1. `cmd_prepare()` captures the pre-compaction snapshot and persists a `pre_report` in session state. **Note:** `pre_tokens` should be sourced from runtime `ctx.getContextUsage().tokens` (passed into prepare) rather than transcript analysis, since the transcript can lag behind the authoritative total during long streaming turns.
2. `cmd_reinject()` reads the pre-report, analyzes post-compaction state, computes delta, and builds `compactionReport`.
3. The TS shim surfaces `compactionReport` in the post-compaction chat announcement.
4. A `compaction_report` telemetry event is logged with full fields (content-free: no transcript text in telemetry).

**`pre_report` fields (captured in `cmd_prepare`):**

```jsonc
{
  "ts": "2026-06-26T14:32:01",
  "trigger": "actuate",  // "actuate" | "native" | "manual"
  "pre_tokens": 380000,
  "effective_window": 960000,
  "phase": "implementation",
  "thresholds": {"soft": 96000, "hard": 384000},
  "occupancy": "39.6%",
  "stale_frac": 0.39,
  "composition": {"tool": 120000, "assistant": 85000, ...},
  "artifacts": {"classes": 6, "total_bytes": 8200},
  "instructions_chars": 4100,
  "compaction_count": 3
}
```

**`compactionReport` fields (built in `cmd_reinject`):**

```jsonc
{
  "compaction_count": 3,
  "trigger": "actuate",
  "pre_tokens": 380000,
  "post_tokens": 112000,
  "reclaimed_tokens": 268000,
  "pre_phase": "implementation",
  "post_phase": "exploration",
  "effective_window": 960000,
  "post_occupancy": "11.7%",
  "thresholds": {"soft": 96000, "hard": 384000},
  "composition": {"tool": 120000, "stale_frac": 0.39, ...},
  "artifacts": {"classes": 6, "total_bytes": 8200},
  "instructions_chars": 4100,
  "next_step_source": "todo:pending[0]",
  "next_step_length": 15,
  "reinject_digest_chars": 1200,
  "ts": "2026-06-26T14:32:15"
}
```

**Telemetry event (`compaction_report`):** same fields as `compactionReport` above, but `next_step` text is excluded (content-free convention — only source/category/length in telemetry). The `next_step` text is surfaced in chat only.

**Chat message shape (rendered by TS shim):**

```
autocompactor: compaction #3 completed — context 380k → 112k (reclaimed ~268k)
  before: implementation, 39.6% of ~960k · 39% stale tool output
  after:  11.7% of ~960k
  artifacts: 6 classes, 8.2kB preserved to disk
  instructions: 4.1kB (fresh analysis)
  next step: [todo:pending[0]] Add rate limiter
  thresholds: soft 96k · hard 384k
```

**Implementation:**

- `pi_bridge.py::cmd_prepare()`: persist `pre_report` dict in session state alongside existing `last_compaction_stats`.
- `pi_bridge.py::cmd_reinject()`: read `pre_report`, compute post state, build `compactionReport`, return it alongside existing `text`, `compactionStats`, etc. Log a `compaction_report` telemetry event with full fields.
- `src/pi/autocompactor.ts` (`session_compact` handler): consume `inj?.compactionReport` and surface it in the post-compaction `announce()` call. Extend the existing message builder to include the report lines.

### 3. Detail notice at soft line (code change)

**Requires a minor code change in T4.** The existing pre-gate in `autocompactor.ts` nests the `DETAIL_MIN_TOKENS` readout *under* `occupancy < softPct` (line 388-407), so when tokens exceed the soft gate, the detail branch is skipped entirely. After the soft boundary drops, this means the detail notice won't fire at the exact point where it's most useful.

**Fix:** move the detail readout gate *before* the `occupancy < softPct` check, so it fires at/above the soft boundary when `evaluate` returns no recommendation. Alternatively, render `contextState` on the no-recommend path when occupancy is at/above soft.

## Risks & Mitigations

| Risk | Mitigation |
|---|---|
| 300k tier soft band never fires due to min-savings | Per-tier absolute target (70k) ensures the soft boundary is set independently of the floor. Min-savings guard means soft is advisory on small windows; hard line (104k) still compacts. |
| Report is verbose for first compaction | Compaction count gates verbosity — first compaction shows the full report, subsequent ones can be elided if desired. |
| Pre-report state grows across compactions | Only the latest `pre_report` is kept (overwritten each `cmd_prepare`). Bounded to one session state entry. |
| `next_step` leaks transcript text into telemetry | Telemetry logs only source/category/length; `next_step` text is surfaced in chat only (not in the telemetry event). |
| Detail notice doesn't fire at soft boundary | T4 moves the detail gate before the soft check so it fires at/above soft. |

## Out of scope

- Changing the hard boundary (HARD_PCT_WIDE stays at 0.40)
- Changing MIN_SAVINGS for wide models (30k guard is sufficient at the new soft line)
- `SOFT_TARGET_1M` config key (used `SOFT_TARGET_1M` etc.)
- Detail notice fires at soft line (requires code change in T4)
