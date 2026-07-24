#!/bin/sh
# Hourly live → git backup of the HA yaml files the UI edits. The counterpart
# of config-sync: config-sync applies the repo to the data disk on deploy;
# this commits UI-made edits back to the repo. Commits carry [skip ci] so a
# backup never triggers a redeploy (and thus never restarts HA).
#
# Runs one check at startup, then every BACKUP_INTERVAL seconds. When the
# live copy matches the repo (the steady state, incl. right after a deploy)
# it commits nothing.
set -eu

: "${HOMELAB_PUSH_TOKEN:?set HOMELAB_PUSH_TOKEN (fine-grained PAT with contents:write on this repo)}"
REPO_URL="${REPO_URL:-https://github.com/lorainemg/homelab.git}"
BRANCH="${BRANCH:-main}"
INTERVAL="${BACKUP_INTERVAL:-3600}"
# configuration.yaml is deliberately absent: the live copy still holds a
# scrubbed-from-git plaintext password in a comment; add it here once the
# live file is cleaned up.
WATCH_FILES="${WATCH_FILES:-automations.yaml scripts.yaml scenes.yaml helpers.yaml}"
REPO_DIR=/work/repo
DEST=home-assistant/ha-config

AUTH_URL=$(printf '%s' "$REPO_URL" | sed "s|^https://|https://x-access-token:${HOMELAB_PUSH_TOKEN}@|")

git config --global user.name "ha-config-backup"
git config --global user.email "ha-config-backup@homelab"

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
      [ -f "/live/$f" ] || continue
      if ! cmp -s "/live/$f" "$DEST/$f"; then
        cp "/live/$f" "$DEST/$f"
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
