# Review: turn profiler spec — GPT-5.5-pro adversarial pass

Date: 2026-06-22
Reviewer: gpt-5.5 (xhigh, adversarial) via native `codex review --adversarial`
(dispatch adversary backend was down — all skynet-local lanes timed out — so the
native codex CLI fallback was used; same model, gpt-5.5).
Verdict: the design leaves core attribution, context-accounting, prefix-analysis,
and degraded-input semantics underspecified or misleading. These should be fixed
in the spec before implementation to avoid building a diagnostic whose headline
numbers disagree with existing autocompactor semantics.

Findings (verbatim from the review):

## P1 — Separate pre-call tokens from context occupancy
*spec.md:84-86*
**Failure mode:** `peak_ctx`, reclaimable tokens, the sparkline, and the
cache-hit denominator will not match the context figure autocompactor actually
uses: this defines `ctx` as input-only at call start, while the same usage sample
has `totalTokens = input + cacheRead + cacheWrite + output` and
`pi_session_lib._usage_context()` prefers `totalTokens` or falls back to including
`output` (`src/autocompactor/pi_session_lib.py:151-172`; `transcript_lib.py:12-15`).
**Fix:** split `pre_call_tokens` from `post_call_context`/occupancy, using the
latter for peak/final/reclaimable context, and labelling deltas as
since-previous-call rather than exact content growth.

## P1 — Attribute fed-by content by interval, not toolResult only
*spec.md:87-91*
**Failure mode:** context added by `bashExecution`, `custom`, user messages,
prior assistant output/thinking, or unmatched/out-of-order parallel results can
be dropped or attached to the wrong assistant turn because the design makes
`toolResult`/`toolCallId` the fed-by primitive. Existing `pi_session_lib.analyze()`
has separate handling for `toolResult`, `bashExecution`, and `custom`
(`src/autocompactor/pi_session_lib.py:327`, `:337`, `:361`).
**Fix:** define fed-by as all active-path entries between the previous assistant
message and this assistant usage block, with `toolCallId` used only as an
optional tool-name resolver and unmatched entries surfaced explicitly.

## P2 — Add a public prefix analyzer for composition at peak
*spec.md:120-122*
**Failure mode:** `context_composition()` only formats counters already
accumulated on a `TranscriptStats`; Approach A exposes `active_path()` but no API
to build those counters over an arbitrary active prefix, so `turn_profile.py`
must either duplicate `pi_session_lib.analyze()` summary/stale/recent-window
logic or re-analyze synthetic prefixes.
**Fix:** add a tested public `analyze_entries`/`analyze_active_prefix(full_path,
active_prefix, recent_window)` helper and use it once for the peak prefix, or
defer composition-at-peak.

## P2 — Do not present result-share dollars as tool cost attribution
*spec.md:274-276*
**Failure mode:** the per-tool `est-cost` table can mislead the main cost
question because a tool result drives repeated later input/cacheRead spend until
compaction, while proportional total-result-token allocation makes early and late
reads with identical output look equally expensive and underweights low-result
tools.
**Fix:** report per-tool result tokens separately from exact per-call costs, or
computing a clearly named per-call fed-by interval share rather than presenting
blended result-share dollars as attribution.

## P2 — Define missing-usage and empty-profile behavior
*spec.md:66-67*
**Failure mode:** sessions with no usage blocks or an empty active segment have
no defensible values for non-null fields like `peak_turn_index`,
`biggest_growth_turn`, and exact per-turn cost/context, but the spec assumes
every assistant message carries usage and only says to exit 0 with a best-effort
message.
**Fix:** specify skip-vs-include semantics with `has_usage`/warnings, nullable
summary fields for empty profiles, and a valid `--json` error/warning object for
degraded inputs.

## Disposition

All five accepted and folded into the spec (rev 2): the two P1s reshape the
attribution model (pre-call vs occupancy split; fed-by defined as the interval,
toolCallId demoted to an optional name resolver); the three P2s add the
`analyze_active_prefix` helper, rename/restructure the per-tool cost table away
from "cost attribution," and define degenerate-profile behavior. See the spec's
"GPT-5.5 review revisions" note and revised Attribution / Data model / Testing
sections.
