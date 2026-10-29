#!/usr/bin/env bash
# Runs Olympus as a background service that starts with the PC.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

if ! command -v python3 >/dev/null; then echo "python3 is missing: sudo apt install python3"; exit 1; fi

# keep the port it already has; otherwise pick a free one, starting at 8097
EXISTING=$(grep -oP 'OLYMPUS_PORT=\K[0-9]+' /etc/systemd/system/olympus.service 2>/dev/null || true)
if [ -n "$EXISTING" ] && [ -z "$OLYMPUS_PORT" ]; then PORT=$EXISTING; else
  PORT="${OLYMPUS_PORT:-8097}"
  while ss -ltn | awk '{print $4}' | grep -qE "[:.]$PORT\$"; do PORT=$((PORT + 1)); done
fi

# keep the ntfy topic it already has, unless a new one is given
OLD_TOPIC=$(grep -oP 'NTFY_TOPIC=\K\S+' /etc/systemd/system/olympus.service 2>/dev/null || true)
NTFY_TOPIC="${NTFY_TOPIC:-${OLD_TOPIC:-olympus}}"

echo "Installing Olympus from $DIR (port $PORT)…"
sudo tee /etc/systemd/system/olympus.service >/dev/null <<EOF
[Unit]
Description=Olympus home screen
After=network-online.target navidrome.service jellyfin.service docker.service
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$DIR
ExecStart=/usr/bin/python3 $DIR/server.py
Environment=OLYMPUS_PORT=$PORT
Environment=ODYSSEUS_URL=${ODYSSEUS_URL:-http://127.0.0.1:7011}
Environment=APOLLO_URL=${APOLLO_URL:-http://127.0.0.1:8090}
Environment=THEIA_URL=${THEIA_URL:-http://127.0.0.1:8095}
Environment=NTFY_URL=${NTFY_URL:-http://127.0.0.1:8091}
Environment=NTFY_TOPIC=${NTFY_TOPIC:-olympus}
Environment=NTFY_TOKEN=${NTFY_TOKEN:-}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable olympus >/dev/null 2>&1
sudo systemctl restart olympus
sleep 1

if systemctl is-active --quiet olympus; then
  echo
  echo "  Olympus is running on http://localhost:$PORT"
  echo
  echo "  For your phone:   sudo tailscale serve --bg --https=9443 $PORT"
  echo "  then open:        https://$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || echo '<your-pc>.ts.net'):9443"
  echo
else
  echo "Olympus didn't start. See why with:  journalctl -u olympus -n 30 --no-pager"
  exit 1
fi
