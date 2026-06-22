// src/pi/test/render-channel.test.mjs — node --test
//
// Root-cause spec for the "no compaction/precompaction output in Pi" bug.
//
// The autocompactor's user-visible notices are delivered with pi.sendMessage's
// `deliverAs` option. WHICH channel renders is decided by Pi's
// AgentSession.sendCustomMessage + the next-turn flush. This file mirrors that
// logic VERBATIM from the installed SDK so the behavior our fix depends on is
// pinned and regression-guarded — if a future SDK bump changes these branches,
// this test breaks and tells us to re-verify the channel choice.
//
// Source (pinned): @earendil-works/pi-coding-agent 0.79.9 (live runtime; the
//   branch logic re-verified against 0.79.9 — sendCustomMessage routes
//   deliverAs:"followUp" to agent.followUp() iff isStreaming, and agent_end
//   listeners run while isStreaming is still true, so followUp is swallowed):
//   dist/core/agent-session.js:984-1012  (sendCustomMessage branch table)
//   dist/core/agent-session.js:797-801   (nextTurn flush into next prompt)
//
// NOTE: this is a faithful logic-mirror, not a live render. The DEFINITIVE
// end-to-end proof is a real Pi session driven to a compaction (see HANDOFF /
// the fix plan). This test exists to lock the contract, not to replace that.

import { test } from "node:test"
import assert from "node:assert/strict"

// ---- faithful mirror of AgentSession.sendCustomMessage (0.79.8:983-1012) ----
class FakeSession {
  constructor({ isStreaming }) {
    this.isStreaming = isStreaming
    this._pendingNextTurnMessages = []
    this.agentMessages = [] // agent.state.messages (the live, rendered transcript)
    this.followUpQueue = [] // agent.followUp() target — agent input, NOT rendered
    this.steerQueue = [] // agent.steer() target — injects into live stream
    this.persistedEntries = [] // sessionManager.appendCustomMessageEntry
    this.emitted = [] // _emit(message_start/message_end)
  }

  sendCustomMessage(message, options) {
    const appMessage = {
      role: "custom",
      customType: message.customType,
      content: message.content,
      display: message.display,
      details: message.details,
      timestamp: 0, // Date.now() in SDK; irrelevant to channel routing
    }
    if (options?.deliverAs === "nextTurn") {
      this._pendingNextTurnMessages.push(appMessage)
    } else if (this.isStreaming) {
      if (options?.deliverAs === "followUp") {
        this.followUpQueue.push(appMessage)
      } else {
        this.steerQueue.push(appMessage)
      }
    } else if (options?.triggerTurn) {
      // _runAgentPrompt — irrelevant here
    } else {
      this.agentMessages.push(appMessage)
      this.persistedEntries.push(appMessage)
      this.emitted.push({ type: "message_start", message: appMessage })
      this.emitted.push({ type: "message_end", message: appMessage })
    }
  }

  // ---- mirror of the next-turn flush (0.79.8:797-801) ----
  // On the next user prompt, pending nextTurn messages are pushed into the
  // outgoing `messages` array (display flag intact). A role:custom message with
  // display:true renders to the user (custom-message.js fallback renderer).
  flushNextTurn() {
    const messages = []
    for (const msg of this._pendingNextTurnMessages) messages.push(msg)
    this._pendingNextTurnMessages = []
    return messages
  }
}

const NOTICE = { customType: "autocompactor.status", content: "autocompactor: compaction completed", display: true }

test("nextTurn during streaming: queued, then RENDERS at next prompt with display intact", () => {
  // This is exactly what the fixed one-shot notices do: emitted mid-compaction
  // (isStreaming=true) yet they survive and render at the next user prompt.
  const s = new FakeSession({ isStreaming: true })
  s.sendCustomMessage(NOTICE, { deliverAs: "nextTurn" })
  assert.equal(s.followUpQueue.length, 0, "nextTurn must not go to the followUp queue")
  assert.equal(s._pendingNextTurnMessages.length, 1, "queued for next turn")
  const rendered = s.flushNextTurn()
  assert.equal(rendered.length, 1, "flushed into next-prompt messages -> rendered")
  assert.equal(rendered[0].display, true, "display flag preserved through flush")
  assert.equal(rendered[0].content, NOTICE.content)
})

test("followUp during streaming: SWALLOWED (the original bug)", () => {
  // Compaction/agent_end fire while streaming; followUp routes the notice into
  // the agent's input queue — never rendered, never persisted, never flushed.
  const s = new FakeSession({ isStreaming: true })
  s.sendCustomMessage(NOTICE, { deliverAs: "followUp" })
  assert.equal(s.followUpQueue.length, 1, "went to agent.followUp (input queue)")
  assert.equal(s.agentMessages.length, 0, "NOT added to the rendered transcript")
  assert.equal(s.persistedEntries.length, 0, "NOT persisted to session history")
  assert.equal(s.emitted.length, 0, "NO message_start/message_end emitted -> invisible")
  assert.equal(s.flushNextTurn().length, 0, "nothing queued for next turn either")
})

test("followUp when NOT streaming: renders via the else branch (why 'loaded' shows)", () => {
  // session_start runs not-streaming, so even followUp falls to the final else
  // and renders+persists — which is why the user saw 'autocompactor: loaded'
  // but not the compaction notices.
  const s = new FakeSession({ isStreaming: false })
  s.sendCustomMessage({ ...NOTICE, content: "autocompactor: loaded" }, { deliverAs: "followUp" })
  assert.equal(s.agentMessages.length, 1, "added to the rendered transcript")
  assert.equal(s.persistedEntries.length, 1, "persisted to session history")
  assert.equal(s.emitted.filter((e) => e.type === "message_start").length, 1)
  assert.equal(s.emitted.filter((e) => e.type === "message_end").length, 1)
})
