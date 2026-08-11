#!/bin/bash
set -euo pipefail
PROJECT_DIR="${PROJECT_DIR:-/opt/asterisk-crm}"
cd "$PROJECT_DIR"
set -a
. ./.env
set +a
docker compose ps
docker compose exec -T asterisk asterisk -rx 'pjsip show endpoints'
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "SELECT processing_status,count(*) FROM calls GROUP BY processing_status ORDER BY 1"
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "SELECT status,count(*) FROM processing_jobs GROUP BY status ORDER BY 1"
df -h /var/lib/docker
