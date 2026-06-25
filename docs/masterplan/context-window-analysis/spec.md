# Spec — Deeper analysis of what's in the context window

**Bundle:** `context-window-analysis` · **Date:** 2026-06-25 · **Complexity:** high
**Status:** brainstorm → (pending user approval) → plan

## 1. Goal

Add a **unified context inventory**: a deeper, below-the-category analysis of what
occupies the Pi context window — the fixed floor decomposed, the dynamic content
itemized, with reclaim ranking and per-item dormancy/redundancy. Build it as a
**consumer-agnostic analysis layer** that feeds both a richer **readout** and the
**compaction decision** in v1.

This continues the thread of commit `55cdfef` ("Show detailed autocompactor context
accounting", owner request a), which added *category-level* accounting
(skills / base / summary / tool / assistant / prompts). "Deeper" means going *below*
that category level — `context_composition()` already covers the categories.

## 2. Existing state (what we build on)

- `transcript_lib.context_composition()` — category breakdown + per-tool `tool_breakdown`
  + `tool_stale_frac`; wired into `build_context_state`.
- `policy.composition_detail_lines()` — renders that breakdown for Pi's contextState (55cdfef).
- `turn_profile.py` — **standalone** per-turn diagnostic: per-item token attribution,
  behavior flags (large-output, redundant-read, think-bloat, …), `reclaimable_tokens`,
  per-tool result tokens. NOT wired into the decision. Helpers `active_path()` /
  `analyze_active_prefix()` already compose over an arbitrary active prefix.
- `pi_bridge.cmd_evaluate()` — the decision:
  `recommend = (occ>=hard OR (occ>=soft AND gating_signals)) AND (context_tokens - post_floor)
  >= min_savings AND not cooldown`. **`post_floor` is a static 70000 guess.**
- `active_signals()` includes `stale_output` (≥50% of tool-output chars older than the
  window) — a medium-strength gate.
- Pre→post compaction totals are already observed (`compactionPreTokens` + post `usage.tokens`,
  "reclaimed ~X" — 55cdfef / 9fe5e37), and `nightly_eval.py` runs nightly.
- Prior **designed-but-unbuilt** bundle `simplify-compaction-model` defined a signal-strength
  table (strong/medium/observe-only) and `PolicyInput`/`PolicyDecision` shapes.

## 3. Feasibility constraints (verified)

These are hard limits the design must respect:

- **Pi `ctx` API is aggregate-only.** Used surface across all installed extensions:
  `compact`, `cwd`, `getContextUsage()` → `{tokens, contextWindow, percent}`, `hasUI`,
  `sessionManager.getSessionFile()`, `ui.notify`. **No per-item token API; no
  loaded-package/skill/message enumeration API; no pre-actuation summary-size oracle.**
- **All per-item analysis comes from parsing the session JSONL ourselves**
  (`pi_session_lib.py`); per-item token counts are **chars/4 estimates**. The aggregate
  total is exact.
- **The fixed floor's biggest bucket is tool JSON schemas**, generated at load via
  `zod-to-json-schema` — **not measurable from on-disk source**. Per the 2026-06-09
  isolation investigation: pi-subagents +11,229 (one giant subagent schema), context-mode
  +10,973 (~16 verbose tools) = 22.2k = 82% of the ~28k package cost. Context-files
  (AGENTS.md ~8k) and skills metadata (~3k) ARE markdown injected ~verbatim and chars/4 is
  accurate for them.
- `/compact` produces a **new summary of the active transcript** whose size is not knowable
  in advance from any API. Any post-compaction-floor estimate must account for this term, or
  be derived empirically from observed post-compaction totals.

## 4. The `ContextInventory` model

`context_inventory.py` (new, consumer-agnostic) emits:

```
ContextInventory
├─ total_tokens            # exact, from ctx.getContextUsage()
├─ window, occupancy
├─ floor: FloorBreakdown   # the FIXED layer (survives /compact)
│   ├─ context_files       # MEASURED live (AGENTS.md etc., chars/4 — accurate)
│   ├─ skills_meta         # MEASURED live (loaded skills metadata)
│   ├─ tools_system        # probe-cached per-package breakdown (READOUT ONLY) or
│   │                      #   honest single "tools+system (fixed)" bucket if no probe data
│   └─ true_residual       # total − attributed (honesty bucket; never hides estimate error
│                          #   into a decision input — see §6)
├─ dynamic: [ContextItem]  # per-item ledger over the ACTIVE PREFIX
│   └─ ContextItem{ kind(tool_result|assistant|user|summary), tool_name,
│                   tokens(chars/4), age_turns, last_read_turn,
│                   dormant: bool, redundant: bool, reclaimable: bool }
├─ categories: {tool, assistant, prompts, summary}   # rollup (back-compat)
└─ reclaim: ReclaimEstimate
    ├─ reclaimable_now      # dynamic items /compact would drop (advisory, chars/4)
    ├─ post_floor_estimate  # telemetry-calibrated; see §6
    └─ ranking: [(bucket, tokens, reducible_by)]   # readout advisory
```

- **Dynamic ledger** built from the active prefix (reusing `pi_session_lib.active_path()` /
  `analyze_active_prefix()` + `turn_profile`'s per-item engine). Distinction from
  `turn_profile`: turn_profile walks *all* turns for diagnostics; the inventory walks *what
  is in the window now*.
- **dormant** = older than the STALE window **and** not referenced/re-read since creation
  (the per-item proxy; "unreferenced" is heuristic — duplicate-read / textual reference, the
  only observable proxy from JSONL). **dormant is advisory + an additive gate only — see §6.**
- `context_composition()` becomes a **thin adapter** projecting the inventory back to today's
  dict, so 55cdfef's readout and all existing callers keep working (parity-tested).

## 5. Readout consumer (v1)

- Inventory-aware detail lines in `policy.py` (extending `composition_detail_lines`) for Pi's
  contextState: floor decomposed (with per-package tool-schema breakdown labeled
  `measured <date>` when probe data exists), dynamic per-item highlights, and a
  **reducible-floor advisory** ("unload pi-subagents ≈ 11k", "`--exclude-tools` context-mode
  admin tools ≈ 8k") — user-actionable guidance, since `/compact` cannot unload packages.
- A standalone `--inventory` report mode (alongside `turn_profile`'s CLI) for on-demand
  "what's in my window right now".

## 6. Decision consumer (v1) — corrected per cross-vendor adversarial review

The decision rides **exact aggregates**; per-item chars/4 estimates stay advisory and never
drive a threshold flip. Two changes only:

1. **Telemetry-calibrated `post_floor`** (replaces static 70000). Derived from the **median
   observed post-compaction total** across recent compactions. The bridge already *computes*
   pre→post per compaction (55cdfef / 9fe5e37); **persisting that series to telemetry** so the
   median can be read back is part of this work if not already present. This inherently includes the new
   `/compact` summary term — the variable a floor-sum would miss. Falls back to the static
   70000 when no telemetry history exists. Lands as a `PolicyInput` field (the seam the
   unbuilt `simplify-compaction-model` model already defines).
2. **`dormant_output` additive gating signal.** Fires on `dormant_tokens >= threshold`.
   **OR'd with the unchanged `stale_output` gate — it NEVER suppresses it.** It can only add
   compaction opportunities, never miss one `stale_output` would catch, so the
   "apparently-unused-but-referenced bulk" regression is impossible. Its own medium-tier
   entry in the signal-strength table (no hidden precedence).

**The probe-decomposed floor does NOT drive the decision** — only the live aggregate
(`total`, and the always-correct `total − measured` residual) does. This neutralizes the
probe-cache staleness risk for compaction correctness (a stale per-package number is a
readout cosmetic, never a decision flip). Estimate error is confined to the readout; it is
never the variable a near-threshold decision turns on.

## 7. The floor probe (readout-only)

- `nightly_eval.py` hosts an isolation probe (the investigation's `--no-extensions` /
  temp-`PI_CODING_AGENT_DIR` method) that measures per-package tool-schema cost and writes
  `~/.autocompactor/pi/floor-probe.json` = `{per_package: {name: tokens}, date, pi_version}`.
- The inventory reads it **only for the readout** per-package breakdown, always labeled with
  its measurement date. No probe data → the honest single "tools+system (fixed)" bucket.
- `install_pi.py --status` reports probe freshness/staleness.
- Because the probe feeds readout only, its known staleness limits (not keyed on
  enabled-tool set / `--exclude-tools` / package versions) degrade a cosmetic, not the gate.

## 8. Architecture / robustness

- `context_inventory.py` new; `context_composition()` an adapter over it.
- Shared per-item primitives move to a neutral home (`pi_session_lib`) so `turn_profile.py`
  and `context_inventory.py` reuse them with no circular import.
- **Never-raise, but VISIBLE.** Inventory build never throws; on any failure it falls back to
  today's `context_composition` AND **emits a telemetry/log event** — the fallback is
  observable, not a silent policy-regime switch. The decision then degrades to static
  `post_floor` + aggregate `stale_output` (today's behavior).

## 9. Testing

- Unit tests off existing `tests/fixtures/pi/*.jsonl`.
- Adapter-parity test: old vs new `context_composition()` output identical.
- Mocked probe-delta test for the floor probe + the no-probe fallback bucket.
- Decision tests: telemetry-calibrated `post_floor` (incl. no-telemetry fallback to 70000);
  `dormant_output` as additive-OR (assert it never suppresses a `stale_output` firing — the
  regression guard); near-threshold estimate-error cannot flip the gate.
- Visible-fallback test: a forced inventory error emits the telemetry event and the decision
  matches today's behavior.

## 10. Config

- Inventory enable flag.
- Dormancy age + token thresholds.
- Probe staleness window.
- `post_floor` calibration window (how many recent compactions feed the median) + the static
  fallback (70000).

## 11. Scope

**In v1:** the inventory layer; the readout (contextState lines + `--inventory` report +
reducible-floor advisory); the readout-only floor probe; the two decision corrections
(telemetry-calibrated `post_floor`, additive `dormant_output`).

**Out of v1 (→ v2):** keep/drop instructions to `prepare`/`reinject` (per-item dormancy/
redundancy's natural consumer — the inventory is built decision-ready for it).

## 12. Review record

Cross-vendor adversarial review (gpt-5.5 via skynet `dispatch-adversary`, xhigh, 2026-06-25)
returned REVISE with 4 must-fixes; all folded in above:
1. `post_floor` must model the new `/compact` summary → telemetry-calibrated (§6.1).
2. Probe floor must not drive the gate without live validation → probe is readout-only (§6, §7).
3. `dormant_output` must not suppress `stale_output` → additive-OR (§6.2).
4. Estimate error must not flip near-threshold decisions → decision rides exact aggregates;
   chars/4 advisory only; visible (not silent) fallback (§6, §8).
