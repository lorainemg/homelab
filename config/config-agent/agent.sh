#!/bin/sh
# The one config container: owns every repo -> live config flow.
#   1. Sync the configs baked into this image (HA yaml, mosquitto.conf,
#      Caddyfile) into the live dirs. Only files that exist in the repo are
#      written; runtime state living alongside them (.storage/, *.db,
#      custom_components/, mosquitto passwd, ...) is untouched; the previous
#      version of every overwritten file is kept in .sync-backup/ next to it.
#   2. Poke the running services: caddy runs with --watch and reloads its
#      Caddyfile by itself; Home Assistant is reloaded (or restarted, when
#      configuration.yaml changed) through its API.
#   3. Loop forever, committing UI-made edits to the watched HA yaml back to
#      git every BACKUP_INTERVAL seconds with [skip ci], so a backup never
#      triggers a redeploy.
set -eu

: "${HOMELAB_PUSH_TOKEN:?set HOMELAB_PUSH_TOKEN (fine-grained PAT with contents:write on this repo)}"
HA_URL="${HA_URL:-http://homeassistant:8123}"
REPO_URL="${REPO_URL:-https://github.com/lorainemg/homelab.git}"
BRANCH="${BRANCH:-main}"
INTERVAL="${BACKUP_INTERVAL:-3600}"
# configuration.yaml is deliberately absent: the live copy still holds a
# scrubbed-from-git plaintext password in a comment; add it here once the
# live file is cleaned up.
WATCH_FILES="${WATCH_FILES:-automations.yaml scripts.yaml scenes.yaml helpers.yaml}"
REPO_DIR=/work/repo
DEST=home-assistant/ha-config
CHANGES=/tmp/changed

# --- 1. repo -> live sync ---------------------------------------------------

sync_dir() {
  src="$1" dest="$2"
  mkdir -p "$dest"
  (cd "$src" && find . -type f) | sed 's|^\./||' | while IFS= read -r f; do
    # skipping identical files keeps agent restarts from re-poking services
    [ -f "$dest/$f" ] && cmp -s "$src/$f" "$dest/$f" && continue
    if [ -f "$dest/$f" ]; then
      mkdir -p "$dest/.sync-backup/$(dirname "$f")"
      cp -a "$dest/$f" "$dest/.sync-backup/$f"
    fi
    mkdir -p "$dest/$(dirname "$f")"
    cp -a "$src/$f" "$dest/$f"
    printf '%s\n' "$dest/$f" >> "$CHANGES"
    echo "synced: $dest/$f"
  done
}

: > "$CHANGES"
sync_dir /src/ha-config /live/ha-config
# configuration.yaml has `!include_dir_merge_named themes/`; HA fails to boot
# on a fresh volume if the directory is missing.
mkdir -p /live/ha-config/themes
sync_dir /src/mosquitto /live/mosquitto
sync_dir /src/caddy /live/caddy

# --- 2. poke the services ---------------------------------------------------

ha_call() {
  curl -fsS -m 10 -X POST -H "Authorization: Bearer ${HA_TOKEN:-}" \
    -H 'Content-Type: application/json' -d '{}' \
    "$HA_URL/api/services/homeassistant/$1" > /dev/null
}

if grep -q '^/live/ha-config/configuration\.yaml$' "$CHANGES"; then
  echo "configuration.yaml changed - restarting Home Assistant"
  ha_call restart || echo "HA restart call failed (starting up? it boots with the new config anyway)"
elif grep -q '^/live/ha-config/' "$CHANGES"; then
  echo "HA yaml changed - reloading"
  ha_call reload_all || echo "HA reload call failed (starting up? it boots with the new config anyway)"
fi
if grep -q '^/live/mosquitto/' "$CHANGES"; then
  echo "NOTE: mosquitto config changed - restart the mosquitto container to apply it."
fi
echo "Config sync complete."

# --- 3. live -> git backup loop ---------------------------------------------

AUTH_URL=$(printf '%s' "$REPO_URL" | sed "s|^https://|https://x-access-token:${HOMELAB_PUSH_TOKEN}@|")

git config --global user.name "config-agent"
git config --global user.email "config-agent@homelab"

clone() {
  rm -rf "$REPO_DIR"
  git clone --quiet --depth 1 --branch "$BRANCH" "$AUTH_URL" "$REPO_DIR"
}
clone

while :; do
  (
    cd "$REPO_DIR"
    git fetch --quiet origin "$BRANCH"
    git reset --quiet --hard "origin/$BRANCH"
    changed=""
    for f in $WATCH_FILES; do
      [ -f "/live/ha-config/$f" ] || continue
      if ! cmp -s "/live/ha-config/$f" "$DEST/$f"; then
        cp "/live/ha-config/$f" "$DEST/$f"
        changed="$changed $f"
      fi
    done
    if [ -n "$changed" ]; then
      git add -A "$DEST"
      git commit --quiet -m "HA UI:$changed [skip ci]"
      git push --quiet origin "HEAD:$BRANCH" || echo "push failed, retrying next cycle"
      echo "backed up:$changed"
    else
      echo "no UI changes"
    fi
  ) || { echo "cycle failed, recloning"; clone || echo "reclone failed, retrying next cycle"; }
  sleep "$INTERVAL"
done
