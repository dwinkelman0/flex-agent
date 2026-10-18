# Tech Debt — in-repo plane

Feature work is tracked in GitHub. This file tracks only work that is
complex, open-ended, and off the critical path: proper fixes we are
deliberately deferring, each with a checkable firing condition.

Rules:

- A trigger must be a true/false condition: an observation, a metric, or
  a complexity threshold. Never a vibe ("when we feel like it").
- An item only qualifies if the proper fix costs MORE than the shortcut
  already taken plus whatever instrumentation validates the trigger.
  Otherwise it is not debt — just fix it now.
- Items live in QUEUED (dormant) or PENDING (trigger fired, unscheduled).
  Either list is fixable at any time. For PENDING items the trigger has
  already fired, so stating it is optional.
- Resolving an item means deleting its entry in the same commit as the fix.
- `TECH_DEBT.md` updates commit together with code, never as standalone
  commits.
- Small risks that could manifestly misbehave do NOT belong here: fix them
  now, or mark the call site with a 1-line `NOTE(<trigger>): …` comment
  (select few only — a wishlist is not a tracker).

## QUEUED

### Modularize rocketchat interface
- **Shortcut**: rocketchat communication is a bit ad hoc and only tested
  "in production" currently (`chat/lib/protocol.py`, `chat/lib/realtime.py`,
  `chat/client.py` each own a slice of the wire protocol).
- **Proper Solution**: one consolidated library for rocketchat
  communication, complete with "loopback" tests (e.g. post a message in a
  "debug" channel and validate that the message was received).
- **Trigger**: next time flaky rocketchat communication is the culprit
  for a bug.

### Standardize environment variables and their provenance
- **Shortcut**: `.env` / `.agent_env` files plus ad-hoc variables
  (`PEER_USERNAME`, `OPENCODE_PORT`, …) with implicit precedence
  scattered across `chat/lib/config.py` and scripts.
- **Proper Solution**: documented env schema with provenance (file vs
  env vs default) surfaced at startup.
- **Status**: RESOLVED for naming — canonical names are now in place
  (e.g. `ROCKETCHAT_AGENT_USERNAME`, `ROCKETCHAT_ADMIN_PASSWORD`,
  `ROCKETCHAT_PEER_USERNAME`, `GITEA_WEBHOOK_PORT`, `GITEA_WEBHOOK_BIND`,
  `OPENCODE_HOSTNAME`) with deprecated-name fallback via
  `chat/lib/config.py:getenv_compat`. See `docker/global.env.example`
  and `docker/agent.env.example`.
- **Compat-shim removal trigger**: when every deployment has migrated
  to the canonical names and the oldest supported release no longer
  documents the legacy names.

### Enable security features for rocketchat and opencode servers
- **Shortcut**: loopback-only, no password/TLS (gateway logs
  `auth=none`; serve warns it is unsecured).
- **Proper Solution**: auth, TLS, and access controls on both servers.
- **Trigger**: prereq for exposing anything outside of localhost.

### Consolidate timeout handling in chat/
- **Shortcut**: ad-hoc timeouts scattered across the chat layer (poll
  intervals, DDP 5s/30s windows, 10s dispose, several `requests` calls
  with no timeout at all).
- **Proper Solution**: standardized timeout policy/helper for chat/.
- **Trigger**: next time a timeout-related bug is observed.

### Inject channel catalog on agent idle (spare-cycle soak)
- **Shortcut**: the catalog is only announced on channel creation and on
  explicit `channel catalog` runs; an idle agent with undecided channels
  sits unused.
- **Proper Solution**: gateway detects agent idleness (session idle AND
  nothing pending across all subscribed channels) and injects the
  undecided catalog as a prompt.
- **Trigger**: first time an agent sits idle >5 min while the catalog
  holds unhandled entries.

