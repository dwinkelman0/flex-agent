#!/usr/bin/env python3
"""Prefix stdin lines with a UTC ISO timestamp and a process name.

Usage: python3 logprefix.py <name>

Each input line is rewritten as:

    <utc-iso-timestamp> [<name>] <original-line>

The tool is intentionally stdlib-only so it can prefix output from any
program without adding a dependency to the container image.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: logprefix.py <name>", file=sys.stderr)
        sys.exit(1)
    name = sys.argv[1]
    for line in sys.stdin:
        ts = datetime.now(timezone.utc).isoformat()
        sys.stdout.write(f"{ts} [{name}] {line}")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
