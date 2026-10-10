"""Post to any Rocket.Chat room from an agent session.

Usage::

    python channel.py post <room> "text" [--thread TMID]

``room`` is a DM username, a ``#channel`` name, or ``rid:<id>``.
This is the sanctioned toolkit path for reaching non-DM rooms and for
any agent-initiated (non-plugin) posts. The opencode plugin owns plain
DM replies; use this tool for everything else (rule (b) turns).

Authentication: ``USERNAME``/``PASSWORD`` env vars or repo-root
``.agent_env``. Server: ``--server`` / ``ROCKETCHAT_URL`` / ``../.env``.
"""

from __future__ import annotations

import os
import sys

import click
import requests
from lib import config
from lib.protocol import RocketChatClient


def _resolve_server(server: str | None) -> str:
    if server:
        return server.rstrip("/")
    return config.get_server_url()


def _resolve_creds() -> tuple[str, str]:
    username = os.environ.get("USERNAME")
    password = os.environ.get("PASSWORD")
    if not username or not password:
        agent_env = config.load_agent_env()
        username = username or agent_env.get("USERNAME")
        password = password or agent_env.get("PASSWORD")
    if not username or not password:
        click.echo(
            "error: credentials not found; set USERNAME/PASSWORD env "
            "or provision via .agent_env",
            err=True,
        )
        sys.exit(2)
    return username, password


def _resolve_rid(client: RocketChatClient, room: str) -> str:
    """Map a room reference to a room id."""
    if room.startswith("rid:"):
        return room[len("rid:") :]
    if room.startswith("#"):
        name = room[1:]
        for endpoint in ("channels.info", "groups.info"):
            resp = client.session.get(
                f"{client.server_url}/api/v1/{endpoint}",
                params={"roomName": name},
                headers=client._auth_headers(),
            )
            if resp.status_code == 200:
                body = resp.json()
                rid = (body.get("channel") or body.get("group") or {}).get("_id")
                if rid:
                    return str(rid)
        raise click.ClickException(f"no channel or group named #{name}")
    return client.ensure_dm(room)


@click.group()
def cli() -> None:
    """Post to Rocket.Chat rooms."""


@cli.command("post")
@click.argument("room")
@click.argument("message")
@click.option(
    "--thread",
    "thread_id",
    default=None,
    help="Parent message id to reply inside a thread.",
)
@click.option(
    "--server", default=None, envvar="ROCKETCHAT_URL", help="Rocket.Chat server URL."
)
def channel_post(room: str, message: str, thread_id: str | None, server: str | None) -> None:
    """Post MESSAGE to ROOM (DM user, #channel, or rid:ID)."""
    server_url = _resolve_server(server)
    username_me, password = _resolve_creds()

    client = RocketChatClient(server_url)
    try:
        client.login(username_me, password)
    except (requests.RequestException, RuntimeError, TypeError) as exc:
        click.echo(f"error: login failed: {exc}", err=True)
        sys.exit(3)
    try:
        try:
            rid = _resolve_rid(client, room)
        except (requests.RequestException, RuntimeError, TypeError) as exc:
            click.echo(f"error: cannot resolve room {room!r}: {exc}", err=True)
            sys.exit(3)
        body: dict[str, object] = {"channel": rid, "text": message}
        if thread_id:
            body["tmid"] = thread_id
        try:
            resp = client.session.post(
                f"{server_url}/api/v1/chat.postMessage",
                json=body,
                headers=client._auth_headers(),
            )
            resp.raise_for_status()
        except (requests.RequestException, RuntimeError) as exc:
            click.echo(f"error: post failed: {exc}", err=True)
            sys.exit(3)
        msg = resp.json().get("message") or {}
        click.echo(str(msg.get("_id", "")))
    finally:
        try:
            client.logout()
        except (requests.RequestException, RuntimeError) as exc:
            click.echo(f"warning: logout failed: {exc}", err=True)


if __name__ == "__main__":
    cli()
