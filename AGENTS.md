# ras/autocompactor


<!-- agentic-dispatch:central-pointer v2 -->
## Central agent policy

Cross-repo AskUserQuestion/ask_user_question (AUQ), RTK, Serena, Hindsight,
context-mode, and subagent/model-dispatch policy is centralized in the
agent-dispatch repo. Read it via `agent-dispatch where` (repo root) or
`agent-dispatch digest` (live routing policy). Do not duplicate or override
that policy here.

## §routing — managed by agent-dispatch (do not hand-edit)

Binding rules (enforced by PreToolUse guard — violations are hard-blocked):
- haiku: FORBIDDEN — no dispatch path exists, no override possible.
- sonnet: OVERRIDE-ONLY — requires a live, unexpired grant (`agent-dispatch override grant sonnet …`).
- model param MUST be explicit — missing model is denied.

For the full routing policy, fallback chains, and backend health:
  agent-dispatch digest          # live, from the canonical policy file
  agent-dispatch resolve <class> # deterministic tier for a task class

Source of truth: `policy/dispatch-policy.jsonc` in the agent-dispatch repo (`agent-dispatch where`)
<!-- agent-dispatch:end -->

## Project substance

Autocompactor provides earlier, instruction-tailored compaction for coding-agent
sessions: boundary-aware timing for when to compact, phase-aware structured
instructions for how to summarize, mechanical artifact extraction for content
that should not be entrusted to a summarizer, local telemetry, and an offline
backtester. The core is harness-agnostic; two adapters ship (Claude Code, Pi).
Read `HANDOFF.md` for the decision record, including the pi-custom-compactor
evaluation and prioritized open items.

## Operating notes (harness-agnostic)

- Before changing behavior, run `python3 -m pytest tests/ -q` and
  `bash tests/smoke_test.sh` when safe. Current baseline is 164 pytest cases.
- Owner directive: `>80%` of spend is cached reads. Compact often and keep
  context low.
- Every-turn cheapness relies on the min-savings guard: no recommendation when
  `context - POST_FLOOR(70k) < MIN_SAVINGS(30k)`, quiet below about `100k`.
- For transcripts larger than `MAX_FULL_PARSE_MB(8)`, use tail-only parsing
  from the last verified `compact_boundary`; `peak_ctx` is carried in session
  state for the window clamp.
- After a few live days, run `python3 src/analyze_corpus.py --events` to inspect
  reduction-ratio-by-phase and tune phase addenda.
- Open refinements: improve `topic_shift` precision with prompt replay at
  backtest sample points; keep watching `stale_output`, which was below
  baseline at `0.90`.
- Signal gating (`OBSERVE_ONLY`) is re-derived from measured per-signal lift,
  not fixed. 2026-06-10 demoted `error_resolved`/`tests_pass`/`idle_gap`
  (anti-predictive then). 2026-06-17 recalibration (backtest 2026-06-17,
  Claude only): demoted `burn_rate` (0.9x) + `subagent_done` (0.8x) — now
  sub-baseline as gates; re-promoted `idle_gap` (7.5x) + `tests_pass` (2.7x) —
  reversed by the larger corpus. `error_resolved` stays observe-only.
  `idle_gap` (n=16) / `tests_pass` (n=30) are thin — re-check next nightly.
  Pi keeps the pre-2026-06-17 conservative set (it actuates; must retain
  `subagent_done`/`commit` as strong gates — design trap #4).

## Conventions

- Transcript JSONL schema is not a public API. Pin the producer version and
  re-run smoke tests after upgrades.
- Telemetry and artifacts are local-only and content-free by design:
  counts/ratios/paths only, never transcript text.
- Hooks must never raise into the hook path. Degrade silently and log
  best-effort.
- `transcript_lib.active_signals()` is the single signal registry; the live
  monitor and backtester must not diverge.

## Architecture

All implementation modules live in the `src/autocompactor/` package. The thin
shims in `src/*.py` are the entrypoints (hooks, cron, CLI) — they put `src/`
on `sys.path` and call the matching `autocompactor.<module>.main()`.
`config.json` and `config.local.json` stay at the checkout root as
user-facing config.

| file | role |
|---|---|
| `src/autocompactor/transcript_lib.py` | JSONL parsing, signal registry, phase detection, instruction builder (shared brain) |
| `src/autocompactor/config_lib.py` | unified config reader: `config.json`(+local) + `AUTOCOMPACTOR_*` env overrides, per-harness sections |
| `src/autocompactor/context_monitor.py` | prompt-time signal monitor: signals + burn-rate -> recommendation; one-shot artifact re-injection |
| `src/autocompactor/precompact_analyzer.py` | pre-compaction analyzer: backup, phase-aware instructions, artifact extraction |
| `src/autocompactor/artifacts.py` | mechanical extraction -> disk -> budgeted digest |
| `src/autocompactor/stats.py` | telemetry appender (`harness` field) |
| `src/autocompactor/statedir.py` | harness-namespaced state roots |
| `src/autocompactor/window_resolver.py` | effective-window resolution (native ceiling vs learned tier) |
| `src/autocompactor/analyze_corpus.py` | offline backtester + `--events` aggregator |
| `src/autocompactor/nightly_eval.py` | cron self-evaluation: tests, 1-day backtest, telemetry health checks, dated reports, retention pruning |
| `src/autocompactor/pi_session_lib.py` | Pi v3 tree-format JSONL -> `TranscriptStats` |
| `src/autocompactor/pi_bridge.py` | never-raise JSON CLI bridging the Pi extension to the Python core (evaluate/prepare/reinject) |
| `src/autocompactor/install.py` | Claude Code harness adapter installer |
| `src/autocompactor/install_pi.py` | Pi harness adapter installer (copy-with-rewrite TS shim, version-pin) |
| `src/*.py` | thin entrypoint shims (hook/cron/CLI targets): `context_monitor`, `precompact_analyzer`, `pi_bridge`, `analyze_corpus`, `nightly_eval`, `install`, `install_pi` |
| `src/pi/autocompactor.ts` | Pi TypeScript extension |
| `config.json` | versioned tuning (top-level + per-harness sections); `config.local.json` is the gitignored site-local overlay |
| `tests/` | fixtures + `smoke_test.sh` + `smoke_test_pi.sh` + `test_*.py` |

## Harness adapters

Two adapters ship; each has its own installer and operating specifics. Keep
adapter-specific operating detail in the matching harness doc, not here.

- **Claude Code** — `python3 src/install.py` registers hooks, env defaults,
  and the nightly cron. Claude-specific operating detail (hook wiring,
  `settings.json` thresholds, native-ceiling tuning, state paths, verification)
  lives in **`CLAUDE.md`**.
- **Pi** (`@earendil-works/pi-coding-agent`) — `python3 src/install_pi.py`
  installs `src/pi/autocompactor.ts`, which shells out to `src/pi_bridge.py`
  (the shared Python core). State/telemetry live under `~/.autocompactor/pi/`.
  See `HANDOFF.md` ("Pi harness") for the architecture, actuate-vs-advise
  decision, and verified ground-truth pins.
<!-- agent-dispatch:begin routing hash=6d8307801e22f016588774ae010198516e8402aa6a7cd0724a433885e67b981b -->
## §routing — managed by agent-dispatch (do not hand-edit)

Binding rules (enforced by PreToolUse guard — violations are hard-blocked):
- haiku: FORBIDDEN — no dispatch path exists, no override possible.
- sonnet: OVERRIDE-ONLY — requires a live, unexpired grant (`agent-dispatch override grant sonnet …`).
- model param MUST be explicit — missing model is denied (exception: harness built-ins Explore/Plan inherit the frontier session model).

For the full routing policy, fallback chains, and backend health:
  agent-dispatch digest          # live, from the canonical policy file
  agent-dispatch resolve <class> # deterministic tier for a task class

Source of truth: policy/dispatch-policy.jsonc in the agent-dispatch repo (run `agent-dispatch where` for its root).
<!-- agent-dispatch:end -->
