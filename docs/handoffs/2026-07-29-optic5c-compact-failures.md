# Handoff: autocompactor failures from optic-5c build session (2026-07-29)

**Source session:** pi @ `/srv/dev/yanos-project/yanos-os` (build `Y260728-170946`)
**Trigger:** user ran `/compact` after the optic-5c image work was already finished;
compact dump + nextstep were wrong in several key areas. User rejected a
"here's how compact works" reply and demanded the failures be named.

## Founding context (what the session actually did)

1. Built optic-5c via yanos-builder → `Y260728-170946`.
2. BitBake 5652/5652 succeeded in ~29m; flashable WIC produced.
3. Catalog false-failed: `capture.py` lacked `.cpio.zst` while
   `INITRAMFS_FSTYPES=cpio.zst` (yanos-rauc-image.inc). Fixed in yanos-builder
   commit `a6de087`; revalidated catalog to **succeeded**.
4. User accidentally pasted an AGENTS.md cleanup line into this session, then
   **explicitly withdrew it** ("disregard… resume optic-5c").
5. At compact time the build was already terminal success. Compact still armed
   a 20× 60s wait-poll loop on `open_work:waiting_monitor`.

## Failures exposed (fix these)

### F1 — Wait open_work not invalidated by terminal success
**Symptom:** After success, digest still had `WAITING: Y260728-170946` and
`[autocompactor-nextstep]` kept injecting poll turns (remaining 19, 18…).
**Where:**
- `src/autocompactor/transcript_lib.py` — `extract_open_work_from_text`,
  `merge_open_work`, `resolve_next_step`, `format_open_work_brief`
- `src/autocompactor/progress_lib.py` — `extract_open_work_progress`
  (lifts waiting_monitor at confidence 0.95, status="waiting", no terminal check)
- `src/pi/autocompactor.ts` / installed `~/.pi/agent/extensions/autocompactor.ts`
  — `scheduleWaitPoll` / `fireWaitPoll` re-inject frozen brief; no live
  success detection; no clear on agent reporting `status: succeeded`.
**Required behavior:**
- If transcript already contains terminal status for the primary resource
  (`status : succeeded` / `failed` / Tasks Summary all succeeded / catalog
  succeeded), do **not** emit `waiting_monitor` / do not schedule wait polls.
- Prefer: on prepare, drop or demote wait items whose resource_id is terminal
  in later assistant/tool text.
- TS: if a poll turn's tool/agent result shows terminal status, call
  `clearWaitPoll()` immediately and stop re-injection (do not burn remaining
  slots on a finished job).
- Optional belt: one `yanos-builder show <id>` (or generic status probe) before
  arming wait polls when monitor_cmds look like yanos-builder — fail open to
  wait only if probe fails.

### F2 — Founding goals polluted by duplicates + withdrawn mispaste
**Symptom:** FOUNDING GOAL listed:
- Build a working optic-5c image
- Build a working optic-5c image  (duplicate)
- Wait, there's a ~/AGENTS.md… clean that mess…  (mispaste, later withdrawn)
**Where:** initial_user_prompts / initial_prompts collection in
`pi_session_lib.py` / artifacts.
**Required behavior:**
- Deduplicate near-identical founding prompts.
- Drop or demote prompts that a later user message explicitly withdraws
  ("disregard", "wrong session", "resume previous X", "ignore that paste").
- Do not treat a one-line accidental paste as co-equal founding goal with the
  long-running workstream.

### F3 — USER CORRECTIONS inverted / over-eager
**Symptom:** The AGENTS.md mispaste was filed under
`USER CORRECTIONS (verbatim, still binding)` — i.e. promoted into a permanent
constraint after the user withdrew it.
**Where:** `CORRECTION_RE` + correction extraction in `transcript_lib.py` /
`pi_session_lib.py`; artifacts section title in `artifacts.py`.
**Required behavior:**
- Corrections require a clear "do differently" shape, not any "Wait," line.
- Explicit withdraw/disregard of a prior user message must **remove** that
  message from corrections and founding goals, not freeze it as binding.
- Section label "still binding" must not apply to withdrawn content.

### F4 — Cancel vs complete race messaging
**Symptom:** UI showed `Error: Compaction cancelled` then
`compaction completed` for the same run.
**Where:** TS intercept of native compact + cancel path in `autocompactor.ts`.
**Required behavior:** Don't surface a hard Error for an intentional
intercept/cancel of the *native* path when the *enriched* compact continues
and succeeds. Operator-facing status should be one coherent outcome.

### F5 — Wrapup phase arming a wait loop
**Symptom:** Accounting said `phase: wrapup` while nextstep still scheduled
wait-monitor polls.
**Required behavior:** In wrapup (or when last task is complete / no live
wait), nextstep must not be wait-shaped. Prefer empty nextstep or a short
"work complete; flash path is …" if artifacts show success.

## Acceptance tests (must add / extend)

1. **test_open_work:** transcript with wait language for build ID X, then later
   assistant/tool text `status : succeeded` for X → no waiting_monitor in
   open_work; resolve_next_step is not `open_work:waiting_monitor`.
2. **test_open_work / founding:** founding prompts with duplicate + withdrawn
   mispaste → only the real goal remains.
3. **corrections:** "Wait, … AGENTS.md" then "disregard, resume optic-5c" →
   AGENTS.md line not in corrections digest.
4. **shim_wait_resume:** after a poll result containing terminal success,
   waitPoll is cleared (no further injects).
5. Fixture modeled on this session's compact dump shape if useful
   (`tests/fixtures/…`).

## Non-goals
- Do not rebuild optic-5c; image work is done.
- Do not expand scope into yanos-builder (cpio.zst fix already landed a6de087).
- Do not modify Pi core without ALLOW_PI_MODIFICATION; extension + Python core only.

## Verify
```bash
cd /srv/dev/ras/autocompactor
python3 -m pytest tests/test_open_work.py tests/test_progress_lib.py -q
# if TS tests wired:
# npm test / node --test tests/shim_wait_resume.test.ts
python3 src/install_pi.py   # redeploy extension after TS changes
# manual: compact a synthetic session with terminal success; confirm no wait polls
```

## Install note
Live extension is `~/.pi/agent/extensions/autocompactor.ts` (install copies from
`src/pi/autocompactor.ts` with bridge path baked). Edit source under this repo,
then reinstall; do not only patch ~/.pi.

## User intent for this session
"seed a new session to fix the several autocompactor issues we exposed"
