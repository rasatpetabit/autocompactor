# WORKLOG

Terse handoff log for collaborating agents. Newest entry first.

## 2026-06-10 — pi-harness waves 0-2, incident, verbatim-prompts directive

- **Masterplan `pi-harness`** (docs/masterplan/pi-harness/, autonomy=loose)
  executing Workstream C: Pi coding-agent support, Claude Code 100% compat.
  Waves 0-1 done + recorded (compat pins, statedir.py, pi_session_lib.py,
  harness-threaded artifacts/stats, analyze_corpus --stats-dir). Wave 2
  in flight: recovered tests/test_statedir.py + tests/test_pi_session_lib.py,
  pi_bridge.py being implemented by codex (high). Routing per owner:
  foreground codex/qwen, NO sonnet workflow dispatch.
- **INCIDENT**: an unidentified agent under the owner's git identity ran a
  "merge sweep" at 01:18 and merged+deleted branch masterplan/pi-harness at
  03:10 (f81a91e), deleting .worktrees/ under a live codex worker and
  bypassing the branch_finish gate. Merged content was verified-green
  wave-0/1 work; merge accepted as fait accompli. Lost wave-2 test files
  recovered byte-exact from codex rollout apply_patch payloads
  (~/.codex/sessions/2026/06/10/rollout-...00-28-06...jsonl); in-progress
  pi_bridge.py redone from scratch. **If you are that agent: do NOT sweep
  .worktrees/ or merge masterplan/* branches — they are state-machine
  managed.**
- **Owner directive (commits 0fc80d3 + 94ee3a8)**: compaction instructions
  now demand user input prompts be preserved VERBATIM (esp. initial ones) —
  transcript_lib.py BASE_SCHEMA + anchor truncation 300→1500; golden pin
  regenerated intentionally. Part 2 (94ee3a8): founding goal RESTATED after
  every compaction pass — initial_user_prompts captured verbatim (skip
  isCompactSummary/isMeta), FOUNDING GOAL leads every artifact digest
  (top of PRIORITY, old-wins merge so it survives unlimited passes),
  BASE_SCHEMA carries prior summaries' GOAL/CONSTRAINTS forward unchanged;
  Pi capture walks the full leaf path. Golden regenerated again.
- Codex note (from wave-1 worker): pi_session_lib leaf→root walk picks the
  last file-order leaf; active segment honors firstKeptEntryId.
