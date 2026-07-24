#!/usr/bin/env bash
# Bring up the whole homelab on a fresh machine.
#
# Prerequisites:
#   - Docker Engine + Compose plugin installed
#   - A data disk mounted at $DATA_ROOT (default /data) holding service state
#   - Each stack's .env created from its .env.example
set -euo pipefail

cd "$(dirname "$0")/.."

# portainer first: it's the control plane (Portainer + the Cloudflare
# tunnel) everything else is managed and published through.
STACKS=(portainer registry config immich home-assistant monitoring)

# Shared bridge network that lets Caddy reach every stack by container name.
docker network inspect internal >/dev/null 2>&1 || docker network create internal

# The monitoring stack joins the trakt bot's network (the bot deploys itself
# from its own repo's CI); pre-create it so monitoring can start first.
docker network inspect trakt-tg-bot_aspire >/dev/null 2>&1 || docker network create trakt-tg-bot_aspire

for stack in "${STACKS[@]}"; do
  if [[ -f "$stack/.env.example" && ! -f "$stack/.env" ]]; then
    echo "!! $stack/.env is missing — copy $stack/.env.example and fill it in first." >&2
    exit 1
  fi
done

for stack in "${STACKS[@]}"; do
  echo "==> $stack"
  docker compose --project-directory "$stack" up -d
done

echo "All stacks up. Point your Cloudflare tunnel at caddy:80 and you're done."
