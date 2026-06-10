# autocompactor

Smarter, earlier, instruction-tailored compaction for Claude Code:
boundary-aware timing (when to compact), phase-aware structured
instructions (how to summarize), mechanical artifact extraction (what
never to entrust to a summarizer), local telemetry, and an offline
backtester. Developed in a claude.ai sandbox session 2026-06-09; this
directory is the rsync handoff. **Read HANDOFF.md first** — it contains
the full decision record (including the pi-custom-compactor evaluation)
and the prioritized open items.

## Status (2026-06-10): INSTALLED, tuned to a 200k ceiling — see HANDOFF.md session log

Hooks are registered user-wide in ~/.claude/settings.json and validated
against real transcripts. Owner directive: >80% of spend is cached
reads — compact often, keep context low. Thresholds (settings.json
env): CLAUDE_CODE_AUTO_COMPACT_WINDOW=200000 (native ceiling = min(
setting, model window)); AUTOCOMPACTOR_WINDOW=200000, SOFT_PCT=0.5
(100k), HARD_PCT=0.62 (124k), STALE_FRAC=0.90, COOLDOWN=20000. Native
auto-trigger expected ~135k (absolute-reserve model window−65k,
confirmed at 400k by max preTokens 336,512 = 400k−63,488). Every-turn
cheapness: min-savings guard (no recommendation when context −
POST_FLOOR(70k) < MIN_SAVINGS(30k) → quiet below ~100k) and tail-only
parsing for transcripts > MAX_FULL_PARSE_MB(8) (from last verified
compact_boundary; peak_ctx carried in session state for the window
clamp).

## Resume here — priority order

1. `python3 -m pytest tests/ -q` (37 cases) and `bash tests/smoke_test.sh`
   — both must pass before touching anything.
2. Check the next nightly report (~/.claude/autocompactor/reports/) —
   first clean read under the 200k ceiling. Expect auto preTokens
   median ~135k; the watches line reports trigger drift, rapid-refill
   breaker suspects, and native microcompaction markers. If the
   measured trigger differs, keep HARD_PCT ~20-30k ahead of it.
3. Floor cuts (owner-approval gated, audit in HANDOFF.md): global
   CLAUDE.md diet, subagent-models.md injection→digest, serena
   disable-until-configured, plugin pruning. Each 10k off the ~53k
   interactive floor ≈ ~10% of all cache-read volume.
4. After a few live days: `python3 analyze_corpus.py --events` —
   reduction-ratio-by-phase to tune phase addenda.
5. Open refinements: topic_shift precision (needs prompt replay at
   backtest sample points); demote error_resolved/tests_pass/idle_gap
   from the gating set (anti-predictive on both 14-day and day-one
   data); watch stale_output (below baseline at 0.90).

## Conventions

- Transcript JSONL schema is NOT a public API. Pin the Claude Code
  version; re-run smoke tests after upgrades.
- Telemetry and artifacts are local-only and content-free by design
  (counts/ratios/paths — never transcript text). Keep it that way.
- All thresholds are env vars (AUTOCOMPACTOR_*) — see README.md table.
- Hooks must never raise into the hook path: degrade silently, log
  best-effort.
- transcript_lib.active_signals() is the single signal registry — the
  live monitor and the backtester must never diverge again.

## File map

| file | role |
|---|---|
| context_monitor.py | UserPromptSubmit hook: signals + burn-rate -> recommend /compact; one-shot artifact re-injection post-compaction |
| precompact_analyzer.py | PreCompact hook: backup, phase-aware customInstructions, artifact extraction |
| transcript_lib.py | JSONL parsing, signal registry, phase detection, instruction builder |
| artifacts.py | mechanical extraction -> disk -> budgeted digest |
| stats.py | telemetry appender |
| analyze_corpus.py | offline backtester + --events aggregator |
| install.py | idempotent settings.json hook registration |
| nightly_eval.py | cron self-evaluation: tests, 1-day backtest, telemetry health checks, ceiling + trigger-drift + refill-breaker + microcompaction watches, dated reports, retention pruning (crontab: 03:30, marker `# autocompactor-nightly`) |
| tests/ | fixtures + smoke_test.sh + test_autocompactor.py (pytest) |

Runtime state lives under ~/.claude/autocompactor/ (stats/, artifacts/,
backups/, per-session *.state.json).
