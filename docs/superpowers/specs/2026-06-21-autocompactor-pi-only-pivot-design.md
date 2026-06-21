# Spec 0 — Autocompactor Pi-only pivot

**Date:** 2026-06-21
**Status:** design (pre-plan) — revised through two Codex GPT-5.5 xhigh adversarial
rounds (round 1: 3 BLOCKER/2 MAJOR; round 2: 1 BLOCKER/2 MAJOR/1 MINOR; all addressed below).
**Owner decision:** drop Claude Code support entirely; Pi becomes the sole product.
**Scaffolding teardown:** full flatten (owner-chosen).
**Sequence:** first of three specs. Spec A (Pi display / compaction-output) and
Spec B (Pi auto-tuning) follow and depend on this landing first.

---

## 1. Goal & rationale

Make the Pi harness the sole product. Remove the Claude Code adapter, delete the
dual-harness scaffolding that only existed to serve two harnesses, and slim the
shared core to **harness-agnostic logic + Pi**.

**Why this is defensible, not a retreat.** The Claude adapter's history (see
`WORKLOG.md` / `CLAUDE.md`) is largely a record of fighting Claude Code's hook
channels: `systemMessage` swallowed by the compaction redraw, `additionalContext`
relayed unreliably, `PreCompact` `hookSpecificOutput` rejected by CC 2.1.x,
cooldown starvation of the prompt-time readout, statusline-target drift. Claude
*advises only* — it cannot invoke `/compact`. Pi's in-process model actually
honors `customInstructions` and actuates real compactions via `ctx.compact()`.
Cutting the channel-fighting battle concentrates effort where the tool works.

## 2. The central correction (from adversarial review)

The first draft claimed the "kept agnostic brain" was already fed by the Pi
parser. **It is not.** The Claude JSONL parser silently supplies several fields
the kept functions read; the Pi parser (`pi_session_lib.py`) never populates
them. Verified against code:

| Field read by kept code | Reader | Claude populates | Pi populates |
|---|---|---|---|
| `skill_chars`, `skill_names` | `context_composition` (`transcript_lib.py:514,546`) | `:325-326` | **no** |
| `summary_chars` | `context_composition` (`:515`) | `:457` | **no** (Pi `continue`s past `compaction`, `pi_session_lib.py:275-276`) |
| `assistant_text_chars` | `context_composition` (`:517`) | `:373-378` (text **and** thinking) | **no** |
| `user_prompt_chars` | `context_composition` (`:518`) | `:464` | **no** |
| `st.todos` → `todos_all_done`/`todo_step` | `active_signals` (`:581-584`) | `:391-412` | **no** (only derives booleans *if* `st.todos` exists, `pi_session_lib.py:353-358`) |

A second coupling, also not Claude-only: `llm_digest` (the optional cheap-model
"what must survive" digest) lives in `precompact_analyzer.py:145-169` together
with its provider helpers (`:51-143`), and **`pi_bridge.py:49,254` imports and
calls it**. So `precompact_analyzer.py` is *not* a pure-Claude module — it is the
Claude PreCompact/PostCompact hook (`:172-410`) **plus** the shared LLM-digest
subsystem (`:51-169`).

**Resolution, decided by inspecting the Pi fixtures** (`tests/fixtures/pi/*.jsonl`
— entry types `session/message/model_change/thinking_level_change/compaction`;
roles `user/assistant/toolResult/custom/compactionSummary`; tools
`find/read/grep/edit/write/bash`; **no todo/task/skill markers**):

- **Port** `assistant_text_chars`, `user_prompt_chars` — data already flows
  through Pi's `assistant`/`user` branches (`pi_session_lib.py:278-302,328-335`).
  `assistant_text_chars` is documented as **text + thinking**
  (`transcript_lib.py:46`); Pi's `_message_text` excludes thinking unless
  `include_thinking=True` (`pi_session_lib.py:123-134`), so the port **must** count
  thinking blocks too (the field is fed to composition, and Pi thinking is real
  context — `tests/fixtures/pi/real_shapes.jsonl:5`).
- **Port** `summary_chars` with **one explicit source and a pre-segment scan.**
  Pi segmentation cuts the active path *after* the last compaction
  (`pi_session_lib.py:86-102`) and `analyze()` loops only the active segment
  (`:234-236,261-276`), but the summary-bearing records sit *before* the kept
  segment (`tests/fixtures/pi/with_compaction.jsonl:10` `role="compactionSummary"`
  and `:11` `type="compaction"`). So `summary_chars` is measured by scanning the
  records around the segmentation boundary, with **single-source precedence:
  prefer the `compactionSummary` role text; fall back to the `type="compaction"`
  entry text only when no `compactionSummary` role is present — never sum both**
  (avoids the double-count the two co-located records would cause).
- **Degrade** `skill_chars`/`skill_names` to `0`/`[]` on Pi — Pi marks no skill
  injections. Composition still reconciles (skills fold into `base`;
  `skill_warning` returns empty for zero skills, `policy.py:181-187`). Real Pi
  skill-detection is **Spec A** (display) work.
- **Drop** `todos_done`/`todo_step` from `active_signals` and delete their tests —
  Pi has no todo tool/producer. The pinned gates `subagent_done`/`commit` are
  unaffected: Pi produces them (`pi_session_lib.py:299-302,320-321`).
- **Extract** the `llm_digest` subsystem (`precompact_analyzer.py:51-169`) into a
  kept module **before** the Claude hook is deleted (see §4/§7) and repoint the
  `pi_bridge` import. `llm_digest(transcript_path)` reads the raw transcript tail
  and is format-agnostic, so the extraction is mechanical (no logic change).

## 3. Guiding constraints (preserve through the pivot)

- **Behavior-preserving deletion, NOT refactor-while-deleting.** Agnostic
  functions are deleted *around*, never renamed/restructured in the same pass.
- **Pi parser field-completion and the `llm_digest` extraction precede any Claude
  removal** (§2) so neither the brain nor `pi_bridge` import goes hungry.
- **Pi suite + Pi smoke are the regression oracle.** `pytest` (Pi + migrated
  agnostic subset) and `tests/smoke_test_pi.sh` must be green **after every
  phase**.
- **Invariants unchanged:** bridge/hooks never raise into the host path
  (degrade silently); telemetry + readouts are content-free; artifacts on disk
  hold verbatim content intentionally; `transcript_lib.active_signals()` stays the
  single signal registry.
- **Pi signal-gate set is pinned and becomes the only set.** Pi actuates →
  retain `subagent_done`/`commit` as strong gates (design trap #4); the
  Claude-only 2026-06-17 recalibration does not transfer.

## 4. Scope — what is deleted

| Path | Reason |
|---|---|
| `src/autocompactor/context_monitor.py` (+ `src/context_monitor.py`) | Claude UserPromptSubmit/PostToolUse hook |
| `precompact_analyzer.py` **Claude-hook portion only** (`:172-410` `_run`/`_run_precompact`/`_run_postcompact`/`main`; + `src/precompact_analyzer.py` shim) | Claude PreCompact/PostCompact hook. **The `llm_digest` subsystem (`:51-169`) is extracted first (§2/§7), not deleted.** |
| `src/autocompactor/install.py` (+ `src/install.py`) | Claude installer (hooks/env/cron/native ceiling) |
| `CLAUDE.md` | Claude operating guide |

From `transcript_lib.py` (module stays): Claude-JSONL front-end —
`load_transcript`, tail-parse, `find_last_boundary_offset`,
`current_context_tokens`, the `isMeta` skill-injection scan, and the Claude-only
`skill_chars`/`summary_chars`/`assistant_text_chars`/`user_prompt_chars`/`todos`
producers (their Pi equivalents land in §2 first).

`window_resolver.py`: delete native-ceiling cap + learned-tier Claude branch
(Pi's effective window is exact: `contextWindow − reserveTokens`). Keep the
minimal residue Pi needs, or inline into `pi_bridge.py` and delete the module.

`nightly_eval.py`: remove Claude-specific watches; reduce to Pi-relevant health.

`analyze_corpus.py` (backtester): **deferred to Spec B**, quarantined/removed in
the SAME phase as `load_transcript` (it imports `load_transcript` at module load,
`analyze_corpus.py:48-50`, and `test_autocompactor.py:23` imports it at
collection — a later-phase quarantine breaks pytest collection; see §7).

Tests deleted (Claude-hook-contract families in `tests/test_autocompactor.py`):
`test_monitor_*`, `test_analyzer_*`, `test_postcompact_*`, `test_posttooluse_*`,
`test_userpromptsubmit_*`, `test_run_hook_*`, `test_hooks_*` (Claude variants),
`test_find_last_boundary_offset_*`, `test_find_compactions_*`,
`test_current_context_tokens_*`, `test_backtest_*`, and the two todo-signal
tests. Plus `tests/test_install.py` and the Claude part of
`tests/test_window_resolver.py`.

## 5. Scope — what is kept and slimmed

**Pi core (untouched except the §2 work):** `pi_bridge.py` (import repointed to the
extracted `llm_digest`), `pi_session_lib.py` (gains the ported producers),
`install_pi.py`, `src/pi/autocompactor.ts`, `tests/test_pi_bridge.py`,
`tests/test_pi_session_lib.py`, `tests/smoke_test_pi.sh`.

**New kept module:** `src/autocompactor/llm_digest.py` — the extracted LLM-digest
subsystem (`llm_digest` + `_llm_digest_openai`/`_command`/`_claude` + `_env`/
`_llm_timeout`/`_openai_url`). Harness-agnostic; one live consumer (`pi_bridge`).

**Shared brain (kept; Claude bits sliced out):**
- `transcript_lib.py` — keep `active_signals` (minus the two todo signals),
  `detect_phase`, `context_composition`, `build_preservation_instructions`, the
  registry. Fed by `pi_session_lib → TranscriptStats`.
- `policy.py` — keep the renderers; drop the Claude `forced_auto` native-ceiling
  anchor; collapse `PolicyInput.harness` branching to the single Pi shape.
- `artifacts.py`, `stats.py`, `statedir.py`, `config_lib.py` — kept; flattened (§6).

**Agnostic-brain tests (migrated onto Pi fixtures):** signal registry
(`topic_shift`, `burn_rate`, `stale_output`, `active_signals`,
`subagent_done`/`commit`), `detect_phase`, instruction builder, artifacts,
**verbatim initial-prompt capture** (memory directive: never compress user input
prompts), policy renderers (`readout_line`, `context_composition` — asserting
skills may be 0 on Pi — `skill_warning`, `preservation_ledger`, `is_ctx_spike`,
`burst_milestone`).

## 6. Full flatten — single-namespace collapse (corrected twice)

Owner-chosen degree. The dual-harness abstraction overhead is removed:

- **Config — materialize the full *effective Pi* config as the new flat
  top-level.** Pi reads keys from BOTH the nested `pi.*` section AND top-level
  today: `pi_bridge` reads top-level `STALE_FRAC`/`POST_FLOOR`/`MIN_SAVINGS`
  (`:143-146`), `ARTIFACT_BUDGET` (`:319-322`); `policy` reads
  `PROFILE`/`STALE_FRAC`/`POST_FLOOR` (`:347-356`); plus `WINDOW`,
  `MAX_FULL_PARSE_MB`, the `AUTO_WINDOW_*` keys. The `pi.*` section overlays
  `MODE=actuate`, `RESERVE`, `SOFT_PCT`/`SOFT_PCT_WIDE`, `HARD_PCT`/`HARD_PCT_WIDE`,
  `COOLDOWN`, `MIN_SAVINGS`, `OBSERVE_ONLY`. **Flatten = compute the effective
  value (`pi.*` overlaid on top-level) for every key Pi reads, write those as flat
  top-level keys, then delete the `claude` and `pi` sections plus any Claude-only
  key.** This is NOT a narrow "promote the nested keys" move — dropping a
  top-level key Pi reads silently reverts it to a code default (the trap:
  top-level `STALE_FRAC=0.90` vs `pi_bridge` default `0.50`, `:144`). Removing the
  nested section moots the TS-deep-merge vs Python-deep-merge BLOCKER.
  `config_lib` loses harness-section precedence → `env > config.local.json >
  config.json`.
  **Gate test:** enumerate every config key read under `harness="pi"` (from
  `pi_bridge`, `policy`, `config_lib`, the TS shim); snapshot the effective value
  of each before and after the flatten; assert all identical — not just the keys
  named in this section.
- **Env prefix:** single `AUTOCOMPACTOR_*`. Delete `AUTOCOMPACTOR_PI_*` reads in
  `config_lib` and the TS shim (`src/pi/autocompactor.ts:76-86`), and the residual
  `CFG?.pi` lookups (`:50-61`).
- **`harness=` params:** removed from `policy`/`stats`/`build_context_state`/
  `config_lib` call sites.
- **`stats.py`:** drop the `harness` field; **repoint** `STATS_DIR`/`_stats_dir`
  fallback (`stats.py:25-32`) from `~/.claude/...` to the Pi root.
- **`artifacts.py`:** **repoint** `ART_DIR` (`:34`) and `_artifact_dir` exception
  fallback (`:40-44`) to `~/.autocompactor/pi/artifacts`.
- **`statedir.py`:** remove harness-namespacing *machinery*, keep the literal
  `~/.autocompactor/pi/` path (no migration). Keep a `state_root(*args)`-compatible
  signature through the transition so partial call sites can't fall back to Claude
  dirs. **Smoke assertion:** all smoke-run state/artifacts land under
  `~/.autocompactor/pi/`.

## 7. Phasing & verification gate

Each phase ends green (`pytest` Pi + migrated agnostic subset +
`smoke_test_pi.sh`) before the next begins.

1. **Tag** the pre-deletion commit `pre-pi-only-pivot` (recoverability insurance).
2. **Pi field-completion + `llm_digest` extraction (§2).** (a) Extract the
   LLM-digest subsystem to `llm_digest.py`, repoint `pi_bridge` import, green.
   (b) Port `assistant_text_chars` (incl. thinking), `user_prompt_chars`,
   `summary_chars` (single-source, pre-segment scan) into `pi_session_lib`;
   degrade `skill_chars`/`skill_names` to 0/[]; drop the two todo signals from
   `active_signals`. Add/adjust Pi-fed composition + signal tests. Green. *(All of
   this precedes any Claude removal so the brain and `pi_bridge` stay fed.)*
3. **Delete pure-Claude modules** (context_monitor, install + shims) and the
   **Claude-hook portion** of `precompact_analyzer` (`:172-410` + shim), plus their
   hook-contract tests. Green.
4. **Slice the Claude-JSONL front-end out of `transcript_lib`** AND
   **quarantine/remove `analyze_corpus`** + drop its `test_autocompactor.py` import
   in the *same* phase. Re-fixture remaining agnostic tests onto Pi. Green.
   *(Highest-risk phase; no renames here.)*
5. **Slim `window_resolver`, `nightly_eval`.** Green.
6. **Full flatten (§6).** Green, plus the effective-Pi-config equivalence test
   (all keys Pi reads) and the smoke state-path assertion.
7. **Docs rewrite** (§8) + `WORKLOG` entry. Commit.

## 8. Docs

- Delete `CLAUDE.md`.
- Rewrite `AGENTS.md`: single adapter; architecture table drops deleted modules,
  adds `llm_digest.py`, drops the Claude/Pi gating split; "two adapters ship" →
  "Pi is the sole adapter"; agnostic-core framing stays.
- Update `HANDOFF.md` and `README` to position a **Pi context compactor**,
  preserving the decision record (the Claude removal and the §2 Pi-field gap are
  recorded decisions; Pi skill-detection is flagged for Spec A).
- `WORKLOG.md`: dated entry — scope + why (channel-fighting → Pi-only), not what.

## 9. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Slicing the Claude front-end starves the kept brain | §2 field-completion is Phase 2, before any removal; composition tests assert Pi-fed values |
| Deleting `precompact_analyzer` breaks `pi_bridge` import | Extract `llm_digest.py` + repoint import in Phase 2a; delete only the hook portion in Phase 3 |
| `summary_chars` double-counts or misses (records sit outside the active segment) | Single-source precedence (`compactionSummary` role > `compaction` entry, never both) + explicit pre-segment scan |
| `assistant_text_chars` undercounts by dropping thinking | Port with `include_thinking=True` |
| Flatten silently reverts a Pi-read top-level key to a code default (e.g. `STALE_FRAC` 0.90→0.50) | Flatten materializes the full effective Pi config; equivalence gate snapshots *every* key Pi reads, not a named subset |
| Flatten makes Pi stop actuating / demotes a gate | `pi.*` overlay wins (MODE=actuate, Pi `OBSERVE_ONLY`); equivalence gate |
| Partial flatten writes to `~/.claude` dirs | Repoint `ART_DIR`/`STATS_DIR` fallbacks + `state_root` signature-compatible + smoke path assertion |
| pytest collection breaks when `load_transcript` is removed | Quarantine `analyze_corpus` + drop its import in the same phase (4) |
| Rename-while-deleting injects a brain bug | Behavior-preserving deletion only; no renames this spec |

## 10. Out of scope (explicit)

- Pi backtester / auto-tuning (Spec B), incl. atomic preserve-site-local-keys
  `config.local.json` writes (B is what writes config).
- **Pi skill-detection** (`skill_chars`/`skill_names` from real Pi skill
  injections) — **Spec A**. Spec 0 degrades skills to 0.
- Pi display & compaction-output enhancements (Spec A).
- Any directory/module rename (`install_pi`→`install`, `src/pi/`→`src/`,
  `~/.autocompactor/pi/`→`~/.autocompactor/`).

## 11. Success criteria

- No Claude adapter code, shims, tests, hooks, cron, or `CLAUDE.md` remain;
  `llm_digest` lives in a kept module and `pi_bridge` imports it from there.
- Pi parser populates `assistant_text_chars` (incl. thinking)/`user_prompt_chars`/
  `summary_chars` (single-source); `context_composition` reconciles on Pi inputs
  (skills may be 0); the two todo signals are gone; `subagent_done`/`commit` gates
  intact.
- `config.json` is single-namespace holding the **full effective Pi** config;
  the equivalence test (every key Pi reads) passes; `config_lib` and the TS shim
  read it identically (no nested-section / `AUTOCOMPACTOR_PI_*` path).
- All state/artifacts land under `~/.autocompactor/pi/`; no `~/.claude` fallback.
- `pytest` + `smoke_test_pi.sh` green at every phase; agnostic-brain tests
  survive, re-fixtured onto Pi inputs.
- Docs describe a single-harness Pi product; pre-deletion tag exists.
