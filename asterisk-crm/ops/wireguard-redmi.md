# WireGuard for the Redmi softphone

The CRM Asterisk ports are intentionally closed to the public internet. The
Redmi connects through a single WireGuard peer (`10.66.0.2`). Linphone uses
the public Asterisk address (`72.56.36.46`) as its SIP domain, but that one IP
is routed through WireGuard, so SIP/RTP never becomes publicly reachable.

The phone configuration must use `AllowedIPs = 10.66.0.1/32, 72.56.36.46/32`,
not `0.0.0.0/0`. That sends only VPN control traffic plus CRM SIP/RTP through
the tunnel and leaves the phone's ordinary internet connection unchanged. Keep
the generated client configuration under `private/`; it contains the phone's
private key and is intentionally ignored by Git.

The Docker firewall permits SIP/RTP only from Novofon and this one WireGuard
address. Do not open ports 5060 or 10000-10200 to the general internet.
