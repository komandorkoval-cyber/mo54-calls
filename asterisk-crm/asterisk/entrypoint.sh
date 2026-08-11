#!/bin/bash
set -euo pipefail

# Генерируем pjsip.conf из шаблона, подставляя переменные окружения.
# Остальные конфиги (extensions.conf, rtp.conf и т.д.) — статические.
envsubst '${ASTERISK_PUBLIC_IP} ${SIP_SERVER} ${SIP_SERVER_IP} ${IGOR_EXT_PASSWORD}' \
    < /etc/asterisk/pjsip.conf.tpl \
    > /etc/asterisk/pjsip.conf

echo "[entrypoint] pjsip.conf сгенерирован из шаблона"

# Убедиться, что каталоги для записей и Asterisk существуют
mkdir -p /recordings /queue /var/lib/asterisk /var/run/asterisk /var/log/asterisk

exec asterisk -f -C /etc/asterisk/asterisk.conf
