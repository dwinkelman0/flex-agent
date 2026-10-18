#!/usr/bin/env python3
"""Transparent HTTP forwarder for Gitea webhooks.

Listens on $LISTEN_PORT (default 3030) and forwards every request to the
gateway receiver at $FORWARD_TO.  The request method, path, query string,
all headers, and the raw request body bytes are preserved unchanged.  The
upstream response status, headers, and body are returned to the caller so
Gitea sees the real delivery result.

The proxy never stores or inspects secrets; HMAC verification stays in the
gateway (chat/lib/gitea_hooks.py).
"""
from __future__ import annotations

import http.client
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin, urlparse

FORWARD_TO = os.environ.get("FORWARD_TO", "")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "3030"))
LISTEN_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")


class ForwardHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _forward(self) -> None:
        if not FORWARD_TO:
            self.send_error(500, "FORWARD_TO not configured")
            return

        # Read the raw body bytes without decoding.  BaseHTTPRequestHandler
        # only guarantees Content-Length bodies are intact; chunked requests
        # are not supported by the stdlib server.
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)

        # FORWARD_TO is the base gateway receiver URL; the incoming path is
        # appended.  A trailing slash is required for urljoin to append the
        # path rather than replace it, so normalize here.
        base = FORWARD_TO if FORWARD_TO.endswith("/") else FORWARD_TO + "/"
        target = urljoin(base, self.path.lstrip("/"))

        parsed = urlparse(target)
        if parsed.scheme not in ("http", "https"):
            self.send_error(500, "FORWARD_TO must be http or https")
            return

        conn_class = (
            http.client.HTTPSConnection
            if parsed.scheme == "https"
            else http.client.HTTPConnection
        )
        host = parsed.hostname or "localhost"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)

        # Forward the relative path + query so the request line looks like a
        # normal proxy delivery, not a full-URL CONNECT-style request.
        target_path = parsed.path or "/"
        if parsed.query:
            target_path = target_path + "?" + parsed.query

        try:
            conn = conn_class(host, port, timeout=30)
            # Forward all headers verbatim; X-Gitea-* headers are included
            # because we do not filter the header set.
            headers = dict(self.headers.items())
            conn.request(self.command, target_path, body=body, headers=headers)
            resp = conn.getresponse()
            resp_body = resp.read()
            conn.close()

            self.send_response(resp.status)
            # Hop-by-hop headers must not be forwarded.
            for header, value in resp.getheaders():
                lower = header.lower()
                if lower in (
                    "transfer-encoding",
                    "content-length",
                    "connection",
                    "keep-alive",
                    "upgrade",
                ):
                    continue
                self.send_header(header, value)
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)
        except Exception as exc:  # noqa: BLE001
            self.send_error(502, f"forward failed: {exc}")

    def do_GET(self) -> None:
        self._forward()

    def do_POST(self) -> None:
        self._forward()

    def do_PUT(self) -> None:
        self._forward()

    def do_PATCH(self) -> None:
        self._forward()

    def do_DELETE(self) -> None:
        self._forward()

    def do_HEAD(self) -> None:
        self._forward()

    def do_OPTIONS(self) -> None:
        self._forward()

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[proxy] {self.address_string()} - {fmt % args}", file=sys.stderr)


def main() -> None:
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), ForwardHandler)
    print(
        f"[proxy] listening on {LISTEN_HOST}:{LISTEN_PORT} -> {FORWARD_TO}",
        file=sys.stderr,
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
