#!/bin/bash
set -euo pipefail
if [ "$#" -ne 1 ]; then echo "Usage: $0 /path/to/backup.dump"; exit 2; fi
backup="$(realpath "$1")"
test -f "$backup"
container="mo54-calls-restore-check"
docker run --rm -d --name "$container" -e POSTGRES_PASSWORD=restore_test postgres:16-alpine >/dev/null
trap 'docker rm -f "$container" >/dev/null 2>&1 || true' EXIT
until docker exec "$container" pg_isready -U postgres >/dev/null 2>&1; do sleep 1; done
docker cp "$backup" "$container:/tmp/backup.dump"
docker exec "$container" createdb -U postgres restore_check
docker exec "$container" pg_restore -U postgres -d restore_check --exit-on-error /tmp/backup.dump
docker exec "$container" psql -U postgres -d restore_check -Atc \
  "SELECT 'calls='||count(*) FROM calls; SELECT 'contacts='||count(*) FROM contacts;"
echo "Restore check passed."

