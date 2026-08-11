#!/bin/bash
# Firewall: открыть SIP/RTP только с IP Zadarma, всё остальное — deny.
# Запускать от root на хосте VPS (не в контейнере).
# Перед запуском: задать ZADARMA_IPS и убедиться, что SSH уже в allow.
set -euo pipefail

# ─── НАСТРОИТЬ ПОД СВОЮ ИНФРАСТРУКТУРУ ──────────────────────────────────────
# IP-адреса Zadarma: взять из ЛК оператора → Настройки → Список IP-адресов.
NOVOFON_NETWORKS=(
  "37.139.38.0/24"   # official Novofon SIP network
)
RTP_START=10000
RTP_END=10200
SOFTPHONE_VPN_SUBNET="${SOFTPHONE_VPN_SUBNET:-10.8.0.0/24}"
# ─────────────────────────────────────────────────────────────────────────────

echo "[*] Базовые правила (без сброса — чтобы не трогать существующие)..."
ufw default deny incoming
ufw default allow outgoing

echo "[*] SSH — разрешить (чтобы не потерять доступ)"
ufw allow ssh

echo "[*] HTTP/HTTPS (если нужен веб-интерфейс Планировщика)"
# ufw allow http
# ufw allow https

echo "[*] SIP и RTP — только с IP Zadarma"
for ip in "${NOVOFON_NETWORKS[@]}"; do
  echo "    → разрешаю $ip: SIP 5060 udp/tcp, RTP ${RTP_START}-${RTP_END} udp"
  ufw allow from "$ip" to any port 5060 proto udp
  ufw allow from "$ip" to any port 5060 proto tcp
  ufw allow from "$ip" to any port "${RTP_START}:${RTP_END}" proto udp
done

echo "[*] SIP/RTP для софтфона — только через VPN ${SOFTPHONE_VPN_SUBNET}"
ufw allow from "$SOFTPHONE_VPN_SUBNET" to any port 5060 proto udp
ufw allow from "$SOFTPHONE_VPN_SUBNET" to any port "${RTP_START}:${RTP_END}" proto udp

echo "[*] Включаю ufw..."
ufw --force enable
ufw status verbose

echo "[OK] Firewall настроен. SIP/RTP открыты только с IP Zadarma."
