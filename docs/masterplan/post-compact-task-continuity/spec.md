# Spec: Post-compact task continuity (progress ledger + hard resume)

**Slug:** `post-compact-task-continuity`  
**Date:** 2026-07-17  
**Status:** design approved (brainstorm)  
**Complexity:** high  
**Repo:** `/srv/dev/ras/autocompactor`

## Problem

After an autocompactor compaction, sessions often **lose multi-step plan position**
and/or **resume the wrong next step**. Existing machinery (founding-goal
restatement, mechanical artifacts, `NEXTSTEP=autonomous`, waiting-state
`open_work`) preserves goals and background waits, but does **not** mechanically
re-enter the live multi-step unit of work (especially masterplan/wave tasks).

### Failure modes in scope

1. **Multi-step task derail** — agent restarts earlier steps, redoes finished
   work, or forgets wave/task/files/verify after compact.
2. **Weak next-step recovery** — `resolve_next_step` falls through to a stale
   `last_user_task` (or empty todos) instead of disk-backed plan position.

### Out of scope

- Changing compact *timing* thresholds (`SOFT_PCT`, dormant gates, etc.).
- Writing or mutating masterplan / coord state (read-only observer).
- LLM “what should I do next?” calls.
- Redesigning the coord blackboard schema.
- Replacing the waiting-state resume path (it stays; this design must not
  regress it).

## Goals

1. After compact mid multi-step work, the session **hard-resumes** the same
   unit of work via a structured task brief (not the founding topic alone).
2. Progress is taken from **generic progress surfaces**, preferring disk over
   summarizer goodwill.
3. Hard resume is **safe**: affinity + confidence gates prevent stale-plan
   hijack; wait-shaped work still wins over coding resume.
4. Telemetry remains **content-free** (counts/ids/ranks only — never brief text).

## Non-goals (explicit)

- Guaranteeing correct resume when **no** progress surface exists.
- Auto-finishing masterplan runs or auto-waiving blockers.
- Cross-repo progress discovery beyond prepare `--cwd` and standard coord home.

## Background (current stack)

| Mechanism | Role today | Gap |
|---|---|---|
| Founding goal + `initial_prompts` | Survives unlimited passes | Goal ≠ current task |
| Artifacts digest | Corrections, errors, files, open_work | No plan position class |
| `resolve_next_step` | todo → wait → next_on_success → last_user_task → correction | Todos largely unfilled on Pi; no disk plan position |
| Waiting-state resume | Wait-aware poll, open_work extraction | Coding multi-step still derails |
| `NEXTSTEP=autonomous` | `triggerTurn` with staged step | Wrong step is forced derail |

Related prior work: `docs/superpowers/specs/2026-07-17-waiting-state-resume-design.md`
(implemented). This spec **extends** that stack; it does not replace it.

## Design overview

**Approach A — Progress ledger + hard resume**

At prepare time, mechanically extract ranked progress hits from pluggable
surfaces, stage a first-class `progress_position` artifact, rewrite next-step
resolution, and on reinject either:

- **autonomous hard resume** (`triggerTurn` with structured brief), or
- **advisory only** (digest + notice, no triggerTurn),

depending on mode, affinity, confidence, and config.

```
pre-compact transcript + ctx.cwd
        │
        ▼
progress_lib.extract_all()     # mechanical only
        │ ProgressHit (mode + rank + confidence + affinity)
        ▼
artifacts.progress_position  → disk (section budget ≤400 tokens)
resolve_next_step()          → staged_next_step / source
preservation instructions    → PLAN POSITION block
        │
        ▼ compact
reinject digest + nextStep + progressResume
        │
        ▼
flushAutoResume: wait path unchanged | code hard resume | advisory
```

## Architecture

### New module

`src/autocompactor/progress_lib.py` — harness-agnostic extractors, ranking,
affinity, brief formatting. Never raises into the prepare path (callers
catch/degrade).

### Touched modules

| File | Change |
|---|---|
| `progress_lib.py` | **new** — extract/rank/format |
| `artifacts.py` | `progress_position` in PRIORITY / extract / merge / digest |
| `transcript_lib.py` | `resolve_next_step` priority rewrite; optional PLAN POSITION in preservation helpers |
| `pi_session_lib.py` | Best-effort todo fill if tool shapes allow (explicit may-be-empty) |
| `pi_bridge.py` | prepare stages progress; reinject surfaces fields; content-free telemetry |
| `src/pi/autocompactor.ts` | honor `progressResume` + eligibility; reuse `flushAutoResume` |
| `config.json` | new knobs (below) |
| tests | `test_progress_lib.py`, bridge + shim regressions |

### Non-goals for module layout

Do not fold extractors into `transcript_lib.py` (already large). Do not invent
a second TS resume engine.

## Progress model

### `ProgressHit`

```python
{
  "surface": "masterplan" | "coord" | "todo" | "plan_file" | "open_work",
  "key": str,              # stable merge key, e.g. masterplan:<slug>:t<id>
  "mode": "wait" | "code", # orthogonal to rank
  "rank": int,             # higher = better coding position within mode=code
  "confidence": float,     # 0..1
  "affinity": bool,        # session tied to this progress surface
  "summary": str,          # one-line
  "brief": str,            # multi-line agent-facing resume brief
  "files": list[str],      # ≤8
  "verify": list[str],     # ≤3
  "resource_ids": list[str],
  "status": str,           # pending|in_progress|blocked|waiting|…
  "mtime": float,
}
```

### Mode vs rank (adversarial amendment A2)

- **`mode=wait` always outranks `mode=code`** in `resolve_next_step`,
  regardless of numeric rank.
- Numeric `rank` only orders coding (or only wait) candidates within their mode.

### Rank bands (coding surfaces)

| Band | Surface | Notes |
|-----:|---------|--------|
| 100–90 | masterplan `state.yml` | Best task-level detail |
| 85–80 | open_work waiting | Emits `mode=wait` (not coding rank competition) |
| 75–70 | coord blackboard | Only when task-level payload exists (`PROGRESS_COORD=task_only` default) |
| 60–50 | transcript todos | Best-effort; may be empty on Pi today |
| 40–30 | plan files | Default **off** (`PROGRESS_PLAN_FILES=false`) |
| ≤20 | last_user_task / correction | Existing fallbacks |

### Masterplan extractor

- Discover `docs/masterplan/*/state.yml` under prepare `cwd` (and, if cwd is a
  linked worktree, the main checkout’s bundle path when resolvable).
- Ignore `status in {archived, done}` and `phase == archived`.
- Prefer runs with live affinity signals (below).
- Task pick: first `in_progress`, else first `pending` by `(wave, id)`.
- If all tasks done but run not archived: brief = finish/verify posture, not a
  fake implement step.
- **Read-only** — never write `state.yml` / events.

### Affinity gate (A1) — required for hard resume from masterplan/coord

Hard resume from masterplan or coord requires **at least one**:

1. Transcript/session text mentions slug, `docs/masterplan/<slug>`, or task id, or
2. `cwd` is the run worktree (`.worktrees/<slug>` / branch `masterplan/<slug>`), or
3. Bundle shows live execution (`active_run` non-null and/or recent owner heartbeat).

If affinity is false:

- Still stage `progress_position` into the **digest** (visibility),
- **Do not** set autonomous coding `nextStep` from that hit,
- Fall through to wait/todo/last_user_task chain as appropriate.

Missing/empty `cwd`: refuse masterplan/coord **hard** resume (A6). Note: the
Pi shim already passes `--cwd ctx.cwd` on every bridge call; keep that as a
regression pin.

### Coord extractor (A5)

- Default `PROGRESS_COORD=task_only`: require task-level payload under the job’s
  `tasks/` (or equivalent); wave-only `goal: "wave N"` is **observe-only**
  (telemetry), not nextStep.
- `wave_ok` opt-in allows weak wave-level briefs.
- `off` disables the surface.

### Plan-file extractor (A5)

- Default **off**.
- When on: require checklist markers and fresh mtime; exclude archive-like
  trees (`docs/superpowers/plans/` historical plans) unless mtime is within the
  freshness window.

### Todo surface (A7 honesty)

- Pi today largely does not fill `TranscriptStats.todos` (dormant fields).
- Phase 1 may ship best-effort parse **or** leave empty; success criteria must
  not claim todo-driven resume until tests prove fill rate on real fixtures.

## Next-step priority rewrite

```
1. mode=wait progress_position          → wait brief (NEXTSTEP_WAIT path)
2. open_work waiting_monitor            → existing wait brief (belt + suspenders)
3. mode=code progress_position
     if affinity && confidence ≥ PROGRESS_MIN_CONFIDENCE
        && PROGRESS_RESUME allows
        → structured brief (hard or advisory per config)
     else → do not use as autonomous nextStep; digest still carries position
4. open_work next_on_success
5. last_user_task
6. correction[-1]
```

Source tags (telemetry): `progress:masterplan`, `progress:coord`,
`progress:todo`, `progress:plan_file`, existing open_work/todo/last_user_task tags.

## Artifact / prepare / reinject contract

### PRIORITY

```
initial_prompts, corrections, progress_position, open_work,
error_ledger, working_commands, hex_constants, files
```

### Merge

- `progress_position` is a **single object** (not a list).
- Higher rank (within mode) wins; same `key` refreshes fields from newer extract.
- Position tracks live work — **not** old-wins immutable merge (unlike
  `initial_prompts`).

### Budget (A4)

- `PROGRESS_BUDGET_TOKENS` default **400** for the progress section.
- Cap files ≤8, verify ≤3; remainder as `+N more`.
- Global `ARTIFACT_BUDGET` (1500) unchanged.

### Agent-facing brief (A7)

```
RESUME mid-task — do not restart from scratch.
1) git status + diff on files[] first; keep existing in-scope edits.
2) Continue ONLY this unit: <title/id/wave>.
3) Stay inside files[] scope; run verify[] before claiming done.
4) If blocked, stop and report the blocker — do not invent a new task.
```

### prepare

1. Analyze transcript; extract open_work + existing artifacts.
2. `progress_lib.extract_all(st, cwd=…)`.
3. Merge progress into artifacts; stage next step via rewritten resolver.
4. Append PLAN POSITION to preservation instructions (budget-aware).
5. Log content-free progress fields on the prepare event.

### reinject

| Field | Meaning |
|---|---|
| digest | includes `## PLAN POSITION` when present |
| `nextStep` | brief if hard-resume eligible; short summary if advisory |
| `nextStepSource` | `progress:<surface>` or existing tags |
| `nextStepWait` | true for wait mode (unchanged semantics) |
| `progressResume` | effective `autonomous\|advisory\|off` after gates |
| `openWork` | unchanged |

### Telemetry (A8)

Log only: `progress_surface`, `progress_key`, `progress_mode`,
`progress_rank`, `progress_confidence`, `progress_affinity`,
`progress_resume`, lengths. **Never** brief/summary text.

## TypeScript hard-resume behavior

Reuse `flushAutoResume` — no second engine.

| Case | Behavior |
|---|---|
| Wait-shaped | Unchanged wait UI + `NEXTSTEP_WAIT` poll |
| Code + autonomous eligible | `triggerTurn` with brief; status cites `progress:<surface>` |
| Code + advisory / low confidence / no affinity | Advisory notice only |
| No progress hit | Existing NEXTSTEP chain |

### Anti-thrash

If the same `progress_key` was autonomously resumed within
`PROGRESS_RESUME_COOLDOWN_MS` (default: align with compact cooldown or 60s),
force advisory for that cycle and log `progress_resume_throttled`.

### Global gates

- `NEXTSTEP=off` disables all next-step surfacing (including progress).
- `PROGRESS_RESUME=off` disables progress-driven next step only (wait path
  still uses open_work).

## Config

| Key | Default | Env override |
|-----|---------|--------------|
| `PROGRESS_RESUME` | `autonomous` | `AUTOCOMPACTOR_PROGRESS_RESUME` |
| `PROGRESS_MIN_CONFIDENCE` | `0.75` | `AUTOCOMPACTOR_PROGRESS_MIN_CONFIDENCE` |
| `PROGRESS_BUDGET_TOKENS` | `400` | `AUTOCOMPACTOR_PROGRESS_BUDGET_TOKENS` |
| `PROGRESS_PLAN_FILES` | `false` | `AUTOCOMPACTOR_PROGRESS_PLAN_FILES` |
| `PROGRESS_COORD` | `task_only` | `AUTOCOMPACTOR_PROGRESS_COORD` |
| `PROGRESS_AFFINITY` | `true` | `AUTOCOMPACTOR_PROGRESS_AFFINITY` |
| `PROGRESS_RESUME_COOLDOWN_MS` | `60000` | `AUTOCOMPACTOR_PROGRESS_RESUME_COOLDOWN_MS` |

All via existing `config_lib` single-namespace + `AUTOCOMPACTOR_*` pattern.

## Failure modes

| Failure | Handling |
|---|---|
| Stale masterplan, no affinity | Digest only; no hard resume |
| Wait + coding progress | Wait wins |
| Empty cwd | No masterplan/coord hard resume |
| Coord wave-only | Skip nextStep under `task_only` |
| Todo parse missing | Empty surface; no fake priority |
| Brief too long | Truncate at section budget; keep RESUME header + id |
| Extractor exception | Never-raise; fall back to pre-existing chain |
| Same key re-resume loop | Cooldown → advisory + throttle event |

## Testing

1. **`tests/test_progress_lib.py`** — ranking, mode supremacy, affinity true/false,
   confidence gate, budget cap, coord `task_only`, plan_files off, cwd missing.
2. **Fixtures** — minimal masterplan `state.yml` + Pi JSONL with/without slug
   affinity; wait+masterplan co-presence.
3. **`tests/test_pi_bridge.py`** — prepare stages progress; reinject fields;
   telemetry has no brief text.
4. **Shim tests** — wait still blocks coding `triggerTurn`; eligible progress
   triggers; low confidence does not; cooldown throttles.
5. **Regression** — all `test_open_work` + existing reinject tests green.
6. **Suite bar** — `python3 -m pytest tests/ -q` and
   `PI_SMOKE=1 bash tests/smoke_test_pi.sh` when safe.

## Rollout

1. Land Python extractors + artifacts + resolver + bridge (defaults on with
   affinity).
2. Land TS eligibility wiring; install via `python3 src/install_pi.py` when
   owner-gated.
3. Optional shadow week: compare “would hard-resume” vs affinity failures in
   telemetry.
4. Update `HANDOFF.md` / README tunables table.
5. Owner live mid-wave compact verification (headless harness cannot fully
   prove interactive resume).

## Success criteria

1. Mid-wave compact **with affinity** → autonomous brief names correct task
   id/wave/files; agent instructed not to restart.
2. In-progress plan in repo but session **not** on it → **no** hard resume.
3. Waiting-state path remains wait-shaped (no coding `triggerTurn`).
4. Telemetry never stores brief/summary text.
5. Full pytest + open_work/wait tests + Pi smoke green.
6. (Stretch) Todo-driven resume only claimed once fixtures prove parse fill.

## Adversarial amendments incorporated

| ID | Amendment |
|----|-----------|
| A1 | Affinity gate for masterplan/coord hard resume |
| A2 | `mode` (`wait`/`code`) orthogonal; wait always wins |
| A3 | `PROGRESS_RESUME` + confidence threshold; advisory fallback |
| A4 | ~400 token ceiling on progress artifact section |
| A5 | Coord task-level-or-skip; plan files default off |
| A6 | Session cwd required for hard masterplan/coord resume |
| A7 | Resume-not-restart brief + git-status-first discipline |
| A8 | Content-free telemetry only |

## Assumptions & Open Decisions

| question | decision | rationale | source |
|---|---|---|---|
| Primary failure modes? | Multi-step derail + weak next-step recovery | Owner multi-select in brainstorm | user-confirmed |
| Primary work shape? | Masterplan / wave tasks first | Owner choice; highest leverage | user-confirmed |
| Resume aggression? | Hard plan-position resume | Autonomous structured brief when eligible | user-confirmed |
| Position sources? | Generic progress surfaces | state.yml + coord + todos + plan files (gated) | user-confirmed |
| Design approach? | A: progress ledger + hard resume | Only approach satisfying all four constraints | user-confirmed |
| Adversarial A1–A8? | Accept all | Prevent forced derail / hijack / budget blowups | user-confirmed |
| Architecture module split? | New `progress_lib.py` | Avoid bloating transcript_lib; clear boundary | user-confirmed |
| Wait vs masterplan? | Wait mode always wins | Protects waiting-state incident class | assumed (from prior incident + A2) |
| Default `PROGRESS_RESUME`? | `autonomous` with affinity+confidence | Matches NEXTSTEP default; gates prevent hijack | assumed |
| Default `PROGRESS_MIN_CONFIDENCE`? | 0.75 | High enough to block weak hits; tunable | assumed |
| Coord default? | `task_only` | Fleet jobs often wave-level noise | assumed (fleet sample) |
| Plan files default? | off | Historical plans false-positive risk | assumed |
| Todo fill on Pi? | Best-effort; may ship empty | Parser currently dormant | assumed |
| Bundle path for this design? | masterplan `spec.md` (this file) | Masterplan brainstorm contract | assumed |
| Also mirror under `docs/superpowers/specs/`? | No separate mirror unless plan phase asks | Single source of truth in bundle | assumed |

## References

- Waiting-state design: `docs/superpowers/specs/2026-07-17-waiting-state-resume-design.md`
- Waiting-state plan: `docs/superpowers/plans/2026-07-17-waiting-state-resume.md`
- Core: `src/autocompactor/{transcript_lib,artifacts,pi_bridge,pi_session_lib}.py`
- Shim: `src/pi/autocompactor.ts`
- Config: `config.json` (`NEXTSTEP`, `NEXTSTEP_WAIT`, `ARTIFACT_BUDGET`, …)
