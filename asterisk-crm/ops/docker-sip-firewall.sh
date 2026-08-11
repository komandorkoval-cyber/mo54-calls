#!/bin/sh
set -eu

CHAIN=MO54_SIP_FILTER
NOVOFON_NETWORKS=37.139.38.0/24
WIREGUARD_INTERFACE=wg0
WIREGUARD_SOFTPHONE_IP=10.66.0.2/32

iptables -N "$CHAIN" 2>/dev/null || true
iptables -F "$CHAIN"
iptables -A "$CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
iptables -A "$CHAIN" -d "$NOVOFON_NETWORKS" -p udp --dport 5060 -j RETURN
iptables -A "$CHAIN" -d "$NOVOFON_NETWORKS" -p tcp --dport 5060 -j RETURN
iptables -A "$CHAIN" -s "$NOVOFON_NETWORKS" -p udp --dport 5060 -j RETURN
iptables -A "$CHAIN" -s "$NOVOFON_NETWORKS" -p tcp --dport 5060 -j RETURN
iptables -A "$CHAIN" -s "$NOVOFON_NETWORKS" -p udp --dport 10000:10200 -j RETURN

# The only non-Novofon source permitted to reach the Docker SIP/RTP ports is
# the dedicated WireGuard address of the Redmi softphone. Linphone uses UDP.
iptables -A "$CHAIN" -i "$WIREGUARD_INTERFACE" -s "$WIREGUARD_SOFTPHONE_IP" -p udp --dport 5060 -j RETURN
iptables -A "$CHAIN" -i "$WIREGUARD_INTERFACE" -s "$WIREGUARD_SOFTPHONE_IP" -p udp --dport 10000:10200 -j RETURN
iptables -A "$CHAIN" -p udp --dport 5060 -j DROP
iptables -A "$CHAIN" -p tcp --dport 5060 -j DROP
iptables -A "$CHAIN" -p udp --dport 10000:10200 -j DROP
iptables -A "$CHAIN" -j RETURN

iptables -C DOCKER-USER -j "$CHAIN" 2>/dev/null ||
  iptables -I DOCKER-USER 1 -j "$CHAIN"
