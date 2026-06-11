<!-- agentic-dispatch:claude-shim v2 -->
# CLAUDE.md — autocompactor

Thin Claude Code shim. Canonical repo instructions are imported below; do not
add vendor-neutral policy here — put it in AGENTS.md.

@AGENTS.md

Central cross-repo policy (AUQ, RTK, Serena, Hindsight, context-mode, model
routing) lives in the agent-dispatch repo: run `agent-dispatch digest` for the
live routing policy, `agent-dispatch where` for the repo root. Never copy that
policy here.

## Claude Code specifics

- All user-facing questions go through `AskUserQuestion`; never end a turn with prose questions.
- Use Claude Code plugins, hooks, plan mode, and slash commands only as documented by repo-local `AGENTS.md` or active plugin settings.

<!-- agentic-dispatch:claude-notes — repo-specific Claude notes below this line survive re-migration -->

- Recovered install status from 2026-06-10: hooks were registered user-wide in
  `~/.claude/settings.json` and validated against real transcripts.
- Claude native ceiling used `CLAUDE_CODE_AUTO_COMPACT_WINDOW=200000`
  (`native ceiling = min(setting, model window)`).
- Recovered `settings.json` hook env thresholds: `AUTOCOMPACTOR_WINDOW=200000`,
  `SOFT_PCT=0.5` (`100k`), `HARD_PCT=0.62` (`124k`),
  `STALE_FRAC=0.90`, `COOLDOWN=20000`.
- Native auto-trigger was expected near `135k` (`absolute-reserve model
  window-65k`), confirmed at `400k` by max `preTokens`
  `336,512 = 400k-63,488`. If the measured trigger differs, keep `HARD_PCT`
  about `20-30k` ahead of it.
- Check Claude nightly reports under `~/.claude/autocompactor/reports/`; the
  watches line reports trigger drift, rapid-refill breaker suspects, and
  native microcompaction markers.
- Runtime state lives under `~/.claude/autocompactor/`: `stats/`,
  `artifacts/`, `backups/`, and per-session `*.state.json`.
- `context_monitor.py` is the Claude `UserPromptSubmit` hook: signals +
  burn-rate -> recommend `/compact`; one-shot artifact re-injection
  post-compaction.
- `precompact_analyzer.py` is the Claude `PreCompact` hook: backup,
  phase-aware `customInstructions`, artifact extraction.
- `install.py` performs idempotent `settings.json` hook registration.
- Claude floor cuts are owner-approval gated and audited in `HANDOFF.md`;
  recovered examples included global `CLAUDE.md` diet and plugin pruning.
  Each `10k` off the `~53k` interactive floor was estimated at about `~10%`
  of all cache-read volume.
