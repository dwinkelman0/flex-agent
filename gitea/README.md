# Gitea (git + web UI + issues)

Lightweight forge (single Go binary, SQLite, ~200MB idle) replacing
GitLab, which needed 4GB+ and restart-looped this host. Powers the
agent's `gitea:` channels: issue comments dispatch as prompts, and the
agent replies via `chat/issues.py`.

## Setup (manual, one time)

1. Bring it up from the repo root:
   ```
   docker compose -f gitea-compose.yml up -d
   ```
   Web UI at `http://localhost:3001` (host 3000 belongs to Rocket.Chat),
   SSH on `2222` (host 22 is sshd). First boot takes ~30s.
2. Open `http://localhost:3001` — first boot shows the setup screen.
   Create the admin user (persisted in the `gitea_data` volume).
3. Token: avatar → Settings → Applications → **Generate New Token**.
   This must be a **personal access token**, not an OAuth2 app
   registration (those don't work as `token` auth). Scopes, all
   read+write: `repository`, `issue`, `user` — note repo *creation*
   specifically requires `write:user`, and issue labels take IDs.
   Copy with the copy button (shown once) into root `.env` as
   `GITEA_TOKEN=<token>` (gitignored).
4. Agent users (optional, one per agent): run from `chat/` —
   ```
   USERNAME=<agent> PASSWORD=<secret> python provision_gitea.py
   ```
   This creates the Gitea user (admin API) and mints an `agent` token
   via the agent's own BasicAuth, storing it as `GITEA_TOKEN` in
   `.agent_env`. Consumers prefer `.agent_env` over root `.env`, so the
   agent acts as itself; the admin token remains the fallback.
5. Webhook (push instead of polling): repo/org Settings → Webhooks →
   add, URL `http://<gateway-host>:3030`, type `gitea`, pick a secret.
   Put the same secret in root `.env` as `GITEA_WEBHOOK_SECRET`
   (gitignored), then run the gateway with the receiver on:
   ```
   WEBHOOK_PORT=3030 ./start-dev.sh
   ```
   Deliveries are HMAC-SHA256 verified; bad signatures get 401.
   Covered events: `issue_comment`, `issues`, `pull_request`, `push`
   (see `chat/lib/gitea_hooks.py`). Polling stays as fallback.

## Persistence

Named volume `gitea_data:/data` (repos, SQLite DB, config). Same
Docker-Desktop caveat as chat: data lives inside the Linux VM, inspect
via throwaway container —
`docker run --rm -v gitea_data:/data alpine ls /data` — never delete
unless disposable. (Full table in `chat/README.md`, "Server persistence".)

## Troubleshooting

- `401 invalid username, password or token`: wrong token type (OAuth2
  app, not personal), stale/old paste, or extra whitespace. Verify
  without guessing: compare PBKDF2-SHA256/10000/dklen=50 of the
  candidate against `token_hash` in the `access_token` table.
- `403 ... required=[write:user]` on repo creation: add the scope,
  regenerate, repaste.
- `403 ... required=[write:admin]` on user creation: regenerate with
  admin scope, or create the user in the UI and let
  `provision_gitea.py` mint the token.
- Old/stale tokens: `access_token` rows show name/scope/creation time;
  match by `token_last_eight`.
