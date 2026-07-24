#!/bin/sh
# Copy the config files versioned in the repo (baked into this image by CI)
# into the live bind-mounted config directories, then exit. Only files that
# exist in the repo are written; runtime state living alongside them
# (.storage/, *.db, custom_components/, mosquitto passwd, ...) is untouched.
# The previous version of every file that gets overwritten is kept under
# <dest>/.sync-backup/ so a bad push is recoverable on the host.
set -eu

sync_dir() {
  src="$1" dest="$2"
  mkdir -p "$dest"
  (cd "$src" && find . -type f) | sed 's|^\./||' | while IFS= read -r f; do
    if [ -f "$dest/$f" ] && ! cmp -s "$src/$f" "$dest/$f"; then
      mkdir -p "$dest/.sync-backup/$(dirname "$f")"
      cp -a "$dest/$f" "$dest/.sync-backup/$f"
    fi
    mkdir -p "$dest/$(dirname "$f")"
    cp -a "$src/$f" "$dest/$f"
    echo "synced: $dest/$f"
  done
}

sync_dir /src/ha-config /config
# configuration.yaml has `!include_dir_merge_named themes/`; HA fails to boot
# on a fresh volume if the directory is missing.
mkdir -p /config/themes

sync_dir /src/mosquitto /mosquitto-config

echo "Config sync complete."
