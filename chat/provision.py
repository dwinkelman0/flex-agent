"""Provision a Rocket.Chat user defined in ``.agent_env``.

Target credentials (the user to ensure exists):

* ``USERNAME`` / ``PASSWORD`` from ``.agent_env`` at the repo root,
  with real environment variables taking precedence if both are set.

Admin/server credentials come from the repo-root ``.env`` (``ROCKETCHAT_URL``,
``ROCKETCHAT_USER``, ``ROCKETCHAT_PASSWORD``).

This script only consumes ``.agent_env``; it never writes it.

Exit codes:

* 0 — user ensured
* 2 — missing env configuration
* 3 — runtime error talking to Rocket.Chat
"""

from __future__ import annotations

import os
import sys

import requests
from lib import config
from lib.protocol import RocketChatClient


def _resolve_target(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        value = config.load_agent_env().get(name)
    if not value:
        print(
            f"error: required {name} is not set (set env var or define it in .agent_env)",
            file=sys.stderr,
        )
        sys.exit(2)
    return str(value)


def main() -> int:
    target_username = _resolve_target("USERNAME")
    target_password = _resolve_target("PASSWORD")

    root_env = config.load_root_env()
    server_url = config.get_server_url()

    admin_user = os.environ.get("ROCKETCHAT_USER") or root_env.get("ROCKETCHAT_USER")
    admin_pass = os.environ.get("ROCKETCHAT_PASSWORD") or root_env.get(
        "ROCKETCHAT_PASSWORD"
    )
    if not admin_user or not admin_pass:
        print(
            "error: admin credentials missing (set ROCKETCHAT_USER/ROCKETCHAT_PASSWORD in env or ../.env)",
            file=sys.stderr,
        )
        return 2

    client = RocketChatClient(server_url)
    try:
        client.login(admin_user, admin_pass)
        user = client.ensure_user(
            username=target_username,
            password=target_password,
            email=config.synthetic_email(target_username),
        )
    except (requests.RequestException, RuntimeError, TypeError) as exc:  # network / API errors
        print(f"error: failed to provision user: {exc}", file=sys.stderr)
        return 3
    finally:
        try:
            client.logout()
        except (requests.RequestException, RuntimeError) as exc:
            print(f"warning: logout failed: {exc}", file=sys.stderr)

    path = config.AGENT_ENV_PATH
    print(f"provisioned user '{target_username}' (id={user.get('_id', '?')})")
    print(f"consumed credentials from {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
