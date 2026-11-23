#!/bin/sh
# Install a DuckDNS config that arrives on stdin (run as root):
#
#   sudo sh duckdns-install-conf.sh < /path/to/duckdns.conf
#
# CR characters and blank lines are stripped first. Piping the two lines from
# PowerShell appends a Windows line ending, and a stray CR-only line makes the
# sourced config fail with a confusing "not found" error.
set -eu
umask 077

install -d -m 700 /etc/duckdns
chown root:root /etc/duckdns

tmp="$(mktemp /etc/duckdns/.conf.XXXXXX)"
trap 'rm -f "$tmp"' EXIT
tr -d '\r' | sed '/^[[:space:]]*$/d' > "$tmp"

if ! grep -Eq '^DUCKDNS_DOMAIN=[A-Za-z0-9.-]+$' "$tmp"; then
	echo "the config needs a line DUCKDNS_DOMAIN=<subdomain>" >&2
	exit 2
fi
if ! grep -Eq '^DUCKDNS_TOKEN=[A-Za-z0-9-]+$' "$tmp"; then
	echo "the config needs a line DUCKDNS_TOKEN=<token>" >&2
	exit 2
fi

chmod 600 "$tmp"
chown root:root "$tmp"
mv -f "$tmp" /etc/duckdns/duckdns.conf

# Ask DuckDNS to re-assert the record right away and print the one-word answer.
status=0
/usr/local/bin/duckdns-update || status=$?

case "$0" in
/tmp/*) rm -f "$0" ;;
esac
exit "$status"
