# autocompactor

Smarter, earlier, cheaper compaction for Claude Code. Two cooperating hooks:

```
UserPromptSubmit ─→ context_monitor.py ──┐  (watches occupancy + boundaries,
                                         │   recommends /compact, stages
                                         │   tailored instructions)
                                         ▼
PreCompact ──────→ precompact_analyzer.py   (backs up transcript, injects
                                             customInstructions into the
                                             compaction — manual OR auto)
                              │
                    transcript_lib.py        (shared JSONL parsing,
                                              phase detection, instruction builder)
                    stats.py                 (local telemetry -> events.jsonl)
                    analyze_corpus.py        (offline backtester + --events aggregator)
```

## Why this shape

Hooks cannot invoke `/compact` — `PreCompact` only fires once a compaction
is already happening. So the system splits into an **advisor** (monitor)
and an **enricher** (analyzer). The economics it exploits: compaction cost
scales with the context being summarized, and every token of bloat is
re-paid as input on every subsequent turn. Compacting at ~40–50% occupancy
at a task boundary is dramatically cheaper than autocompact firing near
the ceiling — and the summary is better because the instructions are
generated from what actually happened in the session (files edited,
TodoWrite state, recent errors, last task statement).

## Install

1. Copy the three `.py` files to `~/.claude/hooks/` and make them
   executable (`chmod +x ~/.claude/hooks/*.py`).
2. Merge into `~/.claude/settings.json` (or a project's
   `.claude/settings.json`):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          { "type": "command",
            "command": "python3 ~/.claude/hooks/context_monitor.py" }
        ]
      }
    ],
    "PreCompact": [
      {
        "matcher": "manual",
        "hooks": [
          { "type": "command",
            "command": "python3 ~/.claude/hooks/precompact_analyzer.py" }
        ]
      },
      {
        "matcher": "auto",
        "hooks": [
          { "type": "command",
            "command": "python3 ~/.claude/hooks/precompact_analyzer.py" }
        ]
      }
    ]
  }
}
```

3. Run `/hooks` inside Claude Code to verify registration.

## Tunables (environment variables)

| Variable                  | Default | Meaning                                  |
|---------------------------|---------|------------------------------------------|
| `AUTOCOMPACTOR_WINDOW`    | 200000  | Model context window in tokens. Set to 1000000 for 1M-context models. |
| `AUTOCOMPACTOR_SOFT_PCT`  | 0.40    | Recommend at this occupancy *if* a boundary signal is present. |
| `AUTOCOMPACTOR_HARD_PCT`  | 0.65    | Recommend unconditionally at this occupancy. |
| `AUTOCOMPACTOR_COOLDOWN`  | 25000   | Min tokens of growth between recommendations. |
| `AUTOCOMPACTOR_STALE_FRAC`| 0.50    | Stale-tool-output fraction that counts as a boundary signal. |
| `AUTOCOMPACTOR_POST_FLOOR`| 70000   | Estimated post-compaction context (measured ~69k median here). A compaction can only reclaim what sits above this. |
| `AUTOCOMPACTOR_MIN_SAVINGS` | 30000 | Min estimated reclaim (context − POST_FLOOR) to recommend; below it a compaction stalls 30-60s for almost nothing. |
| `AUTOCOMPACTOR_MAX_FULL_PARSE_MB` | 8 | Above this transcript size, parse only the active segment after the last compaction boundary (bounds worst-case hook latency). |
| `AUTOCOMPACTOR_ARTIFACT_BUDGET` | 1500 | Token budget for the post-compaction artifact digest. |
| `AUTOCOMPACTOR_LLM`       | unset   | `1` = PreCompact also runs `claude -p --model haiku` over the transcript tail for a smarter must-preserve digest. Adds latency and its own (small) token cost. |

## Boundary signals detected

- `git commit` executed in the recent window
- test-suite success markers in recent tool output (`N passed`, etc.)
- all TodoWrite items completed
- ≥50% of tool-result bytes in context are older than the recent window

## Billing note

The recommendation path costs nothing — it's local Python parsing the
transcript JSONL (context occupancy is read from the `usage` block of the
last assistant message, the same technique ccusage uses). The compaction
itself is still a model call billed/quota'd normally; this system just
makes each one smaller, earlier, and better targeted. The optional
`AUTOCOMPACTOR_LLM=1` digest is the only feature that spends tokens.

## Caveats

- The transcript JSONL schema is not a stable public API. Pin your Claude
  Code version; re-run the included smoke test after upgrades.
- `transcript_path` has had reported bugs in some Claude Code versions
  (empty string on PreCompact). Both hooks degrade gracefully: backup and
  analysis are skipped, nothing breaks.
- Occupancy is an estimate; it doesn't account for the system prompt
  share that never appears in usage deltas, so treat thresholds as
  approximate and tune to taste with `/context` as ground truth.
- On subscription plans this saves *quota*, not dollars; on API billing
  it saves both.

## Smoke test

```bash
echo '{"session_id":"s1","transcript_path":"/path/to/real/session.jsonl","cwd":"'$PWD'","hook_event_name":"UserPromptSubmit","prompt":"x"}' \
  | python3 ~/.claude/hooks/context_monitor.py
```

Point it at a real transcript under `~/.claude/projects/` and confirm it
emits JSON (or exits silently below threshold).
