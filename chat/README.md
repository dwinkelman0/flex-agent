# flex-agent-chat

Rocket.Chat agent scripts (Python 3.13, uv-managed).

## Setup

```
cd chat
uv sync
```

One-time repo-root setup to activate pre-commit hooks:

```
git config core.hooksPath hooks
```

(Or run `./scripts/setup-hooks.sh` from the repo root.)

## Provision a user

Reads `USERNAME` and `PASSWORD` from `../.agent_env` (real environment
variables take precedence if set); uses admin credentials
from `../.env` (`ROCKETCHAT_USER`, `ROCKETCHAT_PASSWORD`, optional
`ROCKETCHAT_URL`).

```
# define desired agent creds in ../.agent_env first, then:
python provision.py
```

This ensures the user exists on the server. It only consumes
`../.agent_env`; it never writes it.

## Send / receive DMs

```
python client.py dm post alice "hello from the agent"
python client.py dm listen alice --timeout 30
```

Auth for `client.py` is resolved from `USERNAME`/`PASSWORD` env vars or the
`../.agent_env` dotfile. Server URL comes from `--server`, the
`ROCKETCHAT_URL` env var, or `../.env`.

## Gateway (outer loop)

```
python gateway.py PEER [--server URL] [--timeout 60] [--interval 1.5] \
    [--since ISO] [--opencode-url URL] [--dry-run]
```

Runs `dm listen` in-process against `PEER` forever. On startup it enters
the listen loop immediately — there is no initial `list-recent` call
(reentrance reconciliation is the agent's own concern via
`agent/AGENTS.md`). On a hit, the gateway performs exactly ONE additional
non-blocking history fetch to batch siblings already present at the
server, concatenates all hits in timestamp order as `[@ts] user: text`
lines, and dispatches a single prompt.

No timers, sleeps, or batching windows are introduced between hits; any
single chat message triggers exactly one prompt dispatch. After a
dispatch, the since-floor advances past the latest dispatched message.
On timeout the loop iterates again immediately (no extra sleep).

With `--opencode-url`, prompts are POSTed to `/session/:id/prompt_async`
(the session is created once via `/session`, Basic auth from
`OPENCODE_SERVER_PASSWORD`). Otherwise (or with `--dry-run`) the prompt
JSON is printed to stdout.

Exit codes: `0` only on Ctrl-C; `2` on config/usage errors; `3` on
runtime errors.
