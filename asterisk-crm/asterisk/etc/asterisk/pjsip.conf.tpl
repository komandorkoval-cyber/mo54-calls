; ──────────────────────────────────────────────────────
; ТРАНСПОРТ
; ──────────────────────────────────────────────────────
[transport-udp]
type=transport
protocol=udp
bind=0.0.0.0
external_signaling_address=${ASTERISK_PUBLIC_IP}
external_media_address=${ASTERISK_PUBLIC_IP}
local_net=172.16.0.0/12
local_net=192.168.0.0/16

; ──────────────────────────────────────────────────────
; SIP-ТРАНК НОВОФОН (IP-авторизация, без логина/пароля)
; Новофон идентифицирует нас по IP VPS, мы его — по SIP_SERVER_IP
; ──────────────────────────────────────────────────────
[sip-trunk-aor]
type=aor
contact=sip:${SIP_SERVER}

[sip-trunk]
type=endpoint
context=from-trunk
disallow=all
allow=ulaw,alaw,g729
aors=sip-trunk-aor
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes
direct_media=no
trust_id_inbound=yes
trust_id_outbound=yes
send_pai=yes
from_domain=${SIP_SERVER}

; Входящие от Новофона идентифицируются по IP (из .env SIP_SERVER_IP)
[sip-trunk-identify]
type=identify
endpoint=sip-trunk
match=${SIP_SERVER_IP}

; ──────────────────────────────────────────────────────
; ВНУТРЕННИЙ НОМЕР 100 (Linphone на Redmi)
; Имя секции должно совпадать с user-частью SIP URI (100)
; ──────────────────────────────────────────────────────
[100-auth]
type=auth
auth_type=userpass
username=100
password=${IGOR_EXT_PASSWORD}

[100]
type=aor
max_contacts=2
remove_existing=yes
qualify_frequency=30

[100]
type=endpoint
context=from-internal
disallow=all
allow=ulaw,alaw,g729
auth=100-auth
aors=100
callerid=Igor <100>
# Use the VPS public address in in-dialog requests as well.  Without this,
# Asterisk may advertise its Docker bridge address (172.x) in SIP headers,
# which remote softphones cannot route to.
from_domain=${ASTERISK_PUBLIC_IP}
direct_media=no
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes

; ──────────────────────────────────────────────────────
; ВТОРОЙ ТЕЛЕФОН (тест)
; ──────────────────────────────────────────────────────
