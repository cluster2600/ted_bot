#!/bin/bash
# Find your Telegram chat_id. Run locally, NOT on the box.
#   1. Create a bot with @BotFather, copy its token.
#   2. Send any message to your new bot (or add it to a group and post there).
#   3. TELEGRAM_BOT_TOKEN=123:ABC ./get_chat_id.sh
set -euo pipefail
: "${TELEGRAM_BOT_TOKEN:?set TELEGRAM_BOT_TOKEN=<botfather token> first}"

curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates" \
  | python3 -c 'import sys,json
d=json.load(sys.stdin)
if not d.get("ok"): sys.exit("Telegram error: %s" % d)
seen=set()
for u in d.get("result", []):
    m=u.get("message") or u.get("channel_post") or {}
    c=m.get("chat", {})
    if c and c.get("id") not in seen:
        seen.add(c["id"])
        print(f"chat_id={c[\"id\"]}  type={c.get(\"type\")}  name={c.get(\"title\") or c.get(\"username\") or c.get(\"first_name\")}")
if not seen: print("No messages yet — send your bot a message, then re-run.")'
