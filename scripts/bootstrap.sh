#!/usr/bin/env bash
# Bring up the two host-managed stacks on a fresh machine: the tunnel and the
# control plane. Everything else is a Komodo Stack declared in
# komodo/stacks.toml, which the seed step below points Komodo at.
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

create_networks() {
  # Shared bridge network that lets Caddy reach every stack by container name.
  docker network inspect internal >/dev/null 2>&1 || docker network create internal
  # The monitoring stack joins the trakt bot's network (the bot deploys itself
  # from its own repo's CI); pre-create it so monitoring can start first.
  docker network inspect trakt-tg-bot_aspire >/dev/null 2>&1 || docker network create trakt-tg-bot_aspire
}

check_env_files() {
  for stack in "${STACKS[@]}"; do
    if [[ -f "$stack/.env.example" && ! -f "$stack/.env" ]]; then
      echo "!! $stack/.env is missing — copy $stack/.env.example and fill it in first." >&2
      exit 1
    fi
  done
}

start_stacks() {
  for stack in "${STACKS[@]}"; do
    echo "==> $stack"
    docker compose --project-directory "$stack" up -d
  done
}

# POST to Komodo Core from inside the compose network. Core publishes no host
# port (only Caddy reaches it, and Caddy is itself a Komodo Stack — not up
# yet), so every call runs from a throwaway container on that network.
#   api <path> <json-body> [jwt]
api() {
  docker run --rm --network komodo curlimages/curl:8.11.1 \
    -s -X POST "http://komodo-core:9120$1" \
    -H 'content-type: application/json' \
    ${3:+-H "Authorization: Bearer $3"} \
    -d "$2"
}

# Logs in with the admin credentials from komodo/.env (whose existence
# check_env_files guarantees), retrying until Core is up. Sets JWT.
wait_for_core() {
  local user pass
  user=$(grep -oP '^KOMODO_INIT_ADMIN_USERNAME=\K.*' komodo/.env)
  pass=$(grep -oP '^KOMODO_INIT_ADMIN_PASSWORD=\K.*' komodo/.env)
  echo "==> waiting for Komodo Core"
  for _ in $(seq 1 30); do
    JWT=$(api /auth/login "{\"type\":\"LoginLocalUser\",\"params\":{\"username\":\"$user\",\"password\":\"$pass\"}}" \
      | grep -oP '"jwt":"\K[^"]*' || true)
    [[ -n "$JWT" ]] && return 0
    sleep 2
  done
  echo "!! Komodo Core never answered the login." >&2
  exit 1
}

# Komodo's Stacks are declared in komodo/stacks.toml, but the ResourceSync
# object that points Komodo at that file lives only in Mongo and cannot
# declare itself. Create it if it is missing, then run the first sync so a
# fresh machine ends up with every Stack, no UI involved. Safe to re-run.
seed_resource_sync() {
  echo "==> seeding the resource sync"
  if api /read '{"type":"GetResourceSync","params":{"sync":"homelab"}}' "$JWT" | grep -q '"name":"homelab"'; then
    echo "    already exists — skipping"
    return 0
  fi
  api /write '{"type":"CreateResourceSync","params":{"name":"homelab","config":{
    "repo":"lorainemg/homelab","branch":"main",
    "resource_path":["komodo/stacks.toml"],
    "managed":false,"delete":false,"webhook_enabled":true}}}' "$JWT" \
    | grep -q '"name":"homelab"' || { echo "!! CreateResourceSync failed." >&2; exit 1; }
  api /execute '{"type":"RunSync","params":{"sync":"homelab"}}' "$JWT" >/dev/null
  echo "    created; first sync started (async — watch it in the Komodo UI)"
}

main() {
  check_env_files
  create_networks
  start_stacks
  wait_for_core
  seed_resource_sync

  echo "Tunnel and Komodo are up; komodo/stacks.toml declares the Stacks."
  echo "Still manual: each stack's .env on the host, and the GitHub webhooks"
  echo "(one per stack + /listener/github/sync/homelab/sync for the sync itself)."
  echo "The trakt-tg-bot Stack is not seeded here: its own repo's CI creates it,"
  echo "once the trakt-tg-bot-ci service user exists with an API key and Read+Attach on the server."
}

main "$@"
