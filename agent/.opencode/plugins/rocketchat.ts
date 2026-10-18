/**
 * rocketchat.ts — opencode plugin that bridges agent turns to Rocket.Chat.
 *
 * Outer-loop contract (runtime):
 *   The outer loop owns inbound: it watches subscribed channels (Rocket.Chat
 *   DM push, Gitea issue polls) and dispatches each hit as a prompt
 *   immediately, with a provenance envelope header. The agent only ever
 *   calls `dm list-recent` itself when it needs to reconcile (see AGENTS.md
 *   escape hatch).
 *
 * Routing policy (see agent/turn-origins.json, written by the gateway):
 *   (a) turn includes Rocket.Chat input → forward all emitted text to the
 *       DM (may also use other channels via the toolkit — not precluded).
 *   (b) otherwise → stay silent; the agent owns zero-or-more response
 *       actions via the gateway toolkit (`issues.py reply`,
 *       `channel.py post`).
 * Only root sessions ever forward (child/subagent chatter stays internal).
 *
 * Hooks (opencode 2.x bus events):
 *   permission.asked  → "?? <permission>: <patterns>"  (deduped by id)
 *   session.error     → "❌ <message>"                (deduped by envelope id)
 *   message.updated   → completed assistant messages are forwarded
 *                        immediately, one Rocket.Chat message per text
 *                        block, so long turns stay visible as they land
 *   message.part.delta → streaming text appended to the matching block
 *   session.idle      → fallback: forwards any assistant blocks not yet
 *                        posted (each its own message, plain text, no marker)
 *   session.status    → same as session.idle when status.type === "idle"
 *
 * All outbound posts shell out to `python <repo>/chat/client.py dm post`
 * via the ctx `$`. Repo root is resolved relative to this plugin file, not
 * hardcoded to any home directory.
 */

import { define } from "@opencode-ai/plugin/v2/promise"
import { execFile } from "node:child_process"
import * as path from "node:path"
import { fileURLToPath } from "node:url"

// Repo root = dirname(plugin) / ../../..  (agent/.opencode/plugins -> agent -> repo)
const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..")
const CLIENT_PY = path.join(REPO_ROOT, "chat", "client.py")

// Prefer the project's venv python (bare `python` is often absent on PATH);
// fall back to python3, then python.
import { existsSync, readFileSync } from "node:fs"

// Turn origins, written by the gateway on every dispatch
// (agent/turn-origins.json): {sessionID: {rc: bool, channels: [...]}}.
// Rule (a): a turn with at least one RocketChat input is forwarded here.
// Rule (b): anything else is the agent's job via the gateway toolkit
// (issues.py reply, channel.py post) — the plugin stays silent.
const ORIGINS_PATH = path.join(REPO_ROOT, "agent", "turn-origins.json")

function sessionHasRcOrigin(sessionID: string): boolean {
  try {
    if (!existsSync(ORIGINS_PATH)) return true // no gateway yet: old behavior
    const raw = readFileSync(ORIGINS_PATH, "utf-8")
    const table = JSON.parse(raw) as Record<string, { rc?: unknown }>
    const entry = table[sessionID]
    if (entry == null) return true // not gateway-dispatched: old behavior
    return entry.rc === true
  } catch (err) {
    debug(`origins lookup failed for ${sessionID}:`, err)
    return true
  }
}

const VENV_PY = path.join(REPO_ROOT, "chat", ".venv", "bin", "python")
const PYTHON = existsSync(VENV_PY) ? VENV_PY : "python3"

// The opencode server this plugin runs inside. Only root sessions
// (no parentID) are forwarded — child/subagent sessions post their internal
// coordination chatter (DONE summaries, verifier JSON) here otherwise, which
// does not belong in the chat window.
//
// The plugin talks to the local serve API directly (Basic auth with the
// shared server password): ctx-provided clients are absent in serve 2.x.
const OPENCODE_PORT = process.env.OPENCODE_PORT ?? "4096"
const OPENCODE_PASSWORD = process.env.OPENCODE_SERVER_PASSWORD ?? ""
const serveBase = (): string => `http://127.0.0.1:${OPENCODE_PORT}`
function serveHeaders(): Record<string, string> {
  if (!OPENCODE_PASSWORD) return {}
  return {
    Authorization: `Basic ${Buffer.from(`opencode:${OPENCODE_PASSWORD}`).toString("base64")}`,
  }
}

const rootCache = new Map<string, boolean>()

async function isRootSession(sessionID: string, _ctx?: unknown): Promise<boolean> {
  const cached = rootCache.get(sessionID)
  if (cached !== undefined) return cached
  void _ctx
  try {
    const resp = await fetch(`${serveBase()}/api/session/${sessionID}`, {
      headers: serveHeaders(),
    })
    if (!resp.ok) throw new Error(`session fetch failed: ${resp.status}`)
    const data = (await resp.json()) as any
    const parentID = data?.data?.parentID ?? data?.parentID
    const root = parentID == null
    rootCache.set(sessionID, root)
    if (rootCache.size > MAX_SEEN) {
      const first = rootCache.keys().next()
      if (!first.done) rootCache.delete(first.value)
    }
    return root
  } catch (err) {
    // Fail closed: an unverifiable session is treated as a child and its
    // output stays out of the chat window. A failed session lookup is loud
    // (always logged, not debug-gated) so auth or discovery issues cannot
    // silently swallow user-visible replies.
    log(`session parent lookup FAILED for ${sessionID}; suppressing post:`, err)
    return false
  }
}

// --- Logging ----------------------------------------------------------------
// All state transitions go to stderr (console.error) so they appear live when
// `opencode serve` runs with --print-logs. Set ROCKETCHAT_DEBUG=1 to also log
// every ignored/non-matching event.
const DEBUG = process.env.ROCKETCHAT_DEBUG === "1"

function log(...args: unknown[]): void {
  console.error(`[${new Date().toISOString()} rocketchat]`, ...args)
}

function debug(...args: unknown[]): void {
  if (DEBUG) console.error(`[${new Date().toISOString()} rocketchat:debug]`, ...args)
}

// --- Dedupe -----------------------------------------------------------------
// Module-level Set of ids we have already forwarded. Capped so long-lived
// agent processes don't leak memory.
const MAX_SEEN = 200
const seen = new Set<string>()

function markSeen(id: string): boolean {
  if (!id) return false
  if (seen.has(id)) return true
  seen.add(id)
  if (seen.size > MAX_SEEN) {
    // Set preserves insertion order; drop the oldest half.
    const arr = [...seen]
    const keep = arr.slice(arr.length - Math.floor(MAX_SEEN / 2))
    seen.clear()
    for (const k of keep) seen.add(k)
  }
  return false
}

// NOTE: no truncation here by design. Blocks are forwarded whole; concision
// is the agent's job, enforced via agent/AGENTS.md (prompt layer), not by
// silently cutting text mid-word in the transport.

// --- Assistant-text tracker -------------------------------------------------
// Text parts stream without a role field, so we record the role per message
// from `message.updated` and accumulate text per message from
// `message.part.updated` and `message.part.delta`. Keyed by session so
// concurrent sessions don't mix.
const roleByMessage = new Map<string, string>()
const textByMessage = new Map<string, string>()
const orderBySession = new Map<string, string[]>()

function trackMessage(sessionID: string, messageID: string, role: string): void {
  roleByMessage.set(messageID, role)
  const order = orderBySession.get(sessionID) ?? []
  if (!order.includes(messageID)) {
    order.push(messageID)
    orderBySession.set(sessionID, order.slice(-50))
  }
}

function trackPartText(messageID: string, text: string): void {
  // TextPart.text carries the full current text; latest wins.
  textByMessage.set(messageID, text)
}

function appendPartDelta(messageID: string, delta: string): void {
  // message.part.delta carries incremental text; append it.
  textByMessage.set(messageID, (textByMessage.get(messageID) ?? "") + delta)
}

// Per-block posting state: messageIDs already forwarded as their own
// Rocket.Chat message. Idle only forwards blocks missing from this set,
// so completed messages post progressively and idle never duplicates.
const postedBlocks = new Set<string>()

// MessageIDs with a debounce timer in flight (see message.updated). Guards
// against scheduling duplicate timers for repeated completion events.
const pendingDebounce = new Set<string>()

function markPosted(messageID: string): void {
  postedBlocks.add(messageID)
  if (postedBlocks.size > MAX_SEEN) {
    const arr = [...postedBlocks]
    for (const k of arr.slice(0, arr.length - MAX_SEEN)) postedBlocks.delete(k)
  }
}

/** Assistant text blocks in session order that have not been posted yet. */
function unpostedBlocks(sessionID: string): { id: string; text: string }[] {
  const order = orderBySession.get(sessionID) ?? []
  const out: { id: string; text: string }[] = []
  for (const mid of order) {
    if (roleByMessage.get(mid) !== "assistant") continue
    if (postedBlocks.has(mid)) continue
    const t = textByMessage.get(mid)
    if (t) out.push({ id: mid, text: t })
  }
  return out
}

// --- Plugin entry -----------------------------------------------------------
const RocketchatPlugin = define({
  id: "rocketchat",
  setup(ctx) {
    const anyCtx = ctx as any
    const peer =
      process.env.ROCKETCHAT_PEER_USERNAME ??
      process.env.PEER_USERNAME ??
      ""
    if (!peer) {
      log("ROCKETCHAT_PEER_USERNAME (or legacy PEER_USERNAME) not set; outbound posts will be skipped")
    }
    log(`plugin loaded client=${CLIENT_PY} peer=${peer || "(unset)"} opcode-url=${anyCtx.serverUrl?.href ?? anyCtx.serverUrl ?? anyCtx.options?.serverUrl ?? ""}`)

    async function postMessage(kind: string, text: string): Promise<void> {
      if (!peer) return
      const preview = text.length > 80 ? text.slice(0, 80) + "…" : text
      log(`posting ${kind} to ${peer}: ${preview}`)
      try {
        await new Promise<void>((resolve, reject) => {
          execFile(
            PYTHON,
            [CLIENT_PY, "dm", "post", peer, text],
            (error, stdout, stderr) => {
              if (error) reject(error)
              else resolve()
            }
          )
        })
        log(`${kind} posted ok`)
      } catch (err) {
        log(`${kind} post FAILED:`, err)
      }
    }

    const eventApi = anyCtx.event
    if (!eventApi?.subscribe) {
      log("ctx.event.subscribe not available; no events will be handled")
      return
    }

    // Serve 2.x delivers bus events through an async iterator, not a
    // callback, and envelopes are flat ({type, data}) with no `properties`
    // wrapper. On each completed turn, refetch the session's messages from
    // the local API and forward assistant blocks not yet posted.
    const inFlight = new Set<string>()
    async function forwardCompletedTurn(sessionID: string): Promise<void> {
      if (inFlight.has(sessionID)) return
      inFlight.add(sessionID)
      try {
        if (!(await isRootSession(sessionID))) {
          debug(`skip completed turn from child session ${sessionID}`)
          return
        }
        if (!sessionHasRcOrigin(sessionID)) {
          debug(`rule (b): no RC origin for ${sessionID}; not forwarding`)
          return
        }
        const resp = await fetch(
          `${serveBase()}/api/session/${sessionID}/message?limit=20`,
          { headers: serveHeaders() },
        )
        if (!resp.ok) throw new Error(`messages fetch failed: ${resp.status}`)
        const data = (await resp.json()) as { data?: Array<any> }
        for (const m of data.data ?? []) {
          const mid = m?.id
          if (typeof mid !== "string") continue
          const role =
            m?.type === "assistant" ? "assistant" : m?.type === "user" ? "user" : null
          if (role === null) continue
          trackMessage(sessionID, mid, role)
          if (role === "assistant") {
            const text = (m?.content ?? [])
              .filter((c: any) => c?.type === "text" && typeof c?.text === "string")
              .map((c: any) => c.text as string)
              .join("")
            if (text) textByMessage.set(mid, text)
          }
        }
        const pending = unpostedBlocks(sessionID)
        if (pending.length === 0) {
          debug(`completed turn session=${sessionID} with nothing new; skipping`)
          return
        }
        log(`event turn session=${sessionID} blocks=${pending.length}`)
        for (const block of pending) {
          markPosted(block.id)
          await postMessage("turn", block.text)
        }
      } catch (err) {
        log(`forward turn FAILED for ${sessionID}:`, err)
      } finally {
        inFlight.delete(sessionID)
        for (const mid of orderBySession.get(sessionID) ?? []) {
          textByMessage.delete(mid)
        }
      }
    }

    void (async () => {
      try {
        for await (const event of eventApi.subscribe({}) as AsyncIterable<any>) {
          const t = (event as any)?.type
          if (t === "session.execution.succeeded") {
            const sid = (event as any)?.data?.sessionID
            if (typeof sid === "string") {
              await forwardCompletedTurn(sid)
            } else {
              debug("execution.succeeded without data.sessionID; skipping")
            }
          } else {
            debug(`ignore event type=${t}`)
          }
        }
      } catch (err) {
        log("bus subscribe failed:", err)
      }
    })()
  },
})

export { RocketchatPlugin }
export default RocketchatPlugin
