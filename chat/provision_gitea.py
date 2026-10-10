"""Provision a Gitea agent user and persist its token to ``.agent_env``.

Mirrors ``provision.py`` (Rocket.Chat), but for Gitea:

* Target credentials (the agent user to ensure): ``USERNAME`` /
  ``PASSWORD`` from the environment, else ``.agent_env``.
* Admin client: an admin ``GITEA_TOKEN`` from the environment or the
  repo-root ``.env`` (``POST /admin/users`` needs admin scope).
* Token minting: ``POST /users/{USERNAME}/tokens`` authenticated with the
  agent's own BasicAuth password (that endpoint requires it — an admin
  token alone is not enough). The secret is returned once and persisted
  immediately to ``.agent_env`` as ``GITEA_TOKEN`` (0600, atomic).
* Idempotent: existing users are reused; an existing ``agent`` token
  (matched by name) is reused instead of minting a duplicate.

Exit codes: 0 provisioned, 2 missing configuration, 3 runtime error.
"""

from __future__ import annotations

import os
import sys

import requests
from lib import config
from requests.auth import HTTPBasicAuth

TOKEN_NAME = "agent"
TOKEN_SCOPES = ["write:repository", "write:issue", "read:user", "write:user"]


def _require_target(name: str) -> str:
    value = os.environ.get(name) or config.load_agent_env().get(name)
    if not value:
        print(
            f"error: required {name} is not set (env var or .agent_env)",
            file=sys.stderr,
        )
        sys.exit(2)
    return str(value)


def _admin_token(server: str) -> str:
    token = os.environ.get("GITEA_TOKEN") or config.load_root_env().get(
        "GITEA_TOKEN"
    )
    token = token or os.environ.get("GITEA_TOKEN")
    if not token:
        print(
            "error: admin GITEA_TOKEN missing (env or ../.env); "
            "it must belong to a site admin for user creation",
            file=sys.stderr,
        )
        sys.exit(2)
    return str(token)


def _api(
    server: str,
    method: str,
    path: str,
    *,
    token: str | None = None,
    auth: HTTPBasicAuth | None = None,
    **kwargs: object,
) -> requests.Response:
    raw_headers = kwargs.pop("headers", None)
    headers: dict[str, str] = {}
    if isinstance(raw_headers, dict):
        headers = {str(k): str(v) for k, v in raw_headers.items()}
    if token:
        headers["Authorization"] = f"token {token}"
    resp = requests.request(
        method,
        f"{server}/api/v1{path}",
        headers=headers,
        auth=auth,
        timeout=30,
        **kwargs,  # type: ignore[arg-type]
    )
    return resp


def main() -> int:
    username = _require_target("USERNAME")
    password = _require_target("PASSWORD")
    server = os.environ.get("GITEA_URL") or "http://localhost:3001"
    server = server.rstrip("/")
    admin_token = _admin_token(server)

    # 1. Ensure the user exists.
    try:
        probe = _api(server, "GET", f"/users/{username}", token=admin_token)
        if probe.status_code == 404:
            create = _api(
                server,
                "POST",
                "/admin/users",
                token=admin_token,
                json={
                    "username": username,
                    "password": password,
                    "email": config.synthetic_email(username),
                    "must_change_password": False,
                    "send_notify": False,
                },
            )
            if create.status_code == 403 and "required scope" in create.text:
                print(
                    "error: admin token lacks a required scope "
                    f"({create.text[:160]}); regenerate it with write:admin",
                    file=sys.stderr,
                )
                return 3
            create.raise_for_status()
            user = create.json()
            print(f"created Gitea user '{username}' (id={user.get('id', '?')})")
        elif not probe.ok:
            probe.raise_for_status()
            return 3
        else:
            user = probe.json()
            print(f"Gitea user '{username}' exists (id={user.get('id', '?')})")
    except requests.RequestException as exc:
        print(f"error: failed to ensure Gitea user: {exc}", file=sys.stderr)
        return 3

    # 2. Reuse an existing `agent` token if present (secret shown once only).
    try:
        existing = _api(
            server,
            "GET",
            f"/users/{username}/tokens",
            auth=HTTPBasicAuth(username, password),
        )
        existing.raise_for_status()
        names = [t.get("name") for t in existing.json()]
    except requests.RequestException as exc:
        print(f"error: failed to list tokens: {exc}", file=sys.stderr)
        return 3
    if TOKEN_NAME in names:
        print(f"token '{TOKEN_NAME}' already exists; not minting a duplicate")
        print("note: its secret cannot be re-read — ensure .agent_env holds it")
        return 0

    # 3. Mint + persist.
    try:
        minted = _api(
            server,
            "POST",
            f"/users/{username}/tokens",
            auth=HTTPBasicAuth(username, password),
            json={"name": TOKEN_NAME, "scopes": TOKEN_SCOPES},
        )
        minted.raise_for_status()
        secret = minted.json().get("sha1")
        if not secret:
            print("error: token response carried no secret", file=sys.stderr)
            return 3
    except requests.RequestException as exc:
        print(f"error: failed to mint token: {exc}", file=sys.stderr)
        return 3

    path = config.upsert_dotfile_values(
        config.AGENT_ENV_PATH, {"GITEA_TOKEN": str(secret)}
    )
    print(f"minted token '{TOKEN_NAME}' (scopes={','.join(TOKEN_SCOPES)})")
    print(f"wrote GITEA_TOKEN to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
