"""Channel discovery, polling, backfill, and destroy-checks.

Covers every channel kind EXCEPT the mandatory DM (owned by the main
loop) and the demo ``--issue`` path (kept as-is until the dynamic
engine subsumes it). All helpers are transport-thin: fetch, shape to
plain dicts, let the caller dispatch.
"""

from __future__ import annotations

from typing import Any

import requests

ROOM_HISTORY = {
    "c": "channels.history",
    "p": "groups.history",
    "d": "im.history",
}


def discover_rc_rooms(client: Any) -> list[dict[str, str]]:
    """Non-DM rooms the agent is in: [{key, rid, type, name}]."""
    resp = client.session.get(
        client.server_url + "/api/v1/subscriptions.get",
        headers=client._auth_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    out = []
    for sub in resp.json().get("update", []):
        rtype = sub.get("t") or ""
        if rtype == "d":
            continue
        rid = sub.get("rid") or ""
        if not rid:
            continue
        out.append(
            {
                "key": f"rc:room:{rid}",
                "rid": rid,
                "type": rtype,
                "name": sub.get("name") or rid,
            }
        )
    return out


def discover_gitea_issues(server: str, token: str) -> list[dict[str, Any]]:
    """Open issues assigned to the token user: [{key, owner, repo, number}]."""
    resp = requests.get(
        f"{server}/api/v1/repos/issues/search",
        params={"assigned": "true", "state": "open", "limit": 50},
        headers={"Authorization": f"token {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    if isinstance(body, list):
        items = body
    elif isinstance(body, dict):
        items = body.get("data") or body.get("issues") or []
    else:
        items = []
    out = []
    for iss in items:
        repo = (iss.get("repository") or {}).get("full_name") or ""
        number = iss.get("number")
        if not repo or not isinstance(number, int) or "/" not in repo:
            continue
        out.append(
            {
                "key": f"gitea:issue:{repo}#{number}",
                "owner": repo.split("/", 1)[0],
                "repo": repo.split("/", 1)[1],
                "number": number,
                "title": iss.get("title") or "",
            }
        )
    return out


def _room_history(
    client: Any, rid: str, rtype: str, oldest: str | None, count: int = 50
) -> list[dict[str, Any]]:
    endpoint = ROOM_HISTORY.get(rtype, "channels.history")
    params: dict[str, Any] = {"roomId": rid, "count": count}
    if oldest is not None:
        params.update({"oldest": oldest, "inclusive": "false", "unreads": "false"})
    resp = client.session.get(
        f"{client.server_url}/api/v1/{endpoint}",
        params=params,
        headers=client._auth_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("messages", [])


def poll_rc_room(
    client: Any, rid: str, rtype: str, oldest: str, self_username: str
) -> list[dict[str, Any]]:
    """New peer messages in a room since ``oldest`` (own/system dropped)."""
    return [
        m
        for m in _room_history(client, rid, rtype, oldest)
        if m.get("t") is None
        and ((m.get("u") or {}).get("username") or "") != self_username
    ]


def backfill_rc_room(
    client: Any, rid: str, rtype: str, self_username: str, cap: int = 500
) -> list[dict[str, Any]]:
    """Oldest-first peer messages from the beginning (up to ``cap``)."""
    seen_pages: list[dict[str, Any]] = []
    oldest: str | None = None
    while len(seen_pages) < cap:
        page = _room_history(client, rid, rtype, oldest)
        if not page:
            break
        seen_pages = page + seen_pages
        oldest = min(m.get("ts") or "" for m in page)
        if len(page) < 50:
            break
    return [
        m
        for m in sorted(seen_pages, key=lambda m: m.get("ts") or "")
        if m.get("t") is None
        and ((m.get("u") or {}).get("username") or "") != self_username
    ][:cap]


def check_rc_room(client: Any, rid: str) -> bool:
    """True if the room still exists (destroy detection).

    Deleted rooms answer 400 error-room-not-found (not 404), so both
    count as destroyed. Anything else (network blips, 429s) is treated
    as alive — transient failure is not destruction.
    """
    try:
        resp = client.session.get(
            client.server_url + "/api/v1/rooms.info",
            params={"roomId": rid},
            headers=client._auth_headers(),
            timeout=30,
        )
        if resp.status_code == 404:
            return False
        if resp.status_code == 400 and "room-not-found" in resp.text:
            return False
        resp.raise_for_status()
        return True
    except requests.RequestException:
        return True  # transient failure is not destruction


def _issue_comments(
    server: str, token: str, owner: str, repo: str, number: int
) -> list[dict[str, Any]]:
    resp = requests.get(
        f"{server}/api/v1/repos/{owner}/{repo}/issues/{number}/comments",
        headers={"Authorization": f"token {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    comments = resp.json()
    if not isinstance(comments, list):
        raise TypeError(f"unexpected comments payload: {comments!r}")
    return sorted(comments, key=lambda c: c.get("id") or 0)


def poll_gitea_issue(
    server: str, token: str, owner: str, repo: str, number: int, after_id: int
) -> list[dict[str, Any]]:
    """Issue comments with id above ``after_id``."""
    return [
        c
        for c in _issue_comments(server, token, owner, repo, number)
        if isinstance(c.get("id"), int) and int(c["id"]) > after_id
    ]


def backfill_gitea_issue(
    server: str, token: str, owner: str, repo: str, number: int
) -> list[dict[str, Any]]:
    """All comments on the issue, oldest first."""
    return _issue_comments(server, token, owner, repo, number)


def check_gitea_issue(server: str, token: str, owner: str, repo: str, number: int) -> bool:
    """True if the issue still exists (destroy detection)."""
    try:
        resp = requests.get(
            f"{server}/api/v1/repos/{owner}/{repo}/issues/{number}",
            headers={"Authorization": f"token {token}"},
            timeout=30,
        )
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        return True
    except requests.RequestException:
        return True  # transient failure is not destruction


def parse_issue_key(key: str) -> tuple[str, str, int] | None:
    """``gitea:issue:o/r#n`` -> (owner, repo, number); else None."""
    if not key.startswith("gitea:issue:"):
        return None
    try:
        repo_part, num_part = key[len("gitea:issue:"):].rsplit("#", 1)
        owner, repo = repo_part.split("/", 1)
        return owner, repo, int(num_part)
    except ValueError:
        return None


def parse_rc_room_key(key: str) -> str | None:
    """``rc:room:<rid>`` -> rid; else None."""
    if not key.startswith("rc:room:"):
        return None
    return key[len("rc:room:"):] or None


def control_prompt(kind: str, key: str, detail: str = "") -> str:
    """Prompt body for lifecycle control messages (never data)."""
    if kind == "available":
        return (
            f"[control: channel-available] key={key} {detail}\n"
            "A new channel is available. Accept it with "
            f"`channel subscribe {key}` (replays history from the beginning) "
            f"or decline with `channel unsubscribe {key}`. Undecided channels "
            "stay listed under `channel catalog`. Silence decides nothing — "
            "an unaccepted channel never dispatches."
        )
    if kind == "destroyed":
        return (
            f"[control: channel-destroyed] key={key} {detail}\n"
            "That channel no longer exists; its subscription was removed. "
            "No action needed unless you kept state referencing it."
        )
    raise ValueError(f"unknown control kind: {kind}")
