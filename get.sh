#!/usr/bin/env bash
# One-line install / update for Olympus:
#   gh api repos/Mrmoody1/olympus/contents/get.sh -H "Accept: application/vnd.github.raw" | bash
# Downloads the latest Olympus into ~/olympus (or $OLYMPUS_DIR) and runs the installer.
set -e
REPO="${OLYMPUS_REPO:-Mrmoody1/olympus}"
DEST="${OLYMPUS_DIR:-$HOME/olympus}"
TMP=$(mktemp -d)
echo "Downloading Olympus from github.com/$REPO…"
ARCHIVE=$(mktemp)
if command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then
  gh api "repos/$REPO/tarball/main" > "$ARCHIVE"          # works for private repos
elif ! curl -fsSL "https://github.com/$REPO/archive/refs/heads/main.tar.gz" -o "$ARCHIVE"; then
  echo "Couldn't download $REPO. It's private, so sign in first:  sudo apt install gh && gh auth login"; exit 1
fi
tar xzf "$ARCHIVE" -C "$TMP"; rm -f "$ARCHIVE"
mkdir -p "$DEST"
cp -r "$TMP"/*/. "$DEST"/
rm -rf "$TMP"
find "$DEST" -name '*.sh' -exec chmod +x {} +
cd "$DEST"
./install.sh < /dev/tty
