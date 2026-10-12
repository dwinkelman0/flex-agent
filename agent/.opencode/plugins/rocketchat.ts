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
const rootCache = new Map<string, boolean>()

async function isRootSession(
  sessionID: string,
  ctx: unknown,
): Promise<boolean> {
  const cached = rootCache.get(sessionID)
  if (cached !== undefined) return cached
  const anyCtx = ctx as any
  const client = anyCtx.client ?? anyCtx.app?.client
  const serverUrl =
    anyCtx.serverUrl ?? anyCtx.app?.serverUrl ?? anyCtx.options?.serverUrl
  try {
    let parentID: unknown
    if (client?.session?.get) {
      const { data, error } = await client.session.get({ path: { id: sessionID } })
      if (error) throw error
      if (!data) throw new Error("empty session response")
      parentID = data.parentID
    } else if (serverUrl) {
      const url = typeof serverUrl === "string" ? serverUrl : serverUrl.href
      const resp = await fetch(`${url.replace(/\/$/, "")}/api/session/${sessionID}`)
      if (!resp.ok) throw new Error(`session fetch failed: ${resp.status}`)
      const data = await resp.json()
      parentID = data?.parentID ?? data?.data?.parentID
    } else {
      throw new Error("no opencode client or serverUrl available for root check")
    }
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
    const $ = anyCtx.$
    const peer = process.env.PEER_USERNAME ?? ""
    if (!$) {
      log("ctx.$ not available; outbound posts will be skipped")
    }
    if (!peer) {
      log("PEER_USERNAME not set; outbound posts will be skipped")
    }
    log(`plugin loaded client=${CLIENT_PY} peer=${peer || "(unset)"} opcode-url=${anyCtx.serverUrl?.href ?? anyCtx.serverUrl ?? anyCtx.options?.serverUrl ?? ""}`)

    async function postMessage(kind: string, text: string): Promise<void> {
      if (!peer || !$) return
      const preview = text.length > 80 ? text.slice(0, 80) + "…" : text
      log(`posting ${kind} to ${peer}: ${preview}`)
      try {
        await $`${PYTHON} ${CLIENT_PY} dm post ${peer} ${text}`.quiet()
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

    eventApi.subscribe(async (event: any) => {
      try {
        const t = event.type as string
        const props = (event.properties ?? {}) as Record<string, unknown>
        const envelopeId = (event as unknown as { id?: string }).id

        // Track message roles (assistant vs user) for the turn assembler.
        // A completed assistant message is forwarded immediately as its own
        // post so long-running turns stay visible instead of going silent.
        if (t === "message.updated") {
          const info = props.info as
            | {
                id?: string
                sessionID?: string
                role?: string
                time?: { completed?: number }
              }
            | undefined
          if (info?.id && info?.sessionID && info?.role) {
            trackMessage(info.sessionID, info.id, info.role)
            if (
              info.role === "assistant" &&
              info.time?.completed &&
              !postedBlocks.has(info.id) &&
              !pendingDebounce.has(info.id)
            ) {
              // `completed` can fire before the last text parts arrive, so
              // wait for a quiet period before posting. At most two rounds;
              // then post whatever we have (idle remains a backstop, and the
              // postedBlocks claim below keeps it from duplicating us).
              const sid = info.sessionID
              const mid = info.id
              const attempt = (round: number): void => {
                const snapshot = textByMessage.get(mid)
                setTimeout(() => {
                  pendingDebounce.delete(mid)
                  const current = textByMessage.get(mid)
                  if (!current || postedBlocks.has(mid)) return
                  if (current !== snapshot && round < 2) {
                    pendingDebounce.add(mid)
                    attempt(round + 1)
                    return
                  }
                  void (async () => {
                    if (!(await isRootSession(sid, ctx))) {
                      debug(`skip completed message from child session ${sid}`)
                      markPosted(mid)
                      return
                    }
                    // Rule (b): no RocketChat input this turn → the agent
                    // owns response actions via the toolkit; stay silent.
                    if (!sessionHasRcOrigin(sid)) {
                      debug(`rule (b): no RC origin for ${sid}; not forwarding`)
                      markPosted(mid)
                      return
                    }
                    // Re-check: idle may have posted this block while we awaited.
                    if (postedBlocks.has(mid)) return
                    const id = `${sid}:${mid}`
                    if (markSeen(id)) return
                    markPosted(mid)
                    log(`event message completed session=${sid} chars=${current.length}`)
                    await postMessage("turn", current)
                  })()
                }, 1500)
              }
              if (textByMessage.get(mid)) {
                pendingDebounce.add(mid)
                attempt(1)
              } else {
                debug(`completed message ${mid} has no tracked text yet`)
              }
            }
          } else {
            debug("message.updated without info.id/sessionID/role")
          }
          return
        }

        // Accumulate streamed assistant text from full part snapshots.
        if (t === "message.part.updated") {
          const part = props.part as
            | { type?: string; messageID?: string; text?: string }
            | undefined
          if (part && part.type === "text" && part.messageID && part.text) {
            trackPartText(part.messageID, part.text)
          } else {
            debug("message.part.updated non-text or missing text")
          }
          return
        }

        // Accumulate streamed assistant text from incremental deltas (2.x).
        if (t === "message.part.delta") {
          const delta = props as
            | {
                messageID?: string
                partID?: string
                field?: string
                delta?: string
              }
            | undefined
          if (delta?.field === "text" && delta.messageID && delta.delta) {
            appendPartDelta(delta.messageID, delta.delta)
          } else {
            debug("message.part.delta non-text or missing ids")
          }
          return
        }

        // permission.asked → "??" (2.x)
        if (t === "permission.asked") {
          const perm = props as {
            id?: string
            sessionID?: string
            permission?: string
            patterns?: unknown
          }
          const sid = perm.sessionID
          if (sid && !(await isRootSession(sid, ctx))) {
            debug(`skip permission from child session ${sid}`)
            return
          }
          if (sid && !sessionHasRcOrigin(sid)) {
            debug(`rule (b): no RC origin for ${sid}; not forwarding permission`)
            return
          }
          const id = perm.id ?? `perm:${String(perm.permission ?? "unknown")}`
          if (markSeen(id)) {
            debug(`dedupe skip permission id=${id}`)
            return
          }
          const patterns = Array.isArray(perm.patterns) ? perm.patterns : []
          const body = `${perm.permission ?? "permission requested"}${patterns.length ? `: ${patterns.join(", ")}` : ""}`
          log(`event permission.asked id=${id}`)
          await postMessage("permission", `?? ${body}`)
          return
        }

        // session.error → "❌"
        if (t === "session.error") {
          const errSid = props.sessionID as string | undefined
          if (errSid && !(await isRootSession(errSid, ctx))) {
            debug(`skip error from child session ${errSid}`)
            return
          }
          if (errSid && !sessionHasRcOrigin(errSid)) {
            debug(`rule (b): no RC origin for ${errSid}; not forwarding error`)
            return
          }
          const id = envelopeId ?? `err:${errSid ?? "unknown"}`
          if (markSeen(id)) {
            debug(`dedupe skip error id=${id}`)
            return
          }
          const errObj = props.error as
            | { data?: { message?: string }; message?: string }
            | undefined
          const msg =
            errObj?.data?.message ??
            (errObj ? JSON.stringify(errObj) : "session error")
          log(`event session.error id=${id}`)
          await postMessage("error", `❌ ${msg}`)
          return
        }

        // session.status idle (object shape) and legacy session.idle → summary.
        if (t === "session.status" || t === "session.idle") {
          let isIdle = t === "session.idle"
          let sessionID = props.sessionID as string | undefined
          if (t === "session.status") {
            const status = props.status as { type?: string } | undefined
            isIdle = status?.type === "idle"
            if (!isIdle) {
              debug(`ignore session.status=${status?.type}`)
              return
            }
          }
          if (!sessionID) {
            debug("idle without sessionID; skipping")
            return
          }
          if (!(await isRootSession(sessionID, ctx))) {
            debug(`skip idle from child session ${sessionID}`)
            for (const mid of orderBySession.get(sessionID) ?? []) {
              markPosted(mid)
              textByMessage.delete(mid)
            }
            return
          }
          if (!sessionHasRcOrigin(sessionID)) {
            debug(`rule (b): no RC origin for ${sessionID}; not forwarding`)
            for (const mid of orderBySession.get(sessionID) ?? []) {
              markPosted(mid)
              textByMessage.delete(mid)
            }
            return
          }
          // Fallback: forward any assistant blocks the per-message path has
          // not posted yet — each as its own message, plain text, no marker.
          const pending = unpostedBlocks(sessionID)
          if (pending.length === 0) {
            debug(`idle session=${sessionID} with nothing new; skipping`)
            return
          }
          const idleId = `${sessionID}:${pending.map((b) => b.id).join(",")}`
          if (markSeen(idleId)) {
            debug(`dedupe skip idle id=${idleId}`)
            return
          }
          log(`event idle session=${sessionID} blocks=${pending.length}`)
          for (const block of pending) {
            markPosted(block.id)
            await postMessage("turn", block.text)
          }
          // Drop consumed text so later idles stay silent.
          for (const mid of orderBySession.get(sessionID) ?? []) {
            textByMessage.delete(mid)
          }
          return
        }

        debug(`ignore event type=${t}`)
      } catch (handlerErr) {
        log("event handler error:", handlerErr)
      }
    })
  },
})

export { RocketchatPlugin }
export default RocketchatPlugin
