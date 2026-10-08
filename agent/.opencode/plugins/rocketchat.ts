/**
 * rocketchat.ts — opencode plugin that bridges agent turns to Rocket.Chat.
 *
 * Outer-loop contract (runtime):
 *   The outer loop owns the long poll: it calls `chat/client.py dm listen`
 *   on startup and dispatches each inbound message to the agent as a prompt
 *   immediately. The agent only ever calls `dm list-recent` itself when it
 *   needs to reconcile (see AGENTS.md escape hatch). This plugin only
 *   *forwards* outbound agent events back to the DM room; it never listens.
 *
 * Hooks (opencode 1.18 bus events):
 *   permission.updated → "?? <title>"      (deduped by permission id)
 *   session.error      → "❌ <error>"     (deduped by error id)
 *   message.updated    → completed assistant messages are forwarded
 *                         immediately, one Rocket.Chat message per text
 *                         block, so long turns stay visible as they land
 *   session.idle       → fallback: forwards any assistant blocks not yet
 *                         posted (each its own message, plain text, no marker)
 *   session.status     → same as session.idle when status.type === "idle"
 *
 * All outbound posts shell out to `python <repo>/chat/client.py dm post`
 * via the ctx `$`. Repo root is resolved relative to this plugin file, not
 * hardcoded to any home directory.
 */

import { type Plugin } from "@opencode-ai/plugin"
import * as path from "node:path"

// Repo root = dirname(plugin) / ../../..  (agent/.opencode/plugins -> agent -> repo)
const REPO_ROOT = path.resolve(import.meta.dir, "../../..")
const CLIENT_PY = path.join(REPO_ROOT, "chat", "client.py")

// Prefer the project's venv python (bare `python` is often absent on PATH);
// fall back to python3, then python.
import { existsSync } from "node:fs"

const VENV_PY = path.join(REPO_ROOT, "chat", ".venv", "bin", "python")
const PYTHON = existsSync(VENV_PY) ? VENV_PY : "python3"

// The opencode server this plugin runs inside (loopback). Only root
// sessions (no parentID) are forwarded — child/subagent sessions post their
// internal coordination chatter (DONE summaries, verifier JSON) here
// otherwise, which does not belong in the chat window.
const OPENCODE_PORT = process.env.OPENCODE_PORT ?? "4096"
const rootCache = new Map<string, boolean>()

async function isRootSession(sessionID: string): Promise<boolean> {
  const cached = rootCache.get(sessionID)
  if (cached !== undefined) return cached
  try {
    const res = await fetch(`http://127.0.0.1:${OPENCODE_PORT}/session/${sessionID}`)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const body = (await res.json()) as { parentID?: string | null }
    const root = body.parentID == null
    rootCache.set(sessionID, root)
    if (rootCache.size > MAX_SEEN) {
      const first = rootCache.keys().next()
      if (!first.done) rootCache.delete(first.value)
    }
    return root
  } catch (err) {
    // Fail closed: an unverifiable session is treated as a child and its
    // output stays out of the chat window. A failed loopback lookup is loud
    // (always logged, not debug-gated) so a misconfigured OPENCODE_PORT
    // cannot silently swallow user-visible replies — if you see this line
    // and expected a post, check the port.
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
// `message.part.updated`. Keyed by session so concurrent sessions don't mix.
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
export const RocketchatPlugin: Plugin = async ({ $ }) => {
  const peer = process.env.PEER_USERNAME ?? ""
  if (!peer) {
    log("PEER_USERNAME not set; outbound posts will be skipped")
  }
  log(`plugin loaded client=${CLIENT_PY} peer=${peer || "(unset)"} opcode-port=${OPENCODE_PORT}`)

  async function postMessage(kind: string, text: string): Promise<void> {
    if (!peer) return
    const preview = text.length > 80 ? text.slice(0, 80) + "…" : text
    log(`posting ${kind} to ${peer}: ${preview}`)
    try {
      await $`${PYTHON} ${CLIENT_PY} dm post ${peer} ${text}`.quiet()
      log(`${kind} posted ok`)
    } catch (err) {
      log(`${kind} post FAILED:`, err)
    }
  }

  return {
    event: async ({ event }) => {
      const t = event.type
      const props = (event.properties ?? {}) as Record<string, unknown>

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
                  if (!(await isRootSession(sid))) {
                    debug(`skip completed message from child session ${sid}`)
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

      // Accumulate streamed assistant text.
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

      // permission.updated → "??" (1.18 has no permission.asked event)
      if (t === "permission.updated") {
        const sid = (props as { sessionID?: string }).sessionID
        if (sid && !(await isRootSession(sid))) {
          debug(`skip permission from child session ${sid}`)
          return
        }
        const perm = props as unknown as {
          id?: string
          title?: string
          pattern?: string | string[]
        }
        const id = perm.id ?? `perm:${String(perm.pattern ?? "unknown")}`
        if (markSeen(id)) {
          debug(`dedupe skip permission id=${id}`)
          return
        }
        const pattern = Array.isArray(perm.pattern)
          ? perm.pattern.join(",")
          : (perm.pattern ?? "permission requested")
        const body = perm.title ?? pattern
        log(`event permission.updated id=${id}`)
        await postMessage("permission", `?? ${body}`)
        return
      }

      // session.error → "❌"
      if (t === "session.error") {
        const errSid = props.sessionID as string | undefined
        if (errSid && !(await isRootSession(errSid))) {
          debug(`skip error from child session ${errSid}`)
          return
        }
        const id =
          (props.errorID as string | undefined) ??
          (props.id as string | undefined) ??
          `err:${String(props.message ?? "unknown")}`
        if (markSeen(id)) {
          debug(`dedupe skip error id=${id}`)
          return
        }
        const msg =
          (props.message as string | undefined) ??
          (props.error as string | undefined) ??
          "session error"
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
        if (!(await isRootSession(sessionID))) {
          debug(`skip idle from child session ${sessionID}`)
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
        const id = `${sessionID}:${pending.map((b) => b.id).join(",")}`
        if (markSeen(id)) {
          debug(`dedupe skip idle id=${id}`)
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
    },
  }
}

export default RocketchatPlugin
