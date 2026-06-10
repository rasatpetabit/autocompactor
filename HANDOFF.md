# autocompactor — handoff notes

Project: smarter, earlier, instruction-tailored compaction for Claude Code.
Status: built and smoke-tested in a sandbox against synthetic transcripts.
**Not yet validated against real data** — the epyc1 transcript tarball never
arrived, so the tuning pass is still open. This document is the handoff
into a Claude Code session running directly on the server.

## Components (all in this directory)

| File                   | Role |
|------------------------|------|
| `transcript_lib.py`    | Shared JSONL parsing; phase detection; instruction builder (structured-handoff schema + phase addenda + session anchors) |
| `context_monitor.py`   | `UserPromptSubmit` hook: occupancy + boundary-signal scoring, recommends `/compact`, stages tailored instructions, logs telemetry |
| `precompact_analyzer.py` | `PreCompact` hook (manual+auto): transcript backup, injects `customInstructions`, logs telemetry |
| `artifacts.py`         | Mechanical extraction -> durable disk artifacts -> budgeted one-shot re-injection digest |
| `stats.py`             | Local telemetry appender (`~/.claude/autocompactor/stats/events.jsonl`) |
| `analyze_corpus.py`    | Offline backtester for real transcripts + `--events` aggregator for live telemetry |
| `install.py`           | Idempotent hook registration in ~/.claude/settings.json |
| `tests/`               | Fixtures + smoke_test.sh (isolated-HOME, end-to-end) |
| `CLAUDE.md`            | Resume context for the on-server Claude Code session |
| `README.md`            | Install, settings.json registration, tunables |

## Design decisions (and why)

1. **Advisor/enricher split.** Hooks cannot invoke `/compact`; PreCompact
   only fires once compaction is underway. So the monitor advises at cheap
   boundaries and the analyzer enriches whatever compaction happens.
2. **Occupancy from usage blocks.** Last assistant message's
   `input + cache_read + cache_creation + output` ≈ live context. Free to
   compute, no model calls.
3. **Boundary signals**: recent `git commit`, test-pass markers in tool
   output, all-TodoWrite-completed, stale tool-output fraction ≥ 50%.
4. **Instructions are three-layered**: base structured-handoff schema
   (verbatim-identifier rule + recoverability principle: keep what cannot
   be re-derived from disk, drop what can, pointer when unsure) + phase
   addendum (debugging / implementation / exploration / wrapup) +
   session-specific anchors extracted from the transcript.
5. **Telemetry is local-only** and content-free (counts, ratios, phases —
   no transcript text).


## Decision record: pi-custom-compactor evaluation (2026-06-09)

Evaluated `@davidorex/pi-custom-compactor` (npm) against autocompactor.
Conclusion: **complementary, not duplicative** — they own summary
durability, we own timing and evaluation. Decisions taken:

1. **Adopted (adapted): mechanical extraction -> disk artifacts.** Their
   core insight — facts a regex can extract should never depend on an LLM
   summarizer's goodwill — is now `artifacts.py`. At compaction we extract
   corrections / error ledger / working commands / hex constants / file
   lists mechanically (zero LLM cost), persist to
   `~/.claude/autocompactor/artifacts/<session>.json`, tell the summarizer
   NOT to duplicate them, and re-inject a priority-trimmed, budgeted
   digest (`AUTOCOMPACTOR_ARTIFACT_BUDGET`, default ~1500 tok) on the
   FIRST prompt after compaction. Deliberate difference from their design:
   they re-inject per LLM call (possible in Pi's in-process extensions);
   Claude Code hooks can't intercept every call, so we inject once into
   the fresh post-compaction context — same durability, no per-call tax.
2. **Adopted: per-artifact cost accounting + stats visibility.** The
   precompact telemetry event now records per-artifact byte sizes; the
   re-injection digest carries a one-line stats header (their
   `<compaction-stats>` trick) so the model knows a compaction happened
   and what it cost.
3. **Not adopted: YAML spec system.** Their specs are explicit,
   selected by workflow state; our phases are inferred from transcript
   behavior. For the Pi port these compose (our detector can write their
   `compactionSpec`); on Claude Code, inference stands alone.
4. **Kept ours: boundary-timing engine and offline backtester.** No
   counterpart exists in their package (or, as far as found, anywhere in
   this niche). This is autocompactor's differentiation; expanded this
   session — see below.
5. **Engineering-maturity gap acknowledged**: they ship 189 tests and
   error fallbacks; we have smoke tests. Porting to real pytest cases is
   an open item for the on-server session.

## Timing engine v2 (same session)

`active_signals()` in `transcript_lib.py` is now the single registry
shared by monitor and backtester. New signals beyond the original four
(commit / tests_pass / todos_done / stale_output):

| Signal | Fires when |
|---|---|
| `todo_step` | latest TodoWrite has >=1 completed AND >=1 pending — a plan step just closed |
| `error_resolved` | errors occurred in the recent window but the trailing 3 results are clean — debug loop concluded |
| `idle_gap` | >=30 min gap between recent entry timestamps — new sitting |
| `subagent_done` | a Task (subagent) call returned recently — burst finished |
| `burn_rate` | projected to hit autocompact within ~8 turns at the median per-turn context growth — predictive, fires even with no boundary |
| `topic_shift` | the incoming prompt shares <20% content-word vocabulary with the recent window |

`detect_phase` also updated: a concluded debug loop no longer classifies
as `debugging`.

Validation status: all of the above verified on synthetic transcripts
only. The real-data backtest (open item #1) should now additionally
report per-signal precision — which signals precede compactions that the
user actually wanted vs. which nag.

## Open items for the on-server session

1. **Run the backtest on real data** (the step that was blocked):
   `python3 analyze_corpus.py --root ~/.claude/projects --days 4 --json report.json`
   Then apply its suggested `AUTOCOMPACTOR_SOFT_PCT` / `HARD_PCT`.
2. **Verify schema assumptions against that machine's Claude Code
   version**: usage-block field names, TodoWrite input shape, whether
   compactions leave an explicit marker (the analyzer currently infers
   them from >30% context drops; an explicit marker would be strictly
   better). Also confirm `transcript_path` arrives non-empty in PreCompact
   input — there are reported version-specific bugs.
3. **Check signal hit rates**: the synthetic backtest flagged that
   `todos_done` never fired — confirm whether that workflow actually uses
   TodoWrite, and check the test-pass regex against the real test
   runner's output format.
4. **Settings registration on the target user account** — merge the
   hooks stanza from README.md into `~/.claude/settings.json`. Do this
   from the interactive session so the account owner sees and approves
   what's being installed.
5. **After a few days of live telemetry**: `analyze_corpus.py --events`
   → look at `compaction_reduction_ratio` by phase to see which phase
   addenda produce weak summaries, and tune.
6. ~~Stretch: state-externalization~~ — superseded: built as the
   artifact layer (`artifacts.py`), see decision record above. Remaining
   refinement: continuous extraction via PostToolUse rather than
   compaction-time-only, and project-local artifact storage option.
7. Add per-signal precision to the backtest report (which new signals
   are predictive vs. noisy on real sessions).
8. Port smoke tests to pytest; add error-path tests (maturity gap vs.
   pi-custom-compactor).

## On-server session 2026-06-10 — status

Open items 1–4 closed: real-data backtest ran (567 sessions, 633
compactions, ~21M tokens late-compaction waste measured); schema verified
(usage fields OK; compactions DO leave explicit `system/compact_boundary`
+ `isCompactSummary` markers — backtester now uses them with drop-heuristic
fallback); TodoWrite→TaskCreate/TaskUpdate and Task→Agent renames fixed in
transcript_lib (todo_step/todos_done/subagent_done now fire on real data);
hooks installed user-wide via install.py, legacy precompact-instructions.sh
stanza removed (superseded). Thresholds applied in settings.json env per owner's spec (1M models,
work at ~200k, ENFORCED 400k max): CLAUDE_CODE_AUTO_COMPACT_WINDOW
lowered 650000→400000 so native autocompact enforces the ceiling;
AUTOCOMPACTOR_WINDOW=400000, SOFT_PCT=0.5 (200k), HARD_PCT=0.75 (300k),
STALE_FRAC=0.90.
Trigger semantics confirmed from the CLI binary (2.1.170): the effective
autocompact window is min(CLAUDE_CODE_AUTO_COMPACT_WINDOW, model max
window), and it fires "approaching" that limit — observed floor on 200k
models is ~135k (≈65k reserve), so a 400k ceiling should trigger
~320-335k; HARD_PCT=0.75 (300k) keeps the hard nag ahead of it. Live
precompact telemetry records context_tokens at each auto trigger —
confirm with `analyze_corpus.py --events` after a few days.

Item #7 (per-signal precision) DONE — backtester reports precision
(compaction within 50k-token lead after firing) with a signal-agnostic
baseline for lift, split by trigger via compactMetadata (which also
provides authoritative preTokens/postTokens, now used). 14-day results
(1,684 sessions, 1,568 compactions): todo_step 1.7x lift (best); commit
1.3x; idle_gap 1.3x; subagent_done 1.2x; todos_done 1.2x; burn_rate 1.1x;
stale_output 1.0x (NO lift — fires at 85% of evaluated points; hence
STALE_FRAC 0.50→0.90); tests_pass 0.9x and error_resolved 0.6x
(anti-predictive — debug conclusions precede continued work, not
compaction; left in registry, candidates for demotion). True median
reduction per compaction is 88% (from postTokens). Measured
late-compaction waste: ~80.7M tokens / 14 days. Caveat: precision vs
mostly-AUTO compactions measures context momentum, not boundary quality;
the manual-compact column (n=81) is the truer wantedness label but thin.
Smoke tests now scrub inherited AUTOCOMPACTOR_* env (live settings.json
tuning leaks into child processes and broke fixture expectations).

Item #8 (pytest port) DONE — tests/test_autocompactor.py, 27 cases:
unit coverage for parsing/signals/phases/task-tool synthesis/artifacts/
compaction detection plus the hook contract (hooks exit 0 on empty,
malformed, and missing-transcript input — never raise into the hook
path). smoke_test.sh kept as the zero-deps runner.

Item #6 refinement (continuous artifact extraction) DONE — the monitor
merge-persists artifacts on every prompt (artifacts.merge(): union with
new-supersedes-old, max() on error counts), so mechanically extracted
facts survive autocompacts that arrive with no warning. PreCompact also
merges instead of overwriting.

Per-session effective-window clamp added to the monitor: sessions whose
peak observed context is <190k are evaluated against a 200k window even
when AUTOCOMPACTOR_WINDOW is tuned to 400k for 1M models — first nightly
eval caught that 53/53 of a day's auto-compactions fired on 200k-window
sessions the 400k thresholds never engaged with.

Nightly self-evaluation (nightly_eval.py) registered in crontab
(03:30, marker `# autocompactor-nightly`, logs to
~/.claude/autocompactor/nightly.log): runs pytest+smoke as a
schema-drift canary, detects CLI version changes, backtests the last
day, aggregates hook telemetry, checks the purpose metric (fraction of
auto-compactions with no advance recommendation — flags >50%), checks
ceiling enforcement, writes reports/nightly-YYYY-MM-DD.md +
nightly_history.jsonl, prunes artifacts/backups/reports older than 30
days. Verified under a cron-equivalent minimal environment.

Remaining: topic_shift precision (needs prompt replay at sample
points), possible demotion of error_resolved/tests_pass from the gating
set (anti-predictive, but cheap at high occupancy). Review
reports/nightly_history.jsonl trends after a week.

## Session 2026-06-10 (later) — 200k ceiling, every-turn cheapness, floor audit

Owner's directive: >80% of dollar spend is cached reads; compact far
more often, keep context as low as possible, and make sure a hook
evaluated every turn stays cheap when there's nothing to do.

Ceiling lowered 400k→200k. CLAUDE_CODE_AUTO_COMPACT_WINDOW=200000;
monitor retuned to AUTOCOMPACTOR_WINDOW=200000, SOFT_PCT=0.5 (100k),
HARD_PCT=0.62 (124k), COOLDOWN=20000. Trigger-model evidence from
day-one nightly: yesterday's max auto preTokens was 336,512 ≈ 400,000 −
63,488, confirming the absolute-reserve model (window − ~65k) at the
one point where it diverges from the proportional model (0.675×window).
Both models predict ~135k at a 200k ceiling, so HARD 124k stays ahead
of native autocompact either way. Verify against tomorrow's nightly —
the first clean read under the new ceiling.

Every-turn cheapness, two mechanisms (both in context_monitor.py,
tested):
* Min-savings guard — est_reclaim = context − AUTOCOMPACTOR_POST_FLOOR
  (70k default; measured post-compaction median here is 69,043, n=494).
  Below AUTOCOMPACTOR_MIN_SAVINGS (30k) no recommendation fires: a
  compaction below ~100k context stalls 30–60s to reclaim almost
  nothing. This is why "compress as low as 100k" is the practical
  floor — the post-compaction footprint plus minimum worthwhile
  savings, not a tuning preference.
* Bounded tail parsing — transcripts over AUTOCOMPACTOR_MAX_FULL_PARSE_MB
  (8) parse only from the last compact_boundary
  (find_last_boundary_offset: chunked reverse scan, JSON-verified so
  transcripts that merely DISCUSS the marker don't match, 4096-byte
  chunk overlap). peak_ctx is carried in the per-session state file so
  the <190k → 200k window clamp survives tail-only views. Fixture
  timing: rich fixture parse ~3ms; an 8MB full parse ~300ms; tail
  parse returns that to single-digit ms.

Nightly gained three watches (nightly_eval.py): (a) expected-trigger
drift — auto preTokens median vs 0.675×min(ceiling,200k), note at
>25k deviation; (b) rapid-refill-breaker suspects — sessions with ≥2
autos whose post-last-compaction peak exceeds trigger+40k with no
further compaction (the tengu_auto_compact_rapid_refill_breaker
symptom: autocompact silently disabled); (c) native microcompaction
marker "[Old tool result content cleared]" scanned in <26h transcripts
(statsig-gated off for this account today; the watch catches it
turning on) — the autocompactor dev project dir is excluded because
its sessions discuss the literal string. analyze_corpus.py records
post_last_compaction_peak per session to feed (b). Day-one run: 131
sessions, 83 compactions, breaker suspects 0, micro markers 0;
trigger-drift and ceiling notes fired correctly-but-transitionally
(yesterday's data ran under the 400k ceiling: median auto pre 171,831,
max 336,512).

Day-one signal precision (nightly backtest, n=83): subagent_done
89%/3.8x lift, burn_rate 54%/2.3x, commit 42%/1.8x, todo_step
38%/1.6x, todos_done 26%/1.1x, baseline 24%, stale_output 22%/0.9x
(below baseline even at STALE_FRAC 0.90), tests_pass 9%/0.4x,
error_resolved 3%/0.1x, idle_gap 0%. Demotion candidates firming up:
error_resolved, tests_pass, idle_gap; watch stale_output.

Context-floor audit (the biggest lever: interactive sessions START at
~53k median, >half of every cached read at ~100k occupancy). Probes
with `claude -p` in scratch dirs: A default = 35,184 first-call
tokens; B empty --mcp-config = 41,162; D empty MCP + disableAllHooks =
38,203. NON-monotonic (B>A) — theory: tool-schema deferral only
engages above a tool-count threshold, so removing MCP servers inlines
the remaining schemas; component-toggle probing is unreliable,
direct file sizes are the audit basis. Clean deltas: hooks ≈ 2,959
tokens (B→D); interactive-only surface (claude.ai remote MCPs etc.,
absent headless) ≈ 12–18k. Measured files: ~/.claude/CLAUDE.md
24,464B (~6.1k tok); subagent-models.md SessionStart injection
12,944B (~3.2k tok); project CLAUDE.md 3,269B; RTK.md 964B. Cut list
proposed to owner (global CLAUDE.md diet, injection→digest, serena
disable-until-configured, plugin pruning); cuts are owner-approval
gated. Probe leftovers to clean: /home/grojas/floor-probe-{b,c},
~/.claude/projects/-home-grojas-floor-probe-*. Note --bare is
unusable on this box (requires ANTHROPIC_API_KEY; OAuth-only here),
and --mcp-config is variadic — pass a file path, never inline JSON
followed by the prompt.

Tests: 37 pytest cases + smoke green. New coverage: boundary-offset
real-vs-mention/none/across-chunks, min-savings suppression, tail
parse after boundary, carried-peak clamp. Fixture note: the rich
fixture sits at ~84k context, below the default guard threshold —
tests and smoke pin POST_FLOOR=50000/MIN_SAVINGS=20000; enlarge
fixtures instead if that ever chafes.

Signal demotion executed (owner-approved same day): error_resolved,
tests_pass, idle_gap are observe-only — active_signals() still reports
them (telemetry + backtester precision unchanged), but the monitor's
recommendation gate and the backtester's recommendation replay filter
them via transcript_lib.observe_only() (AUTOCOMPACTOR_OBSERVE_ONLY,
default "error_resolved,tests_pass,idle_gap", empty = full gating).
39 pytest cases + smoke green.

Project moved 2026-06-10 to /srv/dev/ras/autocompactor (public repo
github.com/rasatpetabit/autocompactor); settings.json hook paths and
the nightly crontab entry updated to the new location. Local-only
artifacts (report*.json, backtest logs, the handoff tgz, .serena/) are
gitignored — backtest reports reference real session paths and must
never be pushed.

Floor cuts executed 2026-06-10 (all four owner-approved):

* Serena disabled until configured: four serena-hooks groups removed
  from ~/.claude/settings.json (backup
  settings.json.bak-pre-serena-disable-20260610) and the MCP server
  entry removed from ~/.claude.json (backup
  .claude.json.bak-pre-serena-disable-20260610). Restore:
  `claude mcp add serena -s user -- serena start-mcp-server
  --context=claude-code --project-from-cwd` + restore the hooks from
  the settings backup.
* Global config SPLIT per owner directive: multi-agent standardizable
  policy moved to ~/AGENTS.md (file-convention rule called out there
  and in CLAUDE.md; hindsight block untouched); ~/.claude/CLAUDE.md is
  now Claude-specific only (AUQ, masterplan contracts, fluffmods
  block verbatim, Claude Code tooling) + @~/AGENTS.md import.
  25,153 -> 12,401 B CLAUDE.md + 6,313 B AGENTS.md. Backups:
  CLAUDE.md.bak-pre-diet-20260610, AGENTS.md.bak-pre-split-20260610.
* Dispatch policy UPGRADED (owner correction 2026-06-10): sonnet is no
  longer the fallback — Claude is reserved for Opus/Fable-tier work.
  Sub-frontier routes to skynet-qwen (any tier) or codex-5.5 (medium
  mechanical, high/xhigh demanding); if neither fits, that is an ERROR
  CONDITION — halt and AskUserQuestion; sonnet only on explicit user
  override. Applied to both refs/subagent-models.md and the digest.
* Global CLAUDE.md diet: AUQ
  enforcement sections kept at full strength (owner caveat: past diets
  regressed AUQ) and EXTENDED with a "rejected AUQ = DISCUSS signal"
  section — a declined/Esc'd AskUserQuestion ("The user doesn't want
  to proceed with this tool use", 356 occurrences in transcripts) is a
  talk request, not consent; batched answers alongside a rejection are
  suspect (UI auto-advances); batch only independent questions. The
  Stop hook (auq-guard.sh) already counts a rejected AUQ as an AUQ, so
  no hook change was needed. fluffmods-managed block kept verbatim.
* subagent-models SessionStart injection -> digest: full ref stays at
  ~/.claude/refs/subagent-models.md; injection swapped to
  refs/subagent-models-digest.md (12,944 -> 2,300 B, ~-2.7k tok),
  rewritten as a mandatory numbered pre-dispatch checklist encoding
  the upgraded policy (HAIKU FORBIDDEN; qwen/codex-5.5 for all
  sub-frontier work; sonnet = ERROR CONDITION until user override;
  enumerate-before-asserting-absence) per owner caveat "strengthen
  without making them unnecessarily long".
* Plugin prune: of 19 enabledPlugins, transcript-wide invocation
  counts showed 6 in active use (superpowers, masterplan, codex,
  feature-dev, context7, cloudflare) — kept; 12 with zero invocations
  disabled (claude-code-setup, claude-md-management, code-review,
  code-simplifier, commit-commands, frontend-design, github, gemini,
  pragma, rust-analyzer-lsp, security-guidance, skill-creator);
  playwright was already off. Re-enable = one settings.json flip.

Combined CLAUDE.md+digest cut ~5.2k tok off the ~53k interactive
floor (~10% of all cache-read volume) before plugin savings.

Open: confirm ~135k auto trigger under the 200k ceiling (tomorrow's
nightly); topic_shift precision via prompt replay; --events
reduction-by-phase after a few live days.

## Pi harness (2026-06-10) — architecture, actuate memo, deferrals

### Architecture summary

Additive adapters, zero moves of existing files — the Claude install base
(settings.json hook entry points) stays byte-stable. `TranscriptStats` is
the normalized model; everything downstream of it (`active_signals`,
`detect_phase`, `build_preservation_instructions`, `artifacts.extract`)
was already harness-agnostic, so Pi support is a new producer plus
plumbing:

* `statedir.py` — harness-namespaced state roots: `claude` →
  `~/.claude/autocompactor` (unchanged), `pi` → `~/.autocompactor/pi`;
  `AUTOCOMPACTOR_STATE_DIR` overrides all (tests pin it).
* `pi_session_lib.py` — Pi v3 tree-format JSONL → `TranscriptStats`
  (leaf-path walk, active segment = entries after the last
  `type:"compaction"` on the path).
* `pi_bridge.py` — never-raise JSON CLI (`evaluate`/`prepare`/`reinject`),
  the ONE brain shared with the Claude hooks: same signal registry, same
  decision model, judged against the Pi effective window
  (`contextWindow − reserveTokens`). No stdin channel exists in
  `pi.exec`, so all inputs are CLI flags.
* `pi/autocompactor.ts` — logic-minimal shim: `agent_end` zero-spawn
  pre-gate → bridge `evaluate` → advise or actuate;
  `session_before_compact` → `prepare` fire-and-forget (backup +
  artifacts + founding-goal restatement); `session_compact` → `reinject`
  digest via `pi.sendMessage(..., {deliverAs:"nextTurn"})`. Every handler
  try/caught — a broken bridge can never break a Pi compaction.
* `install_pi.py` — copy-with-rewrite (NOT symlink: the
  `__AUTOCOMPACTOR_BRIDGE_PATH__` placeholder is baked to this checkout's
  `pi_bridge.py`) into `~/.pi/agent/extensions/`, plus a version pin and
  a `--status` doctor.

Telemetry: `stats.log_event(..., harness="pi")` routes to the pi state
dir; rows stay content-free. Pi thresholds read `AUTOCOMPACTOR_PI_<NAME>`
first, then `AUTOCOMPACTOR_<NAME>`, then the Claude defaults — do NOT
tune Pi-specific values until live Pi telemetry exists.

### Verified ground-truth pins (do not re-derive)

* Validated against `@earendil-works/pi-coding-agent` **0.79.1**
  (installed at `~/.npm-global/lib/node_modules/`); every API name in the
  shim was checked against its `dist/core/extensions/types.d.ts` before
  writing. `install_pi.py` re-pins the version observed at install time.
* This host pins Pi reserveTokens to **40,000** in `~/.pi/agent/settings.json`
  (Pi default is 16,384). The bridge's `RESERVE_FALLBACK = 40_000`
  mirrors the host pin; the shim passes the live `contextWindow` through
  and the effective window is `contextWindow − reserve`.
* `pi.exec` has NO stdin channel — bridge inputs are flags only.

### Flip-to-actuate decision memo

**Advise ships now; the flip-to-actuate is a later deploy decision gated
on Pi telemetry.** `AUTOCOMPACTOR_PI_MODE=advise` (default) only posts an
`autocompactor.advice` message; `actuate` lets the shim call
`ctx.compact({customInstructions})` itself — Pi is the first harness
where we hold an actuator, so it earns a burn-in: flip only after ≥1 day
of `monitor_eval` rows (harness `"pi"`) shows sane occupancy/recommend
behavior at the 40k reserve. Reentrancy: a `selfTriggered` flag blocks a
concurrent second compact while one is in flight (verified by
`pi/test/extension.test.mjs` — the second boundary degrades to advice;
`onComplete`/`onError` reset the flag). Native-auto interception
(cancel-and-retrigger in `session_before_compact`) is separately gated
`AUTOCOMPACTOR_PI_INTERCEPT=1`, default OFF, and auto-disables when
`@davidorex/pi-custom-compactor` appears in Pi settings `packages[]`
(coexist passively). Even with both gates off, `prepare` still runs
fire-and-forget on every native compaction, so backups + artifacts +
founding-goal restatement are never lost.

### Deferred / out of scope (recorded, not scheduled)

* Claude Code plugin packaging (Workstream B stage 2) — revisit now that
  the Pi file layout is settled.
* Native-auto interception default-on — needs actuate burn-in first.
* Pi backtester (`analyze_corpus.py --harness pi`) — the Pi trigger is
  exact (`contextWindow − reserve`), so the backtester adds value only
  after live telemetry accumulates.
* pi-custom-compactor `compactionSpec` integration — package not
  installed here; passive coexistence (skip interception) is implemented.
* Nightly Pi-version canary — extend `nightly_eval.py` to diff the
  `install_pi.py` version pin against the live package and flag drift,
  as it already does for the Claude CLI version.
* Pi founding-capture parity test — `tests/test_pi_session_lib.py` is
  wave-2 scoped; MAIN gained `initial_user_prompts` capture (94ee3a8)
  after the worktree forked. `pi_bridge.py` uses getattr-safe access, so
  the merge is safe either way; add the test post-merge.

## Known limitations

* Transcript JSONL schema is not a public API; re-run smoke tests after
  Claude Code upgrades.
* Occupancy estimate ignores the fixed system-prompt share; thresholds
  are approximate by design — `/context` is ground truth.
* Compaction detection in the backtester is heuristic (usage-drop);
  eyeball a few detections against raw JSONL before trusting aggregates.
* On subscription billing this saves quota, not dollars; on API billing
  both.

## Smoke test (run after any change)

```bash
echo '{"session_id":"t","transcript_path":"/path/to/session.jsonl","cwd":"'$PWD'","hook_event_name":"UserPromptSubmit","prompt":"x"}' \
  | python3 context_monitor.py
echo '{"session_id":"t","transcript_path":"/path/to/session.jsonl","cwd":"'$PWD'","hook_event_name":"PreCompact","trigger":"manual","custom_instructions":""}' \
  | python3 precompact_analyzer.py
```
