#!/bin/bash
set -euo pipefail
PROJECT_DIR="${PROJECT_DIR:-/opt/asterisk-crm}"
cd "$PROJECT_DIR"
set -a
. ./.env
set +a
docker compose exec -T postgres psql -v ON_ERROR_STOP=1 \
  -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc \
  "SELECT storage_path FROM recordings WHERE retention_until < current_date" |
while IFS= read -r path; do
  case "$path" in
    /recordings/*) docker compose exec -T worker rm -f -- "$path" ;;
    *) echo "Refusing unsafe recording path: $path" >&2 ;;
  esac
done
docker compose exec -T postgres psql -v ON_ERROR_STOP=1 \
  -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "DELETE FROM recordings WHERE retention_until < current_date"
