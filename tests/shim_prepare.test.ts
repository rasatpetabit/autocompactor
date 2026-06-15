// tests/shim_prepare.test.ts — regression test for the Pi shim's prepare
// call counts. Run with:  bun tests/shim_prepare.test.ts
//
// The double-prepare bug: in actuate mode, agent_end runs bridge("prepare",
// trigger=actuate) and passes its customInstructions into ctx.compact(); Pi
// then fires session_before_compact, which (before the fix) redundantly ran
// bridge("prepare", trigger=native) — its result discarded, but with the
// optional LLM digest on it doubled the digest + backup cost per
// self-triggered compaction. The fix: session_before_compact returns early
// when selfTriggered.
//
// This test exercises the live event flow against the real shim module with
// a mocked ExtensionAPI/ExtensionContext, so it catches regressions in the
// reentrancy/early-return logic that no Python test can reach.

import {
  describe,
  expect,
  test,
  beforeEach,
} from "bun:test"

// Cache-bust so each test gets a FRESH module instance with its own closure
// state (the shim's selfTriggered / lastRecTokens live at module scope).
const freshShim = () =>
  import(`../src/pi/autocompactor.ts?t=${Math.random().toString(36).slice(2)}`)

// A mock ExtensionContext + ExtensionAPI that record every bridge (pi.exec)
// call and let us drive the event lifecycle ourselves.
function makeHarness() {
  const execCalls: { cmd: string; args: string[] }[] = []
  const handlers: Record<string, (e: any, ctx: any) => any> = {}
  // Which bridge subcommands we're stubbing responses for. `prepare` records
  // a call but returns minimal valid output; `evaluate` returns actuate.
  const bridgeResponse: Record<string, any> = {
    evaluate: { recommend: true, mode: "actuate", reason: "test", context_tokens: 200000 },
    prepare: { customInstructions: "TEST-INSTR" },
    reinject: { text: "TEST-DIGEST", customType: "autocompactor.digest" },
  }
  const ctx: any = {
    cwd: "/tmp",
    hasUI: false,
    sessionManager: { getSessionFile: () => "/tmp/sess.jsonl" },
    // Simulate Pi: ctx.compact() fires session_before_compact (then the
    // session_compact / onComplete lifecycle) synchronously.
    compactCalls: 0,
    compact(opts?: any) {
      ctx.compactCalls++
      void handlers["session_before_compact"]?.({}, ctx)
      void handlers["session_compact"]?.({}, ctx)
      opts?.onComplete?.()
    },
    getContextUsage: () => ({ tokens: 200000, contextWindow: 300000 }),
    ui: { notify: () => {} },
  }
  const pi: any = {
    on(ev: string, h: any) { handlers[ev] = h },
    async exec(cmd: string, args: string[], _opts?: any) {
      execCalls.push({ cmd, args })
      const sub = args[1]
      const payload = bridgeResponse[sub]
      return { code: 0, stdout: payload ? JSON.stringify(payload) : "" }
    },
    sendMessage() {},
  }
  const prepareCalls = () => execCalls.filter((c) => c.args[1] === "prepare")
  return { pi, ctx, handlers, execCalls, prepareCalls }
}

describe("autocompactor Pi shim — prepare call counts", () => {
  let h: ReturnType<typeof makeHarness>
  beforeEach(() => { h = makeHarness() })

  test("actuate mode runs prepare exactly ONCE per self-triggered compaction", async () => {
    const mod = await freshShim()
    mod.default(h.pi)

    // Drive the boundary moment: high usage -> evaluate(recommend,actuate)
    // -> prepare(actuate) -> ctx.compact() -> session_before_compact.
    await h.handlers["agent_end"]({}, h.ctx)

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

