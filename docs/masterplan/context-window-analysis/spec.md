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

**Safety guarantee rides exact aggregates; estimates only modulate *opportunistic* compaction.**
The hard-line trigger is `occupancy = context_tokens / window` — authoritative provider usage,
**not** chars/4 — and Pi's native ceiling autocompact is the exact backstop. Estimate-dependent
inputs (`min_savings`, `dormant_output`, the soft gate) only modulate compaction within the
**soft→hard band**; a bounded chars/4 error there causes at most a slightly-early/late
*opportunistic* compaction, never a missed safety compaction. Estimate noise is further damped by
the existing cooldown. (This replaces the earlier, false claim that estimates "never drive a
threshold flip" — they can, but only inside the band, and only in the bounded/additive direction.)
Two changes plus one guard correction:

1. **Config-aware `post_floor`** (replaces static 70000). The current session's fixed floor is
   knowable live: `base = total − measured` is a residual of the **exact** total, so it already
   reflects *whatever* tool schemas/packages are loaded **now** — no telemetry, no probe, no
   staleness. So:
   `post_floor = live_fixed_floor (base + persistent skills, this session) + summary_term`,
   where `summary_term` = telemetry median of historical `post_total − (base + skills)` — the
   **summary size only**, which is far more config-stable than the whole post-total (the variable
   the stale-median bug turned on). The bridge already *computes* pre→post per compaction
   (55cdfef / 9fe5e37); **persisting `post_total`, `base`, `skills` per compaction** so the
   summary-term median can be read back is part of this work. Static 70000 only when no telemetry
   history exists. Lands as a `PolicyInput` field (the `simplify-compaction-model` seam).
   `savings = total − post_floor = dynamic_reclaimable − expected_new_summary` — intuitive and
   config-correct.

   **Guard correction:** `min_savings` is **not applied at/above the hard line** (a hard-line
   compaction always proceeds). `min_savings` guards only the opportunistic soft-band trigger —
   its purpose is to avoid churny tiny compactions, not to let an estimate suppress a needed hard
   compaction. (Behavior change from today, where `min_savings` AND-gates the hard trigger too.)
2. **`dormant_output` additive gating signal.** Fires on `dormant_tokens >= threshold`.
   **OR'd with the unchanged `stale_output` gate — it NEVER suppresses it.** It can only add
   compaction opportunities, never miss one `stale_output` would catch, so the
   "apparently-unused-but-referenced bulk" regression is impossible. Its own medium-tier
   entry in the signal-strength table (no hidden precedence). `dormant_tokens` is a chars/4
   estimate, so this is an **estimate-based opportunistic gate** — acceptable precisely because
   it is additive (a false positive triggers at most an *extra* soft-band compaction, never
   suppresses a needed one) and band-limited (the exact hard line is unaffected). A **deadband /
   hysteresis** on the threshold damps estimate-noise churn. *(If even bounded-additive estimate
   influence on the gate is undesirable, the fallback is to make dormancy readout-only in v1 and
   defer the gate to v2 — see §11; this spec keeps it as an additive gate per the chosen design.)*

**The probe-decomposed floor does NOT drive the decision** — only the live aggregate `total`
and the live `base = total − measured` residual do. So a stale per-package probe number is a
readout cosmetic, never a decision flip. Where estimates *do* enter the decision (the chars/4
in `base`, `dynamic_reclaimable`, and `dormant_tokens`), their influence is **bounded to the
soft→hard band**: the exact hard line plus the native ceiling guarantee the safety compaction
regardless, `min_savings` no longer gates the hard line, and `dormant_output` is additive-only.
That is the honest, provable property — not "estimates never matter."

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
  observable, not a silent policy-regime switch.
- **Fallback swaps INPUTS, never the policy formula.** On inventory failure the decision uses
  *degraded inputs* — static `post_floor` + aggregate `stale_output` — but **still runs the
  corrected policy**: the hard line is never gated by `min_savings` (§6.1). It must NOT revert to
  the pre-fix formula (which AND-gated the hard trigger with `min_savings`) — that would
  reintroduce the suppress-at-hard bug via the never-raise path.

## 9. Testing

- Unit tests off existing `tests/fixtures/pi/*.jsonl`.
- Adapter-parity test: old vs new `context_composition()` output identical.
- Mocked probe-delta test for the floor probe + the no-probe fallback bucket.
- Decision tests: config-aware `post_floor = base + skills + summary_term` (uses the live
  residual `base`, not a stale total median — assert it tracks a changed fixed floor; incl.
  no-telemetry fallback to 70000); `min_savings` **not applied at/above the hard line** (a
  hard-line compaction fires even when estimated savings < min_savings — the
  estimate-can't-suppress-safety guard); `dormant_output` additive-OR (never suppresses a
  `stale_output` firing); dormancy threshold deadband/hysteresis damps noise. The provable
  property under test is **band-limited**: the exact hard line + native ceiling always compact;
  estimate error only shifts opportunistic soft-band timing.
- Visible-fallback test: a forced inventory error emits the telemetry event and degrades to
  **static inputs while keeping the corrected policy** — assert that with occupancy ≥ hard and
  estimated savings < `min_savings`, compaction is STILL recommended (the hard line is not
  gated by `min_savings` even on the fallback path).

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

**Pass 1** (gpt-5.5 via skynet `dispatch-adversary`, xhigh, 2026-06-25) — REVISE, 4 must-fixes:
1. `post_floor` must model the new `/compact` summary → §6.1.
2. Probe floor must not drive the gate without live validation → probe is readout-only (§6, §7).
3. `dormant_output` must not suppress `stale_output` → additive-OR (§6.2).
4. Estimate error must not flip near-threshold decisions → §6, §8.

**Pass 2** (same lane, on the revised spec) — REVISE; two findings folded in:
- Must-fix #4 was only cosmetic: `dormant_output` fires on a chars/4 `dormant_tokens`, so the
  estimate *is* policy-active. Resolution: dropped the false "estimates never flip" claim;
  adopted the honest **band-limited** property (exact hard line + native ceiling guarantee the
  safety compaction; estimates only shift opportunistic soft-band timing; `dormant_output`
  additive-only with a deadband) — §6, §6.2.
- New: telemetry "median post-compaction **total**" is a stale decision cache across different
  floor configs (floor can swing ~22k). Resolution: `post_floor` uses the **live** residual
  `base` (config-correct for this session) + a telemetry **summary-term** only; and `min_savings`
  no longer gates the hard line — §6.1.

**Pass 3** (same lane, on the twice-hardened spec) — REVISE, one small must-fix (all pass-2
fixes verified holding):
- The never-raise fallback (§8/§9) said it "matches today's behavior," which would reintroduce
  the suppress-at-hard bug (today's formula AND-gated the hard line with `min_savings`).
  Resolution: fallback swaps **inputs only** (static `post_floor` + aggregate `stale_output`),
  never the policy formula — the corrected hard-line rule always applies; the visible-fallback
  test asserts compaction at hard even when estimated savings < `min_savings` (§8, §9).
- Residual risks to monitor in implementation (not blockers): chars/4 soft-band noise;
  summary-term drift by workload/model; cooldown semantics at the hard line.
