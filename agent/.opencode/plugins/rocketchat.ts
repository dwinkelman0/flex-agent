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
 *   session.idle       → turn summary      (last assistant text, ~800 chars)
 *   session.status     → same, when status.type === "idle"
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

// --- Truncation -------------------------------------------------------------
const SUMMARY_CAP = 800

function truncate(s: string, cap = SUMMARY_CAP): string {
  if (s.length <= cap) return s
  return s.slice(0, cap).trimEnd() + "…"
}

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

function takeAssistantText(sessionID: string): { text: string; lastID: string } {
  const order = orderBySession.get(sessionID) ?? []
  const chunks: string[] = []
  let lastID = ""
  for (const mid of order) {
    if (roleByMessage.get(mid) !== "assistant") continue
    const t = textByMessage.get(mid)
    if (t) {
      chunks.push(t)
      lastID = mid
    }
  }
  // Consume so a second idle for the same turn emits nothing.
  for (const mid of order) {
    textByMessage.delete(mid)
  }
  return { text: chunks.join("\n\n"), lastID }
}

// --- Plugin entry -----------------------------------------------------------
export const RocketchatPlugin: Plugin = async ({ $ }) => {
  const peer = process.env.PEER_USERNAME ?? ""
  if (!peer) {
    log("PEER_USERNAME not set; outbound posts will be skipped")
  }
  log(`plugin loaded client=${CLIENT_PY} peer=${peer || "(unset)"}`)

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
      if (t === "message.updated") {
        const info = props.info as
          | { id?: string; sessionID?: string; role?: string }
          | undefined
        if (info?.id && info?.sessionID && info?.role) {
          trackMessage(info.sessionID, info.id, info.role)
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
        const { text, lastID } = takeAssistantText(sessionID)
        const id = `${sessionID}:${lastID || Date.now()}`
        if (markSeen(id)) {
          debug(`dedupe skip idle id=${id}`)
          return
        }
        if (!text) {
          debug(`idle session=${sessionID} with no assistant text; nothing to forward`)
          return
        }
        const summary = truncate(text)
        log(`event idle session=${sessionID} chars=${summary.length}`)
        await postMessage("turn", `💬 ${summary}`)
        return
      }

      debug(`ignore event type=${t}`)
    },
  }
}

export default RocketchatPlugin
