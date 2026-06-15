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

- Claude native ceiling uses `CLAUDE_CODE_AUTO_COMPACT_WINDOW`
  (`native ceiling = min(setting, model window)`).
- Recovered `settings.json` hook env thresholds: `AUTOCOMPACTOR_WINDOW=200000`,
  `SOFT_PCT=0.5` (`100k`), `HARD_PCT=0.62` (`124k`), `STALE_FRAC=0.90`,
  `COOLDOWN=20000`. Live tuning on this host keeps
  `CLAUDE_CODE_AUTO_COMPACT_WINDOW=400000`.
- Native auto-trigger confirmed at `400k` (max `preTokens`
  `336,512 = 400k-63,488`). If the measured trigger differs, keep `HARD_PCT`
  about `20-30k` ahead of it.
- Tuning lives in `config.json` (+ gitignored `config.local.json`); runtime
  env (`AUTOCOMPACTOR_*`) overrides. The installer never seeds `AUTOCOMPACTOR_*`
  tuning as env.

### Runtime state + nightly

- Runtime state lives under `~/.claude/autocompactor/`: `stats/events.jsonl`,
  `artifacts/`, `backups/`, and per-session `*.state.json`.
- Nightly reports under `~/.claude/autocompactor/reports/`; the watches line
  reports trigger drift, rapid-refill breaker suspects, and native
  microcompaction markers. Cron: `03:30`, marker `# autocompactor-nightly`.
- Claude floor cuts are owner-approval gated and audited in `HANDOFF.md`; each
  `10k` off the `~53k` interactive floor is roughly `~10%` of all cache-read
  volume.
