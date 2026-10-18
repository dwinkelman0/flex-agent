#!/bin/sh
# docker/run-agent.sh — build the agent image and start a --rm instance.
# Usage: ./docker/run-agent.sh [--no-build] [--tag NAME] [-- <cmd>...]
#   Defaults: builds flex-agent:latest, runs interactive bash as `dev`.
#   Env files docker/global.env + docker/agent.env must exist; copy them
#   from the .example files and fill in secrets first (both gitignored).
# Env overrides:
#   IMAGE_TAG, FLEX_AGENT_DIR, UV_VERSION, OPENCODE_VERSION,
#   ROCKETCHAT_URL, GITEA_URL, OPENCODE_HOSTNAME, GITEA_WEBHOOK_BIND,
#   OPENCODE_PORT, GITEA_WEBHOOK_PORT, HOST_OPENCODE_PORT, HOST_WEBHOOK_PORT,
#   WORKSPACE_DIR
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE_TAG="${IMAGE_TAG:-flex-agent:latest}"
# Framework checkout bind-mounted to /opt/flex-agent (image is toolchain-only).
GLOBAL_ENV="$ROOT/docker/global.env"
AGENT_ENV="$ROOT/docker/agent.env"
WORKSPACE_DIR="${WORKSPACE_DIR:-$ROOT/workspace}"
# Framework checkout bind-mounted to /opt/flex-agent at runtime (the image
# carries toolchain only). Defaults to this repo checkout.
FLEX_AGENT_DIR="${FLEX_AGENT_DIR:-$ROOT}"

# Container-reachable service endpoints (defaults assume RC/Gitea on the
# Docker host; 0.0.0.0 binds make serve + webhook receiver reachable).
ROCKETCHAT_URL="${ROCKETCHAT_URL:-http://host.docker.internal:3000}"
GITEA_URL="${GITEA_URL:-http://host.docker.internal:3001}"
OPENCODE_HOSTNAME="${OPENCODE_HOSTNAME:-0.0.0.0}"
GITEA_WEBHOOK_BIND="${GITEA_WEBHOOK_BIND:-0.0.0.0}"
OPENCODE_PORT="${OPENCODE_PORT:-4096}"
GITEA_WEBHOOK_PORT="${GITEA_WEBHOOK_PORT:-3030}"
HOST_OPENCODE_PORT="${HOST_OPENCODE_PORT:-$OPENCODE_PORT}"
HOST_WEBHOOK_PORT="${HOST_WEBHOOK_PORT:-$GITEA_WEBHOOK_PORT}"

BUILD=1
while [ $# -gt 0 ]; do
  case "$1" in
    --no-build) BUILD=0; shift ;;
    --tag) IMAGE_TAG="$2"; shift 2 ;;
    --) shift; break ;;
    -h|--help)
      echo "usage: $0 [--no-build] [--tag NAME] [-- <cmd>...]" >&2
      exit 0 ;;
    *) break ;;
  esac
done
# Remaining args become the container command (default: bash).
if [ $# -eq 0 ]; then
  set -- bash
fi

command -v docker >/dev/null 2>&1 || {
  echo "error: docker not on PATH" >&2
  exit 2
}
for f in "$GLOBAL_ENV" "$AGENT_ENV"; do
  if [ ! -f "$f" ]; then
    echo "error: $f missing; copy ${f}.example and fill in values first" >&2
    exit 2
  fi
done
mkdir -p "$WORKSPACE_DIR"

if [ "$BUILD" -eq 1 ]; then
  echo "run-agent: building $IMAGE_TAG" >&2
  # shellcheck disable=SC2086
  docker build \
    ${UV_VERSION:+--build-arg "UV_VERSION=$UV_VERSION"} \
    ${OPENCODE_VERSION:+--build-arg "OPENCODE_VERSION=$OPENCODE_VERSION"} \
    -f "$ROOT/docker/Dockerfile.agent" -t "$IMAGE_TAG" "$ROOT"
fi

echo "run-agent: starting --rm instance of $IMAGE_TAG" >&2
ADD_HOST_ARG=""
if [ "$(uname -s)" = "Linux" ]; then
  # Docker Desktop resolves host.docker.internal automatically; plain
  # Linux needs the host-gateway mapping.
  ADD_HOST_ARG="--add-host host.docker.internal:host-gateway"
fi
# shellcheck disable=SC2086
docker run --rm -it \
  --env-file "$GLOBAL_ENV" \
  --env-file "$AGENT_ENV" \
  -e "ROCKETCHAT_URL=$ROCKETCHAT_URL" \
  -e "GITEA_URL=$GITEA_URL" \
  -e "OPENCODE_HOSTNAME=$OPENCODE_HOSTNAME" \
  -e "GITEA_WEBHOOK_BIND=$GITEA_WEBHOOK_BIND" \
  -e "OPENCODE_PORT=$OPENCODE_PORT" \
  -e "GITEA_WEBHOOK_PORT=$GITEA_WEBHOOK_PORT" \
  ${OPENCODE_API_KEY:+-e "OPENCODE_API_KEY=$OPENCODE_API_KEY"} \
  -p "$HOST_OPENCODE_PORT:$OPENCODE_PORT" \
  -p "$HOST_WEBHOOK_PORT:$GITEA_WEBHOOK_PORT" \
  -v "$WORKSPACE_DIR:/workspace" \
  -v "$FLEX_AGENT_DIR:/opt/flex-agent" \
  $ADD_HOST_ARG \
  "$IMAGE_TAG" "$@"
