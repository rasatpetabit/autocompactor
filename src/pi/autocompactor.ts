// autocompactor Pi extension — boundary-aware compaction advisor.
//
// Logic-minimal shim: ALL analysis lives in pi_bridge.py (one brain with the
// Claude Code hooks). This file only wires Pi events to the bridge and never
// lets a failure reach Pi — every handler is fully try/caught.
//
// Modes: "advise" (notify only) | "actuate" (self-trigger ctx.compact with
// bridge-built customInstructions). The mode comes from the bridge's
// evaluate verdict (config.json MODE, pi section overrides top-level) so it
// reaches Pi regardless of launch environment; the AUTOCOMPACTOR_PI_MODE
// env var, when set, overrides the verdict.
// Native-auto interception (cancel-and-retrigger in session_before_compact)
// is gated by config.json PI_INTERCEPT (durable path) with the
// AUTOCOMPACTOR_PI_INTERCEPT env var, when set, overriding in both
// directions ("1" on, anything else off). Default OFF, and skipped when
// @davidorex/pi-custom-compactor is configured (coexist passively).

import * as fs from "node:fs"
import * as os from "node:os"
import * as path from "node:path"
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent"

// install_pi.py rewrites this placeholder to the absolute checkout path.
const BRIDGE =
  process.env.AUTOCOMPACTOR_BRIDGE ?? "__AUTOCOMPACTOR_BRIDGE_PATH__"
const EXEC_TIMEOUT_MS = 5_000
// prepare runs backup + artifact extraction + (optionally) an LLM digest
// with a 45s budget — it gets a long leash; 5s would kill the digest.
const PREPARE_TIMEOUT_MS = 60_000

// Repo config (config.json + config.local.json overlay) read once at load.
// Lets the pre-gate share the bridge's tuning even in env-less processes;
// env vars still override. BRIDGE points into src/, so the config lives at
// the repo root (dirname of BRIDGE dir) — matching the Python bridge's
// config_lib._load_config() which reads _REPO_ROOT/config.json.
const CONFIG_DIR = path.dirname(path.dirname(BRIDGE))
const CFG: any = (() => {
  let merged: any = {}
  for (const name of ["config.json", "config.local.json"]) {
    try {
      const data = JSON.parse(
        fs.readFileSync(path.join(CONFIG_DIR, name), "utf8"),
      )
      merged = { ...merged, ...data }
    } catch {
      /* missing/unreadable config -> env + code defaults */
    }
  }
  return merged
})()

function cfgNum(key: string, dflt: number): number {
  // Single-namespace config post-pivot: the `pi` config section is gone, so
  // read the flat key directly (the old `CFG?.pi?.[key]` lookup was dead).
  const v = parseFloat(String(CFG?.[key] ?? ""))
  return Number.isFinite(v) ? v : dflt
}

// Zero-spawn pre-gate thresholds (mirror pi_bridge defaults; the bridge
// re-checks with full signal analysis — this gate only avoids spawns).
// Env overrides use the SAME `AUTOCOMPACTOR_*` namespace the Python bridge
// reads (config_lib), so the gate and the bridge can never diverge on a
// tuned value. The old `AUTOCOMPACTOR_PI_*` threshold aliases were TS-only
// (the flattened Python config never honored them) and were removed; the
// Pi-only control flags AUTOCOMPACTOR_PI_MODE / _INTERCEPT stay namespaced.
const SOFT_PCT = num("AUTOCOMPACTOR_SOFT_PCT", cfgNum("SOFT_PCT", 0.40))
const MIN_SAVINGS = num("AUTOCOMPACTOR_MIN_SAVINGS", cfgNum("MIN_SAVINGS", 30_000))
const POST_FLOOR = num("AUTOCOMPACTOR_POST_FLOOR", cfgNum("POST_FLOOR", 70_000))
const COOLDOWN = num("AUTOCOMPACTOR_COOLDOWN", cfgNum("COOLDOWN", 25_000))
const RESERVE_FALLBACK = num("AUTOCOMPACTOR_RESERVE", cfgNum("RESERVE", 40_000))
const DETAIL_MIN_TOKENS = num(
  "AUTOCOMPACTOR_DETAIL_MIN_TOKENS",
  cfgNum("DETAIL_MIN_TOKENS", POST_FLOOR + MIN_SAVINGS),
)
const DETAIL_COOLDOWN = num(
  "AUTOCOMPACTOR_DETAIL_COOLDOWN",
  cfgNum("DETAIL_COOLDOWN", Math.max(COOLDOWN, 75_000)),
)

function num(name: string, dflt: number): number {
  const v = parseFloat(process.env[name] ?? "")
  return Number.isFinite(v) ? v : dflt
}

// Window-aware SOFT_PCT: mirrors pi_bridge's float_windowed(). When the
// model's context window is >= 300K, the _WIDE variant wins (config.json
// SOFT_PCT_WIDE or env), falling back to the flat SOFT_PCT. A 976K window
// at 0.40 would need 374K tokens to trigger — unreachable in most sessions.
// SOFT_PCT_WIDE (~0.25) scales the gate so compaction advise fires at a
// practical threshold (~234K), with HARD_PCT_WIDE (~0.40) forcing it.
function softPctFor(ctxWindow: number): number {
  if (ctxWindow >= 300_000) {
    return num("AUTOCOMPACTOR_SOFT_PCT_WIDE", cfgNum("SOFT_PCT_WIDE", SOFT_PCT))
  }
  return SOFT_PCT
}

function mode(verdictMode?: unknown): "advise" | "actuate" {
  const env = process.env.AUTOCOMPACTOR_PI_MODE
  if (env === "actuate" || env === "advise") return env
  if (verdictMode === "actuate" || verdictMode === "advise") return verdictMode
  return CFG?.MODE === "actuate" ? "actuate" : "advise"
}

function configuredNextStepMode(value?: unknown): "off" | "advisory" | "autonomous" {
  const raw = (
    process.env.AUTOCOMPACTOR_NEXTSTEP ??
    String(value ?? CFG?.NEXTSTEP ?? "autonomous")
  ).trim().toLowerCase()
  if (raw === "off" || raw === "advisory" || raw === "autonomous") return raw
  return "autonomous"
}

function configuredWaitMode(value?: unknown): "poll" | "advisory" | "off" {
  const raw = (
    process.env.AUTOCOMPACTOR_NEXTSTEP_WAIT ??
    String(value ?? CFG?.NEXTSTEP_WAIT ?? "poll")
  ).trim().toLowerCase()
  if (raw === "poll" || raw === "advisory" || raw === "off") return raw
  return "poll"
}

function configuredWaitPollS(value?: unknown): number {
  const env = parseFloat(process.env.AUTOCOMPACTOR_WAIT_POLL_S ?? "")
  if (Number.isFinite(env) && env > 0) return env
  const fromVal = parseFloat(String(value ?? ""))
  if (Number.isFinite(fromVal) && fromVal > 0) return fromVal
  return cfgNum("WAIT_POLL_S", 60)
}

function configuredWaitPollMax(value?: unknown): number {
  const env = parseFloat(process.env.AUTOCOMPACTOR_WAIT_POLL_MAX ?? "")
  if (Number.isFinite(env) && env > 0) return Math.floor(env)
  const fromVal = parseFloat(String(value ?? ""))
  if (Number.isFinite(fromVal) && fromVal > 0) return Math.floor(fromVal)
  return Math.floor(cfgNum("WAIT_POLL_MAX", 20))
}

function isWaitShapedStep(stepSrc: string, nextStepWait?: unknown): boolean {
  if (nextStepWait === true || nextStepWait === 1 || nextStepWait === "1") return true
  return String(stepSrc || "").startsWith("open_work:waiting")
}

// Visible delivery: when the session is idle (not streaming), omit deliverAs
// so Pi's final branch renders+persists immediately. When not idle, use
// nextTurn (the proven anti-swallow path for mid-stream events).
type Deliver = "nextTurn" | "immediate"

function deliveryFor(ctx: ExtensionContext): Deliver {
  try {
    const idle = typeof ctx.isIdle === "function" ? ctx.isIdle() : true
    return idle ? "immediate" : "nextTurn"
  } catch {
    return "nextTurn"
  }
}

type AutoResumePayload = {
  compactionId: string
  digestText?: string
  digestType?: string
  statusText: string
  step: string
  stepSrc: string
  waitShaped?: boolean
  waitMode?: "poll" | "advisory" | "off"
  waitPollS?: number
  waitPollMax?: number
}

type WaitPollState = {
  brief: string
  stepSrc: string
  compactionId: string
  remaining: number
  delayMs: number
  timer: ReturnType<typeof setTimeout> | null
}

function interceptEnabled(): boolean {
  // Durable delivery is config.json PI_INTERCEPT; the env var, when SET (to
  // anything non-empty), WINS in both directions — "1" forces on, any other
  // value forces off — so a lingering export can't silently fight the config
  // (mirrors the MODE fix pattern; avoids the 2026-06-10 env-delivery
  // regression where advise-only shipped because the env never reached Pi).
  const env = process.env.AUTOCOMPACTOR_PI_INTERCEPT
  if (env !== undefined && env !== "") {
    if (env !== "1") return false
  } else {
    const cfg = CFG?.PI_INTERCEPT
    if (!(cfg === true || cfg === 1 || cfg === "1")) return false
  }
  try {
    // Coexist passively with pi-custom-compactor: never intercept if present.
    const settings = JSON.parse(
      fs.readFileSync(path.join(os.homedir(), ".pi", "agent", "settings.json"), "utf8"),
    )
    const pkgs: string[] = settings?.packages ?? []
    if (pkgs.some((p) => String(p).includes("pi-custom-compactor"))) return false
  } catch {
    // unreadable settings -> stay conservative, no intercept
    return false
  }
  return true
}

type StatusLevel = "info" | "warning" | "error"

function setAcStatus(ctx: ExtensionContext, text: string | undefined): void {
  try {
    if (!ctx.hasUI) return
    const ui = ctx.ui as any
    if (typeof ui?.setStatus === "function") {
      ui.setStatus("autocompactor", text)
    }
  } catch {
    /* status is best-effort */
  }
}

function notify(ctx: ExtensionContext, message: string, level: StatusLevel): void {
  try {
    if (!ctx.hasUI) return
    ctx.ui.notify(message, level)
  } catch {
    /* notification is best-effort */
  }
}

// Persistent, user-visible chat line. The delivery channel matters: Pi's
// AgentSession.sendCustomMessage (verified against 0.79.9) only renders+persists
// a message via its final (not-streaming) `else` branch. agent_end listeners run
// while `agent.state.isStreaming` is still true, and compaction events fire
// mid-stream — in BOTH cases `deliverAs:"followUp"` routes the message to
// agent.followUp() (the agent's input queue): not rendered, never persisted, and
// it can even trigger a spurious continuation turn that injects the status text
// as agent input. So `followUp` is never used for a visible status.
//
// When the session is IDLE, omit deliverAs entirely so Pi's final branch
// renders+persists immediately (user sees compact status without typing).
// When NOT idle (mid-stream / agent_end), use deliverAs:"nextTurn" — the only
// channel that survives a compaction and renders at the next user prompt.
// The recurring advisory is de-duplicated by text (see lastAdvisory).

function persistVisible(
  pi: ExtensionAPI,
  message: string,
  level: StatusLevel = "info",
  deliver: Deliver = "nextTurn",
): void {
  try {
    const opts = deliver === "immediate" ? undefined : { deliverAs: "nextTurn" as const }
    pi.sendMessage(
      {
        customType: "autocompactor.status",
        content: message,
        display: true,
        details: { level, timestamp: Date.now() },
      },
      opts,
    )
  } catch {
    /* persistent status is best-effort */
  }
}

function announce(
  pi: ExtensionAPI,
  ctx: ExtensionContext,
  message: string,
  level: StatusLevel = "info",
  persist = true,
  deliver?: Deliver,
): void {
  setAcStatus(ctx, message)
  notify(ctx, message, level)
  if (persist) persistVisible(pi, message, level, deliver ?? deliveryFor(ctx))
}

function errorText(err: unknown): string {
  if (err instanceof Error && err.message) return err.message
  if (typeof err === "string" && err) return err
  return "unknown error"
}

function cleanBlock(value: unknown): string {
  return typeof value === "string" ? value.trim() : ""
}

function indentBlock(text: string): string {
  return text.split("\n").map((line) => `  ${line}`).join("\n")
}

function withContextState(message: string, contextState: unknown): string {
  const state = cleanBlock(contextState)
  if (!state) return message
  return `${message}\ncontext composition:\n${indentBlock(state)}`
}

function withStatsBlock(message: string, title: string, stats: unknown): string {
  const body = cleanBlock(stats)
  if (!body) return message
  return `${message}\n${title}:\n${indentBlock(body)}`
}

// Fire-and-forget ctx.compact() that can never leave the reentrancy flag stuck.
// ctx.compact reports completion via onComplete/onError callbacks, but if it
// throws synchronously or returns a promise that rejects WITHOUT invoking a
// callback, the caller's try/catch would swallow it and `selfTriggered` would
// stay true forever — bricking all future compaction. We settle exactly once
// (onComplete | onError | sync-throw | promise-reject), always clearing the flag.
//
// Transient provider blips ("Summarization failed: Connection error") are
// retried a few times with backoff so actuate does not burn the cooldown on a
// single failed summarizer call and then sit silent until the user pokes.
const COMPACT_RETRY_MAX = Math.max(
  0,
  Math.floor(num("AUTOCOMPACTOR_COMPACT_RETRIES", cfgNum("COMPACT_RETRIES", 2))),
)
const COMPACT_RETRY_BASE_MS = Math.max(
  250,
  Math.floor(num("AUTOCOMPACTOR_COMPACT_RETRY_MS", cfgNum("COMPACT_RETRY_MS", 2_000))),
)

function isTransientCompactError(errMsg: string): boolean {
  const m = (errMsg || "").toLowerCase()
  if (!m) return false
  // Never retry intentional cancel (user abort or our intercept of a
  // *different* compact). Retrying cancel just loops.
  if (m.includes("cancelled") || m.includes("canceled") || m.includes("aborted")) {
    return false
  }
  return (
    m.includes("connection error") ||
    m.includes("connection reset") ||
    m.includes("econnreset") ||
    m.includes("econnrefused") ||
    m.includes("etimedout") ||
    m.includes("socket hang up") ||
    m.includes("network") ||
    m.includes("fetch failed") ||
    m.includes("503") ||
    m.includes("502") ||
    m.includes("504") ||
    m.includes("rate limit") ||
    m.includes("overloaded") ||
    m.includes("temporarily unavailable") ||
    m.includes("summarization failed")
  )
}

function safeCompact(
  pi: ExtensionAPI,
  ctx: ExtensionContext,
  customInstructions: string | undefined,
  clear: () => void,
  afterComplete?: () => void,
  afterError?: (errMsg: string) => void,
  attempt: number = 0,
): void {
  let settled = false
  const finish = (errMsg?: string): void => {
    if (settled) return
    settled = true
    if (errMsg && isTransientCompactError(errMsg) && attempt < COMPACT_RETRY_MAX) {
      const next = attempt + 1
      const delay = COMPACT_RETRY_BASE_MS * Math.pow(2, attempt)
      announce(
        pi,
        ctx,
        `autocompactor: compaction failed (transient) — ${errMsg}; retry ${next}/${COMPACT_RETRY_MAX} in ${Math.round(delay / 1000)}s.`,
        "warning",
        true,
      )
      // Keep selfTriggered set across retries (clear only on final settle).
      setTimeout(() => {
        safeCompact(pi, ctx, customInstructions, clear, afterComplete, afterError, next)
      }, delay)
      return
    }
    if (errMsg) announce(pi, ctx, `autocompactor: compaction failed — ${errMsg}.`, "error", true)
    clear()
    if (errMsg) afterError?.(errMsg)
    else afterComplete?.()
  }
  try {
    const ret: any = ctx.compact({
      customInstructions,
      onComplete: () => finish(),
      onError: (err: unknown) => finish(errorText(err)),
    })
    if (ret && typeof ret.then === "function") {
      ret.then(undefined, (err: unknown) => finish(errorText(err)))
    }
  } catch (err) {
    finish(errorText(err))
  }
}

async function bridge(
  pi: ExtensionAPI,
  ctx: ExtensionContext,
  sub: string,
  extraArgs: string[] = [],
  timeoutMs: number = EXEC_TIMEOUT_MS,
): Promise<any | null> {
  try {
    const session = ctx.sessionManager.getSessionFile()
    const args = [BRIDGE, sub, ...extraArgs, "--cwd", ctx.cwd]
    if (session) args.push("--session", session)
    const res = await pi.exec("python3", args, {
      timeout: timeoutMs,
      cwd: ctx.cwd,
    })
    const out = (res.stdout ?? "").trim()
    if (res.code !== 0 || !out) return null
    return JSON.parse(out)
  } catch {
    return null // bridge absent/garbage/timeout -> autocompactor goes quiet
  }
}

export default function autocompactor(pi: ExtensionAPI) {
  // In-memory cooldown: token reading at the last recommendation.
  let lastRecTokens = -Infinity
  // Count of in-flight ctx.compact() calls we started with our own
  // customInstructions (actuate or intercept re-trigger). Boolean selfTriggered
  // alone races: prepare(LLM) can take tens of seconds, and a concurrent
  // native compact's session_before_compact must still see "we own a compact"
  // so it does NOT return {cancel:true} and abort our in-flight call with
  // Pi's "Compaction cancelled" error.
  let enrichedCompactsInFlight = 0
  let selfTriggered = false // reentrancy flag for actuate/intercept mode
  let compactionPreTokens = 0 // captured in session_before_compact for post summary
  let lastDetailTokens = -Infinity // lower-cost context-composition notice cadence
  let bridgeWarned = false
  let lastAdvisory = "" // dedupe key for the recurring advise/reentrancy notice
  let pendingAutoResume: AutoResumePayload | null = null
  let lastAutoResumeCompactionId = ""
  let waitPoll: WaitPollState | null = null
  // Bound for tests / host overrides; production uses ExtensionContext.
  let waitPollCtx: ExtensionContext | null = null

  const beginEnrichedCompact = (): void => {
    enrichedCompactsInFlight += 1
    selfTriggered = true
  }
  const endEnrichedCompact = (): void => {
    enrichedCompactsInFlight = Math.max(0, enrichedCompactsInFlight - 1)
    if (enrichedCompactsInFlight === 0) selfTriggered = false
  }
  const ownsCompaction = (event?: any): boolean => {
    if (enrichedCompactsInFlight > 0 || selfTriggered) return true
    const instr = event?.customInstructions
    return typeof instr === "string" && instr.trim().length > 0
  }

  const clearWaitPoll = (): void => {
    if (waitPoll?.timer) {
      try { clearTimeout(waitPoll.timer) } catch { /* ignore */ }
    }
    waitPoll = null
    waitPollCtx = null
  }

  const fireWaitPoll = (): void => {
    const state = waitPoll
    const ctx = waitPollCtx
    if (!state || !ctx) return
    try {
      const idle = typeof ctx.isIdle === "function" ? ctx.isIdle() : true
      if (!idle) {
        // Session busy — try once more after the same delay, without
        // burning a remaining slot if we still have budget.
        if (state.remaining > 0) {
          state.timer = setTimeout(fireWaitPoll, state.delayMs)
        } else {
          clearWaitPoll()
        }
        return
      }
      state.remaining -= 1
      const pollMessage =
        `[autocompactor-nextstep] Polling open work after wait interval ` +
        `(${state.stepSrc || "open_work:waiting"}; remaining polls: ${state.remaining}).\n` +
        `${state.brief}\n\n` +
        "Check the monitor command(s) now. If still running, report status and stop; " +
        "if succeeded, complete the on-success handoff. Do not start unrelated work."
      pi.sendMessage(
        {
          customType: "autocompactor.nextstep.poll",
          content: pollMessage,
          display: true,
          details: {
            waitPoll: true,
            compactionId: state.compactionId,
            source: state.stepSrc,
            remaining: state.remaining,
          },
        },
        { triggerTurn: true },
      )
      setAcStatus(
        ctx,
        state.remaining > 0
          ? `autocompactor: waiting · next poll in ${Math.round(state.delayMs / 1000)}s · ${state.remaining} left`
          : "autocompactor: waiting · final poll fired",
      )
      if (state.remaining > 0) {
        state.timer = setTimeout(fireWaitPoll, state.delayMs)
      } else {
        waitPoll = null
        waitPollCtx = null
      }
    } catch {
      clearWaitPoll()
    }
  }

  const scheduleWaitPoll = (
    ctx: ExtensionContext,
    brief: string,
    stepSrc: string,
    compactionId: string,
    delayS: number,
    maxPolls: number,
  ): void => {
    clearWaitPoll()
    const delayMs = Math.max(5, delayS) * 1000
    const remaining = Math.max(1, Math.floor(maxPolls))
    waitPollCtx = ctx
    waitPoll = {
      brief,
      stepSrc,
      compactionId,
      remaining,
      delayMs,
      timer: null,
    }
    waitPoll.timer = setTimeout(fireWaitPoll, delayMs)
    setAcStatus(
      ctx,
      `autocompactor: waiting · poll in ${Math.round(delayMs / 1000)}s · max ${remaining}`,
    )
  }

  const flushAutoResume = (ctx: ExtensionContext): void => {
    const payload = pendingAutoResume
    if (!payload) return
    pendingAutoResume = null
    if (payload.compactionId && payload.compactionId === lastAutoResumeCompactionId) return
    lastAutoResumeCompactionId = payload.compactionId
    try {
      const idle = typeof ctx.isIdle === "function" ? ctx.isIdle() : true
      const queuedOptions = idle ? undefined : ({ deliverAs: "followUp" } as const)
      if (payload.digestText) {
        pi.sendMessage(
          {
            customType: payload.digestType ?? "autocompactor.digest",
            content: payload.digestText,
            display: false,
          },
          queuedOptions,
        )
      }
      if (payload.statusText) {
        pi.sendMessage(
          {
            customType: "autocompactor.status",
            content: payload.statusText,
            display: true,
          },
          queuedOptions,
        )
      }

      const waitShaped = Boolean(payload.waitShaped)
      const waitMode = payload.waitMode ?? "poll"

      // Wait-shaped autonomous resume: do NOT re-enter a coding task with
      // triggerTurn. Surface the wait brief and optionally schedule polls.
      if (waitShaped && waitMode !== "off") {
        const waitMessage =
          `[autocompactor-nextstep] Session is waiting for background work ` +
          `(${payload.stepSrc || "open_work:waiting"}).\n` +
          `${payload.step}\n\n` +
          (waitMode === "poll"
            ? `Polling every ${Math.round(payload.waitPollS ?? 60)}s (max ${payload.waitPollMax ?? 20}). ` +
              "Do not start unrelated work."
            : "Advisory only — no automatic poll. Reply when ready to check status.")
        pi.sendMessage(
          {
            customType: "autocompactor.nextstep.wait",
            content: waitMessage,
            display: true,
            details: {
              waitShaped: true,
              waitMode,
              compactionId: payload.compactionId,
              source: payload.stepSrc,
            },
          },
          idle ? undefined : { deliverAs: "followUp" },
        )
        if (waitMode === "poll" && idle) {
          scheduleWaitPoll(
            ctx,
            payload.step,
            payload.stepSrc,
            payload.compactionId,
            payload.waitPollS ?? 60,
            payload.waitPollMax ?? 20,
          )
        } else {
          setAcStatus(
            ctx,
            waitMode === "poll"
              ? "autocompactor: waiting (poll deferred until idle)"
              : "autocompactor: waiting (advisory)",
          )
        }
        return
      }

      const taskMessage =
        `[autocompactor-nextstep] Continuing automatically after compaction. Recovered next step (${payload.stepSrc || "unknown"}):\n` +
        `${payload.step}\n\n` +
        "Verify the current repository/session state before editing, then continue executing the active task using the available tools."
      pi.sendMessage(
        {
          customType: "autocompactor.nextstep.task",
          content: taskMessage,
          display: true,
          details: {
            autonomous: true,
            compactionId: payload.compactionId,
            source: payload.stepSrc,
          },
        },
        idle ? { triggerTurn: true } : { deliverAs: "followUp" },
      )
      setAcStatus(ctx, "autocompactor: autonomous next step queued after compaction.")
    } catch {
      /* never break Pi */
    }
  }

// agent_end: the boundary moment. Mostly zero-spawn pre-gate; once context
  // is large enough to reclaim meaningful tokens, occasionally ask the bridge
  // for a composition-only monitoring readout before the compaction gate.
  pi.on("agent_end", async (_event, ctx) => {
    try {
      const usage = ctx.getContextUsage()
      if (!usage || usage.tokens === null) {
        setAcStatus(ctx, "autocompactor: monitoring — context usage unavailable.")
        return // unknown right after compaction
      }
      const window = usage.contextWindow - RESERVE_FALLBACK
      if (window <= 0) {
        setAcStatus(ctx, "autocompactor: monitoring — invalid effective context window.")
        return
      }
      // Pre-gate: below SOFT or nothing worth reclaiming or cooling down -> no spawn.
      // Window-aware: large windows (>=300K) use SOFT_PCT_WIDE so the gate
      // scales with the model's context window — a 976K GLM-5.2 window at
      // 0.40 would need 374K tokens (unreachable); WIDE (~0.25) fires at ~234K.
      const softPct = softPctFor(usage.contextWindow)
      const occupancy = usage.tokens / window
      const estReclaim = usage.tokens - POST_FLOOR
      if (estReclaim < MIN_SAVINGS) {
        setAcStatus(
          ctx,
          `autocompactor: monitoring — estimated reclaim ~${Math.max(estReclaim, 0).toLocaleString()} tokens, below ${MIN_SAVINGS.toLocaleString()} minimum.`,
        )
        return
      }
      if (occupancy < softPct) {
        if (
          usage.tokens >= DETAIL_MIN_TOKENS &&
          usage.tokens - lastDetailTokens >= DETAIL_COOLDOWN
        ) {
          const detail = await bridge(pi, ctx, "evaluate", [
            "--tokens", String(usage.tokens),
            "--context-window", String(usage.contextWindow),
            "--reserve", String(RESERVE_FALLBACK),
          ])
          if (detail) {
            lastDetailTokens = usage.tokens
            const msg = withContextState(
              `autocompactor: monitoring — ${detail.reason ?? `${usage.tokens.toLocaleString()} tokens`}; below compaction gate.`,
              detail.contextState,
            )
            setAcStatus(ctx, msg)
            notify(ctx, msg, "info")
            return
          }
        }
        setAcStatus(
          ctx,
          `autocompactor: monitoring — ${usage.tokens.toLocaleString()} tokens (${(occupancy * 100).toFixed(0)}% effective), below ${(softPct * 100).toFixed(0)}% gate.`,
        )
        return
      }
      if (usage.tokens - lastRecTokens < COOLDOWN) {
        setAcStatus(
          ctx,
          `autocompactor: monitoring — cooldown (${Math.max(usage.tokens - lastRecTokens, 0).toLocaleString()}/${COOLDOWN.toLocaleString()} tokens since last recommendation).`,
        )
        return
      }

      const verdict = await bridge(pi, ctx, "evaluate", [
        "--tokens", String(usage.tokens),
        "--context-window", String(usage.contextWindow),
        "--reserve", String(RESERVE_FALLBACK),
      ])
      if (!verdict) {
        const msg = "autocompactor: bridge evaluate returned no data; run install_pi.py --status or reinstall."
        setAcStatus(ctx, msg)
        if (!bridgeWarned) {
          announce(pi, ctx, msg, "warning", true)
          bridgeWarned = true
        }
        return
      }
      if (!verdict.recommend) {
        setAcStatus(ctx, `autocompactor: evaluated — no compaction: ${verdict.reason ?? "criteria not met"}.`)
        return
      }
      lastRecTokens = usage.tokens
      const effMode = mode(verdict.mode)

      // Build a criteria-aware message from the verdict reason (which already
      // includes occupancy + any gating signals from the bridge) or fall back
      // to the raw token count.
      const reason = verdict.reason ?? `${usage.tokens?.toLocaleString() ?? "?"} tokens`

      if (effMode === "actuate" && !selfTriggered) {
        // Mark ownership BEFORE prepare so concurrent native compact hooks
        // see ownsCompaction() and never cancel our in-flight enriched path.
        beginEnrichedCompact()
        compactionPreTokens = usage.tokens
        announce(
          pi,
          ctx,
          withContextState(
            `autocompactor: criteria met — ${reason}; running compaction now.`,
            verdict.contextState,
          ),
          "info",
          true,
        )
        const prep = await bridge(
          pi, ctx, "prepare", ["--trigger", "actuate"], PREPARE_TIMEOUT_MS)
        if (!prep?.customInstructions) {
          announce(
            pi,
            ctx,
            "autocompactor: prepare returned no custom instructions; compacting anyway.",
            "warning",
            true,
          )
        }
        safeCompact(
          pi,
          ctx,
          prep?.customInstructions,
          () => { endEnrichedCompact() },
          () => flushAutoResume(ctx),
          () => {
            // Failed compact must not lock the session behind cooldown.
            lastRecTokens = -Infinity
            pendingAutoResume = null
            clearWaitPoll()
          },
        )
      } else {
        // Advise mode OR actuate mode with reentrancy guard (compaction in
        // flight): RECURRING advisory — fires on every qualifying agent_end
        // (cooldown-gated). It MUST persist via "nextTurn": followUp would be
        // swallowed while streaming (agent_end runs before isStreaming clears),
        // which is exactly when this fires. Live status rides setStatus/notify;
        // the durable chat line is deduped by text so identical advisories
        // can't pile up at the next user prompt (the reason it was on followUp).
        const modeTag = effMode === "actuate"
          ? "compaction in progress"
          : "advise mode"
        const advisory = withContextState(
          `autocompactor: criteria met — ${reason} (${modeTag}).`,
          verdict.contextState,
        )
        setAcStatus(ctx, advisory)
        notify(ctx, advisory, "warning")
        if (advisory !== lastAdvisory) {
          lastAdvisory = advisory
          persistVisible(pi, advisory, "warning")
        }
      }
    } catch {
      /* never break Pi */
    }
  })

  // session_before_compact: prepare fire-and-forget (backup + artifacts);
  // optional cancel-and-retrigger enrichment when PI_INTERCEPT is on.
  //
  // CRITICAL: Pi throws "Compaction cancelled" when ANY handler returns
  // {cancel:true} for the compact that is currently starting. That includes
  // OUR own actuate ctx.compact({customInstructions}) — so we must NEVER
  // cancel a compact we already own / already enriched. See ownsCompaction().
  pi.on("session_before_compact", async (event, ctx) => {
    try {
      // Self-triggered / already-enriched: agent_end (or a prior intercept
      // re-trigger) already ran prepare and passed customInstructions into
      // ctx.compact(). Let that compaction run untouched — a second
      // prepare(trigger=native) here is redundant and, worse, returning
      // {cancel:true} aborts the in-flight compact with "Compaction cancelled".
      if (ownsCompaction(event)) {
        setAcStatus(
          ctx,
          "autocompactor: enriched compaction in progress; not cancelling.",
        )
        return
      }
      const usage = ctx.getContextUsage()
      // Capture pre-compaction tokens for the post-compaction summary.
      if (usage && usage.tokens != null) {
        compactionPreTokens = usage.tokens
      }
      // Non-intercept native path: AWAIT prepare before yielding to native
      // compaction. Pi awaits this handler's promise (it honors a {cancel}
      // return), so awaiting here holds the compaction until backup + artifacts
      // + state are persisted — otherwise session_compact's reinject can race
      // ahead and build the digest from stale/empty artifacts (data loss).
      // We pass --skip-llm because the prepare's customInstructions are DISCARDED
      // on this path (native compaction uses Pi's own summarizer), so the only
      // outputs that matter — the on-disk artifacts and state — are cheap and
      // fast; the 45s LLM digest would just stall every native compaction.
      if (!interceptEnabled()) {
        announce(
          pi,
          ctx,
          "autocompactor: native compaction starting — preparing backup/artifacts.",
          "info",
          true,
        )
        await bridge(pi, ctx, "prepare", ["--trigger", "native", "--skip-llm", "1"], PREPARE_TIMEOUT_MS)
        return
      }
      // Intercept mode: cancel *native* (unenriched) compaction and re-trigger
      // with our customInstructions. Only cancel if we successfully arm a
      // replacement compact — otherwise let native proceed.
      announce(
        pi,
        ctx,
        "autocompactor: native compaction starting — preparing backup/artifacts with custom instructions.",
        "info",
        true,
      )
      const prep = await bridge(
        pi, ctx, "prepare", ["--trigger", "native"], PREPARE_TIMEOUT_MS)
      // Re-check ownership after the (possibly long) prepare: another actuate
      // may have started while we awaited the bridge.
      if (ownsCompaction(event)) {
        setAcStatus(
          ctx,
          "autocompactor: enriched compaction started during prepare; not cancelling.",
        )
        return
      }
      if (!prep?.customInstructions) {
        announce(
          pi,
          ctx,
          "autocompactor: prepare returned no custom instructions; allowing native compaction unchanged.",
          "warning",
          true,
        )
        return
      }
      beginEnrichedCompact()
      announce(
        pi,
        ctx,
        "autocompactor: intercepting native compaction with custom instructions.",
        "info",
        true,
      )
      safeCompact(
        pi,
        ctx,
        prep.customInstructions,
        () => { endEnrichedCompact() },
        () => flushAutoResume(ctx),
        () => {
          lastRecTokens = -Infinity
          pendingAutoResume = null
          clearWaitPoll()
        },
      )
      return { cancel: true }
    } catch {
      /* fall through to native compaction untouched */
    }
  })

  // session_compact: re-inject the artifact digest as a persisted one-shot,
  // and show a clear post-compaction summary to the user.
  pi.on("session_compact", async (event, ctx) => {
    try {
      // Pass the runtime window so the bridge resolves the SAME effective
      // window the evaluate/prepare paths used (otherwise reinject's
      // post-compaction occupancy readout is computed off a stale config window).
      const postUsage = ctx.getContextUsage()
      const reinjectArgs = postUsage?.contextWindow
        ? ["--context-window", String(postUsage.contextWindow)]
        : []
      const inj = await bridge(pi, ctx, "reinject", reinjectArgs)
      const digestText = typeof inj?.text === "string" ? inj.text : ""
      const digestType = typeof inj?.customType === "string" ? inj.customType : "autocompactor.digest"

      // Optional post-compaction next-step surfacing. Sourced at prepare time
      // from the rich pre-compaction transcript (pending todo → open_work wait
      // → last user task → last correction) and staged in bridge state;
      // recovered here on reinject. Gated by NEXTSTEP mode:
      //   "off"        — never surface
      //   "advisory"   — surface a ready-to-run brief for the next human turn
      //   "autonomous" — resume after compaction (coding triggerTurn, or
      //                  wait-shaped scheduled poll — see NEXTSTEP_WAIT)
      // Config: top-level NEXTSTEP in config.json, overridable by
      // AUTOCOMPACTOR_NEXTSTEP env var. Default is autonomous.
      // New compact cancels any in-flight wait poll for this session instance.
      clearWaitPoll()
      const nextStepMode = configuredNextStepMode(inj?.nextStepMode)
      const step = ((inj?.nextStep as string | undefined) ?? "").trim()
      const stepSrc = ((inj?.nextStepSource as string | undefined) ?? "").trim()
      const waitShaped = isWaitShapedStep(stepSrc, inj?.nextStepWait)
      const waitMode = configuredWaitMode(inj?.nextStepWaitMode)
      const waitPollS = configuredWaitPollS(inj?.waitPollS)
      const waitPollMax = configuredWaitPollMax(inj?.waitPollMax)
      const compactionId = String(
        event?.compactionEntry?.id ??
        `${event?.reason ?? "unknown"}:${event?.compactionEntry?.timestamp ?? ""}:${stepSrc}:${step.slice(0, 80)}`,
      )
      const autoResume = Boolean(
        step && nextStepMode === "autonomous" && !event?.willRetry &&
        (selfTriggered || event?.reason !== "manual"),
      )

      if (digestText && !autoResume) {
        // Artifact digest (persisted in context for the model — display: false
        // keeps it out of the user-facing chat history). Autonomous mode flushes
        // this explicitly with the triggered resume turn; nextTurn would wait
        // for another human prompt and miss the resumed turn.
        // Idle: omit deliverAs so it lands immediately; non-idle: nextTurn.
        const digOpts = deliveryFor(ctx) === "immediate"
          ? undefined
          : ({ deliverAs: "nextTurn" } as const)
        pi.sendMessage(
          {
            customType: digestType,
            content: digestText,
            display: false,
          },
          digOpts,
        )
      }

      if (step && nextStepMode === "advisory") {
        const advOpts = deliveryFor(ctx) === "immediate"
          ? undefined
          : ({ deliverAs: "nextTurn" } as const)
        pi.sendMessage(
          {
            customType: "autocompactor.nextstep.advisory",
            content:
              `autocompactor-nextstep: recovered next step (${stepSrc || "unknown"}) —\n` +
              `${step}\n\n` +
              `Continue with this step manually, or set AUTOCOMPACTOR_NEXTSTEP=autonomous to resume automatically after future compactions.`,
            display: true,
          },
          advOpts,
        )
      }

      // Visible post-compaction status is both a UI notification/status and a
      // persistent displayed custom message. tokensBefore from the compaction
      // entry survives runtime reloads better than closure state.
      const usage = ctx.getContextUsage()
      const preTokens = event?.compactionEntry?.tokensBefore ?? compactionPreTokens
      const postTokens = usage?.tokens
      let msg = "autocompactor: compaction completed."
      if (preTokens > 0 && postTokens != null && postTokens > 0) {
        const reclaimed = Math.max(preTokens - postTokens, 0)
        // Anchor the post-compaction occupancy to the true model window the
        // runtime reports (no reserve guess, no bare %): "X% of ~Wt model
        // window" cannot be misread the way a lone "(47%)" was.
        const modelWindow = usage?.contextWindow ?? 0
        const postOcc = modelWindow > 0
          ? ` (${((postTokens / modelWindow) * 100).toFixed(0)}% of ~${modelWindow.toLocaleString()}t model window)`
          : ""
        msg = `autocompactor: compaction completed — context ${preTokens.toLocaleString()} → ${postTokens.toLocaleString()} tokens${postOcc}; reclaimed ~${reclaimed.toLocaleString()} tokens.`
      } else if (preTokens > 0) {
        msg = `autocompactor: compaction completed — before context was ${preTokens.toLocaleString()} tokens; current usage will refresh on the next turn.`
      } else if (postTokens != null && postTokens > 0) {
        msg = `autocompactor: compaction completed — current context is ${postTokens.toLocaleString()} tokens.`
      }
      msg = withStatsBlock(msg, "pre-compaction accounting", inj?.compactionStats)
      if (autoResume) {
        pendingAutoResume = {
          compactionId,
          digestText,
          digestType,
          statusText: msg,
          step,
          stepSrc,
          waitShaped,
          waitMode,
          waitPollS,
          waitPollMax,
        }
        // Status only (persist=false); flushAutoResume surfaces the durable
        // status + nextstep together so wait/coding paths share one channel.
        announce(pi, ctx, msg, "info", false)
        if (!selfTriggered) setTimeout(() => flushAutoResume(ctx), 0)
      } else {
        announce(pi, ctx, msg, "info", true)
      }
      compactionPreTokens = 0 // reset for next compaction
    } catch {
      /* never break Pi */
    }
  })

  // /contextinventory — on-demand deep context-window breakdown (spec §5/§11).
  // Renders the ContextInventory report as a widget above the editor, using the
  // REAL aggregate token total from ctx.getContextUsage() (not the chars/4
  // fallback the standalone shim uses when --total is absent). Surfaces
  // per-package tool schemas in the floor (via floor-probe.json) and the
  // per-tool/per-item dynamic ledger with dormancy/reclaim flags.
  //   /contextinventory            — with floor probe (per-package schemas)
  //   /contextinventory no-probe   — honest residual bucket, no probe read
  pi.registerCommand("contextinventory", {
    description: "Deep context-window breakdown (floor + dynamic + reclaim)",
    handler: async (args, ctx) => {
      try {
        if (!ctx.hasUI) {
          notify(ctx, "contextinventory: requires an interactive UI.", "info")
          return
        }
        const session = ctx.sessionManager.getSessionFile()
        if (!session) {
          notify(ctx, "contextinventory: no active session to analyze.", "warning")
          return
        }
        const shim = path.join(path.dirname(BRIDGE), "context_inventory.py")
        const cmdArgs: string[] = [shim, "--session", session]
        const usage = ctx.getContextUsage()
        if (usage && usage.tokens != null && usage.tokens > 0) {
          cmdArgs.push("--total", String(usage.tokens))
        }
        if (usage && usage.contextWindow) {
          cmdArgs.push("--window", String(usage.contextWindow))
        }
        // "/contextinventory no-probe" skips the floor probe.
        if (String(args ?? "").includes("no-probe")) {
          cmdArgs.push("--no-probe")
        }
        setAcStatus(ctx, "autocompactor: building context inventory…")
        const res = await pi.exec("python3", cmdArgs, {
          timeout: 15_000,
          cwd: ctx.cwd,
        })
        const out = (res.stdout ?? "").trimEnd()
        if (res.code !== 0 || !out) {
          setAcStatus(ctx, "autocompactor: context inventory failed.")
          notify(
            ctx,
            "contextinventory: inventory returned no output (check the bridge).",
            "warning",
          )
          return
        }
        const lines = out.split("\n")
        const header =
          "contextinventory · " +
          (session.split(path.sep).pop() ?? session)
        // setWidget replaces any prior inventory render; re-invoking the
        // command refreshes it. Clear with the same name + undefined.
        ctx.ui.setWidget("autocompactor-inventory", [header, ...lines])
        setAcStatus(ctx, "autocompactor: context inventory shown above.")
        notify(ctx, "contextinventory: breakdown shown above the editor.", "info")
      } catch (err) {
        setAcStatus(ctx, "autocompactor: context inventory errored.")
        notify(ctx, `contextinventory: ${errorText(err)}`, "error")
      }
    },
  })
}
