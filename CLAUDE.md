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
  `settings.json` env: `CLAUDE_CODE_AUTO_COMPACT_WINDOW=300000`,
  `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=90` (native auto fires near 90% × 300k ≈ 270k).
- **No `AUTOCOMPACTOR_*` env keys are set** on this host — `config.json` + code
  defaults govern. Live tuning (`config.json` `claude` section + top-level):
  `WINDOW=200000`, `HARD_PCT=0.55` (claude; hard line ≈ 110k), `COOLDOWN=15000`,
  `STALE_FRAC=0.90`, `POST_FLOOR=70000`, `MIN_SAVINGS=30000`, `PROFILE=economy`.
  Top-level `SOFT_PCT` is retired — the window-aware `target(W)` curve governs
  the soft line (≈100k at a 200k window).
- The 200k configured target is **deliberately aggressive** (well below the
  ~270k native trigger): compact early, keep context low. This is intended, not
  a misconfiguration — do not "fix" the effective window up to the ceiling.
- Measured native auto-trigger (nightly 2026-06-17, 300k ceiling): **median
  ~254k, max ~280k**. If you tighten toward native, keep `HARD_PCT` comfortably
  ahead of the measured trigger, not the ceiling.
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
- **Recommendation readout is terse** (no fixed advisory trailer — the anchors
  already convey urgency): prompt-time (`UserPromptSubmit`) and mid-burst
  (`PostToolUse`) emit `systemMessage: "autocompactor: {readout}"` and a short,
  non-duplicating `additionalContext` (Claude-only awareness, no numbers).
- **One combined notice at compaction time.** Claude's compaction redraw
  swallows any `PreCompact` `systemMessage`, so PreCompact now emits *only*
  `customInstructions` (plus its telemetry/stash) — **no** user-facing message.
  The single user-visible compaction notice is **`PostCompact`** (renders in the
  fresh post-compaction view): `compaction #N complete — before→after (reclaimed
  ~Z)` + composition (a) + preservation ledger (b) + (when armed) the
  customInstructions probe verdict. PreCompact stashes `pre_ledger`/`pre_comp`/
  `compaction_count`/`pre_tokens` so the post notice renders even if a fresh
  re-parse of the compacted transcript fails.

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
