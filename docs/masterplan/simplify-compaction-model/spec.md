# Spec: three-knob compaction model

Date: 2026-06-16
Status: brainstorm/masterplan draft (revised after GPT-5.5 advisor pass — see review.md)

## Summary

Replace the current parameter-heavy tuning surface with a three-knob public model:

```json
{
  "PROFILE": "balanced",
  "MAX_CONTEXT_TOKENS": 300000,
  "MODE": "advise"
}
```

The public interface should describe user intent, not implementation mechanics. Existing low-level keys remain temporarily as deprecated compatibility overrides, but normal configuration and documentation center on the three knobs.

> **Revision (advisor finding #1):** simplifying config is a UX goal, not the project's actual pain. The live failure is Claude auto-compactions arriving with no advance recommendation. This spec therefore treats the three knobs as the *public* layer and mandates a miss-attribution investigation (Workstream 0) before any behavior change.

## Goals

- Make the model explainable in one paragraph.
- Make Claude and Pi decisions come from the same **decision rule** (a `policy.py` that takes resolved inputs and returns a decision).
- Preserve current behavior where possible through derived defaults and compatibility aliases.
- Reduce normal user tuning to intent-level decisions.
- Keep telemetry/backtesting rich enough to validate the migration.
- **First, understand and fix the "no advance recommendation before auto-compaction" failure** (Workstream 0); config simplification follows that, it does not substitute for it.

## Non-goals

- Do not remove advanced overrides in the first migration.
- Do not rewrite artifact extraction, preservation-instruction generation, tail parsing, or reinjection — out of scope unless they affect the decision policy.
- Do not depend on an LLM to decide when to compact.
- Do not make Claude and Pi identical at the adapter layer; centralize the *rule*, not the adapters.
- Do not require users to know effective-window tiers, stale-output percentages, or min-savings math.
- Do not gate success on "fewer keys in config.json".

## Public knobs

### `PROFILE`

Intent-level compaction style.

| Profile | Meaning | Expected behavior |
|---|---|---|
| `economy` | Preserve quota/cost aggressively | compact earlier; accept more interruptions at natural boundaries |
| `balanced` | Default | compact when reclaim is worthwhile and either context is high or a boundary appears |
| `lazy` | Minimize interruptions | compact later; mostly at strong boundaries or near the safety limit |

Derived policy examples:

| Profile | target occupancy | hard occupancy | boundary threshold | cooldown |
|---|---:|---:|---:|---:|
| `economy` | low | medium | low | short |
| `balanced` | medium | high | medium | medium |
| `lazy` | high | very high | high | long |

Numeric constants live in code as internal policy defaults, **surfaced verbatim in `--status`/docs** so they are not hidden magic. A versioned profile table (profile → derived constants) is the single source of truth.

### `MAX_CONTEXT_TOKENS` — optional user cap only

> **Revision (advisor finding #2): cap, not target.** The previous draft left this ambiguous ("cap or target"). It is a **cap**: a user can only *tighten* the limit, never enlarge the runtime window.

Resolution order:

1. Authoritative limit = runtime context window if the adapter provides it (Pi: `contextWindow`).
2. Else = native harness ceiling if known (Claude `CLAUDE_CODE_AUTO_COMPACT_WINDOW`).
3. Else = inferred from observed peak/tier logic.
4. Apply `MAX_CONTEXT_TOKENS` as a downward-only cap: `effective_limit = min(authoritative_limit, MAX_CONTEXT_TOKENS)`.
5. Subtract adapter reserve.

For backwards compatibility, existing `WINDOW` maps to `MAX_CONTEXT_TOKENS` during the deprecation period.

### `MODE`

| Mode | Meaning |
|---|---|
| `observe` | log decisions only; no user-facing recommendation/action |
| `advise` | recommend compaction when policy fires |
| `actuate` | compact automatically when the adapter can safely do so |

Claude remains effectively advisory (hooks cannot invoke `/compact`). Pi can support `actuate`. That difference is adapter capability, not separate Pi-only policy math.

## Internal policy model — rule, not adapter

> **Revision (advisor finding #4):** `policy.py` centralizes the **decision rule**. It does **not** own adapter state, file paths, instruction staging, or actuation.

Suggested conceptual API:

```python
@dataclass
class PolicyInput:
    harness: str
    profile: str
    mode: str
    context_tokens: int
    effective_limit: int
    transcript_stats: TranscriptStats
    prompt: str = ""
    last_reco_tokens: int = -10**9   # cooldown baseline (adapter-owned, passed in)

@dataclass
class PolicyDecision:
    recommend: bool
    reason: str
    mode: str
    context_tokens: int
    effective_limit: int
    profile: str
    reclaim_tokens: int
    boundary: bool            # binary for now; score deferred (see below)
    signals: list[str]
    suppressed_by_cooldown: bool
    telemetry: dict
```

Adapters keep owning: per-session state files, `peak_ctx` carry, instruction staging, reinjection, actuation. They gather resolved inputs, call `policy.decide()`, then perform their own side effects.

Decision shape:

```text
estimated_floor = learned/session/default post-compaction floor
reclaim = context_tokens - estimated_floor
recommend when:
  reclaim >= min_reclaim(profile, effective_limit)
  AND (
    context_tokens >= hard_limit(profile, effective_limit)
    OR (
      context_tokens >= boundary_floor(profile, effective_limit)
      AND at_least_one_gating_signal
    )
  )
  AND not cooldown_suppressed
```

### Boundary handling — keep binary for now

> **Revision (advisor finding #3):** do **not** replace binary gating with a weighted score in this migration. It moves complexity from public knobs into an opaque table that is harder to validate. Keep `bool(gating)` (one or more non-observe signals) for the migration.

If a score is ever added (final phase only), it must be brutally small — strong=1, medium=0.5, observe=0, one threshold per profile — and every weight surfaced in `--status`.

### Signal classification (corrected)

> **Revision (advisor finding #3):** the previous draft wrongly demoted `burn_rate`. Telemetry (`HANDOFF.md:258`) shows `burn_rate 54% precision / 2.3x lift` — it is predictive. Corrected classification:

| Class | Signals | Notes |
|---|---|---|
| Strong | todos done, commit, subagent returned, topic shift | good natural breakpoints |
| Medium | stale output, idle gap, tests passed, todo step completed, **burn_rate** | noisier but predictive enough to gate |
| Observe (telemetry only) | error resolved | measured anti-predictive; keep measuring, do not gate |

`OBSERVE_ONLY` becomes an internal table or advanced override, not normal user config.

### Derived internals

Derived from `PROFILE`, `MAX_CONTEXT_TOKENS`, telemetry, or adapter facts:

`SOFT_PCT`, `HARD_PCT`, `HARD_PCT_WIDE`, `STALE_FRAC`, `COOLDOWN`, `POST_FLOOR`, `MIN_SAVINGS`, `AUTO_WINDOW_TIERS`, `AUTO_WINDOW_PROMOTE_FRAC`, `RESERVE`.

`MAX_FULL_PARSE_MB` and `ARTIFACT_BUDGET` are not part of the decision policy; they remain advanced operational settings.

## Compatibility plan

For at least one release window:

- Accept old keys.
- Emit telemetry when an old key overrides derived policy.
- Prefer explicit old keys over derived defaults (no silent behavior changes).
- Mark old keys advanced/deprecated in docs.
- `--status`/doctor explains effective policy in the new vocabulary.

Example effective-policy block:

```text
PROFILE=balanced
MAX_CONTEXT_TOKENS=300000
MODE=advise
effective_limit=260000  # runtime 300000 - reserve 40000
hard_limit=186000
boundary_floor=120000
min_reclaim=30000
adapter=pi actuate-capable
deprecated_overrides_in_effect: HARD_PCT=0.90
```

## Success criteria

> **Revision (advisor finding #1/#5): success is behavioral, measured by backtest — not "config has fewer keys".**

- README's normal config section shows ≤3 primary knobs.
- Claude and Pi call the same `policy.decide()`; parity tests assert identical decisions given identical resolved inputs.
- Existing tests pass.
- New tests cover: profile-derived decisions, cap-only `MAX_CONTEXT_TOKENS`, old-key compatibility, cooldown reset, min-reclaim guard.
- **`auto_unwarned` rate (advance recommendation before native auto) does not regress** vs current baseline — this is the primary correctness metric.
- Recommendation rate, cooldown suppressions, and lead tokens reported in nightly; old-vs-new backtest attached before flipping defaults.
- Users can answer "why did it compact?" from one reason string + the effective-policy block.
