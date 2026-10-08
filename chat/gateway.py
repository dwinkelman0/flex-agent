"""Outer-loop gateway: listen for DMs from a peer and dispatch as prompts.

Runs an in-process equivalent of ``dm listen`` against a single peer, forever.
There is no initial ``list-recent`` call; reentrance reconciliation is the
agent's own concern (see ``agent/AGENTS.md``).

Dispatch semantics:

* On a poll/backlog hit, the gateway performs exactly ONE additional,
  non-blocking ``fetch_im_history`` to grab any sibling messages already
  present at the server. All hits are concatenated in timestamp order as
  ``[@ts] user: text`` lines and dispatched as a single prompt.
* No timers, sleeps, or batching windows are introduced between hits; any
  single chat message triggers exactly one prompt dispatch.
* ``--opencode-url``: POST the prompt to ``/session/:id/prompt_async``
  (creating the session once via ``/session``, Basic auth from
  ``OPENCODE_SERVER_PASSWORD``).
* Otherwise (or with ``--dry-run``): print the prompt JSON to stdout.

Exit codes:

* ``0`` — only on ``KeyboardInterrupt`` / ``SIGTERM`` (clean shutdown).
* ``2`` — configuration / usage error (``click.UsageError`` / ``click.ClickException``).
* ``3`` — runtime error (``requests.RequestException``, ``RuntimeError``, ``TypeError``).
"""

from __future__ import annotations

import json
import logging
import os
import queue
import signal
import sys
import threading
import time
from typing import Any

import click
import requests
import websocket
from client import _find_backlog
from lib import config
from lib.protocol import RocketChatClient
from lib.realtime import watch_room
from lib.util import now_iso
from requests.auth import HTTPBasicAuth


def _setup_logging(level: str) -> logging.Logger:
    """Configure stderr logging; stdout stays pure prompt-JSON for --dry-run."""
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s [gateway] %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        stream=sys.stderr,
        force=True,
    )
    return logging.getLogger("gateway")


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
        raise click.ClickException(
            "credentials not found; set USERNAME/PASSWORD env or provision via .agent_env"
        )
    return username, password


def _author_username(msg: dict[str, Any]) -> str:
    user = msg.get("u") or {}
    return str(user.get("username") or msg.get("username") or "unknown")


def _format_prompt_line(msg: dict[str, Any]) -> str:
    ts = msg.get("ts") or ""
    user = _author_username(msg)
    text = (msg.get("msg") or "").replace("\n", " ")
    return f"[@{ts}] {user}: {text}"


def _fetch_siblings(
    client: RocketChatClient,
    rid: str,
    primary: dict[str, Any],
    since_floor: str,
) -> list[dict[str, Any]]:
    """Return peer messages at/after ``since_floor`` including ``primary``.

    Performs a single non-blocking ``fetch_im_history`` (no ``oldest`` bound)
    and filters to peer-authored messages with ``ts >= since_floor``. The
    returned list is sorted ascending by ``ts``. ``primary`` is guaranteed to
    be present even if it falls outside the 50-message window.
    """
    try:
        messages = client.fetch_im_history(rid, oldest=None, count=50)
    except requests.RequestException as exc:
        click.echo(f"warning: sibling fetch failed: {exc}", err=True)
        return [primary]

    primary_id = primary.get("_id")
    peer_msgs = [
        m
        for m in messages
        if (m.get("u", {}).get("_id") or m.get("userId")) != client.user_id
        and (m.get("ts") or "") >= since_floor
    ]

    # Ensure primary is in the set (it may not be if history returned newer msgs only).
    if primary_id and not any(m.get("_id") == primary_id for m in peer_msgs):
        peer_msgs.append(primary)
    # No stable id — keep primary by reference identity.
    if not primary_id and not any(m is primary for m in peer_msgs):
        peer_msgs.append(primary)

    peer_msgs.sort(key=lambda m: m.get("ts") or "")
    return peer_msgs


def _build_prompt(peer: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    lines = [_format_prompt_line(m) for m in messages]
    return {"peer": peer, "prompt": "\n".join(lines), "messages": len(lines)}


class _OpenCodeDispatcher:
    """Dispatch prompts to an opencode-server, with optional Basic auth."""

    def __init__(
        self, base_url: str, password: str | None, user: str = "opencode"
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = HTTPBasicAuth(user, password) if password else None
        self.session_id: str | None = None

    def _ensure_session(self) -> str:
        if self.session_id is not None:
            return self.session_id
        url = f"{self.base_url}/session"
        resp = requests.post(url, auth=self.auth, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        sid = data.get("id") or data.get("session_id")
        if not isinstance(sid, str):
            raise TypeError(f"opencode /session did not return an id: {data!r}")
        self.session_id = sid
        return sid

    def dispose(self) -> None:
        """DELETE the gateway session so no server-side state persists."""
        if self.session_id is None:
            return
        url = f"{self.base_url}/session/{self.session_id}"
        # Short timeout: dispose runs during teardown races when the server
        # may already be half-dead; never let it stall shutdown.
        resp = requests.delete(url, auth=self.auth, timeout=10)
        resp.raise_for_status()
        self.session_id = None

    def dispatch(self, payload: dict[str, Any]) -> None:
        sid = self._ensure_session()
        url = f"{self.base_url}/session/{sid}/prompt_async"
        text = str(payload.get("prompt") or "")
        body = {"parts": [{"type": "text", "text": text}]}
        resp = requests.post(url, json=body, auth=self.auth, timeout=30)
        resp.raise_for_status()


def _dispatch(
    dispatcher: _OpenCodeDispatcher | None,
    payload: dict[str, Any],
    *,
    dry_run: bool,
) -> None:
    if dry_run or dispatcher is None:
        click.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    assert dispatcher is not None  # narrow for type-checkers
    dispatcher.dispatch(payload)


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("peer")
@click.option(
    "--server",
    default=None,
    envvar="ROCKETCHAT_URL",
    help="Rocket.Chat server URL (default: $ROCKETCHAT_URL / ../.env / localhost:3000).",
)
@click.option(
    "--timeout",
    default=60.0,
    show_default=True,
    type=float,
    help="Seconds for each fallback poll window (used only if push drops).",
)
@click.option(
    "--interval",
    default=1.5,
    show_default=True,
    type=float,
    help="Seconds between poll requests inside a fallback poll window.",
)
@click.option(
    "--since",
    "since_floor",
    default=None,
    help="ISO-8601 lower bound for the first poll window. Defaults to startup time.",
)
@click.option(
    "--opencode-url",
    default=None,
    help="If set, dispatch prompts to this opencode-server base URL.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print prompt JSON to stdout instead of dispatching to opencode.",
)
@click.option(
    "--log-level",
    default="INFO",
    show_default=True,
    help="Log level for stderr state logs (DEBUG, INFO, WARNING, ERROR).",
)
@click.option(
    "--ephemeral",
    is_flag=True,
    default=False,
    help="DELETE the opencode session on shutdown so no server-side state persists.",
)
def main(
    peer: str,
    server: str | None,
    timeout: float,
    interval: float,
    since_floor: str | None,
    opencode_url: str | None,
    dry_run: bool,
    log_level: str,
    ephemeral: bool,
) -> None:
    """Outer loop: listen for DMs from PEER and dispatch each hit as a prompt.

    Subscribes to the DM room via push (``dm watch``/DDP) forever. On startup
    a single backlog check covers messages sent while the gateway was down.
    On a hit, performs one extra non-blocking history fetch to batch
    siblings, then dispatches the concatenated prompt (``[@ts] user: text``
    lines) either to an opencode server (``--opencode-url``) or to stdout
    (``--dry-run`` or no URL). If push drops, falls back to one poll window
    (``--timeout``/``--interval``) before resubscribing.

    Exit codes: 0 only on Ctrl-C; 2 on config/usage errors; 3 on runtime errors.

    State is observable via stderr logs (stdout stays pure prompt-JSON in
    dry-run mode). Use --log-level DEBUG to watch every hit/dispatch.
    With --ephemeral the opencode session is DELETEed on shutdown.
    """
    log = _setup_logging(log_level)
    server_url = _resolve_server(server)
    username_me, password = _resolve_creds()
    log.info(
        "startup peer=%s server=%s mode=%s ephemeral=%s",
        peer,
        server_url,
        "dry-run" if (dry_run or not opencode_url) else f"opencode={opencode_url}",
        ephemeral,
    )

    dispatcher: _OpenCodeDispatcher | None = None
    if opencode_url and not dry_run:
        oc_password = os.environ.get("OPENCODE_SERVER_PASSWORD")
        dispatcher = _OpenCodeDispatcher(opencode_url, oc_password)
        log.info(
            "opencode dispatcher ready url=%s auth=%s",
            opencode_url,
            "basic" if oc_password else "none",
        )

    stop = threading.Event()

    def _shutdown(reason: str) -> None:
        log.info("shutdown (%s)", reason)
        stop.set()
        if (
            dispatcher is not None
            and ephemeral
            and dispatcher.session_id is not None
        ):
            try:
                sid = dispatcher.session_id
                dispatcher.dispose()
                log.info("opencode session disposed id=%s", sid)
            except (requests.RequestException, RuntimeError, TypeError) as exc:
                log.warning("session dispose failed: %s", exc)
        try:
            client.logout()
            log.info("rocketchat logout ok")
        except (requests.RequestException, RuntimeError) as exc:
            log.warning("rocketchat logout failed: %s", exc)

    client = RocketChatClient(server_url)

    shutdown_reason = "ctrl-c"
    shutting_down = False

    def _sigterm_handler(signum: int, frame: object) -> None:
        # Translate SIGTERM into the same graceful path as Ctrl-C so the
        # opencode session is disposed (with --ephemeral) and RC logged out.
        # Idempotent: a second signal during teardown exits immediately
        # instead of raising inside interpreter finalization (traceback).
        nonlocal shutdown_reason, shutting_down
        if shutting_down:
            os._exit(128 + signum)
        shutting_down = True
        shutdown_reason = "sigterm"
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _sigterm_handler)
    try:
        client.login(username_me, password)
        log.info("rocketchat login ok user=%s", username_me)
    except (requests.RequestException, RuntimeError, TypeError) as exc:
        raise click.ClickException(f"login failed: {exc}") from exc

    try:
        try:
            rid = client.ensure_dm(peer)
            log.info("dm ready peer=%s rid=%s", peer, rid)
        except (requests.RequestException, RuntimeError, TypeError) as exc:
            raise click.ClickException(f"ensure_dm({peer!r}) failed: {exc}") from exc

        since: str = since_floor if since_floor is not None else now_iso()
        log.info("push-subscribed DM peer=%s rid=%s since=%s", peer, rid, since)
        seen_ids: set[str] = set()
        inbox: queue.Queue[dict[str, Any]] = queue.Queue()

        def _dispatch_primary(primary: dict[str, Any]) -> None:
            """Batch siblings around `primary` and dispatch as one prompt."""
            nonlocal since
            siblings = _fetch_siblings(client, rid, primary, since)
            siblings = [m for m in siblings if m.get("_id") not in seen_ids]
            if not siblings:
                # Everything fetched was already dispatched (same-ts redelivery);
                # advance past it and keep listening without emitting.
                primary_ts = str(primary.get("ts") or "")
                if primary_ts and primary_ts >= since:
                    since = primary_ts
                log.debug("all siblings already seen; floor now %s", since)
                return
            payload = _build_prompt(peer, siblings)
            log.info(
                "dispatching %d message(s) latest_ts=%s",
                len(siblings),
                max((m.get("ts") or "" for m in siblings), default=""),
            )
            try:
                _dispatch(dispatcher, payload, dry_run=dry_run)
                if dispatcher is not None and not dry_run:
                    log.info("dispatched to opencode session=%s", dispatcher.session_id)
                else:
                    log.info("dry-run prompt printed to stdout")
            except (requests.RequestException, RuntimeError, TypeError) as exc:
                raise click.ClickException(f"dispatch failed: {exc}") from exc
            # Advance the floor past the latest dispatched message.
            latest_ts = max((m.get("ts") or "" for m in siblings), default="")
            since = latest_ts or now_iso()
            for m in siblings:
                if m.get("_id"):
                    seen_ids.add(str(m.get("_id")))
            log.debug("floor advanced to %s (seen=%d)", since, len(seen_ids))

        def _refresh_login() -> tuple[str, str]:
            """Re-login via REST so pushes/polls use a fresh auth token.

            Tokens can be rotated or expire (e.g. server restart); reusing
            the startup token forever ends in an unrecoverable auth crash
            loop. Called before every (re)subscribe and fallback poll.
            """
            try:
                uid, token = client.login(username_me, password)
            except (requests.RequestException, RuntimeError, TypeError) as exc:
                raise click.ClickException(f"re-login failed: {exc}") from exc
            log.debug("auth token refreshed user=%s", username_me)
            return uid, token

        def _start_watcher() -> threading.Thread:
            """Run one push subscription in a daemon thread; errors -> inbox."""

            def _run() -> None:
                try:
                    uid, token = _refresh_login()
                    watch_room(
                        server_url,
                        token,
                        uid,
                        rid,
                        uid,
                        inbox.put,
                        stop_event=stop,
                    )
                except (
                    click.ClickException,
                    RuntimeError,
                    OSError,
                    websocket.WebSocketException,
                ) as exc:
                    inbox.put({"_watch_error": str(exc)})

            thread = threading.Thread(target=_run, daemon=True)
            thread.start()
            return thread

        # Startup catch-up: messages sent while the gateway was down are not
        # pushed, so check once before subscribing.
        startup = _find_backlog(client, rid, username_me, since)
        if startup is not None:
            log.info(
                "startup backlog hit id=%s ts=%s",
                startup.get("_id"),
                startup.get("ts"),
            )
            _dispatch_primary(startup)

        auth_token = client.auth_token
        user_id = client.user_id
        if not auth_token or not user_id:
            raise click.ClickException("login did not yield auth token/user id")
        _start_watcher()

        while True:
            # NOTE: sleep-poll, not inbox.get(): on this platform a thread
            # parked in Queue/lock acquisition does not take SIGINT promptly
            # (observed 25s+ blackout), while sleep() interrupts immediately.
            # 0.2s bounds push→dispatch latency.
            time.sleep(0.2)
            try:
                primary = inbox.get_nowait()
            except queue.Empty:
                continue
            if stop.is_set():
                continue
            if "_watch_error" in primary:
                log.warning(
                    "push unavailable (%s); one fallback poll window",
                    primary["_watch_error"],
                )
                try:
                    _refresh_login()
                    fallback = client.poll_dm(
                        rid,
                        since_iso=since,
                        timeout=timeout,
                        interval=interval,
                    )
                except (requests.RequestException, RuntimeError, TypeError) as exc:
                    raise click.ClickException(f"poll failed: {exc}") from exc
                _start_watcher()
                if fallback is None:
                    log.debug("fallback poll timeout; resubscribed")
                    continue
                if fallback.get("_id") in seen_ids:
                    continue
                log.info(
                    "fallback poll hit id=%s ts=%s",
                    fallback.get("_id"),
                    fallback.get("ts"),
                )
                _dispatch_primary(fallback)
                continue
            if primary.get("_id") in seen_ids:
                log.debug(
                    "push hit already dispatched id=%s; skipping",
                    primary.get("_id"),
                )
                continue
            log.info("push hit id=%s ts=%s", primary.get("_id"), primary.get("ts"))
            _dispatch_primary(primary)
    except KeyboardInterrupt:
        _shutdown(shutdown_reason)
        sys.exit(0)
    except click.ClickException:
        _shutdown("error")
        raise
    except (requests.RequestException, RuntimeError, TypeError) as exc:
        _shutdown("error")
        raise click.ClickException(str(exc)) from exc

    # Unreachable (infinite loop), but keeps type-checkers happy.
    _shutdown("unreachable")
    sys.exit(0)


if __name__ == "__main__":
    main()
