// tests/shim_wait_resume.test.ts — wait-shaped autonomous resume must NOT
// fire an immediate coding nextstep.task with triggerTurn; it should surface
// a wait advisory and schedule a poll. Run with:
//   bun tests/shim_wait_resume.test.ts

import { describe, expect, test } from "bun:test"

const freshShim = () =>
  import(`../src/pi/autocompactor.ts?t=${Math.random().toString(36).slice(2)}`)

type HarnessOptions = {
  bridgeResponse?: Record<string, any>
  idle?: boolean
}

function makeHarness(options: HarnessOptions = {}) {
  const sendMessages: { message: any; options: any }[] = []
  const handlers: Record<string, (e: any, ctx: any) => any> = {}
  const compactPromises: Promise<void>[] = []
  const bridgeResponse: Record<string, any> = {
    evaluate: { recommend: true, mode: "actuate", reason: "test", context_tokens: 200000 },
    prepare: { customInstructions: "TEST-INSTR" },
    reinject: {
      text: "TEST-DIGEST",
      customType: "autocompactor.digest",
      nextStep:
        "WAITING: Y260717-114448\nMonitor: yanos-builder show Y260717-114448\n" +
        "On success: fill rebuild-artifact.txt\n" +
        "Do not start unrelated work. Poll now; if still running, report status and stop.",
      nextStepSource: "open_work:waiting_monitor",
      nextStepWait: true,
      nextStepMode: "autonomous",
      nextStepWaitMode: "poll",
      waitPollS: 60,
      waitPollMax: 20,
    },
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
        await handlers["session_compact"]?.(
          { reason: "actuate", willRetry: false, compactionEntry: entry },
          ctx,
        )
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
    async exec(_cmd: string, args: string[]) {
      const sub = args[1]
      const payload = bridgeResponse[sub]
      return { code: 0, stdout: payload ? JSON.stringify(payload) : "" }
    },
    sendMessage(message: any, msgOptions?: any) {
      sendMessages.push({ message, options: msgOptions })
    },
    registerCommand() {},
  }
  const waitForCompactions = async () => {
    await Promise.all(compactPromises)
    // flushAutoResume uses setTimeout(0)
    await new Promise((r) => setTimeout(r, 5))
  }
  return { pi, ctx, handlers, sendMessages, waitForCompactions }
}

describe("wait-shaped autonomous resume", () => {
  test("does not immediate-trigger coding nextstep.task; surfaces wait + schedules poll", async () => {
    const timers: Array<{ ms: number; fn: () => void }> = []
    const realSetTimeout = globalThis.setTimeout
    // @ts-expect-error test stub
    globalThis.setTimeout = ((fn: any, ms?: number) => {
      // Keep the flushAutoResume setTimeout(0) path working by running tiny delays now.
      if (!ms || ms < 20) {
        return realSetTimeout(fn, ms ?? 0)
      }
      timers.push({ ms: ms ?? 0, fn })
      return 0 as any
    }) as any

    try {
      const mod = await freshShim()
      const h = makeHarness({ idle: true })
      mod.default(h.pi)

      // Drive actuate agent_end → compact → session_compact → flushAutoResume
      await h.handlers["agent_end"]?.({}, h.ctx)
      // Force compact path by calling compact directly if agent_end didn't (evaluate stub may not recommend depending on mode)
      // The evaluate returns recommend:true mode actuate; if agent_end path works it will compact.
      if (h.ctx.compactCalls === 0) {
        // Fallback: invoke session_compact as if actuate just finished.
        // Mark selfTriggered by calling prepare-path: instead just fire compact via a fake self-trigger.
        // Use session_compact after manually setting reinject by calling prepare through compact.
        await h.ctx.compact({})
      }
      await h.waitForCompactions()
      // Allow flushAutoResume setTimeout(0)
      await new Promise((r) => realSetTimeout(r, 20))

      const types = h.sendMessages.map((m) => m.message?.customType)
      // Must surface wait, must NOT fire coding task with triggerTurn
      expect(types).toContain("autocompactor.nextstep.wait")
      const coding = h.sendMessages.filter(
        (m) => m.message?.customType === "autocompactor.nextstep.task" && m.options?.triggerTurn,
      )
      expect(coding.length).toBe(0)
      // Poll scheduled at ~60s
      expect(timers.some((t) => t.ms >= 50_000 && t.ms <= 70_000)).toBe(true)

      // Fire the poll timer
      const pollTimer = timers.find((t) => t.ms >= 50_000)
      expect(pollTimer).toBeTruthy()
      pollTimer!.fn()
      await new Promise((r) => realSetTimeout(r, 5))

      const polls = h.sendMessages.filter(
        (m) => m.message?.customType === "autocompactor.nextstep.poll",
      )
      expect(polls.length).toBe(1)
      expect(polls[0].options?.triggerTurn).toBe(true)
      expect(String(polls[0].message?.content || "")).toContain("Y260717-114448")
    } finally {
      globalThis.setTimeout = realSetTimeout
    }
  })

  test("non-wait autonomous still triggerTurn-s nextstep.task", async () => {
    const mod = await freshShim()
    const h = makeHarness({
      idle: true,
      bridgeResponse: {
        reinject: {
          text: "TEST-DIGEST",
          customType: "autocompactor.digest",
          nextStep: "finish packaging the masks package",
          nextStepSource: "last_user_task",
          nextStepWait: false,
          nextStepMode: "autonomous",
          nextStepWaitMode: "poll",
          waitPollS: 60,
          waitPollMax: 20,
        },
      },
    })
    mod.default(h.pi)
    // Drive session_compact as actuate (selfTriggered path via agent_end)
    await h.handlers["agent_end"]?.({}, h.ctx)
    if (h.ctx.compactCalls === 0) await h.ctx.compact({})
    await h.waitForCompactions()
    await new Promise((r) => setTimeout(r, 20))

    const coding = h.sendMessages.filter(
      (m) => m.message?.customType === "autocompactor.nextstep.task" && m.options?.triggerTurn,
    )
    expect(coding.length).toBeGreaterThanOrEqual(1)
    const waits = h.sendMessages.filter(
      (m) => m.message?.customType === "autocompactor.nextstep.wait",
    )
    expect(waits.length).toBe(0)
  })

  test("idle actuate status omits deliverAs nextTurn", async () => {
    const mod = await freshShim()
    const h = makeHarness({ idle: true })
    mod.default(h.pi)
    await h.handlers["agent_end"]?.({}, h.ctx)
    if (h.ctx.compactCalls === 0) await h.ctx.compact({})
    await h.waitForCompactions()
    await new Promise((r) => setTimeout(r, 20))

    // Criteria-met / wait / status messages sent while idle should not force nextTurn.
    const statusMsgs = h.sendMessages.filter(
      (m) => m.message?.customType === "autocompactor.status" ||
             m.message?.customType === "autocompactor.nextstep.wait",
    )
    // At least one status-like message should have been sent without deliverAs nextTurn
    // (options undefined OR without deliverAs).
    const immediate = statusMsgs.filter(
      (m) => !m.options || m.options.deliverAs !== "nextTurn",
    )
    expect(immediate.length).toBeGreaterThan(0)
  })
})
