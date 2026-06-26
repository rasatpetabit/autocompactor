// tests/shim_prepare.test.ts — regression tests for the Pi shim's prepare
// call counts and post-compaction autonomous continuation behavior. Run with:
//   bun tests/shim_prepare.test.ts
//
// The double-prepare bug: in actuate mode, agent_end runs bridge("prepare",
// trigger=actuate) and passes its customInstructions into ctx.compact(); Pi
// then fires session_before_compact, which (before the fix) redundantly ran
// bridge("prepare", trigger=native) — its result discarded, but with the
// optional LLM digest on it doubled the digest + backup cost per
// self-triggered compaction. The fix: session_before_compact returns early
// when selfTriggered.
//
// The autonomous next-step path must also avoid firing inside
// session_compact. It stages the recovered step there, then triggers exactly
// one model-visible custom message after ctx.compact() completes.

import {
  describe,
  expect,
  test,
  beforeEach,
} from "bun:test"

// Cache-bust so each test gets a FRESH module instance with its own closure
// state (the shim's selfTriggered / pending auto-resume state live there).
const freshShim = () =>
  import(`../src/pi/autocompactor.ts?t=${Math.random().toString(36).slice(2)}`)

type HarnessOptions = {
  bridgeResponse?: Record<string, any>
  compactError?: string
  idle?: boolean
}

// A mock ExtensionContext + ExtensionAPI that record every bridge (pi.exec)
// call and let us drive the event lifecycle ourselves.
function makeHarness(options: HarnessOptions = {}) {
  const execCalls: { cmd: string; args: string[] }[] = []
  const sendMessages: { message: any; options: any }[] = []
  const handlers: Record<string, (e: any, ctx: any) => any> = {}
  const compactPromises: Promise<void>[] = []
  const triggerCountsBeforeComplete: number[] = []
  // Which bridge subcommands we're stubbing responses for. `prepare` records
  // a call but returns minimal valid output; `evaluate` returns actuate.
  const bridgeResponse: Record<string, any> = {
    evaluate: { recommend: true, mode: "actuate", reason: "test", context_tokens: 200000 },
    prepare: { customInstructions: "TEST-INSTR" },
    reinject: { text: "TEST-DIGEST", customType: "autocompactor.digest" },
    ...(options.bridgeResponse ?? {}),
  }
  const ctx: any = {
    cwd: "/tmp",
    hasUI: false,
    sessionManager: { getSessionFile: () => "/tmp/sess.jsonl" },
    compactCalls: 0,
    compact(opts?: any) {
      ctx.compactCalls++
      const entry = { id: `compact-${ctx.compactCalls}`, timestamp: ctx.compactCalls }
      const p = (async () => {
        await handlers["session_before_compact"]?.(
          { reason: "manual", willRetry: false, compactionEntry: entry },
          ctx,
        )
        if (options.compactError) {
          opts?.onError?.(new Error(options.compactError))
          return
        }
        await handlers["session_compact"]?.(
          { reason: "manual", willRetry: false, compactionEntry: entry },
          ctx,
        )
        triggerCountsBeforeComplete.push(sendMessages.filter((m) => m.options?.triggerTurn).length)
        opts?.onComplete?.({})
      })()
      compactPromises.push(p)
      return p
    },
    getContextUsage: () => ({ tokens: 200000, contextWindow: 300000 }),
    isIdle: () => options.idle ?? true,
    ui: { notify: () => {}, setStatus: () => {} },
  }
  const pi: any = {
    on(ev: string, h: any) { handlers[ev] = h },
    async exec(cmd: string, args: string[], _opts?: any) {
      execCalls.push({ cmd, args })
      const sub = args[1]
      const payload = bridgeResponse[sub]
      return { code: 0, stdout: payload ? JSON.stringify(payload) : "" }
    },
    sendMessage(message: any, msgOptions?: any) {
      sendMessages.push({ message, options: msgOptions })
    },
    registerCommand(_name: string, _def: any) {},
  }
  const prepareCalls = () => execCalls.filter((c) => c.args[1] === "prepare")
  const waitForCompactions = async () => {
    await Promise.all(compactPromises)
    await Promise.resolve()
  }
  return { pi, ctx, handlers, execCalls, prepareCalls, sendMessages, triggerCountsBeforeComplete, waitForCompactions }
}

describe("autocompactor Pi shim — prepare call counts", () => {
  let h: ReturnType<typeof makeHarness>
  beforeEach(() => {
    delete process.env.AUTOCOMPACTOR_NEXTSTEP
    h = makeHarness()
  })

  test("actuate mode runs prepare exactly ONCE per self-triggered compaction", async () => {
    const mod = await freshShim()
    mod.default(h.pi)

    // Drive the boundary moment: high usage -> evaluate(recommend,actuate)
    // -> prepare(actuate) -> ctx.compact() -> session_before_compact.
    await h.handlers["agent_end"]({}, h.ctx)
    await h.waitForCompactions()

    const prep = h.prepareCalls()
    expect(prep.length).toBe(1)
    expect(prep[0].args).toContain("actuate")
    expect(prep[0].args).not.toContain("native")
  })

  test("NATIVE compaction (not self-triggered) still runs prepare(native)", async () => {
    // selfTriggered starts false in a fresh module, so a session_before_compact
    // fired by Pi's own native auto-compact must still do its prepare work.
    const mod = await freshShim()
    mod.default(h.pi)

    await h.handlers["session_before_compact"]({}, h.ctx)

    const prep = h.prepareCalls()
    expect(prep.length).toBe(1)
    expect(prep[0].args).toContain("native")
  })
})

describe("autocompactor Pi shim — autonomous next-step resume", () => {
  beforeEach(() => {
    delete process.env.AUTOCOMPACTOR_NEXTSTEP
  })

  test("autonomous mode triggers exactly one post-compaction continuation", async () => {
    const h = makeHarness({
      bridgeResponse: {
        reinject: {
          text: "TEST-DIGEST",
          customType: "autocompactor.digest",
          nextStep: "Run the next verification command",
          nextStepSource: "todo:pending[0]",
          nextStepMode: "autonomous",
        },
      },
    })
    const mod = await freshShim()
    mod.default(h.pi)

    await h.handlers["agent_end"]({}, h.ctx)
    await h.waitForCompactions()

    expect(h.triggerCountsBeforeComplete).toEqual([0])
    const triggered = h.sendMessages.filter((m) => m.options?.triggerTurn)
    expect(triggered.length).toBe(1)
    expect(triggered[0].message.customType).toBe("autocompactor.nextstep.task")
    expect(triggered[0].message.content).toContain("Run the next verification command")
    expect(triggered[0].message.content).not.toContain("dispatch_task")
    expect(h.sendMessages.some((m) => m.message.customType === "autocompactor.digest" && m.message.display === false)).toBe(true)
  })

  test("autonomous is the default when bridge omits nextStepMode", async () => {
    const h = makeHarness({
      bridgeResponse: {
        reinject: {
          text: "TEST-DIGEST",
          customType: "autocompactor.digest",
          nextStep: "Continue from default mode",
          nextStepSource: "last_user_task",
        },
      },
    })
    const mod = await freshShim()
    mod.default(h.pi)

    await h.handlers["agent_end"]({}, h.ctx)
    await h.waitForCompactions()

    const triggered = h.sendMessages.filter((m) => m.options?.triggerTurn)
    expect(triggered.length).toBe(1)
    expect(triggered[0].message.content).toContain("Continue from default mode")
  })

  test("advisory mode surfaces next step without triggering a turn", async () => {
    const h = makeHarness({
      bridgeResponse: {
        reinject: {
          text: "TEST-DIGEST",
          customType: "autocompactor.digest",
          nextStep: "Review manually",
          nextStepSource: "last_user_task",
          nextStepMode: "advisory",
        },
      },
    })
    const mod = await freshShim()
    mod.default(h.pi)

    await h.handlers["agent_end"]({}, h.ctx)
    await h.waitForCompactions()

    expect(h.sendMessages.some((m) => m.options?.triggerTurn)).toBe(false)
    expect(h.sendMessages.some((m) => m.message.customType === "autocompactor.nextstep.advisory")).toBe(true)
  })

  test("empty next step does not trigger a continuation", async () => {
    const h = makeHarness({
      bridgeResponse: {
        reinject: {
          text: "TEST-DIGEST",
          customType: "autocompactor.digest",
          nextStep: "",
          nextStepMode: "autonomous",
        },
      },
    })
    const mod = await freshShim()
    mod.default(h.pi)

    await h.handlers["agent_end"]({}, h.ctx)
    await h.waitForCompactions()

    expect(h.sendMessages.some((m) => m.options?.triggerTurn)).toBe(false)
    expect(h.sendMessages.some((m) => m.message.customType === "autocompactor.nextstep.task")).toBe(false)
  })

  test("compaction error does not trigger autonomous continuation", async () => {
    const h = makeHarness({
      compactError: "boom",
      bridgeResponse: {
        reinject: {
          nextStep: "Should not run",
          nextStepSource: "last_user_task",
          nextStepMode: "autonomous",
        },
      },
    })
    const mod = await freshShim()
    mod.default(h.pi)

    await h.handlers["agent_end"]({}, h.ctx)
    await h.waitForCompactions()

    expect(h.sendMessages.some((m) => m.options?.triggerTurn)).toBe(false)
    expect(h.sendMessages.some((m) => m.message.customType === "autocompactor.nextstep.task")).toBe(false)
  })
})
