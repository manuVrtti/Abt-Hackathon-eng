#!/usr/bin/env bash
# Daily database backup to S3-compatible object storage (run from cron):
#   15 3 * * *  cd /srv/hackeval && scripts/backup.sh >> data/backup.log 2>&1
# Needs the aws CLI configured for your storage (BACKUP_BUCKET, optional BACKUP_ENDPOINT).
set -euo pipefail
dump=$(hackeval backup)
args=()
[ -n "${BACKUP_ENDPOINT:-}" ] && args+=(--endpoint-url "$BACKUP_ENDPOINT")
aws s3 cp "${args[@]}" "$dump" "s3://${BACKUP_BUCKET:?set BACKUP_BUCKET}/hackeval/$(basename "$dump")"
find "$(dirname "$dump")" -name 'hackeval-*' -mtime +3 -delete
echo "backed up $dump"
