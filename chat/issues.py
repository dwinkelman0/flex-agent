"""Gitea issue executable for the demo loop (and later, agents).

Usage::

    python issues.py show OWNER/REPO#N
    python issues.py comment OWNER/REPO#N "text"
    python issues.py reply OWNER/REPO#N "text" [--quote COMMENT_ID]

Authentication: ``GITEA_TOKEN`` env var, repo-root ``.env``, or the
per-agent token in ``.agent_env`` (see ``provision_gitea.py``).
Server: ``GITEA_URL`` env or ``http://localhost:3001``.

Gitea issue comments are flat (no threading); ``reply`` posts a normal
comment, quoting the parent body when ``--quote`` is given.
"""

from __future__ import annotations

import json
import os
import sys

import click
import requests
from lib import config


def _resolve_server(server: str | None) -> str:
    if server:
        return server.rstrip("/")
    return os.environ.get("GITEA_URL") or "http://localhost:3001"


def _resolve_token() -> str:
    # Precedence: env, then per-agent .agent_env (provision_gitea.py),
    # then root .env (usually the admin token). Agent identity wins when
    # provisioned; admin token remains the fallback.
    token = (
        config.getenv_compat("GITEA_TOKEN", dotfile=config.load_agent_env())
        or config.getenv_compat("GITEA_TOKEN", dotfile=config.load_root_env())
    )
    if not token:
        click.echo(
            "error: GITEA_TOKEN not found; set env, run provision_gitea.py "
            "for a per-agent token, or define it in ../.env",
            err=True,
        )
        sys.exit(2)
    return str(token)


def _split_target(target: str) -> tuple[str, str, int]:
    """Parse OWNER/REPO#N into (owner, repo, number)."""
    try:
        repo_part, num_part = target.rsplit("#", 1)
        owner, repo = repo_part.split("/", 1)
        return owner, repo, int(num_part)
    except ValueError:
        raise click.UsageError(
            f"TARGET must look like OWNER/REPO#N, got {target!r}."
        ) from None


def _api(
    server: str, token: str, method: str, path: str, **kwargs: object
) -> requests.Response:
    resp = requests.request(
        method,
        f"{server}/api/v1{path}",
        headers={"Authorization": f"token {token}"},
        timeout=30,
        **kwargs,  # type: ignore[arg-type]
    )
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Gitea {method} {path} failed ({resp.status_code}): "
            f"{resp.text[:200]}"
        )
    return resp


@click.group()
def cli() -> None:
    """Post and read Gitea issue comments."""


def _server_option(fn):  # type: ignore[no-untyped-def]
    return click.option(
        "--server", default=None, help="Gitea base URL."
    )(fn)


@cli.command("show")
@click.argument("target")
@_server_option
def show(target: str, server: str | None) -> None:
    """Print issue TARGET as JSON (issue + its comments)."""
    server_url = _resolve_server(server)
    token = _resolve_token()
    owner, repo, number = _split_target(target)
    issue = _api(server_url, token, "GET", f"/repos/{owner}/{repo}/issues/{number}").json()
    comments = _api(
        server_url, token, "GET", f"/repos/{owner}/{repo}/issues/{number}/comments"
    ).json()
    click.echo(json.dumps({"issue": issue, "comments": comments}, sort_keys=True))


@cli.command("comment")
@click.argument("target")
@click.argument("body")
@_server_option
def comment(target: str, body: str, server: str | None) -> None:
    """Post BODY as a comment on TARGET. Prints the comment id."""
    server_url = _resolve_server(server)
    token = _resolve_token()
    owner, repo, number = _split_target(target)
    resp = _api(
        server_url,
        token,
        "POST",
        f"/repos/{owner}/{repo}/issues/{number}/comments",
        json={"body": body},
    ).json()
    click.echo(str(resp.get("id", "")))


@cli.command("reply")
@click.argument("target")
@click.argument("body")
@click.option(
    "--quote",
    "quote_id",
    default=None,
    type=int,
    help="Comment id to quote at the top of the reply.",
)
@_server_option
def reply(target: str, body: str, quote_id: int | None, server: str | None) -> None:
    """Reply to TARGET (optionally quoting COMMENT_ID). Prints comment id."""
    server_url = _resolve_server(server)
    token = _resolve_token()
    owner, repo, number = _split_target(target)
    text = body
    if quote_id is not None:
        comments = _api(
            server_url, token, "GET", f"/repos/{owner}/{repo}/issues/{number}/comments"
        ).json()
        parent = next((c for c in comments if c.get("id") == quote_id), None)
        if parent is None:
            raise click.ClickException(f"no comment id={quote_id} on {target}")
        quoted = str(parent.get("body") or "").strip().replace("\n", "\n> ")
        text = f"> {quoted}\n\n{body}" if quoted else body
    resp = _api(
        server_url,
        token,
        "POST",
        f"/repos/{owner}/{repo}/issues/{number}/comments",
        json={"body": text},
    ).json()
    click.echo(str(resp.get("id", "")))


if __name__ == "__main__":
    cli()
