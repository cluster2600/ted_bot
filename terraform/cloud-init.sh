#!/bin/bash
# Rendered by Terraform's templatefile: Terraform values use braced
# interpolation; shell variables deliberately use the unbraced $NAME form.
# Runs once as root on first boot. Full log at /var/log/ted_bot_bootstrap.log
set -euxo pipefail
exec > /var/log/ted_bot_bootstrap.log 2>&1

APP=/home/opc/ted_bot

# Swap 3G AVANT tout dnf/pip : 1 GB seul fait OOM-killer sur dnf (constaté).
# dd (pas fallocate) -> fichier sans trous, sinon swapon refuse et on reste à 1 GB.
if ! swapon --show 2>/dev/null | grep -q /swapfile; then
  dd if=/dev/zero of=/swapfile bs=1M count=3072 status=none
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
swapon --show   # trace le swap actif dans le log de bootstrap

dnf install -y python3 python3-pip git sqlite
timedatectl set-timezone UTC || true

# shellcheck disable=SC2154 # Value is injected by Terraform's templatefile.
SLUG="${git_repo_slug}"
REPO="https://github.com/$SLUG.git"

sudo -u opc git clone "$REPO" "$APP"
cd "$APP"

sudo -u opc python3 -m venv .venv
sudo -u opc "$APP/.venv/bin/pip" install --upgrade pip
sudo -u opc "$APP/.venv/bin/pip" install -r requirements.txt

# log rotation for the daily scan log
install -m 0644 "$APP/deploy/ted_bot.logrotate" /etc/logrotate.d/ted_bot

# Schema, plus optional whitelist seed (commit deploy/whitelist.sql to enable)
sudo -u opc "$APP/.venv/bin/python" ted_scanner.py --init-db
if [ -f "$APP/deploy/whitelist.sql" ]; then
  sudo -u opc bash -c 'sqlite3 "$1" < "$2"' _ "$APP/ted.db" "$APP/deploy/whitelist.sql"
fi

# Daily scan at 07:45 UTC, logs to ~/ted_scanner.log
cat > /tmp/ted.cron <<'CRON'
CRON_TZ=UTC
45 7 * * * cd /home/opc/ted_bot && set -a && . /home/opc/ted_bot/.env && set +a && .venv/bin/python3 ted_scanner.py >> /home/opc/ted_scanner.log 2>&1
15 3 * * 0 cd /home/opc/ted_bot && set -a && . /home/opc/ted_bot/.env && set +a && bash deploy/backup.sh >> /home/opc/ted_scanner.log 2>&1
CRON
sudo -u opc crontab /tmp/ted.cron
rm -f /tmp/ted.cron

echo "ted_bot bootstrap complete; application secrets pending"
