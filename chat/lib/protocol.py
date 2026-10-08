"""Raw requests-based Rocket.Chat client.

This module deliberately avoids the ``rocket-python`` SDK so that the wire
calls are explicit and easy to debug. Only the endpoints required for
provisioning users and exchanging DMs are implemented.
"""

from __future__ import annotations

import sys
import time
from typing import Any

import requests


class RocketChatClient:
    """Minimal synchronous Rocket.Chat REST client."""

    def __init__(self, server_url: str) -> None:
        self.server_url: str = server_url.rstrip("/")
        self.session: requests.Session = requests.Session()
        self.user_id: str | None = None
        self.auth_token: str | None = None

    # ------------------------------------------------------------------ auth

    def login(self, username: str, password: str) -> tuple[str, str]:
        """Authenticate and store ``userId``/``authToken`` for later calls."""
        url = f"{self.server_url}/api/v1/login"
        resp = self.session.post(url, json={"user": username, "password": password})
        if resp.status_code == 401:
            try:
                body = resp.json()
            except ValueError:
                body = {}
            if body.get("error") == "totp-required":
                raise RuntimeError(
                    "Login blocked by email 2FA (totp-required). "
                    "Disable Administration > Accounts > Two Factor > Email 2FA "
                    "for local dev, or log in via UI once to verify."
                )
            # Generic 401 — most likely a password mismatch.
            raise RuntimeError(
                f"Login failed for user '{username}': invalid password "
                "(or user exists with a different password)"
            )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("status") == "success":
            raise RuntimeError(f"Login failed: {data}")
        payload = data["data"]
        user_id = payload.get("userId")
        auth_token = payload.get("authToken")
        if not isinstance(user_id, str) or not isinstance(auth_token, str):
            raise TypeError("Login response missing userId or authToken.")
        self.user_id = user_id
        self.auth_token = auth_token
        return self.user_id, self.auth_token

    def _auth_headers(self) -> dict[str, str]:
        if not self.user_id or not self.auth_token:
            raise RuntimeError("Client is not authenticated; call login() first.")
        return {"X-Auth-Token": self.auth_token, "X-User-Id": self.user_id}

    def logout(self) -> None:
        """Invalidate the current auth token on the server."""
        if not self.auth_token:
            return
        try:
            url = f"{self.server_url}/api/v1/logout"
            self.session.post(url, headers=self._auth_headers())
        finally:
            self.user_id = None
            self.auth_token = None

    # ------------------------------------------------------------------ users

    def get_user_info(self, username: str) -> dict[str, Any] | None:
        """Fetch user info by username. Returns ``None`` if not found."""
        url = f"{self.server_url}/api/v1/users.info"
        resp = self.session.get(
            url, params={"username": username}, headers=self._auth_headers()
        )
        if resp.status_code == 400:
            body = (
                resp.json()
                if resp.headers.get("content-type", "").startswith("application/json")
                else {}
            )
            error = body.get("error") or body.get("errorType") or ""
            error_normalized = str(error).strip().rstrip(".").lower()
            if error_normalized in ("error-user-not-found", "user not found"):
                return None
            # Fall through to raise on other 400s.
        resp.raise_for_status()
        return resp.json().get("user")

    def create_user(
        self,
        username: str,
        password: str,
        email: str,
        name: str | None = None,
        roles: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a new user and return the user object."""
        url = f"{self.server_url}/api/v1/users.create"
        payload: dict[str, Any] = {
            "username": username,
            "password": password,
            "email": email,
            "name": name or username,
            "roles": roles or ["user"],
            "verified": True,
            "active": True,
            "joinDefaultChannels": False,
            "sendWelcomeEmail": False,
            "requirePasswordChange": False,
        }
        resp = self.session.post(url, json=payload, headers=self._auth_headers())
        resp.raise_for_status()
        return resp.json().get("user", {})

    def ensure_user(
        self,
        username: str,
        password: str,
        email: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Return existing user or create a new one with the given creds.

        Note: when the user already exists, this method does *not* verify
        that ``password`` matches — Rocket.Chat does not expose a password
        check endpoint. Callers needing strict parity should compare by
        attempting a login with the supplied creds; a 401 from ``login()``
        raises ``RuntimeError`` with a password-mismatch message (see
        :meth:`login`).
        """
        info = self.get_user_info(username)
        if info is not None:
            return info
        if email is None:
            from .config import synthetic_email  # local import to avoid cycle

            email = synthetic_email(username)
        return self.create_user(username, password, email, name=name)

    # ------------------------------------------------------------------ DMs

    def ensure_dm(self, peer_username: str) -> str:
        """Open (or reuse) a DM room with ``peer_username`` and return its rid."""
        url = f"{self.server_url}/api/v1/im.create"
        resp = self.session.post(
            url, json={"username": peer_username}, headers=self._auth_headers()
        )
        resp.raise_for_status()
        body = resp.json()
        room = body.get("room") or {}
        rid = room.get("_id")
        if not rid:
            raise RuntimeError(f"im.create did not return a room id: {body}")
        return rid

    def post_message(self, rid: str, text: str) -> dict[str, Any]:
        """Post ``text`` into the room ``rid`` and return the API response."""
        url = f"{self.server_url}/api/v1/chat.postMessage"
        resp = self.session.post(
            url,
            json={"channel": rid, "text": text},
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        return resp.json()

    def fetch_im_history(
        self,
        rid: str,
        oldest: str | None = None,
        count: int = 50,
    ) -> list[dict[str, Any]]:
        """Fetch recent messages from a DM room.

        When ``oldest`` is ``None``, the server returns the most recent
        ``count`` messages regardless of timestamp. When provided, only
        messages strictly newer than ``oldest`` are returned.
        """
        url = f"{self.server_url}/api/v1/im.history"
        params: dict[str, Any] = {"roomId": rid, "count": count}
        if oldest is not None:
            params["oldest"] = oldest
            params["inclusive"] = False
            params["unreads"] = False
        resp = self.session.get(url, params=params, headers=self._auth_headers())
        resp.raise_for_status()
        return resp.json().get("messages", [])

    def upload_image(
        self,
        rid: str,
        image_path: str,
        msg: str | None = None,
    ) -> dict[str, Any]:
        """Upload ``image_path`` to room ``rid`` and return the confirm response.

        Uses the Rocket.Chat 8.8 two-step flow:
          1. ``POST /api/v1/rooms.media/{rid}`` uploads the file and returns
             ``{file: {_id, url}}``.
          2. ``POST /api/v1/rooms.mediaConfirm/{rid}/{fileId}``` confirms the
             upload and posts the message. The response contains the final
             ``message`` object.

        The older ``/api/v1/rooms.upload/{rid}`` endpoint has returned 404
        since Rocket.Chat 8.0.
        """
        import mimetypes
        import os

        basename = os.path.basename(image_path)
        mime, _ = mimetypes.guess_type(image_path)
        if mime is None:
            mime = "application/octet-stream"

        media_url = f"{self.server_url}/api/v1/rooms.media/{rid}"
        with open(image_path, "rb") as fh:
            files = {"file": (basename, fh, mime)}
            media_resp = self.session.post(
                media_url,
                files=files,
                headers=self._auth_headers(),
            )
        try:
            media_resp.raise_for_status()
        except requests.HTTPError as exc:
            raise RuntimeError(
                f"upload media to room '{rid}' failed "
                f"(status {media_resp.status_code}): {exc}"
            ) from exc

        file_id = media_resp.json()["file"]["_id"]
        confirm_url = f"{self.server_url}/api/v1/rooms.mediaConfirm/{rid}/{file_id}"
        payload = {"msg": msg or "", "description": msg or ""}
        confirm_resp = self.session.post(
            confirm_url,
            json=payload,
            headers=self._auth_headers(),
        )
        try:
            confirm_resp.raise_for_status()
        except requests.HTTPError as exc:
            raise RuntimeError(
                f"confirm upload in room '{rid}' failed "
                f"(status {confirm_resp.status_code}): {exc}"
            ) from exc
        return confirm_resp.json()

    def poll_dm(
        self,
        rid: str,
        since_iso: str,
        timeout: float,
        interval: float = 1.5,
        skip_own: bool = True,
    ) -> dict[str, Any] | None:
        """Poll DM history for the first message from the peer.

        ``since_iso`` is an ISO-8601 timestamp; only messages newer than this
        are considered. Messages authored by the current user are skipped when
        ``skip_own`` is true. Deduplicates by ``_id`` and returns the first
        peer message, or ``None`` if the timeout elapses.

        Transient ``requests.RequestException`` errors (network blips, 5xx)
        are logged to stderr and retried until the deadline.
        """
        url = f"{self.server_url}/api/v1/im.history"
        deadline = time.monotonic() + timeout
        seen: set[str] = set()

        while True:
            params = {
                "roomId": rid,
                "oldest": since_iso,
                "inclusive": False,
                "count": 50,
                "unreads": False,
            }
            try:
                resp = self.session.get(
                    url, params=params, headers=self._auth_headers()
                )
                resp.raise_for_status()
            except requests.RequestException as exc:
                print(
                    f"warning: poll_dm transient error ({exc}); retrying",
                    file=sys.stderr,
                )
                if time.monotonic() >= deadline:
                    return None
                time.sleep(interval)
                continue

            messages = resp.json().get("messages", [])
            for msg in messages:
                msg_id = msg.get("_id")
                if not msg_id or msg_id in seen:
                    continue
                seen.add(msg_id)
                user_id = msg.get("u", {}).get("_id") or msg.get("userId")
                if skip_own and user_id == self.user_id:
                    continue
                return msg

            if time.monotonic() >= deadline:
                return None
            time.sleep(interval)
