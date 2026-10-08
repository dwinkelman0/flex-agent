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

### Manage gateway + serve with systemd instead of bash
- **Shortcut**: `start-dev.sh` supervises both processes with traps,
  pid tracking (`pgrep -P` for the `uv` wrapper's python child), and
  bounded-wait loops.
- **Proper Solution**: systemd units (or container supervisor) with
  restart policies, dropping the bash supervision entirely.
- **Trigger**: when we upgrade to containerization.

### Standardize environment variables and their provenance
- **Shortcut**: `.env` / `.agent_env` files plus ad-hoc variables
  (`PEER_USERNAME`, `OPENCODE_PORT`, …) with implicit precedence
  scattered across `chat/lib/config.py` and scripts.
- **Proper Solution**: documented env schema with provenance (file vs
  env vs default) surfaced at startup.
- **Trigger**: when we upgrade to containerization or deploy a
  production-grade rocketchat server.

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

## PENDING

### Dump per-process logs to /tmp
- **Shortcut**: `start-dev.sh` interleaves gateway + serve on one console
  stream, and the gateway bakes timestamp/process-name into its own log
  format (`chat/gateway.py`).
- **Proper Solution**: per-process log files under /tmp (one per process),
  still streaming to console (e.g. via tee).

### Log hygiene: bare level/message in-process, wrapper adds name+timestamp
- **Shortcut**: each process formats its own timestamp/name (gateway
  logging format, opencode native format).
- **Proper Solution**: processes emit only level/message/etc.; a
  standardized wrapper adds process name and timestamp.
