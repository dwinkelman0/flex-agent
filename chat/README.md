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
