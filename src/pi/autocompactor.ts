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
// is gated by AUTOCOMPACTOR_PI_INTERCEPT=1, default OFF, and is skipped when
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
  return verdictMode === "actuate" ? "actuate" : "advise"
}

function interceptEnabled(): boolean {
  if (process.env.AUTOCOMPACTOR_PI_INTERCEPT !== "1") return false
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
// as agent input. So `followUp` is never used for a visible status. The only
// channel that survives a compaction and renders is `deliverAs:"nextTurn"`: it is
// queued and injected+rendered at the next user prompt (flush at agent-session.js
// :797). EVERY visible notice — one-shot (load/compaction start/done/errors) and
// the recurring agent_end advisory alike — uses "nextTurn". The recurring advisory
// is de-duplicated by text (see lastAdvisory) so nextTurn cannot pile up stale
// duplicates at the next prompt, which was the original reason it was on followUp.
type Deliver = "nextTurn"

function persistVisible(
  pi: ExtensionAPI,
  message: string,
  level: StatusLevel = "info",
  deliver: Deliver = "nextTurn",
): void {
  try {
    pi.sendMessage(
      {
        customType: "autocompactor.status",
        content: message,
        display: true,
        details: { level, timestamp: Date.now() },
      },
      { deliverAs: deliver },
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
  deliver: Deliver = "nextTurn",
): void {
  setAcStatus(ctx, message)
  notify(ctx, message, level)
  if (persist) persistVisible(pi, message, level, deliver)
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
function safeCompact(
  pi: ExtensionAPI,
  ctx: ExtensionContext,
  customInstructions: string | undefined,
  clear: () => void,
): void {
  let settled = false
  const finish = (errMsg?: string): void => {
    if (settled) return
    settled = true
    if (errMsg) announce(pi, ctx, `autocompactor: compaction failed — ${errMsg}.`, "error", true)
    clear()
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
  let selfTriggered = false // reentrancy flag for actuate/intercept mode
  let compactionPreTokens = 0 // captured in session_before_compact for post summary
  let lastDetailTokens = -Infinity // lower-cost context-composition notice cadence
  let bridgeWarned = false
  let lastAdvisory = "" // dedupe key for the recurring advise/reentrancy notice

  pi.on("session_start", async (_event, ctx) => {
    try {
      announce(
        pi,
        ctx,
        `autocompactor: loaded (mode=${mode()}, bridge=${BRIDGE}).`,
        "info",
        true,
      )
    } catch {
      /* never break Pi */
    }
  })

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
        // Set selfTriggered BEFORE the prepare call to block overlapping
        // agent_end handlers from both triggering compaction.
        selfTriggered = true
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
        safeCompact(pi, ctx, prep?.customInstructions, () => { selfTriggered = false })
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
  // optional cancel-and-retrigger enrichment, default OFF.
  pi.on("session_before_compact", async (_event, ctx) => {
    try {
      // Self-triggered (actuate mode): agent_end already ran prepare
      // (trigger=actuate), captured pre-tokens, and passed its
      // customInstructions into ctx.compact(). Let that compaction run
      // untouched — a second prepare(trigger=native) here is redundant
      // (its result is discarded) and, with the optional LLM digest on,
      // would double the digest cost per self-triggered compaction.
      if (selfTriggered) {
        setAcStatus(ctx, "autocompactor: compaction already in progress; letting self-triggered compaction continue.")
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
      // Intercept mode: cancel native compaction and re-trigger with
      // our customInstructions.
      announce(
        pi,
        ctx,
        "autocompactor: native compaction starting — preparing backup/artifacts with custom instructions.",
        "info",
        true,
      )
      const prep = await bridge(
        pi, ctx, "prepare", ["--trigger", "native"], PREPARE_TIMEOUT_MS)
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
      selfTriggered = true
      announce(
        pi,
        ctx,
        "autocompactor: intercepting native compaction with custom instructions.",
        "info",
        true,
      )
      safeCompact(pi, ctx, prep.customInstructions, () => { selfTriggered = false })
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
      if (inj?.text) {
        // Artifact digest (persisted in context for the model — display: false
        // keeps it out of the user-facing chat history)
        pi.sendMessage(
          {
            customType: inj.customType ?? "autocompactor.digest",
            content: inj.text,
            display: false,
          },
          { deliverAs: "nextTurn" },
        )
      }

      // Optional post-compaction next-step surfacing (advisory by default).
      // Sourced at prepare time from the rich pre-compaction transcript
      // (pending todo → last user task → last correction) and staged in
      // bridge state; recovered here on reinject. Gated by NEXTSTEP mode:
      //   "off"    — never surface (default)
      //   "advisory" — surface a ready-to-run brief the user can confirm
      //   "autonomous" — surface + inject as a model-visible task hint to continue
      // Config: top-level NEXTSTEP in config.json, overridable by
      // AUTOCOMPACTOR_NEXTSTEP env var. Off keeps the classic behavior.
      const nextStepMode =
        (process.env.AUTOCOMPACTOR_NEXTSTEP ?? "") ||
        String(inj?.nextStepMode ?? "") || "off"
      const step = ((inj?.nextStep as string | undefined) ?? "").trim()
      const stepSrc = ((inj?.nextStepSource as string | undefined) ?? "").trim()
      if (step && nextStepMode === "advisory") {
        pi.sendMessage(
          {
            customType: "autocompactor.nextstep.advisory",
            content:
              `autocompactor-nextstep: recovered next step (${stepSrc || "unknown"}) —\n` +
              `${step}\n\n` +
              `Reply "go" to dispatch via dispatch_task, or ignore.`,
            display: true,
          },
          { deliverAs: "nextTurn" },
        )
      } else if (step && nextStepMode === "autonomous") {
        // Model-visible task hint delivered next-turn; the assistant picks it
        // up and routes through dispatch_task/subagent. Not a headless auto-run
        // (that would need a separate worker extension to avoid acting in the
        // compaction event path). Kept terse so it reads as a nudge, not a command.
        pi.sendMessage(
          {
            customType: "autocompactor.nextstep.task",
            content:
              `[autocompactor-nextstep] Resuming after compaction. Next step (${stepSrc || "unknown"}):\n${step}\n\n` +
              `Verify prior state (git status) before editing; route via dispatch_task.`,
            display: true,
          },
          { deliverAs: "nextTurn" },
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
      announce(pi, ctx, msg, "info", true)
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
