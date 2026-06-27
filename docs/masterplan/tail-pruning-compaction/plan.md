# Tail-Pruning Compaction — Plan

**Spec:** `docs/masterplan/tail-pruning-compaction/spec.md`
**Date:** 2026-06-26
**Two-repo plan:** Part A lands in upstream Pi; Part B lands in autocompactor.

## Part A — Pi primitive (upstream `github.com/earendil-works/pi`, `packages/coding-agent`)

> Part A is blocked on access to the Pi source repo. The published npm
> package (`@earendil-works/pi-coding-agent@0.80.2`) is the only artifact
> available locally; the source lives in the closed upstream repo. Tasks A1–A4
> are the design against the confirmed public API surface
> (`dist/core/extensions/types.d.ts`, `dist/core/session-manager.d.ts`,
> `docs/compaction.md`). Implementation requires a contributor with push
> access to the upstream repo.

### A1. `SessionManager.editEntries` + `editEntriesRevisions`
- New `editEntries(edits)` (no-revision path) + `editEntriesRevisions(edits, expectedRevision)` (atomic TOCTOU-safe path, re-review [P1]) on the full `SessionManager`.
- `_editEntries` / `_editEntriesRevisions` do the JSONL rewrite; the revision path uses **temp-file + atomic rename under the session lock** (NOT append-only `_persist`) for crash/ENOSPC safety with rollback (re-review round 15 [P1]). The revision check (entry count + prefix-tail id + per-target content hash + per-proof-source content hash + per-interval content hash) AND the rewrite happen under the same session lock, all-or-nothing.
- Preserves `type`/`id`/`parentId`/`timestamp`/call-result pairing.
- **Branch isolation (re-review [P1]):** refuses entries reachable from non-active branches (caller passes active leaf; Pi walks tree, rejects cross-branch refs), OR copy-on-write before stubbing. Default: refuse-on-cross-branch.
- **Sparse edits within a contiguous cache-break span allowed (re-review round 14 [P1]):** the contiguous tail span is the cache-break SPAN; actual edits within it may be SPARSE (only `is_cruft` entries edited, live-between verbatim).
- Documents the cache-break rule: edits invalidate KV/prefix cache from the first edited entry onward.

### A2. ExtensionContext: `editEntries` + `editEntriesRevisions` actions
- Both exposed on `ExtensionContext` (base, NOT command-only — reachable from `agent_end`).
- Prune path uses `editEntriesRevisions`; `editEntries` (no-revision) stays for non-prune callers.
- `ReadonlySessionManager` stays read-only (no `editEntries` on it).

### A3. `session_after_edit` event
- New event type carrying content-free metadata: `editedIds`, `editedCount`, `prunedTokensEstimate`, `firstEditedId`, `editedSpan`, `extensionId`.

### A4. Docs: "Cache-preserving tail-pruning" section in `docs/compaction.md`

**Verification (Part A):** Pi's own extension test suite + a new test that edits a tail span and asserts the prefix is unchanged. Published as a Pi release.

## Part B — autocompactor consumer (`/srv/dev/ras/autocompactor`)

### B1. Contiguous tail-cruft suffix detection (decision-safe)
- **File:** `src/autocompactor/context_inventory.py`
- New `build_tail_cruft_suffix(active_prefix, total, keep_recent_tokens)` returns a **contiguous suffix** (not a sparse top-N): earliest `prune_safe` item through tail end, including live-between items **only to preserve suffix ordering / the cache-break span — NEVER charged in the net-savings calc, NEVER edited** (re-review round 6+7 [P1]).
- `prune_safe` proof (re-review [P1] — strict full-content, not weak): content hash match against a later entry (verbatim duplicate) OR strict-substring of a later tool_result for the **same tool/path** (subsumption), with NO intervening write to that path. **Both the duplicate AND the subsumer MUST be from the same tool/path** (re-review round 19 [P2]: two different commands producing identical bytes do NOT make the earlier `prune_safe` — the only remaining full content could belong to a different command, losing path-specific evidence). **Read-then-edit-then-read is explicitly rejected** (older read holds pre-edit bytes). Persisted-to-artifacts alone is NOT sufficient for tool results (`artifacts.extract` stores only prompts/corrections/errors/commands/hex/paths, not arbitrary tool bodies). Age/size-only `dormant` is surfaced in the report but NOT stubbed.
- **Net-savings gate (cache-rebuild-aware, re-review round 9+18 [P1/P2]):** suffix returned only when `sum(cruft) − stub_overhead − downstream_live_tail_rebuild_tokens >= PRUNE_MIN_RECLAIM`. Editing the first cruft entry invalidates KV/prefix cache for every later entry on the next turn, so a small cruft before a large live tail must charge that downstream live-tail rebuild cost or be rejected. Live-between/live-tail tokens are NOT reclaimed and NOT edited, but ARE charged through the rebuild term. Else empty → caller falls through to no-recommendation (below hard line) or `compact()` (at/above hard line).
- Output: `[{ entry_id, tool_name, tokens, reason, is_cruft }]` in entry order.

### B2. Prune trigger + recommendation kind + hard-line fall-through + PRUNE_MODE
- **Files:** `config.json`, `src/autocompactor/pi_bridge.py`
- Add to `config.json`: `PRUNE_MODE: "advise"` (default, gates ONLY prune — separate from `MODE` which governs `compact()` fallback), `PRUNE_EVAL_TIERS`, `PRUNE_MIN_RECLAIM`, `PRUNE_MAX_STUB_CHARS`, `PRUNE_COOLDOWN_TOKENS`, `PRUNE_SAFE_TARGET` (per-tier). (Re-review [P1].)
- `cmd_evaluate`: when `context_tokens >= PRUNE_EVAL_TIERS[tier]`, call `build_tail_cruft_suffix`. The verdict carries `evaluate_revision` (entry_count + target_hashes + proof_hashes + interval_hashes + prefix_tail_id — all five, re-review round 7+9 [P1]) as the baseline for `editEntriesRevisions`'s atomic stale-race check INSIDE Pi's session lock (NOT compared by `prepare_prune`, which is backup-only — re-review round 3 [P1]). Decision table:
  - **Below hard line** + cruft net >= PRUNE_MIN_RECLAIM AND post-prune est. <= PRUNE_SAFE_TARGET[tier] → `kind: "prune"` + `evaluate_revision`.
  - **Below hard line** + cruft net >= PRUNE_MIN_RECLAIM AND post-prune est. > PRUNE_SAFE_TARGET[tier] → `kind: "prune"` (still cache-preserving; compact is hard-line-only; re-review round 9 [P2]).
  - **Below hard line** + cruft net < PRUNE_MIN_RECLAIM → **no recommendation** (do NOT compact at the low threshold — would bust cache for no reason; re-review round 3 [P1]).
  - **At/above hard line** + cruft net >= PRUNE_MIN_RECLAIM AND post-prune est. <= PRUNE_SAFE_TARGET[tier] → `kind: "prune"` + `evaluate_revision` — BUT if `PRUNE_MODE != "actuate"` OR `editEntriesRevisions` unavailable, fall back to `kind: "compact"` so the hard-wall safety is never regressed by an advise-only prune rollout (re-review round 10 [P1]).
  - **At/above hard line** + cruft net >= PRUNE_MIN_RECLAIM AND post-prune est. > PRUNE_SAFE_TARGET[tier] → `kind: "compact"`.
  - **At/above hard line** + cruft net < PRUNE_MIN_RECLAIM → `kind: "compact"`.
- The `compact` recommendation (summarizing fallback) fires ONLY at/above the hard line; the low-threshold no-cruft case recommends nothing.

### B3. TS shim executes the prune (atomic revision-checked edit)
- **File:** `src/pi/autocompactor.ts`, `src/autocompactor/pi_bridge.py` (new `cmd_prepare_prune`, `cmd_clear_prune_cooldown`, `cmd_mark_prune_unavailable`)
- `agent_end` handler: on a `prune` verdict:
  0. **PRUNE_MODE gate (re-review round 14+17 [P1]):** destructive prune requires `PRUNE_MODE === "actuate"` AND `typeof ctx.editEntriesRevisions === "function"`. Otherwise surface-only advisory (no `prepare_prune`, no edit) AND `clear_prune_cooldown` (roll back evaluate-time cooldown so next eval isn't suppressed). At/above hard line in advise-only, surface compact advisory if `MODE === "advise"` or invoke `safeCompact()` only if `MODE === "actuate"` (re-review round 16 [P1]) — NEVER call `safeCompact()` unconditionally.
  1. Call `bridge("prepare_prune", --targets, --revision <verdict.evaluate_revision>)` — backs up the session JSONL to `statedir/backups/<id>-<ts>-prune.jsonl` ONLY (pre-edit snapshot for rollback). **Backup-only, NOT a stale-check** — running the revision comparison here would recreate the TOCTOU gap (re-review round 3 [P1]). Returns `{ok, backup_path}`.
     - If `!prep.ok` (backup failed) → **`clear_prune_cooldown` FIRST (re-review round 18 [P2])** so below-hard backup failures also roll back cooldown, then abort+announce. **At/above hard line, mark prune unavailable + `safeCompact()` ONLY if `MODE === "actuate"` (else compact advisory)** so the hard-wall safety path is never stranded by backup failure (re-review round 13+16 [P1]).
  2. Build `edits` from `is_cruft` targets in the contiguous suffix (live-between items NOT edited — stay verbatim).
  3. Call `ctx.editEntriesRevisions(edits, verdict.evaluate_revision)` — atomic verify+rewrite under Pi's session lock, all-or-nothing (re-review round 2 [P1]).
     - If `!result.ok || result.mismatched.length || result.skipped.length` → failed prune: `clear_prune_cooldown` (re-review round 16 [P1]), do NOT report success, do NOT hide compact fallback. Announce the failure.
     - **HARD-LINE FAILURE FALLBACK (re-review round 12+16 [P1]):** if `verdict.kind === "prune" && verdict.at_hard_line`, mark prune unavailable for this session (sticky bridge state flag, cleared on next successful compact or Pi restart) + `clear_prune_cooldown` + invoke `safeCompact()` ONLY if `MODE === "actuate"` (else compact advisory) — so a deterministic edit failure (e.g. cross-branch skipped targets) does NOT loop forever and the hard-wall safety path is never stranded. Below the hard line, a failed prune just defers to the next evaluation (no compact).
  4. Only on `ok && edited.length === edits.length` → surface the per-prune report (B4).
- Graceful degradation: `if (typeof ctx.editEntriesRevisions !== "function")`
  → fall back to advise-only (no actuate), log a `bridge_missing_primitive`
  telemetry row. The check is on `editEntriesRevisions` specifically — NOT the
  non-revision `editEntries` — because every prune calls the revision-safe
  path; a Pi version with only the non-revision `editEntries` is treated as
  missing the primitive (no actuate), undermining the TOCTOU fix if we fell
  back (re-review round 6+7 [P2]).

### B4. Per-prune report
- **Files:** `src/pi/autocompactor.ts` (chat), `src/autocompactor/stats.py`/bridge (telemetry)
- Small chat line: `autocompactor: tail-pruned 7.3k tokens (4 entries) without summarizing … prefix cache preserved; compaction deferred.`
- Telemetry event `prune_report` (content-free): counts, tool names, reasons, token estimate, before/after.

### B5. Tests
- **File:** `tests/test_pi_bridge.py`, `src/pi/test/extension.test.mjs`
- Detect: `build_tail_cruft_suffix` returns a contiguous suffix (not sparse); live-between items carried with `is_cruft: false` **only to preserve suffix ordering / cache-break span — NOT reclaimed, NOT edited, but charged via `downstream_live_tail_rebuild_tokens`** (re-review round 18+19 [P2]); net-savings gate is **`sum(cruft) − stub_overhead − downstream_live_tail_rebuild_tokens >= PRUNE_MIN_RECLAIM`** (re-review round 9+11 [P1]) — include a rejection fixture where small cruft precedes a large live tail and the net is negative.
- `prune_safe` proof: age/size-only `dormant` items surfaced but NOT in the edit list; verbatim-duplicate AND strict-substring items are `is_cruft: true` ONLY when the duplicate/subsumer is from the **same tool/path** (re-review round 19 [P2]); **read-then-edit-then-read is NOT prune_safe** (older read rejected even though "same path read again later"); persisted-to-artifacts alone does NOT make a tool result `prune_safe`.
- Recommend: at 100k on 1M (BELOW hard line) with a net-positive contiguous suffix and post-prune est. <= PRUNE_SAFE_TARGET → `kind: "prune"` AND verdict carries `evaluate_revision` AND each prune_target carries `is_cruft`; **with the same cruft but post-prune est. > PRUNE_SAFE_TARGET (still below hard line) → `kind: "prune"`** (compact is hard-line-only; re-review round 9 [P2]); **with <5k net cruft below the hard line → no recommendation (NOT compact)** (re-review round 4 [P1]); at/above hard line with prune-safe cruft AND `PRUNE_MODE=actuate` AND primitive present → `kind: "prune"`; **at/above hard line with prune-safe cruft but `PRUNE_MODE=advise` OR primitive missing → `kind: "compact"`** so hard-wall safety never regressed (re-review round 10 [P1]); with <5k net cruft AT/above the hard line → `kind: "compact"`.
- Shim: `prune` verdict calls `prepare_prune` (backup only) THEN `ctx.editEntriesRevisions` (atomic verify+rewrite under session lock); `evaluate_revision` includes **`target_hashes` AND `proof_hashes` AND `interval_hashes`** (every proof-source entry AND every intervening entry in the no-write proof interval is hashed and validated — a proof-source mutation OR an intervening-entry change-to-write between eval and edit would otherwise leave target_hashes unchanged while stubbing the only live copy; re-review round 9+11 [P1]); a revision/proof/interval mismatch OR partial edit (`!ok`/`mismatched`/`skipped`) aborts the prune with NO edit applied, NO success report, `clear_prune_cooldown`, and at hard line falls back to `safeCompact()` ONLY if `MODE === "actuate"` (else compact advisory); `compact` verdict never calls `editEntries`/`editEntriesRevisions`; missing `editEntriesRevisions` degrades gracefully.
- `PRUNE_MODE` + `MODE` matrix: `PRUNE_MODE=advise` gates prune to surface-only (with `clear_prune_cooldown`); `MODE=actuate` does NOT actuate prune (only compact fallback); `PRUNE_MODE=actuate, MODE=advise` surfaces prune edits but compact stays advisory; below-hard backup failure rolls back cooldown. Test all combinations.
- Telemetry: `prune_report` event has NO message content (content-free assertion).

### B6. Live gating
- Ship with `PRUNE_MODE=advise` initially (surfacing recommendations without acting).
- Flip `PRUNE_MODE` to actuate after ≥1 day of clean prune telemetry (matches the prior flip-to-actuate discipline from `mem:` records). `MODE` continues to govern `compact()` throughout.

## Execution strategy
- Part A is the gate. Spec A is design-ready; implementation requires upstream repo access.
- Part B can be developed in parallel against the Part A spec, with the graceful-degradation guard, but cannot actuate until Part A is published.
- The old `sooner-firing-and-reports` spec is archived; this plan replaces it entirely.