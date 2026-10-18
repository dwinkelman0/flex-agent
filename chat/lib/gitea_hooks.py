"""Gitea webhook receiver: push notifications instead of polling.

Runs a stdlib ``http.server`` in a daemon thread (no new dependencies).
Gitea POSTs ``gitea``-type deliveries to ``/hooks/gitea``; each delivery
is HMAC-SHA256 verified against the shared secret
(``X-Gitea-Signature`` header) and normalized into channel items the
gateway dispatches like poll hits. Polling stays as fallback/reconciler.

Covered events: ``issue_comment`` (issues + PR timeline comments),
``issues`` (opened/closed), ``pull_request`` (opened/synchronize/closed),
``push`` (per-commit, branch-filtered by ``branch_filter`` when set).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Check ``X-Gitea-Signature`` (hex HMAC-SHA256 of the raw body)."""
    if not header:
        return False
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, header.strip().lower())


def _user(payload: dict[str, Any]) -> dict[str, Any]:
    sender = payload.get("sender") or {}
    return {"login": sender.get("login") or "unknown"}


def normalize_event(
    event: str, payload: dict[str, Any], branch_filter: str | None = None
) -> tuple[str, list[dict[str, Any]]]:
    """Map a delivery to (channel_key, items); empty items = nothing to do.

    Items mimic Gitea comment dicts (``id``/``body``/``user``/``created_at``)
    so existing prompt shaping applies. Push commits synthesize
    ``gitea-commit-<sha>`` string ids.
    """
    repo = (payload.get("repository") or {}).get("full_name") or ""
    if event == "issue_comment":
        issue = payload.get("issue") or {}
        number = issue.get("number")
        comment = payload.get("comment") or {}
        if not repo or not isinstance(number, int) or not comment:
            return "", []
        return f"gitea:{repo}#{number}", [comment]
    if event == "issues":
        issue = payload.get("issue") or {}
        number = issue.get("number")
        action = payload.get("action") or ""
        if not repo or not isinstance(number, int):
            return "", []
        if action not in ("opened", "closed", "reopened"):
            return "", []
        return f"gitea:{repo}#{number}", [
            {
                "id": f"issue-{number}-{action}",
                "body": f"[{action}] {issue.get('title') or ''}\n\n{issue.get('body') or ''}".strip(),
                "user": _user(payload),
                "created_at": issue.get("created_at") or "",
            }
        ]
    if event == "pull_request":
        pr = payload.get("pull_request") or {}
        number = pr.get("number")
        action = payload.get("action") or ""
        if not repo or not isinstance(number, int):
            return "", []
        if action not in ("opened", "synchronize", "closed", "reopened"):
            return "", []
        merged = " (merged)" if (pr.get("merged") or False) else ""
        return f"gitea:{repo}#{number}", [
            {
                "id": f"pr-{number}-{action}-{pr.get('updated_at') or ''}",
                "body": f"[pull_request {action}{merged}] {pr.get('title') or ''}\n\n{pr.get('body') or ''}".strip(),
                "user": _user(payload),
                "created_at": pr.get("updated_at") or "",
            }
        ]
    if event == "push":
        ref = payload.get("ref") or ""
        branch = ref.split("/")[-1] if ref else ""
        if branch_filter and branch != branch_filter:
            return "", []
        if not repo:
            return "", []
        items = []
        for commit in payload.get("commits") or []:
            sha = commit.get("id") or ""
            author = ((commit.get("author") or {}).get("username")
                      or (commit.get("author") or {}).get("name")
                      or "unknown")
            items.append(
                {
                    "id": f"gitea-commit-{sha}",
                    "body": f"[push {branch} {sha[:8]}] {commit.get('message') or ''}".strip(),
                    "user": {"login": author},
                    "created_at": commit.get("timestamp") or "",
                }
            )
        return f"gitea:{repo}@{branch}" if branch else f"gitea:{repo}", items
    return "", []


class _Handler(BaseHTTPRequestHandler):
    secret: str = ""
    on_event: Callable[[str, str, dict[str, Any]], None] = lambda *a: None

    def log_message(self, format: str, *args: object) -> None:  # keep logs ours
        return

    def do_POST(self) -> None:
        if self.path.split("?")[0] != "/hooks/gitea":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else b""
        event = self.headers.get("X-Gitea-Event") or ""
        if not verify_signature(
            type(self).secret, body, self.headers.get("X-Gitea-Signature")
        ):
            self.send_response(401)
            self.end_headers()
            return
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except ValueError:
            self.send_response(400)
            self.end_headers()
            return
        if not isinstance(payload, dict):
            self.send_response(400)
            self.end_headers()
            return
        try:
            type(self).on_event(event, self.headers.get("X-Gitea-Delivery") or "", payload)
        except Exception as exc:  # noqa: BLE001 -- boundary: never break delivery ack
            print(f"warning: webhook handler failed: {exc}", file=sys.stderr)
        self.send_response(200)
        self.end_headers()


def start_receiver(
    port: int,
    secret: str,
    on_event: Callable[[str, str, dict[str, Any]], None],
    host: str = "127.0.0.1",
) -> threading.Thread:
    """Serve ``/hooks/gitea`` in a daemon thread; returns the thread."""
    handler = type(
        "GiteaHookHandler",
        (_Handler,),
        {"secret": secret, "on_event": staticmethod(on_event)},
    )
    server = HTTPServer((host, port), handler)

    def _serve() -> None:
        try:
            server.serve_forever(poll_interval=0.5)
        except Exception as exc:  # noqa: BLE001 -- boundary: daemon thread must not crash silently
            print(f"warning: webhook receiver stopped: {exc}", file=sys.stderr)

    thread = threading.Thread(target=_serve, daemon=True, name="gitea-hooks")
    thread.start()
    return thread
