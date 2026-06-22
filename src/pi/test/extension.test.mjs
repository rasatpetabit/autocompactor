// src/pi/test/extension.test.mjs — node --test for the Pi shim (src/pi/autocompactor.ts).
//
// The shim is TypeScript; node 22 cannot import it directly, so a before()
// hook transpiles it with esbuild (type-only imports erased — output depends
// only on node builtins) and dynamic-imports the result from a temp dir.
//
// Stubs: a minimal `pi` (on/exec/sendMessage) and `ctx` (getContextUsage/
// sessionManager/cwd/compact plus UI status hooks). Each test wires a FRESH
// extension instance so closure state (cooldown, reentrancy flag) never leaks
// across tests.

import { execFileSync } from "node:child_process"
import * as fs from "node:fs"
import * as os from "node:os"
import * as path from "node:path"
import { pathToFileURL, fileURLToPath } from "node:url"
import { test, before, after } from "node:test"
import assert from "node:assert/strict"

const HERE = path.dirname(fileURLToPath(import.meta.url))
const SHIM_TS = path.join(HERE, "..", "autocompactor.ts")

let autocompactor // default export, loaded in before()
let tmpDir

before(async () => {
  tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "autocompactor-pi-test-"))
  const js = execFileSync(
    "npx", ["--yes", "esbuild", SHIM_TS, "--format=esm"],
    { encoding: "utf8", timeout: 120_000 },
  )
  const out = path.join(tmpDir, "autocompactor.mjs")
  fs.writeFileSync(out, js)
  autocompactor = (await import(pathToFileURL(out).href)).default
})

after(() => {
  try { fs.rmSync(tmpDir, { recursive: true, force: true }) } catch {}
})

// ------------------------------------------------------------------ stubs

function makePi({ exec } = {}) {
  const handlers = {}
  const sent = []
  const execCalls = []
  return {
    handlers,
    sent,
    execCalls,
    on(event, handler) { handlers[event] = handler },
    async exec(cmd, args, opts) {
      execCalls.push({ cmd, args, opts })
      if (exec) return exec(cmd, args, opts)
      return { stdout: "", stderr: "", code: 1, killed: false }
    },
    sendMessage(message, opts) { sent.push({ message, opts }) },
  }
}

function makeCtx({ tokens, contextWindow = 200_000 } = {}) {
  const compactCalls = []
  const notifications = []
  const statuses = []
  return {
    compactCalls,
    notifications,
    statuses,
    hasUI: true,
    ui: {
      notify(message, type) { notifications.push({ message, type }) },
      setStatus(key, text) { statuses.push({ key, text }) },
    },
    cwd: "/tmp",
    sessionManager: { getSessionFile: () => "/tmp/fake-session.jsonl" },
    getContextUsage: () =>
      tokens === undefined
        ? undefined
        : { tokens, contextWindow, percent: null },
    compact(opts) { compactCalls.push(opts ?? {}) },
  }
}

// Bridge responder: routes on the subcommand (args[1] after the bridge path).
function bridgeResponder(byCmd) {
  return (_cmd, args) => {
    const sub = args[1]
    const body = byCmd[sub]
    if (body === undefined) return { stdout: "", stderr: "", code: 1, killed: false }
    return { stdout: JSON.stringify(body), stderr: "", code: 0, killed: false }
  }
}

const RECOMMEND = {
  evaluate: { recommend: true, reason: "test boundary", context_tokens: 150_000 },
  prepare: { customInstructions: "PRESERVE THESE THINGS" },
  reinject: { text: "digest body", customType: "autocompactor.digest" },
}

function visibleStatuses(pi) {
  return pi.sent.filter((s) => s.message.customType === "autocompactor.status")
}

function hiddenDigests(pi) {
  return pi.sent.filter((s) => s.message.customType === "autocompactor.digest")
}

// EVERY visible notice — one-shot (load, compaction start/done, errors) AND
// the recurring agent_end advisory — rides "nextTurn": the only channel that
// renders+persists across a compaction (Pi's sendCustomMessage drops a
// "followUp" message while the agent is streaming, which is when both the
// compaction events AND agent_end fire). The advisory is deduped by text so
// nextTurn can't pile up stale dupes.
function assertVisibleStatus(sent, pattern, deliver = "nextTurn") {
  assert.equal(sent.message.display, true)
  assert.deepEqual(sent.opts, { deliverAs: deliver })
  assert.match(sent.message.content, pattern)
  assert.equal(typeof sent.message.details?.timestamp, "number")
}

// Safety net: every visible status must use "nextTurn" with display:true —
// never "followUp"/"steer"/"triggerTurn"/undefined. followUp is swallowed
// while streaming; steer/triggerTurn would inject status text into the live
// model stream.
function assertVisibleChannelsValid(pi) {
  for (const sent of visibleStatuses(pi)) {
    assert.equal(
      sent.opts?.deliverAs, "nextTurn",
      `visible status used unexpected channel: ${sent.opts?.deliverAs}`,
    )
    assert.equal(sent.message.display, true)
  }
}

// ------------------------------------------------------------------ tests
// Module-level thresholds are the defaults (no AUTOCOMPACTOR_* set when the
// module loads): SOFT_PCT 0.40, MIN_SAVINGS 30k, POST_FLOOR 70k,
// COOLDOWN 25k, RESERVE 40k. Effective window below = 200k - 40k = 160k.

test("session_start: extension announces it loaded visibly", async () => {
  const pi = makePi()
  autocompactor(pi)
  const ctx = makeCtx({ tokens: 50_000 })
  await pi.handlers.session_start({ reason: "startup" }, ctx)
  const statuses = visibleStatuses(pi)
  assert.equal(statuses.length, 1)
  assertVisibleStatus(statuses[0], /autocompactor: loaded/)
  assertVisibleChannelsValid(pi)
  assert.equal(ctx.statuses.at(-1).key, "autocompactor")
})

test("pre-gate: below SOFT_PCT never spawns the bridge", async () => {
  const pi = makePi()
  autocompactor(pi)
  const ctx = makeCtx({ tokens: 50_000 }) // 31% of 160k, and reclaim < MIN_SAVINGS
  await pi.handlers.agent_end({}, ctx)
  assert.equal(pi.execCalls.length, 0, "bridge must not be spawned below the gate")
  assert.equal(pi.sent.length, 0)
  assert.equal(ctx.compactCalls.length, 0)
  assert.match(ctx.statuses.at(-1).text, /below .*gate/)
})

test("pre-gate: null tokens (right after compaction) never spawns", async () => {
  const pi = makePi()
  autocompactor(pi)
  const ctx = makeCtx({ tokens: null })
  await pi.handlers.agent_end({}, ctx)
  assert.equal(pi.execCalls.length, 0)
})

test("pre-gate: reclaim below MIN_SAVINGS never spawns", async () => {
  const pi = makePi()
  autocompactor(pi)
  // 90k of 160k = 56% (over SOFT) but 90k - 70k floor = 20k < 30k savings.
  const ctx = makeCtx({ tokens: 90_000 })
  await pi.handlers.agent_end({}, ctx)
  assert.equal(pi.execCalls.length, 0)
})

test("advise mode: recommend -> visible persistent status", async () => {
  delete process.env.AUTOCOMPACTOR_PI_MODE
  const pi = makePi({ exec: bridgeResponder(RECOMMEND) })
  autocompactor(pi)
  const ctx = makeCtx({ tokens: 150_000 })
  await pi.handlers.agent_end({}, ctx)
  assert.equal(pi.execCalls.length, 1, "exactly one bridge call (evaluate)")
  assert.equal(pi.execCalls[0].args[1], "evaluate")
  assert.equal(ctx.compactCalls.length, 0, "advise mode never compacts")
  const statuses = visibleStatuses(pi)
  assert.equal(statuses.length, 1)
  assertVisibleStatus(statuses[0], /criteria met.*test boundary.*advise mode/)
  assertVisibleChannelsValid(pi)
  assert.equal(ctx.notifications.length, 1)
  assert.equal(ctx.notifications[0].type, "warning")
  assert.match(ctx.notifications[0].message, /criteria met/)
  assert.match(ctx.notifications[0].message, /test boundary/)
  assert.match(ctx.notifications[0].message, /advise mode/)
  assert.doesNotMatch(ctx.notifications[0].message, /running compaction/)
  assert.doesNotMatch(ctx.notifications[0].message, /Run \/compact/)
})

test("actuate mode: compact exactly once; reentrancy blocks a concurrent second", async () => {
  process.env.AUTOCOMPACTOR_PI_MODE = "actuate"
  try {
    const pi = makePi({ exec: bridgeResponder(RECOMMEND) })
    autocompactor(pi)

    const ctx1 = makeCtx({ tokens: 150_000 })
    await pi.handlers.agent_end({}, ctx1)
    assert.equal(ctx1.compactCalls.length, 1, "first boundary actuates")
    assert.equal(ctx1.compactCalls[0].customInstructions, "PRESERVE THESE THINGS")
    assert.equal(typeof ctx1.compactCalls[0].onComplete, "function")
    // Actuate mode notifies before compacting — "running compaction now"
    assert.equal(ctx1.notifications.length, 1)
    assert.equal(ctx1.notifications[0].type, "info")
    assert.match(ctx1.notifications[0].message, /criteria met/)
    assert.match(ctx1.notifications[0].message, /running compaction now/)
    assert.doesNotMatch(ctx1.notifications[0].message, /advise mode/)
    assert.doesNotMatch(ctx1.notifications[0].message, /Run \/compact/)

    let statuses = visibleStatuses(pi)
    assert.equal(statuses.length, 1)
    assertVisibleStatus(statuses[0], /criteria met.*running compaction now/)
    assertVisibleChannelsValid(pi)

    // Compaction still in flight (onComplete NOT called): a second boundary
    // past the cooldown must NOT compact again — it shows "compaction in progress".
    const ctx2 = makeCtx({ tokens: 176_000 }) // 150k + COOLDOWN(25k) + margin
    await pi.handlers.agent_end({}, ctx2)
    assert.equal(ctx2.compactCalls.length, 0, "reentrancy flag blocks concurrent compact")
    statuses = visibleStatuses(pi)
    assert.equal(statuses.length, 2)
    assertVisibleStatus(statuses[1], /criteria met.*test boundary.*compaction in progress/)
    assertVisibleChannelsValid(pi)
    assert.equal(ctx2.notifications.length, 1)
    assert.match(ctx2.notifications[0].message, /criteria met/)
    assert.match(ctx2.notifications[0].message, /test boundary/)
    assert.match(ctx2.notifications[0].message, /compaction in progress/)
    assert.doesNotMatch(ctx2.notifications[0].message, /running compaction/)

    // onComplete resets the flag: a later boundary may actuate again.
    ctx1.compactCalls[0].onComplete()
    const ctx3 = makeCtx({ tokens: 202_000 }) // past cooldown from 176k
    await pi.handlers.agent_end({}, ctx3)
    assert.equal(ctx3.compactCalls.length, 1, "flag reset allows the next actuate")
    statuses = visibleStatuses(pi)
    assert.equal(statuses.length, 3)
    assertVisibleStatus(statuses[2], /criteria met.*running compaction now/)
    assertVisibleChannelsValid(pi)
  } finally {
    delete process.env.AUTOCOMPACTOR_PI_MODE
  }
})

test("error-swallow: bridge that throws never breaks any handler and warns visibly", async () => {
  const pi = makePi()
  pi.exec = async () => { throw new Error("bridge exploded") }
  autocompactor(pi)
  const ctx = makeCtx({ tokens: 150_000 })
  await pi.handlers.agent_end({}, ctx) // must not reject
  await pi.handlers.session_before_compact({}, ctx)
  await pi.handlers.session_compact({}, ctx)
  assert.equal(ctx.compactCalls.length, 0)
  const statuses = visibleStatuses(pi)
  assert.equal(statuses.length, 3)
  assertVisibleStatus(statuses[0], /bridge evaluate returned no data/)
  assertVisibleStatus(statuses[1], /native compaction starting/)
  assertVisibleStatus(statuses[2], /compaction completed/)
  assertVisibleChannelsValid(pi)
})

test("error-swallow: garbage bridge stdout warns but never compacts", async () => {
  const pi = makePi({
    exec: () => ({ stdout: "not json at all {", stderr: "", code: 0, killed: false }),
  })
  autocompactor(pi)
  const ctx = makeCtx({ tokens: 150_000 })
  await pi.handlers.agent_end({}, ctx)
  await pi.handlers.session_compact({}, ctx)
  assert.equal(ctx.compactCalls.length, 0)
  const statuses = visibleStatuses(pi)
  assert.equal(statuses.length, 2)
  assertVisibleStatus(statuses[0], /bridge evaluate returned no data/)
  assertVisibleStatus(statuses[1], /compaction completed/)
  assertVisibleChannelsValid(pi)
})

test("session_compact: hidden digest is queued; visible summary is persistent", async () => {
  const pi = makePi({ exec: bridgeResponder(RECOMMEND) })
  autocompactor(pi)
  const ctx = makeCtx({ tokens: 10_000 })
  await pi.handlers.session_compact({ compactionEntry: { tokensBefore: 150_000 } }, ctx)
  assert.equal(pi.execCalls.length, 1)
  assert.equal(pi.execCalls[0].args[1], "reinject")
  const digests = hiddenDigests(pi)
  assert.equal(digests.length, 1)
  assert.equal(digests[0].message.content, "digest body")
  assert.equal(digests[0].message.display, false)
  assert.deepEqual(digests[0].opts, { deliverAs: "nextTurn" })
  const statuses = visibleStatuses(pi)
  assert.equal(statuses.length, 1)
  assertVisibleStatus(statuses[0], /150,000 → 10,000 tokens/)
  assertVisibleChannelsValid(pi)
  assert.equal(ctx.notifications.length, 1)
  assert.equal(ctx.notifications[0].type, "info")
  assert.match(ctx.notifications[0].message, /150,000 → 10,000 tokens/)
})

test("session_compact: visible summary still persists when there is no digest", async () => {
  const pi = makePi({ exec: bridgeResponder({ reinject: {} }) })
  autocompactor(pi)
  const ctx = makeCtx({ tokens: 0 })
  await pi.handlers.session_compact({ compactionEntry: { tokensBefore: 150_000 } }, ctx)
  assert.equal(hiddenDigests(pi).length, 0)
  const statuses = visibleStatuses(pi)
  assert.equal(statuses.length, 1)
  assertVisibleStatus(statuses[0], /before context was 150,000 tokens/)
  assertVisibleChannelsValid(pi)
  assert.equal(ctx.notifications.length, 1)
  assert.match(ctx.notifications[0].message, /before context was 150,000 tokens/)
})

test("session_before_compact: native path awaits prepare (skip-llm), no cancel by default", async () => {
  delete process.env.AUTOCOMPACTOR_PI_INTERCEPT
  // prepare resolves only after a turn of the event loop; if the handler did
  // NOT await it, prepareResolved would still be false when the handler returns
  // (the race that let session_compact's reinject read stale artifacts).
  let prepareResolved = false
  const pi = makePi({
    exec: async (_cmd, args) => {
      if (args[1] === "prepare") {
        await new Promise((r) => setTimeout(r, 5))
        prepareResolved = true
        return { stdout: "{}", stderr: "", code: 0, killed: false }
      }
      return { stdout: "", stderr: "", code: 1, killed: false }
    },
  })
  autocompactor(pi)
  const ctx = makeCtx({ tokens: 150_000 })
  const result = await pi.handlers.session_before_compact({}, ctx)
  assert.equal(result, undefined, "default path never cancels native compaction")
  assert.equal(ctx.compactCalls.length, 0)
  assert.equal(prepareResolved, true, "handler must AWAIT prepare before yielding to native compaction")
  const prep = pi.execCalls.find((c) => c.args[1] === "prepare")
  assert.ok(prep, "prepare fired for backup+artifacts")
  assert.ok(prep.args.includes("--trigger") && prep.args.includes("native"), "native trigger")
  assert.ok(prep.args.includes("--skip-llm"), "native prepare skips the (discarded) LLM digest")
  assertVisibleStatus(visibleStatuses(pi)[0], /native compaction starting.*backup\/artifacts/)
  assertVisibleChannelsValid(pi)
})

test("actuate: ctx.compact throwing synchronously clears selfTriggered (no permanent brick)", async () => {
  process.env.AUTOCOMPACTOR_PI_MODE = "actuate"
  try {
    const pi = makePi({ exec: bridgeResponder(RECOMMEND) })
    autocompactor(pi)
    const ctx = makeCtx({ tokens: 150_000 })
    ctx.compact = () => { throw new Error("compact boom") } // throws, NO callback fires
    await pi.handlers.agent_end({}, ctx) // must not reject
    // The synchronous throw is reported AND the reentrancy flag is reset:
    assert.ok(
      visibleStatuses(pi).some((s) => /compaction failed.*compact boom/.test(s.message.content)),
      "compact failure is surfaced",
    )
    // Proof the flag cleared: a later native compaction is NOT short-circuited
    // by the "already in progress" guard — it proceeds to prepare.
    const ctx2 = makeCtx({ tokens: 150_000 })
    await pi.handlers.session_before_compact({}, ctx2)
    assert.ok(
      visibleStatuses(pi).some((s) => /native compaction starting/.test(s.message.content)),
      "selfTriggered was cleared — native prepare not blocked by a stuck reentrancy flag",
    )
  } finally {
    delete process.env.AUTOCOMPACTOR_PI_MODE
  }
})
