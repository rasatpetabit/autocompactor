# Plan: waiting-state resume after compaction

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After compaction (or any idle pause), autocompactor must not forget
an in-flight "waiting for background work" state — preserve it mechanically,
resume with the right next step, and re-wake polls without requiring the user
to type `status?`.

**Architecture:** Extend the Python brain (`transcript_lib` / `pi_session_lib` /
`artifacts` / `pi_bridge`) to extract and stage **open work** (wait monitors +
on-success handoffs). Rewrite `resolve_next_step` priority so live waits beat
stale `last_user_task`. Make the TS shim wait-aware: autonomous next-step
becomes a scheduled poll for wait-shaped steps, and idle actuate notices
render immediately instead of only on the next user prompt.

**Tech stack:** Python 3 (pytest), Pi extension TypeScript (node:test /
existing smoke), `config.json` single-namespace + `AUTOCOMPACTOR_*` env.

**Design doc:** `docs/superpowers/specs/2026-07-17-waiting-state-resume-design.md`

**Incident pin:** session `019f7130-6e24-7ad5-b7a1-3d336e79ac88`, backup
`~/.autocompactor/pi/backups/2026-07-17T17-47-07-172Z_019f7130-…-20260717-114104-actuate.jsonl`

---

## File map

| File | Change |
|---|---|
| `src/autocompactor/transcript_lib.py` | `open_work` on `TranscriptStats`; extract helpers; preservation seeds; `resolve_next_step` priority; structured wait brief |
| `src/autocompactor/pi_session_lib.py` | Fill `open_work` during analyze; `last_user_task` hygiene (base64 strip, trivial-ping ignore) |
| `src/autocompactor/artifacts.py` | New `open_work` class in PRIORITY / extract / digest |
| `src/autocompactor/pi_bridge.py` | Stage + reinject `openWork` / wait metadata; log source tags only |
| `src/pi/autocompactor.ts` | Wait-aware `flushAutoResume`; poll timer; idle-immediate status delivery |
| `config.json` | `NEXTSTEP_WAIT`, `WAIT_POLL_S`, `WAIT_POLL_MAX` |
| `tests/test_open_work.py` (new) | Extraction, priority, hygiene, brief shape |
| `tests/test_pi_bridge.py` | prepare/reinject surface open work + nextStepSource |
| `tests/shim_prepare.test.ts` / new wait-path test | Wait path does not immediate-trigger coding step; poll message shape |
| `tests/fixtures/waiting_build_session.jsonl` (new) | Minimal synthetic fixture (not full 1.3MB backup) |
| `HANDOFF.md` / `WORKLOG.md` | Note the edge case + config knobs |

---

## Task 1: Fixture + failing tests for open-work extraction

**Files:**
- Create: `tests/fixtures/waiting_build_session.jsonl`
- Create: `tests/test_open_work.py`

- [ ] **Step 1: Write a minimal Pi v3-ish JSONL fixture**

Synthetic session with:

1. User: "fix grok fingerprint"  
2. Assistant: completes grok fix  
3. User: (optional) unrelated  
4. Assistant final: declares wait for build `Y260717-114448`, monitor cmds
   `yanos-builder show/logs`, on-success "fill rebuild-artifact.txt; task 11"  
5. No pending TodoWrite  

Keep it small (<50 lines). Mirror the shapes `pi_session_lib.analyze` already
parses (`type=message`, `message.role`, content blocks).

- [ ] **Step 2: Write failing tests**

```python
def test_extract_waiting_monitor_from_assistant():
    st = pi_session_lib.analyze(FIXTURE)
    assert any(w.get("kind") == "waiting_monitor" for w in st.open_work)
    assert "Y260717-114448" in json.dumps(st.open_work)

def test_resolve_next_step_prefers_wait_over_stale_user_task():
    st = pi_session_lib.analyze(FIXTURE)
    step, src = transcript_lib.resolve_next_step(st)
    assert src == "open_work:waiting_monitor"
    assert "Y260717-114448" in step
    assert "real session" not in step  # not the stale grok ask

def test_last_user_task_strips_base64_and_ignores_status_ping():
    # build tiny in-memory / temp jsonl: user sends text+base64, then "status?"
    ...
    assert "iVBORw0KGgo" not in st.last_user_task
    assert st.last_user_task  # previous real task retained after status?
```

- [ ] **Step 3: Run to confirm fail**

```bash
python3 -m pytest tests/test_open_work.py -q
```

Expected: FAIL (no `open_work`, wrong next step).

- [ ] **Step 4: Commit fixture + tests**

```bash
git add tests/fixtures/waiting_build_session.jsonl tests/test_open_work.py
git commit -m "test: failing coverage for waiting-state open_work extraction"
```

---

## Task 2: Extract open work + hygiene in the brain

**Files:**
- Modify: `src/autocompactor/transcript_lib.py`
- Modify: `src/autocompactor/pi_session_lib.py`

- [ ] **Step 1: Extend `TranscriptStats`**

Add `open_work: list = field(default_factory=list)`.

- [ ] **Step 2: Implement extractors in `transcript_lib`**

- `extract_open_work_from_text(text) -> list[dict]` — pure, unit-testable.
- Wait verbs: `when it succeeds`, `leaving … running`, `I can poll`,
  `poll and finish`, `waiting for`, `still running`, `monitor`.
- Require a handle: build-id pattern and/or a known monitor command token
  (`yanos-builder`, `task_list`, `subagent`, `--follow`).
- Capture `next_on_success` lines in the same message when present.
- Cap summary length; no full transcript echo.

- [ ] **Step 3: Call extractors from `pi_session_lib.analyze`**

On assistant messages (text blocks only), update `st.open_work` (keep latest
wins per kind, or append-and-trim to last ~5).

- [ ] **Step 4: `last_user_task` hygiene**

Before assignment:

- Drop lines matching base64/data-URI bulk (`^[A-Za-z0-9+/]{80,}={0,2}$`,
  `data:image`, PNG/JPEG header markers in-text).
- If message is in trivial-ping set (`status?`, `status`, `ok`, `thanks`,
  `…`, single char), do not overwrite `last_user_task`.
- If stripped text `< 8` chars, keep previous.

- [ ] **Step 5: Run tests**

```bash
python3 -m pytest tests/test_open_work.py -q
```

Expected: extraction + hygiene tests PASS; resolve_next_step still FAIL until Task 3.

- [ ] **Step 6: Commit**

```bash
git add src/autocompactor/transcript_lib.py src/autocompactor/pi_session_lib.py
git commit -m "feat: extract open_work and sanitize last_user_task"
```

---

## Task 3: resolve_next_step priority + structured wait brief

**Files:**
- Modify: `src/autocompactor/transcript_lib.py`
- Modify: `src/autocompactor/transcript_lib.py` `build_preservation_instructions`

- [ ] **Step 1: Rewrite `resolve_next_step`**

Priority:

1. pending todo  
2. latest `waiting_monitor` open_work → structured brief + src
   `open_work:waiting_monitor`  
3. latest `next_on_success` → src `open_work:next_on_success`  
4. cleaned `last_user_task`  
5. last correction  

Structured brief format (stable, model-facing):

```
WAITING: <resource_ids>
Monitor: <cmds joined; or "(none declared)">
On success: <next_on_success or "(unspecified)">
Do not start unrelated work. Poll now; if still running, report status and stop.
```

- [ ] **Step 2: Seed preservation instructions**

In `build_preservation_instructions`, after todos:

```
- OPEN WORK (must survive compaction; resume these, do not drop):
  * WAITING … 
```

- [ ] **Step 3: Tests green for resolve + preservation**

```bash
python3 -m pytest tests/test_open_work.py tests/test_autocompactor.py -q
```

- [ ] **Step 4: Commit**

```bash
git commit -am "feat: prefer open_work waits in resolve_next_step + preservation"
```

---

## Task 4: Artifacts + bridge staging

**Files:**
- Modify: `src/autocompactor/artifacts.py`
- Modify: `src/autocompactor/pi_bridge.py`
- Modify: `tests/test_pi_bridge.py`

- [ ] **Step 1: Add `open_work` to artifacts**

- Include in `PRIORITY` near `corrections` (high — must survive budget trim).
- `extract(st)` copies `st.open_work`.
- Digest section title: `OPEN WORK (resume after compact)`.
- Ledger counts only (content-free stats).

- [ ] **Step 2: Bridge prepare/reinject**

- `cmd_prepare`: already stages `resolve_next_step`; ensure wait brief is what
  gets staged (automatic once Task 3 lands). Optionally also
  `state["staged_open_work"] = st.open_work[:5]`.
- `cmd_reinject`: surface `openWork` array + existing `nextStep` /
  `nextStepSource` / `nextStepMode`. Add `nextStepWait: true` when source
  starts with `open_work:waiting`.

- [ ] **Step 3: Telemetry**

`log_event` precompact/reinject: add `next_step_src`, `open_work_n`,
`next_step_wait` — no brief text.

- [ ] **Step 4: Tests**

Bridge test: prepare on fixture session → reinject JSON has
`nextStepSource == "open_work:waiting_monitor"` and `nextStepWait is True`.

```bash
python3 -m pytest tests/test_pi_bridge.py tests/test_open_work.py -q
```

- [ ] **Step 5: Commit**

```bash
git commit -am "feat: stage open_work through artifacts and pi_bridge reinject"
```

---

## Task 5: Config knobs

**Files:**
- Modify: `config.json`
- Modify: `src/autocompactor/config_lib.py` only if new keys need defaults beyond `str`/`float` helpers (prefer existing helpers)

- [ ] **Step 1: Add defaults**

```json
"NEXTSTEP_WAIT": "poll",
"WAIT_POLL_S": 60,
"WAIT_POLL_MAX": 20
```

Document in comment block at top of config or HANDOFF.

- [ ] **Step 2: Bridge surfaces `nextStepWaitMode`** from config for the shim
  (mirror how `nextStepMode` is already returned).

- [ ] **Step 3: Commit**

```bash
git commit -am "config: NEXTSTEP_WAIT / WAIT_POLL_* for wait-aware resume"
```

---

## Task 6: TS shim — idle visibility (D5)

**Files:**
- Modify: `src/pi/autocompactor.ts`
- Modify: existing extension tests if present (`tests/*.mjs` / `*.test.ts`)

- [ ] **Step 1: Delivery helper**

```ts
function deliveryFor(ctx: ExtensionContext): { deliverAs?: "nextTurn" } | undefined {
  const idle = typeof ctx.isIdle === "function" ? ctx.isIdle() : true
  // Idle + not mid-stream: let Pi's final branch render immediately.
  // Non-idle: nextTurn (proven anti-swallow path).
  return idle ? undefined : { deliverAs: "nextTurn" }
}
```

Use for actuate "criteria met / running compaction" and post-compact status
when announcing.

- [ ] **Step 2: Keep notify + setAcStatus** (already live).

- [ ] **Step 3: Unit/smoke assert** message options omit `deliverAs` when
  `isIdle()` is true.

- [ ] **Step 4: Commit**

```bash
git commit -am "fix(pi): show actuate status immediately when session is idle"
```

---

## Task 7: TS shim — wait-aware auto-resume + poll timer (D4)

**Files:**
- Modify: `src/pi/autocompactor.ts`
- Create/modify: `tests/shim_wait_resume.test.ts` (or extend existing)

- [ ] **Step 1: Classify wait-shaped reinject**

```ts
const waitShaped =
  Boolean(inj?.nextStepWait) ||
  String(stepSrc).startsWith("open_work:waiting")
const waitMode = configuredWaitMode(inj?.nextStepWaitMode) // poll|advisory|off
```

- [ ] **Step 2: Branch in post-compact next-step handling**

- `autonomous && waitShaped && waitMode==="poll"`:
  - Persist digest + status + advisory wait message (`display: true`).
  - **Do not** send `autocompactor.nextstep.task` with immediate `triggerTurn`.
  - `scheduleWaitPoll(ctx, brief, { delayS, max })`.
- `autonomous && waitShaped && waitMode==="advisory"`:
  - Same visible brief; no timer; no triggerTurn.
- `autonomous && !waitShaped`:
  - Existing `flushAutoResume` path.
- `advisory` nextstep mode: unchanged (surface only).

- [ ] **Step 3: `scheduleWaitPoll`**

- One pending timer per extension instance; clear on new compact, user
  prompt (`session` events if available), or max reached.
- On fire: if `!ctx.isIdle()` reschedule once; else
  `pi.sendMessage({ customType: "autocompactor.nextstep.poll", content: brief },
  { triggerTurn: true })`.
- Status bar: `waiting · poll in Ns`.
- Injectable clock/timer for tests (`global.setTimeout` stub).

- [ ] **Step 4: Config read**

`WAIT_POLL_S` / `WAIT_POLL_MAX` / `NEXTSTEP_WAIT` from `CFG` + env
(`AUTOCOMPACTOR_WAIT_POLL_S`, etc.), same pattern as other knobs.

- [ ] **Step 5: Tests**

- Wait-shaped reinject → no immediate `nextstep.task` triggerTurn.  
- Timer callback → exactly one `nextstep.poll` with triggerTurn.  
- Non-wait autonomous → existing path unchanged (regression).

- [ ] **Step 6: Commit**

```bash
git commit -am "feat(pi): wait-aware nextstep with scheduled poll resume"
```

---

## Task 8: Incident regression pin + full verify

**Files:**
- Optional: `tests/fixtures/incident_019f7130_snippet.jsonl` (trimmed from
  backup — only the last ~N messages around the wait declaration; no secrets).
- Modify: `HANDOFF.md`, `WORKLOG.md`

- [ ] **Step 1: If backup is usable as a snippet**, pin
  `resolve_next_step` against it (or keep synthetic fixture if backup is too
  large / sensitive). Prefer synthetic + one optional integration mark.

- [ ] **Step 2: Full test suite**

```bash
python3 -m pytest tests/ -q
PI_SMOKE=1 bash tests/smoke_test_pi.sh   # when safe on this host
```

- [ ] **Step 3: Manual sanity (owner session pattern)**

1. Actuate mode, large context, assistant ends with an explicit wait + id.  
2. Force/await compact.  
3. Expect: chat shows compact status without user poke; nextstep is wait brief;
   ~WAIT_POLL_S later a poll turn runs (or advisory only if configured).

- [ ] **Step 4: Docs**

- HANDOFF: open item → closed with pointer to this plan + knobs.  
- WORKLOG: short entry for the incident + fix.

- [ ] **Step 5: Final commit**

```bash
git commit -am "docs: waiting-state resume incident + knobs"
```

---

## Task 9: Install path note

**Files:**
- `src/install_pi.py` (only if version pin / copy rewrite needs a bump)

- [ ] Reinstall shim so live Pi picks up TS changes:
  `python3 src/install_pi.py`  
- Confirm `install_pi.py --status` points at this checkout.  
- No Pi core modification.

---

## Out of scope / follow-ups

- External job watcher daemon (yanos-builder webhook → pi wake).  
- Using wait as a compaction **gate** (suppress/force) — explicitly deferred
  (design trap #4).  
- Multi-wait fan-in (several builds) beyond list-in-brief.  
- Cross-session wait recovery after process death beyond artifact digest.

## Rollback

- `NEXTSTEP_WAIT=off` restores pre-change autonomous behavior for wait steps.  
- `NEXTSTEP=advisory` disables all auto triggerTurn.  
- Feature is additive; removing open_work fields is backward compatible
  (empty list / missing keys).
