#!/bin/bash
# Weekly SQLite backup (whitelist + processed notices + alert evaluations).
# Keeps 4 local snapshots
# and, if OCI_BACKUP_BUCKET is set and the oci CLI is present, uploads one copy.
# cloud-init installs a weekly cron for this. Safe to run by hand any time.
set -euo pipefail

APP="${APP:-/home/opc/ted_bot}"
DB="$APP/ted.db"
DEST="$APP/backups"
mkdir -p "$DEST"

[ -f "$DB" ] || { echo "no db at $DB"; exit 0; }

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$DEST/ted_$STAMP.db"

# .backup is safe against a live WAL database (unlike cp)
sqlite3 "$DB" ".backup '$OUT'"
gzip -f "$OUT"
OUT="$OUT.gz"
echo "backup: $OUT"

# keep the 4 newest local snapshots
ls -1t "$DEST"/ted_*.db.gz 2>/dev/null | tail -n +5 | xargs -r rm -f

# optional off-box copy
if [ -n "${OCI_BACKUP_BUCKET:-}" ] && command -v oci >/dev/null 2>&1; then
  oci os object put --bucket-name "$OCI_BACKUP_BUCKET" \
    --file "$OUT" --name "ted_bot/$(basename "$OUT")" --force >/dev/null \
    && echo "uploaded to oci://$OCI_BACKUP_BUCKET/ted_bot/$(basename "$OUT")"
fi
