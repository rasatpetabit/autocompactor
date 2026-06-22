# Spec: turn profiler — per-turn transcript analysis

Date: 2026-06-22
Status: brainstorm/masterplan draft, rev 2 — revised after GPT-5.5-pro adversarial review (see review.md; 2×P1 + 3×P2 folded in)

## Summary

Add a read-only diagnostic that profiles a Pi session transcript **turn by
turn**, showing — for each assistant LLM call — the context-window size, the
growth delta, the output generated, the $ cost, the cache-hit ratio, and the
tools invoked, plus a per-turn "what fed this call" composition and behavioral
flags. Output is 2–3 lines per turn with token counts in SI (`147k`, `1.5m`),
a summary block, an ASCII context-growth sparkline, and a per-tool cost table.
A `--json` mode emits the full structured record for scripting/charts; a
`--rollup` flag collapses consecutive assistant turns under each user prompt.

The profiler is a **standalone diagnostic run manually by the developer**. It
is *not* wired into the Pi compaction flow and never touches `pi_bridge.py`
(whose file contract is a never-raise JSON bridge emitting at most one JSON
object). It reuses the existing transcript primitives and adds one small public
helper; `analyze()` and `TranscriptStats` are untouched in v1.

## Motivation

Autocompactor currently reasons about a transcript as a single aggregate
(`TranscriptStats`) — total chars, one context figure, one composition
breakdown at "now." There is no visibility into *how* a session got to its
current state: which turns spiked the context, which tool outputs were
oversized, how cache efficiency decayed, where the money went, or where the
optimal compact point would have been. The per-turn profiler fills that gap and
gives the owner a comprehensive analysis tool grounded in the exact per-call
usage data the Pi transcript already records.

## Goals

- **Per-turn token profiling**: context-window size (exact), growth delta
  (exact), output tokens (exact), cost (exact), cache-hit ratio (exact) — for
  every assistant LLM call in the active segment.
- **Per-turn content-window analysis**: a "fed by" composition showing what
  entered the window for each call (tool output, prior assistant text/thinking),
  clearly labelled estimated (chars/4) vs exact.
- **Cost & cache efficiency**: cumulative spend, input/output/cache cost split,
  overall cache-hit ratio, per-tool cost/token attribution table.
- **Waste & reclaim detection**: oversized tool outputs, redundant re-reads,
  thinking bloat, error/retry loops, parallel-tool bursts — flagged inline.
- **Timing & behavior patterns**: wall-clock per turn, idle gaps, tool-usage
  frequency, parallel-vs-serial tool calls.
- **Human + machine output**: compact SI text report (2–3 lines/turn) by
  default; `--json` for structured consumption.
- **Rollup view**: collapse the agentic loop under each human prompt.

## Non-goals (v1)

- Do **not** wire the profiler into the Pi extension compaction flow, `evaluate`,
  `prepare`, `reinject`, or `contextState`. It is a separate manual diagnostic.
- Do **not** modify `pi_bridge.py` (contract: never-raise, one JSON object max),
  `TranscriptStats`, or `pi_session_lib.analyze()`.
- Do **not** add a tokenizer dependency — per-category token splits stay at the
  existing chars/4 estimate, clearly labelled `≈`.
- Do **not** build a "compact-here what-if" projection, `--watch` live tail, or
  `--compare` (stretch — see below).
- Do **not** depend on an LLM; the analysis is mechanical.

## Data foundation (verified against a real Pi transcript)

Each assistant `message` entry in a Pi session JSONL carries a `usage` block:

```json
{ "input": 5968, "cacheRead": 6144, "cacheWrite": 0,
  "output": 74, "totalTokens": 12186,
  "cost": { "input": 0.0298, "output": 0.0022,
            "cacheRead": 0.0031, "cacheWrite": 0, "total": 0.0351 } }
```

Turn model (verified, 295-entry session → 134 assistant turns):

- One **turn** = one assistant `message` entry (the natural unit with its own
  usage block + timestamp). `toolResult` entries link back via `toolCallId`.
- Roles observed: `user`, `assistant`, `toolResult`; block types: `text`,
  `thinking`, `toolCall`.

### Attribution semantics (correctness — load-bearing; revised per review P1#1/P1#2)

Two distinct context figures are tracked and never conflated:

- **`pre_call_tokens` (exact, window-at-call-start)** = `input + cacheRead +
  cacheWrite` — what was on the wire going INTO the model this call. This is the
  precise per-call reading.
- **`occupancy` (exact, autocompactor-consistent)** = `totalTokens` when present
  (else the `_usage_context()` fallback sum) — **the same figure `evaluate`,
  `contextState`, and `st.context_tokens` use everywhere else** (via
  `pi_session_lib._usage_context()`, which prefers `totalTokens`).
  `peak_ctx`, `final_ctx`, `reclaimable_tokens`, and the sparkline are computed
  from `occupancy`, NOT `pre_call_tokens`, so the profiler's headline numbers
  reconcile with the rest of autocompactor. `output` and `cost.*` are exact.

- **Growth delta (since-previous-call, NOT "exact content growth")**: `Δctx =
  occupancy_now − occupancy_prev`. Labelled since-previous-call because cacheRead
  shifts (a previously-uncached prefix flipping to cached) change `occupancy`
  composition without adding new content — Δctx tracks the call-to-call figure,
  not a strict content delta.
- **`fed by` = the interval, not `toolCallId` linkage** (review P1#2). What
  entered the window for call N is **every active-path entry between the
  previous assistant message and call N**: `toolResult`, `bashExecution`,
  `custom`, any new `user` text, and the *prior* assistant's own output+thinking
  (which re-enters cached). `toolCallId` is used ONLY as an optional tool-name
  resolver for `toolResult` entries; entries with no resolvable call id are
  surfaced explicitly (e.g. `tool(unknown)`) rather than dropped. This handles
  parallel/out-of-order results, `bashExecution`, and `custom` roles that the
  toolCallId-only design would misattribute.
- **Tool calls emitted by call N feed call N+1's window** (one-call skew) —
  reported as `emitted`, distinct from `fed by`.
- **Per-category token splits (estimated)**: tool-result / assistant-text /
  thinking tokens are derived via the project's existing `CHARS_PER_TOKEN = 4`
  heuristic and labelled `≈`. Only `usage` fields and derived `occupancy`/
  `pre_call_tokens`/`Δctx`/`cost` are exact.

## Architecture (Approach A — recommended)

Additive, non-invasive. New bounded module + thin shim, one small public helper,
no edits to existing hot paths.

### Files

| file | role |
|---|---|
| `src/autocompactor/turn_profile.py` | **Core** (harness-agnostic). `profile_turns() -> ProfileResult`, `TurnRecord`, analysis/aggregates, sparkline + per-tool table builders, and `main()` (text default / `--json` / `--rollup`). No CLI side effects outside `main()`. |
| `src/turn_profile.py` | **Thin entrypoint shim** (mirrors `src/pi_bridge.py`, `src/nightly_eval.py`): puts `src/` on `sys.path`, calls `autocompactor.turn_profile.main()`. |
| `src/autocompactor/pi_session_lib.py` | **Two new public helpers**: `active_path(path) -> (full_path, active, compaction_count)` factoring `_leaf_path` + `_active_segment`, and `analyze_active_prefix(full_path, active_prefix, recent_window) -> TranscriptStats` factoring the `analyze()` walk to accept an explicit prefix (for composition @ peak). `analyze()` unchanged in behavior (it can delegate to the helpers; parity-tested). |

### Reuse (do not duplicate)

- `pi_session_lib._leaf_path`, `_active_segment`, `_message`, `_content_blocks`,
  `_message_text`, `_tool_calls`, `_tool_result_text`, `_entry_ts`,
  `_usage_context`, `_usage_compat`, `PI_TOOL_ARG_KEYS` — imported into
  `turn_profile.py`. The new `active_path()` exposes the extraction so the
  profiler imports few underscore helpers, not many.
- `transcript_lib._block_text`, `_content_words`, `CHARS_PER_TOKEN` (single
  source for the chars/4 estimate).
- `policy._fmt_tokens` — **single source for SI formatting** (`147k`, `1.5m`).
- `transcript_lib.context_composition` — reused ONCE for the authoritative
  window composition @ peak. **Because `context_composition()` only formats
  counters already accumulated on a `TranscriptStats` (review P2#3), a new
  tested public helper `analyze_active_prefix(full_path, active_prefix,
  recent_window) -> TranscriptStats` is added to `pi_session_lib`** that builds
  the counters over an arbitrary prefix (it's the existing `analyze()` walk
  factored to accept an explicit prefix instead of always the post-compaction
  segment). Called once for the prefix up to the peak turn — O(n), not O(n²).
  If that helper proves non-trivial, composition-at-peak is deferred (stretch)

## Data model

```python
@dataclass
class TurnRecord:
    index: int               # assistant-turn ordinal (0-based)
    role: str                # "assistant" | "user" (rollup row)
    timestamp: datetime | None
    has_usage: bool          # False when this call carries no usage block
    # --- exact (from usage) ---
    occupancy: int           # totalTokens (or _usage_context fallback) — the
                             # autocompactor-consistent figure; drives peak/sparkline
    pre_call_tokens: int     # input + cacheRead + cacheWrite — window-at-call-start
    delta_occupancy: int     # occupancy − prev call's occupancy (since-previous-call)
    input_tokens: int
    cache_read: int
    cache_write: int
    output_tokens: int
    cost: float              # cost.total
    cache_hit_ratio: float   # cache_read / pre_call_tokens (0 if 0)
    # --- estimated (chars/4) ---
    fed_by_tokens: int       # ALL interval entries since prev assistant call
    assistant_text_tokens: int
    thinking_tokens: int
    # --- structural ---
    tools_called: list[str]           # tool names this turn EMITTED (feed next)
    tool_call_args: dict              # name -> arg summary (path/command) for flags
    fed_by: list[dict]                # [{role, tool_or_unknown, tokens, is_error}]
                                      # interval entries that grew THIS call's window
    is_error_turn: bool
    # --- flags (computed) ---
    flags: list[str]         # e.g. "large-output", "redundant-read", "think-bloat",
                             #      "error-retry", "parallel-tools", "idle-gap"
    wall_seconds: float | None       # since previous turn


@dataclass
class ProfileResult:
    session_id: str
    turns: list[TurnRecord]
    human_turns: list[HumanTurnRollup]   # only populated under --rollup
    summary: ProfileSummary


@dataclass
class ProfileSummary:
    turn_count: int
    human_turn_count: int           # user-prompt count
    has_usage: bool                 # False when no assistant call carried usage
    peak_ctx: int                   # max occupancy across turns (0 if none)
    peak_turn_index: int | None     # None when has_usage is False / empty segment
    start_ctx: int
    final_ctx: int
    # cost
    total_cost: float
    cost_split: dict                # {input, output, cacheRead, cacheWrite}
    # cache
    overall_cache_hit_ratio: float
    avg_cache_write_per_turn: float
    # composition @ peak (reuses context_composition via analyze_active_prefix;
    # estimated, labelled ≈)
    composition_at_peak: dict | None
    # waste / reclaim
    reclaimable_tokens: int         # peak_ctx − POST_FLOOR
    redundant_read_count: int
    oversized_output_count: int
    # behavior (all nullable for empty/degenerate profiles)
    tool_frequency: dict[str, int]
    biggest_growth_turn: tuple[int, int] | None      # (index, delta)
    biggest_tool_output_turn: tuple[int, int] | None # (index, tokens)
    total_wall_seconds: float | None
    sparkline: str                  # ASCII occupancy trend
    per_tool_result_tokens: list[dict]  # [{tool, calls, result_tokens}] — exact
                                        # result-token share, SEPARATE from cost
    warnings: list[str]             # degraded-input notices (no usage, empty, etc.)
```

### `active_path()` helper (pi_session_lib)

```python
def active_path(path: str) -> tuple[list, list, int]:
    """Extract the live active conversation path and its post-compaction segment.

    Returns (full_path, active, compaction_count):
      full_path        — root→leaf path through the tree (founding prompts live here)
      active           — segment after the last compaction boundary
      compaction_count — number of compaction entries on the path
    Factored from _leaf_path + _active_segment for reuse by turn_profile
    without importing many private helpers.
    """
    full_path = _leaf_path(_load_jsonl(path))
    active, compaction_count = _active_segment(full_path)
    return full_path, active, compaction_count
```

## CLI surface

```
python3 src/turn_profile.py --session <path> [--json] [--rollup]
                            [--recent N] [--cwd <dir>]
```

- `--session <path>` (required) — Pi session JSONL.
- `--json` — emit the full `ProfileResult` as one JSON object on stdout; suppress
  the text report.
- `--rollup` — collapse consecutive assistant turns under each user prompt into
  one `HumanTurnRollup` row (loop length, aggregate growth/cost/duration/tools).
- `--recent N` — show only the last N turns in the text report (summary still
  covers all).
- Exit 0 always (diagnostic, never-raise on malformed input — degrade with a
  best-effort message, matching the project's "hooks/CLIs must never raise"
  convention).

## Output format

### Default text (2–3 lines per turn, SI)

```
T  12  asst  ctx 147k ▲4.2k  out 0.3k  $0.04  cache 96%  read×2 bash
       └ fed by ≈3.8k tool(read:file.py) · 0.4k asst · Δ 96% cached
       ⚠ large bash output (2.1k) · parallel reads
```

- **Line 1 — always**: turn index, role, `ctx` (= `occupancy`, exact, reconciles
  with autocompactor), `▲Δ` growth (since-previous-call), `out` (exact), `$cost`
  (exact), `cache %` (exact, of `pre_call_tokens`), tools emitted this turn.
- **Line 2 — when meaningful**: `fed by` composition (estimated, `≈`) of what
  entered the window for this call + cache share of the delta.
- **Line 3 — when any flag fires**: `⚠` flagged observations.

Omitted lines collapse (hence "2–3 lines"). Token counts via `policy._fmt_tokens`.

### Rollup row (`--rollup`)

```
H  3  human  8 turns  ctx 23k→71k (+48k)  $0.41  wall 4.2m  read×9 bash×3
```

One row per user prompt, aggregating its agentic loop.

### Summary block (all four dimensions)

```
══════════════ SESSION PROFILE ══════════════
134 turns (8 human) · peak 150k · grew 5k→150k · wall 47m
ctx trend ▁▂▂▃▄▅▆▇█▇▆▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅
Cost $4.71 · input $1.92 · output $0.88 · cache-read $1.91 (92% cached)
Composition @ peak ≈ 6k skills · 89k system+tools · 110k tool (78% stale)
Biggest growth T6 (+11k, read) · Biggest tool output T48 (8k bash)
Reclaimable ≈ 80k (150k − 70k floor) · 3 redundant reads · 4 oversized outputs

Per-tool result tokens   calls   result-tok   share
read                       62       48k         45%
bash                       41       52k         49%
grep                       28        6k          6%
edit                       14        —          —
```

- Sparkline: compact Unicode block chars scaled across the turn axis (plotted
  from `occupancy`, not `pre_call_tokens`, so it reconciles with `evaluate`).
- **Per-tool table is result-token share, NOT cost attribution** (review P2#4):
  the real cost of a tool result is the repeated later `input`/`cacheRead` spend
  it drives until compaction, which a single-call result-share cannot capture.
  The table reports exact per-tool result tokens + their share of total result
  tokens; total/cumulative $ cost is shown separately and exactly in the summary
  line above. The two are never blended into a misleading per-tool "cost".

### JSON (`--json`)

One `ProfileResult` object on stdout (no trailing text).

## Flags (waste / behavior detection)

Computed per turn; thresholds read from config (reuse `AUTOCOMPACTOR_*` namespace,
new keys with sensible defaults):

| flag | condition | default threshold |
|---|---|---|
| `large-output` | a tool result fed to this call exceeds N tokens | 5k |
| `redundant-read` | a `read`/`grep` path repeats one seen in a recent prior turn | within last 10 turns |
| `think-bloat` | thinking tokens > N× output tokens | 5× |
| `error-retry` | an error tool result, or 2+ consecutive error turns | — |
| `parallel-tools` | turn emitted >1 `toolCall` | — |
| `idle-gap` | wall-clock since previous turn > N min | 30m |

## Testing

- **Fixtures**: 1–2 anonymized transcripts carved from real Pi sessions
  (small + one with a compaction boundary + oversized outputs), committed under
  `tests/fixtures/`. Plus a degenerate fixture (no usage blocks) and an empty one.
- **Reconciliation (review P1#1)**: assert `occupancy == _usage_context(usage)`
  exactly and `peak_ctx == max(occupancy)`; assert it equals what
  `pi_session_lib.analyze()` would report as `context_tokens` for the same
  session (the headline must not diverge from autocompactor).
- **Attribution tests**: assert `pre_call_tokens == input + cacheRead + cacheWrite`
  exactly; assert `fed_by` covers ALL interval entries (toolResult, bashExecution,
  custom, user, prior assistant output) since the previous assistant call —
  including a parallel-tool-call fixture with out-of-order results; assert
  unmatched toolCallId results surface as `tool(unknown)`, not dropped.
- **Degenerate profiles (review P2#5)**: no-usage session → `has_usage=False`,
  nullable summary fields, valid `--json` object with a `warnings` entry, exit 0;
  empty active segment likewise.
- **Estimate labelling**: assert estimated fields round-trip through chars/4 and
  text output carries `≈`.
- **Parity**: assert `active_path()` returns the same active segment `analyze()`
  walks; assert `analyze_active_prefix(full, full_active, window)` matches
  `analyze()` for the full segment (review P2#3 helper equivalence).
- **Format**: assert SI formatting reuses `policy._fmt_tokens` (no drift);
  assert rollup grouping matches user-prompt boundaries; assert the per-tool
  table carries no per-tool "cost" column (P2#4).
- **Never-raise**: malformed/empty/missing session → exit 0 with a best-effort
  message, no traceback (project convention).
- Run `python3 -m pytest tests/ -q` (baseline 100 cases) plus the new suite.

## v1 scope vs. stretch

**v1 (this spec):** per-turn profiler + summary + sparkline + per-tool cost
table + `--json` + `--rollup` + flags + `active_path()` helper.

**Stretch (explicitly deferred, not built unless re-scoped):**
- `--watch` live tail mode (profile a running session).
- `--compare` two sessions/profiles.
- "compact-here what-if" projection per turn (reclaimable at each turn + optimal
  compact-point marker) — *deferred at the user's direction for v1*.
- Feeding ideal-compact-points back into the backtester.
- Per-tool cost as a true attribution (needs per-call cost splitting, not in
  transcript).

## Risks & open questions

*GPT-5.5 review (rev 2) — the load-bearing attribution risks below were the
review's P1/P2 findings and are now resolved in the spec; they remain the
highest-risk areas to get right in implementation and are pinned by tests.*

- **Context-figure divergence (review P1#1, resolved)**: `occupancy` (=
  `totalTokens` via `_usage_context`) is used for all headline figures so they
  reconcile with `evaluate`/`contextState`; `pre_call_tokens` is the precise
  per-call window-at-start, kept separate and never used for peak/reclaimable.
  Δctx is labelled since-previous-call, not content growth.
- **Fed-by misattribution (review P1#2, resolved)**: fed-by is the full interval
  (all roles + prior assistant output), `toolCallId` demoted to optional name
  resolver; tests cover parallel/out-of-order/`bashExecution`/`custom`.
- **Composition @ peak (review P2#3, resolved)**: needs `analyze_active_prefix`;
  if factoring it cleanly proves non-trivial, composition-at-peak is deferred to
  stretch (the per-turn figures stand on their own without it).
- **Per-tool cost misleading (review P2#4, resolved)**: result-token share and
  total cost are reported separately, never blended into per-tool "cost".
- **Degenerate profiles (review P2#5, resolved)**: no-usage / empty-segment paths
  are explicitly defined with nullable fields + `warnings`.
- **Token-estimate drift**: chars/4 is the project-wide heuristic; staying on it
  keeps the profiler consistent with `context_composition`, but it can mislead on
  code-heavy vs prose-heavy content. No tokenizer in v1.
- **Shim naming**: `src/turn_profile.py` (not `src/profile.py`) to avoid any
  shadowing of the stdlib `profile` module.
- **Refactor risk to `analyze()`**: both new helpers are factored FROM `analyze()`
  with no behavior change; the parity test asserts `analyze_active_prefix` on the
  full segment equals `analyze()`, and the 100-case baseline must stay green.
