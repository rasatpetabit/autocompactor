<!-- agentic-dispatch:claude-shim v2 -->
# CLAUDE.md — autocompactor (Claude Code operating guide)

This is the Claude Code-specific companion to `AGENTS.md` (the harness-agnostic
project brain, imported below). It carries **only** Claude Code operating
specifics: hook wiring, `settings.json` thresholds, native-ceiling tuning,
state paths, and verification. Vendor-neutral project info lives in
`AGENTS.md` — do not duplicate it here.

@AGENTS.md

Central cross-repo policy (AUQ, RTK, Serena, Hindsight, context-mode, model
routing) lives in the agent-dispatch repo: run `agent-dispatch digest` for the
live routing policy, `agent-dispatch where` for the repo root. Never copy that
policy here.

## Claude Code specifics

- All user-facing questions go through `AskUserQuestion`; never end a turn with prose questions.
- Use Claude Code plugins, hooks, plan mode, and slash commands only as documented by repo-local `AGENTS.md` or active plugin settings.

<!-- agentic-dispatch:claude-notes — repo-specific Claude notes below this line survive re-migration -->

### Install + verify (this checkout)

```bash
python3 src/install.py            # register hooks + fill missing AUTOCOMPACTOR_* env keys
python3 src/install.py --cron     # ...and the 03:30 nightly self-evaluation cron
python3 src/install.py --verify   # pytest + smoke + live-transcript probe (nonzero on failure)
python3 src/install.py --status   # doctor: hooks/env/cron/state/report-age/CLI-version drift
python3 src/install.py --remove   # unregister hooks + our env keys + cron
```

All flags idempotent; tuned env values are never overwritten (use `--force-env`
to reset). Run `/hooks` inside Claude Code to confirm registration.

### Hook wiring

- **`UserPromptSubmit` → `src/context_monitor.py`** — prompt-time signal
  monitor: occupancy + boundary signals + burn-rate -> recommend `/compact`
  via `systemMessage`; one-shot artifact re-injection post-compaction.
- **`PreCompact` → `src/precompact_analyzer.py`** (both `manual` and `auto`
  matchers) — transcript backup, phase-aware `customInstructions`, artifact
  extraction.

Hooks must never raise into the hook path — every failure degrades to exit 0
with no traceback.

### Tuning + native ceiling

Values below are the live state verified 2026-06-17 on this host (Opus 4.8,
1M model window). Re-verify against `config.json` and `~/.claude/settings.json`
rather than trusting prose — earlier revisions of this section had drifted
badly (claimed 400k ceiling / 336k trigger / env thresholds that were never set).

- Claude native ceiling uses `CLAUDE_CODE_AUTO_COMPACT_WINDOW`; the resolver
  treats it as a **cap** (`effective = min(configured WINDOW, native_ceiling)`,
  Claude-only — see `window_resolver.resolve_window`), never a source. Live
  `settings.json` env (verified 2026-06-17): `CLAUDE_CODE_AUTO_COMPACT_WINDOW=900000`,
  `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=90` (native auto fires near 90% × 900k ≈ 810k).
  This is owner-intended (was 300k in earlier revisions of this doc). Because the
  resolver caps `effective = min(WINDOW 200000, 900000) = 200000`, the autocompactor's
  advisory behavior is UNCHANGED by the larger ceiling — only the "forced
  auto-compact" native-wall anchor (~810k) moves. The `~/.claude/statusline.js`
  ctx meter is now anchored to that same `effective` target (`min(CTX_TARGET
  200000, acw) = 200000`), so it is INVARIANT to the ceiling (fixed 2026-06-18;
  previously it rescaled `remaining_percentage` against the raw `acw`, so the
  same 150k context swung 50%→17% when the ceiling went 300k→900k — the
  "statusbar context calculation is completely wrong now" report). Override the
  target with `CLAUDE_STATUSLINE_CTX_TARGET` if `config.json` `WINDOW` changes.
- **No `AUTOCOMPACTOR_*` env keys are set** on this host — `config.json` + code
  defaults govern. Live tuning (`config.json` `claude` section + top-level):
  `WINDOW=200000`, `HARD_PCT=0.55` (claude; hard line ≈ 110k), `COOLDOWN=15000`,
  `STALE_FRAC=0.90`, `POST_FLOOR=70000`, `MIN_SAVINGS=30000`, `PROFILE=economy`.
  Top-level `SOFT_PCT` is retired — the window-aware `target(W)` curve governs
  the soft line (≈100k at a 200k window).
- The 200k configured target is **deliberately aggressive** (far below the
  ~810k native trigger): compact early, keep context low. This is intended, not
  a misconfiguration — do not "fix" the effective window up to the ceiling.
- Measured native auto-trigger **under the old 300k ceiling** (nightly 2026-06-17):
  median ~254k, max ~280k — now historical; at the live 900k ceiling native auto
  is expected ~810k (90%), not yet re-measured. Either way the 200k-capped target
  fires far earlier, so this only matters if you tighten `HARD_PCT` toward native
  — and then track the *measured* trigger for the current ceiling, not the ceiling.
- Tuning lives in `config.json` (+ gitignored `config.local.json`); runtime
  env (`AUTOCOMPACTOR_*`) overrides if present. The installer never seeds
  `AUTOCOMPACTOR_*` tuning as env.
- User-facing readouts use absolute anchors (`policy.readout_line`:
  *in context · compact advised ~soft–hard · forced auto-compact ~Nk (~Mk away)
  · model window*), not a bare occupancy % — the % is computed against the
  aggressive 200k target and routinely exceeds 100% on this 1M host, so it is
  kept to telemetry only. The advisory band (soft–hard) and the forced native
  wall are shown as *distinct* anchors with headroom, so "near the soft limit"
  can't be misread as "one turn from auto-compacting".
- Two intelligence sub-displays ride under the readout / on the compaction
  notice: **(a) composition** — `policy.composition_line(transcript_lib.`
  `context_composition(st))` renders *≈ skills (names) · floor · summary ·
  tool (stale%) · asst · prompts*, per-category token estimates (chars/4)
  reconciled so the parts always sum to the true total (residual `base`/"floor"
  absorbs error); **(b) preservation ledger** — `artifacts.preservation_ledger()`
  names what was extracted verbatim to disk vs left to the lossy summarizer vs
  dropped for budget (keep/drop comes from `artifacts.budget_plan()`, shared
  with `build_digest` so they can't disagree). Both surface on Claude and Pi;
  both are content-free (counts/token-estimates/category-names only).
- **Recommendation readout rides `systemMessage` (verbatim); `additionalContext`
  stays silent.** Claude Code renders a hook's `systemMessage` to the user
  verbatim — it is the reliable, visible channel — so the full readout (anchors
  + composition + skill warning) is emitted there by both `UserPromptSubmit` and
  `PostToolUse`. `additionalContext` is Claude-only, carries **no numbers**, and
  exists only to tell Claude the readout is already shown to the user and NOT to
  restate it — so the user sees exactly one rich readout, never a duplicate.
  History (each over-correction caused the next complaint): additionalContext-
  only → "no useful info" (Claude relayed inconsistently); both channels with
  numbers → "double"; systemMessage dropped, numbers in additionalContext-relay
  → "stripped/useless" (the relay is unreliable — Claude paraphrases/omits). The
  landing point: numbers in `systemMessage` (reliable + verbatim), Claude told to
  stay quiet. Do NOT move the readout back into `additionalContext`. (The
  compaction notice rides the same `systemMessage` channel, below.)
- **Window-invariant occupancy-milestone mid-burst readout (owner Q2: "escalating
  thresholds", revised 2026-06-18 to occupancy rungs).** During an autonomous burst
  that produces no `UserPromptSubmit`, the `PostToolUse` watchdog is the only chance
  to surface a readout — but emitting one per qualifying tool call reads as spam. So
  `_run_posttooluse` emits the readout only on the **first cross of the soft line**,
  the **first cross of the hard line**, each **occupancy rung above hard**
  (`policy.burst_milestone`, default `0.70/0.80/0.90/0.97` × effective window;
  override via `BURST_MILESTONE_OCCUPANCY`), then **+`tail_pct`×window** steps past
  the window. `last_milestone_tokens` is the highest rung announced this burst; it
  resets to `0` when context drops below soft (a fresh burst can re-announce). This
  **replaced the old fixed `+BURST_MILESTONE_STEP` (100k) ladder**, which was wider
  than the `[hard≈110k, window 200k]` danger band — after the single hard-line
  announce the next step (210k) landed past the window, so the watchdog went
  **silent across the entire 110k→200k danger zone** (the 2026-06-18 "autocompactor
  doesn't appear to be running" report: a session in `/srv/dev/petabit/skynet`
  climbed 114k→203k unwarned). Occupancy rungs scale with the window, so no
  rung-to-rung gap can exceed the `occ_pcts` spacing whatever the window/ceiling.
- **Single-sample spike guard (same 2026-06-18 fix).** A tail-parse read can
  momentarily double-count (observed: `303k` for one eval, back to `155k` 4s later).
  `policy.is_ctx_spike` (>1.5× ratio **and** >0.5×window absolute jump vs the prior
  `last_ctx`) flags such a read; on a spike the watchdog neither advances the
  milestone ladder (a spurious high rung would mute it for the rest of the burst)
  nor updates `peak_ctx` (a spike had inflated the learned window to the 512k tier
  via `observed_peak`). `last_ctx` is recorded every eval, so a *genuine* sustained
  jump is corroborated and let through on the next eval. Skip events carry
  `spike_suspected`. Set `AUTOCOMPACTOR_LOG_WATCHDOG_SKIPS=1` to log cheap
  `watchdog_skip` evals on at/above-soft non-recommends so PostToolUse coverage is
  measurable for a day.
- **Prompt-time readout decoupled from the burst cooldown (2026-06-18, the "I see
  literally nothing across several invocations" report).** `last_reco_tokens` is the
  `UserPromptSubmit` token-distance cooldown anchor (`suppressed = 0 ≤ ctx −
  last_reco_tokens < COOLDOWN`, 15k). The `PostToolUse` burst path used to **also**
  write it (`state.update(last_reco_tokens=ctx, …)`) even though the burst already
  gates on its own `last_milestone_tokens`. So every mid-burst recommend bumped the
  shared anchor to ~current ctx, and the user's next `UserPromptSubmit` — the one
  moment they reliably look — landed inside the 15k window and was suppressed *every
  time* (measured live: 3/3 prompt evals at 126k/160k/162k all
  `suppressed_by_cooldown`). Combined with mid-burst readouts the user isn't watching,
  the net was zero visible output. **The `systemMessage` channel itself was never
  broken** — a 2.1.181-binary audit confirmed a dedicated `hook_system_message` render
  path for UserPromptSubmit/PostToolUse, and the readout persists in `hook_success`
  attachments — it was simply being *gated away* at the only moment the user looks.
  Fix: the burst path no longer writes `last_reco_tokens`, so burst and prompt cadences
  are independent and the prompt-time readout fires whenever ctx is over the line.
  Confirmed live (owner saw `autocompactor: 253k in context · compact advised…` at
  prompt submit). Regression: `test_burst_recommend_does_not_starve_prompt_readout`.
  (A statusline-indicator variant was prototyped and rejected — it stepped on the
  separately-versioned `~/.claude/statusline.js` and leaked a contradictory token into
  other agents' status lines.)
- **Single compaction notice — first-of-either, one-shot (owner Q1).** Neither
  `PreCompact` nor `PostCompact` can carry a user-visible message: **CC 2.1.x
  rejects a `PreCompact` `hookSpecificOutput` outright** (PreCompact/PostCompact
  are not in the hook-output union — emitting one fails JSON-output validation and
  the user sees an "Invalid input" error), and a `PostCompact` `systemMessage` is
  **swallowed by the compaction redraw**. So `PreCompact` now does its work
  *silently* (backup + `customInstructions` *stash in state*, never emitted —
  Claude does not act on the field anyway — + artifact extraction) and `PostCompact`
  is **telemetry-only**. PreCompact instead **arms `pending_notice`** (alongside
  `pending_reinject`) and stashes `pre_ledger`/`pre_comp`/`compaction_count`/
  `pre_compact_tokens`. The single user-visible notice — `compaction #N complete —
  before→after (reclaimed ~Z)` + composition (a) + preservation ledger (b) —
  rides whichever fires first of the **next `UserPromptSubmit` or next
  `PostToolUse`** (via `policy.compaction_notice()` on `systemMessage`, which
  self-prefixes `autocompactor: `), then disarms. The `UserPromptSubmit` arm also
  carries the reinject digest in `additionalContext`; the `PostToolUse` arm matters
  during bursts that produce no prompt (G2/G3). (`customInstructions` is still a
  legitimate, acted-on channel on **Pi**, via `pi_bridge.py` — Claude-only no-op.)

### Runtime state + nightly

- Runtime state lives under `~/.claude/autocompactor/`: `stats/events.jsonl`,
  `artifacts/`, `backups/`, and per-session `*.state.json`.
- Nightly reports under `~/.claude/autocompactor/reports/`; the watches line
  reports trigger drift, rapid-refill breaker suspects, and native
  microcompaction markers. Cron: `03:30`, marker `# autocompactor-nightly`.
- **Auto warning-coverage metric (WI-1 corrected).** nightly's "auto-compactions
  arrived unwarned" check is epoch-filtered (only the current `native_ceiling`
  config — old-config autos at e.g. ~133k are excluded), cold-start-separated (a
  native auto that fires before any `monitor_eval` ran in the session is
  *unwarnable*, reported as a note, not a miss), and session-level (warned if ANY
  prior recommended eval, not just within the last precompact interval). It only
  alarms with ≥4 measurable autos. Live current-epoch coverage is ~86% warned;
  the old metric's "6/10 unwarned" was an artifact of mixing epochs + the
  per-interval window. See `nightly_eval.auto_warning_coverage()` + WORKLOG.
- **`LOG_WATCHDOG_SKIPS` (off by default).** PostToolUse logs a `monitor_eval`
  only on the recommend branch, so its non-recommends are invisible. Set
  `AUTOCOMPACTOR_LOG_WATCHDOG_SKIPS=1` (env or `config.json`) to log cheap
  `watchdog_skip` evals (no full `analyze()`) for at/above-soft non-recommends, so
  PostToolUse coverage becomes measurable for a day. Leave off in steady state —
  it logs per qualifying tool call.
- Claude floor cuts are owner-approval gated and audited in `HANDOFF.md`; each
  `10k` off the `~53k` interactive floor is roughly `~10%` of all cache-read
  volume.
