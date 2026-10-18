"""Configuration and dotfile management for the chat agent.

Handles loading environment from the repo-root ``.env`` (server + admin
credentials) and the repo-root ``.agent_env`` (agent user credentials), and
exposes helpers used by provisioning and CLI scripts.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

# chat/ directory
CHAT_DIR: Path = Path(__file__).resolve().parent.parent

# Repo root (parent of chat/)
REPO_ROOT: Path = CHAT_DIR.parent

ROOT_ENV_PATH: Path = REPO_ROOT / ".env"
AGENT_ENV_PATH: Path = REPO_ROOT / ".agent_env"

# Hosts considered local for cleartext-credential warnings.
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def load_root_env() -> dict[str, str | None]:
    """Load the repo-root ``.env`` into ``os.environ`` and return its values.

    Keys of interest: ``ROCKETCHAT_URL``, ``ROCKETCHAT_ADMIN_USER``
    (legacy ``ROCKETCHAT_USER``), ``ROCKETCHAT_ADMIN_PASSWORD`` (legacy
    ``ROCKETCHAT_PASSWORD``).
    """
    values: dict[str, str | None] = {}
    if ROOT_ENV_PATH.is_file():
        load_dotenv(ROOT_ENV_PATH, override=False)
        values = dotenv_values(ROOT_ENV_PATH)
    return values


def load_agent_env() -> dict[str, str | None]:
    """Return values from the ``.agent_env`` dotfile at the repo root.

    Keys of interest: ``ROCKETCHAT_AGENT_USERNAME`` (legacy ``USERNAME``),
    ``ROCKETCHAT_AGENT_PASSWORD`` (legacy ``PASSWORD``). Does not mutate
    ``os.environ``; callers decide whether to export. Returns ``{}`` when
    the file is missing.
    """
    if not AGENT_ENV_PATH.is_file():
        return {}
    return dotenv_values(AGENT_ENV_PATH)


def getenv_compat(
    new: str, *olds: str, dotfile: dict[str, str | None] | None = None
) -> str | None:
    """Resolve a canonical env var with deprecated fallbacks.

    Resolution order:

    1. ``new`` from ``os.environ``
    2. ``new`` from ``dotfile`` (if provided)
    3. Each old name from ``os.environ`` (in order)
    4. Each old name from ``dotfile`` (in order)

    The first truthy value wins. Returns ``None`` when nothing is set.
    """
    sources = [os.environ]
    if dotfile is not None:
        sources.append(dotfile)
    for name in (new, *olds):
        for source in sources:
            value = source.get(name)
            if value:
                return str(value)
    return None


def get_server_url() -> str:
    """Return the Rocket.Chat server URL.

    Resolution order:

    1. ``ROCKETCHAT_URL`` environment variable (if already set)
    2. ``ROCKETCHAT_URL`` from repo-root ``.env`` (loads lazily)
    3. Default ``http://localhost:3000``

    A trailing slash is stripped if present. Warns to stderr when the
    resolved URL uses plain ``http://`` against a non-local host, since
    credentials would travel in cleartext.
    """
    url = os.environ.get("ROCKETCHAT_URL")
    if not url:
        values = load_root_env()
        url = values.get("ROCKETCHAT_URL") or "http://localhost:3000"
    url = url.rstrip("/")
    lower = url.lower()
    if lower.startswith("http://"):
        host = lower.removeprefix("http://").split("/", 1)[0].split("@", 1)[-1]
        host = host.rsplit(":", 1)[0]
        host = host.strip("[]")
        if host not in _LOCAL_HOSTS:
            print(
                f"warning: ROCKETCHAT_URL={url} uses plain http to non-local host "
                f"'{host}'; credentials will be sent in cleartext",
                file=sys.stderr,
            )
    return url


def synthetic_email(username: str, domain: str | None = None) -> str:
    """Build a deterministic local-only email address for ``username``.

    ``domain`` defaults to the ``ROCKETCHAT_EMAIL_DOMAIN`` env var, falling
    back to ``"localhost"`` for backwards compatibility.
    """
    if domain is None:
        domain = os.environ.get("ROCKETCHAT_EMAIL_DOMAIN") or "localhost"
    return f"{username}@{domain}"


def upsert_dotfile_values(path: Path, values: dict[str, str]) -> Path:
    """Append or update ``values`` keys in the dotfile at ``path``.

    Returns the path written. Existing keys are updated in place;
    missing keys are appended. Atomic write with mode ``0o600``; warns
    to stderr if the existing file has loose permissions.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    # Warn if the existing file has loose perms.
    try:
        existing_mode = path.stat().st_mode & 0o077
    except FileNotFoundError:
        existing_mode = 0
    if existing_mode:
        print(
            f"warning: {path} has loose permissions (mode & 0o077 = "
            f"{oct(existing_mode)}); rewriting with 0o600",
            file=sys.stderr,
        )

    lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()

    updated = dict.fromkeys(values, False)
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            new_lines.append(line)
            continue
        key, _, _ = line.partition("=")
        key = key.strip()
        if key in values:
            new_lines.append(f"{key}={values[key]}")
            updated[key] = True
        else:
            new_lines.append(line)

    for key, seen in updated.items():
        if not seen:
            new_lines.append(f"{key}={values[key]}")

    content = "\n".join(new_lines)
    if not content.endswith("\n"):
        content += "\n"

    # Atomic write with mode 0o600.
    fd, tmp_path = tempfile.mkstemp(
        prefix=path.name + ".",
        dir=str(path.parent),
        text=True,
    )
    try:
        os.write(fd, content.encode("utf-8"))
        os.fchmod(fd, 0o600)
        os.close(fd)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return path


def write_agent_env(username: str, password: str) -> Path:
    """Append or update agent credentials in ``.agent_env``.

    Writes the canonical names ``ROCKETCHAT_AGENT_USERNAME`` /
    ``ROCKETCHAT_AGENT_PASSWORD`` and keeps the legacy ``USERNAME`` /
    ``PASSWORD`` keys for backwards compatibility. Returns the path that
    was written. See :func:`upsert_dotfile_values` for the
    atomicity/permission guarantees.
    """
    return upsert_dotfile_values(
        AGENT_ENV_PATH,
        {
            "ROCKETCHAT_AGENT_USERNAME": username,
            "ROCKETCHAT_AGENT_PASSWORD": password,
            "USERNAME": username,
            "PASSWORD": password,
        },
    )
