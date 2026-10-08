#!/bin/sh
# start-dev.sh — debug startup for the RocketChat <> opencode loop.
# Locks in the configuration verified end-to-end (Oct 8):
#   - opencode serve runs with cwd=agent/ (so agent/.opencode loads),
#     --print-logs --log-level DEBUG, PEER_USERNAME set.
#   - gateway runs with --ephemeral --log-level DEBUG against that server.
# Usage: ./start-dev.sh [PEER]   (default PEER=dwinkelman, PORT via OPENCODE_PORT or 4096)
set -eu

ROOT="$(cd "$(dirname "$0")" && pwd)"
PEER="${1:-dwinkelman}"
PORT="${OPENCODE_PORT:-4096}"

command -v uv >/dev/null 2>&1 || {
  echo "error: uv not on PATH" >&2
  exit 2
}
if [ ! -f "$ROOT/chat/pyproject.toml" ]; then
  echo "error: $ROOT/chat/pyproject.toml missing" >&2
  exit 2
fi
if [ ! -f "$ROOT/.agent_env" ]; then
  echo "error: $ROOT/.agent_env missing; define USERNAME/PASSWORD there first" >&2
  exit 2
fi
command -v opencode >/dev/null 2>&1 || {
  echo "error: opencode not on PATH" >&2
  exit 2
}

cleanup() {
  echo "start-dev: shutting down…" >&2
  kill "$SERVE_PID" "$GATEWAY_PID" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

echo "start-dev: opencode serve (cwd=agent/, port=$PORT, peer=$PEER)" >&2
cd "$ROOT/agent"
PEER_USERNAME="$PEER" opencode serve --port "$PORT" --hostname 127.0.0.1 --print-logs --log-level DEBUG &
SERVE_PID=$!
cd "$ROOT"

echo "start-dev: gateway ($PEER -> 127.0.0.1:$PORT, ephemeral, DEBUG)" >&2
PEER_USERNAME="$PEER" uv run --project "$ROOT/chat" chat/gateway.py "$PEER" \
  --opencode-url "http://127.0.0.1:$PORT" \
  --ephemeral --log-level DEBUG &
GATEWAY_PID=$!

echo "start-dev: running (serve=$SERVE_PID gateway=$GATEWAY_PID). Ctrl-C stops both." >&2
wait
