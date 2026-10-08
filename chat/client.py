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
    """Direct-message commands (post, listen, list-recent)."""


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


@dm.command("post-image")
@click.argument("username")
@click.argument("image_path", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--message",
    "caption",
    default=None,
    help="Optional caption / description sent alongside the image.",
)
@click.option(
    "--server", default=None, envvar="ROCKETCHAT_URL", help="Rocket.Chat server URL."
)
def dm_post_image(
    username: str,
    image_path: str,
    caption: str | None,
    server: str | None,
) -> None:
    """Upload IMAGE_PATH to the DM room with USERNAME.

    The image is posted to the room via ``/api/v1/rooms.upload/{rid}``.
    ``--message`` (if provided) is used as both the caption and description.
    """
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
            rid = client.ensure_dm(username)
            resp = client.upload_image(rid, image_path, msg=caption)
        except (requests.RequestException, RuntimeError, TypeError) as exc:
            click.echo(f"error: {exc}", err=True)
            sys.exit(3)
    finally:
        try:
            client.logout()
        except (requests.RequestException, RuntimeError) as exc:
            click.echo(f"warning: logout failed: {exc}", err=True)

    message = resp.get("message") or {}
    if message.get("_id"):
        click.echo(format_message(message))
    else:
        # No message object (e.g. raw upload response); fall back to full JSON.
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


@dm.command("list-recent")
@click.argument("username")
@click.option(
    "--server", default=None, envvar="ROCKETCHAT_URL", help="Rocket.Chat server URL."
)
@click.option(
    "--count",
    default=50,
    show_default=True,
    type=int,
    help="Number of recent messages to fetch from history.",
)
@click.option(
    "--since",
    "since_floor",
    default=None,
    help=(
        "ISO-8601 timestamp used as a lower bound. Combined with the agent's last "
        "message timestamp (max of the two is used as the floor)."
    ),
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Ignored; output is always one JSON object per line. Kept for interface symmetry.",
)
def dm_list_recent(
    username: str,
    server: str | None,
    count: int,
    since_floor: str | None,
    as_json: bool,  # reserved for future binary flag
) -> None:
    """Reentrance escape hatch for ``dm listen``.

    Fetches the most recent ``COUNT`` messages from the DM room with
    ``USERNAME``. Finds the agent's own last message in that window (matched
    by ``user_id`` / agent username) and prints every message authored by
    ``USERNAME`` with ``ts`` strictly greater than the agent's last message
    ``ts``, sorted ascending.

    If the agent has no message in the window, prints every peer message in
    the window (documented fallback). If ``--since`` is given, it is used as
    an additional floor (max of the two). Output is one JSON line per
    message via ``format_message`` (includes ``ts`` and ``age_seconds``).
    Exits 0 (silent on stdout) when there are no new messages.
    """
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
            rid = client.ensure_dm(username)
            messages = client.fetch_im_history(rid, oldest=None, count=count)
        except (requests.RequestException, RuntimeError, TypeError) as exc:
            click.echo(f"error: {exc}", err=True)
            sys.exit(3)

        # Find agent's own last message ts in the window.
        self_ts: str | None = None
        for msg in messages:
            author_id = msg.get("u", {}).get("_id") or msg.get("userId")
            if author_id == client.user_id:
                msg_ts = msg.get("ts") or ""
                if self_ts is None or msg_ts > self_ts:
                    self_ts = msg_ts

        # Floor: max(agent_last_ts, --since).
        floor: str | None = self_ts if self_ts is not None else since_floor
        if self_ts is not None and since_floor is not None:
            floor = max(self_ts, since_floor)
        elif since_floor is not None:
            floor = since_floor

        # Filter: peer messages with ts > floor.
        peer_msgs = [
            m
            for m in messages
            if (m.get("u", {}).get("_id") or m.get("userId")) != client.user_id
        ]
        if floor is not None:
            peer_msgs = [m for m in peer_msgs if (m.get("ts") or "") > floor]
        peer_msgs.sort(key=lambda m: m.get("ts") or "")

        for msg in peer_msgs:
            click.echo(format_message(msg))
    finally:
        try:
            client.logout()
        except (requests.RequestException, RuntimeError) as exc:
            click.echo(f"warning: logout failed: {exc}", err=True)


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
