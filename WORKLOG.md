# WORKLOG

Terse handoff log for collaborating agents. Newest entry first.

A 2026-06-25 compression pass folded the verbose 2026-06-17 Claude display/
visibility iteration narrative and the closed/superseded HANDOFF sections into
summary blocks (decision rationale + ground-truth pins preserved). Per-entry
detail for pre-2026-06-20 work is recoverable from `git log` and the
docs/masterplan/* bundles.

## 2026-06-25 — HANDOFF/WORKLOG compression

Compressed HANDOFF.md and WORKLOG.md (owner chose "Moderate" via AUQ). Kept all
decision rationale, ground-truth pins, signal-precision results, and every Pi
entry from 2026-06-20 onward verbatim. Collapsed the 11x 2026-06-17 Claude
display/visibility iterations into one consolidated historical block (the
Claude adapter is removed; only the durable lessons + shared-brain displays
survive). Merged the two near-duplicate 2026-06-16 window-aware entries.
Updated the post-pivot follow-ups: item #1 (this WORKLOG entry) and item #2
(TS-shim dead-branch cleanup) CLOSED; item #3 (~/.claude/settings.json
deregister) and the inert nits still OPEN. No code change; decision record
preserved. Files: HANDOFF.md, WORKLOG.md.

## 2026-06-22 — AUDIT (fresh-eyes, Pi-only) + fixes for the findings

Full post-pivot audit (Opus synthesis + 2 GPT-5 codex adversarial passes [base
vs origin/main + working-tree] + a Sonnet verify pass + a direct 0.79.9 SDK
trace). Verdict was do-not-ship-as-is; fixed the CRITICAL/HIGH/MEDIUM findings
this turn. Code-level facts confirmed at source, not guessed.

- **CRITICAL — native artifact-loss race** (`autocompactor.ts` session_before_compact
  non-intercept): was `void bridge("prepare")` un-awaited then returned, so native
  compaction + `session_compact`→reinject could build the digest before prepare
  persisted artifacts/state (near-certain with the 45s LLM digest). Fix: `await`
  prepare (Pi awaits the handler, so this holds compaction) + pass `--skip-llm`
  (customInstructions are discarded on the native path, so only the cheap on-disk
  artifacts/state matter — no 45s stall). pi_bridge `cmd_prepare` honors `--skip-llm`.
- **HIGH — sticky `selfTriggered` brick** (actuate + intercept): flag set before
  `ctx.compact()`, cleared only in callbacks; a sync throw / unhandled rejection
  left it stuck → all future compaction wedged. Fix: new `safeCompact()` settles
  once (onComplete|onError|sync-throw|promise-reject), always clearing the flag.
- **HIGH — advise advisory on the invisible channel**: recurring agent_end advisory
  was `deliverAs:"followUp"`, which 0.79.9 swallows while streaming (agent_end runs
  before isStreaming clears — verified in agent-session.js). Fix: route it via
  `nextTurn` deduped by text (so it can't pile up — the original followUp rationale).
  `Deliver` type narrowed to `"nextTurn"`; no visible status uses followUp now.
- **MEDIUM**: shim now passes `--reserve` to `evaluate`; dropped the TS-only
  `AUTOCOMPACTOR_PI_*` threshold aliases + dead `CFG.pi` lookups (gate now shares
  the bridge's `AUTOCOMPACTOR_*` namespace — closes HANDOFF open-item #2; PI_MODE/
  PI_INTERCEPT control flags kept); `cmd_reinject` now resolves the window via
  `window_resolver` (runtime `--context-window`) like evaluate/prepare.
- **Rollout**: kept `MODE=actuate` (the race/brick that gated it are now fixed);
  README/HANDOFF reconciled to declare actuate the intended default (Sonnet worker).
- **Cleanup**: removed dead `LOG_WATCHDOG_SKIPS` (config.local.json); fixed stale
  Claude-module docstrings (`__init__`, `policy`, `pi_bridge`, `transcript_lib`
  thinking-branch); labeled policy `decide()`/`target_tokens`/`PolicyConfig` as
  RESERVED-not-wired (deliberate masterplan scaffolding — NOT deleted). Doc pins →
  0.79.9 (render-channel spec comment + HANDOFF). README tunable table fixed to
  shipped values (was documenting balanced defaults; ships economy 0.50/0.90/…).
- Left as-is (noted, not bugs): `harness="claude"` default params (inert — statedir
  ignores them); `todos_all_done`/`skill_*` TranscriptStats fields unset by the Pi
  parser (one wrapup-signal arm dormant — needs Pi todo parsing + telemetry, not a
  blind edit); `_WIDE` thresholds unreachable at the 200k window (dead until a
  big-window model self-reports).
- Gate green: 100 pytest + 15 node (+2 regressions: native-await/skip-llm,
  selfTriggered-clear-on-throw) + Pi smoke. NOT yet reinstalled to
  `~/.pi/agent/extensions/` (live shim still old build, pin 0.79.8 vs runtime
  0.79.9) — owner-gated out-of-tree deploy: `python3 src/install_pi.py`.
- DEFINITIVE proof still pending (unchanged): owner-driven live Pi compaction; the
  headless harness can't launch the interactive TUI.

## 2026-06-21 — Pi-only pivot (Claude adapter removed)

Flattened the scaffolding and removed the Claude Code adapter. Branch
`docs/spec0-pi-only-pivot` → `main` @ `495088e`; README rewritten under a
Pi-only heading. Pi is now the sole harness; the core stays harness-agnostic
by design. Extracted `llm_digest` to a kept module before any removal; completed
the Pi parser's assistant/user/summary field-completion first. Single-namespace
config; Pi state under `~/.autocompactor/pi/`. Rationale: the Claude adapter's
history was channel-fighting (systemMessage redraw, additionalContext relay,
PreCompact hookSpecificOutput rejection, cooldown starvation) and Claude only
ever advised — it cannot invoke `/compact`. Pi actuates via `ctx.compact()`.
Post-pivot follow-ups tracked in HANDOFF.md.

## 2026-06-20 — FIX (Pi): compaction/precompaction notices invisible

Report: "autocompactor displays nothing from Pi" → narrowed by owner to "I see it
loaded, but not compaction or precompaction output." Root cause (code-trace +
session-data, not guessed): all user notices go through `announce()` →
`persistVisible()` which hardcoded `pi.sendMessage(..., {deliverAs:"followUp"})`. In
Pi 0.79.8 `AgentSession.sendCustomMessage` (agent-session.js:983-1012), a `followUp`
message only renders/persists when NOT streaming (falls to the final else). The
compaction events (`session_before_compact`/`session_compact`) and `agent_end` fire
while the agent IS streaming (compaction is a summarization turn), so followUp →
`agent.followUp()` (agent input queue) → swallowed: never rendered, never persisted.
`session_start` runs not-streaming, so its followUp hits the else and renders — which
is why "loaded" showed. Empirical: `autocompactor.status` persists 0× across all
sessions vs digest (nextTurn) 4× and hindsight (no-deliverAs) 27×. notify/setStatus
(hasUI-gated) are swallowed by Pi's compaction redraw, so they don't rescue it.

Fix (`src/pi/autocompactor.ts` only): parameterized `persistVisible`/`announce` with
`deliver: "nextTurn"|"followUp"` (default **nextTurn** — the only channel proven to
survive a compaction and render, via the next-prompt flush at agent-session.js:797;
same mechanism the digest already uses). One-shot notices (load, before_compact,
compact summary, onError, actuate "running compaction") now use nextTurn. The
**recurring** agent_end advisory ("criteria met … advise mode" / reentrancy
"compaction in progress") keeps `followUp` explicitly, to avoid piling up stale dup
advisories at the next prompt (the reason it was on followUp originally). `appendEntry`
was evaluated and rejected — it persists silently (no `_emit`), doesn't render.

Tests: existing extension.test.mjs had codified the bug (asserted followUp +
`assertNoVisibleNextTurn`) — updated to the corrected per-handler channel contract.
New `render-channel.test.mjs` mirrors the SDK delivery+flush branches verbatim
(version-pinned 0.79.8) as an executable root-cause spec. 198 pytest + smoke +
14 node tests + pi smoke green; extension reinstalled (pinned 0.79.8). DEFINITIVE
proof still pending: owner-driven live Pi session driven to a real compaction (the
headless harness can't launch the interactive TUI).

## 2026-06-18 — FIX: "literally nothing" visible output (cooldown starvation)

Root cause of the recurring "autocompactor shows no output across several Claude
invocations" report: the PostToolUse burst path wrote the SHARED
`last_reco_tokens`, which is the UserPromptSubmit token-distance cooldown anchor
(`suppressed = 0 ≤ ctx − last_reco_tokens < COOLDOWN`, 15k). Burst recommends kept
bumping it to ~current ctx, so every prompt-time eval landed inside the 15k window
and was suppressed — measured live: 3/3 prompt evals at 126k/160k/162k all
`suppressed_by_cooldown`. The prompt-time `systemMessage` (the one channel the user
reliably sees) thus never fired; only mid-burst PostToolUse readouts emitted, which
the user isn't watching. A 2.1.181 binary audit (claude-code-guide) confirmed the
`systemMessage` channel renders fine — dedicated `hook_system_message` path for
UserPromptSubmit/PostToolUse, persisted in `hook_success` attachments — so it was
gated away, not broken.

Fix (one line): the burst path no longer writes `last_reco_tokens`; it gates on its
own `last_milestone_tokens`. Burst and prompt cadences are now decoupled, so the
prompt-time readout fires whenever ctx is over the line. Owner confirmed live (saw
`autocompactor: 253k in context · compact advised…` at prompt submit). 196 pytest +
smoke green; regression `test_burst_recommend_does_not_starve_prompt_readout` added.

Rejected mid-session: a statusline-verdict indicator (prototyped, rendered, owner-
approved, then pulled). It edits the separately-versioned `~/.claude/statusline.js`
(a copy of /srv/dev/ras/claude-statusline, already drifted) and leaked a
contradictory `⚡ compact 234k` token into OTHER agents' status lines while their ctx
meter read 0%. Owner: "use something else which doesn't step on statusline."
statusline.js fully reverted to its prior state (no autocompactor traces; the
pre-existing deployed-vs-repo divergence is untouched and owner-managed).

## 2026-06-18 — FIX: burst watchdog silent across the danger band + spike guard

Report: "autocompactor doesn't appear to be running" in a live session in
`/srv/dev/petabit/skynet` (NOT the skynet *hosts* — wasted a detour SSHing to
skynet1/3 before the owner corrected me). It WAS running and logging; it warned at
107k/110k then went silent as ctx climbed 114k→203k.

Root cause (confirmed from events.jsonl, advisor-corrected): the `PostToolUse`
burst ladder used a fixed `+100k` step above the hard line, but `hard≈110k` and
`effective window=200k` make the danger band only 90k wide — narrower than one
step. After the hard-line announce the next milestone (210k) is past the window,
so zero announcements across the whole 110k→200k zone, *by design, on every
burst*. A transient 303k tail-parse spike was a red herring for the silence (it
already silenced at 114k, 8s earlier) but DID poison `peak_ctx`→303331 (inflated
learned window to 512k tier).

Fix (TDD, +5 tests, 194 pass): `policy.burst_milestone` — window-invariant
occupancy rungs (0.70/0.80/0.90/0.97×W, `BURST_MILESTONE_OCCUPANCY` override) + tail
steps past W, replacing the fixed step. `policy.is_ctx_spike` (>1.5× AND >0.5×W vs
prior `last_ctx`) gates both the ladder and the `peak_ctx` write; `last_ctx` is
tracked per eval so a genuine sustained jump corroborates on the next eval. Skip
events carry `spike_suspected`. Doc: CLAUDE.md cadence paragraph rewritten.

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

## 2026-06-17 — Claude display & visibility iterations (consolidated historical)

The Claude adapter is removed (2026-06-21 Pi-only pivot). This block preserves
the durable lessons and the shared-brain displays that survive on Pi. Eleven
same-day display/visibility iterations, owner-driven against recurring "no
useful output / stripped warning / not intelligent / wrong limits" reports.
Per-commit detail: `git log --before=2026-06-18`. Pytest baseline climbed
157→189 across the day; all passes + smoke + `--verify` green at each step.

**Durable display rule (still load-bearing on Pi):** readout → `systemMessage`
(verbatim, the only channel proven to survive a compaction/redraw and render);
`additionalContext` → silent model-awareness only, never the data channel.
Bare occupancy-% is forbidden in any human-facing string — use absolute anchors
(advisory soft–hard band vs the forced native wall, with explicit
*headroom-away*), so "near the soft limit" can never read as "about to be
force-compacted". Final Claude readout format: `"<used> in context · compact
advised ~<soft>–<hard> · forced auto-compact ~<ceiling×pct> (~<headroom> away)
· model window <tier>"`; model-window shown ONLY when an observed peak exceeds
the native ceiling. Pi uses `forced_auto=None` (it actuates at its own hard
line — no separate native wall) + true `model_window`.

**Shared-brain displays added (Claude + Pi, content-free — kept):**
- (a) **COMPOSITION** — `transcript_lib.context_composition()` +
  `policy.composition_line()`: per-category token estimate (chars/4)
  reconciled so parts ALWAYS sum to the true `context_tokens` (residual
  absorbs estimation error; content scaled down on overshoot — never sums past
  the real total). Rides as a `└` second line on the live readout AND the
  compaction summary.
- (b) **PRESERVATION LEDGER** — `artifacts.preservation_ledger()`: names
  preserved-verbatim-to-disk (lossless) vs left-to-summarizer (lossy, w/ token
  estimate) vs dropped-for-budget. Keep/drop comes from
  `artifacts.budget_plan()`, factored out of `build_digest` as the SINGLE
  source so ledger and digest can't disagree about what survived.
- **Loaded-skills surfacing** — `analyze()` scans the FULL transcript for
  isMeta skill injections ("Base directory for this skill:" marker), deduped
  by name at max size (they persist across compaction boundaries) →
  `skill_chars` / `skill_names`; `summary_chars` from compaction-summary.
  `composition_line()` renders `loaded skills (names — reclaimable)` and
  relabels the residual `system+tools`. Skills persist across /compact — the
  reclaim lever is unloading the skill, not compacting.
- **Skill-dominance warning** — `policy.skill_warning(comp,
  threshold=SKILL_DOMINANCE_FRAC=0.40)` returns a one-line warning when loaded
  skills exceed 40% of the window. Wired at all readout sites. Content-free.

**Claude-specific channels (historical, do not re-derive):** PreCompact
`systemMessage` is swallowed by the compaction redraw; PreCompact
`hookSpecificOutput.customInstructions` is a Claude NO-OP (field not in the
hooks docs/changelog — the robust path was always artifact extraction +
re-injection; the customInstructions feature is Pi-only). CC 2.1.x's
`PostCompact` event renders in the fresh post-compaction view; PreCompact was
made SILENT on stdout (stash only) and the single compaction notice moved to
PostCompact + a "first of either, one-shot" pending-notice armed by PreCompact
and rendered by the next UserPromptSubmit/PostToolUse. Burst readout uses
escalating thresholds (first soft cross, first hard cross, each further
+`BURST_MILESTONE_STEP`) — not per tool call.

**Signal recalibration (2026-06-17 corpus re-run, backtest baseline 8%):**
demoted gating→observe `burn_rate` (0.9×) + `subagent_done` (0.8×);
re-promoted observe→gating `idle_gap` (7.5×, n=16) + `tests_pass` (2.7×, n=30)
— these reversed since the smaller 2026-06-10 corpus. `error_resolved` stays
observe. Pi pinned to the prior conservative set (it actuates — design trap
#4). TRADEOFF: demoting burn_rate removes early SOFT nags in pure-burst
sessions; the mid-burst HARD watchdog is unaffected. idle_gap/tests_pass are
thin-sample — re-check before trusting as load-bearing.

**WI-1 "mid-burst lateness" — root cause (diagnostic-only then, fix later):**
`pending_reinject` (set True on every compaction, cleared only inside the
UserPromptSubmit reinject block) mutes the PostToolUse occupancy watchdog for
the whole post-compaction autonomous burst → context rides to the next native
auto unwarned. Candidate fix: `pending_reinject` should gate reinjection only,
not the watchdog (the hard-line gate already prevents post-compaction spam).

**WI-1 metric fix (nightly_eval, shipped):** `auto_warning_coverage` epoch-
filters autos to the current `native_ceiling`, separates cold-start autos
(zero prior monitor_evals = unwarnable, reported as a note not a miss), and
counts session-level warned (any prior recommended eval) replacing the per-
interval window. Alarm now needs ≥4 measurable autos (kills the n=1→100%
false alarm). The nightly "6/10 autos unwarned" complaint was dominated by
measurement defects (no epoch filtering; telemetry asymmetry — PostToolUse
logs only on the recommend branch; per-interval + cold-start window). On the
current config epoch (native_ceiling=300000, 22 autos): 86% warned (19/22).
Data-quality caveat: precompact `context_tokens` is unreliable on cold start /
tail parse — don't trust a single precompact ctx.

**WI-A/B/C (nightly epoch-correctness, shipped):** deviation watch re-sourced
from epoch-stamped `precompact` events (`epoch_auto_trigger`); breaker
re-anchored to each session's OWN max auto-pre (+CEILING_SLACK); realized
reduction reconstructed by joining each precompact to its first later smaller
same-session monitor_eval — the verified "reclaim ~Z" lands in the nightly md
+ history.

**Docs reconciled to reality (shipped):** CLAUDE.md "Tuning + native ceiling"
rewritten (ceiling 300k not 400k; native trigger ~254k median/280k max not
336k; no `AUTOCOMPACTOR_*` env set; claude HARD_PCT 0.55, COOLDOWN 15000;
SOFT_PCT retired). nightly_eval expected_trigger model: retired the
`0.675×min(ceiling,200k)`=135k phantom for `native_auto_estimate(ceiling,
pct_override)` = 270k, matching the measured median.

## 2026-06-16 — window-aware target(W) curve + native_ceiling cap (LIVE) + 3-host rollout

Opus-4.8/xhigh advisor (paseo 040952be) designed the window-aware policy
(`docs/masterplan/simplify-compaction-model/window-aware.md`). Two verified
pivots: (1) the ~69k post-compaction floor is WINDOW-INDEPENDENT (small
windows like 64k are already physics-protected — new value lands at ≥256k);
(2) `stale_output` is a RECENCY proxy not relevance (anti-predictive 0.9×) →
WRONG gate for expansion. Design: `target(W) = F + a[profile]·sqrt(W − F)`
(SOFT line) + `ceiling(W)` (HARD safety line); gate expansion on
`est_reclaim ≥ MS` + predictive signals, NOT `stale_output`. Window=shape,
profile=aggressiveness.

Implemented + LIVE + rolled to epyc2/skynet3 (157 pytest + Claude/Pi smoke
green; commit 089df24; /srv/dev Syncthing-converged code+config, live state
the gap, closed via `python3 src/install.py`):

- **`target(W)` — `policy.py`:** `target_tokens(W, profile, F, MS, hard_pct)`
  sub-linear SOFT curve, clamped to the current HARD line as interim ceiling
  (proper `ceiling(W)` deferred until the native-auto reserve is measured).
  `_A={economy:130, balanced:188, lazy:266}`. `resolve_policy_config` derives
  `soft = target/effective_limit` UNLESS a deprecated `SOFT_PCT` override is
  set (migration safety — tuned installs unchanged; curve governs once
  `SOFT_PCT` retired). ACTIVATED on the Claude main path: `context_monitor._run()`
  reads SOFT from `resolve_policy_config`; `nightly_eval` derives its backtester
  `--soft` from the curve. Top-level `SOFT_PCT` retired from config.json
  (`pi.SOFT_PCT 0.50` stays pinned — Pi is actuate, kept conservative per
  advisor trap #4). LIVE resolved soft (economy): 200k→50%(100k), 512k→31%
  (156k), 1m→20% (195k) — small windows not starved, large windows target low
  occupancy. `pi_bridge` left on pinned flat soft (actuate; full Pi rewire is
  follow-up).
- **`native_ceiling` cap — `window_resolver.py`:** `effective =
  min(resolved, native_ceiling)`, Claude-only. Chose cap over the advisor's
  full-replacement so the deliberate aggressive WINDOW (200k < ceiling 300k)
  is not loosened + the unmeasured-reserve regression avoided. Caps over-
  inference (512k→500k) and small models (128k binds). Live effect for current
  config: DORMANT (200k<300k); activates for big-window models / small
  enforced ceilings.
- **`ceiling(W)`/`native_safe_line` DEFERRED:** needs the reserve re-
  measurement (the W−63k constant is stale on CC 2.1.178; ~153k under 500k,
  unknown under 300k). Until then flat `HARD_PCT` is the interim ceiling.
- **3-host consistency:** all at 089df24, PostToolUse+PreCompact registered,
  STATUS OK. Native ceiling intentionally per-host (NOT normalized): epyc1
  =300000 (CC 2.1.178, reserve ~153k → fire ~147k), epyc2/skynet3=200000 (CC
  2.1.170, reserve ~63k → fire ~137k). Both aggressive + floor-safe for their
  CC versions. epyc1/grojas, epyc2+skynet3/ras.

GPT-5.5/xhigh advisor (paseo 02633d39) pressure-tested the aggressive config.
Key catch: the native-auto reserve model is UNKNOWABLE from corrupted data
(nightly_history mislabels old sessions with the CURRENT ceiling — backtest
reads `native_ceiling` at backtest time, not session time). Only trustworthy
same-day point: 2026-06-16, 500k ceiling → autos at 344–350k → reserve ~153k
(NOT the ~65k the old HANDOFF claimed for 2.1.170). Under an absolute-153k
reserve, a 200k native ceiling would fire at ~47k — BELOW the 69k floor
(catastrophic). So native ceiling 500000→300000 (floor-safe under both reserve
models: 147k absolute / 207k proportional). Aggressive config applied
(config.json): PROFILE economy; top SOFT_PCT 0.5→0.35, COOLDOWN 20000→15000;
claude WINDOW 300000→200000, HARD_PCT 0.62→0.55 (hard nag @110k); POST_FLOOR
70000 + MIN_SAVINGS 30000 kept; Pi section PINNED (pi.SOFT_PCT 0.50,
pi.COOLDOWN 20000, pi.MIN_SAVINGS 30000; Pi HARD_PCT 0.90 — actuate = real
compactions; 0.70 on 160k actuates at 112k, reclaiming only 42k — too close
to floor). Result: Claude recommends @100–110k, native enforces @~145–207k
(was 345k); Pi unchanged.

## 2026-06-16 — simplify-compaction-model: miss attribution + Fix #1 + policy.py

Masterplan bundle at `docs/masterplan/simplify-compaction-model/` (findings,
spec, plan, review, miss-attribution). GPT-5.5/xhigh advisor pass (paseo
a406d072) redirected the work: the real bug is NOT config verbosity — it is
Claude auto-compactions arriving with no advance recommendation.

Miss attribution (confirmed against source): **Structural** — the advisor hook
fires on UserPromptSubmit (human prompts only), not tool-result turns; an
unwarned session grew 37k→343k across 130 tool-result turns but only 7 human
prompts → the hook had almost no chance to recommend before native auto. A
profile/config rename cannot fix this. **Secondary** — evals/day collapsed
142→6 after the 2.1.173 upgrade (2026-06-11); PreCompact hook events dropped
to ~1 vs ~87 transcript compactions same day → broad hook-invocation
regression (recovered on 2.1.178). **Ruled out:** tail-parse-returns-0, broken
session_id propagation, the analyze() "under-count" (correct: compaction
summarizes away pre-boundary usage; `peak_ctx` state-carrying preserves the
true peak).

Fix #1 (the actual cure) — DONE, live, tested:
- `transcript_lib.current_context_tokens()`: cheap reverse-tail read of the
  last assistant usage block (~1ms), so the hook can check occupancy mid-burst
  without a full `analyze()` per tool call.
- `context_monitor._run_posttooluse()`: a PostToolUse watchdog. Cheap path
  gates on `current_context_tokens`; full `analyze` runs only at/above the
  hard line; cooldown debounces; quiet + no telemetry below the line.
- `install.py`: registers PostToolUse → `context_monitor.py` (idempotent).

Workstream 2 — DONE: `nightly_eval` hook-coverage self-check
(precompact_events / transcript compactions; flags <50% with ≥3 compactions)
— robust to total hook death because both counts drop together.

Workstream 3 — foundation landed: `policy.py` (unified decision rule: hard
line, soft+gating, min-savings guard, rising-only cooldown; PROFILE table at
parity with today's defaults; old-key overrides still win). Wired into the
PostToolUse path (gating=False: hard-line-only mid-burst). `tests/test_policy.py`.

## 2026-06-15 — profiling-pass improvements (never-raise, cheapness, de-dup, tests)

115 → 130 pytest; smoke + `install.py --verify` green. No behavior change to
recommendation/compaction logic except where noted.

- **Never-raise (A):** Claude hooks called `sys.exit(main())` with only local
  guards — a non-dict `message.usage` could raise out of `analyze()` and break
  the hook. Added shared `stats.run_hook()` backstop (exit 0 + content-free
  `hook_skip` breadcrumb), wrapped both hook `main()`s, made
  `analyze()`/`load_transcript()` defensive, guarded the two unguarded state
  writes. (`pi_bridge` already had a top-level guard.)
- **Cheapness (C):** artifact merge writes only when content changed (was a
  full read+write every prompt); per-prompt state writes batched to ≤1 via a
  `_save_state()` flush (was up to 4). Restored the immediate `peak_ctx` write
  (C2 batching had deferred it — a mid-prompt exception could lose a peak
  update that tail-only parses depend on); cooldown/staged writes stay batched.
- **De-dup (D):** founding-goal + NOTE restatement → shared
  `transcript_lib.append_artifact_restatement`; `_block_text` unified (Pi
  aliases transcript_lib's, which gained the `thinking` branch — unreachable
  on the Claude signal path). D4 (`window_resolver` Pi `small_session_clamp`
  skipping the tier clamp) is **by design** — Pi uses exact
  `contextWindow − reserve`.
- **Cross-vendor review (Codex/GPT-5):** no blocking issues; confirmed the
  `_block_text` thinking branch unreachable on the Claude path and the
  `aggregate_events` rewrite equivalent.

## 2026-06-14 — observe-first auto-window learning + adversarial-review fixes

- **auto-window learning:** added `window_resolver` support for observe-only
  learned context tiers (200k/300k/512k/1m) across Claude, Pi, backtest,
  nightly, and status reporting. Live recommendation windows stay on the
  current configured/runtime path. Telemetry/backtest/nightly now record
  effective/configured/learned windows, learned tier/source, Pi runtime
  window/reserve, and native-cap bottlenecks.
- **adversarial-review fixes (H1–H3, M1–M2, C1–C4) + Pi double-prepare:**
  backtester now replays with live config window + STALE_FRAC via the shared
  `window_resolver`, uses inclusive prefixes (`entries[:upto+1]`), measures
  lateness per compaction cycle; nightly reads thresholds from `config_lib`.
  `analyze()` truncates to the post-boundary segment (founding prompts still
  captured from the full path); `build_digest` no longer returns header-only;
  `TEST_PASS_RE` rejects TAP `not ok`; `build_context_state` threads
  window/harness/stale; `compaction_count` populated (was getattr-0). Pi
  `session_before_compact` returns early when `selfTriggered` → actuate-mode
  compactions no longer fire a redundant native prepare. 113 pytest (was 96).

## 2026-06-10 — pi-harness masterplan + incident + verbatim-prompts directive

Masterplan `pi-harness` (Workstream C: Pi coding-agent support, Claude Code
100% compat) executed and merged (e3301c4) — waves 0–2, bundle archived,
worktree removed. 81 pytest on merged main. Live shim re-installed from MAIN;
`install_pi.py --status` OK, pin 0.79.1.

- **INCIDENT:** an unidentified agent under the owner's git identity ran a
  "merge sweep" at 01:18 and merged+deleted branch `masterplan/pi-harness` at
  03:10 (f81a91e), deleting `.worktrees/` under a live codex worker and
  bypassing the branch_finish gate. Merged content was verified-green wave-0/1
  work; merge accepted as fait accompli. Lost wave-2 test files recovered
  byte-exact from codex rollout apply_patch payloads; in-progress
  `pi_bridge.py` redone from scratch. **Do NOT sweep `.worktrees/` or merge
  `masterplan/*` branches — they are state-machine managed.**
- **Owner directive (commits 0fc80d3 + 94ee3a8):** compaction instructions
  preserve user input prompts VERBATIM (esp. initial ones) —
  `transcript_lib.py` BASE_SCHEMA + anchor truncation 300→1500. Part 2:
  founding goal RESTATED after every compaction pass —
  `initial_user_prompts` captured verbatim (skip isCompactSummary/isMeta),
  FOUNDING GOAL leads every artifact digest (top of PRIORITY, old-wins merge
  so it survives unlimited passes), BASE_SCHEMA carries prior summaries'
  GOAL/CONSTRAINTS forward unchanged; Pi capture walks the full leaf path.
- **Pi tuned like Claude:** `AUTOCOMPACTOR_PI_*` exports in `~/.bashrc` managed
  block (Pi has no settings env; shell inheritance is the only delivery).
  WINDOW intentionally omitted (Pi window is exact from ctx). bashrc not
  settings because `settings.json` has no env map (checked `docs/settings.md`);
  the `PI_`-prefixed names keep Claude sessions untouched.
- **advise-only regression fix (both harnesses):** (Pi) the actuation gate was
  env-only (`AUTOCOMPACTOR_PI_MODE` read by the TS shim) while a73b3a5 moved
  all thresholds to config.json; bashrc exports sit behind the interactive-
  shell guard, so non-interactive Pi launches ran advise mode forever. Fix:
  MODE lives in config.json (`pi.MODE=actuate`), bridge returns it in the
  evaluate verdict, shim uses verdict.mode with env override-only. (Claude)
  `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`/`_AUTO_COMPACT_WINDOW` were XXX-disabled in
  settings.json — re-enabled at 90/500000. a73b3a5 cleanup: context_monitor cfg
  keys were double-prefixed (`AUTOCOMPACTOR_AUTOCOMPACTOR_*`) — fixed;
  `Config.str` now env-first; harness section overrides top-level; `_WIDE` keys
  honored; prepare calls get 60s (LLM digest budget 45s; 5s killed it).
- **single source of truth:** all tuning ported into config.json (claude:
  WINDOW 300k/HARD 0.62; pi: MODE actuate/HARD 0.90/WIDE 0.60/RESERVE 40000;
  top: SOFT 0.5, STALE 0.90, COOLDOWN 20000). Site-local LLM digest settings
  moved to `config.local.json` (gitignored, merged over config.json) —
  preserves the no-endpoints-in-public-config rule while reaching env-less Pi
  processes. `AUTOCOMPACTOR_CONFIG` env: alternate config path, or empty for
  none (tests/smokes use it for hermeticity). Harness sections wholly outrank
  top-level keys — a harness needing a `_WIDE` value must carry it in its own
  section (`pi.HARD_PCT_WIDE`).
- Codex note: `pi_session_lib` leaf→root walk picks the last file-order leaf;
  active segment honors `firstKeptEntryId`.
## 2026-07-16 — Pi intercept becomes config-backed (one compaction owner)

- **PI_INTERCEPT in config.json** (`"PI_INTERCEPT": true`): the shim's
  `interceptEnabled()` now reads config with env override in BOTH directions —
  `AUTOCOMPACTOR_PI_INTERCEPT` set non-empty wins ("1" on, anything else off);
  unset → config value. Fixes the one-compaction-owner gap where native Pi
  compaction raced the autocompactor because intercept was env-only and
  non-interactive launches never saw the bashrc export.
- Fail-open verified: bridge unreachable → native compaction proceeds with a
  surfaced warning; no cancel without a bridge verdict.
- Tests: 4 new cases in `src/pi/test/extension.test.mjs` (config-on cancels
  native w/ customInstructions; env=0 beats config=true; env=1 no-config;
  bridge-down fail-open). 16/17 pass — the 1 failure (`session_start`) is
  pre-existing at HEAD (shim only registers agent_end/session_before_compact/
  session_compact).
- Deployed via `python3 src/install_pi.py`; deployed copy == repo modulo the
  baked bridge path.

## 2026-07-17 — waiting-state resume after compaction

- **Incident:** session `019f7130…` (yanos wave-4). Actuate compacted at ~371k;
  autonomous next-step recovered the *stale* Grok user ask (base64-polluted
  `last_user_task`) instead of the assistant-declared wait for
  `Y260717-114448`. Session went idle; compact status only appeared on the
  next user prompt (`status?`).
- **Fix:** mechanical `open_work` extraction (waiting monitors + on-success);
  `resolve_next_step` prefers wait open_work over last_user_task; structured
  WAIT brief; `last_user_task` strips base64 / ignores trivial pings.
- **Shim:** wait-shaped autonomous → `autocompactor.nextstep.wait` + scheduled
  poll (`NEXTSTEP_WAIT=poll`, `WAIT_POLL_S=60`, `WAIT_POLL_MAX=20`); idle
  actuate status delivers immediately (no `deliverAs: nextTurn` deferral).
- **Tests:** `tests/test_open_work.py` (8), `tests/shim_wait_resume.test.ts` (3),
  full pytest green except pre-existing chonkie skip failures.
- Design/plan: `docs/superpowers/specs/2026-07-17-waiting-state-resume-design.md`,
  `docs/superpowers/plans/2026-07-17-waiting-state-resume.md`.

## 2026-07-17 — compact connection-error retry

- Actuate path hit `Summarization failed: Connection error` with no retry;
  cooldown stayed set so the next agent_end stayed quiet until a human poke.
- `safeCompact` now retries transient summarizer failures (connection/network/
  5xx/rate-limit/overloaded) up to `COMPACT_RETRIES` (default 2) with
  exponential backoff from `COMPACT_RETRY_MS` (default 2000ms).
- Final failure clears `lastRecTokens` so cooldown does not block a later
  attempt. Env: `AUTOCOMPACTOR_COMPACT_RETRIES`, `AUTOCOMPACTOR_COMPACT_RETRY_MS`.

## 2026-07-17 — fix "Compaction cancelled" self-cancel under PI_INTERCEPT

- Symptom: actuate announced criteria met then immediately
  `autocompactor: compaction failed — Compaction cancelled.`
- Root cause: with `PI_INTERCEPT=true` (config.local), `session_before_compact`
  returned `{cancel:true}` for *our own* actuate `ctx.compact()` when
  `selfTriggered` was not yet visible (prepare race) or concurrent native
  compact fired. Pi throws `Compaction cancelled` on any cancel return.
- Fix: track `enrichedCompactsInFlight` + `ownsCompaction(event)` (also
  treats non-empty `event.customInstructions` as already-enriched). Never
  cancel an owned/enriched compact. Re-check ownership after prepare.
  Do not retry cancel as transient.

## 2026-07-17 — deploy race-fix + CacheLane threshold retune

- **Deploy:** fixed shim (`enrichedCompactsInFlight` / `ownsCompaction` +
  `COMPACT_RETRIES`) installed on epyc1, epyc2, skynet3 via
  `python3 src/install_pi.py` (md5 `415e8e58…`). Bridge path baked to
  `/srv/dev/ras/autocompactor/src/pi_bridge.py` on all three; pin versions
  remain host-local (0.78.0 / 0.80.9 / 0.78.1).
- **Threshold reevaluation (Pi CacheLane :7332):** fleet hit ~42%, savings
  ~63%, ~6.4M tokens K-pruned. `HARD_PCT_WIDE=0.40` hard-fired at ~184k on
  460k windows — near post-compact residual (~120k med) → thrash. Raised:
  `SOFT_PCT_WIDE` 0.25→0.40, `HARD_PCT_WIDE` 0.40→0.58, `COOLDOWN` 20k→30k
  (~184k / ~267k soft/hard @460k effective).
- **CacheLane → evaluate:** new `cachelane_stats.read_rollup` (sqlite ro on
  `~/.cachelane-litellm`) attaches fleet hit/prune fields to `monitor_eval`
  and the reason string when `CACHELANE_STATS=true` (default). Optional
  `CACHELANE_SOFT_BIAS` (default false) suppresses SOFT-band only when hit
  ≥ `CACHELANE_MIN_SAVINGS_RATIO` (0.40); hard line never suppressed.
  Session IDs still don't join (proxy UUID vs Pi id) — fleet rollup only.
