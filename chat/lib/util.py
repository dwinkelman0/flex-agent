"""Small shared utilities: timestamps and message formatting.

Note: ``parse_ts`` and ``poll_until`` were removed (they had no callers
inside ``chat/``). Re-add them here if needed in future.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any


def now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


def age_seconds(ts: str) -> float:
    """Return seconds elapsed between ``ts`` (ISO-8601) and now (UTC).

    Naive inputs are treated as UTC. Raises ``ValueError`` on unparseable
    timestamps.
    """
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (datetime.now(UTC) - dt).total_seconds()


def format_message(msg: dict[str, Any]) -> str:
    """Serialize ``msg`` to a single JSON line, augmenting with ``age_seconds``.

    If the message contains a ``ts`` field (ISO-8601), the output includes a
    derived ``age_seconds`` key with the elapsed seconds since that timestamp.
    """
    out = dict(msg)
    ts = out.get("ts")
    if isinstance(ts, str):
        try:
            out["age_seconds"] = age_seconds(ts)
        except ValueError:
            pass
    return json.dumps(out, ensure_ascii=False, sort_keys=True)
