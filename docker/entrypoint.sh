#!/bin/sh
# docker/entrypoint.sh — runtime setup for the flex-agent container.
# Secret-safe: this script never prints values that could be credentials.

set -eu

# Shared opencode serve password: serve honors OPENCODE_SERVER_PASSWORD from
# the environment (else it generates one and prints it to its log), and the
# gateway Basic-auths with the same value. Generate when unset (mirrors
# start-dev.sh) and export so supervised children inherit it. Never logged.
if [ -z "${OPENCODE_SERVER_PASSWORD:-}" ]; then
    OPENCODE_SERVER_PASSWORD="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    export OPENCODE_SERVER_PASSWORD
fi

# Provider auth for serve: opencode reads $HOME/.local/share/opencode/auth.json.
# If the caller mounted one, leave it alone; else mint it from
# OPENCODE_API_KEY when set. Never logged.
_AUTH_DIR="${HOME:-/home/dev}/.local/share/opencode"
if [ ! -f "$_AUTH_DIR/auth.json" ] && [ -n "${OPENCODE_API_KEY:-}" ]; then
    mkdir -p "$_AUTH_DIR"
    printf '{"opencode-go": {"type": "api", "key": "%s"}}' "$OPENCODE_API_KEY" > "$_AUTH_DIR/auth.json"
    chmod 600 "$_AUTH_DIR/auth.json"
fi
unset _AUTH_DIR

# Ensure the user workspace exists (it is commonly a volume mount).
mkdir -p /workspace

# Mark the framework clone and the user workspace as git safe directories.
git config --global --add safe.directory /opt/flex-agent 2>/dev/null || true
git config --global --add safe.directory /workspace 2>/dev/null || true

# If callers mounted /opt/flex-agent/.env or /opt/flex-agent/.agent_env, use
# them as-is. This image never creates, generates, or writes secret files.

# One-line effective config: hostnames/ports only, NEVER secret values
# (credential keys are reported as <set>/<unset>, values never printed).
if [ -n "${OPENCODE_API_KEY:-}" ]; then
    _apikey="<set>"
else
    _apikey="<unset>"
fi
printf '[entrypoint] ROCKETCHAT_URL=%s GITEA_URL=%s OPENCODE_HOSTNAME=%s OPENCODE_PORT=%s GITEA_WEBHOOK_BIND=%s GITEA_WEBHOOK_PORT=%s OPENCODE_API_KEY=%s\n' \
    "${ROCKETCHAT_URL:-<unset>}" \
    "${GITEA_URL:-<unset>}" \
    "${OPENCODE_HOSTNAME:-<unset>}" \
    "${OPENCODE_PORT:-<unset>}" \
    "${GITEA_WEBHOOK_BIND:-<unset>}" \
    "${GITEA_WEBHOOK_PORT:-<unset>}" \
    "$_apikey" >&2
unset _apikey

# First-boot dependency warming. The image carries toolchain only; the
# framework itself is a host bind-mount, so repo deps build here on first
# run (later boots skip via these markers). Warnings only — supervision
# still starts if warming fails.
if [ -f /opt/flex-agent/chat/pyproject.toml ] && [ ! -d /opt/flex-agent/chat/.venv ]; then
    echo "[entrypoint] warming chat venv…" >&2
    uv sync --project /opt/flex-agent/chat >&2 || echo "[entrypoint] warning: uv sync failed" >&2
fi
if [ -f /opt/flex-agent/agent/.opencode/package.json ] && [ ! -d /opt/flex-agent/agent/.opencode/node_modules ]; then
    echo "[entrypoint] installing plugin deps…" >&2
    (cd /opt/flex-agent/agent/.opencode && npm ci --no-audit --no-fund >&2) || echo "[entrypoint] warning: npm ci failed" >&2
fi
# serve resolves plugin deps from its cwd: link the tree where it looks.
if [ -d /opt/flex-agent/agent/.opencode/node_modules ] && [ ! -e /opt/flex-agent/agent/node_modules ]; then
    ln -s .opencode/node_modules /opt/flex-agent/agent/node_modules 2>/dev/null || true
fi

# Default (no args) or explicit --supervise starts the container supervisor.
if [ $# -eq 0 ] || [ "$1" = "--supervise" ]; then
    exec supervisord -n -c /opt/docker/supervisord.conf
fi

# Any other argument (e.g. "bash") runs interactively, bypassing supervision.
exec "$@"
