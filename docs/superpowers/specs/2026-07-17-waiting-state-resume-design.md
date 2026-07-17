# Design: waiting-state resume after compaction

Date: 2026-07-17  
Status: draft for plan approval  
Incident session: `019f7130-6e24-7ad5-b7a1-3d336e79ac88` (yanos-project, wave-4 rebuild)

## Problem (owner report)

Autocompactor sometimes derails a process that is waiting for a background task
to complete. Compaction runs "silently"; only after a later user prompt
(e.g. `status?`) does the session surface that it compacted and then failed to
resume the wait/poll handoff.

## Ground-truth incident reconstruction

Session file:
`~/.pi/agent/sessions/--srv-dev-yanos-project--/2026-07-17T17-47-07-172Z_019f7130-6e24-7ad5-b7a1-3d336e79ac88.jsonl`

| t (UTC) | What happened |
|---|---|
| 18:41:03 | Assistant finished Grok CLI-fingerprint fix (user ask at 18:32). |
| 18:41:04–47 | Actuate compaction at ~370k tokens (dormant-token gate). Prepare + reinject. |
| 18:41:48 | `autocompactor.nextstep.task` auto-fired with **wrong** step: the Grok user message (including base64 PNG header), source `last_user_task`. `triggerTurn: true`. |
| 18:42–18:46 | Agent re-verified Grok, re-enqueued wave-4 builds, fixed recipe wiring, then wrote **"leaving the full image build running"** for `Y260717-114448` and **stopped**. |
| 18:46–19:07 | Session idle ~21 minutes while the build ran. No poll wake-up. |
| 19:07:22 | User: `status?`. Queued `nextTurn` compaction notices flush here (looks like "silent" compact). Agent finally checks build → succeeded. |
| 19:09:17 | User correction: waiting detection should be automatic. |

State file confirms the wrong staged step:

```
staged_next_step_src = last_user_task
staged_next_step     = "You may need to do something to make it look like
                        its coming from a real session …" + base64 PNG
```

Backup re-analysis with current code reproduces the same `resolve_next_step`
output. Wait language **is** present in the pre-compact transcript (~28
wait-like lines) but is never extracted into next-step / artifacts / preservation
instructions.

## Root causes (three stacked failures)

### RC1 — Next-step source is user-only and stale

`transcript_lib.resolve_next_step` priority today:

1. first pending TodoWrite item  
2. `last_user_task`  
3. latest correction  

`last_user_task` is set only from **user** text (`pi_session_lib.analyze`).
Assistant-declared open work ("when it succeeds I'll fill rebuild-artifact and
run task 11", "leaving the build running") is invisible to the resolver.

In this session there was no pending todo, so the resolver picked the most
recent substantive user message — a **completed** Grok-fingerprint ask, polluted
with image/base64 text that `_block_text` does not strip (image blocks are
ignored, but the user also pasted base64 as text).

### RC2 — Autonomous resume re-enters the wrong task, then goes idle on wait

`NEXTSTEP=autonomous` (default) + `flushAutoResume` with `triggerTurn: true`
when `ctx.isIdle()` immediately starts a new turn on the recovered step.

That is correct for "keep coding after compact". It is **wrong** for:

- a completed user ask that is no longer the live goal, and  
- a live **wait/poll** goal, which needs a later re-check, not an immediate
  re-implementation of the previous user prompt.

After the wrong auto-resume, the model did rediscover the build — then
explicitly entered a wait state and stopped. Nothing in the extension
re-wakes an idle session. Waiting is therefore "user must poke".

### RC3 — Compaction notices are deferred to next user prompt

Visible notices use `deliverAs: "nextTurn"` (Pi 0.79.9: only channel that
survives mid-stream and renders). Consequence: the "criteria met; running
compaction now" and post-compact accounting lines appear only when the user
types again. Combined with RC2, the user experience is: silence → poke →
"oh, it compacted and lost the wait."

Status bar (`setAcStatus`) and `notify` fire live, but chat is the durable
record the user reads.

## Non-goals

- General long-running job orchestrator / yanos-builder integration.
- Changing Pi core delivery semantics (requires `ALLOW_PI_MODIFICATION`).
- Turning off autonomous next-step globally (it is load-bearing for
  non-wait resume).
- Claude adapter / dual-harness work.

## Design

### D1 — Mechanical open-work extraction (Python)

Add to `TranscriptStats`:

```python
open_work: list[dict] = field(default_factory=list)
# each: {kind, summary, monitor_cmds?, resource_ids?, next_on_success?,
#        source_ts?, confidence}
```

Kinds (v1):

| kind | Detection (heuristic, content-free beyond short quotes) |
|---|---|
| `waiting_monitor` | Assistant text matches wait/poll language near a concrete resource id or monitor command (`yanos-builder show/logs`, `task_list`, `subagent` async, `--follow`, "when it succeeds", "leave … running", "I can poll"). |
| `in_progress_task` | Pending TodoWrite items (already exist; also surface here). |
| `next_on_success` | Explicit post-success actions in the same assistant turn ("fill rebuild-artifact.txt", "run task 11 → results.md"). |

Extraction rules:

- Prefer the **latest** assistant message that declares open work (not the
  earliest). Cap stored summaries (~500 chars) and command lists (≤5).
- Resource ids: simple patterns (`Y\d{6}-\d+`, common build/job id shapes,
  `task \d+`). Content-free telemetry: counts + id shapes only in events.
- Do **not** invent work; empty list when no signal.

Wire into:

- `artifacts.extract` → new class `open_work` (priority near `corrections`).
- `build_preservation_instructions` → seed PLAN & POSITION with open work
  verbatim (so the summarizer cannot drop "waiting for build X").
- `resolve_next_step` → see D2.
- `cmd_prepare` / `cmd_reinject` → stage + surface `openWork` for the shim.

### D2 — Next-step priority rewrite

New priority (first non-empty wins):

1. `todo:pending[0]` (unchanged — explicit user plan)  
2. `open_work:waiting_monitor` — live wait beats a stale user ask  
3. `open_work:next_on_success` if no active wait but a declared handoff remains  
4. cleaned `last_user_task` (see D3)  
5. `correction[-1]`

Source tags stay short and stable for telemetry:
`todo:pending[0] | open_work:waiting_monitor | open_work:next_on_success |
last_user_task | correction[-1]`.

When the winner is wait-shaped, the recovered step text is a **structured
resume brief**, not a raw user quote:

```
WAITING: build Y260717-114448
Monitor: yanos-builder show Y260717-114448
On success: fill rebuild-artifact.txt checksum; run task 11 → results.md
Do not start unrelated work. Poll now; if still running, report status and stop.
```

### D3 — `last_user_task` hygiene

In `pi_session_lib.analyze` (user-message path):

- Strip pure base64/data-URI blobs and lines that look like image payloads
  before assigning `last_user_task`.
- Ignore trivial pings as task updates: `status?`, `ok`, `thanks`, bare
  `?` (configurable small denylist). They must not overwrite a real task.
- If after stripping the text is empty/too short, keep the previous
  `last_user_task`.

This alone would have prevented the base64-polluted Grok message from being
a worse resume target; D2 still needed because the Grok ask was real text
but **done**.

### D4 — Wait-aware auto-resume (TS shim)

In `session_compact` / `flushAutoResume`:

```
if nextStepMode == autonomous:
  if step is wait-shaped (source starts with open_work:waiting OR content
     classifier):
    → WAIT path
  else:
    → existing triggerTurn path
```

**WAIT path (default, config `NEXTSTEP_WAIT=poll`):**

1. Persist digest + status + a visible advisory:
   `autocompactor: session is waiting for <resource>; scheduling poll in Ns`.
2. **Do not** fire the wrong coding task with `triggerTurn` immediately.
3. Schedule a single `setTimeout` (default 60s, env/config
   `AUTOCOMPACTOR_WAIT_POLL_S`) that, when the session is still idle, sends:

   ```
   customType: autocompactor.nextstep.poll
   content: <structured resume brief from D2>
   options: { triggerTurn: true }
   ```

4. Re-arm at most `WAIT_POLL_MAX` times (default 20) while the model keeps
   reporting "still running"; clear on success language or user interrupt.
5. Status bar always shows `waiting · <id> · next poll in Ns`.

**WAIT path alternatives (config):**

| `NEXTSTEP_WAIT` | Behavior |
|---|---|
| `poll` (default) | Timer re-wake as above. |
| `advisory` | Surface the wait brief; no timer; no triggerTurn. User still pokes, but at least the wait is not forgotten after compact. |
| `off` | Current behavior for wait steps (treat as normal autonomous). |

Timers live only in the extension process for the open session — no daemon.
If the session/process dies, the next user prompt still sees open_work via
digest/artifacts (D1).

### D5 — Visibility of actuate when idle

When actuate fires at `agent_end` and `ctx.isIdle()`:

- Keep `notify` + `setAcStatus` (already live).
- Additionally `pi.sendMessage(..., { display: true })` **without**
  `deliverAs: "nextTurn"` when idle (idle + not streaming → Pi's final
  branch renders+persists immediately). Use `nextTurn` only when not idle
  (streaming / mid-turn), preserving the existing anti-swallow fix.
- Same rule for the post-compact status line when reinject runs idle.

Net effect: user sees "compacting now" / "compaction completed" in chat
without having to type `status?`.

### D6 — Config surface (single namespace)

Add to `config.json` (with `AUTOCOMPACTOR_*` env overrides via `config_lib`):

```jsonc
{
  "NEXTSTEP": "autonomous",          // existing
  "NEXTSTEP_WAIT": "poll",           // poll | advisory | off
  "WAIT_POLL_S": 60,
  "WAIT_POLL_MAX": 20
}
```

No new top-level mode. Defaults preserve non-wait autonomous resume.

## Failure modes / traps

1. **False-positive wait detection** → spurious poll turns. Mitigate with
   requiring (resource id **or** monitor command) + wait verb; unit-test
   negative cases.
2. **False-negative** → same as today. Prefer recall over precision only
   when both a wait verb and a concrete handle exist.
3. **Timer storms** after rapid compact loops → single pending timer +
   `WAIT_POLL_MAX` + clear on user message / new compact.
4. **triggerTurn while user is typing** → only fire poll when `isIdle()`.
5. **Design trap #4 (actuate gates)** — wait extraction is for resume, not
   for compaction *recommendation*. Do not add wait as a suppress/force
   compaction signal in v1.
6. **Content in telemetry** — events store source tag + resource-id shape +
   lengths only; full brief stays in session messages / local artifacts.

## Success criteria

From a fixture cloned from this incident (pre-compact backup):

1. `resolve_next_step` returns source `open_work:waiting_monitor` and a brief
   naming `Y260717-114448` (or the fixture's id), **not** the Grok/base64
   user message.
2. Preservation instructions include the open wait.
3. Shim wait path: autonomous + wait-shaped → no immediate coding
   `triggerTurn`; one scheduled poll message shape is emitted (unit-tested
   via injectable clock).
4. Idle actuate status message is sent without `deliverAs: "nextTurn"`.
5. `last_user_task` ignores `status?` and strips base64-only tails.
6. Existing non-wait next-step tests still pass; pytest baseline green;
   `PI_SMOKE=1 bash tests/smoke_test_pi.sh` green when safe.

## Rollout

1. Land D1–D3 + tests (brain-only; safe under advise or actuate).  
2. Land D5 (visibility) — low risk, high UX.  
3. Land D4 with `NEXTSTEP_WAIT=poll` default; allow site overlay to
   `advisory` if a host dislikes timers.  
4. Watch one week of `stats/events.jsonl` for
   `next_step_src=open_work:*` rates and poll counts; no content.
