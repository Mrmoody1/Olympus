#!/usr/bin/env bash
# Home lab backups: restic to the HDD (everything) + Koofr (everything except films & shows).
# Run as yourself (not sudo):   bash ~/olympus/backup/homelab-backup-setup.sh
# Phone alerts go to $NTFY_URL (default http://127.0.0.1:8091/HomeLab).
# Safe to run again: it keeps your existing password, repos and Koofr login.
set -euo pipefail

if [ "$(id -u)" = 0 ]; then echo "Run this as yourself, without sudo."; exit 1; fi
[ "$(uname -m)" = x86_64 ] || { echo "This script expects a 64-bit Intel/AMD PC."; exit 1; }
ME_HOME="$HOME"
CONF=/etc/homelab-backup
RESTIC_VER=0.17.3
say(){ printf '\n\033[38;2;224;128;95m▸ %s\033[0m\n' "$*"; }

sudo -v

# ---------------------------------------------------------------- 1. tools
say "Installing restic $RESTIC_VER, rclone and rsync"
sudo apt-get update -qq
sudo apt-get install -y -qq rsync bzip2 curl python3 >/dev/null
if ! /usr/local/bin/restic version 2>/dev/null | grep -q "$RESTIC_VER"; then
  curl -fsSL "https://github.com/restic/restic/releases/download/v$RESTIC_VER/restic_${RESTIC_VER}_linux_amd64.bz2" \
    | bunzip2 | sudo tee /usr/local/bin/restic >/dev/null
  sudo chmod 755 /usr/local/bin/restic
fi
if ! command -v rclone >/dev/null; then
  TMPD=$(mktemp -d)
  curl -fsSL -o "$TMPD/rclone.deb" https://downloads.rclone.org/rclone-current-linux-amd64.deb
  sudo dpkg -i "$TMPD/rclone.deb" >/dev/null
  rm -rf "$TMPD"
fi
/usr/local/bin/restic version; rclone version 2>/dev/null | head -1 || true

# ---------------------------------------------------------------- 2. config + password
say "Creating settings in $CONF"
sudo install -d -m 700 "$CONF"
sudo install -d -m 755 /var/lib/homelab-backup
if ! sudo test -s "$CONF/restic-pass"; then
  python3 -c "import secrets;a='ABCDEFGHJKLMNPQRSTUVWXYZ23456789';s=''.join(secrets.choice(a) for _ in range(25));print('-'.join(s[i:i+5] for i in range(0,25,5)))" \
    | sudo tee "$CONF/restic-pass" >/dev/null
fi
sudo chmod 600 "$CONF/restic-pass"
NEW_PASS=1; sudo test -f "$CONF/.password-shown" && NEW_PASS=0
KOOFR_ENABLED=0
sudo test -f "$CONF/env" && KOOFR_ENABLED=$(sudo sh -c ". $CONF/env; echo \${KOOFR_ENABLED:-0}")
write_env(){
sudo tee "$CONF/env" >/dev/null <<EOF
HOME_DIR=$ME_HOME
HDD_MOUNT=/mnt/backup
HDD_REPO=/mnt/backup/restic
KOOFR_REPO=rclone:koofr:homelab-backup
KOOFR_ENABLED=$KOOFR_ENABLED
NTFY_URL=${NTFY_URL:-http://127.0.0.1:8091/HomeLab}
EOF
sudo chmod 600 "$CONF/env"
}
write_env

# ---------------------------------------------------------------- 3. the backup command
say "Installing the homelab-backup command"
sudo tee /usr/local/bin/homelab-backup >/dev/null <<'RUNNER'
#!/usr/bin/env bash
# homelab-backup            run a backup now (HDD + Koofr)
# homelab-backup list       show snapshots
# homelab-backup test       restore a few files from each place and compare them
# homelab-backup check      verify the backups aren't damaged
# homelab-backup restore <hdd|koofr> <path> [snapshot]   restore a folder to /tmp/restore
set -uo pipefail
[ "$(id -u)" = 0 ] || exec sudo "$0" "$@"
CONF=/etc/homelab-backup
. "$CONF/env"
export RESTIC_PASSWORD_FILE="$CONF/restic-pass" RCLONE_CONFIG="$CONF/rclone.conf"
export RESTIC_CACHE_DIR=/var/cache/restic
STATE=/var/lib/homelab-backup
SNAP="$STATE/snap"
R=/usr/local/bin/restic
mkdir -p "$STATE" "$SNAP"

notify(){ # title priority tags message
  curl -fsS -m 10 -H "Title: $1" -H "Priority: $2" -H "Tags: $3" -d "$4" "$NTFY_URL" >/dev/null 2>&1 || true; }
hdd_ok(){ mountpoint -q "$HDD_MOUNT" && [ -w "$HDD_MOUNT" ]; }
repos(){ # name repo pairs that are available
  hdd_ok && echo "hdd $HDD_REPO"
  [ "$KOOFR_ENABLED" = 1 ] && echo "koofr $KOOFR_REPO"; }

# Live databases are copied to $SNAP with the apps briefly stopped (a few seconds),
# then everything is backed up from there, so the apps are back up straight away.
stage(){
  local stopped=() dstopped=() rc=0
  # Plutus investments (Ghostfolio): a proper database dump, taken while it runs.
  if [ -n "$(docker ps -q -f name=^plutus-postgres$ 2>/dev/null)" ]; then
    mkdir -p "$SNAP/plutus"
    docker exec plutus-postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists' > "$SNAP/plutus/ghostfolio.sql.tmp" \
      && mv "$SNAP/plutus/ghostfolio.sql.tmp" "$SNAP/plutus/ghostfolio.sql" || rc=1
    cp -a /opt/plutus/.env /opt/plutus/docker-compose.yml "$SNAP/plutus/" 2>/dev/null || true
  fi
  for s in navidrome jellyfin vaultwarden; do
    systemctl is-active -q "$s" 2>/dev/null && systemctl stop "$s" && stopped+=("$s")
  done
  for c in odysseus-chromadb-1 odysseus-odysseus-1 cerberus plutus-actual; do
    [ -n "$(docker ps -q -f "name=^${c}$" 2>/dev/null)" ] && docker stop -t 20 "$c" >/dev/null && dstopped+=("$c")
  done
  for d in /var/lib/navidrome /var/lib/jellyfin /var/lib/docker/volumes/odysseus_chromadb-data /opt/cerberus/data /opt/plutus/actual; do
    [ -d "$d" ] || continue
    mkdir -p "$SNAP$d"
    rsync -a --delete --exclude transcodes --exclude cache "$d/" "$SNAP$d/" || rc=1
  done
  # Odysseus: chats, settings, memory, skills. Leaves out tools/caches that reinstall themselves.
  if [ -d "$HOME_DIR/odysseus/data" ]; then
    mkdir -p "$SNAP/odysseus-data"
    rsync -a --delete --exclude /local --exclude /fastembed_cache --exclude /huggingface \
      --exclude /cache --exclude /logs --exclude /tts_cache "$HOME_DIR/odysseus/data/" "$SNAP/odysseus-data/" || rc=1
  fi
  for s in "${stopped[@]}"; do systemctl start "$s"; done
  for c in "${dstopped[@]}"; do docker start "$c" >/dev/null; done
  return $rc
}

paths(){
  for p in /srv/media "$SNAP" /etc/jellyfin /etc/navidrome "$HOME_DIR/odysseus" \
           "$HOME_DIR/apollo" "$HOME_DIR/theia" "$HOME_DIR/olympus" \
           /etc/systemd/system/apollo.service /etc/systemd/system/theia.service \
           /etc/systemd/system/olympus.service /etc/fstab \
           "$HOME_DIR/.config/olympus" "$HOME_DIR/.config/apollo" "$HOME_DIR/.config/theia" \
           "$HOME_DIR/mnemosyne" /opt/mnemosyne/.env /opt/mnemosyne/docker-compose.yml \
           "$HOME_DIR/clio" /opt/clio/config /opt/clio/metadata /opt/clio/docker-compose.yml \
           "$HOME_DIR/aegis" /opt/AdGuardHome/AdGuardHome.yaml; do
    [ -e "$p" ] && echo "$p"
  done; }
excludes(){
  echo "--exclude=$HOME_DIR/odysseus/data"
  echo "--exclude=$HOME_DIR/odysseus/logs"
  echo "--exclude=**/transcodes"
  echo "--exclude=/srv/media/photos/thumbs"            # Mnemosyne remakes these itself
  echo "--exclude=/srv/media/photos/encoded-video"
  echo "--exclude=/opt/clio/metadata/cache"; echo "--exclude=/opt/clio/metadata/logs"   # Clio remakes these
  echo "--exclude=/srv/media/.cleared"                  # cleared-out films: on their way to being deleted
  if [ "$1" = koofr ]; then echo "--exclude=/srv/media/movies"; echo "--exclude=/srv/media/shows"; echo "--exclude=/srv/media/inbox"; echo "--exclude=/srv/media/books/audiobooks"; echo "--exclude=/srv/media/books/podcasts"; fi; }

human(){ numfmt --to=iec --suffix=B "${1:-0}" 2>/dev/null || echo "${1}B"; }

backup(){
  # After a boot (missed 3am run) give the apps a moment to start.
  [ "$(cut -d. -f1 /proc/uptime)" -lt 180 ] && sleep 120
  local start; start=$(date -Is)
  local lines=() fail=0 status="{"
  if ! stage; then lines+=("⚠ copying the app databases had a problem"); fail=1; fi
  mapfile -t P < <(paths)
  if ! hdd_ok; then lines+=("✗ HDD: backup drive not mounted at $HDD_MOUNT"); fail=1
    status+="\"hdd\":{\"ok\":false,\"error\":\"not mounted\"},"
  fi
  while read -r name repo; do
    mapfile -t X < <(excludes "$name")
    local out; out=$("$R" -r "$repo" backup --host homelab --tag nightly --json --quiet "${X[@]}" "${P[@]}" 2>&1 | tail -n 5)
    local summary; summary=$(echo "$out" | grep '"message_type":"summary"' | tail -n1)
    if [ -n "$summary" ]; then
      local added; added=$(echo "$summary" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("data_added",0))')
      "$R" -r "$repo" forget --host homelab --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune --quiet >/dev/null 2>&1 \
        || lines+=("⚠ $name: tidying old backups failed")
      local count; count=$("$R" -r "$repo" snapshots --json 2>/dev/null | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))' 2>/dev/null || echo 0)
      local total; total=$("$R" -r "$repo" stats --mode raw-data --json 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin).get("total_size",0))' 2>/dev/null || echo 0)
      lines+=("✓ ${name^^}: +$(human "$added"), $count backups, $(human "$total") used")
      status+="\"$name\":{\"ok\":true,\"added\":$added,\"snapshots\":$count,\"size\":$total},"
    else
      fail=1; local err; err=$(echo "$out" | tail -n1 | tr -d '"\\' | cut -c1-200)
      lines+=("✗ ${name^^}: $err")
      status+="\"$name\":{\"ok\":false,\"error\":\"$err\"},"
    fi
  done < <(repos)
  local free=0; hdd_ok && free=$(df -B1 --output=avail "$HDD_MOUNT" | tail -1 | tr -d ' ')
  status+="\"hdd_free\":$free,\"started\":\"$start\",\"finished\":\"$(date -Is)\",\"ok\":$([ $fail = 0 ] && echo true || echo false)}"
  echo "$status" > "$STATE/status.json"; chmod 644 "$STATE/status.json"
  local msg; msg=$(printf '%s\n' "${lines[@]}")
  echo "$msg"
  if [ $fail = 0 ]; then notify "Backup done" low "white_check_mark,floppy_disk" "$msg"
  else notify "Backup problem" high "warning,floppy_disk" "$msg"; fi
  # Monthly: verify a sample of the stored data.
  [ "$(date +%d)" = 01 ] && check quiet
  return $fail
}

check(){
  local bad=0
  while read -r name repo; do
    echo "Checking $name…"
    "$R" -r "$repo" check --read-data-subset=10% >/tmp/hb-check.log 2>&1 || { bad=1; notify "Backup check failed" high warning "$name: $(tail -n2 /tmp/hb-check.log)"; }
    [ "${1:-}" = quiet ] || tail -n2 /tmp/hb-check.log
  done < <(repos)
  [ $bad = 0 ] && [ "${1:-}" != quiet ] && echo "All good."
  return $bad
}

# ---- for Olympus's Restore sheet: browse and restore, limited to your media and home folders ----
allowed(){ local p; p=$(realpath -m -- "$1"); case "$p" in /srv/media|/srv/media/*|"$HOME_DIR"|"$HOME_DIR"/*) return 0 ;; *) return 1 ;; esac; }
repo_of(){ case "$1" in hdd) echo "$HDD_REPO" ;; koofr) [ "$KOOFR_ENABLED" = 1 ] && echo "$KOOFR_REPO" ;; esac; }
valid_snap(){ [[ "$1" =~ ^[0-9a-f]{8,64}$ || "$1" = latest ]]; }

testrestore(){
  local bad=0
  while read -r name repo; do
    local t; t=$(mktemp -d)
    "$R" -r "$repo" restore latest --host homelab --target "$t" --include /etc/jellyfin --include "$SNAP/var/lib/navidrome" --quiet
    if diff -rq /etc/jellyfin "$t/etc/jellyfin" >/dev/null && [ -d "$t$SNAP/var/lib/navidrome" ]; then
      echo "✓ ${name^^}: restored Jellyfin settings + Navidrome database and they match"
    else echo "✗ ${name^^}: restore test failed"; bad=1; fi
    rm -rf "$t"
  done < <(repos)
  return $bad
}

case "${1:-backup}" in
  backup|now) backup ;;
  list) while read -r name repo; do echo "== ${name^^} =="; "$R" -r "$repo" snapshots --compact; done < <(repos) ;;
  check) check ;;
  test) testrestore ;;
  restore)
    name=${2:?hdd or koofr}; what=${3:?path to restore, e.g. /srv/media/music}; snap=${4:-latest}
    repo=$HDD_REPO; [ "$name" = koofr ] && repo=$KOOFR_REPO
    mkdir -p /tmp/restore
    "$R" -r "$repo" restore "$snap" --host homelab --target /tmp/restore --include "$what"
    chown -R "$(stat -c %U "$HOME_DIR")": /tmp/restore
    echo "Restored into /tmp/restore$what — copy back what you need." ;;
  snapshots-json)
    while read -r name repo; do
      printf '{"place":"%s","snapshots":%s}\n' "$name" "$("$R" -r "$repo" snapshots --json --host homelab 2>/dev/null || echo '[]')"
    done < <(repos) | python3 -c 'import sys,json; print(json.dumps([json.loads(l) for l in sys.stdin if l.strip()]))' ;;
  ls-json)
    repo=$(repo_of "${2:-}"); snap=${3:-}; what=$(realpath -m -- "${4:-/}")
    [ -n "$repo" ] && valid_snap "$snap" && allowed "$what" || { echo '{"error":"not allowed"}'; exit 2; }
    "$R" -r "$repo" ls --json "$snap" "$what" ;;
  restore-to)
    repo=$(repo_of "${2:-}"); snap=${3:-}; what=$(realpath -m -- "${4:-/}")
    [ -n "$repo" ] && valid_snap "$snap" && allowed "$what" || { echo '{"error":"not allowed"}'; exit 2; }
    me=$(stat -c %U "$HOME_DIR"); dest="$HOME_DIR/Restored/$(date +%Y-%m-%d)"
    tmp=$(mktemp -d /var/tmp/olympus-restore.XXXXXX)
    if ! "$R" -r "$repo" restore "$snap" --host homelab --target "$tmp" --include "$what" >/dev/null 2>&1 || [ ! -e "$tmp$what" ]; then
      rm -rf "$tmp"; echo '{"error":"restore failed"}'; exit 1
    fi
    mkdir -p "$dest"; name=$(basename "$what"); out="$dest/$name"; n=2
    while [ -e "$out" ]; do out="$dest/$name ($n)"; n=$((n + 1)); done     # never overwrite
    mv "$tmp$what" "$out"; rm -rf "$tmp"
    chown -R "$me": "$HOME_DIR/Restored"
    python3 -c 'import json,sys; print(json.dumps({"to": sys.argv[1]}))' "$out" ;;
  *) sed -n '2,7p' "$0" ;;
esac
RUNNER
sudo chmod 755 /usr/local/bin/homelab-backup

# ---------------------------------------------------------------- 4. HDD repository
say "Setting up the HDD backup"
if mountpoint -q /mnt/backup; then
  if ! sudo test -f /mnt/backup/restic/config; then
    sudo RESTIC_PASSWORD_FILE="$CONF/restic-pass" /usr/local/bin/restic -r /mnt/backup/restic init
  else echo "Already set up."; fi
else
  echo "⚠ The backup drive isn't mounted at /mnt/backup — run: sudo mount /mnt/backup"; exit 1
fi

# ---------------------------------------------------------------- 5. Koofr
say "Koofr (offsite copy)"
if [ "$KOOFR_ENABLED" = 1 ]; then
  echo "Already set up."
else
  read -rp "Set up Koofr now? You'll need your Koofr email and the 'homelab-backup' app password. [y/N] " yn </dev/tty
  if [[ "$yn" =~ ^[Yy] ]]; then
    read -rp "Koofr email: " KEMAIL </dev/tty
    read -rsp "Koofr app password (hidden as you type): " KPASS </dev/tty; echo
    sudo RCLONE_CONFIG="$CONF/rclone.conf" rclone config create koofr koofr \
      provider=koofr user="$KEMAIL" password="$KPASS" --obscure >/dev/null
    unset KPASS
    sudo chmod 600 "$CONF/rclone.conf"
    if sudo RCLONE_CONFIG="$CONF/rclone.conf" rclone lsd koofr: >/dev/null 2>&1; then
      echo "✓ Logged in to Koofr"
      if ! sudo RCLONE_CONFIG="$CONF/rclone.conf" rclone lsf koofr:homelab-backup/config >/dev/null 2>&1; then
        sudo RCLONE_CONFIG="$CONF/rclone.conf" RESTIC_PASSWORD_FILE="$CONF/restic-pass" \
          /usr/local/bin/restic -r rclone:koofr:homelab-backup init
      fi
      KOOFR_ENABLED=1; write_env
    else
      echo "✗ Koofr login failed — check the email / app password, then run this script again. rclone said:"
      sudo RCLONE_CONFIG="$CONF/rclone.conf" rclone lsd koofr: 2>&1 | tail -n 3 || true
      sudo rm -f "$CONF/rclone.conf"
    fi
  else
    echo "Skipped — run this script again any time to add it."
  fi
fi

# ---------------------------------------------------------------- 6. nightly timer
say "Scheduling it for 3am every night (or at the next start-up if the PC was off)"
sudo tee /etc/systemd/system/homelab-backup.service >/dev/null <<'EOF'
[Unit]
Description=Home lab backup (restic → HDD + Koofr)
After=network-online.target docker.service mnt-backup.mount
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/homelab-backup backup
Nice=10
IOSchedulingClass=idle
EOF
sudo tee /etc/systemd/system/homelab-backup.timer >/dev/null <<'EOF'
[Unit]
Description=Nightly home lab backup

[Timer]
OnCalendar=*-*-* 03:00
Persistent=true
RandomizedDelaySec=10m

[Install]
WantedBy=timers.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now homelab-backup.timer >/dev/null
systemctl list-timers homelab-backup.timer --no-pager 2>/dev/null | head -2 || true

# ---------------------------------------------------------------- 7. first run + test
say "Running the first backup now"
sudo /usr/local/bin/homelab-backup backup || true
say "Test restore"
sudo /usr/local/bin/homelab-backup test || true

if [ $NEW_PASS = 1 ]; then
  say "WRITE THIS DOWN — your backup password (it's on the PC too, but if the PC dies you need it)"
  echo
  echo "     ┌───────────────────────────────────────────┐"
  printf '     │   %-38s  │\n' "$(sudo cat "$CONF/restic-pass")"
  echo "     └───────────────────────────────────────────┘"
  echo "     Put it on paper with your Koofr login. Don't send it to anyone (including Claude)."
  echo "     Show it again later with:  sudo cat $CONF/restic-pass"
  sudo touch "$CONF/.password-shown"
fi
say "Done. Handy commands: homelab-backup | homelab-backup list | homelab-backup test | homelab-backup check"
