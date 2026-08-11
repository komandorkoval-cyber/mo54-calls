#!/bin/bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/asterisk-crm}"
cd "$PROJECT_DIR"

echo "Existing services (read-only inventory):"
docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}" 2>/dev/null || true

if ! command -v docker >/dev/null; then
  echo "Docker is missing. Install it through the VPS provider or approved package procedure."
  exit 1
fi
docker compose version >/dev/null

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "Created .env. Set every replace_/changeme value and rerun."
  exit 1
fi

if grep -Eq 'changeme|replace_with|sk-XXXX|base64_' .env; then
  echo "Unsafe placeholder found in .env; deployment stopped."
  exit 1
fi

echo "Validating configuration and building isolated services..."
docker compose config --quiet
docker compose build asterisk worker api
docker compose up -d postgres
docker compose run --rm migrate
docker compose up -d asterisk worker api
docker compose ps

echo "Validating PJSIP and CRM health..."
docker compose exec -T asterisk asterisk -rx 'module show like res_pjsip'
docker compose exec -T asterisk asterisk -rx 'pjsip show endpoints'
docker compose exec -T api python -c \
  "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"

echo "MO54 Calls CRM deployed. Existing /opt/mo54 services were not modified."

