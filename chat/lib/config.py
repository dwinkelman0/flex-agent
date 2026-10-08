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

    Keys of interest: ``ROCKETCHAT_URL``, ``ROCKETCHAT_USER``,
    ``ROCKETCHAT_PASSWORD``.
    """
    values: dict[str, str | None] = {}
    if ROOT_ENV_PATH.is_file():
        load_dotenv(ROOT_ENV_PATH, override=False)
        values = dotenv_values(ROOT_ENV_PATH)
    return values


def load_agent_env() -> dict[str, str | None]:
    """Return values from the ``.agent_env`` dotfile at the repo root.

    Keys of interest: ``USERNAME``, ``PASSWORD``. Does not mutate
    ``os.environ``; callers decide whether to export. Returns ``{}`` when
    the file is missing.
    """
    if not AGENT_ENV_PATH.is_file():
        return {}
    return dotenv_values(AGENT_ENV_PATH)


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


def write_agent_env(username: str, password: str) -> Path:
    """Append or update ``USERNAME`` and ``PASSWORD`` in ``.agent_env``.

    Returns the path that was written. Existing keys are updated in place;
    missing keys are appended as new lines. The file is created with mode
    ``0o600`` (owner read/write only). Writes are atomic: content goes to a
    temp file on the same filesystem and is renamed into place. If the file
    already exists with loose permissions (any of the lower 7 bits set), a
    warning is emitted to stderr.
    """
    AGENT_ENV_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Warn if the existing file has loose perms.
    try:
        existing_mode = AGENT_ENV_PATH.stat().st_mode & 0o077
    except FileNotFoundError:
        existing_mode = 0
    if existing_mode:
        print(
            f"warning: {AGENT_ENV_PATH} has loose permissions (mode & 0o077 = "
            f"{oct(existing_mode)}); rewriting with 0o600",
            file=sys.stderr,
        )

    lines: list[str] = []
    if AGENT_ENV_PATH.is_file():
        lines = AGENT_ENV_PATH.read_text(encoding="utf-8").splitlines()

    updated = {"USERNAME": False, "PASSWORD": False}
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            new_lines.append(line)
            continue
        if "=" not in line:
            new_lines.append(line)
            continue
        key, _, _ = line.partition("=")
        key = key.strip()
        if key == "USERNAME":
            new_lines.append(f"USERNAME={username}")
            updated["USERNAME"] = True
        elif key == "PASSWORD":
            new_lines.append(f"PASSWORD={password}")
            updated["PASSWORD"] = True
        else:
            new_lines.append(line)

    if not updated["USERNAME"]:
        new_lines.append(f"USERNAME={username}")
    if not updated["PASSWORD"]:
        new_lines.append(f"PASSWORD={password}")

    content = "\n".join(new_lines)
    if not content.endswith("\n"):
        content += "\n"

    # Atomic write with mode 0o600.
    fd, tmp_path = tempfile.mkstemp(
        prefix=".agent_env.",
        dir=str(AGENT_ENV_PATH.parent),
        text=True,
    )
    try:
        os.write(fd, content.encode("utf-8"))
        os.fchmod(fd, 0o600)
        os.close(fd)
        os.replace(tmp_path, AGENT_ENV_PATH)
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
    return AGENT_ENV_PATH
