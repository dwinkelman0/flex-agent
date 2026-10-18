# Docker Setup

This repo defines two Docker Compose stacks that run on a local Colima VM,
plus a containerized `flex-agent` runtime and a Gitea webhook proxy.

## Runtime Environment

- **VM**: Colima on macOS, using Virtualization.Framework (`aarch64`), Docker runtime
- **Profile**: `~/.colima/default/colima.yaml`
- **Resources**: 2 CPU / 2 GiB RAM / 100 GiB disk
- `docker info` reports ~1.91 GiB of usable memory — a deliberate ~2 GB budget.

## Stacks

### 1. Rocket.Chat (`rocketchat-compose.yml`)

Project name: `rocketchat-minimal`

| Service | Image | Notes |
|---|---|---|
| `mongodb` | `mongodb/mongodb-community-server:8.2-ubi8` | Replica set `rs0`, WiredTiger cache 0.256 GB, memory cap 384 m, healthcheck enabled |
| `mongodb-init-replica` | (one-shot from same image) | Initializes `rs0` then exits |
| `nats` | `nats:2.11-alpine` | Memory cap 64 m |
| `rocketchat` | `registry.rocket.chat/rocketchat/rocket.chat:8.8.0` | Memory cap 960 m, `NODE_OPTIONS` heap 720, `TRANSPORTER=monolith+nats` |

- **Port**: `3000` → Rocket.Chat web UI
- **Volumes**:
  - `mongodb_data` — **EXTERNAL named volume** (`rocketchat-compose_mongodb_data`) containing pre-existing data. **DO NOT DELETE.**
  - `rocketchat-minimal_rocketchat_uploads` — Rocket.Chat uploads
- No `env_file` usage.

### 2. Gitea (`gitea-compose.yml`)

Project name: `flex-agent`

| Service | Image | Notes |
|---|---|---|
| `gitea` | `gitea/gitea:latest` | SQLite (no DB sidecar), memory cap 320 m |

- **Ports**:
  - `3001:3000` → Gitea web (host `3000` is Rocket.Chat)
  - `2222:22` → Gitea SSH (host `22` is system `sshd`)
- **Volume**: `gitea_data`

## Port Map

| Host Port | Service |
|---|---|
| `3000` | Rocket.Chat |
| `3001` | Gitea web |
| `2222` | Gitea SSH |
| `4096` | opencode serve API inside the agent container |
| `3030` | Gitea webhook receiver / proxy inside the agent container |

**Not running in Docker**:
- `opencode serve` — host process on an ephemeral `127.0.0.1` port (also available inside the agent image)
- `:3030` webhook receiver — only active when the gateway runs with `--webhook-port`
- GitLab — fully removed; `8081`/`8022` are free

## Agent Image

Build the agent image from the repo root (toolchain only — the framework
itself is bind-mounted from the host at runtime, so no clone args):

```bash
docker build -f docker/Dockerfile.agent -t flex-agent:latest .
```

### Build args

| Arg | Default | Description |
|---|---|---|
| `UV_VERSION` | `0.13.0` | astral-sh/uv release to install |
| `OPENCODE_VERSION` | `2.0.26` | opencode release to install (via https://opencode.ai/v2/install) |

> **Host-mount note**: `/opt/flex-agent` is a bind mount of your checkout
> (`FLEX_AGENT_DIR`, default: the repo you run from). First boot warms
> repo deps into the mount (`uv sync`, `npm ci` — see
> `docker/entrypoint.sh`); later boots skip via markers. (Plan: flip back
> to a clone-based stable image once that lands.)

Both tools are installed system-wide into `/usr/local/bin` so they remain on
PATH after the image switches to the non-root `dev` user.

### Environment convention

The image expects two env files. Example files are maintained by the
environment workstream:

- `docker/global.env.example` — service URLs and ports (non-secret)
- `docker/agent.env.example` — credentials and tokens

Copy or rename them (without the `.example` suffix) and mount them at runtime.
The entrypoint never creates or modifies secret files; if you mount
`/opt/flex-agent/.env` or `/opt/flex-agent/.agent_env`, those are used as-is.

Shortcut — `docker/run-agent.sh` builds and starts a `--rm` instance in
one step (see `--help` for `--no-build` / `--tag` / command overrides):

```bash
./docker/run-agent.sh
```

```bash
docker run --rm -it \
  --env-file docker/global.env \
  --env-file docker/agent.env \
  -p 4096:4096 -p 3030:3030 \
  -v "$(pwd)/workspace:/workspace" \
  -v "$(pwd):/opt/flex-agent" \
  flex-agent:latest
```

### Service URLs from inside the container

When the container talks to Rocket.Chat/Gitea running on the Docker host,
override the default `localhost` URLs:

```bash
ROCKETCHAT_URL=http://host.docker.internal:3000 \
GITEA_URL=http://host.docker.internal:3001 \
OPENCODE_HOSTNAME=0.0.0.0 \
GITEA_WEBHOOK_BIND=0.0.0.0 \
  docker run --rm -it \
    --env-file docker/global.env \
    --env-file docker/agent.env \
    -p 4096:4096 -p 3030:3030 \
    -v "$(pwd):/opt/flex-agent" \
    flex-agent:latest
```

- `OPENCODE_HOSTNAME=0.0.0.0` lets `opencode serve` bind its API so the gateway
  inside the container can reach it.
- `GITEA_WEBHOOK_BIND=0.0.0.0` does the same for the gateway's
  `--webhook-bind` receiver.

If your checkout of `chat/gateway.py` does not yet support `--webhook-bind`,
use `--webhook-port` alone (note that older versions may bind `127.0.0.1`
only).

### Running the gateway inside the container

Once inside the container, the framework is at `/opt/flex-agent` and the
runtime workspace is `/workspace`:

```bash
opencode serve --port "${OPENCODE_PORT:-4096}" --hostname "${OPENCODE_HOSTNAME:-127.0.0.1}" &

uv run --project /opt/flex-agent/chat /opt/flex-agent/chat/gateway.py "$PEER" \
  --opencode-url "http://127.0.0.1:${OPENCODE_PORT:-4096}" \
  --issue dwinkelman/demo#1 \
  --webhook-bind "${GITEA_WEBHOOK_BIND:-0.0.0.0}" \
  --webhook-port "${GITEA_WEBHOOK_PORT:-3030}" \
  --ephemeral --log-level DEBUG
```

If `--webhook-bind` is absent, fall back to `--webhook-port` alone.

## Authentication and model selection

The container entrypoint mints `$HOME/.local/share/opencode/auth.json` from
`OPENCODE_API_KEY` when no auth.json is already mounted. The gateway then
reads `OPENCODE_MODEL` (default `opencode/longcat-2.5-preview-free`) and
posts sessions with a bare model id; override it in `docker/global.env`.
Because the framework is cloned from Gitea main at build time, a fully fresh
image will not contain these fixes until they are pushed to the upstream
branch; rebuild after pushing.

## Supervisor ops

The container entrypoint starts both `opencode serve` and `chat/gateway.py`
under `supervisord` by default. Process output is prefixed with a UTC
ISO timestamp and the program name (`serve` or `gateway`) by
`docker/logprefix.py`, then written to per-process log files:

```
/var/log/flex-agent/serve.log
/var/log/flex-agent/gateway.log
/var/log/flex-agent/supervisord.log
```

A foreground `tail -F` also streams those files to the container's stdout,
so `docker logs` shows the same prefixed format.

By default `/var/log/flex-agent` lives inside the container, so logs are
lost when the container is removed. To keep them across `--rm` runs,
volume-mount a host directory:

```bash
docker run --rm -it \
  --env-file docker/global.env \
  --env-file docker/agent.env \
  -p 4096:4096 -p 3030:3030 \
  -v "$(pwd)/workspace:/workspace" \
  -v "$(pwd)/logs:/var/log/flex-agent" \
  flex-agent:latest
```

Restart a program with `supervisorctl` inside the container:

```bash
docker exec -it <container> supervisorctl -c /opt/docker/supervisord.conf restart gateway
```

### Known limitations

- `opencode serve` emits its own native timestamps; under the prefix those
  timestamps appear after `YYYY-MM-DDTHH:MM:SS+00:00 [serve]`, producing a
  double-timestamp effect.
- Lines printed by the opencode Rocket.Chat plugin are written to serve's
  stdout, so they are captured as `serve` lines even though they originate
  in plugin code.

## Gitea Webhook Proxy

A separate proxy image forwards Gitea webhook deliveries to the gateway
receiver without holding any secret.

Build it from `gitea/proxy/`:

```bash
docker build -f gitea/proxy/Dockerfile -t flex-agent-gitea-proxy:latest gitea/proxy
```

Run it, forwarding to the agent container's webhook port:

```bash
docker run --rm -d \
  -e FORWARD_TO=http://host.docker.internal:3030/ \
  -p 3030:3030 \
  --name gitea-webhook-proxy \
  flex-agent-gitea-proxy:latest
```

Configure the Gitea repo webhook to point at the proxy, e.g.
`http://host.docker.internal:3030/`. See `gitea/proxy/README.md` for the full
env contract.

## Orphaned Leftovers

Do not auto-delete; listed for a future manual prune:
- 5 exited one-off containers
- `rocketchat-compose_*` monitoring/Traefik volumes
- Stale networks
- Unrelated `dwinkelman_postgres_data` volume

## Common Commands

Per-file management:

```bash
docker compose -f rocketchat-compose.yml up -d
docker compose -f rocketchat-compose.yml ps
docker compose -f rocketchat-compose.yml logs -f

docker compose -f gitea-compose.yml up -d
docker compose -f gitea-compose.yml ps
docker compose -f gitea-compose.yml logs -f
```

Agent image:

```bash
docker build -f docker/Dockerfile.agent -t flex-agent:latest .
docker run --rm -it flex-agent:latest bash
```

MongoDB restore / replica-set work:

```bash
# Bring up MongoDB and the one-shot replica-set initializer only
docker compose -f rocketchat-compose.yml up -d mongodb mongodb-init-replica
```

> **Never** run `docker compose down -v` on `rocketchat-compose.yml`; the external `mongodb_data` volume must be preserved.
