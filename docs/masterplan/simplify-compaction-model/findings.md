# Findings: simplify compaction model

Date: 2026-06-16

## Prompt

User direction: reevaluate and simplify the autocompactor model because it has too many confusing parameters and is difficult to tune. Selected direction: **three-knob model**.

## Current complexity inventory

`config.json` currently exposes or implies several independent tuning surfaces:

- occupancy thresholds: `SOFT_PCT`, `HARD_PCT`, `HARD_PCT_WIDE`
- window sizing/inference: `WINDOW`, `AUTO_WINDOW_MODE`, `AUTO_WINDOW_TIERS`, `AUTO_WINDOW_PROMOTE_FRAC`, per-harness `WINDOW`, native Claude ceiling, Pi runtime window/reserve
- boundary/signal gates: `STALE_FRAC`, `OBSERVE_ONLY`, signal-specific heuristics in `transcript_lib.active_signals()`
- debounce/economics: `COOLDOWN`, `POST_FLOOR`, `MIN_SAVINGS`
- performance/detail budgets: `MAX_FULL_PARSE_MB`, `ARTIFACT_BUDGET`
- adapter behavior: top-level `MODE`, `pi.MODE`, `pi.RESERVE`, Pi-specific hard threshold overrides
- optional LLM digest settings, delivered outside the primary decision model but documented in the same tunables list

The resulting mental model is hard to explain: users must understand percentages, effective windows, stale-output fractions, cooldown deltas, reclaim floors, observe-only signals, and adapter-specific overrides before they can predict behavior.

## Code split points

The decision model is spread across multiple modules:

- `src/autocompactor/context_monitor.py`: Claude monitor reads window, thresholds, cooldown, stale fraction, post-floor, min-savings, max parse size directly in the hot path.
- `src/autocompactor/pi_bridge.py`: Pi evaluate path mirrors much of the threshold/cooldown/min-savings logic and adds runtime context-window/reserve handling.
- `src/autocompactor/transcript_lib.py`: signal registry and observe-only filtering live separately from the policy thresholds that consume them.
- `src/autocompactor/window_resolver.py`: effective-window inference adds another layer of behavior outside the immediate recommendation rule.
- `README.md`: documents many knobs together, making advanced/internal controls look equally user-facing.

## Telemetry observations from local data

Recent local telemetry reinforces the need to simplify and unify the model:

- Claude stats: 206 `monitor_eval` events, 85 recommendations, 59 cooldown-suppressed recommendations. Median context at eval was about 122k tokens; median effective window was 200k despite configured 300k.
- Pi stats: 165 `monitor_eval` events, 133 recommendations. Recommendation rate is high, suggesting Pi policy/thresholds are not intuitively aligned with Claude behavior.
- Nightly 2026-06-16: 7 compactions, 3 auto / 4 manual; all auto-compactions arrived without advance recommendation; auto trigger median around 348k tokens while hard nag was configured around 186k tokens for the observed setup.
- Nightly 2026-06-15: 27 compactions, mostly auto; learned-window tiers and native-ceiling behavior appeared in the report, illustrating how much hidden state is required to interpret tuning.

## Core conclusion

The project should stop exposing the mechanics as the main interface. Users should choose intent; the system should derive mechanics.

Recommended public model:

1. `PROFILE`: how aggressively to compact.
2. `MAX_CONTEXT_TOKENS`: an optional **user cap** (downward-only; runtime/native/inferred limit always wins).
3. `MODE`: observe, advise, or actuate.

Everything else should become internal policy, telemetry-derived defaults, deprecated compatibility overrides, or advanced/debug-only config.

## Revised framing (after GPT-5.5 advisor pass)

The advisor's blunt point: config verbosity is a UX problem, but the *project's actual pain* is Claude auto-compactions arriving with no advance recommendation (2026-06-16: 3/3 autos unwarned). A config rename does not fix that. The dominant miss causes are upstream of config shape — no `UserPromptSubmit` between threshold crossing and native auto, cooldown suppression, wrong effective window, hook absence/crash, or one-turn context jumps. So the masterplan now leads with a miss-attribution investigation (Workstream 0) and measures success by `auto_unwarned` rate and lead tokens, not by fewer keys in `config.json`. See review.md for the full ranked findings and reconciliation.
