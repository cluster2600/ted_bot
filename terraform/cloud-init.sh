#!/bin/bash
# Rendered by Terraform (templatefile). ${...} = Terraform var, $${...} = shell var.
# Runs once as root on first boot. Full log at /var/log/ted_bot_bootstrap.log
set -euxo pipefail
exec > /var/log/ted_bot_bootstrap.log 2>&1

APP=/home/opc/ted_bot

dnf install -y python3 python3-pip git sqlite
timedatectl set-timezone UTC || true

SLUG="${git_repo_slug}"
TOKEN="${github_token}"
if [ -n "$${TOKEN}" ]; then
  REPO="https://$${TOKEN}@github.com/$${SLUG}.git"   # private repo via PAT
else
  REPO="https://github.com/$${SLUG}.git"
fi

sudo -u opc git clone "$${REPO}" "$${APP}"
cd "$${APP}"

sudo -u opc python3 -m venv .venv
sudo -u opc "$${APP}/.venv/bin/pip" install --upgrade pip
sudo -u opc "$${APP}/.venv/bin/pip" install -r requirements.txt

# Secrets -> .env (heredoc body is NOT traced by set -x, so keys stay out of the log)
cat > "$${APP}/.env" <<'ENV'
TELEGRAM_BOT_TOKEN=${telegram_bot_token}
TELEGRAM_CHAT_ID=${telegram_chat_id}
ANTHROPIC_API_KEY=${anthropic_api_key}
TED_LLM_MODEL=${ted_llm_model}
TED_LOOKBACK_DAYS=${lookback_days}
ENV
chown opc:opc "$${APP}/.env"
chmod 600 "$${APP}/.env"

# Schema, plus optional whitelist seed (commit deploy/whitelist.sql to enable)
sudo -u opc "$${APP}/.venv/bin/python" ted_scanner.py --init-db
if [ -f "$${APP}/deploy/whitelist.sql" ]; then
  sudo -u opc sqlite3 "$${APP}/ted.db" < "$${APP}/deploy/whitelist.sql"
fi

# Daily scan at 07:45 UTC, logs to ~/ted_scanner.log
cat > /tmp/ted.cron <<'CRON'
CRON_TZ=UTC
45 7 * * * cd /home/opc/ted_bot && set -a && . /home/opc/ted_bot/.env && set +a && .venv/bin/python3 ted_scanner.py >> /home/opc/ted_scanner.log 2>&1
CRON
sudo -u opc crontab /tmp/ted.cron
rm -f /tmp/ted.cron

# One dry run to validate wiring (sends nothing, records nothing)
sudo -u opc bash -c 'cd /home/opc/ted_bot && set -a && . .env && set +a && .venv/bin/python ted_scanner.py --dry-run -v' || true

echo "ted_bot bootstrap complete"
