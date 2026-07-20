# Retro — post-compact-task-continuity

## Outcome

Shipped a mechanical **progress ledger** + gated hard resume so post-autocompactor
sessions re-enter multi-step (especially masterplan) work instead of a stale
founding topic. Completed all six plan tasks; goals G1–G5 are attested as achieved.

## What worked

- Disk-first extractors (`progress_lib`) over summarizer goodwill.
- Adversarial amendments A1–A8 (affinity, wait supremacy, confidence, budget)
  prevented designs that could force derailment.
- Reuse of waiting-state `flushAutoResume` — no second resume engine.
- Test-first coverage: 15 progress_lib tests, open_work regressions, bridge tests,
  and 7 Bun shim tests.

## Friction

- `dispatch_fabric` / skynet_edit_files refused **new** files without
  `create_files` — implemented wave 0 inline in the worktree.
- Spec/plan adversary lane only saw empty git diffs; design review was
  in-session + owner AUQ (gates recorded as skipped with digest evidence).
- Full `pytest` still has 4 pre-existing chonkie/llm_digest failures on main;
  progress-related suite is green (61 + 225 excl. those).

## Follow-ups

- ~~Wire `create_files` into fabric prepare~~ — agent-dispatch task 49 S-B
  (`create_files` defaults true on `dispatch_task` gateway edit; masterplan
  `dispatch-wave` auto-opts when targets missing). Schema text aligned 2026-07-18.
- ~~Mid-wave progress actuate smoke~~ — `test_midwave_prepare_reinject_progress_hard_resume`
  + `smoke_test_pi.sh` step 7 (prepare→reinject with active masterplan cwd).
- ~~Fill Pi `st.todos`~~ — defensive TodoWrite-shaped parser + flags shipped
  (2026-07-18); stock fixtures remain honest-empty (no live Pi todo tool yet).
