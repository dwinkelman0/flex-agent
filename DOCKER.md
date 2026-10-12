# Docker Setup

This repo defines two Docker Compose stacks that run on a local Colima VM.

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

**Not running in Docker**:
- `opencode serve` — host process on an ephemeral `127.0.0.1` port
- `:3030` webhook receiver — only active when the gateway runs with `--webhook-port`
- GitLab — fully removed; `8081`/`8022` are free

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

MongoDB restore / replica-set work:

```bash
# Bring up MongoDB and the one-shot replica-set initializer only
docker compose -f rocketchat-compose.yml up -d mongodb mongodb-init-replica
```

> **Never** run `docker compose down -v` on `rocketchat-compose.yml`; the external `mongodb_data` volume must be preserved.
