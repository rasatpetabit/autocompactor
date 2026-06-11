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
backtester. This directory came from an rsync handoff of a claude.ai sandbox
session on 2026-06-09. Read `HANDOFF.md` first for the decision record,
including the pi-custom-compactor evaluation and prioritized open items.

## Operating notes

- Before changing behavior, run `python3 -m pytest tests/ -q` and
  `bash tests/smoke_test.sh` when safe. The recovered baseline expected
  37 pytest cases.
- Owner directive: `>80%` of spend is cached reads. Compact often and keep
  context low.
- Every-turn cheapness relies on the min-savings guard: no recommendation when
  `context - POST_FLOOR(70k) < MIN_SAVINGS(30k)`, quiet below about `100k`.
- For transcripts larger than `MAX_FULL_PARSE_MB(8)`, use tail-only parsing
  from the last verified `compact_boundary`; `peak_ctx` is carried in session
  state for the window clamp.
- After a few live days, run `python3 analyze_corpus.py --events` to inspect
  reduction-ratio-by-phase and tune phase addenda.
- Open refinements from the recovered handoff: improve `topic_shift` precision
  with prompt replay at backtest sample points; keep watching `stale_output`,
  which was below baseline at `0.90`.
- DONE 2026-06-10: `error_resolved`, `tests_pass`, and `idle_gap` were demoted
  to observe-only via `AUTOCOMPACTOR_OBSERVE_ONLY`; telemetry and the
  backtester still measure them, but they no longer gate recommendations.

## Conventions

- Transcript JSONL schema is not a public API. Pin the producer version and
  re-run smoke tests after upgrades.
- Telemetry and artifacts are local-only and content-free by design:
  counts/ratios/paths only, never transcript text.
- Hooks must never raise into the hook path. Degrade silently and log
  best-effort.
- `transcript_lib.active_signals()` is the single signal registry; the live
  monitor and backtester must not diverge.

## File map

| file | role |
|---|---|
| `context_monitor.py` | prompt-time signal monitor: signals + burn-rate -> compaction recommendation; one-shot artifact re-injection post-compaction |
| `precompact_analyzer.py` | pre-compaction analyzer: backup, phase-aware instructions, artifact extraction |
| `transcript_lib.py` | JSONL parsing, signal registry, phase detection, instruction builder |
| `artifacts.py` | mechanical extraction -> disk -> budgeted digest |
| `stats.py` | telemetry appender |
| `analyze_corpus.py` | offline backtester + `--events` aggregator |
| `install.py` | idempotent local settings registration |
| `nightly_eval.py` | cron self-evaluation: tests, 1-day backtest, telemetry health checks, ceiling + trigger-drift + refill-breaker + microcompaction watches, dated reports, retention pruning; crontab: `03:30`, marker `# autocompactor-nightly` |
| `tests/` | fixtures + `smoke_test.sh` + `test_autocompactor.py` |
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
