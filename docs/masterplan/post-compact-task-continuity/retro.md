# Retro — post-compact-task-continuity

## Outcome

Shipped a mechanical **progress ledger** + gated hard resume so post-autocompactor
sessions re-enter multi-step (especially masterplan) work instead of a stale
founding topic. All 6 plan tasks done; goals G1–G5 attested achieved.

## What worked

- Disk-first extractors (`progress_lib`) over summarizer goodwill.
- Adversarial amendments A1–A8 (affinity, wait supremacy, confidence, budget)
  prevented forced derail designs.
- Reuse of waiting-state `flushAutoResume` — no second resume engine.
- Tests first: 15 progress_lib + open_work regressions + bridge + 7 bun shim.

## Friction

- `dispatch_fabric` / skynet_edit_files refused **new** files without
  `create_files` — implemented wave 0 inline in the worktree.
- Spec/plan adversary lane only saw empty git diffs; design review was
  in-session + owner AUQ (gates recorded as skipped with digest evidence).
- Full `pytest` still has 4 pre-existing chonkie/llm_digest failures on main;
  progress-related suite is green (61 + 225 excl. those).

## Follow-ups

- Wire `create_files` into fabric prepare for new-file tasks (agent-dispatch).
- ~~Optional live mid-wave compact smoke~~ — bridge prepare/reinject exercised
  on a live session copy (2026-07-18); full mid-wave actuate still needs a
  long natural session.
- ~~Fill Pi `st.todos`~~ — defensive TodoWrite-shaped parser + flags shipped
  (2026-07-18); stock fixtures remain honest-empty (no live Pi todo tool yet).
