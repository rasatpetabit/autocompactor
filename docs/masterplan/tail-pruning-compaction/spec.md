# Tail-Pruning Compaction — Pi primitive + autocompactor consumer

**Date:** 2026-06-26
**Complexity:** high
**Status:** spec

This masterplan supersedes `sooner-firing-and-reports` (archived to
`docs/masterplan/_superseded-sooner-firing-and-reports/`). The prior spec was
built on the wrong mental model — size-triggered *summarizing* compaction that
rewrites the prefix and invalidates the KV/prefix cache. This plan inverts it:
keep the cached prefix intact while there is live value in the context, and
routinely replace cruft accumulating near the **tail** with inline stubs so the
prefix cache stays valid downstream.

## Problem

Pi compaction today walks *backwards* from the newest message, keeps a recent
window (`keepRecentTokens`, default 20k), and summarizes everything older by
inserting a `CompactionEntry`. That entry rewrites the prefix the model cached,
so every downstream token is a cache miss until the new prefix is re-read. The
cost of freeing space is therefore the cache budget itself — the opposite of
cheap.

The owner's mental model (which this plan implements):

- Context is allowed to run **high when it is earning its keep**. A high
  context full of live, still-referenced material is *good* — the prefix cache
  is doing work.
- We prune only **stretches that have no remaining value**: stale tool output,
  redundant results, already-consumed reads — accumulating near the *tail*
  (most recent) of the window, where deletion/edit does not invalidate anything
  upstream.
- "Fires at 100k" means "begins *evaluating the tail for prunable cruft* at
  100k," not "force a full compaction at 100k."
- The primitive must preserve the prefix cache. **The prune frontier is a
  contiguous tail suffix**, not a sparse set of high-token items: KV/prefix
  cache validity ends at the FIRST edited entry, so a sparse edit forces the
  model to re-read every later (live) entry. We therefore define the prune
  frontier (cache-break span) as a contiguous suffix of the tail, but **only the
  `is_cruft` items within that span are edited** — live-between items are
  left verbatim and never stubbed (the cache-break point is the first cruft
  edit; live content is never replaced). A prune is only taken when the **net**
  savings clears `PRUNE_MIN_RECLAIM`, where the net is **cache-rebuild-aware**:
  `cruft tokens − stub_overhead − downstream_live_tail_rebuild_tokens` (the
  tokens of live entries after the first cruft edit that will be re-read next
  turn, since editing the first cruft entry invalidates KV/prefix cache for
  every later entry). A small cruft before a large live tail therefore does
  NOT pass the gate. If the net is negative, we fall through to
  no-recommendation (below hard line) or the summarizing fallback (at/above
  hard line). (Re-review round 12 [P1].)
- Stubs are content-free: `[pruned: N tokens of stale bash output]`. The
  original content is NOT lost — a pre-prune backup is taken before any edit
  (see B3), so a misclassification is recoverable.

## The blocker: Pi exposes no edit/delete primitive

Confirmed against the installed `@earendil-works/pi-coding-agent@0.80.2`
`.d.ts` surface (`dist/core/extensions/types.d.ts`,
`dist/core/session-manager.d.ts`) and `docs/compaction.md`:

- `ExtensionContext` exposes only: `appendEntry`, `sendMessage`,
  `sendUserMessage`, `compact(options)`, `getContextUsage`, `sessionManager`
  (read-only), `getSystemPrompt`, `isIdle`, `abort`, `signal`,
  `hasPendingMessages`, `shutdown`.
- `ReadonlySessionManager` is literally `Pick<SessionManager,
  "getCwd"|"getSessionDir"|"getSessionId"|"getSessionFile"|"getLeafId"|
  "getLeafEntry"|"getEntry"|"getLabel"|"getBranch"|"getHeader"|"getEntries"|
  "getTree"|"getSessionName">` — read methods only.
- The full `SessionManager` class has `_appendEntry`, `_persist`, and the
  `append*` family. There is **no** `deleteEntry`, `removeEntry`,
  `editEntry`, `replaceEntry`, `updateEntry`, `truncate`, or any in-place
  content mutation method — public or private.
- `session_before_compact` can return a custom `{ compaction: { summary,
  firstKeptEntryId, ... } }`, but Pi still inserts a `CompactionEntry` that
  rewrites whatever precedes `firstKeptEntryId`. Even "custom compaction"
  rewrites the prefix; it cannot *keep the prefix verbatim* and edit only tail
  entries.

So the inline-stub model is not implementable on the current Pi surface. This
masterplan is therefore **two repos**: Pi gains a new primitive (Part A), then
autocompactor consumes it (Part B). Part A is gated by the upstream Pi repo
(the published npm package is the only artifact here; the source lives at
`github.com/earendil-works/pi`, `packages/coding-agent`).

## Decision

- **Part A — Pi (upstream):** add a `editEntries` primitive to
  `SessionManager` and expose it (read-safe subset) on `ExtensionContext`, plus
  a new `session_after_edit` event for telemetry. In-place content replacement,
  no new `CompactionEntry`, prefix cache preserved.
- **Part B — autocompactor (consumer):** a tail-cruft detector that fires at a
  low threshold (100k on 1M models), identifies prunable entries near the tail
  via the existing ContextInventory, and calls `editEntries` to replace each
  with a content-free inline stub. Per-prune report (what was stubbed, how many
  tokens freed) surfaced in chat + telemetry.
- **Keep `compact()` as the rare fallback:** when ContextInventory reports *no*
  prunable cruft but context is genuinely at the hard wall (HARD_PCT_WIDE), the
  existing summarizing compaction runs. The cache-bust is acceptable *only* when
  there is no cache-preserving alternative.

## Part A — Pi primitive (upstream)

### A1. SessionManager: `editEntries`

New method on `SessionManager` (and a private `_editEntries`):

```typescript
interface EntryEdit {
  id: string;                 // SessionEntry.id to edit
  /** Replacement content. For a message entry, replaces the message content
   *  blocks. For a tool result, replaces the tool_result content blocks.
   *  null means "stub this entry" — Pi replaces content with a fixed stub
   *  string derived from entryType + prunedTokenCount. */
  content: AgentMessage["content"] | string | null;
  /** Mark this entry as pruned so subsequent reads can skip/flag it. */
  pruned?: boolean;
  /** Optional small content-free note recorded in the entry for audit. */
  note?: string;
}

class SessionManager {
  /** Edit existing entries in place. Idempotent: unknown ids are skipped.
   *  Cache semantics: edits invalidate the KV/prefix cache from the FIRST
   *  edited entry onward — callers MUST edit a contiguous tail span to keep
   *  the cache break point at the tail, not mid-window. */
  editEntries(edits: EntryEdit[]): { edited: string[]; skipped: string[] };
  /** Atomic expected-revision edit (TOCTOU-safe). Verifies the session has
   *  not changed since the caller's evaluate time AND that every target id
   *  is present + non-cross-branch, all under the same session lock as the
   *  rewrite — then performs the edit all-or-nothing. `expectedRevision`
   *  mismatch (entry count, prefix-tail id, ANY target content hash, ANY
   *  proof-source content hash, OR ANY intervening-interval content hash
   *  differs — re-review round 9+13 [P1]) -> NO edit happens and `mismatched`
   *  lists the offenders. If any target is unknown or cross-branch-reachable,
   *  NO edit happens and `skipped` lists it. A prune is only "successful" when
   *  `edited.length === edits.length` AND `mismatched.length === 0` AND
   *  `skipped.length === 0`. This closes the re-review [P1] TOCTOU/partial-edit
   *  gap and the round-6+9 proof/interval mutation holes. */
  editEntriesRevisions(
    edits: EntryEdit[],
    expectedRevision: { entry_count: number; prefix_tail_id: string;
                         target_hashes: Record<string, string>;
                         proof_hashes: Record<string, string>;
                         interval_hashes: Record<string, string> }
  ): { edited: string[]; skipped: string[]; mismatched: string[];
       ok: boolean };
  private _editEntries(edits: EntryEdit[]): { edited: string[]; skipped: string[] };
  private _editEntriesRevisions(
    edits: EntryEdit[], expectedRevision: ...
  ): { edited: string[]; skipped: string[]; mismatched: string[]; ok: boolean };
}
```

**Constraints (documented, not type-enforced):**

- Callers edit a **contiguous tail span** as the cache-break SPAN, but the
  actual edits within that span may be SPARSE: only `is_cruft` entries are
  edited, live-between entries are left verbatim. The cache-break point is the
  first edited (`is_cruft`) entry; live entries between cruft items are NOT
  edited and NOT stubbed. (Re-review round 14 [P1]: a contract that required
  editing every entry in the contiguous span would force stubbing live content.)
- Editing preserves `type`, `id`, `parentId`, `timestamp`, and the
  call/result pairing (a stub on a tool_result keeps the parent tool_call id).
- Editing preserves `type`, `id`, `parentId`, `timestamp`, and the
  call/result pairing (a stub on a tool_result keeps the parent tool_call id).
- The edit rewrites the JSONL entry via a **temp-file + atomic rename under
  the session lock** (NOT Pi's append-only `_persist` path, which would leave
  duplicate/original rows or a partially-written session on crash/ENOSPC).
  `_editEntriesRevisions` writes the new entry content to a temp file, fsyncs,
  then renames over the live JSONL inside the session lock; on any error the
  temp is unlinked and the live file is untouched (rollback). This is the
  crash-safe destructive-edit analog of `_persist`'s append-only guarantee.
  (Re-review round 15 [P1].)
- **Atomic expected-revision edit (TOCTOU-safe):** the destructive prune
  path MUST use `editEntriesRevisions(edits, expectedRevision)` — the revision
  check (entry count + prefix-tail id + per-target content hash) and the
  rewrite happen under the same session lock, all-or-nothing. The separate
  `prepare_prune` backup stays as the pre-edit JSONL snapshot for rollback, but
  it is NOT the race guard — `editEntriesRevisions` is. A partial edit (some
  targets skipped as unknown/cross-branch) or a revision mismatch yields `ok:
  false` with NO edits applied. The shim treats `!ok` as a failed prune
  (reports the failure, does NOT consume cooldown, does NOT report success,
  falls through to the next evaluation). This closes the re-review [P1] finding.
- **Branch isolation (non-active-branch safety):** a Pi session may have
  multiple branches/leaves sharing an ancestor entry. Preserving `id`/`parentId`
  while rewriting content in place would mutate that shared ancestor for EVERY
  branch referencing it, not just the active path — "scoped active session" does
  not create branch isolation. `editEntries` therefore MUST refuse any entry that
  is reachable from a non-active branch (the caller passes the active leaf;
  Pi walks the tree and rejects entries with cross-branch references), OR
  perform copy-on-write (clone the entry to a new id scoped to the active
  branch, leaving the shared ancestor untouched) before stubbing. The
  adversarial review's [P1] finding. The default implementation is the
  refuse-on-cross-branch path; copy-on-write is the more flexible fallback.
- `ReadonlySessionManager` does **not** gain `editEntries` (read-only stays
  read-only). `ExtensionContext` gains a *separate* `editEntries` action that
  delegates to the real `SessionManager`, scoped to the active session.

### A2. ExtensionContext: `editEntries` + `editEntriesRevisions` actions

```typescript
export interface ExtensionContext {
  // ... existing ...
  /** Edit existing session entries in place (cache-preserving tail-prune).
   *  Edits a contiguous tail span; edits outside the tail are rejected with
   *  an error logged to the extension. No-revision path for non-prune callers. */
  editEntries(edits: EntryEdit[]): { edited: string[]; skipped: string[] };
  /** Atomic expected-revision edit (TOCTOU-safe, re-review [P1]): revision
   *  check (entry count + prefix-tail id + per-target content hash + per
   *  proof-source content hash) AND the rewrite happen under the SAME session
   *  lock, all-or-nothing. Partial edit (target unknown/cross-branch) or
   *  revision/proof mismatch -> {ok:false}, NO edits applied. The destructive
   *  prune path MUST use this. */
  editEntriesRevisions(
    edits: EntryEdit[],
    expectedRevision: { entry_count: number; prefix_tail_id: string;
                         target_hashes: Record<string, string>;
                         proof_hashes: Record<string, string>;
                         interval_hashes: Record<string, string> }
  ): { edited: string[]; skipped: string[]; mismatched: string[]; ok: boolean };
}
```

Both actions are available from `agent_end` handlers (base `ExtensionContext`,
not command-only). The prune path uses `editEntriesRevisions`; `editEntries`
(no-revision) stays for non-prune callers.

### A3. `session_after_edit` event (telemetry)

A new event fired after `editEntries` runs, carrying content-free metadata:

```typescript
interface SessionAfterEditEvent {
  type: "session_after_edit";
  editedIds: string[];          // entry ids edited
  editedCount: number;
  prunedTokensEstimate: number; // caller-supplied estimate (chars/4)
  firstEditedId: string;         // cache-break anchor
  editedSpan: { start: number; end: number }; // entry indices
  extensionId?: string;
}
```

No content, no message text, no ids beyond the entry ids themselves. This is
the durable audit trail for tail-prunes.

### A4. Cache-break documentation

`docs/compaction.md` gains a new section "Cache-preserving tail-pruning"
documenting:

- The KV/prefix cache is valid up to the first edited entry. Editing a
  contiguous tail span keeps the break point at the tail.
- `editEntries` is the cache-preserving alternative to `compact()`; the
  summarizer (`compact()` / `session_before_compact`) rewrites the prefix and
  is the cache-busting fallback, reserved for when no prunable cruft exists.
- The "fires at N" semantic: a low threshold triggers *cruft evaluation*, not
  forced summarization.

## Part B — autocompactor consumer

### B1. Detection: contiguous tail-cruft suffix

The existing `ContextInventory` (`src/autocompactor/context_inventory.py`)
classifies dynamic items with `dormant`/`redundant`/`reclaimable` flags and a
`reclaim.ranking`. The consumer reuses it but with two corrections to the
adversarial-review findings (sparse-edits cache break + advisory-flag safety):

- **Contiguous suffix, not a sparse top-N.** New
  `build_tail_cruft_suffix(active_prefix, total, keep_recent_tokens)` in
  `context_inventory.py` returns a *contiguous suffix* of the tail: the
  earliest `prune_safe` item through the tail end, including any live items
  in between. KV/prefix cache validity ends at the first edited entry, so a
  sparse edit would force re-read of every later live entry — the contiguous
  suffix puts the single cache-break point at the start of the suffix. Output:
  `[{ entry_id, tool_name, tokens, reason, is_cruft }]` in entry order, where
  `is_cruft` marks the reclaimable items and the live-between items are carried
  **only to preserve suffix ordering / the cache-break span — they are NEVER
  charged in the net-savings calculation and NEVER edited** (re-review round 6
  [P1]).
- **Decision-safe eligibility, not advisory flags.** The existing
  `dormant`/`redundant`/`reclaimable` flags are readout-level advisory only —
  they are NOT promoted to destructive edit targets directly. A cruft item is
  `prune_safe` only with a stricter **full-content proof**, not a weak one:
  - **Content hash / subsumption proof with NO intervening write.** A tool
    result is `prune_safe` only when its content hash matches an entry LATER
    in the tail (a verbatim duplicate), OR its content is a strict substring
    of a later tool_result for the same tool/path (subsumption), AND there has
    been no write/edit to the relevant path between the two. **In BOTH cases
    the duplicate/subsumer MUST be from the same tool/path** (re-review round
    19 [P2]): two different commands producing identical bytes do NOT make
    the earlier result `prune_safe`, because stubbing it would leave the only
    full content belonging to a different command/path, losing path-specific
    evidence. The read-then-edit-then-read sequence is explicitly NOT safe:
    the older read holds the pre-edit bytes the model may need, so it is
    rejected even though it is a "same path read again later." This closes the
    [P1] finding.
  - **Persisted-to-artifacts proof is NOT sufficient on its own.**
    `artifacts.extract()` stores only prompts/corrections/error counts/working
    commands/hex constants/file paths — NOT arbitrary tool-result bodies. So
    "content already persisted to on-disk artifacts" is a necessary but not
    sufficient condition; it never alone justifies a content-free stub of a
    tool_result. The full-content hash/subsumption proof above is required for
    tool results. (The persisted-to-artifacts proof remains sufficient for
    the narrow classes artifacts actually store — e.g. a duplicate user
    prompt.)
  - Age/size-only `dormant` items are surfaced in the report but NOT stubbed
    (they may be needed later — e.g. a dormant test failure that surfaces a
    regression). This closes the [P2] finding.
- A tail span is the entries after the prefix-break point: the index of the
  last `CompactionEntry` (if any) plus the contiguous prefix of unedited
  entries since. Everything after is the tail.
- **Net-savings gate (cache-rebuild-aware):** a suffix is only returned when
  `sum(cruft tokens) − stub_overhead_tokens − downstream_live_tail_rebuild_tokens
  >= PRUNE_MIN_RECLAIM`. Editing the first cruft entry invalidates the KV/prefix
  cache for EVERY later entry on the next turn — so a 5k cruft at the start of
  a 400k live tail would pass a naive gate but force a 400k re-read, defeating
  the cost goal. The gate therefore charges the **downstream live-tail rebuild
  cost** (tokens of live entries after the first cruft edit that will be
  re-read next turn). If non-cruft tail dominates and the net is negative, the
  suffix is empty and the caller falls through to no-recommendation (below
  hard line) or `compact()` (at/above hard line). (Re-review round 9 [P1].)
  Live-between items are NOT charged separately (they're part of the
  downstream live tail already) and NOT edited.

### B2. Trigger: "fires at 100k on 1M"

The trigger is **per-tier**, absolute, and means "start evaluating the tail for
cruft," not "force compaction":

```jsonc
// config.json (autocompactor)
"PRUNE_MODE": "advise",                  // "advise" (default) | "actuate" — gates ONLY prune; compact fallback honors MODE
"PRUNE_EVAL_TIERS": {"1m": 100000, "512k": 80000, "300k": 70000},
"PRUNE_MIN_RECLAIM": 5000,         // don't prune if net cruft < 5k tokens
"PRUNE_MAX_STUB_CHARS": 200,       // inline stub cap
"PRUNE_COOLDOWN_TOKENS": 15000,    // don't re-evaluate too often
"PRUNE_SAFE_TARGET": {"1m": 320000, "512k": 160000, "300k": 90000}
```

`PRUNE_MODE` is a separate gate from the existing `MODE`. The checked-in
`config.json` has `MODE: "actuate"` (the existing summarizing compaction is
already actuated); the destructive prune path ships as `PRUNE_MODE: "advise"`
until ≥1 day of clean prune telemetry, per B6. `MODE` continues to govern the
`compact()` fallback; `PRUNE_MODE` governs ONLY the cache-preserving prune path.
This closes the adversarial [P1] finding.

The `cmd_evaluate` path resolves the tier (via `window_resolver.learned_tier`)
and, when `context_tokens >= PRUNE_EVAL_TIERS[tier]`, calls
`build_tail_cruft_suffix`. The suffix is returned only if the **net-savings
gate** holds (`cruft − stub_overhead − downstream_live_tail_rebuild >=
PRUNE_MIN_RECLAIM`; cache-rebuild-aware — see B1). When the net gate
holds, the bridge follows the **B2b hard-line/safe-target decision table**
BEFORE emitting a recommendation. `PRUNE_SAFE_TARGET` gates prune-vs-compact
ONLY at/above the hard line (re-review round 11 [P2]): below the hard line,
any net-positive contiguous suffix emits `kind: "prune"` even if post-prune
est. > PRUNE_SAFE_TARGET (the prune still moves toward the safe line and
compact is hard-line-only); at/above the hard line with post-prune est. >
PRUNE_SAFE_TARGET, it emits `kind: "compact"` so a tiny prune never strands
the session or skips the summarizing fallback (re-review round 8 [P1]). When
the net gate fails, the bridge recommends **nothing at this evaluation
threshold** (NOT compact — see
B2b). The bridge emits:

```jsonc
{
  "recommend": true,
  "kind": "prune",            // NEW: "prune" | "compact"
  "at_hard_line": false,     // true when context >= HARD_PCT_WIDE × window; drives the hard-line failure fallback in B3
  "prune_targets": [           // content-free: ids + token estimate + reason + is_cruft
    {"entry_id": "e123", "tool_name": "bash", "tokens": 4200, "reason": "stale", "is_cruft": true},
    {"entry_id": "e131", "tool_name": "read", "tokens": 3100, "reason": "redundant", "is_cruft": true}
  ],
  "prune_total_tokens": 7300,
  "evaluate_revision": {       // baseline for editEntriesRevisions' atomic stale-race check
    "entry_count": 142,
    "target_hashes": {"e123": "sha256:…", "e131": "sha256:…"},  // hash of each target entry content
    "proof_hashes": {"e150": "sha256:…", "e155": "sha256:…"}, // hash of each proof-source entry the prune_safe verdict relied on (the later duplicate/subsumer whose preserved bytes justify stubbing the target)
    "interval_hashes": {"e140": "sha256:…", "e145": "sha256:…"}, // hash of each intervening entry in the no-write proof interval (target..proof-source); catches another extension's non-revision edit inserting a write
    "prefix_tail_id": "e141"                                  // last entry id at evaluate time
  },
  "reason": "7.3k of stale/redundant tool output in the tail — prune without summarizing"
}
```

`evaluate_revision` carries the baseline that `ctx.editEntriesRevisions`
compares against INSIDE Pi's session lock (the atomic verify+rewrite). It is
NOT compared by `prepare_prune` — `prepare_prune` is backup-only (re-review
round 3 [P1]: running the stale-check before taking the session lock
recreates the verify-then-rewrite TOCTOU gap). `evaluate_revision` carries:
- `entry_count` + `prefix_tail_id`: a monotonic-ish revision (entry count +
  last entry id at evaluate time).
- `target_hashes`: a content hash of EACH target entry. Entry-count +
  last-entry-id alone misses in-place edits (they don't append), so the
  per-target content hashes are the authoritative stale-detector for the
  targets. If any target's current content hash differs from
  `evaluate_revision.target_hashes[id]`, the transcript was edited mid-flight
  and the prune is aborted.
- `proof_hashes`: a content hash of EACH **proof-source entry** the
  `prune_safe` verdict relied on — the later duplicate/subsumer whose
  preserved bytes justify stubbing the earlier target. Safety depends on the
  proof entry staying unchanged until the edit: an in-place edit or prune of
  the later proof entry between eval and `editEntriesRevisions` could leave
  `target_hashes`, `entry_count`, and `prefix_tail_id` unchanged while the
  earlier target is stubbed — eliminating the only live copy. So every
  proof-source entry is hashed and validated under the same session lock
  alongside the targets; any proof-source mismatch also aborts the prune
  (re-review round 6 [P1]).
- `interval_hashes`: a content hash of EACH **intervening entry** in the
  no-write proof interval (between the target and its proof-source). The
  `prune_safe` strict-substring/duplicate proof depends on NO intervening
  write to the path. If another extension uses the non-revision `editEntries`
  to change an intervening entry into a write between eval and
  `editEntriesRevisions`, `entry_count`, `prefix_tail_id`, `target_hashes`,
  AND `proof_hashes` can all still match while the no-write proof is stale.
  So every intervening entry in the proof interval is hashed and validated
  under the same session lock; any interval mismatch also aborts the prune
  (re-review round 9 [P1]).

### B2b. Hard-line fall-through (compact fires ONLY at/above the hard line)

The adversarial review's [P1] finding: at/near the hard line, choosing prune
whenever tail cruft >= `PRUNE_MIN_RECLAIM` can leave the session far above the
hard threshold after a tiny 5–7k prune, and `PRUNE_COOLDOWN_TOKENS` then
suppresses the summarizing fallback until enough new tokens accrue.

**Fix:** the summarizing `compact()` fallback fires ONLY at/above the hard
line (`HARD_PCT_WIDE × window`). At the lower `PRUNE_EVAL_TIERS` threshold
with no net-positive contiguous cruft, the bridge recommends NOTHING — it
does NOT fall through to `compact()` there, because summarizing at 100k on a
1M window with no useful cruft would bust the cache for no reason. Prune is
gate-posted by the post-prune estimate vs `PRUNE_SAFE_TARGET` only at/above the
hard line.

Decision table in `cmd_evaluate`:

| Band | Condition | Recommendation |
|---|---|---|
| Below hard line | cruft suffix net >= PRUNE_MIN_RECLAIM AND post-prune est. <= PRUNE_SAFE_TARGET | `kind: "prune"` (cache-preserving) |
| Below hard line | cruft suffix net >= PRUNE_MIN_RECLAIM AND post-prune est. > PRUNE_SAFE_TARGET | `kind: "prune"` (still cache-preserving — compact is hard-line-only; the prune moves toward the safe line even if it doesn't reach it; re-review round 9 [P2]) |
| Below hard line | cruft suffix net < PRUNE_MIN_RECLAIM (no useful cruft) | **no recommendation** (do NOT compact at the low threshold) |
| At/above hard line | cruft suffix net >= PRUNE_MIN_RECLAIM AND post-prune est. <= PRUNE_SAFE_TARGET | `kind: "prune"` (cache-preserving) — BUT if `PRUNE_MODE <> "actuate"` OR `editEntriesRevisions` unavailable, fall back to `kind: "compact"` so the hard-wall safety is never regressed by an advise-only prune rollout (re-review round 10 [P1]) |
| At/above hard line | cruft suffix net >= PRUNE_MIN_RECLAIM AND post-prune est. > PRUNE_SAFE_TARGET | `kind: "compact"` (summarizing fallback — prune alone won't get below the safe line) |
| At/above hard line | cruft suffix net < PRUNE_MIN_RECLAIM | `kind: "compact"` (no useful cruft at the hard line) |

So summarizing compaction becomes the rare fallback, fires ONLY at/above the
hard line, and is NEVER triggered by a low-threshold no-cruft case. This
closes both the adversarial [P1] and the re-review round 3 [P1].

### B3. Execution: TS shim calls `editEntriesRevisions` (atomic, with pre-prune backup)

The adversarial review's [P1] finding: the prune path mutates history directly
from `agent_end` via `ctx.editEntries`, bypassing the existing `prepare` path
that backs up the session and extracts artifacts before compaction. A stale or
misclassified detector would replace original content with a content-free stub
with no recovery.

**Fix — atomic pre-prune backup + session-revision check, BEFORE any edit:**

`src/pi/autocompactor.ts` `agent_end` handler, on a `prune` verdict:

```typescript
// 0. PRUNE_MODE gate (re-review round 14 [P1]): the destructive prune path
//    requires PRUNE_MODE === "actuate". At the default PRUNE_MODE="advise",
//    OR when the revision-safe primitive is missing, the prune verdict is
//    surfaced as an advisory only (no prepare_prune, no editEntriesRevisions).
//    MODE="actuate" alone must NOT actuate prune — only the compact fallback.
const PRUNE_MODE = configuredPruneMode()  // "advise" (default) | "actuate"
const hasPrimitive = typeof ctx.editEntriesRevisions === "function"
if (PRUNE_MODE !== "actuate" || !hasPrimitive) {
  // ADVISE-ONLY: surface the prune verdict as a persistent advisory, no edit.
  // CLEAR THE EVALUATE-TIME COOLDOWN (re-review round 17 [P1]): cmd_evaluate
  // records the prune cooldown before execution; a surface-only advisory must
  // roll it back so the next evaluation isn't suppressed even though no edit
  // or compact happened.
  await bridge(pi, ctx, "clear_prune_cooldown", [], EXEC_TIMEOUT_MS)
  announce(pi, ctx, withContextState(
    `autocompactor: prune opportunity — ${verdict.reason} ` +
    `(PRUNE_MODE=${PRUNE_MODE}${hasPrimitive ? "" : ", primitive missing"}; ` +
    `surface-only, no edit).`, verdict.contextState), "info", true)
  // At/above the hard line in advise-only, fall back to compact so the hard
  // wall is never stranded by the prune rollout being advise-only — BUT only
  // if MODE actuates compact (re-review round 16 [P1]): PRUNE_MODE gates prune
  // ONLY; MODE continues to govern the compact() fallback. In a MODE=advise
  // deployment, this path surfaces the compact recommendation as an advisory
  // (no actuate) instead of calling safeCompact().
  if (verdict.at_hard_line) {
    const MODE = mode(verdict.mode)  // "advise" | "actuate"
    if (MODE === "actuate") {
      safeCompact(pi, ctx, undefined,
        () => { selfTriggered = false },
        () => flushAutoResume(ctx),
        () => { pendingAutoResume = null })
    } else {
      announce(pi, ctx, "autocompactor: at hard line — compact advised " +
        "(MODE=advise, surface-only). Set MODE=actuate to actuate.",
        "warning", true)
    }
  }
  return  // no edit
}

// 1. Pre-prune backup (mirrors the existing prepare backup, content-free audit).
//    Copies the session JSONL to statedir.state_root('pi')/backups/
//    <session_id>-<ts>-prune.jsonl BEFORE any edit, so a misclassification is
//    fully recoverable. The bridge's cmd_prepare is NOT reused here (its
//    customInstructions/artifact-digest path is summarizer-specific); a slim
//    prune-specific backup entry in pi_bridge.py owns this.
const prep = await bridge(pi, ctx, "prepare_prune",
  ["--targets", ids, "--revision", JSON.stringify(verdict.evaluate_revision)],
  PREPARE_TIMEOUT_MS)
//    prepare_prune backs up the JSONL ONLY (pre-edit snapshot for rollback).
//    It does NOT perform the race guard — that lives in the atomic
//    editEntriesRevisions call below, under Pi's session lock. Returns
//    {ok, backup_path}. If the backup failed, abort.
if (!prep?.ok) {
  announce(pi, ctx, "autocompactor: prune aborted — backup failed; " +
    "deferring to next evaluation.", "warning", true)
  // CLEAR COOLDOWN FIRST (re-review round 18 [P2]): cmd_evaluate recorded the
  // prune cooldown before execution; a backup failure (below OR at hard line)
  // must roll it back so the next evaluation isn't suppressed despite no edit.
  await bridge(pi, ctx, "clear_prune_cooldown", [], EXEC_TIMEOUT_MS)
  // HARD-LINE BACKUP-FAILURE FALLBACK (re-review round 13 [P1]): a hard-line
  // prune whose backup fails would otherwise loop — next agent_end picks the
  // same prune, backup fails again, never compacts. Apply the same
  // mark-and-compact fallback as a prune edit failure: mark prune unavailable
  // (sticky bridge flag) and invoke safeCompact() NOW so the hard-wall safety
  // path is never stranded by an unwritable backup dir / full disk.
  if (verdict.kind === "prune" && verdict.at_hard_line) {
    await bridge(pi, ctx, "mark_prune_unavailable", [], EXEC_TIMEOUT_MS)
    if (mode(verdict.mode) === "actuate") {
      announce(pi, ctx, "autocompactor: prune backup failed at hard line — " +
        "falling back to summarizing compaction.", "warning", true)
      safeCompact(pi, ctx, undefined,
        () => { selfTriggered = false },
        () => flushAutoResume(ctx),
        () => { pendingAutoResume = null })
    } else {
      announce(pi, ctx, "autocompactor: prune backup failed at hard line — " +
        "compact advised (MODE=advise, surface-only).", "warning", true)
    }
  }
  return  // no edit applied
}

// 2. Edits are the contiguous suffix (cruft items get content:null stubs;
//    live-between items are NOT in the edit list — they are left verbatim so
//    the suffix stays contiguous from the first cruft edit onward).
const edits = verdict.prune_targets.filter(t => t.is_cruft).map(t => ({
  id: t.entry_id,
  content: null,                 // stub: Pi replaces with a fixed stub string
  pruned: true,
  note: `pruned: ${t.tool_name} ${t.tokens}t (${t.reason})`,
}))
// 3. ATOMIC expected-revision edit (TOCTOU-safe). The revision check (entry
//    count + prefix-tail id + per-target content hash) AND the rewrite happen
//    under the SAME session lock, all-or-nothing. A partial edit (some targets
//    skipped as unknown/cross-branch) or a revision mismatch yields `ok: false`
//    with NO edits applied.
const result = ctx.editEntriesRevisions(edits, verdict.evaluate_revision)
if (!result?.ok || result.mismatched.length || result.skipped.length) {
  // Failed prune: do NOT report success, do NOT consume cooldown, do NOT
  // hide the compact fallback. Surface the failure and defer.
  // ROLL BACK THE EVALUATE-TIME COOLDOWN (re-review round 16 [P1]): the bridge
  // records recommendation cooldown at evaluate time, before execution. A
  // failed prune (mismatch/skip/backup-fail) must clear that cooldown so the
  // next evaluation isn't suppressed — otherwise a hard-line compact fallback
  // can be hidden until enough new tokens accrue.
  await bridge(pi, ctx, "clear_prune_cooldown", [], EXEC_TIMEOUT_MS)
  announce(pi, ctx, "autocompactor: prune aborted — session changed or " +
    `targets unavailable (mismatched: ${result?.mismatched?.length ?? 0}, ` +
    `skipped: ${result?.skipped?.length ?? 0}); deferring.`, "warning", true)
  // HARD-LINE FAILURE FALLBACK (re-review round 12 [P1]): if this verdict was
  // at/above the hard line, a deterministic edit failure (e.g. cross-branch
  // skipped targets) would otherwise loop forever — the next agent_end
  // re-evaluates, picks the same failing prune, and never compacts. So on a
  // failed prune at/above the hard line, mark prune unavailable for this
  // session (a sticky flag in bridge state, cleared on the next successful
  // compact or on Pi restart) and invoke the summarizing `compact()` fallback
  // NOW so the hard-wall safety path is never stranded. Below the hard line,
  // a failed prune just defers to the next evaluation (no compact).
  if (verdict.kind === "prune" && verdict.at_hard_line) {
    await bridge(pi, ctx, "mark_prune_unavailable", [], EXEC_TIMEOUT_MS)
    // Roll back the evaluate-time cooldown (re-review round 16 [P1]).
    await bridge(pi, ctx, "clear_prune_cooldown", [], EXEC_TIMEOUT_MS)
    if (mode(verdict.mode) === "actuate") {
      announce(pi, ctx, "autocompactor: prune unavailable at hard line — " +
        "falling back to summarizing compaction.", "warning", true)
      safeCompact(pi, ctx, undefined,
        () => { selfTriggered = false },
        () => flushAutoResume(ctx),
        () => { pendingAutoResume = null })
    } else {
      announce(pi, ctx, "autocompactor: prune unavailable at hard line — " +
        "compact advised (MODE=advise, surface-only).", "warning", true)
    }
  }
  return  // no edit applied
}
// surface the per-prune report (B4) — only on a fully-successful atomic edit
```

No `ctx.compact()` call, no `CompactionEntry`, no prefix rewrite. The model's
next turn reads the (unchanged) prefix from cache and the stubbed tail fresh —
cheap. The pre-prune backup closes the [P1] recovery finding; the atomic
`editEntriesRevisions` under the session lock closes the TOCTOU/partial-edit
race (re-review [P1]).

### B4. Per-prune report

Small, content-free, surfaced as a persistent chat message:

```
autocompactor: tail-pruned 7.3k tokens (4 entries) without summarizing
  • bash (stale, 4.2k) → stub
  • read (redundant, 3.1k) → stub
  prefix cache preserved; compaction deferred.
```

Telemetry event `prune_report` (content-free):

```jsonc
{"type":"prune_report","session_id":"…","edited_count":4,
 "pruned_tokens_estimate":7300,"first_edited_id":"e123",
 "tools":{"bash":1,"read":1},"reasons":{"stale":1,"redundant":1},
 "context_before":102000,"context_after_estimate":94700}
```

### B5. What this replaces in the old spec

- The per-tier *summarizing* compaction targets (`SOFT_TARGET_1M` etc.) are
  replaced by `PRUNE_EVAL_TIERS` — same thresholds, different semantics
  (evaluate tail cruft, not summarize prefix).
- The big "compaction report" (before/after tokens, phase, composition,
  artifacts, instructions) is replaced by the small per-prune report. The big
  report stays only for the rare fallback `compact()` path.
- The `pre_report`/`compactionReport` lifecycle from the old spec is dropped for
  the prune path; the `session_after_edit` event (Part A3) is the audit trail.

## Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Upstream Pi won't accept a new `editEntries` primitive | Design it minimally (one method, one event, one doc section). The cache-preservation argument is strong; the alternative (status quo: cache-busting summarizer) is worse for every Pi user, not just autocompactor. |
| Editing the wrong entry breaks the cache mid-window | Consumer (B1) only ever edits a contiguous tail suffix after the last compaction boundary; only `is_cruft` items are edited (live-between left verbatim); net-savings gate is cache-rebuild-aware: `sum(cruft) − stub_overhead − downstream_live_tail_rebuild_tokens >= PRUNE_MIN_RECLAIM` (live-between/tail tokens are NOT reclaimed or edited, but ARE charged through the rebuild term — re-review round 18 [P2]). Part A documents the contiguous constraint; Part B enforces it. |
| Sparse edits force re-read of later live entries | B1 returns a contiguous suffix, not a sparse top-N. The cache-break point is the start of the suffix only. (Adversarial [P1].) |
| Tiny cruft blocks hard compaction | B2b decision table: at/above the hard line, prune is taken only if post-prune est. <= PRUNE_SAFE_TARGET; otherwise compact fires. Prune never suppresses the summarizing fallback. (Adversarial [P1].) |
| Advisory-only flags used as destructive targets | B1 `prune_safe` proof: content hash/subsumption with NO intervening write (read-then-edit-then-read explicitly rejected); persisted-to-artifacts alone is NOT sufficient for tool results. Age/size-only `dormant` is surfaced but NOT stubbed. (Adversarial [P2] + re-review [P1].) |
| Proof-source entry mutated between eval and atomic edit, eliminating only live copy | `evaluate_revision` carries `proof_hashes` for every proof-source entry the `prune_safe` verdict relied on; `editEntriesRevisions` validates them under the same session lock alongside targets; any proof-source mismatch aborts the prune. (Re-review round 6 [P1].) |
| Version skew: editEntries exists but editEntriesRevisions missing | Graceful-degradation guard checks `typeof ctx.editEntriesRevisions === "function"` specifically; a Pi version with the non-revision `editEntries` only is treated as missing the primitive (no actuate). (Re-review round 6 [P2].) |
| Emitted verdict schema missing is_cruft | Verdict schema includes `is_cruft` per prune_target so the TS shim's `filter(t => t.is_cruft)` has the field. (Re-review round 4 [P2].) |
| TOCTOU between verify (prepare_prune) and mutate (editEntries) | Part A `editEntriesRevisions` does the revision check + rewrite under the SAME session lock, all-or-nothing. `prepare_prune` is backup-only — the revision comparison is NOT run there (running it before the lock recreates the gap). Partial edit / mismatch → `ok: false`, NO edit applied. Consumer treats `!ok` as failed prune. (Re-review round 2 + round 3 [P1].) |
| Deterministic prune failure at hard line loops forever (never compacts) | B3 hard-line failure fallback: on `!ok`/`mismatched`/`skipped` at/above the hard line, mark prune unavailable (sticky bridge flag) and invoke `safeCompact()` immediately; the next `agent_end` sees prune-unavailable and picks `compact`. Below the hard line, a failed prune just defers. (Re-review round 12 [P1].) |
| Backup failure at hard line strands the session | B3 backup-failure fallback mirrors the edit-failure fallback: on `prepare_prune` `!ok` at/above the hard line, mark prune unavailable and invoke `safeCompact()` immediately — an unwritable backup dir / full disk cannot strand the hard-wall safety path. (Re-review round 13 [P1].) |
| `MODE=actuate` actuates prune during advise rollout | B3 PRUNE_MODE gate: destructive prune requires `PRUNE_MODE === "actuate"` AND primitive present; otherwise surface-only advisory (no edit). At hard line in advise-only, falls back to `compact()` so the hard wall is never stranded. (Re-review round 14 [P1].) |
| Part A contract forbids sparse edits, forcing live-content stubbing | Part A contract updated: contiguous tail span is the cache-break SPAN; actual edits within it may be SPARSE (only `is_cruft` entries edited, live-between verbatim). (Re-review round 14 [P1].) |
| Crash/ENOSPC mid-edit leaves duplicate/partial JSONL | `_editEntriesRevisions` uses temp-file + atomic rename under the session lock (not append-only `_persist`); on any error the temp is unlinked and the live file is untouched (rollback). (Re-review round 15 [P1].) |
| Fallback compaction actuates even when MODE=advise | All `safeCompact()` fallback calls (advise-only hard line, backup-failure hard line, edit-failure hard line) are MODE-gated: only `safeCompact()` when `MODE === "actuate"`; otherwise surface a compact advisory. (Re-review round 16 [P1].) |
| Failed prune leaves cooldown consumed, hiding later compact | Every prune abort path (mismatch/skip/backup-fail/advise-only-no-edit) calls `clear_prune_cooldown` to roll back the evaluate-time cooldown so the next evaluation isn't suppressed. (Re-review round 16 [P1].) |
| PRUNE_MODE undefined → prune actuates immediately under MODE | B2 config contract adds `PRUNE_MODE: "advise"` default, separate from `MODE`; gates ONLY prune, compact fallback honors `MODE`. (Re-review [P1].) |
| In-place edit mutates shared ancestor for non-active branches | Part A `editEntries` refuses entries reachable from non-active branches, OR performs copy-on-write before stubbing. (Re-review [P1].) |
| Stub content loses information the model needs | `prune_safe` proof + pre-prune backup. Live, still-referenced content is never touched. |
| Prune fires too often | `PRUNE_COOLDOWN_TOKENS` + `PRUNE_MIN_RECLAIM` + net-savings gate. Below the net floor, no prune. |
| Part B lands before Part A is merged | Part B is gated on the Pi version that ships `editEntriesRevisions` (the revision-safe API); the bridge degrades gracefully (no prune, falls back to advise-only) when the primitive is absent, detected via `typeof ctx.editEntriesRevisions === "function"` — NOT the non-revision `editEntries` (a Pi version with only the non-revision path is treated as missing). (Re-review round 6+7 [P2].) |

## Sequencing

1. **Part A spec + PR** to `github.com/earendil-works/pi` (`packages/coding-agent`). The primitive is small and self-contained; the PR is the gate.
2. **Part A merged + published** in a Pi release (e.g. 0.81.x).
3. **Part B implementation** in autocompactor against the new Pi version, with graceful degradation on older Pi.
4. **Part B tests** for the prune path (detection, recommendation kind, shim wiring, per-prune report, telemetry content-free).
5. **Live gating:** `PRUNE_MODE=advise` for the prune path first (surfacing the recommendation without acting) — this is separate from `MODE`, which continues to govern the `compact()` fallback; flip `PRUNE_MODE` to actuate after ≥1 day of clean prune telemetry, matching the prior flip-to-actuate discipline. (Re-review round 7 [P2].)

## Out of scope

- Per-tier summarizing compaction targets (`SOFT_TARGET_1M` etc.) — superseded by `PRUNE_EVAL_TIERS`.
- The big before/after compaction report for the prune path — stays only for the rare fallback `compact()`.
- Deleting entries outright — chose inline stubs (replace-in-place) to preserve call/result pairing and keep the cache break local.
- Changing `HARD_PCT_WIDE` / `MIN_SAVINGS` for the fallback path.