# Typed Wait Authority Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent arbitrary or stale wait metadata from displacing the latest genuine user task while retaining narrowly validated, race-safe YANOS-builder polling.

**Architecture:** Add a deep `wait_state` module whose small interface owns typed Pi tool-pair parsing, the single durable authority record, serialized compare-and-swap transitions, and wait validation. `pi_session_lib`, `progress_lib`, artifacts, the bridge, and the TypeScript extension become adapters or inert projections; every poll/reinject action revalidates the current authority generation, so stale timers and cleanup failures cannot cause work.

**Tech Stack:** Python 3.14 standard library (`dataclasses`, `fcntl`, `json`, `os`, `shlex`, `tempfile`), pytest, TypeScript, Bun test, Pi extension events and bridge CLI.

## Global Constraints

- Edit only `/srv/dev/ras/autocompactor`; the Opticboard/Rock 5C repositories are regression-oracle data, not implementation targets.
- Do not modify Pi core or historical Pi session JSONL.
- Follow strict TDD: add each behavioral test, run it red for the intended reason, add minimal production code, then run it green.
- Only exact typed YANOS-builder `show` operations remain automatic; assistant prose, read/search output, `task_list`, subagent waits, arbitrary shell, and legacy wait records become advisory.
- Any nontrivial later interactive/RPC human input supersedes automation before the prompt reaches the agent; only exact whole-message matches in the approved trivial allowlist are excluded.
- One schema-v2 `wait_authority` record in the per-session state file is the sole automation authority. `open_work`, progress hits, artifacts, digest text, and TypeScript memory are projections only.
- Every prepare, reinject, pre-poll, and post-poll action revalidates continuation ID, generation, state, phase, authorized user entry, and typed operation.
- All state mutations use one `flock` plus compare-and-swap and atomic `fsync` + `os.replace`; lock or persistence failure fails closed to no automation.
- Timer cancellation is cleanup only. Stale callbacks must be harmless after supersession, tombstone, restart, rollback, or a newer generation.
- State/bridge hooks remain never-raise. Unknown, malformed, conflicting, duplicate, truncated, corrupt, or legacy inputs produce advisory/no automation.
- Telemetry remains content-free and may contain only the enumerated fields in the approved spec.
- Preserve user-owned worktree state and avoid unrelated refactors.

---

## File Structure

| Path | Responsibility |
|---|---|
| `src/autocompactor/wait_state.py` | New deep module: typed operation parsing, Pi call/result pairing, state locking/persistence, authority registration, validation, transitions, polling handshake, migration, telemetry fields. |
| `src/autocompactor/pi_session_lib.py` | Produce ordered active-branch user/tool facts; stop promoting arbitrary tool output into autonomous waits. |
| `src/autocompactor/progress_lib.py` | Render validated wait authority as a projection; never manufacture confidence/affinity from advisory `open_work`. |
| `src/autocompactor/pi_bridge.py` | Thin CLI adapter for `wait-register`, `wait-check`, and `wait-transition`; prepare/reinject consume authority through `wait_state`. |
| `src/autocompactor/artifacts.py` | Keep wait projections advisory and remove legacy wait projections best-effort without granting authority. |
| `src/pi/autocompactor.ts` | Input supersession and one-shot timer handshake; timer stores only continuation ID/generation/idempotency key. |
| `tests/test_wait_state.py` | New focused unit/integration tests for parsing, state transitions, locking, CAS, recovery, migration, and telemetry. |
| `tests/test_pi_session_lib.py` | Ordered fact extraction and retained-context/user-order behavior. |
| `tests/test_open_work.py` | Exact incident regression and advisory-open-work behavior. |
| `tests/test_progress_lib.py` | Projection cannot grant autonomous mode; validated authority can. |
| `tests/test_pi_bridge.py` | Bridge subcommand contract, prepare/reinject revalidation, legacy cleanup, and corrupt-state failure behavior. |
| `tests/shim_wait_resume.test.ts` | Input-event supersession, one-shot timers, bridge failure, stale callbacks, restart, rollback, and no-second-poll behavior. |
| `tests/fixtures/pi/false_wait_docs.jsonl` | Minimal real-session-shaped Opticboard task + `read` documentation false-positive fixture. |
| `tests/fixtures/pi/typed_wait_running.jsonl` | Exact eligible `yanos-builder show` pair with `running`. |
| `tests/fixtures/pi/typed_wait_terminal.jsonl` | Exact eligible pair with a terminal status. |
| `tests/smoke_test_pi.sh` | End-to-end incident and positive typed-wait bridge smoke checks. |
| `README.md` | Document automatic-wait scope, authority semantics, and rollback modes. |
| `HANDOFF.md` | Record the incident, compatibility change, and verification result. |

## Shared Interfaces

These interfaces are fixed for all tasks. Implementers must use these names and shapes rather than inventing parallel authority paths.

```python
# src/autocompactor/wait_state.py
from dataclasses import dataclass
from typing import Any, Callable, Literal, TypeVar

WAIT_SCHEMA_VERSION = 2
WaitStatus = Literal["OPEN", "TERMINAL", "SUPERSEDED", "INVALID"]
WaitPhase = Literal["READY", "POLL_ISSUED"]
WaitVerdict = Literal["OPEN", "TERMINAL", "SUPERSEDED", "INVALID", "STALE", "DISABLED"]

@dataclass(frozen=True)
class UserFact:
    entry_id: str
    order: int
    text: str
    trivial: bool

@dataclass(frozen=True)
class ToolPairFact:
    call_entry_id: str
    call_order: int
    call_id: str
    tool_name: str
    command: str
    assistant_stop_reason: str
    result_entry_id: str
    result_order: int
    result_tool_name: str
    is_error: bool
    content_types: tuple[str, ...]
    text: str
    duplicate_results: int
    truncated: bool

@dataclass(frozen=True)
class SessionWaitSnapshot:
    latest_nontrivial_user: UserFact | None
    pairs: tuple[ToolPairFact, ...]
    complete: bool

@dataclass(frozen=True)
class WaitDecision:
    verdict: WaitVerdict
    reason: str
    authority: dict[str, Any] | None = None
    operation: dict[str, Any] | None = None
    idempotency_key: str = ""

T = TypeVar("T")

def inspect_session(session_path: str) -> SessionWaitSnapshot: ...
def parse_show_operation(command: str) -> dict[str, Any] | None: ...
def parse_show_result(operation: dict[str, Any], pair: ToolPairFact) -> Literal["OPEN", "TERMINAL", "INVALID"]: ...
def read_state(session_id: str) -> dict[str, Any]: ...
def mutate_state(session_id: str, transform: Callable[[dict[str, Any]], tuple[dict[str, Any], T]]) -> T: ...
def register_latest(session_id: str, snapshot: SessionWaitSnapshot, enabled: bool) -> WaitDecision: ...
def validate_ready(session_id: str, snapshot: SessionWaitSnapshot, enabled: bool) -> WaitDecision: ...
def issue_poll(session_id: str, continuation_id: str, generation: int, snapshot: SessionWaitSnapshot, enabled: bool) -> WaitDecision: ...
def acknowledge_poll(session_id: str, idempotency_key: str, snapshot: SessionWaitSnapshot, enabled: bool) -> WaitDecision: ...
def supersede(session_id: str, reason: str = "human_input") -> WaitDecision: ...
def invalidate_unresolved_issue(session_id: str) -> WaitDecision: ...
def migrate_legacy_state(session_id: str) -> dict[str, int]: ...
def content_free_fields(decision: WaitDecision) -> dict[str, Any]: ...
```

Bridge JSON uses camelCase only at the TypeScript seam:

```json
{
  "waitVerdict": "OPEN",
  "waitReason": "validated",
  "waitAuthority": {
    "continuationId": "opaque",
    "generation": 4,
    "phase": "READY"
  },
  "waitOperation": {
    "kind": "yanos_builder_show",
    "resourceId": "Y260803-092025",
    "json": false
  },
  "waitIdempotencyKey": "opaque:4:1"
}
```

---

### Task 1: Lock Down the False-Wait Reproduction and Ordered Pi Facts

**Files:**
- Create: `tests/fixtures/pi/false_wait_docs.jsonl`
- Modify: `tests/test_open_work.py`
- Modify: `tests/test_pi_session_lib.py`
- Modify: `src/autocompactor/pi_session_lib.py:217-234,356-621`

**Interfaces:**
- Consumes: Pi v3 active root-to-leaf session entries.
- Produces: `pi_session_lib.wait_snapshot(path) -> dict` with literal keys `latest_nontrivial_user`, `pairs`, and `complete`; Task 2 converts this transport-neutral dict to `SessionWaitSnapshot`.

- [ ] **Step 1: Add the exact real-session-shaped regression fixture**

Create `tests/fixtures/pi/false_wait_docs.jsonl` with a linear branch containing:

```jsonl
{"type":"session","version":3,"id":"false-wait-docs","timestamp":"2026-08-03T23:00:00.000Z","cwd":"/srv/dev/yanos-project/yanos-os"}
{"type":"message","id":"u1","parentId":null,"timestamp":"2026-08-03T23:00:01.000Z","message":{"role":"user","content":[{"type":"text","text":"Produce the reviewed cross-repository plan for optic-5c to rock-5c, opticboard-cm3-16gb-2gb, shared core by default, and hw-opticboard drift correction."}],"timestamp":1785798001000}}
{"type":"message","id":"a1","parentId":"u1","timestamp":"2026-08-03T23:00:02.000Z","message":{"role":"assistant","content":[{"type":"toolCall","id":"read-contract","name":"read","arguments":{"path":"docs/conventions/image-bringup-agent.md"}}],"api":"responses","provider":"test","model":"test","usage":{"input":10,"output":5,"cacheRead":0,"cacheWrite":0,"totalTokens":15,"cost":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0,"total":0}},"stopReason":"toolUse","timestamp":1785798002000}}
{"type":"message","id":"r1","parentId":"a1","timestamp":"2026-08-03T23:00:03.000Z","message":{"role":"toolResult","toolCallId":"read-contract","toolName":"read","isError":false,"content":[{"type":"text","text":"Never claim done from enqueued high priority. Authority: 3. catalog / `yanos-builder show` status and git_sha (advisory; may lag or lie). When monitoring a build id: poll once; if terminal stop polling."}],"timestamp":1785798003000}}
```

- [ ] **Step 2: Add a failing incident test at the real analyzer seam**

Append to `tests/test_open_work.py`:

```python
def test_read_documentation_cannot_displace_opticboard_task():
    fixture = os.path.join(FIX, "pi", "false_wait_docs.jsonl")
    st = pi_session_lib.analyze(fixture)
    step, source = transcript_lib.resolve_next_step(st)

    assert not any(
        item.get("kind") == "waiting_monitor" and item.get("autonomous", True)
        for item in st.open_work
    ), st.open_work
    assert source == "last_user_task"
    assert "opticboard-cm3-16gb-2gb" in step
    assert "catalog" not in step.lower()
```

The production mutation this catches is re-enabling generic tool-result text as live wait evidence.

- [ ] **Step 3: Run the incident test and record the expected red result**

Run:

```bash
python3 -m pytest tests/test_open_work.py::test_read_documentation_cannot_displace_opticboard_task -q
```

Expected: FAIL because current `pi_session_lib` creates `waiting_monitor` and `resolve_next_step()` returns `open_work:waiting_monitor`.

- [ ] **Step 4: Add failing ordered-fact tests**

Add to `tests/test_pi_session_lib.py`:

```python
def test_wait_snapshot_pairs_tool_results_by_exact_call_id():
    fixture = PI_FIX / "false_wait_docs.jsonl"
    snapshot = pi_session_lib.wait_snapshot(str(fixture))

    assert snapshot["complete"] is True
    assert snapshot["latest_nontrivial_user"] == {
        "entry_id": "u1",
        "order": 0,
        "text": (
            "Produce the reviewed cross-repository plan for optic-5c to "
            "rock-5c, opticboard-cm3-16gb-2gb, shared core by default, "
            "and hw-opticboard drift correction."
        ),
        "trivial": False,
    }
    assert snapshot["pairs"] == [{
        "call_entry_id": "a1",
        "call_order": 1,
        "call_id": "read-contract",
        "tool_name": "read",
        "command": "",
        "assistant_stop_reason": "toolUse",
        "result_entry_id": "r1",
        "result_order": 2,
        "result_tool_name": "read",
        "is_error": False,
        "content_types": ("text",),
        "text": (
            "Never claim done from enqueued high priority. Authority: 3. "
            "catalog / `yanos-builder show` status and git_sha (advisory; "
            "may lag or lie). When monitoring a build id: poll once; if "
            "terminal stop polling."
        ),
        "duplicate_results": 1,
        "truncated": False,
    }]
```

Add a branch test where a result on an abandoned branch is not returned, and a duplicate-result fixture constructed with `_write_jsonl` yields `duplicate_results == 2`.

- [ ] **Step 5: Run ordered-fact tests red**

Run:

```bash
python3 -m pytest tests/test_pi_session_lib.py -k 'wait_snapshot' -q
```

Expected: FAIL with `AttributeError` because `wait_snapshot` does not exist.

- [ ] **Step 6: Implement ordered fact extraction without authority decisions**

Add this public function in `pi_session_lib.py` after `active_path()`:

```python
def wait_snapshot(path: str) -> dict:
    full_path, active, _ = active_path(path)
    entries = active
    calls = {}
    results = {}
    latest_user = None

    for order, entry in enumerate(entries):
        msg = _message(entry)
        role = msg.get("role")
        if role == "user":
            text = _message_text(msg).strip()
            if text and not text.startswith("/") and "<command-name>" not in text:
                trivial = transcript_lib.is_trivial_user_ping(text)
                if not trivial:
                    latest_user = {
                        "entry_id": str(entry.get("id") or ""),
                        "order": order,
                        "text": text,
                        "trivial": False,
                    }
        elif role == "assistant":
            stop_reason = str(msg.get("stopReason") or "")
            for call in _tool_calls(msg):
                call_id = str(call.get("id") or "")
                if not call_id:
                    continue
                args = call.get("arguments") or {}
                calls[call_id] = {
                    "call_entry_id": str(entry.get("id") or ""),
                    "call_order": order,
                    "call_id": call_id,
                    "tool_name": str(call.get("name") or ""),
                    "command": str(args.get("command") or ""),
                    "assistant_stop_reason": stop_reason,
                }
        elif role == "toolResult":
            call_id = str(msg.get("toolCallId") or "")
            if not call_id:
                continue
            blocks = _content_blocks(msg)
            result = {
                "result_entry_id": str(entry.get("id") or ""),
                "result_order": order,
                "result_tool_name": str(msg.get("toolName") or ""),
                "is_error": bool(msg.get("isError")),
                "content_types": tuple(
                    str(block.get("type") or "")
                    for block in blocks if isinstance(block, dict)
                ),
                "text": _tool_result_text(msg),
                "truncated": "[Output truncated:" in _tool_result_text(msg),
            }
            results.setdefault(call_id, []).append(result)

    pairs = []
    for call_id, call in calls.items():
        matched = results.get(call_id, [])
        if not matched:
            continue
        for result in matched:
            pairs.append({**call, **result, "duplicate_results": len(matched)})
    pairs.sort(key=lambda item: (item["result_order"], item["call_order"]))
    return {
        "latest_nontrivial_user": latest_user,
        "pairs": pairs,
        "complete": bool(full_path) and entries is not None,
    }
```

Do not add wait validation or status parsing here. This module reports ordered facts only.

- [ ] **Step 7: Stop generic tool results from creating autonomous wait entries**

In `analyze_active_prefix()`, replace the generic `live_wait` block under
`role == "toolResult"` with advisory-only behavior. The block must not call
`extract_open_work_from_text()` unless the pending tool is `bash` and the
command itself names a concrete build ID. Mark any retained prose hint:

```python
hit["autonomous"] = False
hit["source_role"] = "toolResult"
hit["source_tool"] = tool_name
```

For `read`, `grep`, `find`, unknown tools, and unmatched results, do not add
`waiting_monitor` at all. Keep terminal-resource extraction for existing
advisory cleanup until Task 3 replaces authority use.

- [ ] **Step 8: Run Task 1 tests green and commit**

Run:

```bash
python3 -m pytest tests/test_open_work.py::test_read_documentation_cannot_displace_opticboard_task tests/test_pi_session_lib.py -k 'wait_snapshot or read_documentation' -q
python3 -m pytest tests/test_open_work.py tests/test_pi_session_lib.py -q
```

Expected: PASS; the false document no longer creates an autonomous wait and ordered facts preserve exact tool provenance.

Commit:

```bash
git add tests/fixtures/pi/false_wait_docs.jsonl tests/test_open_work.py tests/test_pi_session_lib.py src/autocompactor/pi_session_lib.py
git commit -m "fix(wait): reject documentation as runtime wait evidence"
```

---

### Task 2: Build the Typed Wait Parser and Atomic Authority Module

**Files:**
- Create: `src/autocompactor/wait_state.py`
- Create: `tests/test_wait_state.py`
- Create: `tests/fixtures/pi/typed_wait_running.jsonl`
- Create: `tests/fixtures/pi/typed_wait_terminal.jsonl`
- Modify: `src/autocompactor/pi_session_lib.py` only if Task 1 fact shape needs a bug fix revealed by these tests

**Interfaces:**
- Consumes: `pi_session_lib.wait_snapshot()` dict.
- Produces: all shared `wait_state.py` interfaces defined above; generic state persistence becomes the sole state-file interface used by Task 3.

- [ ] **Step 1: Add positive typed fixtures**

Create `typed_wait_running.jsonl` with one nontrivial user message, an assistant
`bash` call whose exact command is `yanos-builder show Y260803-092025`, and a
paired non-error text result:

```text
Build Y260803-092025
  status  : running
```

Create `typed_wait_terminal.jsonl` with the same pair and:

```text
Build Y260803-092025
  status  : failed
```

Every tool result must include `toolName: "bash"`, and every assistant must
use `stopReason: "toolUse"`.

- [ ] **Step 2: Write failing command-parser tables**

Create `tests/test_wait_state.py` and add literal tables:

```python
import concurrent.futures
import json
import os
import pathlib
import subprocess
import sys

from autocompactor import pi_session_lib, wait_state

VALID = [
    ("yanos-builder show Y260803-092025", False),
    ("yanos-builder --json show Y260803-092025", True),
]
INVALID = [
    "yanos-builder show Y260803-092025 --json",
    "yanos-builder show Y260803-092025 | jq .status",
    "yanos-builder show $BUILD_ID",
    "yanos-builder show $(cat bid)",
    "yanos-builder show Y260803-092025 > status.txt",
    "yanos-builder show Y260803-092025 && echo done",
    "yanos-builder show Y260803-092025; echo done",
    "yanos-builder show Y260803-092025 Y260803-092026",
    "echo yanos-builder show Y260803-092025",
    "yanos-builder status Y260803-092025",
    "yanos-builder show X260803-092025",
]

def test_parse_show_operation_accepts_only_exact_argv():
    for command, json_mode in VALID:
        assert wait_state.parse_show_operation(command) == {
            "kind": "yanos_builder_show",
            "resource_id": "Y260803-092025",
            "json": json_mode,
        }
    for command in INVALID:
        assert wait_state.parse_show_operation(command) is None, command
```

Add pair/result tests for wrong tool, duplicate result, out-of-order result,
error, aborted assistant, image block, truncation marker, mismatched ID,
substring ID, multiple Build headers, duplicate/conflicting statuses, unknown
status, malformed JSON, and wrong JSON `build_id`.

- [ ] **Step 3: Run parser tests red**

Run:

```bash
python3 -m pytest tests/test_wait_state.py -k 'parse_show or pair or result' -q
```

Expected: collection ERROR because `autocompactor.wait_state` does not exist.

- [ ] **Step 4: Implement exact typed parsing**

Create `wait_state.py` with the shared dataclasses and these non-negotiable
parser rules:

```python
_BUILD_ID_RE = re.compile(r"Y[0-9]{6}-[0-9]+\Z")
_TERMINAL = frozenset({"succeeded", "failed", "aborted", "cancelled"})
_OPEN = frozenset({"running", "queued"})
_SHELL_META_RE = re.compile(r"(?:&&|\|\||[|;<>`$])")

def parse_show_operation(command: str):
    if not isinstance(command, str) or _SHELL_META_RE.search(command):
        return None
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return None
    json_mode = argv == ["yanos-builder", "--json", "show", argv[-1]] if len(argv) == 4 else False
    plain_mode = len(argv) == 3 and argv[:2] == ["yanos-builder", "show"]
    if not plain_mode and not (
        len(argv) == 4 and argv[:3] == ["yanos-builder", "--json", "show"]
    ):
        return None
    resource_id = argv[-1]
    if not _BUILD_ID_RE.fullmatch(resource_id):
        return None
    return {"kind": "yanos_builder_show", "resource_id": resource_id, "json": json_mode}
```

Before parsing a result, require:

```python
pair.tool_name == "bash"
pair.result_tool_name == "bash"
pair.assistant_stop_reason == "toolUse"
pair.is_error is False
pair.content_types == ("text",)
pair.duplicate_results == 1
pair.call_order < pair.result_order
pair.truncated is False
```

For plain output, split into Build blocks, require exactly one block and exact
ID, then exactly one `status\s*:\s*([A-Za-z_-]+)` match. For JSON, require one
dict with exact `build_id` and string `status`. Normalize status with lowercase
and `_` to `-`; map only the approved OPEN/TERMINAL sets. Return `INVALID` for
everything else.

- [ ] **Step 5: Write failing state-machine and persistence tests**

Add tests that assert:

```python
def test_register_issue_ack_terminal_is_monotonic(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOCOMPACTOR_STATE_DIR", str(tmp_path))
    running = wait_state.inspect_session(str(PI_FIX / "typed_wait_running.jsonl"))
    registered = wait_state.register_latest("typed_wait_running", running, enabled=True)
    assert registered.verdict == "OPEN"
    authority = registered.authority
    assert authority["generation"] == 1
    assert authority["status"] == "OPEN"
    assert authority["phase"] == "READY"

    issued = wait_state.issue_poll(
        "typed_wait_running", authority["continuation_id"], 1, running, True
    )
    assert issued.verdict == "OPEN"
    assert issued.idempotency_key.endswith(":1:1")
    assert issued.authority["phase"] == "POLL_ISSUED"

    duplicate = wait_state.issue_poll(
        "typed_wait_running", authority["continuation_id"], 1, running, True
    )
    assert duplicate.verdict == "STALE"
```

Add separate tests for:

- exact running acknowledgement returns `OPEN/READY`;
- exact terminal acknowledgement returns `TERMINAL`;
- unrelated terminal ID returns `INVALID`, not terminal for the authority;
- supersede makes old issue/ack calls `STALE`;
- a fresh pair after supersession creates generation 2;
- `POLL_ISSUED` on restart becomes `INVALID` through
  `invalidate_unresolved_issue()`;
- disabled registration/check returns `DISABLED`;
- corrupt JSON, unknown schema/status/phase/operation returns no authority;
- `content_free_fields()` contains no IDs, paths, commands, text, or hashes;
- two `multiprocessing`/subprocess writers racing on expected generation yield
  one successful transition and one `STALE`;
- a forced write failure leaves the original state file byte-identical.

- [ ] **Step 6: Run state tests red**

Run:

```bash
python3 -m pytest tests/test_wait_state.py -k 'register or issue or acknowledge or supersede or corrupt or race' -q
```

Expected: FAIL because state/persistence functions are missing.

- [ ] **Step 7: Implement atomic generic state persistence**

Use the following concrete write sequence in `mutate_state()`:

```python
root = statedir.state_root("pi")
os.makedirs(root, exist_ok=True)
state_path = os.path.join(root, f"{session_id}.state.json")
lock_path = os.path.join(root, f"{session_id}.state.lock")
with open(lock_path, "a+", encoding="utf-8") as lock_fh:
    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
    state = _read_json_dict(state_path)
    next_state, result = transform(dict(state))
    fd, tmp_path = tempfile.mkstemp(prefix=f".{session_id}.", suffix=".tmp", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_fh:
            json.dump(next_state, tmp_fh, separators=(",", ":"), sort_keys=True)
            tmp_fh.flush()
            os.fsync(tmp_fh.fileno())
        os.replace(tmp_path, state_path)
        dir_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
    return result
```

Wrap the public never-raise functions so lock/read/write exceptions return
`WaitDecision("INVALID", "state_io")` or `{}` without leaking a traceback.
Tests may call a private strict helper to assert write failure preservation.

- [ ] **Step 8: Implement CAS transitions and migration**

Generate `continuation_id` with `secrets.token_hex(16)`. Allocate generation
under lock from `state.get("wait_generation", 0) + 1`. Store raw operation and
source IDs in state, but never include them in telemetry.

`register_latest()` must choose only the latest eligible pair after the latest
nontrivial user fact. `validate_ready()` must require the authorized user entry
still matches the snapshot. `issue_poll()` must atomically change
`READY -> POLL_ISSUED` and increment sequence. `acknowledge_poll()` must parse
only a later eligible result for the same exact typed operation and perform the
transition table in the spec.

`migrate_legacy_state()` must remove these keys when they are wait-shaped and
no schema-v2 authority exists:

```python
("staged_next_step", "staged_next_step_src", "staged_open_work",
 "staged_progress", "staged_progress_resume")
```

It must preserve unrelated keys such as `last_reco_tokens` and
`compaction_count`.

- [ ] **Step 9: Run Task 2 tests green and commit**

Run:

```bash
python3 -m pytest tests/test_wait_state.py tests/test_pi_session_lib.py -q
```

Expected: PASS, including the two-process CAS test.

Commit:

```bash
git add src/autocompactor/wait_state.py src/autocompactor/pi_session_lib.py tests/test_wait_state.py tests/fixtures/pi/typed_wait_running.jsonl tests/fixtures/pi/typed_wait_terminal.jsonl
git commit -m "feat(wait): add typed authority state machine"
```

---

### Task 3: Make the Bridge, Progress, and Artifacts Consume Authority

**Files:**
- Modify: `src/autocompactor/pi_bridge.py:83-106,373-627`
- Modify: `src/autocompactor/progress_lib.py:540-588,668-787`
- Modify: `src/autocompactor/transcript_lib.py:1093-1175`
- Modify: `src/autocompactor/artifacts.py:84-151,190-260`
- Modify: `tests/test_pi_bridge.py`
- Modify: `tests/test_progress_lib.py`
- Modify: `tests/test_open_work.py`

**Interfaces:**
- Consumes: `wait_state` functions and `SessionWaitSnapshot`.
- Produces: bridge commands `wait-register`, `wait-check`, and `wait-transition`; reinject authority JSON at the TS seam.

- [ ] **Step 1: Add failing bridge command tests**

Extend `tests/test_pi_bridge.py`:

```python
def test_wait_register_check_and_supersede_cli(tmp_path):
    session = REPO_ROOT / "tests" / "fixtures" / "pi" / "typed_wait_running.jsonl"
    registered = parse_single_json(run_bridge(
        ["wait-register", "--session", str(session)], tmp_path
    ).stdout)
    assert registered["waitVerdict"] == "OPEN"
    auth = registered["waitAuthority"]
    assert auth["phase"] == "READY"

    checked = parse_single_json(run_bridge([
        "wait-check", "--session", str(session),
        "--continuation-id", auth["continuationId"],
        "--generation", str(auth["generation"]),
    ], tmp_path).stdout)
    assert checked["waitVerdict"] == "OPEN"
    assert checked["waitAuthority"]["phase"] == "POLL_ISSUED"
    assert checked["waitIdempotencyKey"].endswith(":1:1")

    superseded = parse_single_json(run_bridge([
        "wait-transition", "--session", str(session),
        "--reason", "superseded",
    ], tmp_path).stdout)
    assert superseded["waitVerdict"] == "SUPERSEDED"
```

Also test missing flags, bad generation, unknown subcommand, corrupt state, and
`AUTOCOMPACTOR_NEXTSTEP_WAIT=advisory|off` all exit 0 and return no authority.

- [ ] **Step 2: Add failing prepare/reinject incident and positive tests**

Add tests:

```python
def test_prepare_reinject_false_docs_keeps_opticboard_task(tmp_path):
    fixture = REPO_ROOT / "tests" / "fixtures" / "pi" / "false_wait_docs.jsonl"
    run_bridge(["prepare", "--session", str(fixture)], tmp_path)
    data = parse_single_json(run_bridge(
        ["reinject", "--session", str(fixture)], tmp_path
    ).stdout)
    assert data["nextStepSource"] == "last_user_task"
    assert "opticboard-cm3-16gb-2gb" in data["nextStep"]
    assert data["nextStepWait"] is False
    assert "waitAuthority" not in data
```

For `typed_wait_running.jsonl`, prepare must register authority and reinject
must return `nextStepWait=true`, `waitAuthority.continuationId`, generation 1,
and a structured `waitOperation`. A second reinject for the same generation
must not issue a second poll or mutate phase.

- [ ] **Step 3: Add failing projection tests**

In `tests/test_progress_lib.py`, add:

```python
def test_advisory_open_work_cannot_become_wait_winner():
    st = transcript_lib.TranscriptStats()
    st.last_user_task = "Produce the Opticboard rename plan"
    st.open_work = [{
        "kind": "waiting_monitor",
        "summary": "catalog status",
        "resource_ids": [],
        "monitor_cmds": [],
        "confidence": "medium",
        "autonomous": False,
    }]
    result = progress_lib.extract_all(st, cwd="")
    assert not any(hit.get("mode") == "wait" for hit in result["hits"])
    step, source = transcript_lib.resolve_next_step(st)
    assert source == "last_user_task"
```

Add a validated-authority projection test whose input is the explicit dict:

```python
{
  "schema_version": 2,
  "status": "OPEN",
  "phase": "READY",
  "operation": {"kind": "yanos_builder_show", "resource_id": "Y260803-092025", "json": False}
}
```

and assert it can produce `mode=wait` only when passed through the new explicit
`wait_authority=` argument.

- [ ] **Step 4: Run bridge/projection tests red**

Run:

```bash
python3 -m pytest tests/test_pi_bridge.py -k 'wait_register or false_docs or typed_wait' -q
python3 -m pytest tests/test_progress_lib.py -k 'advisory_open_work or validated_authority' -q
```

Expected: FAIL because commands and explicit authority parameters do not exist.

- [ ] **Step 5: Replace bridge state I/O with the shared atomic seam**

Make `_load_state()` call `wait_state.read_state()`. Remove direct overwrite
from `_save_state()` and convert each load-modify-save block to
`wait_state.mutate_state()` with a transform that changes only intended keys.
Do not retain any plain `open(..., "w")` state write in `pi_bridge.py`.

Add decision serialization:

```python
def _wait_json(decision):
    out = {"waitVerdict": decision.verdict, "waitReason": decision.reason}
    if decision.authority:
        out["waitAuthority"] = {
            "continuationId": decision.authority["continuation_id"],
            "generation": decision.authority["generation"],
            "phase": decision.authority.get("phase", "READY"),
        }
    if decision.operation:
        out["waitOperation"] = {
            "kind": decision.operation["kind"],
            "resourceId": decision.operation["resource_id"],
            "json": bool(decision.operation.get("json")),
        }
    if decision.idempotency_key:
        out["waitIdempotencyKey"] = decision.idempotency_key
    return out
```

Wire CLI handlers exactly:

```python
handler = {
    "evaluate": cmd_evaluate,
    "prepare": cmd_prepare,
    "reinject": cmd_reinject,
    "wait-register": cmd_wait_register,
    "wait-check": cmd_wait_check,
    "wait-transition": cmd_wait_transition,
}.get(cmd)
```

- [ ] **Step 6: Make prepare/reinject validate authority**

At prepare:

1. inspect session;
2. migrate legacy state/artifacts;
3. call `register_latest()` only when `NEXTSTEP_WAIT == "poll"`;
4. call `validate_ready()`;
5. pass validated authority explicitly to progress extraction and
   `resolve_next_step()`;
6. stage ordinary next-step keys only for non-wait work;
7. log only `content_free_fields()`.

At reinject, call `validate_ready()` immediately before building output. Emit
wait fields only for `OPEN/READY`. Never infer wait from `staged_next_step_src`
or `staged_open_work`.

- [ ] **Step 7: Remove projection authority**

Change signatures:

```python
def extract_open_work_progress(st, wait_authority=None) -> list[dict]:
def extract_all(st, *, wait_authority=None, **kwargs) -> dict:
def resolve_next_step(st, progress_position=None, wait_authority=None) -> tuple:
```

If `wait_authority` is absent or invalid, `extract_open_work_progress()` returns
no `mode=wait` hit. It may return an advisory code hit only if a caller needs
digest visibility, with original confidence and `affinity=False`.

For valid authority, format the wait brief from the typed operation and use
`resource_ids=[operation["resource_id"]]`; do not read authorization from
`st.open_work`.

- [ ] **Step 8: Make artifacts explicitly advisory and migrate them**

In `artifacts._sections()`, label unvalidated retained entries:

```text
OPEN WORK (advisory context only — does not authorize polling):
```

Do not emit `PLAN POSITION ... resume this unit` for legacy
`surface == "open_work"` wait records. Add an artifact cleanup helper that
removes wait-shaped `open_work` and `progress_position` when no validated
schema-v2 authority exists, while preserving files, corrections, commands, and
founding prompts.

- [ ] **Step 9: Run Task 3 tests green and commit**

Run:

```bash
python3 -m pytest tests/test_wait_state.py tests/test_pi_bridge.py tests/test_progress_lib.py tests/test_open_work.py -q
```

Expected: PASS. Confirm `rg -n 'open\(_state_path.*"w"|json.dump\(state' src/autocompactor/pi_bridge.py` finds no direct state overwrite.

Commit:

```bash
git add src/autocompactor/pi_bridge.py src/autocompactor/progress_lib.py src/autocompactor/transcript_lib.py src/autocompactor/artifacts.py tests/test_pi_bridge.py tests/test_progress_lib.py tests/test_open_work.py
git commit -m "refactor(wait): make bridge consume typed authority"
```

---

### Task 4: Replace Recurring TypeScript Polls with the One-Shot Handshake

**Files:**
- Modify: `src/pi/autocompactor.ts:139-178,430-680,682-833,963-1145`
- Modify: `tests/shim_wait_resume.test.ts`
- Modify: `src/pi/test/extension.test.mjs` if installed-source contract coverage belongs there

**Interfaces:**
- Consumes: bridge wait JSON from Task 3.
- Produces: input-boundary supersession and a one-shot timer that can act only after bridge `wait-check` returns current authority.

- [ ] **Step 1: Upgrade the test harness to support stateful bridge replies**

Change `HarnessOptions` to accept:

```typescript
type HarnessOptions = {
  bridgeResponse?: Record<string, any>
  bridgeHandler?: (subcommand: string, args: string[]) => any | Promise<any>
  idle?: boolean
}
```

In `pi.exec`, prefer `bridgeHandler`. Record bridge calls in
`bridgeCalls: Array<{ subcommand: string; args: string[] }>` and return it from
`makeHarness()`. This is a test adapter; assertions must remain on emitted
messages/timers and state transitions, not merely mock-call existence.

- [ ] **Step 2: Add failing input supersession tests**

Add tests that invoke the registered `input` handler:

```typescript
test("nontrivial human input supersedes wait before agent start", async () => {
  const h = makeHarness({
    bridgeHandler(subcommand) {
      if (subcommand === "wait-transition") return { waitVerdict: "SUPERSEDED" }
      return {}
    },
  })
  const mod = await freshShim()
  mod.default(h.pi)
  const result = await h.handlers["input"]?.(
    { text: "Return to the Opticboard rename plan", source: "interactive" }, h.ctx,
  )
  expect(result?.action).toBe("continue")
  expect(h.bridgeCalls.some((c) => c.subcommand === "wait-transition")).toBe(true)
})
```

Table-test the exact trivial allowlist and assert it does not call
`wait-transition`. Add `status? also switch to Opticboard` as a nontrivial
mixed-content case. Add source `extension` as never superseding.

- [ ] **Step 3: Add failing one-shot/stale/rollback tests**

Replace the old “schedule recurring poll” expectation with tests that prove:

1. reinject lacking `waitAuthority` never schedules, even if
   `nextStepWait=true`;
2. validated authority schedules one timer;
3. timer calls pre-fire `wait-check` before emitting a poll;
4. duplicate callback receives `STALE` and emits no second poll;
5. the next timer is not scheduled until `agent_end` acknowledgement returns
   `OPEN/READY`;
6. terminal acknowledgement clears state; advancing fake time emits no second
   poll;
7. bridge failure/invalid JSON cancels automation and emits advisory, not a
   poll;
8. old-generation callback after a new reinject is inert;
9. `NEXTSTEP_WAIT=advisory|off` after arming makes pre-fire a no-op;
10. `session_start` invalidates unresolved `POLL_ISSUED` authority and does not
    restore a timer.

The critical terminal assertion is:

```typescript
expect(polls.length).toBe(1)
for (const timer of timers.splice(0)) timer.fn()
await new Promise((resolve) => realSetTimeout(resolve, 10))
expect(h.sendMessages.filter((m) =>
  m.message?.customType === "autocompactor.nextstep.poll"
).length).toBe(1)
```

- [ ] **Step 4: Run shim tests red**

Run:

```bash
bun test tests/shim_wait_resume.test.ts
```

Expected: FAIL because the current shim trusts `nextStepWait`, pre-arms
recurring timers, and has no `input`/post-poll handshake.

- [ ] **Step 5: Implement exact human-input classification**

Add:

```typescript
const TRIVIAL_WAIT_INPUTS = new Set([
  "status?", "status", "ok", "okay", "thanks", "thank you", "thx",
  "?", "…", "...", "y", "n", "yes", "no", "k", "kk", "cool", "great",
])

function isTrivialWaitInput(text: unknown): boolean {
  return TRIVIAL_WAIT_INPUTS.has(String(text ?? "").trim().toLowerCase())
}
```

Register `pi.on("input", ...)`. For `interactive|rpc` and nontrivial text,
await bridge `wait-transition --reason superseded`; always return
`{ action: "continue" }`. On bridge failure, clear local wait state before
continuing so automation fails closed.

- [ ] **Step 6: Replace timer state with authority tokens**

Change `WaitPollState` to contain:

```typescript
type WaitPollState = {
  continuationId: string
  generation: number
  compactionId: string
  delayMs: number
  timer: ReturnType<typeof setTimeout> | null
  idempotencyKey?: string
}
```

Delete frozen `brief`, `stepSrc`, and `remaining` as authorization fields. UI
may render bridge-returned operation data for the current validated call only.

- [ ] **Step 7: Implement the pre-fire/post-poll handshake**

`fireWaitPoll()` becomes async:

1. capture local ID/generation;
2. clear the timer handle immediately;
3. call bridge `wait-check` with ID/generation;
4. require `OPEN`, `POLL_ISSUED`, operation, and idempotency key;
5. emit one poll custom message with `triggerTurn:true`;
6. store the idempotency key;
7. do not schedule another timer.

At the top of `agent_end`, before compaction evaluation, if an idempotency key
exists:

1. call `wait-check --after-poll`;
2. if `OPEN/READY`, schedule one next timer;
3. otherwise clear local state and surface an advisory/status reason;
4. return to ordinary compaction evaluation only after acknowledgement is
   resolved.

Add `session_start` and `session_shutdown` cleanup. On startup, call the bridge
unresolved-issue invalidation path; never infer a timer from transcript text.

- [ ] **Step 8: Gate reinject on authority tokens**

In `session_compact`, `waitShaped` is true only when all are present and valid:

```typescript
inj?.nextStepWait === true
typeof inj?.waitAuthority?.continuationId === "string"
Number.isInteger(inj?.waitAuthority?.generation)
inj?.waitAuthority?.phase === "READY"
```

A legacy boolean without authority becomes advisory. `flushAutoResume()` passes
ID/generation to `scheduleWaitPoll()` and never schedules from a string source
tag.

- [ ] **Step 9: Run Task 4 tests green and commit**

Run:

```bash
bun test tests/shim_wait_resume.test.ts
node --test 'src/pi/test/*.test.mjs'
```

Expected: all tests PASS; no recurring timer exists without an acknowledgement.

Commit:

```bash
git add src/pi/autocompactor.ts tests/shim_wait_resume.test.ts src/pi/test/extension.test.mjs
git commit -m "fix(wait): validate every poll generation"
```

---

### Task 5: Add Full Prepare → Reinject → Shim Regression, Migration, and Telemetry Gates

**Files:**
- Modify: `tests/shim_wait_resume.test.ts`
- Modify: `tests/test_pi_bridge.py`
- Modify: `tests/test_wait_state.py`
- Modify: `tests/smoke_test_pi.sh`
- Modify: `src/autocompactor/pi_bridge.py`
- Modify: `src/autocompactor/artifacts.py`
- Modify: `src/autocompactor/stats.py` only if an explicit allowlist helper is needed; do not expand stored content

**Interfaces:**
- Consumes: completed Python authority and TS handshake.
- Produces: one end-to-end regression proving the real displaced task remains authoritative, plus migration and telemetry evidence.

- [ ] **Step 1: Add an end-to-end incident test using the real bridge**

Extend the shim harness so one test executes `python3 src/pi_bridge.py` against
a temporary state directory and `false_wait_docs.jsonl`, rather than returning
canned reinject JSON. Drive:

1. bridge `prepare`;
2. `session_compact` / reinject;
3. timer advancement.

Assert:

```typescript
expect(allContent).toContain("opticboard-cm3-16gb-2gb")
expect(allContent.toLowerCase()).not.toContain("polling open work")
expect(types).not.toContain("autocompactor.nextstep.wait")
expect(types).not.toContain("autocompactor.nextstep.poll")
expect(h.sendMessages.some((m) =>
  m.options?.triggerTurn && String(m.message?.content || "").includes("catalog")
)).toBe(false)
```

This test must exercise source `src/pi/autocompactor.ts` and real bridge state,
not a mock response.

- [ ] **Step 2: Add failing legacy migration preservation tests**

Seed a state file containing unrelated values plus old staged waits:

```json
{
  "last_reco_tokens": 123456,
  "compaction_count": 7,
  "staged_next_step": "WAITING: catalog",
  "staged_next_step_src": "open_work:waiting_monitor",
  "staged_open_work": [{"kind":"waiting_monitor"}],
  "staged_progress": {"progress_mode":"wait"},
  "staged_progress_resume": "autonomous"
}
```

Seed artifacts containing founding prompts, files, and wait projections. After
prepare/reinject, assert unrelated values remain, wait projections cannot appear
in automation output, and founding/file artifacts remain.

- [ ] **Step 3: Add telemetry allowlist tests**

Read emitted events and recursively reject forbidden keys and string values:

```python
FORBIDDEN_KEYS = {
    "task", "text", "command", "output", "resource_id", "entry_id",
    "tool_call_id", "path", "hash", "error",
}
ALLOWED_WAIT_KEYS = {
    "type", "schema_version", "operation_kind", "status", "phase",
    "transition_reason", "source_class", "has_concrete_handle",
    "generation_delta", "stale_callback_count", "timer_cancel_count",
    "session_id", "ts",
}
```

Assert wait-transition events contain no build ID or Opticboard task text.
If `stats.log_event()` automatically adds `session_id`/`ts`, allow those keys
but do not add new free-form fields.

- [ ] **Step 4: Run the new integration/migration tests red**

Run:

```bash
python3 -m pytest tests/test_wait_state.py tests/test_pi_bridge.py -k 'legacy or telemetry' -q
bun test tests/shim_wait_resume.test.ts -t 'Opticboard|real bridge'
```

Expected: FAIL until cleanup and event shaping are complete.

- [ ] **Step 5: Complete migration and content-free logging**

Make migration idempotent. Log a fixed reason enum only:

```text
registered
terminal
superseded
invalid_handle
invalid_result
legacy_downgrade
stale_callback
rollback_disabled
state_io
```

Never pass exception text to telemetry. Exception detail may appear only in a
local user-visible advisory after sanitization; it must not enter events.

- [ ] **Step 6: Extend Pi smoke coverage**

Add smoke sections after the current reinject check:

```bash
echo "8) false read documentation cannot authorize a wait"
FALSE="$FIX/false_wait_docs.jsonl"
python3 "$BRIDGE" prepare --session "$FALSE" >/dev/null
FALSE_OUT=$(python3 "$BRIDGE" reinject --session "$FALSE")
echo "$FALSE_OUT" | grep -q 'opticboard-cm3-16gb-2gb' || fail "Opticboard task lost"
echo "$FALSE_OUT" | grep -q '"nextStepWait": true' && fail "false wait authorized" || true

echo "9) exact typed show can register current authority"
TYPED="$FIX/typed_wait_running.jsonl"
WAIT_OUT=$(python3 "$BRIDGE" wait-register --session "$TYPED")
echo "$WAIT_OUT" | grep -q '"waitVerdict": "OPEN"' || fail "typed wait not registered"
echo "$WAIT_OUT" | grep -q '"continuationId"' || fail "authority token missing"
```

- [ ] **Step 7: Run Task 5 tests green and commit**

Run:

```bash
python3 -m pytest tests/test_wait_state.py tests/test_pi_bridge.py tests/test_open_work.py tests/test_progress_lib.py -q
bun test tests/shim_wait_resume.test.ts
PI_SMOKE=1 bash tests/smoke_test_pi.sh
```

Expected: PASS. Report the exact test counts in the commit/implementation log.

Commit:

```bash
git add tests/shim_wait_resume.test.ts tests/test_pi_bridge.py tests/test_wait_state.py tests/smoke_test_pi.sh src/autocompactor/pi_bridge.py src/autocompactor/artifacts.py src/autocompactor/stats.py
git commit -m "test(wait): cover displaced-task regression end to end"
```

---

### Task 6: Document, Verify, Review, and Deploy the Extension

**Files:**
- Modify: `README.md`
- Modify: `HANDOFF.md`
- Modify: `AGENTS.md` only if architecture table needs the new module; add outside managed routing markers
- Verify only: all implementation and tests

**Interfaces:**
- Consumes: completed implementation.
- Produces: documented compatibility contract, green verification, review receipt, installed extension, and a live source-vs-installed identity check.

- [ ] **Step 1: Update architecture and behavior documentation**

Add `wait_state.py` to the architecture table with this role:

```text
single typed wait-authority record, exact YANOS show parser, serialized CAS transitions, one-shot poll handshake
```

Document in `README.md`:

- only exact YANOS-builder show waits are automatic;
- prose/read/search/task/subagent waits are advisory;
- any nontrivial human input cancels automation;
- `NEXTSTEP_WAIT=poll|advisory|off` behavior;
- legacy wait state is not grandfathered;
- invalid bridge/state fails closed.

Add a dated `HANDOFF.md` entry naming the incident fixture and commits, without
copying task text into telemetry guidance.

- [ ] **Step 2: Run the complete project verification matrix**

Run exactly:

```bash
python3 -m pytest tests/ -q
bun test tests/shim_prepare.test.ts tests/shim_wait_resume.test.ts
PI_SMOKE=1 bash tests/smoke_test_pi.sh
node --test 'src/pi/test/*.test.mjs'
python3 src/install_pi.py --status
```

Expected:

- every command exits 0;
- no traceback or warning caused by typed wait changes;
- full pytest count is at least the pre-change suite plus all new tests;
- Bun includes all new wait-handshake cases;
- smoke prints `ALL PI SMOKE TESTS PASSED`.

- [ ] **Step 3: Inspect the diff for scope and managed-block safety**

Run:

```bash
git diff --check
git status --short
git diff --stat
git diff -- AGENTS.md | sed -n '/agent-dispatch:begin/,/agent-dispatch:end/p'
rg -n 'WAITING: 3\. catalog|confidence.?0\.95|affinity.?true' src/autocompactor src/pi tests
```

Expected:

- no whitespace errors;
- no unrelated files;
- no new text inside managed AGENTS markers;
- production code has no hard-coded incident string;
- no path remains that upgrades advisory open work to manufactured `.95/true`
  authority.

- [ ] **Step 4: Run adversarial review on the implementation diff**

Use the governed review engine against the non-empty base-to-HEAD diff. Treat
empty, timeout, degraded, or free-text-only review as failure. Resolve every
blocking finding, rerun targeted tests for each change, then rerun review until
it returns structured approval/no blocking findings.

- [ ] **Step 5: Commit documentation and any review fixes**

```bash
git add README.md HANDOFF.md AGENTS.md
git commit -m "docs(wait): document typed polling authority"
```

Do not use `--no-verify`.

- [ ] **Step 6: Re-run final verification after the last change**

Run again after documentation/review fixes:

```bash
python3 -m pytest tests/ -q
bun test tests/shim_prepare.test.ts tests/shim_wait_resume.test.ts
PI_SMOKE=1 bash tests/smoke_test_pi.sh
node --test 'src/pi/test/*.test.mjs'
git diff --check HEAD~1..HEAD
git status --short
```

Expected: all green and clean worktree.

- [ ] **Step 7: Install from source and verify the consumed surface**

Run:

```bash
python3 src/install_pi.py
python3 src/install_pi.py --status
cmp -s src/pi/autocompactor.ts ~/.pi/agent/extensions/autocompactor.ts || {
  echo "installed shim is rewritten by installer; verify normalized source hash/path pin instead"
  python3 src/install_pi.py --status
}
```

Because the installer may rewrite the baked bridge path, the authoritative
observable is `install_pi.py --status` reporting the installed source/version
pin and a subsequent new Pi session loading the extension without error. Record
this as **installed and status-verified**; do not claim the already-running
session hot-loaded the new behavior unless `/reload` or a fresh session is
actually exercised.

- [ ] **Step 8: Record durable completion evidence**

The final report must cite:

- commit SHAs;
- exact test commands and pass counts;
- adversarial review verdict;
- installed extension status;
- whether a fresh-session runtime exercise was completed.

If fresh-session exercise is not performed, state `installed, not live-session verified` rather than claiming deployment complete.

---

## Plan Self-Review Checklist

Before executing this plan, confirm:

- [ ] Every approved spec requirement maps to a task above.
- [ ] No task modifies an Opticboard/Rock 5C repository or Pi core.
- [ ] The exact incident replay is red before code and green after code.
- [ ] The positive typed-wait flow remains covered.
- [ ] User supersession happens at the Pi `input` seam and is also reconstructed after restart.
- [ ] Lock/CAS tests cover stale writers and preserve unrelated state.
- [ ] Every timer action independently validates authority.
- [ ] Legacy projections are inert even if cleanup fails.
- [ ] Telemetry tests reject content-bearing fields.
- [ ] Every task ends in an independently testable commit.
- [ ] Final verification is rerun after the last change and after review fixes.
