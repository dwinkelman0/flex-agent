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
# Opencode serve verbosity (that "global event connected" INFO line and friends
# come from opencode itself). Override with OPENCODE_LOG_LEVEL=INFO to quieten.
OPENCODE_LOG_LEVEL="${OPENCODE_LOG_LEVEL:-DEBUG}"

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
if command -v lsof >/dev/null 2>&1 && lsof -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  # A previous serve may still be winding down after Ctrl-C (its graceful
  # teardown is slow); give it a chance to free the port before failing.
  echo "start-dev: port $PORT busy, waiting up to 20s for it to free…" >&2
  for _ in $(seq 1 20); do
    sleep 1
    lsof -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 || break
  done
fi
if command -v lsof >/dev/null 2>&1 && lsof -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "error: port $PORT already in use (existing serve?)." >&2
  echo "  Stop it, or pick another: OPENCODE_PORT=4099 ./start-dev.sh $PEER" >&2
  lsof -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -5 >&2 || true
  exit 2
fi

# Initialized early: cleanup() can fire (Ctrl-C during spawn waits) before
# the gateway pids below are assigned; under `set -u` an unset variable
# would abort cleanup before it stops anything.
SERVE_PID=""
GATEWAY_PID=""
GATEWAY_PY=""

cleanup() {
  # $1 = exit code (130 for signal-driven shutdown, 0 for child-crash path).
  # Always exits: after a trapped signal we must NOT fall back into the
  # liveness loop below (it would wait on serve's slow teardown all over).
  code="${1:-0}"
  echo "start-dev: shutting down… ($(date +%H:%M:%S))" >&2
  # Order matters: the gateway must dispose its opencode session while the
  # server is still alive, so stop the gateway first, wait for it, then
  # stop serve. NOTE: $GATEWAY_PID is the `uv` wrapper — signals to it do
  # not reliably reach python, so signal the python child ($GATEWAY_PY)
  # directly; the wrapper exits once its child is gone.
  # Use TERM (not INT): TERM is never auto-ignored (backgrounded launches
  # ignore INT at the OS level), and both processes handle TERM gracefully
  # (gateway: dispose session + RC logout; serve: clean stop).
  # Empty pids are skipped (see init above).
  [ -n "$GATEWAY_PY" ] && kill -TERM "$GATEWAY_PY" 2>/dev/null || true
  [ -n "$GATEWAY_PID" ] && kill -TERM "$GATEWAY_PID" 2>/dev/null || true
  for _ in $(seq 1 15); do
    { [ -z "$GATEWAY_PY" ] || kill -0 "$GATEWAY_PY" 2>/dev/null; } || break
    { [ -z "$GATEWAY_PID" ] || kill -0 "$GATEWAY_PID" 2>/dev/null; } || break
    sleep 1
  done
  # Serve gets INT (its graceful path; TERM has produced aborts), with plain
  # TERM only as the final fallback below.
  kill -INT "$SERVE_PID" 2>/dev/null || true
  sleep 2
  kill "$SERVE_PID" "$GATEWAY_PID" 2>/dev/null || true
  # Brief reap only: a full serve teardown is slow and the script must not
  # wait on it (see preflight wait below for the restart case).
  for _ in $(seq 1 3); do
    kill -0 "$SERVE_PID" 2>/dev/null || kill -0 "$GATEWAY_PID" 2>/dev/null || break
    sleep 1
  done
  echo "start-dev: stopped (serve finishing shutdown in background) ($(date +%H:%M:%S))" >&2
  exit "$code"
}
trap 'cleanup 130' INT TERM
# No EXIT trap: cleanup runs explicitly on INT/TERM; normal `wait` return
# (a child crashing) falls through and reports below.

echo "start-dev: opencode serve (cwd=agent/, port=$PORT, peer=$PEER)" >&2
cd "$ROOT/agent"
# OPENCODE_PORT lets the rocketchat plugin tell root sessions from child
# (subagent) sessions via the local API; PEER_USERNAME is the DM target.
PEER_USERNAME="$PEER" OPENCODE_PORT="$PORT" opencode serve --port "$PORT" --hostname 127.0.0.1 --print-logs --log-level "$OPENCODE_LOG_LEVEL" &
SERVE_PID=$!
cd "$ROOT"

echo "start-dev: gateway ($PEER -> 127.0.0.1:$PORT, ephemeral, DEBUG)" >&2
PEER_USERNAME="$PEER" uv run --project "$ROOT/chat" chat/gateway.py "$PEER" \
  --opencode-url "http://127.0.0.1:$PORT" \
  --ephemeral --log-level DEBUG &
GATEWAY_PID=$!
# The python child of `uv run` (signals to the uv wrapper itself are
# unreliable — see cleanup). Give it a moment to spawn.
sleep 2
GATEWAY_PY="$(pgrep -P "$GATEWAY_PID" | head -1)"
if [ -z "$GATEWAY_PY" ]; then
  echo "start-dev: warning: gateway python child not found; signalling wrapper only" >&2
  GATEWAY_PY="$GATEWAY_PID"
fi

echo "start-dev: running (serve=$SERVE_PID gateway=$GATEWAY_PID/py=$GATEWAY_PY). Ctrl-C stops both." >&2
# If either child exits on its own, shut the other down too so nothing strays.
# (Portable `wait -n` alternative: poll liveness. The gateway is alive while
# either the uv wrapper or its python child is.)
while kill -0 "$SERVE_PID" 2>/dev/null && { kill -0 "$GATEWAY_PY" 2>/dev/null || kill -0 "$GATEWAY_PID" 2>/dev/null; }; do
  sleep 1
done
cleanup 0
