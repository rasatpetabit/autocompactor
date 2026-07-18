# Plan: Post-compact task continuity (progress ledger + hard resume)

**Spec:** `docs/masterplan/post-compact-task-continuity/spec.md`  
**Goals:** `goals.md` (G1–G5)  
**Approach:** A — mechanical progress ledger + gated hard resume

## Wave 0 — Progress core (Python)

### T1 — `progress_lib` (all extractors)
**Files:** `src/autocompactor/progress_lib.py` (create), `tests/test_progress_lib.py` (create), `tests/fixtures/progress/*`  
**Goals:** G1, G2, G3  
**Verify:** `python3 -m pytest tests/test_progress_lib.py -q`  
`ProgressHit`, rank bands, mode (`wait`|`code`), masterplan state.yml extractor (read-only), affinity/confidence/brief (RESUME header, files≤8, verify≤3), coord `task_only`, plan_file default off, todo best-effort (may be empty), never-raise wrappers. Fixtures: active masterplan YAML, affinity-true/false cues.

## Wave 1 — Wire brain + bridge

### T2 — artifacts + resolve_next_step + config
**Files:** `src/autocompactor/artifacts.py`, `src/autocompactor/transcript_lib.py`, `config.json`, `tests/test_open_work.py`  
**Goals:** G1, G3, G4  
**Verify:** `python3 -m pytest tests/test_progress_lib.py tests/test_open_work.py -q`  
`progress_position` in PRIORITY/extract/merge/digest (≤400 token section). Rewrite `resolve_next_step` (wait mode first). Config keys + env. open_work regressions green.

### T3 — pi_bridge prepare/reinject
**Files:** `src/autocompactor/pi_bridge.py`, `tests/test_pi_bridge.py`  
**Goals:** G1, G2, G4  
**Verify:** `python3 -m pytest tests/test_pi_bridge.py -q`  
Prepare stages progress with cwd; content-free telemetry. Reinject: `progressResume`, PLAN POSITION, eligibility gates.

### T4 — pi_session_lib todo best-effort
**Files:** `src/autocompactor/pi_session_lib.py`, `tests/test_pi_session_lib.py`  
**Goals:** G1, G5  
**Verify:** `python3 -m pytest tests/test_pi_session_lib.py tests/test_progress_lib.py -q`  
Fill `st.todos` if tool shapes allow; otherwise leave empty with honest tests.

## Wave 2 — TS hard resume + docs

### T5 — Shim hard-resume eligibility + anti-thrash
**Files:** `src/pi/autocompactor.ts`, `tests/shim_wait_resume.test.ts`  
**Goals:** G1, G2, G3  
**Verify:** `node --test tests/shim_wait_resume.test.ts`  
Reuse `flushAutoResume`. Wait path unchanged. Autonomous coding only when eligible. Cooldown → advisory.

### T6 — Docs + full suite gate
**Files:** `HANDOFF.md`, `README.md`  
**Goals:** G5  
**Verify:** `python3 -m pytest tests/ -q` (and `PI_SMOKE=1 bash tests/smoke_test_pi.sh` when safe)  
Document knobs; note waiting-state coexistence.

## Dependency graph

```
T1 → T2 → T5 → T6
T1 → T3 → T5
T1 → T4
```

## Notes for implementers

- Never write masterplan/coord state.
- Never log brief text in telemetry.
- Wait-shaped beats coding progress always.
- Missing cwd ⇒ no masterplan/coord hard resume.
- Prefer surgical edits; do not rewrite pi_bridge/transcript_lib wholesale.
