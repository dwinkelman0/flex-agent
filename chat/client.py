"""Rocket.Chat CLI client.

Usage examples::

    python client.py dm post alice "hello from the agent"
    python client.py dm listen alice --timeout 30

Authentication resolution:

* ``--server`` / ``ROCKETCHAT_URL`` env / repo-root ``.env`` (``ROCKETCHAT_URL``)
  for the server URL
* ``USERNAME`` / ``PASSWORD`` env vars, else keys in ``.agent_env``
"""

from __future__ import annotations

import os
import sys

import click
import requests
from lib import config
from lib.protocol import RocketChatClient
from lib.util import format_message, now_iso


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
            "error: credentials not found; set USERNAME/PASSWORD env or provision via .agent_env",
            err=True,
        )
        sys.exit(2)
    return username, password


@click.group()
def cli() -> None:
    """Rocket.Chat agent client."""


@cli.group()
def dm() -> None:
    """Direct-message commands."""


@dm.command("post")
@click.argument("username")
@click.argument("message", required=False)
@click.option(
    "--text", "text_alias", default=None, help="Alias for MESSAGE positional argument."
)
@click.option(
    "--server", default=None, envvar="ROCKETCHAT_URL", help="Rocket.Chat server URL."
)
def dm_post(
    username: str, message: str | None, text_alias: str | None, server: str | None
) -> None:
    """Send MESSAGE to USERNAME via DM."""
    text = message or text_alias
    if not text:
        raise click.UsageError("MESSAGE argument or --text is required.")

    server_url = _resolve_server(server)
    username_me, password = _resolve_creds()

    client = RocketChatClient(server_url)
    client.login(username_me, password)
    try:
        rid = client.ensure_dm(username)
        resp = client.post_message(rid, text)
    finally:
        try:
            client.logout()
        except (requests.RequestException, RuntimeError) as exc:
            click.echo(f"warning: logout failed: {exc}", err=True)

    msg = resp.get("message") or {}
    if msg.get("_id"):
        click.echo(msg["_id"])
    else:
        # Fallback: emit full response JSON so callers can still inspect it.
        click.echo(format_message(resp))


@dm.command("listen")
@click.argument("username")
@click.option(
    "--timeout",
    default=60,
    show_default=True,
    type=float,
    help="Seconds to wait for a reply.",
)
@click.option(
    "--interval",
    default=1.5,
    show_default=True,
    type=float,
    help="Polling interval in seconds.",
)
@click.option(
    "--since",
    "since_floor",
    default=None,
    help=(
        "ISO-8601 timestamp used as the oldest boundary for backlog drain and "
        "polling. Defaults to the moment just before login."
    ),
)
@click.option(
    "--server", default=None, envvar="ROCKETCHAT_URL", help="Rocket.Chat server URL."
)
def dm_listen(
    username: str,
    timeout: float,
    interval: float,
    since_floor: str | None,
    server: str | None,
) -> None:
    """Wait for the next DM from USERNAME, printing the message JSON on success.

    Before entering the poll loop, performs a one-shot backlog drain: fetches
    the most recent 50 messages in the room (without an ``oldest`` bound) and,
    if any unread peer messages are present, prints the oldest one as JSON
    (with ``ts`` and computed ``age_seconds``) and exits 0.

    When no backlog is found, enters a timed poll loop starting from the
    moment just before login (or the value of ``--since`` if provided).
    Exits 4 on timeout, 2 on credential/config errors.
    """
    # Capture the "now" marker BEFORE login so messages arriving during auth
    # are not missed. --since overrides as a floor.
    since = since_floor if since_floor is not None else now_iso()

    server_url = _resolve_server(server)
    username_me, password = _resolve_creds()

    client = RocketChatClient(server_url)
    client.login(username_me, password)
    try:
        rid = client.ensure_dm(username)

        # Backlog pre-check: find any unread peer message older than `since`.
        backlog = _find_backlog(client, rid, username_me, since)
        if backlog is not None:
            click.echo(format_message(backlog))
            return

        msg = client.poll_dm(rid, since_iso=since, timeout=timeout, interval=interval)
    finally:
        try:
            client.logout()
        except (requests.RequestException, RuntimeError) as exc:
            click.echo(f"warning: logout failed: {exc}", err=True)

    if msg is None:
        click.echo("timeout", err=True)
        sys.exit(4)
    click.echo(format_message(msg))


def _find_backlog(
    client: RocketChatClient,
    rid: str,
    self_username: str,
    since_floor: str | None,
) -> dict | None:
    """Return the oldest unread peer message from a single history fetch.

    Fetches up to 50 recent messages without an ``oldest`` bound, drops
    messages authored by the current user, and applies ``since_floor`` as a
    lower bound if provided (ISO-8601 string comparison). Returns ``None``
    when no peer messages remain after filtering.
    """
    try:
        messages = client.fetch_im_history(rid, oldest=None, count=50)
    except requests.RequestException as exc:
        click.echo(f"warning: backlog pre-check failed: {exc}", err=True)
        return None

    peer_msgs = [
        m
        for m in messages
        if (m.get("u", {}).get("_id") or m.get("userId")) != client.user_id
    ]
    if since_floor is not None:
        peer_msgs = [m for m in peer_msgs if (m.get("ts") or "") >= since_floor]
    if not peer_msgs:
        return None
    # Oldest = smallest ts.
    return min(peer_msgs, key=lambda m: m.get("ts") or "")


if __name__ == "__main__":
    cli()
