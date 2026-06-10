# Spec: Pi coding harness support with 100% Claude Code compatibility

Approved 2026-06-09 (owner, via /plan AskUserQuestion: "Full Pi implementation").
Source: Workstream C of `/home/grojas/.claude/plans/plan-three-workstreams-for-lazy-cake.md`.

## Goal

autocompactor (boundary-aware compaction advisor, currently Claude Code-only)
gains full support for the Pi coding agent installed on this host, while the
existing Claude Code install base remains 100% compatible: entry-point files
byte-stable, signal registry unchanged, state-dir default unchanged, hook
stdout schema unchanged.

## Verified ground truth (do not re-derive; checked 2026-06-09)

- Pi IS installed: `/home/grojas/.npm-global/lib/node_modules/@earendil-works/pi-coding-agent`
  (earendil-works fork of badlogic/pi-mono). Real sessions at `~/.pi/agent/sessions/`.
- Session format: v3 tree JSONL — entries have `id`/`parentId`; the active
  conversation is the leaf→root path (a leaf-path walk is mandatory; sessions
  branch). Compaction entries have `type:"compaction"`. Documented in the
  package's local `docs/session-format.md`.
- Extension API (from `dist/*.d.ts`, authoritative):
  - `ctx.compact({customInstructions, onComplete, onError})` — an ACTUATOR
    (Pi can self-trigger compaction; Claude Code hooks cannot).
  - Events: `agent_end`, `turn_end`, `before_agent_start`, `session_compact`,
    `session_before_compact`.
  - `ctx.getContextUsage()` → `{tokens|null, contextWindow, percent}`
    (`tokens === null` right after a compaction — guard it).
  - `pi.exec(cmd, args, {timeout})` for shelling out;
    `pi.sendMessage({customType, ...}, {deliverAs: "nextTurn"})` for persisted
    one-shot injection.
  - `SessionBeforeCompactResult = {cancel?, compaction?}` ONLY — there is no
    instructions-passthrough; enrichment of a native compaction happens via
    cancel-and-retrigger or by self-triggering first.
- Pi's trigger is exact: `contextWindow − reserveTokens`. This host overrides
  reserveTokens to 40,000 (default 16,384).
- `@davidorex/pi-custom-compactor` is NOT installed here; coexist passively
  (skip interception if it appears in Pi settings `packages[]`).

## Architecture (decided — additive adapters, zero moves)

- **No existing file moves or renames.** `transcript_lib.py`, `context_monitor.py`,
  `precompact_analyzer.py`, `install.py`, `nightly_eval.py` stay byte-stable
  except where a task explicitly threads a parameter (docstring notes allowed).
  Hook command strings registered in `~/.claude/settings.json` must keep working.
- **One brain.** `TranscriptStats` (in `transcript_lib.py`) is the normalized
  model; `active_signals()` stays THE single signal registry; consumers
  (`active_signals`, `detect_phase`, `build_preservation_instructions`,
  `artifacts.extract`) are already harness-agnostic. Only the producer is
  harness-specific: `analyze()` (Claude) and the new `pi_session_lib.py` (Pi).
- New sibling modules (no package restructure):
  - `statedir.py` — harness-namespaced state roots: claude →
    `~/.claude/autocompactor` (UNCHANGED default), pi → `~/.autocompactor/pi`.
  - `pi_session_lib.py` — v3 tree walk leaf→root → `TranscriptStats`; active
    segment = entries after the last `type:"compaction"` on the leaf path.
  - `pi_bridge.py` — never-raise JSON CLI with subcommands
    `evaluate | prepare | reinject`; exit 0 always; stdout is a single JSON
    object (or nothing). This is the only process boundary the TS shim calls.
  - `pi/autocompactor.ts` — logic-minimal Pi extension shim (<~200 lines):
    zero-spawn pre-gate using `ctx.getContextUsage()` vs SOFT_PCT/MIN_SAVINGS
    and an in-memory cooldown; only past the gate does it `pi.exec("python3",
    [pi_bridge.py, "evaluate", ...], {timeout: 5000})`. try/catch everywhere —
    Pi must never break because autocompactor failed.
  - `install_pi.py` — idempotent install into `~/.pi/agent/extensions/`.
- **Actuator policy**: `AUTOCOMPACTOR_PI_MODE=advise|actuate`, ship `advise`
  (notify only). In actuate mode: on `agent_end`, if bridge recommends,
  `ctx.compact({customInstructions})` with a reentrancy flag. Native-auto
  interception (cancel-and-retrigger in `session_before_compact`) is env-gated
  `AUTOCOMPACTOR_PI_INTERCEPT`, default OFF; `prepare` still runs
  fire-and-forget there for artifacts + backup. We never own summarization.
- **Re-injection**: on `session_compact` → bridge `reinject` →
  `pi.sendMessage({customType:"autocompactor.digest", ...}, {deliverAs:"nextTurn"})`.
- **Window math on Pi is exact**: effective ceiling = `contextWindow −
  reserveTokens` passed as `window` into `active_signals()`.
  `AUTOCOMPACTOR_PI_*` env overrides for thresholds; do not tune until Pi
  telemetry exists.
- **Telemetry**: `stats.log_event` gains a `harness` field defaulting to
  `"claude"`; Pi events go to the Pi state dir; `analyze_corpus.py` gains
  `--stats-dir`. Telemetry stays local-only and content-free (counts/ratios/
  paths, never transcript text).

## Conventions binding every task

- Hooks/bridge never raise into the host path: degrade silently, exit 0,
  log best-effort.
- All thresholds are env vars (`AUTOCOMPACTOR_*`); README table is the
  reference.
- Transcript/session JSONL schemas are NOT public APIs — pin versions, smoke
  test after upgrades.
- `python3 -m pytest tests/ -q` AND `bash tests/smoke_test.sh` must be green
  at the end of EVERY wave (the compat gate). Baseline: 51 passed + smoke.

## Wave breakdown (maps 1:1 to plan waves)

### Wave 0 — pin the present (BEFORE any other change)
`tests/test_compat_pins.py`:
- signal-registry name-set pin + `OBSERVE_ONLY_DEFAULT` pin (transcript_lib).
- golden `build_preservation_instructions` output on the rich fixture.
- state-dir default pin: claude paths resolve to `~/.claude/autocompactor`.
- hook stdout schema pin: monitor and analyzer outputs on fixtures parse to
  exactly the expected top-level keys.

### Wave 1 — core seams
- `statedir.py` (new): `state_root(harness)`, claude default unchanged.
- Thread through `artifacts.py` / `stats.py`: module constants keep claude
  defaults; optional parameter/env (`AUTOCOMPACTOR_STATE_DIR` or harness arg)
  selects the Pi root.
- `stats.log_event` gains `harness` field default `"claude"`.
- `analyze_corpus.py --stats-dir` flag.
- Tests for each seam; full suite green.

### Wave 2 — Pi adapter (pure Python, no TS yet)
- Pi fixtures: synthetic v3 trees including a branched session and a
  `type:"compaction"` entry (+ sanitized real-shape lines).
- `pi_session_lib.py`: parse → leaf→root walk → active segment →
  `TranscriptStats`. Parity tests: same logical content as a Claude fixture ⇒
  same signals fire. Verify tool-call arg names against the installed
  package's `dist/core/tools/*.d.ts` (e.g. todo/edit/bash tool naming) before
  hardcoding extraction patterns.
- `pi_bridge.py`: `evaluate` (occupancy + signals + cooldown → recommend JSON),
  `prepare` (backup + artifacts + staged instructions), `reinject` (digest
  JSON for sendMessage). Never-raise tests incl. garbage stdin, missing
  session file, cooldown state round-trip.

### Wave 3 — TS shim + install
- `pi/autocompactor.ts`: events wired (`agent_end` pre-gate → evaluate;
  `session_before_compact` prepare fire-and-forget + optional intercept;
  `session_compact` → reinject via sendMessage), advise/actuate modes,
  reentrancy flag, try/catch everywhere.
- `pi/test/extension.test.mjs` (`node --test`, stub `pi`/`ctx` objects):
  pre-gate spawn-skip, actuate path triggers ctx.compact once (reentrancy),
  error-swallow (bridge absent/garbage), reinject delivery.
- `install_pi.py`: idempotent copy/symlink into `~/.pi/agent/extensions/`,
  version pin of the validated Pi package version, `--remove`.
- README + HANDOFF sections for Pi.

### Wave 4 — integration
- `tests/smoke_test_pi.sh` gated by `PI_SMOKE=1`: isolated HOME, end-to-end
  bridge calls on fixtures, and the bridge-removed never-break case.
- Full matrix run (pytest + smoke + node --test + PI_SMOKE).
- Live install on the real `~/.pi` in advise mode; verify
  `~/.autocompactor/pi/stats/events.jsonl` gets `monitor_eval` rows with
  `harness:"pi"` after a Pi session.
- Flip-to-actuate decision memo (HANDOFF) — actuate is a later deploy
  decision, not this round.

## Risks

- Pi 0.x API churn → pin the validated version in `install_pi.py`; confine
  all Pi-type knowledge to `pi_session_lib.py` + the TS shim.
- Branched-tree parsing → leaf-path walk mandatory; branch fixture included.
- State-dir regression breaking the Claude install base → wave-0 pin guards.
- Todo signals dormant on Pi (no TodoWrite analog) → commit/subagent_done/
  burn_rate carry boundary timing; acceptable.

## Out of scope (deferred, recorded in HANDOFF)

- Claude Code plugin packaging.
- Native-auto interception default-on.
- Pi backtester (`--harness pi` in analyze_corpus).
- pi-custom-compactor `compactionSpec` integration.
- Nightly Pi-version canary.
