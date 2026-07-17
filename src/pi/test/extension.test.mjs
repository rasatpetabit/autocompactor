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
    registerCommand(_name, _def) {},
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

const CONTEXT_STATE = "Context: 150,000 tokens\nComposition:\n  • tool output: ~80k (53% of context; 95% stale; ~76k likely reclaimable)"
const COMPACTION_STATS = "compaction #1 (auto) | 150k before | pre-compaction composition: ≈ 80k tool (95% stale)\n  └ preserved verbatim → disk (survive the summary): 2 files"

const RECOMMEND = {
  evaluate: { recommend: true, mode: "advise", reason: "test boundary", context_tokens: 150_000, contextState: CONTEXT_STATE },
  prepare: { customInstructions: "PRESERVE THESE THINGS", contextState: CONTEXT_STATE },
  reinject: { text: "digest body", customType: "autocompactor.digest", compactionStats: COMPACTION_STATS },
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
  assert.match(ctx.statuses.at(-1).text, /below (.*gate|30,000 minimum)/)
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

test("pre-gate: detail threshold shows composition without compacting", async () => {
  const pi = makePi({ exec: bridgeResponder(RECOMMEND) })
  autocompactor(pi)
  const ctx = makeCtx({ tokens: 120_000, contextWindow: 976_000 })
  await pi.handlers.agent_end({}, ctx)
  assert.equal(pi.execCalls.length, 1, "bridge evaluate gathers composition before soft gate")
  assert.equal(pi.execCalls[0].args[1], "evaluate")
  assert.equal(ctx.compactCalls.length, 0)
  assert.equal(visibleStatuses(pi).length, 0, "early monitoring is UI-only, not persisted into context")
  assert.equal(ctx.notifications.length, 1)
  assert.match(ctx.notifications[0].message, /monitoring/)
  assert.match(ctx.notifications[0].message, /context composition/)
  assert.match(ctx.notifications[0].message, /tool output/)
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
  assert.match(statuses[0].message.content, /context composition:[\s\S]*tool output/)
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
    assert.match(statuses[0].message.content, /context composition:[\s\S]*tool output/)
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
  assert.match(statuses[0].message.content, /pre-compaction accounting:[\s\S]*pre-compaction composition/)
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

// ---------------------------------------------------------- PI_INTERCEPT
// Config-backed intercept (config.json PI_INTERCEPT) with env-override
// semantics: the env var, when SET, wins in both directions. CFG is read at
// module load, so these tests import a FRESH copy of the transpiled shim
// with AUTOCOMPACTOR_BRIDGE pointed at a temp repo containing the config.
// interceptEnabled() also reads ~/.pi/agent/settings.json at call time, so
// HOME is stubbed to a hermetic dir (no pi-custom-compactor).

let shimSeq = 0

async function loadShimWithConfig(cfgObj) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "ac-pi-intercept-"))
  fs.mkdirSync(path.join(dir, "src"))
  if (cfgObj !== undefined) {
    fs.writeFileSync(path.join(dir, "config.json"), JSON.stringify(cfgObj))
  }
  const prevBridge = process.env.AUTOCOMPACTOR_BRIDGE
  process.env.AUTOCOMPACTOR_BRIDGE = path.join(dir, "src", "pi_bridge.py")
  try {
    const out = path.join(dir, `autocompactor-${shimSeq++}.mjs`)
    fs.copyFileSync(path.join(tmpDir, "autocompactor.mjs"), out)
    const mod = (await import(pathToFileURL(out).href)).default
    return { mod, dir }
  } finally {
    if (prevBridge === undefined) delete process.env.AUTOCOMPACTOR_BRIDGE
    else process.env.AUTOCOMPACTOR_BRIDGE = prevBridge
  }
}

function withHermeticHome(fn) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "ac-pi-home-"))
  fs.mkdirSync(path.join(home, ".pi", "agent"), { recursive: true })
  fs.writeFileSync(
    path.join(home, ".pi", "agent", "settings.json"),
    JSON.stringify({ packages: [] }),
  )
  const prev = process.env.HOME
  return Promise.resolve()
    .then(() => { process.env.HOME = home })
    .then(fn)
    .finally(() => {
      if (prev === undefined) delete process.env.HOME
      else process.env.HOME = prev
      try { fs.rmSync(home, { recursive: true, force: true }) } catch {}
    })
}

async function waitForDeferredCompact(ctx, n = 1, ms = 50) {
  const start = Date.now()
  while (ctx.compactCalls.length < n && Date.now() - start < ms) {
    await new Promise((r) => setTimeout(r, 0))
  }
  assert.equal(ctx.compactCalls.length, n, `expected ${n} deferred compact call(s)`)
}

test("intercept via config.json PI_INTERCEPT: cancels native and re-triggers enriched", async () => {
  delete process.env.AUTOCOMPACTOR_PI_INTERCEPT
  const { mod } = await loadShimWithConfig({ PI_INTERCEPT: true })
  await withHermeticHome(async () => {
    const pi = makePi({ exec: bridgeResponder(RECOMMEND) })
    mod(pi)
    const ctx = makeCtx({ tokens: 150_000 })
    const result = await pi.handlers.session_before_compact({}, ctx)
    assert.deepEqual(result, { cancel: true }, "native compaction is cancelled")
    // Re-trigger is setTimeout(0)-deferred so Pi can unwind native auto-compact first.
    assert.equal(ctx.compactCalls.length, 0, "re-trigger not yet scheduled inline")
    await waitForDeferredCompact(ctx, 1)
    assert.equal(ctx.compactCalls[0].customInstructions, "PRESERVE THESE THINGS")
    const prep = pi.execCalls.find((c) => c.args[1] === "prepare")
    assert.ok(prep.args.includes("--trigger") && prep.args.includes("native"))
    assert.ok(!prep.args.includes("--skip-llm"), "intercept prepare keeps the LLM digest")
  })
})

test("stale-mixed state: env AUTOCOMPACTOR_PI_INTERCEPT=0 wins over config PI_INTERCEPT=true", async () => {
  process.env.AUTOCOMPACTOR_PI_INTERCEPT = "0"
  try {
    const { mod } = await loadShimWithConfig({ PI_INTERCEPT: true })
    await withHermeticHome(async () => {
      const pi = makePi({ exec: bridgeResponder(RECOMMEND) })
      mod(pi)
      const ctx = makeCtx({ tokens: 150_000 })
      const result = await pi.handlers.session_before_compact({}, ctx)
      assert.equal(result, undefined, "env=0 disables intercept despite config=true")
      assert.equal(ctx.compactCalls.length, 0)
      const prep = pi.execCalls.find((c) => c.args[1] === "prepare")
      assert.ok(prep.args.includes("--skip-llm"), "non-intercept native prepare")
    })
  } finally {
    delete process.env.AUTOCOMPACTOR_PI_INTERCEPT
  }
})

test("env AUTOCOMPACTOR_PI_INTERCEPT=1 enables intercept with no config key", async () => {
  process.env.AUTOCOMPACTOR_PI_INTERCEPT = "1"
  try {
    const { mod } = await loadShimWithConfig({})
    await withHermeticHome(async () => {
      const pi = makePi({ exec: bridgeResponder(RECOMMEND) })
      mod(pi)
      const ctx = makeCtx({ tokens: 150_000 })
      const result = await pi.handlers.session_before_compact({}, ctx)
      assert.deepEqual(result, { cancel: true })
      await waitForDeferredCompact(ctx, 1)
    })
  } finally {
    delete process.env.AUTOCOMPACTOR_PI_INTERCEPT
  }
})

test("concurrent native while enriched in flight: cancel native, no second re-trigger", async () => {
  // Regression: actuate starts enriched compact, then native auto-compact fires.
  // Old ownsCompaction() treated ANY event as owned and let native proceed →
  // nested compact() raced _compactionAbortController → reading 'signal'.
  process.env.AUTOCOMPACTOR_PI_MODE = "actuate"
  delete process.env.AUTOCOMPACTOR_PI_INTERCEPT
  try {
    const { mod } = await loadShimWithConfig({ PI_INTERCEPT: true })
    await withHermeticHome(async () => {
      const pi = makePi({ exec: bridgeResponder(RECOMMEND) })
      mod(pi)
      const ctxActuate = makeCtx({ tokens: 150_000 })
      await pi.handlers.agent_end({}, ctxActuate)
      assert.equal(ctxActuate.compactCalls.length, 1, "actuate armed one enriched compact")

      // Concurrent native (threshold, no customInstructions) while enriched in flight.
      const ctxNative = makeCtx({ tokens: 150_000 })
      const result = await pi.handlers.session_before_compact(
        { reason: "threshold", customInstructions: undefined },
        ctxNative,
      )
      assert.deepEqual(result, { cancel: true }, "concurrent native is cancelled")
      assert.equal(ctxNative.compactCalls.length, 0, "no second re-trigger")

      // Our enriched event (manual + instructions) must still be allowed through.
      const ctxOurs = makeCtx({ tokens: 150_000 })
      const ours = await pi.handlers.session_before_compact(
        { reason: "manual", customInstructions: "PRESERVE THESE THINGS" },
        ctxOurs,
      )
      assert.equal(ours, undefined, "our enriched compact is not cancelled")
      assert.equal(ctxOurs.compactCalls.length, 0, "no extra re-trigger for our event")
    })
  } finally {
    delete process.env.AUTOCOMPACTOR_PI_MODE
  }
})

test("intercept fail-open: bridge prepare failure lets native compaction proceed", async () => {
  delete process.env.AUTOCOMPACTOR_PI_INTERCEPT
  const { mod } = await loadShimWithConfig({ PI_INTERCEPT: true })
  await withHermeticHome(async () => {
    // Bridge is down: every exec fails (code 1, no stdout).
    const pi = makePi()
    mod(pi)
    const ctx = makeCtx({ tokens: 150_000 })
    const result = await pi.handlers.session_before_compact({}, ctx)
    assert.equal(result, undefined, "no cancel — native compaction must proceed")
    assert.equal(ctx.compactCalls.length, 0, "no re-trigger without instructions")
    assert.ok(
      visibleStatuses(pi).some((s) => /no custom instructions.*allowing native/i.test(s.message.content)),
      "fail-open is surfaced to the user",
    )
  })
})
