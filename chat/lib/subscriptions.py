"""Per-agent channel subscription state (gateway-owned helpers).

State file: ``agent/subscriptions.json`` (next to ``turn-origins.json``).
Both the gateway loop and the ``channel`` CLI mutate through these helpers
(atomic read-modify-write), so there is a single writer discipline even
though two processes touch the file.

Schema::

    {"channels": {key: {"status": "active"|"unhandled"|"declined",
                        "announced": bool, "baseline": iso-str|None}}}

Key forms: ``rc:dm:<peer>``, ``rc:room:<rid>``,
``gitea:issue:<owner>/<repo>#<n>``, ``gitea:repo:<owner>/<repo>``.
The dedicated DM (``rc:dm:<peer>`` matching the gateway peer) is mandatory
and cannot be unsubscribed.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

from lib import config

SUBSCRIPTIONS_PATH = config.REPO_ROOT / "agent" / "subscriptions.json"

STATUSES = ("active", "unhandled", "declined")


def _blank() -> dict[str, Any]:
    return {"channels": {}}


def load() -> dict[str, Any]:
    """Read state; missing/corrupt file yields a blank slate (never raises)."""
    try:
        raw = SUBSCRIPTIONS_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _blank()
    except OSError:
        return _blank()
    try:
        data = json.loads(raw)
    except ValueError:
        return _blank()
    if not isinstance(data, dict) or not isinstance(data.get("channels"), dict):
        return _blank()
    return data


def _save(state: dict[str, Any]) -> None:
    SUBSCRIPTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix="subscriptions.", dir=str(SUBSCRIPTIONS_PATH.parent), text=True
    )
    try:
        os.write(fd, json.dumps(state, indent=2, sort_keys=True).encode("utf-8"))
        os.fchmod(fd, 0o600)
        os.close(fd)
        os.replace(tmp, SUBSCRIPTIONS_PATH)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def is_mandatory(key: str, gateway_peer: str | None = None) -> bool:
    """The dedicated DM channel cannot be unsubscribed."""
    if gateway_peer is not None:
        return key == f"rc:dm:{gateway_peer}"
    return key.startswith("rc:dm:")


def subscribe(
    key: str, *, announced: bool = False, now_iso: str
) -> tuple[str, bool]:
    """Subscribe; returns (status, backfill_from_beginning).

    Accept-on-new (entry was announced and never decided) replays from the
    beginning. Any other (re)subscribe baselines at now — the caller (CLI)
    sets the baseline; the gateway clears it when it sees the flip for
    announced entries (see below).
    """
    state = load()
    channels = state["channels"]
    entry = channels.get(key)
    if (
        isinstance(entry, dict)
        and entry.get("status") == "unhandled"
        and entry.get("announced")
    ):
        entry["status"] = "active"
        entry["baseline"] = None
        _save(state)
        return "active", True
    channels[key] = {"status": "active", "announced": announced, "baseline": now_iso}
    _save(state)
    return "active", False


def unsubscribe(key: str, *, gateway_peer: str | None = None) -> str:
    """Unsubscribe; returns resulting status (``declined`` or ``removed``).

    Unsubscribing an undecided (announced-but-unhandled) channel records an
    explicit decline. The mandatory DM raises.
    """
    if is_mandatory(key, gateway_peer):
        raise ValueError(f"{key} is the dedicated DM and cannot be unsubscribed")
    state = load()
    channels = state["channels"]
    entry = channels.get(key)
    if isinstance(entry, dict) and entry.get("status") == "unhandled":
        entry["status"] = "declined"
        _save(state)
        return "declined"
    channels.pop(key, None)
    _save(state)
    return "removed"


def mark_announced(key: str) -> bool:
    """Record a new-channel announcement; True if newly added as unhandled."""
    state = load()
    channels = state["channels"]
    if key in channels:
        return False
    channels[key] = {"status": "unhandled", "announced": True, "baseline": None}
    _save(state)
    return True


def mark_destroyed(key: str) -> bool:
    """Drop a destroyed channel; True if an entry existed."""
    state = load()
    channels = state["channels"]
    if key not in channels:
        return False
    del channels[key]
    _save(state)
    return True


def catalog() -> list[str]:
    """Keys awaiting an explicit accept/decline decision."""
    state = load()
    channels = state["channels"]
    return sorted(
        k
        for k, v in channels.items()
        if isinstance(v, dict) and v.get("status") == "unhandled"
    )


def active() -> dict[str, dict[str, Any]]:
    """Active subscriptions keyed by channel key."""
    state = load()
    channels = state["channels"]
    return {
        k: v
        for k, v in channels.items()
        if isinstance(v, dict) and v.get("status") == "active"
    }


def set_baseline(key: str, now_iso: str) -> None:
    state = load()
    channels = state["channels"]
    entry = channels.get(key)
    if isinstance(entry, dict):
        entry["baseline"] = now_iso
        _save(state)
