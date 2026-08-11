#!/usr/bin/env bash
# Read-only post-deployment verification for the dedicated MO54 Calls host.
set -Eeuo pipefail

DOMAIN="${CALLS_DOMAIN:-calls.xn--54-6kclkrncnpj3r.xn--p1ai}"
MO54_URL="${MO54_URL:-https://app.xn--54-6kclkrncnpj3r.xn--p1ai/}"

if [[ "$DOMAIN" != "calls.xn--54-6kclkrncnpj3r.xn--p1ai" ]]; then
  echo "Refusing an unexpected domain: $DOMAIN" >&2
  exit 1
fi

echo "[1/5] nginx syntax"
nginx -t

echo "[2/5] nginx service"
systemctl is-active --quiet nginx
echo "nginx: active"

echo "[3/5] MO54 Calls HTTPS health"
calls_status="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' --connect-timeout 8 --max-time 20 "https://${DOMAIN}/health")"
if [[ ! "$calls_status" =~ ^2 ]]; then
  echo "MO54 Calls health returned HTTP ${calls_status}" >&2
  exit 1
fi
echo "MO54 Calls health: HTTP ${calls_status}"

echo "[4/5] existing MO54 HTTPS reachability (read-only)"
mo54_status="$(curl --silent --show-error --location --output /dev/null --write-out '%{http_code}' --connect-timeout 8 --max-time 20 "$MO54_URL")"
if [[ ! "$mo54_status" =~ ^[23] ]]; then
  echo "Existing MO54 URL returned HTTP ${mo54_status}: ${MO54_URL}" >&2
  exit 1
fi
echo "Existing MO54: HTTP ${mo54_status}"

echo "[5/5] expected listener separation"
ss -ltn '( sport = :443 )' || true
echo "Do not test the Novofon webhook from this VPS: nginx correctly permits only 37.139.38.215."
echo "Ingress check passed."
