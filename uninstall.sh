#!/usr/bin/env bash
# Removes the Olympus service. Your apps, data and backups are left alone.
set -e
sudo systemctl disable --now olympus 2>/dev/null || true
sudo rm -f /etc/systemd/system/olympus.service
sudo systemctl daemon-reload
echo "Olympus's service is removed. You can now delete this folder: $(cd "$(dirname "$0")" && pwd)"
echo "Nightly backups are separate and still running. To stop them too:"
echo "  sudo systemctl disable --now homelab-backup.timer"
