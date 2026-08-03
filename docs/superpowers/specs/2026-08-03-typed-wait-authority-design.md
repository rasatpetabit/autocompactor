# Typed Wait Authority Design

**Date:** 2026-08-03
**Status:** Approved design; written-spec review pending
**Repository:** `/srv/dev/ras/autocompactor`

## 1. Goal

Prevent autocompactor from replacing a live user task with false, stale, or
unverifiable wait metadata.

Automatic polling remains available only for a narrow, typed YANOS-builder
contract. Every other legacy wait becomes advisory. A single versioned
continuation record is the only source of authority for scheduling, polling,
or reinjecting a wait.

The Opticboard cross-repository rename is the regression oracle for this bug,
not implementation scope.

## 2. Verified Incident

Pi session `019fc9db-16bb-7e46-9b4e-8de110013dd6` was working on a reviewed
cross-repository plan for:

- `optic-5c` → `rock-5c`
- `optic-cm3-16gb-2gb` → `opticboard-cm3-16gb-2gb`
- `opticboard` as the product family
- shared core by default
- drift correction in `hw-opticboard`

A `read` tool result returned `docs/conventions/image-bringup-agent.md`. That
document contained phrases including `enqueued high priority`,
`yanos-builder show`, and generic polling instructions. The current parser:

1. treated the arbitrary file contents as live tool output;
2. synthesized a wait phrase;
3. created `waiting_monitor` with no resource ID and no monitor command;
4. promoted it in `progress_lib` to `mode=wait`, confidence `0.95`, and
   affinity `true`; and
5. allowed the wait to outrank the current Opticboard planning task during
   compaction.

The resulting compaction injected and polled:

```text
WAITING: 3. catalog / `yanos-builder show` status and `git_sha`
```

instead of resuming the user’s requested plan.

A minimal replay is red at current HEAD: the read-result documentation creates
`open_work`, and `resolve_next_step()` returns
`open_work:waiting_monitor` rather than `last_user_task`.

## 3. Scope

### In scope

- `src/autocompactor/pi_session_lib.py`
- `src/autocompactor/progress_lib.py`
- `src/autocompactor/pi_bridge.py`
- `src/autocompactor/artifacts.py`
- `src/pi/autocompactor.ts`
- a new focused authority module under `src/autocompactor/`
- targeted Python, Bun, smoke, and shim tests
- state migration for existing autocompactor state and artifacts
- content-free telemetry for continuation transitions

### Out of scope

- implementing or planning the Opticboard/Rock 5C rename itself
- changing Pi core
- rewriting historical session JSONL
- automatically interpreting arbitrary assistant prose as executable work
- preserving automatic polling for `task_list` or subagent waits in this change

## 4. Approaches Considered

### 4.1 Typed authority record — selected

Create a single durable, versioned continuation record. Only validated,
structured YANOS-builder tool-call/result pairs may create automatic wait
authority. Every action reloads and checks the current generation.

This addresses false extraction, stale reinjection, timer races, restarts, and
legacy state.

### 4.2 Minimal extraction patch — rejected

Restrict tool-result scanning and require a build ID plus command before
creating `waiting_monitor`.

This would fix the exact read-result false positive but would leave:

- projections as action authority;
- newer-user supersession undefined;
- stale timer/restart races;
- legacy persisted wait state capable of reappearing.

### 4.3 Advisory-only waits — retained as rollback

Disable all automatic polling. This is safest and simplest, but removes a
useful behavior. It remains available through `NEXTSTEP_WAIT=advisory|off`.

## 5. Authority Invariants

1. **Prose never authorizes automation.** Assistant text, read/search output,
   unmatched tool results, compaction summaries, artifact text, and digest text
   may provide advisory context only.
2. **One record authorizes action.** Only the current schema-v2
   `wait_authority` record can authorize wait reinjection or polling.
3. **Projections are inert.** `TranscriptStats.open_work`, progress hits,
   artifact `open_work`, digest sections, and TypeScript timer state cannot
   independently grant authority.
4. **Human input wins.** Any later nontrivial genuine user input supersedes an
   open automatic wait before the prompt reaches the agent.
5. **Every action revalidates.** Reinject and timer callbacks reload authority
   and require the current continuation ID, generation, status, phase, user
   entry, and typed operation.
6. **Timer cancellation is cleanup, not correctness.** A stale callback must be
   harmless even if cancellation fails or the process restarts.
7. **Unknown state fails closed.** Corrupt, legacy, missing, ambiguous,
   conflicting, truncated, or unknown state produces no automatic action.
8. **State transitions are serialized.** All continuation mutations use one
   lock and compare-and-swap contract.

## 6. Module Ownership

### 6.1 `wait_state.py` — new authority module

`src/autocompactor/wait_state.py` owns:

- authority schema and version;
- typed command and result parsing;
- Pi tool-call/result pairing;
- state transition rules;
- lock acquisition;
- atomic state-file writes;
- compare-and-swap checks;
- wait validation for prepare, reinject, and poll callbacks;
- content-free telemetry field construction.

No other module may independently decide that a wait is autonomous.

### 6.2 `pi_session_lib.py` — ordered facts

`pi_session_lib.py` supplies:

- ordered active root-to-leaf entries;
- genuine user entry IDs and ordering;
- exact assistant `bash` tool calls;
- exact paired `toolResult` entries;
- result error and content shape.

It must stop deriving autonomous waits from arbitrary prose or generic tool
output. Existing open-work extraction may retain advisory hints only when they
are marked `autonomous=False`.

### 6.3 `progress_lib.py` — projection only

`progress_lib.py` may render validated authority as a progress projection, but
must:

- preserve source confidence and provenance;
- never manufacture `affinity=True`;
- never manufacture confidence `0.95`;
- never turn advisory `open_work` into autonomous `mode=wait`.

Only a projection created from current validated authority may use
`mode=wait`.

### 6.4 `pi_bridge.py` — command adapter

Add bridge subcommands:

- `wait-register`
- `wait-check`
- `wait-transition`

`pi_bridge.py` delegates authority logic to `wait_state.py`. Its existing
state reads and writes must use the same lock and atomic persistence helper so
unrelated state keys cannot be lost.

`cmd_prepare` and `cmd_reinject` consume validated authority. They do not infer
it.

### 6.5 `artifacts.py` — advisory durability

Artifacts may preserve readable open-work context. Artifact presence never
authorizes polling or wait-shaped reinjection.

Legacy wait projections are removed best-effort during migration. Cleanup
failure does not affect correctness because projections are inert.

### 6.6 `autocompactor.ts` — timer owner

The extension owns only disposable timer state:

- continuation ID;
- generation;
- one-shot timer handle;
- poll idempotency key after issue.

It never trusts `nextStepWait`, digest text, `openWork`, or a wait-shaped string
without a validated authority ID and generation returned by the bridge.

## 7. Trusted Tool Contract

### 7.1 Accepted Pi message pairing

Only Pi session v3 messages with this shape are eligible:

1. an assistant content block:

```json
{
  "type": "toolCall",
  "id": "<nonempty string>",
  "name": "bash",
  "arguments": {"command": "<string>"}
}
```

2. followed on the active root-to-leaf branch by exactly one message:

```json
{
  "role": "toolResult",
  "toolCallId": "<same id>",
  "toolName": "bash",
  "isError": false,
  "content": [{"type": "text", "text": "..."}]
}
```

The following are ineligible:

- missing or malformed IDs;
- duplicate results for one call ID;
- results preceding their call;
- mismatched or missing `toolName`;
- `isError=true`;
- aborted assistant messages;
- image/non-text result blocks;
- user `bashExecution` messages;
- output containing Pi truncation markers;
- calls or results not on the active branch.

Only the latest eligible pair after the latest nontrivial user entry may
register authority.

### 7.2 Accepted command grammar

Parse the command with shell-style tokenization, then accept only:

```text
yanos-builder show Y######-<digits>
yanos-builder --json show Y######-<digits>
```

The build ID must fully match:

```regex
Y[0-9]{6}-[0-9]+
```

Reject:

- any additional flag;
- shell operators or compound commands;
- pipes;
- redirects;
- substitutions;
- variables;
- command separators;
- ID substring matches;
- multiple IDs.

Store a typed operation, never a raw command:

```json
{
  "kind": "yanos_builder_show",
  "resource_id": "Y260803-092025",
  "json": false
}
```

### 7.3 Accepted result grammar

#### Plain output

The output must contain:

- exactly one `Build <same-id>` header; and
- exactly one `status : <value>` line associated with that block before any
  second `Build` header.

#### JSON output

The output must parse as one object with:

- `build_id` equal to the exact authorized ID; and
- `status` as a string.

#### State values

- Open: `running`, `queued`
- Terminal: `succeeded`, `failed`, `aborted`, `cancelled`
- Invalid: unknown, missing, duplicate, conflicting, malformed, or truncated

Result text may update the current operation only. It cannot register a new
resource ID.

## 8. Authoritative State

The per-session state file gains schema-v2 `wait_authority`:

```json
{
  "schema_version": 2,
  "continuation_id": "opaque-random-id",
  "generation": 4,
  "status": "OPEN",
  "phase": "READY",
  "poll_sequence": 0,
  "source_entry_id": "entry-id",
  "source_tool_call_id": "call-id",
  "source_result_entry_id": "result-entry-id",
  "source_order": 173,
  "authorized_user_entry_id": "user-entry-id",
  "operation": {
    "kind": "yanos_builder_show",
    "resource_id": "Y260803-092025",
    "json": false
  },
  "created_at": "observability timestamp",
  "updated_at": "observability timestamp",
  "terminal_reason": ""
}
```

### Correctness fields

Correctness uses:

- continuation ID;
- generation;
- status;
- phase;
- poll sequence;
- source order;
- authorized user entry ID;
- typed operation.

Wall-clock timestamps are observability only.

### Status and phase

Statuses:

- `OPEN`
- `TERMINAL`
- `SUPERSEDED`
- `INVALID`

Open phases:

- `READY`
- `POLL_ISSUED`

## 9. Human-Input Ordering

### 9.1 Immediate extension boundary

The extension subscribes to Pi’s `input` event. For source `interactive` or
`rpc`, never source `extension`, it invokes:

```text
wait-transition --reason superseded
```

before the prompt reaches the agent.

### 9.2 Exact trivial-message allowlist

After trim and lowercase, only exact whole-message matches are excluded:

```text
status?
status
ok
okay
thanks
thank you
thx
?
…
...
y
n
yes
no
k
kk
cool
great
```

Mixed content is nontrivial. For example,
`status? also switch to Opticboard` supersedes automation.

### 9.3 Parser fallback

Python reconstructs the latest nontrivial genuine `role=user` entry ID and
order from the active root-to-leaf branch, including retained post-compaction
material when available.

The reconstructed latest user entry must equal `authorized_user_entry_id`.
If history is unavailable or ambiguous, automation is denied. A fresh eligible
monitor pair after the user entry may register a new generation.

Custom and extension poll messages do not count as human input.

## 10. Serialization and Atomic Persistence

All authority mutations use an advisory `flock` on:

```text
<session-id>.state.lock
```

Under the lock:

1. reload the current state;
2. compare expected continuation ID, generation, status, and phase;
3. apply the transition;
4. write a same-directory temporary file;
5. flush and `fsync` the file;
6. atomically `os.replace` the state file;
7. `fsync` the parent directory;
8. release the lock.

Generation allocation occurs only under this lock as previous generation + 1.
The bridge is the only writer. TypeScript never writes authority state.

A compare-and-swap mismatch returns `STALE` and performs no action.

## 11. Transition Model

Permitted transitions:

| Current | Event | Next |
|---|---|---|
| none/terminal | eligible pair | new `OPEN/READY`, generation + 1 |
| `OPEN/READY` | pre-fire | `OPEN/POLL_ISSUED`, sequence + 1 |
| `OPEN/POLL_ISSUED` | exact running result | `OPEN/READY` |
| `OPEN/*` | exact terminal result | `TERMINAL` |
| `OPEN/*` | later nontrivial user input | `SUPERSEDED` |
| `OPEN/*` | malformed, missing, conflicting, unknown, or stale evidence | `INVALID` |
| terminal state | stale callback | unchanged / `STALE` |

Terminal states never reopen. A new eligible pair creates a new generation.

## 12. Polling Handshake

### 12.1 One-shot timer

Timers never pre-arm the next timer.

Before firing, TypeScript calls:

```text
wait-check <continuation-id> <generation>
```

Under the lock, the bridge requires current `OPEN/READY`, matching user entry,
and a valid typed operation. It atomically moves to `POLL_ISSUED`, increments
`poll_sequence`, and returns:

- the typed operation; and
- idempotency key `<id>:<generation>:<sequence>`.

Only then may TypeScript trigger one poll turn.

Duplicate callbacks see `POLL_ISSUED` and do nothing. Reinject does not return
a wait while `POLL_ISSUED`.

### 12.2 Post-poll acknowledgement

At the top of `agent_end`, TypeScript calls:

```text
wait-check --after-poll <idempotency-key>
```

The bridge inspects only an eligible exact-ID result occurring after the
recorded issue order:

- terminal → `TERMINAL`;
- exact running/queued → `OPEN/READY`;
- later nontrivial user input → `SUPERSEDED`;
- unknown, error, missing, duplicate, or truncated → `INVALID`.

Only `OPEN/READY` schedules the next one-shot timer. Every other result clears
in-memory timer state and cannot re-arm.

### 12.3 Crash behavior

A crash after `POLL_ISSUED` chooses at-most-once behavior over liveness. On
restart or `session_start`, an unresolved `POLL_ISSUED` record becomes
`INVALID` and advisory. It is never repeated automatically.

Stale callbacks after restart, a tombstone, or a new generation fail the
pre-action compare-and-swap check.

## 13. Prepare, Resolution, and Reinject

### `cmd_prepare`

- normal user, todo, and coding next steps continue to stage as today;
- a wait-shaped next step stages only from validated current `OPEN/READY`
  authority;
- advisory `open_work` cannot win absolute priority.

### `resolve_next_step`

Receive validated authority explicitly. Priority becomes:

1. validated current `OPEN/READY` wait authority;
2. eligible coding progress;
3. pending todo;
4. post-success advisory handoff;
5. latest genuine user task;
6. latest correction.

Unvalidated `open_work` never occupies priority 1.

### `cmd_reinject`

Immediately before returning `nextStepWait=true`, revalidate under the lock.
Return authority continuation ID and generation with the wait. Without both,
TypeScript treats `nextStepWait` as inert.

While authority phase is `POLL_ISSUED`, reinject returns no automatic wait.

## 14. Migration and Cleanup

- Schema version increments to 2.
- Existing state without schema-v2 authority is automation-inert.
- Existing concrete waits are not grandfathered; a fresh validated show pair
  must establish authority.
- First prepare/reinject removes legacy staged wait keys and wait-shaped
  progress/open-work projections best-effort under the shared lock.
- Artifact cleanup is best-effort. Stale projections may remain after I/O
  failure but cannot authorize action.
- Unknown schema, status, phase, or operation fails closed.
- Session JSONL is never rewritten.

## 15. Compatibility and Rollback

Only exact YANOS-builder show waits remain automatic.

These intentionally become advisory:

- assistant-prose waits;
- handle-free waits;
- `task_list` waits;
- subagent waits;
- legacy staged waits;
- arbitrary shell-monitor commands.

`NEXTSTEP_WAIT` behavior:

- `poll`: typed polling enabled;
- `advisory`: registration, automatic reinjection, and scheduling disabled;
- `off`: wait surfacing and automation disabled.

Every pre-fire check reads current configuration. An already armed callback is
a no-op after rollback to `advisory` or `off`.

## 16. Telemetry

Allowed fields only:

- event name enum;
- schema version;
- operation kind enum;
- status enum;
- phase enum;
- transition reason enum;
- source class enum;
- concrete-handle boolean;
- generation delta integer;
- stale-callback count;
- timer-cancel count.

Forbidden:

- task text;
- free-form errors;
- raw commands or output;
- resource IDs;
- entry or tool-call IDs;
- snippets;
- paths;
- hashes.

## 17. Error Handling

All new bridge paths preserve the project’s never-raise contract.

- malformed or unreadable state → no automation;
- lock or persistence failure → no transition and no action;
- bridge timeout/failure → cancel automation and surface advisory status;
- unknown producer/session schema → no automation;
- unknown result status → `INVALID`;
- cleanup failure → inert stale projection, no action.

No fallback may silently restore legacy prose-derived automation.

## 18. Acceptance Evidence

### 18.1 Exact regression

A fixture reproduces the incident:

1. user requests the Opticboard cross-repository plan;
2. assistant calls `read` on documentation;
3. read result contains `enqueued`, `yanos-builder show`, and polling guidance;
4. prepare and reinject run.

Assertions:

- no authority record is created;
- no autonomous waiting projection is created;
- `nextStep` remains the Opticboard task;
- no `autocompactor.nextstep.wait` is emitted;
- no `autocompactor.nextstep.poll` is emitted;
- no triggered task contains catalog work.

### 18.2 Negative parser table

Cover:

- assistant prose;
- read/search output;
- unmatched generic output;
- wrong tool name;
- missing/mismatched/duplicate/out-of-order call IDs;
- tool error;
- aborted assistant;
- truncated output;
- image/non-text result;
- ID mismatch or substring;
- multiple IDs;
- compound shell;
- variables, redirects, or substitution;
- unknown/conflicting status;
- producer/schema drift.

### 18.3 Positive typed flows

Exact plain and JSON show pairs create one `OPEN/READY` authority and one
one-shot timer.

An exact running result re-arms only after post-poll acknowledgement. An exact
terminal result tombstones authority and fake-time advancement emits no second
poll.

### 18.4 Human precedence

- exact trivial allowlist messages do not supersede;
- mixed or any other genuine human input supersedes at the extension input
  boundary;
- parser fallback detects supersession after restart;
- a fresh valid show pair after the human message can create a new generation.

### 18.5 Concurrency and recovery

Two-process and callback tests cover:

- user supersession versus pre-fire;
- old versus new generation writes;
- terminal versus new registration;
- duplicate timer callback;
- duplicate/out-of-order `agent_end`;
- crash while `POLL_ISSUED`;
- restart with `OPEN/READY` or `OPEN/POLL_ISSUED`;
- duplicate reinject;
- corrupt, truncated, unknown, and legacy state;
- missing retained history;
- rollback with an armed callback.

### 18.6 Verification commands

Implementation verification must include:

```bash
python3 -m pytest tests/test_open_work.py tests/test_progress_lib.py tests/test_pi_bridge.py -q
bun test tests/shim_wait_resume.test.ts
python3 -m pytest tests/ -q
PI_SMOKE=1 bash tests/smoke_test_pi.sh
node --test 'src/pi/test/*.test.mjs'
```

The final user-visible verification must include the installed extension path
when implementation is deployed, not only source tests.

## 19. Review Record

The initial proposal received `REWORK` because it depended on cross-store
cleanup, left command binding and user-generation semantics ambiguous, and did
not make stale callbacks independently harmless.

The revised proposal introduced:

- one durable authority record;
- typed operations;
- conservative human-input supersession;
- serialized compare-and-swap transitions;
- one-shot polling with idempotency;
- explicit crash and migration behavior.

A fresh-context breaker then required the final design to specify exact Pi
pairing, persistence serialization, durable ordering, status grammar, rollback,
and module ownership. Those findings are incorporated above.

The exact final design received a rigorous governed adversarial verdict of
**APPROVE** with no blocking findings.
