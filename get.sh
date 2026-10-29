#!/usr/bin/env bash
# One-line install / update for Olympus:
#   curl -fsSL https://raw.githubusercontent.com/Mrmoody1/olympus/main/get.sh | bash
# Downloads the latest Olympus into ~/olympus (or $OLYMPUS_DIR) and runs the installer.
set -e
REPO="${OLYMPUS_REPO:-Mrmoody1/olympus}"
DEST="${OLYMPUS_DIR:-$HOME/olympus}"
TMP=$(mktemp -d)
echo "Downloading Olympus from github.com/$REPO…"
curl -fsSL "https://github.com/$REPO/archive/refs/heads/main.tar.gz" | tar xz -C "$TMP"
mkdir -p "$DEST"
cp -r "$TMP"/*/. "$DEST"/
rm -rf "$TMP"
find "$DEST" -name '*.sh' -exec chmod +x {} +
cd "$DEST"
./install.sh < /dev/tty
