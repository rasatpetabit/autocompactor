# WORKLOG

Terse handoff log for collaborating agents. Newest entry first.

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
