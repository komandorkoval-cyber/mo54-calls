#!/usr/bin/env bash
# Install or switch only the separate MO54 Calls nginx virtual host.
# It never edits, disables, reloads from, or otherwise changes /opt/mo54.
set -Eeuo pipefail

MODE="${1:---prepare}"
case "$MODE" in
  --prepare|--enable-https) ;;
  *)
    echo "Usage: $0 [--prepare|--enable-https]" >&2
    exit 2
    ;;
esac

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root (for example: sudo $0 $MODE)." >&2
  exit 1
fi

PROJECT_DIR="${PROJECT_DIR:-/opt/asterisk-crm}"
DOMAIN="${CALLS_DOMAIN:-calls.xn--54-6kclkrncnpj3r.xn--p1ai}"
EXPECTED_IP="${CRM_VPS_PUBLIC_IP:-}"
ACME_WEBROOT="${ACME_WEBROOT:-/var/www/letsencrypt}"
NGINX_SITES_AVAILABLE="${NGINX_SITES_AVAILABLE:-/etc/nginx/sites-available}"
NGINX_SITES_ENABLED="${NGINX_SITES_ENABLED:-/etc/nginx/sites-enabled}"
NGINX_CONF_D="${NGINX_CONF_D:-/etc/nginx/conf.d}"
SITE_NAME="mo54-calls-${DOMAIN}.conf"
SITE_PATH="${NGINX_SITES_AVAILABLE}/${SITE_NAME}"
ENABLED_PATH="${NGINX_SITES_ENABLED}/${SITE_NAME}"
RATE_PATH="${NGINX_CONF_D}/mo54-calls-rate-limit.conf"

if [[ "$DOMAIN" != "calls.xn--54-6kclkrncnpj3r.xn--p1ai" ]]; then
  echo "Refusing an unexpected domain: $DOMAIN" >&2
  exit 1
fi
if [[ "$ACME_WEBROOT" != /* ]]; then
  echo "ACME_WEBROOT must be an absolute path." >&2
  exit 1
fi
if [[ ! -d "$PROJECT_DIR/ops/nginx" ]]; then
  echo "Cannot find $PROJECT_DIR/ops/nginx. Run this from the deployed CRM project." >&2
  exit 1
fi
for required_dir in "$NGINX_SITES_AVAILABLE" "$NGINX_SITES_ENABLED" "$NGINX_CONF_D"; do
  if [[ ! -d "$required_dir" ]]; then
    echo "Required nginx directory is missing: $required_dir" >&2
    exit 1
  fi
done
command -v nginx >/dev/null || { echo "nginx is not installed." >&2; exit 1; }

# Read the value without sourcing .env: passwords may contain shell syntax.
if [[ -z "$EXPECTED_IP" && -r "$PROJECT_DIR/.env" ]]; then
  EXPECTED_IP="$(sed -n 's/^ASTERISK_PUBLIC_IP=//p' "$PROJECT_DIR/.env" | head -n1 | tr -d '\r')"
fi
if [[ -z "$EXPECTED_IP" || "$EXPECTED_IP" == "1.2.3.4" ]]; then
  echo "Set CRM_VPS_PUBLIC_IP to the public IPv4 before running this script." >&2
  echo "Example: sudo CRM_VPS_PUBLIC_IP=72.56.36.46 $0 $MODE" >&2
  exit 1
fi

resolved_ips="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk '{print $1}' | sort -u || true)"
if ! grep -qxF "$EXPECTED_IP" <<<"$resolved_ips"; then
  echo "DNS check failed: $DOMAIN does not currently resolve to $EXPECTED_IP." >&2
  echo "Resolved IPv4 values: ${resolved_ips:-none}" >&2
  echo "Create/fix the A record first; no nginx files were changed." >&2
  exit 1
fi

if [[ "$MODE" == "--enable-https" ]]; then
  template="$PROJECT_DIR/ops/nginx/calls-https.conf.template"
  if [[ ! -r "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" || ! -r "/etc/letsencrypt/live/${DOMAIN}/privkey.pem" ]]; then
    echo "No Let's Encrypt certificate found for $DOMAIN." >&2
    echo "Run --prepare, obtain the certificate, then run --enable-https." >&2
    exit 1
  fi
else
  template="$PROJECT_DIR/ops/nginx/calls-http.conf.template"
fi

for required_file in "$template" "$PROJECT_DIR/ops/nginx/mo54-calls-rate-limit.conf"; do
  [[ -r "$required_file" ]] || { echo "Missing required file: $required_file" >&2; exit 1; }
done

# Do not overwrite a configuration owned by something else.
if [[ -e "$ENABLED_PATH" && ! -L "$ENABLED_PATH" ]]; then
  echo "Refusing to overwrite non-symlink $ENABLED_PATH." >&2
  exit 1
fi
if [[ -L "$ENABLED_PATH" && "$(readlink -f "$ENABLED_PATH")" != "$SITE_PATH" ]]; then
  echo "Refusing to replace $ENABLED_PATH because it points elsewhere." >&2
  exit 1
fi
if [[ -e "$RATE_PATH" ]] && ! cmp -s "$PROJECT_DIR/ops/nginx/mo54-calls-rate-limit.conf" "$RATE_PATH"; then
  echo "Refusing to overwrite different rate-limit file: $RATE_PATH" >&2
  exit 1
fi

install -d -m 0755 "$ACME_WEBROOT"
candidate="$(mktemp "${NGINX_SITES_AVAILABLE}/.${SITE_NAME}.XXXXXX")"
sed \
  -e "s|__DOMAIN__|${DOMAIN}|g" \
  -e "s|__ACME_WEBROOT__|${ACME_WEBROOT}|g" \
  "$template" >"$candidate"

backup=""
site_was_new=false
enabled_was_new=false
rate_was_new=false
rollback() {
  rm -f "$candidate"
  if [[ -n "$backup" && -f "$backup" ]]; then
    install -m 0644 "$backup" "$SITE_PATH"
    rm -f "$backup"
  elif [[ "$site_was_new" == true ]]; then
    rm -f "$SITE_PATH"
  fi
  if [[ "$enabled_was_new" == true ]]; then
    rm -f "$ENABLED_PATH"
  fi
  if [[ "$rate_was_new" == true ]]; then
    rm -f "$RATE_PATH"
  fi
}

if [[ -f "$SITE_PATH" ]]; then
  backup="$(mktemp "${NGINX_SITES_AVAILABLE}/.${SITE_NAME}.backup.XXXXXX")"
  cp -p "$SITE_PATH" "$backup"
else
  site_was_new=true
fi
install -m 0644 "$candidate" "$SITE_PATH"
rm -f "$candidate"

if [[ ! -e "$ENABLED_PATH" ]]; then
  ln -s "$SITE_PATH" "$ENABLED_PATH"
  enabled_was_new=true
fi
if [[ ! -e "$RATE_PATH" ]]; then
  install -m 0644 "$PROJECT_DIR/ops/nginx/mo54-calls-rate-limit.conf" "$RATE_PATH"
  rate_was_new=true
fi

if ! nginx -t; then
  echo "nginx configuration test failed; restoring only the MO54 Calls candidate." >&2
  rollback
  exit 1
fi

if ! systemctl reload nginx; then
  echo "nginx did not reload. The tested candidate remains in place; inspect systemctl status nginx." >&2
  exit 1
fi

rm -f "$backup"
if [[ "$MODE" == "--prepare" ]]; then
  cat <<EOF
HTTP-only ACME host is active for ${DOMAIN}. The CRM is still intentionally unavailable over HTTP.

Next, issue a certificate (replace the email):
  sudo certbot certonly --webroot -w ${ACME_WEBROOT} -d ${DOMAIN} --email you@example.com --agree-tos --no-eff-email

Then activate HTTPS:
  sudo CRM_VPS_PUBLIC_IP=${EXPECTED_IP} $0 --enable-https
EOF
else
  echo "HTTPS ingress for ${DOMAIN} is active. Existing MO54 nginx sites were not changed."
fi
