// autocompactor Pi extension — boundary-aware compaction advisor.
//
// Logic-minimal shim: ALL analysis lives in pi_bridge.py (one brain with the
// Claude Code hooks). This file only wires Pi events to the bridge and never
// lets a failure reach Pi — every handler is fully try/caught.
//
// Modes (AUTOCOMPACTOR_PI_MODE): "advise" (default — notify only) | "actuate"
// (self-trigger ctx.compact with bridge-built customInstructions).
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

// Zero-spawn pre-gate thresholds (mirror pi_bridge defaults; the bridge
// re-checks with full signal analysis — this gate only avoids spawns).
const SOFT_PCT = num("AUTOCOMPACTOR_PI_SOFT_PCT", num("AUTOCOMPACTOR_SOFT_PCT", 0.40))
const MIN_SAVINGS = num("AUTOCOMPACTOR_PI_MIN_SAVINGS", num("AUTOCOMPACTOR_MIN_SAVINGS", 30_000))
const POST_FLOOR = num("AUTOCOMPACTOR_PI_POST_FLOOR", num("AUTOCOMPACTOR_POST_FLOOR", 70_000))
const COOLDOWN = num("AUTOCOMPACTOR_PI_COOLDOWN", num("AUTOCOMPACTOR_COOLDOWN", 25_000))
const RESERVE_FALLBACK = num("AUTOCOMPACTOR_PI_RESERVE", 40_000)

function num(name: string, dflt: number): number {
  const v = parseFloat(process.env[name] ?? "")
  return Number.isFinite(v) ? v : dflt
}

function mode(): "advise" | "actuate" {
  return process.env.AUTOCOMPACTOR_PI_MODE === "actuate" ? "actuate" : "advise"
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

async function bridge(
  pi: ExtensionAPI,
  ctx: ExtensionContext,
  sub: string,
  extraArgs: string[] = [],
): Promise<any | null> {
  try {
    const session = ctx.sessionManager.getSessionFile()
    const args = [BRIDGE, sub, ...extraArgs, "--cwd", ctx.cwd]
    if (session) args.push("--session", session)
    const res = await pi.exec("python3", args, {
      timeout: EXEC_TIMEOUT_MS,
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
  let selfTriggered = false // reentrancy flag for actuate mode
  let compactionPreTokens = 0 // captured in session_before_compact for post summary

  // agent_end: the boundary moment. Zero-spawn pre-gate, then bridge evaluate.
  pi.on("agent_end", async (_event, ctx) => {
    try {
      const usage = ctx.getContextUsage()
      if (!usage || usage.tokens === null) return // unknown right after compaction
      const window = usage.contextWindow - RESERVE_FALLBACK
      if (window <= 0) return
      // Pre-gate: below SOFT or nothing worth reclaiming or cooling down -> no spawn.
      if (usage.tokens / window < SOFT_PCT) return
      if (usage.tokens - POST_FLOOR < MIN_SAVINGS) return
      if (usage.tokens - lastRecTokens < COOLDOWN) return

      const verdict = await bridge(pi, ctx, "evaluate", [
        "--tokens", String(usage.tokens),
        "--context-window", String(usage.contextWindow),
      ])
      if (!verdict?.recommend) return
      lastRecTokens = usage.tokens

      // Build a criteria-aware message from the verdict reason (which already
      // includes occupancy + any gating signals from the bridge) or fall back
      // to the raw token count.
      const reason = verdict.reason ?? `${usage.tokens?.toLocaleString() ?? "?"} tokens`

      if (mode() === "actuate" && !selfTriggered) {
        // Set selfTriggered BEFORE the prepare call to block overlapping
        // agent_end handlers from both triggering compaction.
        selfTriggered = true
        compactionPreTokens = usage.tokens
        if (ctx.hasUI) {
          ctx.ui.notify(
            `autocompactor: criteria met — ${reason}; running compaction now.`,
            "info",
          )
        }
        const prep = await bridge(pi, ctx, "prepare", ["--trigger", "actuate"])
        ctx.compact({
          customInstructions: prep?.customInstructions,
          onComplete: () => { selfTriggered = false },
          onError: () => { selfTriggered = false },
        })
      } else {
        // advise mode OR actuate mode with reentrancy guard (compaction in
        // flight): visible UI notification only. Do not queue visible
        // custom messages with nextTurn: they persist into the session and
        // can surface stale/duplicated advice on the next user prompt.
        if (ctx.hasUI) {
          const modeTag = mode() === "actuate"
            ? "compaction in progress"
            : "advise mode"
          ctx.ui.notify(
            `autocompactor: criteria met — ${reason} (${modeTag}).`,
            "warning",
          )
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
      const usage = ctx.getContextUsage()
      // Capture pre-compaction tokens for the post-compaction summary.
      if (usage && usage.tokens != null) {
        compactionPreTokens = usage.tokens
      }
      // Fire-and-forget prepare for backup + artifacts + founding-goal.
      // Non-intercept: do NOT await — native compaction proceeds immediately.
      if (!interceptEnabled() || selfTriggered) {
        void bridge(pi, ctx, "prepare", ["--trigger", "native"])
        return
      }
      // Intercept mode: cancel native compaction and re-trigger with
      // our customInstructions. SelfTriggered is already true here.
      const prep = await bridge(pi, ctx, "prepare", ["--trigger", "native"])
      if (!prep?.customInstructions) return
      ctx.compact({
        customInstructions: prep.customInstructions,
        onComplete: () => { selfTriggered = false },
        onError: () => { selfTriggered = false },
      })
      return { cancel: true }
    } catch {
      /* fall through to native compaction untouched */
    }
  })

  // session_compact: re-inject the artifact digest as a persisted one-shot,
  // and show a clear post-compaction summary to the user.
  pi.on("session_compact", async (event, ctx) => {
    try {
      const inj = await bridge(pi, ctx, "reinject")
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

      // Visible post-compaction status belongs in the UI, not as a queued
      // session message. tokensBefore from the compaction entry survives
      // runtime reloads better than closure state.
      const usage = ctx.getContextUsage()
      const preTokens = event?.compactionEntry?.tokensBefore ?? compactionPreTokens
      const postTokens = usage?.tokens
      if (ctx.hasUI) {
        let msg = "autocompactor: compaction completed."
        if (preTokens > 0 && postTokens != null && postTokens > 0) {
          const reclaimed = Math.max(preTokens - postTokens, 0)
          const window = (usage?.contextWindow ?? 0) - RESERVE_FALLBACK
          const postOcc = window > 0 ? ` (${((postTokens / window) * 100).toFixed(0)}%)` : ""
          msg = `autocompactor: compaction completed — context ${preTokens.toLocaleString()} → ${postTokens.toLocaleString()} tokens${postOcc}; reclaimed ~${reclaimed.toLocaleString()} tokens.`
        } else if (preTokens > 0) {
          msg = `autocompactor: compaction completed — before context was ${preTokens.toLocaleString()} tokens; current usage will refresh on the next turn.`
        } else if (postTokens != null && postTokens > 0) {
          msg = `autocompactor: compaction completed — current context is ${postTokens.toLocaleString()} tokens.`
        }
        ctx.ui.notify(msg, "info")
      }
      compactionPreTokens = 0 // reset for next compaction
    } catch {
      /* never break Pi */
    }
  })
}
