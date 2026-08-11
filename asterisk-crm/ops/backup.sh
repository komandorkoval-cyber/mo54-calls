#!/bin/bash
set -euo pipefail
PROJECT_DIR="${PROJECT_DIR:-/opt/asterisk-crm}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/mo54-calls}"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
cd "$PROJECT_DIR"
set -a
. ./.env
set +a
docker compose exec -T postgres pg_dump \
  -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom \
  > "$BACKUP_DIR/calls-$stamp.dump"
sha256sum "$BACKUP_DIR/calls-$stamp.dump" > "$BACKUP_DIR/calls-$stamp.dump.sha256"
find "$BACKUP_DIR" -type f -mtime +30 -delete
echo "Backup created: $BACKUP_DIR/calls-$stamp.dump"
