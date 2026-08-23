#!/bin/bash
# Install TED Bot application settings from JSON received on standard input.
# Run as root on the provisioned VM; no secret is accepted on the command line.
set -euo pipefail

VALIDATE_ONLY=false
SKIP_SMOKE=false
case "${1:-}" in
  --validate-only) VALIDATE_ONLY=true; shift ;;
  --skip-smoke) SKIP_SMOKE=true; shift ;;
esac
if [ "$#" -ne 0 ]; then
  echo "usage: install-secrets.sh [--validate-only|--skip-smoke]" >&2
  exit 2
fi

APP_DIR=${TED_BOT_APP_DIR:-/home/opc/ted_bot}
APP_USER=${TED_BOT_APP_USER:-opc}
APP_GROUP=${TED_BOT_APP_GROUP:-$APP_USER}
umask 077
PAYLOAD=$(mktemp /tmp/ted-bot-secrets.XXXXXX.json)
ENV_FILE=$(mktemp /tmp/ted-bot-env.XXXXXX)
trap 'rm -f "$PAYLOAD" "$ENV_FILE"' EXIT

cat > "$PAYLOAD"

python3 - "$PAYLOAD" "$ENV_FILE" <<'PY'
import json
import re
import shlex
import sys

payload_path, env_path = sys.argv[1:]
with open(payload_path, encoding="utf-8") as handle:
    payload = json.load(handle)
if not isinstance(payload, dict):
    raise SystemExit("payload must be a JSON object")

defaults = {
    "NVIDIA_API_KEY": "",
    "TED_LLM_MODEL": "nvidia/nemotron-3-ultra-550b-a55b",
    "TED_LOOKBACK_DAYS": "3",
    "TED_HEARTBEAT_URL": "",
    "OCI_BACKUP_BUCKET": "",
    "CLOUDFLARE_API_TOKEN": "",
    "CLOUDFLARE_ACCOUNT_ID": "",
    "TED_REPORT_PROJECT": "ted-bot-j30-report",
}
required = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
allowed = set(required) | set(defaults)

unknown = sorted(set(payload) - allowed)
if unknown:
    raise SystemExit("unsupported application setting(s): " + ", ".join(unknown))

token = payload.get("TELEGRAM_BOT_TOKEN")
if not isinstance(token, str) or not token.strip():
    raise SystemExit("missing required application setting: TELEGRAM_BOT_TOKEN")

chat_id = payload.get("TELEGRAM_CHAT_ID")
if isinstance(chat_id, bool) or not isinstance(chat_id, (str, int)) or not str(chat_id).strip():
    raise SystemExit("missing required application setting: TELEGRAM_CHAT_ID")

for key, value in payload.items():
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise SystemExit(f"application setting must be a string or integer: {key}")

settings = {**defaults, **payload}
model = str(settings["TED_LLM_MODEL"])
if not re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", model):
    raise SystemExit("TED_LLM_MODEL must use the vendor/model format")

try:
    lookback_days = int(str(settings["TED_LOOKBACK_DAYS"]))
except ValueError as exc:
    raise SystemExit("TED_LOOKBACK_DAYS must be a positive integer") from exc
if lookback_days < 1:
    raise SystemExit("TED_LOOKBACK_DAYS must be a positive integer")

cloudflare_token = str(settings["CLOUDFLARE_API_TOKEN"]).strip()
cloudflare_account = str(settings["CLOUDFLARE_ACCOUNT_ID"]).strip()
if bool(cloudflare_token) != bool(cloudflare_account):
    raise SystemExit("Cloudflare report publication requires both token and account ID")
if cloudflare_account and (
    len(cloudflare_account) != 32
    or any(char not in "0123456789abcdefABCDEF" for char in cloudflare_account)
):
    raise SystemExit("CLOUDFLARE_ACCOUNT_ID has an invalid format")
project = str(settings["TED_REPORT_PROJECT"]).strip().lower()
if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,56}[a-z0-9])?", project):
    raise SystemExit("TED_REPORT_PROJECT has an invalid Pages project name")
settings["TED_REPORT_PROJECT"] = project

with open(env_path, "w", encoding="utf-8") as handle:
    for key in (*required, *defaults):
        handle.write(f"{key}={shlex.quote(str(settings[key]))}\n")
PY

if [ "$VALIDATE_ONLY" = true ]; then
  echo "TED Bot application settings are valid"
  exit 0
fi

install -o "$APP_USER" -g "$APP_GROUP" -m 0600 "$ENV_FILE" "$APP_DIR/.env"

if [ "$SKIP_SMOKE" = true ]; then
  echo "TED Bot application secrets installed"
  exit 0
fi

sudo -u "$APP_USER" bash -c \
  'cd "$1" && set -a && . .env && set +a && .venv/bin/python ted_scanner.py --dry-run -v' \
  _ "$APP_DIR"
sudo -u "$APP_USER" bash -c \
  'cd "$1" && set -a && . .env && set +a && .venv/bin/python ted_scanner.py --test-telegram' \
  _ "$APP_DIR"

echo "TED Bot application secrets installed and Telegram delivery verified"
