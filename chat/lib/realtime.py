"""Rocket.Chat DDP Realtime push watcher.

Implements a minimal DDP client over the Rocket.Chat ``/websocket`` endpoint
so that DMs can be consumed via push subscription instead of REST polling.

Scope: single-room subscription to ``stream-room-messages``. The connection
authenticates with a REST ``authToken`` (resume login), subscribes to one
``rid``, handles server-initiated ``ping`` frames with ``pong`` replies, and
reconnects with bounded backoff on transient drops. Writes stay on the REST
API (DDP method calls are deprecated).
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import websocket  # type: ignore[import-untyped]


def ejson_to_iso(value: Any) -> Any:
    """Recursively convert EJSON ``{"$date": <ms>}`` to ISO-8601 strings.

    The Rocket.Chat DDP stream encodes timestamps as EJSON date objects. This
    helper converts them in place (returning a new structure) so the resulting
    dicts match the REST shape expected by ``format_message``.
    """
    if isinstance(value, dict):
        if "$date" in value and len(value) == 1:
            ms = value["$date"]
            # NOTE(first non-timestamp {"$date"} dict observed): converts any
            # single-key $date dict; a legit non-date payload shaped this way
            # would silently become a string.
            if isinstance(ms, (int, float)):
                return datetime.fromtimestamp(ms / 1000.0, tz=UTC).isoformat()
            return value
        return {k: ejson_to_iso(v) for k, v in value.items()}
    if isinstance(value, list):
        return [ejson_to_iso(v) for v in value]
    return value


def _ws_url(server_url: str) -> str:
    """Derive the DDP WebSocket URL from a Rocket.Chat HTTP(S) URL."""
    parsed = urlparse(server_url.rstrip("/"))
    scheme = parsed.scheme
    if scheme == "https":
        ws_scheme = "wss"
    elif scheme in ("http", "ws", "wss"):
        ws_scheme = "ws" if scheme == "http" else "wss"
    else:
        raise ValueError(f"unsupported server URL scheme: {scheme}")
    return f"{ws_scheme}://{parsed.netloc}/websocket"


def _normalize_ddp_message(frame: dict[str, Any]) -> dict[str, Any]:
    """Apply EJSON->ISO conversion to timestamp fields on a DDP message."""
    return ejson_to_iso(frame)


def watch_room(
    server_url: str,
    auth_token: str,
    user_id: str,
    rid: str,
    self_user_id: str,
    on_message: Callable[[dict[str, Any]], None],
    stop_event: threading.Event | None = None,
    timeout: float | None = None,
    max_reconnects: int = 5,
) -> None:
    """Subscribe to ``rid`` on the DDP stream and invoke ``on_message`` per peer msg.

    The function connects to the server's ``/websocket`` endpoint, performs a
    resume login with ``auth_token``, subscribes to ``stream-room-messages``
    for ``rid``, then dispatches each incoming peer message (EJSON-normalized)
    to ``on_message``. Messages authored by ``self_user_id`` are skipped.

    Server ``ping`` frames are answered with ``pong``. On connection drop, the
    client reconnects up to ``max_reconnects`` times with exponential backoff
    before re-raising. A ``stop_event`` being set or a ``timeout`` elapsing
    ends the loop cleanly.
    """
    if stop_event is None:
        stop_event = threading.Event()
    ws_url = _ws_url(server_url)
    deadline = None if timeout is None else time.monotonic() + timeout
    reconnects = 0

    while True:
        if stop_event.is_set():
            return
        if deadline is not None and time.monotonic() >= deadline:
            return

        ws = websocket.create_connection(ws_url, timeout=30)
        try:
            _do_connect(ws)
            _do_login(ws, auth_token)
            _do_subscribe(ws, rid)

            while True:
                if stop_event.is_set():
                    return
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return
                    # Bound recv so Ctrl-C/stop is honored promptly even on a
                    # quiet socket (server pings also wake us earlier).
                    ws.settimeout(max(0.1, min(5.0, remaining)))
                else:
                    ws.settimeout(5.0)
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    if deadline is not None and time.monotonic() >= deadline:
                        return
                    continue
                if not raw:
                    raise RuntimeError("DDP connection closed: empty frame")
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError as exc:
                    print(
                        f"warning: dropping non-JSON DDP frame ({exc})",
                        file=sys.stderr,
                    )
                    continue

                msg_type = frame.get("msg")
                if msg_type == "ping":
                    ws.send(json.dumps({"msg": "pong"}))
                    continue
                if msg_type in ("connected", "ready", "added", "changed"):
                    pass
                elif msg_type is None and "server_id" in frame:
                    # Bare hello frame, ignore.
                    continue
                else:
                    # method results, errors, unknown frames: ignore silently
                    # except to surface explicit DDP errors to stderr.
                    if msg_type == "failed":
                        print(
                            f"warning: DDP subscription failed: {frame}",
                            file=sys.stderr,
                        )
                    continue

                collection = frame.get("collection")
                if collection != "stream-room-messages":
                    continue
                fields = frame.get("fields") or {}
                args = fields.get("args") or []
                if not args:
                    continue
                payload = args[0]
                if not isinstance(payload, dict):
                    continue
                normalized = _normalize_ddp_message(payload)
                author_id = (
                    (normalized.get("u") or {}).get("_id")
                    if isinstance(normalized.get("u"), dict)
                    else normalized.get("userId")
                )
                if author_id == self_user_id:
                    continue
                on_message(normalized)

            # Inner loop exits cleanly via return paths above.
        except (
            websocket.WebSocketException,
            ConnectionError,
            TimeoutError,
            OSError,
        ) as exc:
            reconnects += 1
            if reconnects > max_reconnects:
                raise
            backoff = min(30.0, 0.5 * (2 ** (reconnects - 1)))
            print(
                f"warning: DDP connection lost ({exc}); "
                f"reconnecting ({reconnects}/{max_reconnects}) in {backoff:.1f}s",
                file=sys.stderr,
            )
            try:
                ws.close()
            except websocket.WebSocketException:
                pass
            if stop_event.wait(backoff):
                return
            if deadline is not None and time.monotonic() >= deadline:
                return
            continue
        finally:
            try:
                ws.close()
            except websocket.WebSocketException:
                pass


def _do_connect(ws: websocket.WebSocket) -> None:
    """Send the DDP handshake and wait for the ``connected`` frame."""
    ws.send(json.dumps({"msg": "connect", "version": "1", "support": ["1"]}))
    ws.settimeout(30.0)
    while True:
        try:
            raw = ws.recv()
        except websocket.WebSocketTimeoutException as exc:
            raise RuntimeError("DDP handshake timed out after 30s") from exc
        if not raw:
            raise RuntimeError("DDP handshake closed before connected frame")
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"DDP handshake non-JSON frame: {exc}") from exc
        if frame.get("msg") == "connected":
            return
        if frame.get("msg") == "failed":
            raise RuntimeError(f"DDP handshake rejected: {frame}")
        # Bare hello / other frames: keep reading.
        continue


def _do_login(ws: websocket.WebSocket, auth_token: str) -> None:
    """Resume-login via the ``login`` DDP method."""
    ws.send(
        json.dumps(
            {
                "msg": "method",
                "method": "login",
                "id": "1",
                "params": [{"resume": auth_token}],
            }
        )
    )
    ws.settimeout(30.0)
    while True:
        try:
            raw = ws.recv()
        except websocket.WebSocketTimeoutException as exc:
            raise RuntimeError("DDP login timed out after 30s") from exc
        if not raw:
            raise RuntimeError("DDP login closed before result frame")
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"DDP login non-JSON frame: {exc}") from exc
        if frame.get("msg") == "ping":
            ws.send(json.dumps({"msg": "pong"}))
            continue
        if frame.get("msg") == "result" and frame.get("id") == "1":
            if "error" in frame and frame["error"] is not None:
                raise RuntimeError(f"DDP login rejected: {frame['error']}")
            return


def _do_subscribe(ws: websocket.WebSocket, rid: str) -> None:
    """Subscribe to ``stream-room-messages`` for ``rid`` and await ``ready``."""
    sub_id = "s1"
    ws.send(
        json.dumps(
            {
                "msg": "sub",
                "id": sub_id,
                "name": "stream-room-messages",
                "params": [rid, False],
            }
        )
    )
    ws.settimeout(30.0)
    while True:
        try:
            raw = ws.recv()
        except websocket.WebSocketTimeoutException as exc:
            raise RuntimeError("DDP subscribe timed out after 30s") from exc
        if not raw:
            raise RuntimeError("DDP subscribe closed before ready frame")
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"DDP subscribe non-JSON frame: {exc}") from exc
        if frame.get("msg") == "ping":
            ws.send(json.dumps({"msg": "pong"}))
            continue
        if frame.get("msg") == "ready":
            subs = frame.get("subs") or []
            if not subs or sub_id in subs:
                return
            # ready for a different sub: keep waiting.
            continue
        if frame.get("msg") == "nosub":
            raise RuntimeError(f"DDP subscription rejected: {frame}")
        # added/changed frames can arrive before ready; keep reading.
        continue
