---
name: code-review
description: High-signal code review for this repo. Load when asked to review changes or before merging gateway/plugin/chat work.
---

## How to review here

- Scope: start with `git diff HEAD --stat`; review the diff, not the world.
- Rank: (a) security/correctness/data-loss with a concrete trigger,
  (b) broken error handling or silent failures, (c) polish last.
  Fewer, sharper findings beat a long list — no style nits, no speculative
  maybes. Every finding needs file:line + trigger + concrete fix.
- Verify API claims against installed types, not memory:
  opencode SDK at ~/.config/opencode/node_modules/@opencode-ai/sdk/,
  RocketChat behavior against developer.rocket.chat or the live
  localhost:3000 server. Docs drift (e.g. permission.asked never existed
  in the 1.18 SDK) — check, don't assume.
- Stack-specific traps: opencode event shapes (nested `{event}`,
  parts carry no role, `session.status.status` is an object);
  RocketChat string params (`"false"` not `False`), DDP deprecations
  (methods out, subscriptions fine), rooms.upload gone since 8.0
  (rooms.media + mediaConfirm); shell `set -u` + traps, `uv run`
  wrapper PIDs vs python child PIDs, backgrounded shells ignoring SIGINT.
- Reviewers run fresh-eyes by default: brief them with paths + scope
  only, no session history. Read-only: never edit, return findings.
