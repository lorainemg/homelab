#!/usr/bin/env bash
# Bring up the two host-managed stacks on a fresh machine: the tunnel and the
# control plane. Everything else is a Komodo Stack that Komodo deploys from
# this repo — see README, "Rebuilding from scratch", steps 4-6.
#
# Prerequisites:
#   - Docker Engine + Compose plugin installed
#   - A data disk mounted at $DATA_ROOT (default /data) holding service state
#   - tunnel/.env and komodo/.env created from their .env.example files
set -euo pipefail

cd "$(dirname "$0")/.."

# Only the two host-managed stacks. Everything else is a Komodo Stack and is
# deployed by Komodo from this repo, so bootstrap's job is to make Komodo
# exist: tunnel first (the only route in from outside), komodo second.
STACKS=(tunnel komodo)

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

echo "Tunnel and Komodo are up. Finish in Komodo: create a Stack per directory"
echo "(or restore Komodo's Mongo from backup), then deploy each one."
