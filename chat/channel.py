"""Post to any Rocket.Chat room from an agent session, and manage channel
subscriptions.

Usage::

    python channel.py post <room> "text" [--thread TMID]
    python channel.py subscribe <key>
    python channel.py unsubscribe <key>
    python channel.py catalog

``room`` is a DM username, a ``#channel`` name, or ``rid:<id>``.
Channel keys look like ``rc:dm:<peer>``, ``rc:room:<rid>``,
``gitea:issue:<owner>/<repo>#<n>``, ``gitea:repo:<owner>/<repo>``.
This is the sanctioned toolkit path for reaching non-DM rooms and for
any agent-initiated (non-plugin) posts. The opencode plugin owns plain
DM replies; use this tool for everything else (rule (b) turns).

Subscription state lives in ``agent/subscriptions.json`` (gateway-owned
helpers in ``lib/subscriptions.py``). The dedicated DM is mandatory and
cannot be unsubscribed. New channels require an explicit accept
(``subscribe``) or decline (``unsubscribe``); silence decides nothing.

Authentication: ``ROCKETCHAT_AGENT_USERNAME``/``ROCKETCHAT_AGENT_PASSWORD``
env vars (legacy ``USERNAME``/``PASSWORD``) or repo-root ``.agent_env``.
Server: ``--server`` / ``ROCKETCHAT_URL`` / ``../.env``.
"""

from __future__ import annotations

import sys

import click
import requests
from lib import config, subscriptions
from lib.protocol import RocketChatClient
from lib.util import now_iso


def _resolve_server(server: str | None) -> str:
    if server:
        return server.rstrip("/")
    return config.get_server_url()


def _resolve_creds() -> tuple[str, str]:
    agent_env = config.load_agent_env()
    username = config.getenv_compat(
        "ROCKETCHAT_AGENT_USERNAME", "USERNAME", dotfile=agent_env
    )
    password = config.getenv_compat(
        "ROCKETCHAT_AGENT_PASSWORD", "PASSWORD", dotfile=agent_env
    )
    if not username or not password:
        click.echo(
            "error: credentials not found; set ROCKETCHAT_AGENT_USERNAME/"
            "ROCKETCHAT_AGENT_PASSWORD env (legacy USERNAME/PASSWORD still "
            "supported) or provision via .agent_env",
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


def _gateway_peer() -> str | None:
    """Best-effort gateway peer for the DM-mandatory guard.

    Prefers the canonical ``ROCKETCHAT_PEER_USERNAME``; falls back to
    legacy ``PEER_USERNAME`` and ``GATEWAY_PEER``.
    """
    return config.getenv_compat(
        "ROCKETCHAT_PEER_USERNAME", "PEER_USERNAME", "GATEWAY_PEER"
    )


@cli.command("subscribe")
@click.argument("key")
def channel_subscribe(key: str) -> None:
    """Accept a channel subscription (explicit opt-in).

    Accept-on-new replays from the beginning; any other (re)subscribe
    baselines at now — see lib/subscriptions.py.
    """
    _, backfill = subscriptions.subscribe(
        key, announced=False, now_iso=now_iso()
    )
    # Late subscribe: baseline at now so only newer items dispatch. (For
    # accept-on-new the gateway clears the baseline when it sees the
    # flip; see gateway discovery tick.)
    if not backfill:
        subscriptions.set_baseline(key, now_iso())
    click.echo(f"subscribed {key} (backfill={'all' if backfill else 'new-only'})")


@cli.command("unsubscribe")
@click.argument("key")
def channel_unsubscribe(key: str) -> None:
    """Leave a channel, or decline an undecided announcement.

    The dedicated DM is mandatory and cannot be unsubscribed.
    """
    try:
        result = subscriptions.unsubscribe(key, gateway_peer=_gateway_peer())
    except ValueError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(2)
    click.echo(f"{result} {key}")


@cli.command("catalog")
def channel_catalog() -> None:
    """List channels awaiting an explicit accept/decline decision."""
    for key in subscriptions.catalog():
        click.echo(key)


if __name__ == "__main__":
    cli()
