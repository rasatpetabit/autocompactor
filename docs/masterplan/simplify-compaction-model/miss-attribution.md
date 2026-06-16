# Workstream 0 — miss attribution

Date: 2026-06-16
Scope: explain why Claude auto-compactions arrive with **no advance recommendation**, before any policy/config refactor. (Mandated by the GPT-5.5 advisor pass — see review.md finding #1.)

## Method

Correlated three data sources for the 3 unwarned autos flagged by the 2026-06-16 nightly backtest:
- backtest-2026-06-16.json (per-session replay: `late_by_tokens`, effective/learned windows),
- `~/.claude/autocompactor/stats/events.jsonl` (actual `monitor_eval`/`precompact`/`reinject` events),
- the raw session transcripts under `~/.claude/projects/`.

## The 3 unwarned autos

| session | before | reduction | late_by_tokens | learned_window | native_ceiling | blocks |
|---|---:|---:|---:|---:|---:|---|
| 629dd714 | 344,500 | 94% | 181,315 | 512,000 | 500,000 | True |
| d1c12003 | 349,724 | 95% | 198,876 | 512,000 | 500,000 | True |
| c229c7ce | 348,277 | 96% | 178,766 | 512,000 | 500,000 | True |

All three: 512k-tier sessions where `native_ceiling (500k) < learned_window (512k)`, auto-firing at ~345k. The backtester's replay (effective window 300k) says a recommendation should have fired ~180–199k tokens earlier.

## Confirmed root cause — structural

**The advisor hook is gated to `UserPromptSubmit`, which fires only on human prompts — not tool-result turns. Native autocompact fires on pure context size. Long autonomous/agentic bursts outpace the hook.**

Evidence (session 629dd714, the unwarned 343,262 auto):
- Growth window to the auto: **7 human prompts vs 130 tool-result turns**; context rose 37k→343k.
- The hook is correctly registered (`UserPromptSubmit`, `matcher=None` → every human prompt; command `src/context_monitor.py`).
- **Zero `monitor_eval` events logged before that auto.** The session's only eval came *after* the compaction (172,775 tokens, 80 min later).
- Per-session eval density across all events is structurally low: median **2** evals/session; 14 of 43 sessions logged exactly **1**.

So even with perfect thresholds and a working hook, a session that does 130 tool calls on few human prompts gives the hook almost no chance to recommend before native auto fires. A profile rename does not touch this.

## Confirmed secondary signal — possible hook-reliability regression

Evals/day collapsed right after the 2026-06-11 Claude Code upgrade:

| date | monitor_evals | compactions | CC version |
|---|---:|---:|---|
| 06-09 | 21 | 31 | 2.1.170 |
| 06-10 | 142 | 73 | 2.1.170 |
| **06-11** | **6** | 1 | **2.1.173** |
| 06-12 | 6 | 1 | 2.1.175 |
| 06-14 | 4 | 4 | 2.1.176 |
| 06-15 | 12 | 8 | 2.1.176 |
| 06-16 | 15 | 2 | 2.1.178 |

Today (06-16) the hook fires multi-eval sessions again, so the mechanism works now — but the 06-11..06-15 window (when these unwarned sessions ran) shows near-zero evals. Either the upgrade changed `UserPromptSubmit` invocation, or the hook errored/no-oped silently during that window. Not yet pinned (needs a hook-alive probe; see fixes).

## Ruled out

- **Tail-only parse returns 0** — refuted. All three transcripts tail-parse to valid `context_tokens` (179,942 / 111,519 / 244,071); the `context_tokens <= 0` early-return is not the cause.
- **`session_id`/`transcript_path` propagation broken** — refuted. 198/206 evals carry valid uuid session_ids; the 8 "other" are installer `verify-*` probes.
- **`analyze()` under-reports context (peak 179,942 vs 343,262)** — **not a bug.** Compaction summarizes away pre-compaction messages, so the transcript's `usage_series` only holds post-compaction values; the state-carried `peak_ctx` is what preserves the true peak. Do not "fix" this by re-reading pre-boundary usage.

## Smallest fixes (per bucket)

1. **Structural — add a non-`UserPromptSubmit` trigger** so the hook can detect high context during tool loops, not only on human prompts. Candidates:
   - a lightweight `PreToolUse` (or periodic watchdog) occupancy check that recommends when context crosses the hard line mid-burst;
   - or, on `UserPromptSubmit`, proactively recommend at the **first** prompt after any gap if carried `peak_ctx` already crossed the threshold (the hook already does this via `peak_ctx`, but only if a human prompt arrives before native auto — which is exactly what fails here).
   This is the only fix that addresses the dominant bucket. It is a *timing/hook-reliability* change, not a config/profile change.

2. **Hook-reliability — add a self-check.** Nightly should compute `evals_per_human_prompt` (or evals-per-active-session-day) and flag near-zero, so a silent `UserPromptSubmit` regression (like 06-11) is caught immediately instead of discovered days later.

3. **Investigate the 2.1.173-era collapse** — was the hook not invoked, or invoked but erroring? A heartbeat log line on every `UserPromptSubmit` (independent of the eval) would distinguish "hook never ran" from "hook ran but no-op'd".

## Implication for the masterplan

- **Do not start the config/profile refactor expecting it to fix `auto_unwarned`.** It will not. Workstream 0's fix #1 is the thing that moves `auto_unwarned`.
- The `policy.py` work (Workstream B) is still valuable for explainability and Claude/Pi parity, but it is downstream of fix #1.
- Success metric stands: `auto_unwarned` rate, recommendation rate, cooldown suppressions, lead tokens — measured before and after fix #1.

## Open

- **06-11 collapse pinned as a regression (not low usage), with a caveat.** On 2026-06-11 the Claude Code upgrade 2.1.170→2.1.173 coincided with `monitor_eval` events dropping to 6 and `precompact` hook events to ~1, while transcripts show ~87 compactions that day. The PreCompact hook under-fired too, so it was a broad hook-invocation regression, not a UserPromptSubmit-specific issue. Caveat: the backtest counts ALL transcripts under `~/.claude/projects` (including sessions where the hook was never installed / non-interactive / other accounts), while `events.jsonl` counts only hook-covered sessions — so the 87/1 gap overstates the regression somewhat. The evals-per-precompact ratio is useless for detection because both hook counts drop together; the robust signal is **hook events vs transcript compactions**, now added as a nightly self-check (`hook_coverage`). Today (2.1.178) the hook fires multi-eval sessions again, so it has recovered.
- Decide whether a `PreToolUse` occupancy trigger is acceptable given the ">80% cached reads / cheap every turn" constraint (it must be bounded and cheap, like the existing tail-parse guard).
