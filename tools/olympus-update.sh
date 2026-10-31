#!/usr/bin/env bash
# olympus-update: keeps the apps Olympus sets up up to date (the parts made by other projects).
#   olympus-update fetch          check and download new versions ahead of time (nightly, 4:30am)
#   olympus-update status         what's waiting (JSON); also in /var/lib/olympus-update/status.json
#   olympus-update apply <name|all>   switch to the new version (runs in the background)
# Installed as /usr/local/bin/olympus-update. Runs as root (it's called through sudo).
set -uo pipefail
[ "$(id -u)" = 0 ] || exec sudo "$0" "$@"
STATE=/var/lib/olympus-update; mkdir -p "$STATE"; chmod 755 "$STATE"
STATUS="$STATE/status.json"
ME=$(stat -c %U /srv/media 2>/dev/null || echo root); HOME_DIR=$(getent passwd "$ME" | cut -d: -f6)
DOCKER_APPS="cerberus:/opt/cerberus plutus:/opt/plutus mnemosyne:/opt/mnemosyne clio:/opt/clio athena:/opt/athena hermes:/opt/hermes hestia:/opt/hestia"

ntfy(){ local u t k; u=$(grep -oP 'NTFY_URL=\K\S*' /etc/systemd/system/olympus.service 2>/dev/null); t=$(grep -oP 'NTFY_TOPIC=\K\S*' /etc/systemd/system/olympus.service 2>/dev/null)
  k=$(grep -oP 'NTFY_TOKEN=\K\S*' /etc/systemd/system/olympus.service 2>/dev/null)
  [ -n "$u" ] && [ -n "$t" ] && curl -fsS -m 10 -H "Title: $1" -H "Tags: arrow_up" ${k:+-H "Authorization: Bearer $k"} -d "$2" "$u/$t" >/dev/null 2>&1 || true; }

# ---- what's waiting: one line per app, "key|ready(0/1)|current|available"
docker_ready(){ # dir -> 0 if a newer image has been downloaded for any of its containers
  local dir=$1 c cfg run latest
  for c in $(docker compose -f "$dir/docker-compose.yml" ps -q 2>/dev/null); do
    cfg=$(docker inspect -f '{{.Config.Image}}' "$c" 2>/dev/null); run=$(docker inspect -f '{{.Image}}' "$c" 2>/dev/null)
    latest=$(docker image inspect -f '{{.Id}}' "$cfg" 2>/dev/null)
    [ -n "$latest" ] && [ "$latest" != "$run" ] && return 0
  done
  return 1
}
navidrome_latest(){ curl -fsS -m 20 https://api.github.com/repos/navidrome/navidrome/releases/latest 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"].lstrip("v"))' 2>/dev/null; }
aegis_call(){ # path [json] -> AdGuard's answer, using Olympus's saved login for it
  local conf="$HOME_DIR/.config/olympus/aegis.json"; [ -f "$conf" ] || return 1
  python3 - "$conf" "$1" "${2:-}" <<'PY'
import base64, json, sys, urllib.request
c = json.load(open(sys.argv[1])); body = sys.argv[3].encode() if sys.argv[3] else None
req = urllib.request.Request(c["url"].rstrip("/") + sys.argv[2], data=body, method="POST" if body is not None else "GET",
      headers={"Authorization": "Basic " + base64.b64encode(f'{c["user"]}:{c["password"]}'.encode()).decode(), "Content-Type": "application/json"})
print(urllib.request.urlopen(req, timeout=30).read().decode())
PY
}

check(){
  local out=() key dir cur new
  for kv in $DOCKER_APPS; do
    key=${kv%%:*}; dir=${kv#*:}
    [ -f "$dir/docker-compose.yml" ] || continue
    if docker_ready "$dir"; then out+=("$key|1||"); else out+=("$key|0||"); fi
  done
  for pkg in jellyfin tailscale; do
    dpkg -s "$pkg" >/dev/null 2>&1 || dpkg -s "$pkg-server" >/dev/null 2>&1 || continue
    local p=$pkg; [ "$pkg" = jellyfin ] && p=jellyfin-server
    cur=$(dpkg-query -W -f '${Version}' "$p" 2>/dev/null); new=$(apt-cache policy "$p" 2>/dev/null | awk '/Candidate:/{print $2}')
    [ -n "$new" ] && [ "$new" != "$cur" ] && [ "$new" != "(none)" ] && out+=("$pkg|1|$cur|$new") || out+=("$pkg|0|$cur|")
  done
  if command -v navidrome >/dev/null; then
    cur=$(navidrome --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1); new=$(navidrome_latest)
    [ -n "$new" ] && [ -n "$cur" ] && [ "$new" != "$cur" ] && out+=("navidrome|1|$cur|$new") || out+=("navidrome|0|$cur|")
  fi
  if v=$(aegis_call /control/version.json '{"recheck_now":true}' 2>/dev/null); then
    new=$(echo "$v" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("new_version","") if d.get("can_autoupdate") else "")' 2>/dev/null)
    [ -n "$new" ] && out+=("aegis|1||$new") || out+=("aegis|0||")
  fi
  printf '%s\n' "${out[@]}" | python3 -c '
import json, sys, time
names = {"cerberus": "Cerberus · passwords", "plutus": "Plutus · money", "mnemosyne": "Mnemosyne · photos", "clio": "Clio · books",
         "athena": "Athena · documents", "hermes": "Hermes · drive", "hestia": "Hestia · smart home", "jellyfin": "Jellyfin · behind Theia",
         "navidrome": "Navidrome · behind Apollo", "tailscale": "Tailscale", "aegis": "Aegis · AdGuard Home"}
items = []
for line in sys.stdin:
    if not line.strip(): continue
    k, ready, cur, new = (line.rstrip("\n").split("|") + ["", "", ""])[:4]
    items.append({"key": k, "name": names.get(k, k), "ready": ready == "1", "current": cur, "available": new})
print(json.dumps({"checked": time.time(), "items": items}))' > "$STATUS.tmp" && mv "$STATUS.tmp" "$STATUS" && chmod 644 "$STATUS"
}

fetch(){
  for kv in $DOCKER_APPS; do dir=${kv#*:}; [ -f "$dir/docker-compose.yml" ] && (cd "$dir" && docker compose pull -q >/dev/null 2>&1); done
  apt-get update -qq >/dev/null 2>&1 || true
  local before; before=$(python3 -c 'import json; print(sum(i["ready"] for i in json.load(open("'"$STATUS"'"))["items"]))' 2>/dev/null || echo 0)
  check
  local n; n=$(python3 -c 'import json; print(sum(i["ready"] for i in json.load(open("'"$STATUS"'"))["items"]))' 2>/dev/null || echo 0)
  [ "$n" -gt 0 ] && [ "$n" != "$before" ] && ntfy "Updates ready" "$n app update$( [ "$n" = 1 ] || echo s) ready. Olympus → Settings → Apps → Update all."
  true
}

apply_one(){
  local key=$1 dir
  for kv in $DOCKER_APPS; do
    if [ "${kv%%:*}" = "$key" ]; then dir=${kv#*:}; (cd "$dir" && docker compose up -d >/dev/null 2>&1); return; fi
  done
  case "$key" in
    jellyfin) DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --only-upgrade $(dpkg-query -W -f '${Package}\n' 'jellyfin*' 2>/dev/null) >/dev/null 2>&1 ;;
    tailscale) DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --only-upgrade tailscale >/dev/null 2>&1 ;;
    navidrome)
      local arch url tmp; arch=$(dpkg --print-architecture)
      url=$(curl -fsS -m 20 https://api.github.com/repos/navidrome/navidrome/releases/latest | python3 -c "import json,sys; print(next(a['browser_download_url'] for a in json.load(sys.stdin)['assets'] if a['name'].endswith('_linux_$arch.deb')))")
      tmp=$(mktemp --suffix=.deb) && curl -fsSL -o "$tmp" "$url" && dpkg -i "$tmp" >/dev/null 2>&1 && systemctl restart navidrome; rm -f "$tmp" ;;
    aegis) aegis_call /control/update '{}' >/dev/null 2>&1 ;;
  esac
}

case "${1:-status}" in
  fetch) fetch ;;
  check) check; cat "$STATUS" ;;
  status) [ -f "$STATUS" ] && cat "$STATUS" || { check; cat "$STATUS"; } ;;
  apply)
    what=${2:?which app, or all}
    [[ "$what" =~ ^[a-z]+$ ]] || { echo '{"error":"not allowed"}'; exit 2; }
    # In the background, so updating Tailscale (which Olympus may be reached through) doesn't cut it short
    systemd-run --quiet --collect --unit "olympus-update-$what-$(date +%s)" "$0" apply-now "$what" >/dev/null 2>&1 \
      && echo '{"started":true}' || { echo '{"error":"could not start"}'; exit 1; } ;;
  apply-now)
    what=$2; echo "{\"running\":\"$what\",\"since\":$(date +%s)}" > "$STATE/running.json"; chmod 644 "$STATE/running.json"
    if [ "$what" = all ]; then
      for k in $(python3 -c 'import json; print(" ".join(i["key"] for i in json.load(open("'"$STATUS"'"))["items"] if i["ready"]))' 2>/dev/null); do apply_one "$k"; done
    else apply_one "$what"; fi
    docker image prune -f >/dev/null 2>&1 || true
    rm -f "$STATE/running.json"; check ;;
  *) sed -n '2,6p' "$0" ;;
esac
