# agent/

Runtime copy target for containers running the autonomous agent.

This directory is what gets baked into agent images (or bind-mounted into
dev containers). It is intentionally **separate from the repo root** so that
dev-time `opencode` invocations from the repo root never load the agent's
plugin or pick up the agent's instructions.

## Why the nested `.opencode/` lives here

opencode walks up from its working directory to find a `.opencode/` config
and any plugins it declares. If the plugin lived at the repo root, every
developer running `opencode` from `flex-agent/` would load the Rocket.Chat
bridge and start forwarding events to whatever `PEER_USERNAME` happened to
be set — including during unrelated coding sessions.

Placing `.opencode/` under `agent/` means:

- Dev-time opencode at the repo root: no plugin, no auto-forwarding.
- Agent-container opencode started with cwd `agent/`: plugin loads and
  the agent is wired into Rocket.Chat as designed.

## Outer-loop contract

The outer loop is the agent's sole interface to Rocket.Chat. Its contract
with the agent (and with this plugin) is:

1. **Listen on startup.** Run `chat/client.py dm listen <peer>` and deliver
   each returned message to the agent as a prompt immediately.
2. **Immediate dispatch.** Every inbound peer message becomes a prompt; the
   agent never polls for mail itself.
3. **Spontaneous batching only.** The agent may call
   `chat/client.py dm list-recent <peer>` to reconcile context, but only
   when it is confused. It never uses `list-recent` as a primary transport.

Outbound replies are auto-forwarded by the plugin (`agent/.opencode/plugins/rocketchat.ts`);
the agent writes normal assistant turns and the plugin handles `dm post`.

## Layout

```
agent/
├── .opencode/
│   └── plugins/
│       └── rocketchat.ts   # event hook: permission.asked / session.error / session.status
├── AGENTS.md               # instructions loaded by opencode when cwd=agent/
└── README.md               # this file
```
