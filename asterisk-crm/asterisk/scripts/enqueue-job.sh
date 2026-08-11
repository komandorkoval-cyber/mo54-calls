#!/bin/bash
# Вызывается из Asterisk dialplan через System() после завершения звонка.
# Аргументы: CALL_ID DIRECTION CALLER CALLEE STARTED_EPOCH DURATION REC_RX REC_TX
set -euo pipefail

CALL_ID="${1}"
DIRECTION="${2}"
CALLER="${3:-unknown}"
CALLEE="${4:-unknown}"
STARTED_EPOCH="${5}"
DURATION="${6:-0}"
REC_RX="${7}"
REC_TX="${8}"

QUEUE_DIR="${QUEUE_DIR:-/queue}"
TMP_FILE="${QUEUE_DIR}/${CALL_ID}.json.tmp"
JOB_FILE="${QUEUE_DIR}/${CALL_ID}.json"

# Конвертируем epoch → ISO 8601 UTC
STARTED_AT=$(date -u -d "@${STARTED_EPOCH}" "+%Y-%m-%dT%H:%M:%SZ" 2>/dev/null \
  || date -u -r "${STARTED_EPOCH}" "+%Y-%m-%dT%H:%M:%SZ")

# Санируем строки (убираем кавычки и слеши, чтобы не сломать JSON)
sanitize() { echo "${1}" | tr -d '"\\'; }

cat > "${TMP_FILE}" <<EOF
{
  "call_id":       "$(sanitize "${CALL_ID}")",
  "direction":     "$(sanitize "${DIRECTION}")",
  "caller_number": "$(sanitize "${CALLER}")",
  "callee_number": "$(sanitize "${CALLEE}")",
  "started_at":    "${STARTED_AT}",
  "duration_sec":  ${DURATION},
  "recording_rx":  "$(sanitize "${REC_RX}")",
  "recording_tx":  "$(sanitize "${REC_TX}")"
}
EOF

# Атомарный rename: worker видит файл только когда он полностью записан
mv "${TMP_FILE}" "${JOB_FILE}"

echo "[enqueue] job записан: ${JOB_FILE}"
