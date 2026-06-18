# WORKLOG

Terse handoff log for collaborating agents. Newest entry first.

## 2026-06-17 — REVERT: restore systemMessage readout for compact suggestions

The previous entry's "move readout to additionalContext, drop systemMessage" was
WRONG and the owner caught it: they still didn't see the useful readout during a
compact suggestion. Root cause of the misfire: `additionalContext` is Claude-only
and Claude relays it unreliably (paraphrases/omits), so the rich readout vanished
from the user's view. The reliable, visible channel in Claude Code is the hook's
`systemMessage` (rendered verbatim) — which is exactly what the owner saw "before".

Fix: both recommendation sites (`UserPromptSubmit` `_run`, `PostToolUse`
`_run_posttooluse`) again emit the full readout in `systemMessage`;
`additionalContext` is now a numbers-free Claude-only note that says "the readout
is already shown to the user — do NOT restate it" (prevents the earlier "double").
Net: exactly one rich, verbatim readout the user reliably sees.

Decision rule for future edits: readout → systemMessage (verbatim, reliable);
additionalContext → silent awareness only, never the data channel. Do not "de-dup"
by moving numbers out of systemMessage again. Tests reverted to match (monitor
schema {hookSpecificOutput, systemMessage}; readout asserted in systemMessage).
180 pytest + smokes + --verify PASS.

## 2026-06-17 — readout to relay (fix "stripped warning") + statusbar buffer bug

Two display bugs the owner hit after the prior de-dup + an acw change.

1. **Recommendation readout was "stripped/useless."** The prior de-dup put the
   numbers in `systemMessage` and made `additionalContext` a number-free "don't
   repeat" directive — on the premise that the user reads the systemMessage. They
   don't; they read Claude's *relayed prose* (driven by additionalContext), which
   was now number-free. Fix (owner-chosen): carry the readout in
   `additionalContext` with an explicit "surface to the user ONCE, concisely, at
   a breakpoint" instruction, and **drop the recommendation `systemMessage`** so
   there's no double. Both sites (UserPromptSubmit `_run`, PostToolUse
   `_run_posttooluse`). Monitor output schema is now `{hookSpecificOutput}` only.
   PostCompact notice is untouched (separate surface, no competing relay).
   Verified live: the mid-burst relay now shows "306k in context · …".

2. **Statusbar "completely wrong"** — `~/.claude/statusline.js` (owner's harness
   file, OUTSIDE this repo; not in any git repo; last touched 2026-05-26 so NOT a
   regression from our commits). Bug: `AUTO_COMPACT_BUFFER_PCT = acw/totalCtx`
   treats `CLAUDE_CODE_AUTO_COMPACT_WINDOW` (the *usable* budget) as the
   *reserved* buffer. With acw=900000/1M that's 90%, pinning the ctx meter at
   100% past ~100k. Correct: `(totalCtx - acw)/totalCtx` = 10%. Verified the
   corrected meter tracks used/900k exactly (25% at 224k, 100% at the 810k
   trigger). Note: `CLAUDE_CODE_AUTO_COMPACT_WINDOW` is now **900000** (CLAUDE.md
   still said 300000 — live env drifted; not changed by us this session).

Tests: rewrote the PostToolUse "user-visible systemMessage" test → asserts the
readout rides additionalContext + no systemMessage; updated the compat-pin
monitor schema to `{hookSpecificOutput}`. 180 pytest + smokes + --verify PASS.

## 2026-06-17 — WI-1 fix: corrected auto-coverage metric + coverage instrumentation

Owner chose "metric fix + instrumentation" for the WI-1 finding (the unwarned
alarm was a measurement artifact, not late thresholds).

Metric (nightly_eval.py) — new `auto_warning_coverage(pre, mon, live_ceiling)`:
(i) epoch-filter autos to the current `native_ceiling` (drops old-config ~133k
autos + None pre-instrumentation events); (ii) cold-start separation — a native
auto with zero prior monitor_evals (resumed/cold-start) is unwarnable, reported
as a note not a miss; (iii) session-level warned (any prior recommended eval),
replacing the per-interval window that marked repeat autos unwarned. Alarm now
needs ≥4 measurable autos (kills the n=1→100% false alarm). Record gains
auto_warned/auto_cold_start/auto_off_epoch/auto_epoch_ceiling; md adds a coverage
line.

Instrumentation (context_monitor.py): UserPromptSubmit monitor_eval now carries
`hook_event="UserPromptSubmit"` (was absent → "?" in telemetry). New gated flag
`AUTOCOMPACTOR_LOG_WATCHDOG_SKIPS` (off by default): logs cheap `watchdog_skip`
evals for PostToolUse at/above-soft non-recommends (no full analyze()), so
PostToolUse coverage becomes measurable without per-tool spam.

Tests +5 (180 total): 3 unit tests for auto_warning_coverage (epoch/cold-start/
session-level), hook_event tag, gated skip-log on/off. Note for test authors:
`_hook_env` is hermetic (AUTOCOMPACTOR_CONFIG="") → balanced defaults, band
soft=110k/hard=130k at a 200k window (NOT the economy/target-curve 100k/110k).
smoke + Pi smoke + --verify PASS. AGENTS.md baseline 164→180.

No monitor/threshold behavior change — advise-only timing is unchanged.

## 2026-06-17 — WI-1 root cause: "mid-burst lateness" is mostly a metric artifact

Diagnostic only (systematic-debugging Phase 1; NO fix this turn). Investigated the
nightly "6/10 autos arrive unwarned" alarm. Evidence: 677-event events.jsonl
(2026-06-09→06-17), code paths in context_monitor.py + nightly_eval.py:278-294.

Verdict — the complaint is dominated by **measurement defects (hypothesis d)**, not
late thresholds. On the CURRENT config epoch (native_ceiling=300000, 22 autos):
**86% warned (19/22), 13% unwarned (3/22)** — far below the 50% alarm line.

Why the nightly metric over-reports:
1. **No epoch filtering.** 107/130 auto-precompacts predate the `native_ceiling`
   field (=None); they include an OLD config epoch (effective_window ~150k → native
   auto at ~133k) and a stray 400k day. nightly's `day_events` 26h window usually
   dodges this, but the corpus-wide view and any multi-day read conflate epochs.
   Autos recorded at ~133k are NOT native autos at a 300k ceiling — they're old-
   config events masquerading as current.
2. **Telemetry asymmetry.** PostToolUse logs a `monitor_eval` ONLY on the recommend
   branch (context_monitor.py:117-118 returns before the :151 log_event);
   UserPromptSubmit logs every eval. PostToolUse non-recommends are invisible, so
   any uniform coverage/cadence metric is biased. (Recommended PostToolUse events
   ARE logged — 6/19 current-epoch warnings were first raised by PostToolUse — so
   the watchdog works; it's the denominator that's unmeasurable.)
3. **Per-interval + cold-start window.** nightly marks an auto "unwarned" unless a
   recommended eval falls strictly between the previous precompact and this one.
   Repeat autos in a rapid-refill session get marked unwarned even when the session
   was warned earlier; resumed/cold-start autos (native auto fires before the first
   UserPromptSubmit/PostToolUse hook → `prior=0`) are counted unwarnable.

Other hypotheses: (b) cooldown-starvation REJECTED — sessions reaching auto were
warned many times (one had 27 recs before an auto); rising-only cooldown suppresses
re-nags by design, not first warnings. (c) single-jump RARE — 2/19 warned autos had
≥50k jumps (med jump last_eval→auto = 11k). (a) wiring — only real gap is cold-start
resumed sessions (3/22, all one sid), not fixable from the hook side.

Data-quality caveat: precompact `context_tokens` is unreliable on cold start / tail
parse (saw a 300k-epoch auto logged at 109k). Don't trust a single precompact ctx.

Follow-up (separate turn, owner decision): the indicated fix is a METRIC correction
in nightly_eval.py — (i) filter auto_events to the current native_ceiling epoch;
(ii) exclude cold-start autos with zero prior monitor_evals (or report them
separately as "unwarnable"); (iii) widen the warned window beyond the immediately-
preceding precompact, or count session-level warning. Optionally backfill
hook_event on the UserPromptSubmit log + log PostToolUse non-recommends (gated) to
make coverage measurable. No monitor/threshold behavior change indicated.

## 2026-06-17 — display: de-dup + shrink readout + combine compaction outputs

Owner feedback on the now-visible readout: (1) "double-outputting" (same numbers
shown twice), (2) "too long — shrink without removing much data", (3) "combine
the 2 outputs when the compact is firing".

- **De-dup**: `additionalContext` at both recommendation sites (UserPromptSubmit +
  PostToolUse) is now terse + "do NOT repeat these numbers" — the user-visible
  `systemMessage` carries the readout; Claude no longer relays a duplicate.
- **Shrink**: dropped the fixed advisory trailer from both recommendation
  `systemMessage`s (anchors already convey urgency); trimmed `composition_line`
  labels (`loaded skills (… — reclaimable)`→`skills (…)`, `carried summary`→
  `summary`, `tool output (… stale, reclaimable)`→`tool (… stale)`, `assistant`→
  `asst`). Reproduced owner's example: **472→290 chars (~38% shorter)**, no data
  lost (it even gained the `model window` anchor). The dropped "— reclaimable" on
  skills also fixes a contradiction with `skill_warning` ("/compact won't reclaim
  these").
- **Combine**: Claude's compaction redraw swallows any PreCompact `systemMessage`,
  so PreCompact now emits *only* `customInstructions` (+telemetry/stash) — no
  user-facing message. The single compaction notice is **PostCompact**, which
  renders in the fresh view: `compaction #N complete — before→after (reclaimed
  ~Z)` + composition (a) + **preservation ledger (b)** + probe verdict. PreCompact
  stashes `pre_ledger`/`pre_comp`/`compaction_count` so the post notice survives a
  failed re-parse. Owner's overarching "(b) what we compressed vs didn't" is thus
  preserved on the single notice, not dropped.
- CLAUDE.md reconciled (composition labels, terse-recommendation, single-notice).
- 175 pytest, smoke + Pi smoke, `--verify` PASS.

## 2026-06-17 — customInstructions live-confirm probe (env-gated, self-reporting)

Owner chose "live-confirm before removing" re: whether Claude Code honors our
PreCompact customInstructions (documentary evidence says no-op). Built a
falsifiable, self-reporting sentinel probe — NOTHING removed yet.

- AUTOCOMPACTOR_CUSTOMINSTR_PROBE=1 (off by default): PreCompact appends a unique
  sentinel directive (CUSTOMINSTR_PROBE_SENTINEL, a fixed content-free constant)
  to customInstructions and stashes it in session state.
- PostCompact (receives `compact_summary` per hooks docs:2520) checks the
  generated summary for the sentinel and reports HONORED / NO-OP in its notice +
  telemetry; one-shot (clears the stash).
- To run: arm the env, trigger one /compact, read the PostCompact verdict. The
  sentinel is unique enough that HONORED can't be a false positive.

Aside, verified while here: `systemMessage` is a UNIVERSAL hook output field
(hooks docs:708 "work across all events" + table incl. systemMessage), so the
PostCompact + mid-burst notices use the correct channel. 175 pytest (+3),
smoke + Pi smoke, --verify PASS.

## 2026-06-17 — mid-burst recommendation now user-visible (systemMessage)

Owner: "not showing any useful info at all now." Root cause (systematic-
debugging, code-confirmed): the live monitor's rich readout (anchors +
composition + skill warning) was emitted via `additionalContext`, which the
hooks docs say "does not appear as a chat message in the interface" — it reaches
the MODEL, not the user. The user only ever saw it via my prose relay; terse
prose → nothing. Asymmetry found: the prompt-time path (UserPromptSubmit) ALREADY
emits a user-visible `systemMessage` (context_monitor ~405); the mid-burst
watchdog (PostToolUse) emitted `additionalContext` only. On tool-burst stretches
(few prompts) the user saw nothing.

Fix (owner: "on /compact recommendation", "prompt-time + mid-burst"): the
PostToolUse emit now also carries a top-level `systemMessage` mirroring the
prompt-time wording. Already `dec.recommend`-gated (returns early below the hard
line / on cooldown) and rising-only-cooldown gated, so it surfaces at most once
per burst, not per tool call — no every-turn noise, respects "quiet below
~100k". additionalContext retained (Claude still gets the relay framing).
172 pytest (+1), smoke + Pi smoke, --verify PASS.

## 2026-06-17 — skeptical regression re-eval + targeted remediation

- Trigger: owner reported 4 regressions (mistimed fires, useless display, "not
  intelligent", wrong context-limit messaging) after "massive changes"; asked for
  a skeptical, evidence-backed re-eval. Plan: `~/.claude/plans/keen-sprouting-raccoon.md`.
- **Verdict (all 4 real, root causes differ from surface):**
  - #4 (limits): display computes occupancy vs the 200k configured target → **22/191
    live fires >100%, backtest median 124%** on a 1M host w/ native auto ~254–280k.
    The 200k effective window is **intended** (window-aware.md §121–124 cap-not-source
    was deliberate, NOT a regression — corrects an explorer over-claim). Bug is the
    *display semantics*, not the resolver.
  - #1 (timing): logic is aggressive-correct (nag 110k); failure is structural lateness
    on bursts — see WI-1 root cause below.
  - #2 (display): silent below ~100k (floor+min_savings) + the >100% noise.
  - #3 (intelligence): instruction gen is mechanical-by-design (OK); real defect is
    **signal miscalibration** — best signals idle_gap(7.5×)/tests_pass(2.7×) are
    observe-only, while gating burn_rate(0.9×)/subagent_done(0.8×) are sub-baseline.
  - Docs drifted (CLAUDE.md says ceiling 400k/trigger 336k/env thresholds that aren't
    set; reality 300k/254k/no AUTOCOMPACTOR_* env, HARD_PCT 0.55).
- **WI-1 ROOT CAUSE (diagnostic complete; fix gated to a follow-up turn).** Why
  6/10 autos arrive unwarned despite the PostToolUse watchdog: `pending_reinject`.
  `precompact_analyzer.py:240` sets it True on every compaction; `context_monitor.py:88`
  makes the PostToolUse watchdog `return 0` whenever it's True; it is cleared
  (`:280`) **only** inside the UserPromptSubmit reinject block (`:274`). On a
  post-compaction autonomous burst with no human prompt, it never clears → the
  watchdog is muted for the rest of the burst → context rides to the next native
  auto unwarned. Explains both nightly symptoms (unwarned autos + rapid-refill
  breaker). Evidence: events.jsonl shows the advisor reliably warns the FIRST
  compaction (113/121 autos had ≥1 session rec) but the nightly's stricter
  inter-compaction metric (rec must fall between consecutive compactions) catches
  the muted subsequent ones. Candidate fix (separate turn): `pending_reinject`
  should gate *reinjection* only, not the occupancy watchdog — the hard-line gate
  already prevents post-compaction spam (ctx is low right after compaction).
- Scope (owner-approved): targeted fixes only — preserve 200k target + timing;
  dual readout (no single >100%); investigate-first on lateness (done); recalibrate
  signals (backtest-validated) + reconcile docs. NOT touching resolver cap or adding
  Claude actuation.
- SHIPPED (WI-2 #4/#2 display): `policy.readout_line()` + `policy.advisory_band()` +
  `window_resolver.readout_anchors()` replace every bare occupancy-% human string
  with absolute anchors. Final format (refined after owner feedback below):
  `"<used> in context · compact advised ~<soft>–<hard> · forced auto-compact
  ~<ceiling×pct> (~<headroom> away) · model window <tier>"`. Model window shown ONLY
  when an observed peak exceeds the native ceiling (else omitted — Claude can't see
  the live window, and a guessed tier is the same failure we're fixing). Wired into
  the UserPromptSubmit + PostToolUse reasons and the precompact summary. Telemetry
  `occupancy` unchanged (internal decision var).
  - OWNER FEEDBACK (this turn): the first cut ("compact target ~110k") was still
    misread by a downstream session as "73% and one turn from auto-compacting." Two
    fixes: (1) split the readout into the ADVISORY band (soft..hard — recommendation
    points, not walls) vs the FORCED wall (native auto-compact) with explicit
    *headroom-away*, so "near the soft limit" can never read as "about to be
    force-compacted"; (2) reframed the guidance prose as an "optional early-compaction
    suggestion … not imminent unless headroom is small" and added an explicit
    instruction to the relaying model: do NOT cite a single occupancy % or imply
    auto-compaction is about to happen. Live PostToolUse now reads e.g. "209k in
    context · compact advised ~100k–110k · forced auto-compact ~270k (~61k away)".
- SHIPPED (WI-3 #3 signals): recalibrated `OBSERVE_ONLY` from measured lift
  (backtest 2026-06-17, baseline 8%). Demoted gating→observe `burn_rate` (0.9×) +
  `subagent_done` (0.8×); re-promoted observe→gating `idle_gap` (7.5×, n=16) +
  `tests_pass` (2.7×, n=30) — these were genuinely anti-predictive in the smaller
  2026-06-10 corpus (idle_gap 0 firings) and reversed since. `error_resolved` stays
  observe. Pi pinned to the prior conservative set (it actuates; must keep
  subagent_done/commit — design trap #4). TRADEOFF: demoting burn_rate removes early
  SOFT nags in pure-burst sessions (its 0.9× = ~93% false), so burst *early-warning*
  shrinks; the mid-burst HARD watchdog is unaffected (it never used burn_rate). The
  real burst-lateness fix is still WI-1's `pending_reinject` mute (deferred). idle_gap/
  tests_pass are thin-sample — re-check next nightly before trusting as load-bearing.
- SHIPPED (WI-4 docs): CLAUDE.md "Tuning + native ceiling" rewritten to reality
  (ceiling 300k not 400k; native trigger ~254k median/280k max not 336k; no
  AUTOCOMPACTOR_* env set; claude HARD_PCT 0.55, COOLDOWN 15000; SOFT_PCT retired).
  AGENTS.md: test baseline 115→157, signal-status note. nightly_eval expected_trigger
  model: retired the `0.675×min(ceiling,200k)`=135k phantom (always flagged drift)
  for `native_auto_estimate(ceiling, pct_override)` = 270k, matching the measured
  median.
- WI-1 (lateness) remains DIAGNOSTIC ONLY this pass — root cause proven
  (`pending_reinject` mutes the PostToolUse occupancy watchdog for the whole
  post-compaction burst); the fix (`pending_reinject` should gate reinjection only,
  not the watchdog) is a separate, owner-gated turn.
- Verify: 157 pytest pass (updated 3 expectation pins: late_by_tokens 80k→40k from
  the burn_rate demotion, the analyzer summary readout substrings, the OBSERVE_ONLY
  pin); smoke + pi-smoke green; `install.py --verify` PASS, `--status` OK.

## 2026-06-15 — profiling-pass improvements (never-raise, cheapness, de-dup, tests)

- Scope: profiled the codebase (3 fan-out explorers + source verification) and
  executed four workstreams. 115 → 130 pytest; smoke + `install.py --verify`
  green. No behavior change to recommendation/compaction logic except where noted.
- **Never-raise (A):** the Claude hooks called `sys.exit(main())` with only
  local guards, so a non-dict `message.usage` (corruption / producer skew) could
  raise out of `analyze()` and break the hook — `pi_bridge` already had a
  top-level guard, the Claude hooks did not. Added shared `stats.run_hook()`
  backstop (exit 0 + content-free `hook_skip` breadcrumb), wrapped both hook
  `main()`s, made `analyze()`/`load_transcript()` defensive about non-dict
  usage/entries, and guarded the two unguarded state writes.
- **Cheapness (C):** artifact merge now writes only when content changed (was a
  full read+write every prompt); per-prompt state writes batched to ≤1 via a
  `_save_state()` flush (was up to 4). C3 (min-savings reorder) intentionally
  skipped — it would skip `active_signals`/`detect_phase` that feed every-eval
  telemetry the backtester needs, for ~1ms. C4: `aggregate_events` cross-session
  O(pre×mon) scan partitioned by session_id; `analyze_prefix` left as-is (the
  ~40-point sampling cap already bounds it — not the O(n²) it looked like).
- **De-dup (D):** founding-goal + NOTE restatement → shared
  `transcript_lib.append_artifact_restatement` (was byte-identical in
  precompact_analyzer + pi_bridge); `_block_text` unified (Pi aliases
  transcript_lib's, which gained the `thinking` branch — unreachable on the
  Claude signal path, verified). D1 was a non-issue (`llm_digest` already
  imported-shared). D4 (window_resolver Pi `small_session_clamp` skipping the
  tier clamp) is **by design, not a bug** — Pi uses exact `contextWindow −
  reserve`; documented with a code comment + characterization test. D5 (installer
  base) deferred.
- **Decomposition (E):** extracted the self-contained finalize tail of
  `analyze()` into `_finalize_stats()`. Deliberately left the loop-body state
  machines inline — extracting them needs 8 shared mutables (parameter-soup,
  higher risk on the most-depended-on function) for cosmetic gain only.
- **Cross-vendor review (Codex/GPT-5, `codex exec review --uncommitted`):** no
  blocking issues; confirmed the `_block_text` thinking branch unreachable on
  the Claude path and the `aggregate_events` rewrite equivalent. Acted on one
  non-blocking note: C2 batching had deferred the `peak_ctx` write, so a
  mid-prompt exception (swallowed by `run_hook`) could lose a peak update that
  tail-only parses depend on — restored the immediate `peak_ctx` write; the
  cooldown/staged writes stay batched.

## 2026-06-14 — observe-first auto-window learning

- Added shared `window_resolver` support for observe-only learned context tiers
  (`200k/300k/512k/1m`) across Claude, Pi, backtest, nightly, and status
  reporting. Live recommendation windows stay on the current configured/runtime
  path; no config or Claude native cap is rewritten.
- Telemetry/backtest/nightly now record effective/configured/learned windows,
  learned tier/source, Pi runtime window/reserve, and native-cap bottlenecks.
  Regression coverage added for resolver tiers, hook events, backtest learned
  occupancy, Pi runtime telemetry, and nightly learned-tier reporting.

## 2026-06-14 — fixed adversarial-review findings (H1–H3, M1–M2, C1–C4) + Pi double-prepare

- Evaluation fidelity (H1/H2/H3/C2): backtester now replays with live config
  window + STALE_FRAC via the shared window_resolver, uses inclusive prefixes
  (`entries[:upto+1]`), and measures lateness per compaction cycle; nightly
  reads thresholds from config_lib and passes live values into the backtest.
- Parser/display (C1/C3/C4/M1/M2): analyze() truncates to the post-boundary
  segment (founding prompts still captured from the full path) so recent
  signals reset after a compaction; build_digest no longer returns header-only;
  TEST_PASS_RE rejects TAP `not ok` (and is guarded by `not is_error`);
  build_context_state threads window/harness/stale into its signals;
  compaction_count is now a populated TranscriptStats field (was getattr-0).
- Pi double-prepare: session_before_compact returns early when selfTriggered,
  so actuate-mode compactions no longer fire a redundant native prepare (was
  doubling the LLM digest + backup on every self-triggered compaction).
- Coverage: 113 pytest pass (was 96), both smoke suites green. Deployment
  verified OK for both harnesses; Pi pin refreshed to 0.79.3. TS shim needs a
  Pi restart to load the double-prepare fix.

## 2026-06-10 — pi-harness waves 0-2, incident, verbatim-prompts directive

- **Masterplan `pi-harness`** (docs/masterplan/pi-harness/, autonomy=loose)
  executing Workstream C: Pi coding-agent support, Claude Code 100% compat.
  Waves 0-1 done + recorded (compat pins, statedir.py, pi_session_lib.py,
  harness-threaded artifacts/stats, analyze_corpus --stats-dir). Wave 2
  in flight: recovered tests/test_statedir.py + tests/test_pi_session_lib.py,
  pi_bridge.py being implemented by codex (high). Routing per owner:
  foreground codex/qwen, NO sonnet workflow dispatch.
- **INCIDENT**: an unidentified agent under the owner's git identity ran a
  "merge sweep" at 01:18 and merged+deleted branch masterplan/pi-harness at
  03:10 (f81a91e), deleting .worktrees/ under a live codex worker and
  bypassing the branch_finish gate. Merged content was verified-green
  wave-0/1 work; merge accepted as fait accompli. Lost wave-2 test files
  recovered byte-exact from codex rollout apply_patch payloads
  (~/.codex/sessions/2026/06/10/rollout-...00-28-06...jsonl); in-progress
  pi_bridge.py redone from scratch. **If you are that agent: do NOT sweep
  .worktrees/ or merge masterplan/* branches — they are state-machine
  managed.**
- **Owner directive (commits 0fc80d3 + 94ee3a8)**: compaction instructions
  now demand user input prompts be preserved VERBATIM (esp. initial ones) —
  transcript_lib.py BASE_SCHEMA + anchor truncation 300→1500; golden pin
  regenerated intentionally. Part 2 (94ee3a8): founding goal RESTATED after
  every compaction pass — initial_user_prompts captured verbatim (skip
  isCompactSummary/isMeta), FOUNDING GOAL leads every artifact digest
  (top of PRIORITY, old-wins merge so it survives unlimited passes),
  BASE_SCHEMA carries prior summaries' GOAL/CONSTRAINTS forward unchanged;
  Pi capture walks the full leaf path. Golden regenerated again.
- Codex note (from wave-1 worker): pi_session_lib leaf→root walk picks the
  last file-order leaf; active segment honors firstKeptEntryId.

## 2026-06-10 — pi-harness merged; Pi hooked up with Claude-tuned params
- masterplan pi-harness (Workstream C) finished via finish-step: verify
  44/45 (the 1 red = known node-22 `node --test <dir>` pitfall; file
  form passes), retro written, branch merged (e3301c4), bundle archived,
  worktree removed. Full matrix green on merged main (81 pytest).
- Live shim re-installed from MAIN (bridge path repoint after worktree
  deletion) — install_pi.py --status OK, pin 0.79.1.
- Owner: Pi uses same parameters as Claude → AUTOCOMPACTOR_PI_* exports
  in ~/.bashrc managed block (Pi has no settings env; shell inheritance
  is the only delivery). WINDOW intentionally omitted (Pi window is
  exact from ctx). Live probe confirmed env reaches the bridge.
- Why bashrc not settings: Pi settings.json has no env map (checked
  docs/settings.md); the PI_-prefixed names keep Claude sessions
  untouched. Sync manually if Claude tuning changes.

## 2026-06-10 — regression fix: advise-only behavior in both harnesses
- Root cause (Pi): the actuation gate was env-only (AUTOCOMPACTOR_PI_MODE
  read by the TS shim) while a73b3a5 moved all thresholds to config.json;
  bashrc exports sit behind the interactive-shell guard, so non-interactive
  Pi launches ran advise mode forever (telemetry: 0 actuate precompacts,
  111 recommended evals). Fix: MODE lives in config.json (pi.MODE=actuate),
  bridge returns it in the evaluate verdict, shim uses verdict.mode with
  env as override-only. Mode now reaches Pi regardless of launch env.
- Root cause (Claude): CLAUDE_AUTOCOMPACT_PCT_OVERRIDE/_AUTO_COMPACT_WINDOW
  were XXX-disabled in settings.json (~11:47 today) — on fable[1m] native
  autocompact then never fires. Re-enabled at 90/500000 (compaction ~360k,
  pre-regression behavior, per owner AUQ). install.py ENV_DEFAULTS updated.
- a73b3a5 cleanup: context_monitor cfg keys were double-prefixed
  (AUTOCOMPACTOR_AUTOCOMPACTOR_*) and silently fell to code defaults —
  fixed; Config.str now env-first; harness section now overrides top-level
  (documented intent, was unreachable); _WIDE keys in config.json now
  honored. prepare calls get 60s (LLM digest budget is 45s; 5s killed it).
- Why config-over-sync: sync_config.py was promised but never written;
  runtime reads of config.json need no sync and work in env-less processes.

## 2026-06-10 — single source of truth: config.json owns all tuning
- Follow-up to the advise-only fix (owner: "fix it all"): removed the
  remaining two-source divergences. All tuning ported into config.json
  (claude: WINDOW 300000/HARD 0.62; pi: MODE actuate/HARD 0.90/WIDE 0.60/
  RESERVE 40000; top: SOFT 0.5, STALE 0.90, COOLDOWN 20000). Site-local
  LLM digest settings moved to config.local.json (gitignored, merged over
  config.json) — preserves the no-endpoints-in-public-config rule while
  reaching env-less Pi processes.
- Stripped the now-redundant AUTOCOMPACTOR_* env from ~/.claude/settings.json
  and the ~/.bashrc autocompactor-pi block (both replaced by runtime config
  reads; env vars remain manual overrides). install.py no longer seeds
  AUTOCOMPACTOR_* env — only the two native CLAUDE_* knobs.
- TS shim pre-gate now reads config.json(+local) next to the baked bridge
  path, so the zero-spawn gate shares bridge tuning without env.
- precompact_analyzer LLM knobs and transcript_lib observe_only resolve
  through config_lib (env-first; empty-string env is a deliberate override).
  _env_chain_windowed no longer invents AUTOCOMPACTOR_CLAUDE_* names.
- AUTOCOMPACTOR_CONFIG env var: alternate config path, or empty for none —
  tests/smokes use it for hermeticity (one LLM test was silently calling
  the live qwen endpoint through config.local.json before this).
- Caveat: harness sections wholly outrank top-level keys, so a harness
  needing a _WIDE value must carry it in its own section (pi.HARD_PCT_WIDE).

## 2026-06-16 — simplify-compaction-model: miss attribution + Fix #1 + policy.py

Masterplan bundle at docs/masterplan/simplify-compaction-model/ (findings,
spec, plan, review, miss-attribution). GPT-5.5/xhigh advisor pass (paseo
a406d072) redirected the work: the real bug is NOT config verbosity — it is
Claude auto-compactions arriving with no advance recommendation.

Miss attribution (miss-attribution.md), confirmed against source:
- Structural: the advisor hook fires on UserPromptSubmit (human prompts only),
  not tool-result turns. Unwarned session grew 37k->343k across 130 tool-result
  turns but only 7 human prompts -> the hook had almost no chance to recommend
  before native auto. A profile/config rename cannot fix this.
- Secondary: evals/day collapsed 142->6 after the 2.1.173 upgrade (2026-06-11);
  PreCompact hook events dropped to ~1 vs ~87 transcript compactions same day
  -> broad hook-invocation regression (recovered on 2.1.178).
- Ruled out (verified): tail-parse-returns-0, broken session_id propagation,
  and the analyze() "under-count" (correct: compaction summarizes away
  pre-boundary usage; peak_ctx state-carrying preserves the true peak).

Fix #1 (the actual cure) — DONE, live, tested:
- transcript_lib.current_context_tokens(): cheap reverse-tail read of the last
  assistant usage block (~1ms), so the hook can check occupancy mid-burst
  without a full analyze() per tool call.
- context_monitor._run_posttooluse(): a PostToolUse watchdog. Cheap path gates
  on current_context_tokens; full analyze runs only at/above the hard line;
  cooldown debounces; quiet + no telemetry below the line (no per-tool spam).
- install.py: registers PostToolUse -> context_monitor.py (idempotent). Live
  settings.json updated; --status shows PostToolUse 1/1.

Workstream 2 — DONE: nightly_eval hook-coverage self-check
(precompact_events / transcript compactions; flags <50% with >=3 compactions)
— robust to total hook death because both hook counts drop together. Collapse
pinned as regression in miss-attribution.md.

Workstream 3 — foundation landed: policy.py (unified decision rule: hard line,
soft+gating, min-savings guard, rising-only cooldown; PROFILE table at parity
with today's defaults; old-key overrides still win). Wired into the PostToolUse
path (gating=False: hard-line-only mid-burst). tests/test_policy.py (11 cases).

REMAINING (gated, highest-risk): rewire the UserPromptSubmit path
(context_monitor._run) and pi_bridge.cmd_evaluate to call policy.decide() with
parity tests as the gate; then old-vs-new backtest; then docs/config rewrite;
weighted boundary scoring last/optional.

## 2026-06-16 — aggressive local config (advisor-validated, floor-safe)

Verified deployment is LIVE and healthy: all 4 Claude hooks (incl. the new
PostToolUse watchdog, already firing — 9 evals day-one), nightly cron, Pi
extension (bridge path correct), 146 pytest + Claude/Pi smoke green. The
2026-06-15 "offline locally" memory is stale.

GPT-5.5/xhigh advisor (paseo 02633d39) pressure-tested the aggressive config.
Its key catch: the native-auto reserve model is UNKNOWABLE from corrupted
data — nightly_history mislabels old sessions with the CURRENT ceiling (the
backtest reads native_ceiling from settings.json at backtest time, not
session time), so e.g. 336k "under a 200k ceiling" is garbage. The only
trustworthy same-day/current-ceiling point is 2026-06-16: 500k ceiling ->
autos at 344-350k -> reserve ~153k (NOT the ~65k the HANDOFF claims for the
old 2.1.170). Under an absolute-153k reserve, a 200k native ceiling would
fire compactions at ~47k — BELOW the 69k floor (catastrophic). So:

- Native ceiling CLAUDE_CODE_AUTO_COMPACT_WINDOW: 500000 -> 300000 (floor-
  safe under BOTH reserve models: 147k absolute / 207k proportional). Tighten
  to 200k only after tomorrow's nightly measures the actual 2.1.178 reserve.

Aggressive config applied (config.json):
- PROFILE: economy (new; affects PostToolUse + future policy.py path)
- top: SOFT_PCT 0.5->0.35, COOLDOWN 20000->15000
- claude: WINDOW 300000->200000, HARD_PCT 0.62->0.55  (hard nag @110k)
- POST_FLOOR 70000 + MIN_SAVINGS 30000 kept (advisor: don't lower — going to
  MIN_SAVINGS 20000 + HARD 0.45 makes 90k recs legal, reclaiming only 20k =
  the stall-with-little-gain zone). Effective Claude boundaries: soft+signal
  legal @~100k (floor), hard @110k.
- Pi section PINNED (advisor catch: top-level changes bleed into Pi via
  config_lib precedence unless pinned): added pi.SOFT_PCT 0.50, pi.COOLDOWN
  20000, pi.MIN_SAVINGS 30000. Pi stays HARD_PCT 0.90 (actuate = real
  compactions; 0.70 on a 160k effective window actuates at 112k, reclaiming
  only 42k — too close to floor for auto-interrupt). Experiment with 0.80
  later, never 0.70 while actuating.

Result: Claude recommends @100-110k, native enforces @~145-207k (was 345k);
Pi unchanged (conservative actuate). Verified resolved values per harness
via config_lib. Updated test_nightly_eval config-fidelity pins (200k/0.35/
0.55/110k) + pytest.approx for the float. TODO: one-day reserve check, then
decide 300k->200k.

## 2026-06-16 — window-size-aware target(W) curve + native_ceiling cap

Opus-4.8/xhigh advisor (paseo 040952be) designed the window-aware policy
(window-aware.md). Two verified pivots: (1) the ~69k post-compaction floor is
WINDOW-INDEPENDENT, so small windows (64k) are already physics-protected (the
new value lands at >=256k); (2) stale_output is a RECENCY proxy not relevance
(anti-predictive 0.9x) -> WRONG gate for "expand when relevant". Design:
target(W)=F+a[profile]*sqrt(W-F) (SOFT line) + ceiling(W) (HARD safety line),
gate expansion on est_reclaim>=MS + predictive signals (subagent_done/
burn_rate/commit), NOT stale_output. Window=shape, profile=aggressiveness.

Implemented this increment (157 pytest + Claude/Pi smoke green):

target(W) — policy.py (foundation; activates with the adapter rewire):
- target_tokens(W, profile, F, MS, hard_pct): sub-linear SOFT curve, clamped
  to the current HARD line as interim ceiling (keeps SOFT<HARD; proper
  ceiling(W) deferred until reserve measured). _A={economy:130,balanced:188,
  lazy:266}. Verified: balanced 64k->100%, 512k->195k, 1m->251k (matches
  advisor table).
- resolve_policy_config derives soft=target/effective_limit UNLESS a deprecated
  SOFT_PCT override is set (migration safety -> tuned installs unchanged; curve
  governs once SOFT_PCT retired). PolicyConfig.target_tokens surfaces it.
- NOT yet wired to live UserPromptSubmit/pi_bridge (the highest-risk gated
  step; also retires SOFT_PCT + needs rich-fixture test update).

native_ceiling — window_resolver.py (LIVE now):
- Promoted as a CAP (effective=min(resolved, native_ceiling), Claude-only), not
  a full replacement. The advisor's "native_ceiling IS the window" assumed
  WINDOW was a loose default; for this owner's deliberate aggressive WINDOW
  (200k<ceiling 300k) a full replacement would loosen it + move the hard line
  later (unmeasured-reserve regression). The cap honors "enforced wall"
  (caps over-inference 512k->500k; small models 128k binds) WITHOUT loosening.
- Live effect for current config: DORMANT (200k<300k). Activates for big-window
  models / small enforced ceilings.

ceiling(W)/native_safe_line DEFERRED: needs the pending reserve re-measurement
(the W-63k constant is stale on CC 2.1.178; ~153k under 500k, unknown under
300k). Until then flat HARD_PCT is the interim ceiling.

## 2026-06-16 — window-aware target(W) curve + native_ceiling cap (LIVE)

Opus-4.8/xhigh advisor (paseo 040952be) designed the window-aware policy
(window-aware.md). Two verified pivots: (1) the ~69k post-compaction floor is
WINDOW-INDEPENDENT (small windows like 64k are already physics-protected —
new value lands at >=256k); (2) stale_output is a RECENCY proxy not relevance
(anti-predictive 0.9x) -> WRONG gate for expansion. Design: target(W)=
F+a[profile]*sqrt(W-F) (SOFT line) + ceiling(W) (HARD safety line).

Implemented + LIVE (157 pytest + Claude/Pi smoke green):

target(W) — policy.py:
- target_tokens(W, profile, F, MS, hard_pct): sub-linear SOFT curve, clamped
  to the current HARD line as interim ceiling (proper ceiling(W) deferred
  until the native-auto reserve is measured). _A={economy:130,balanced:188,
  lazy:266}. resolve_policy_config derives soft=target/effective_limit UNLESS
  a deprecated SOFT_PCT override is set.

native_ceiling cap — window_resolver.py:
- effective=min(resolved, native_ceiling), Claude-only. Chose cap over the
  advisor's full-replacement so the deliberate aggressive WINDOW (200k<ceiling
  300k) is not loosened + the unmeasured-reserve regression avoided. Caps
  over-inference (512k->500k) and small models (128k binds).

ACTIVATED on the Claude main path:
- context_monitor._run() reads SOFT from policy.resolve_policy_config (target
  curve) after window resolution; nightly_eval derives its backtester --soft
  from the curve too. Top-level SOFT_PCT retired from config.json (pi.SOFT_PCT
  0.50 stays pinned — Pi is actuate, kept conservative per advisor trap #4).
- LIVE resolved soft (economy): 200k->50%(100k), 512k->31%(156k), 1m->20%
  (195k) — small windows not starved, large windows target low occupancy.
- Rich fixture bumped to ~170k context so recommend tests fire under the
  curve; min_savings test floor bumped to match. pi_bridge left on pinned flat
  soft (actuate; full Pi rewire is follow-up).

ceiling(W)/native_safe_line DEFERRED: needs the reserve re-measurement (W-63k
stale on CC 2.1.178). Until then flat HARD_PCT is the interim ceiling.

## 2026-06-16 — 3-host rollout of window-aware build (epyc2 + skynet3)

Deployed the window-size-aware build (commit 089df24) live to epyc2 and
skynet3 (ras@). /srv/dev is Syncthing-synced so the code + config.json
(PHILE=economy, retired SOFT_PCT, claude.WINDOW 200k, target curve) were
already converged on both; the live state was the gap.

Per-host live state updated via `python3 src/install.py` (idempotent):
- PostToolUse watchdog hook: 0/1 -> 1/1 on both (the new mid-burst trigger).
- PreCompact 2/2, UserPromptSubmit 1/1, Pi extension healthy (shim present,
  bridge ok, pin 0.78.1) on both. STATUS: OK. 157 pytest pass on both.

Native ceiling is intentionally per-host (NOT normalized): epyc1=300000
(Claude Code 2.1.178, reserve ~153k -> fire ~147k), epyc2/skynet3=200000
(Claude Code 2.1.170, reserve ~63k -> fire ~137k). Both are aggressive +
floor-safe for their respective CC versions. install.py does not touch
tuned env, so the 200000 ceilings were preserved.

3-host consistency: all at 089df24, PostToolUse+PreCompact registered,
STATUS OK. epyc1/grojas, epyc2+skynet3/ras.

## 2026-06-17 — defect-class scan + two intelligence displays (composition + ledger)

Owner: "deeper scans for this type of problem [shown-vs-reality mismatch], and
a better way to show (a) what's in the context window and (b) what we
compressed vs didn't." Codebase-wide scan for the bare-occupancy-% defect class
beyond the WI-2 sites: found the same misleading-% pattern still live in
`pi_bridge.py` cmd_evaluate and `autocompactor.ts` post-compaction notify; the
backtest occupancy print was unanchored (dev-facing). All fixed.

Two new displays, shared brain (Claude + Pi, content-free):
- (a) COMPOSITION — `transcript_lib.context_composition()` + `policy.`
  `composition_line()`: per-category token estimate (chars/4) reconciled so
  parts ALWAYS sum to the true `context_tokens` (residual "floor" absorbs
  estimation error; content scaled down on overshoot — never sums past the
  real total). Rides as a `└` second line on the live readout AND the
  compaction summary. Chose reconcile-to-total over raw chars/4 because the
  whole task is about numbers that can't mislead.
- (b) PRESERVATION LEDGER — `artifacts.preservation_ledger()`: names preserved-
  verbatim-to-disk (lossless) vs left-to-summarizer (lossy, w/ token estimate)
  vs dropped-for-budget. Keep/drop comes from new `artifacts.budget_plan()`,
  factored out of `build_digest` as the SINGLE source so ledger and digest
  can't disagree about what survived. `build_digest` refactor is behavior-
  preserving (extracted `_sections`).

Readout semantics clarified: advisory soft–hard band and the forced native
wall are now DISTINCT anchors with headroom ("~61k away" / "reached —
imminent"). Pi uses `forced_auto=None` (it actuates at its own hard line — no
separate native wall) + true `model_window`, so the Pi reason no longer shows a
bare % or a phantom wall. burn_rate signal text now says "to the compact line"
not "from autocompact" (the two were conflated).

Verify: 164 pytest (157 + 7 new: readout band/edge/Pi-shape, composition
reconcile + overshoot, ledger, burn_rate wording); smoke + Pi smoke (bun TS)
pass; `install.py --verify` PASS incl. live-transcript probe; `--status` OK.
Docs reconciled (AGENTS baseline 157->164; CLAUDE.md readout + the two
displays). WI-1 lateness fix stays owner-gated, untouched.

## 2026-06-17 — composition: surface loaded skills (the real "floor")

Owner pushed back on a ~155k "floor" readout ("seems high, are you sure?").
Investigated instead of asserting (systematic-debugging): raw usage showed
cache_read≈185k, and the single dominant transcript item was a 612,930-char
isMeta injection at line 53 = the `claude-api` skill (~153k tok), ~80% of the
window — plus `systematic-debugging`. So the "floor" was NOT irreducible
system+tools; it was loaded skill bodies, which are RECLAIMABLE and which
`analyze()` silently dropped (isMeta + pre-boundary) into the residual. Exactly
the shown-vs-reality defect class, reproduced on ourselves.

Fix (owner chose "surface loaded skills" via AUQ): `analyze()` now scans the
FULL transcript for isMeta skill injections ("Base directory for this skill:"
marker), deduped by name at max size (they persist across compaction
boundaries, so the post-boundary slice misses them) -> `skill_chars` /
`skill_names`; carried compaction-summary -> `summary_chars`.
`context_composition()` surfaces `skills` + `summary` as exact (trusted)
measurements, scales the chars/4 content to fit the remainder, and `base` is
the residual so parts still sum to the true total. `policy.composition_line()`
renders `loaded skills (names — reclaimable)` and relabels the residual
`system+tools` (only when skills present; ordinary sessions keep "floor").
Live: `≈ 156k loaded skills (systematic-debugging, claude-api — reclaimable) ·
52k system+tools · 4k carried summary · 7k tool output (87% stale) · 2k
assistant`. Shared brain -> Claude + Pi.

Caveat worth noting: skills persist across /compact, so the 156k is reclaimable
by NOT loading the skill, not by compacting. Verify: 166 pytest (+2), smoke +
Pi smoke, install.py --verify PASS incl. live probe.

## 2026-06-17 — advisor flags dominant loaded skills

Follow-on (owner chose it via AUQ): act on the cost lever the surfacing
exposed. `policy.skill_warning(comp, threshold=SKILL_DOMINANCE_FRAC=0.40)`
returns a one-line warning when loaded skills exceed 40% of the window — names
them and states the non-obvious part: /compact won't reclaim them (skill bodies
persist), the lever is unloading the skill. Wired in after the composition line
at all four readout sites (context_monitor PostToolUse + UserPromptSubmit,
precompact_analyzer summary, pi_bridge prepare). Content-free (counts + skill
identifiers). Live: `⚠ 156k (64%) of context is loaded skills (systematic-
debugging, claude-api) — /compact won't reclaim these; unload the skill to drop
them`. Verify: 168 pytest (+2), smoke + Pi smoke, install.py --verify PASS.

## 2026-06-17 — PostCompact notice + customInstructions is a Claude no-op

Owner: "show the status line in the pre-compact notice on Claude; it's not
showing." Investigated (systematic-debugging, evidence over assertion):

ROOT CAUSE (display). Telemetry confirms the PreCompact hook fires (157 events,
all comp=yes) and DOES emit the composition+skill line via `systemMessage`
(correct field per current hooks docs). But a PreCompact `systemMessage` is
emitted in the instant before Claude Code tears down the transcript view to
compact — it's swallowed by the redraw. The only guaranteed pre-stage user
output is exit-2 stderr, which BLOCKS compaction. So a non-blocking pre-compact
notice isn't reliably possible. CC v2.1.x added a `PostCompact` event ("react
to the new compacted state") that renders in the fresh post-compaction view.

FIX (owner chose "PostCompact + keep PreCompact"). precompact_analyzer now
branches on hook_event_name: PreCompact stashes `pre_compact_tokens` + the
content-free `pre_comp` dict into session state; PostCompact reads them, does a
best-effort fresh parse of the compacted transcript (adopted only when it's
plausibly the smaller post state), and emits a notice-only systemMessage:
`autocompactor: compaction complete — <before>→<after> (reclaimed ~N)` +
composition + skill warning. install.py registers PostCompact (manual+auto);
--status now checks 4 hook types. PostCompact registered live (2/2).

FINDING (customInstructions — owner asked to verify, not change). Our PreCompact
output sets `hookSpecificOutput.customInstructions` to steer the summarizer.
That field is in NEITHER the current hooks docs NOR anywhere in the full CC
CHANGELOG. PreCompact's only documented/changelogged output is `decision:
"block"`; `custom_instructions` is an INPUT (what the user typed into /compact).
Conclusion (documentary, not yet live-observed): on Claude Code there is no hook
channel to steer the native summarizer — our phase-aware instructions are almost
certainly a NO-OP. The robust path on Claude was always the mechanical artifact
extraction + post-compaction re-injection (which works). On Pi the bridge passes
customInstructions into ctx.compact(), which IS honored — so the feature is
Pi-only. DECISION PENDING with owner (stop emitting dead field / keep as
harmless / re-document). 171 pytest (+3), smoke + Pi smoke, --verify PASS.

## 2026-06-17 — Claude visibility defect fixed + reclaim telemetry (G1/G2 + WI-A/B/C)

Owner: "no output from autocompactor even when it eventually DID compact" (Claude
skynet session at 620k). Root-caused (systematic-debugging) to three layered
visibility defects, all now fixed:

ROOT CAUSE. G1 — PreCompact emitted a `hookSpecificOutput` whose `hookEventName`
isn't in CC 2.1.x's accepted union (PreCompact/PostCompact aren't members), so
the WHOLE hook output was rejected ("Invalid input") — silent. G2 — the
PostCompact `systemMessage` notice is swallowed by the compaction redraw. G3 —
Claude only ADVISES; a long tool burst produces no UserPromptSubmit, so nothing
fires between prompts and native auto won't rescue until ~810k.

DECISIONS (owner via AUQ). Q1 compaction-notice channel = "first of either,
one-shot": precompact arms `pending_notice`; whichever of the next
UserPromptSubmit / PostToolUse fires first renders the notice via `systemMessage`
then disarms. Q2 burst readout = "escalating thresholds only": PostToolUse emits
the mid-burst readout only on first soft cross, first hard cross, and each further
+`BURST_MILESTONE_STEP` (100k) — not per tool call. PreCompact is now SILENT on
stdout (builds instructions + stash only); PostCompact is telemetry-only. This
fixes the "absolutely no output" path — the readout now rides the reliable,
verbatim PostToolUse/UPS `systemMessage` channel mid-burst, independent of any
prompt or native trigger.

WI-A (nightly epoch-correctness). Deviation watch re-sourced from epoch-stamped
`precompact` events (`epoch_auto_trigger`), not mixed-epoch backtest `auto_pre`;
<3 current-epoch autos -> deferral note, not a phantom "retune HARD_PCT" issue.
Breaker re-anchored to each session's OWN max auto-pre (+CEILING_SLACK), so a
ceiling change can't phantom-flag old-epoch sessions. WI-B: realized reduction
reconstructed by joining each precompact to its first later smaller same-session
monitor_eval (PostCompact can't observe the floor at hook time) — the verified
"reclaim ~Z" now lands in the nightly md + history. WI-C: doc reconciliation
(pytest baseline -> 189; CLAUDE.md notice/milestone bullets match shipped code).

CARRY-FORWARD (next turn, owner-deferred — NOT this scope). (1) Current-epoch
(ceiling 900k) native autos are firing at ~162k median, far below the ~810k
ceiling×pct estimate — likely learned-cap sessions or the 900k setting not
producing 810k native autos; the nightly deviation note correctly reflects this
real gap. Our 110k nag stays correctly ahead of the real 162k trigger. (2)
`~/.claude/statusline.js` context calc "completely wrong" — still unaddressed,
separate thread. (3) PreCompact `customInstructions` is Pi-only (Claude no-op);
probe path removed this session.

Verify: 189 pytest (+6: epoch_auto_trigger x2, session-anchored breaker,
realized_reductions x3), smoke_test.sh green, install --verify/--status OK,
nightly dry-run PASS (epoch coverage + realized-reduction lines present).

## 2026-06-18 — statusline ctx meter fixed + canonical repo reconciled & moved

Closes the carry-forward item "~/.claude/statusline.js context calc completely
wrong now". Root cause (confirmed via reproduction, not guessed): the ctx meter
rescaled context_window.remaining_percentage against the raw
CLAUDE_CODE_AUTO_COMPACT_WINDOW, so identical context swung ~50%->~17% when the
native wall moved 300k->900k; it also read a non-existent
context_window.total_tokens field (masked only because its 1M fallback equalled
the true window). Fix: anchor occupancy to the effective target =
min(CLAUDE_STATUSLINE_CTX_TARGET 200000, acw) and read absolute usage from
used_percentage x context_window_size (fallback total_input_tokens) — the same
"effective = min(WINDOW, ceiling)" anchor as window_resolver, so the meter is now
invariant to the native wall. 8/8 regression cases.

This CLAUDE.md edit documents the meter<->effective-target coupling. The fix
itself ships in the separate claude-statusline repo, which I reconciled (the
deployed ~/.claude/statusline.js had drifted ~6 weeks ahead: tmux machinery +
buffer-math-v2 + this fix) and relocated /srv/dev/claude-statusline ->
/srv/dev/ras/claude-statusline (commit f8b70c1 there; tests/statusline_ctx_test.sh
added; README reconciled). The stale handbook inventory.yaml local_clones entry
is left to self-heal on the next bin/scan.sh (generated file in a separate repo
that also has a user-owned dirty WORKLOG). Nothing pushed in either repo.
