#!/bin/sh
# Refresh the DuckDNS record so the free hostname is not deleted after 30
# idle days. DuckDNS counts any successful update as activity.
#
# Configuration lives in /etc/duckdns/duckdns.conf (root only, mode 600):
#
#   DUCKDNS_DOMAIN=idan-python
#   DUCKDNS_TOKEN=<the token shown on duckdns.org>
#
# The call passes an empty ip=, so DuckDNS stores the address the request
# came from - the server's own reserved public IP. Nothing to keep in sync.
set -eu

CONF=/etc/duckdns/duckdns.conf
if [ ! -r "$CONF" ]; then
	echo "missing $CONF" >&2
	exit 1
fi

# shellcheck disable=SC1090
. "$CONF"

if [ -z "${DUCKDNS_DOMAIN:-}" ] || [ -z "${DUCKDNS_TOKEN:-}" ]; then
	echo "DUCKDNS_DOMAIN or DUCKDNS_TOKEN is empty in $CONF" >&2
	exit 1
fi

response=$(curl -fsS --max-time 20 \
	"https://www.duckdns.org/update?domains=${DUCKDNS_DOMAIN}&token=${DUCKDNS_TOKEN}&ip=" \
	2>&1 || true)

echo "$(date -Is) ${response:-no response}"

case "$response" in
	OK*) exit 0 ;;
	*) exit 1 ;;
esac
