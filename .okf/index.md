---
type: index
title: autocompactor knowledge catalog
timestamp: 2026-07-01T00:00:00Z
privacy: private
tags: [autocompactor, pi, context-compaction, index]
---

# autocompactor knowledge catalog

`autocompactor` is a **Pi context compactor**: it provides earlier,
instruction-tailored context compaction for Pi coding-agent sessions —
boundary-aware timing for *when* to compact, phase-aware structured
instructions for *how* to summarize, mechanical artifact extraction for
content that should never be entrusted to a summarizer's goodwill, and
local telemetry. The core is harness-agnostic by design; Pi
(`@earendil-works/pi-coding-agent`) is the sole adapter that currently
ships (a prior Claude Code adapter was removed in a 2026-06-21 pivot).

## Purpose

- Watch live context occupancy and task-boundary signals (git commit,
  test-pass markers, all-TodoWrite-completed, idle gaps, subagent
  completion, stale tool output, burn rate, topic shift) and decide when
  compaction is worthwhile — earlier and cheaper than waiting for native
  auto-compaction near the context ceiling.
- Build phase-aware compaction instructions (debugging / implementation /
  exploration / wrapup) plus a base structured-handoff schema that
  preserves founding-goal prompts verbatim across passes.
- Mechanically extract facts (files edited, errors, last task statement)
  to disk as artifacts, then re-inject a budgeted digest on the first
  prompt after compaction, instead of trusting an LLM summarizer with
  everything.
- Record local, content-free telemetry (`~/.autocompactor/pi/`) and run a
  nightly self-evaluation (tests, telemetry health, retention pruning).

## Key components

- `src/autocompactor/` — the harness-agnostic Python core package (see
  [pi-bridge-and-core.md](pi-bridge-and-core.md) for the module map).
- `src/pi/autocompactor.ts` — the Pi TypeScript extension shim, installed
  by `python3 src/install_pi.py` into `~/.pi/agent/extensions/`.
- `config.json` / `config.local.json` — single-namespace tuning at repo
  root, read via `config_lib.py`; `AUTOCOMPACTOR_*` env vars override any
  key (see [pi-bridge-and-core.md](pi-bridge-and-core.md) for the tunable
  table).
- `tests/` — ~100+ pytest cases plus `smoke_test_pi.sh` (Pi bridge
  contract) and a Node `--test` suite for the TS shim against a stubbed
  `pi`/`ctx`.
- `AGENTS.md` — architecture table, operating notes, conventions
  (transcript JSONL schema is not a public API; hooks must never raise;
  telemetry is content-free by design).
- `HANDOFF.md` — decision record: Claude-adapter-removal rationale,
  `pi-custom-compactor` evaluation, signal-precision backtests, verified
  ground-truth pins (Pi version, `reserveTokens`, window/cap semantics),
  founding-goal directive, and open follow-up items.
- `WORKLOG.md` — full session-by-session history.

## Pointers

- `README.md` — architecture diagram, install steps, full tunables table,
  test matrix.
- `AGENTS.md` — canonical architecture/module table and operating notes
  (source of truth for module roles — this catalog does not duplicate the
  full table verbatim).
- `HANDOFF.md` — decision record and open items (`~/.claude/settings.json`
  Claude-hook deregistration still OPEN as of 2026-06-25).
- `docs/okf/okf-format-v0.1.md` (in `petabit-sysadmin`) — the OKF spec this
  catalog implements.

## Subsystem docs in this catalog

- [pi-bridge-and-core.md](pi-bridge-and-core.md) — the Pi bridge/extension
  contract, the harness-agnostic core module map, and the compaction
  decision model (signals, thresholds, window resolution, artifacts).
