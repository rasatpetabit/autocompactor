# Retro — pi-harness (2026-06-10)

Pi coding harness support for autocompactor with 100% Claude Code
compatibility — Workstream C of the approved three-workstream plan.
7 waves (0–7 incl. the wave-0 compat pins), all recorded done, zero
failed/qctl tasks, scope ok every wave.

## What shipped

- **Compat pins first** (`tests/test_compat_pins.py`): signal-registry
  name set, OBSERVE_ONLY default, state-dir default, hook-stdout schema
  golden — written before any other change; green throughout.
- **Core seams**: `statedir.py` (harness-namespaced state roots; Claude
  default byte-stable), `stats.py` `harness` field, `analyze_corpus.py
  --stats-dir`. Zero edits to the seven pre-existing source files
  (verified by `git status --porcelain` gate every wave).
- **Pi adapter**: `pi_session_lib.py` (v3 tree walk leaf→root →
  `TranscriptStats`, active segment after last compaction entry),
  `pi_bridge.py` (never-raise JSON CLI: evaluate | prepare | reinject),
  fixtures incl. branched tree + compaction entry.
- **TS shim + tests**: `pi/autocompactor.ts` (zero-spawn pre-gate,
  advise/actuate modes, reentrancy flag, error-swallow everywhere) +
  `pi/test/extension.test.mjs` (9 node --test cases).
- **Installer + docs**: `install_pi.py` (copy-with-rewrite, atomic,
  version pin, --status/--remove doctor), README full-matrix + Pi
  sections, HANDOFF Pi architecture/decision/deferral record.
- **Live install (owner-gated, AUQ approved)**: shim installed to
  `~/.pi/agent/extensions/autocompactor.ts`, pin 0.79.1; probe session
  produced a `harness:"pi"` monitor_eval row in
  `~/.autocompactor/pi/stats/events.jsonl`; Claude install base
  confirmed untouched.

## Verification

Finish verify: 45 commands, 44 pass. The single failure is the literal
`node --test pi/test` — node 22.22.3 resolves a positional directory as
a CJS module (MODULE_NOT_FOUND); the file-explicit equivalent
`node --test pi/test/extension.test.mjs` passes 9/9 in the same list.
Deviation documented in README (glob form + pitfall note) and the
wave-5 digest at execution time; verify answered pass on that evidence.

## Deviations & lessons

- **Codex unreliable on this host** (two silent deaths early in the
  run) → waves driven foreground per owner AUQ: qwen for bounded
  sub-frontier writes (`qwen_write_file` with verify hooks), Claude
  inline for judgment-heavy tasks. Worked cleanly for all 7 waves.
- **node 22 `--test` directory args don't work** — author verify
  commands with globs or explicit files.
- **Worktree-baked bridge path**: install from the WT bakes the WT's
  `pi_bridge.py` path into the live shim. Owner-approved follow-up:
  re-run `python3 install_pi.py` from MAIN immediately after merge,
  before worktree deletion (`--status` flags it; a dangling bridge
  degrades to silence, never breaks Pi).

## Deferred (recorded in HANDOFF)

Plugin packaging; interception default-on; Pi backtester;
compactionSpec integration; nightly Pi-version canary; post-merge
founding-capture parity test (WT's pi_session_lib predates MAIN's
initial_user_prompts capture; pi_bridge uses getattr-safe access).
Flip-to-actuate gated on ≥1 day of clean `harness:"pi"` telemetry.
