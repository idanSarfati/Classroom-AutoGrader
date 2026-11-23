#!/usr/bin/env bash
# Install / update the portal on a Debian or Ubuntu server.
#
# Run as root from the extracted project directory, after the DNS name points
# at this machine and ports 80/443 are reachable:
#
#   sudo ./portal/deploy/setup_server.sh idan-python.duckdns.org
#
# What it does, idempotently:
#   1. checks that python3 (>= 3.12), python3-venv, caddy and systemd exist;
#   2. creates the unprivileged "portal" account;
#   3. installs the pinned dependencies into ./.venv;
#   4. gives that account write access only to portal/data and portal/content;
#   5. installs and starts the systemd unit and the Caddy reverse proxy;
#   6. installs the nightly database backup.
#
# It never touches the database contents, and it is safe to re-run after an
# update (it restarts the service).
set -euo pipefail

DOMAIN="${1:-}"
if [ -z "$DOMAIN" ]; then
	echo "usage: sudo $0 <public-hostname>[, <second-hostname> ...]" >&2
	echo "example: sudo $0 idan-python.duckdns.org, 158.178.250.254.sslip.io" >&2
	exit 2
fi
# A comma-separated list is allowed so one Caddy block can serve the DuckDNS
# name and the address-derived sslip.io fallback at the same time.
BARE="$(printf '%s' "$DOMAIN" | tr -d ' \t')"
case "$BARE" in
	*[!A-Za-z0-9.,-]* | .* | *. | *, | ,* | *,,* | *..*)
		echo "not a valid hostname list: $DOMAIN" >&2
		exit 2
		;;
esac
# Caddy wants ", " between the names of one site block.
NAMES="$(printf '%s' "$BARE" | sed 's/,/, /g')"
FIRST_NAME="${BARE%%,*}"

if [ "$(id -u)" -ne 0 ]; then
	echo "run with sudo" >&2
	exit 1
fi

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SERVICE_USER="${PORTAL_SERVICE_USER:-portal}"
PYTHON="${PORTAL_PYTHON:-python3}"

missing=""
command -v "$PYTHON" >/dev/null 2>&1 || missing="$missing python3"
if command -v "$PYTHON" >/dev/null 2>&1; then
	"$PYTHON" -c 'import venv' >/dev/null 2>&1 || missing="$missing python3-venv"
	"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' \
		|| missing="$missing python3.12+"
fi
command -v caddy >/dev/null 2>&1 || missing="$missing caddy"
command -v curl >/dev/null 2>&1 || missing="$missing curl"
command -v systemctl >/dev/null 2>&1 || missing="$missing systemd"
if [ -n "$missing" ]; then
	echo "missing prerequisites:$missing" >&2
	echo "install them with:" >&2
	echo "  apt-get update && apt-get install -y python3 python3-venv caddy curl rsync" >&2
	exit 1
fi

if [ ! -f "$APP_DIR/portal/data/portal.db" ]; then
	echo "portal/data/portal.db is missing: extract the deploy bundle first" >&2
	exit 1
fi

echo "==> account"
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
	useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

echo "==> python environment"
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
	"$PYTHON" -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/python" -m pip install --upgrade pip --quiet
"$APP_DIR/.venv/bin/python" -m pip install \
	-r "$APP_DIR/portal/requirements-portal.txt" --quiet

echo "==> permissions"
chown -R root:root "$APP_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" \
	"$APP_DIR/portal/data" "$APP_DIR/portal/content"
chmod 750 "$APP_DIR/portal/data"
# API keys: readable by the service account, nobody else.
if [ -f "$APP_DIR/.env" ]; then
	chown "root:$SERVICE_USER" "$APP_DIR/.env"
	chmod 640 "$APP_DIR/.env"
fi
for secret in secret_key google_token.json; do
	if [ -f "$APP_DIR/portal/data/$secret" ]; then
		chown "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/portal/data/$secret"
		chmod 600 "$APP_DIR/portal/data/$secret"
	fi
done

echo "==> systemd"
sed "s#/opt/clasroom-autograder#${APP_DIR}#g" \
	"$APP_DIR/portal/deploy/portal.service" \
	> /etc/systemd/system/portal.service
systemctl daemon-reload
systemctl enable portal >/dev/null
systemctl restart portal

echo "==> caddy"
install -m 644 "$APP_DIR/portal/deploy/Caddyfile" /etc/caddy/Caddyfile
sed -i "s/^classroom\.example\.duckdns\.org /${NAMES} /" /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile >/dev/null
systemctl enable caddy >/dev/null
systemctl reload caddy 2>/dev/null || systemctl restart caddy

echo "==> nightly backup"
install -m 755 "$APP_DIR/portal/deploy/backup.sh" /usr/local/bin/portal-backup
cat > /etc/cron.d/portal-backup <<CRON
17 3 * * * root PORTAL_APP_DIR=${APP_DIR} /usr/local/bin/portal-backup >>/var/log/portal-backup.log 2>&1
CRON

if command -v timedatectl >/dev/null 2>&1; then
	timedatectl set-timezone Asia/Jerusalem || true
fi

echo "==> duckdns refresh"
# DuckDNS deletes a free subdomain that is not updated for 30 days, so the
# server refreshes it once a day. The token is never in this script or in the
# bundle: it is installed separately into /etc/duckdns/duckdns.conf (root, 600).
install -m 755 "$APP_DIR/portal/deploy/duckdns-update.sh" /usr/local/bin/duckdns-update
install -d -m 700 /etc/duckdns
chown root:root /etc/duckdns
install -m 644 "$APP_DIR/portal/deploy/duckdns.cron" /etc/cron.d/duckdns-update
if [ ! -r /etc/duckdns/duckdns.conf ]; then
	echo "note: /etc/duckdns/duckdns.conf is missing, so the daily refresh does"
	echo "      nothing yet. Install the token with"
	echo "      portal/scripts/install_duckdns_token.ps1 (run on your computer)."
fi

echo "==> health"
healthy=""
for _ in $(seq 1 30); do
	if curl -fsS http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
		healthy="yes"
		break
	fi
	sleep 1
done
if [ -z "$healthy" ]; then
	echo "the service did not answer on 127.0.0.1:8000" >&2
	echo "inspect: journalctl -u portal -n 80 --no-pager" >&2
	exit 1
fi

echo
echo "portal is running."
echo "  local:  http://127.0.0.1:8000/healthz"
echo "  public: https://${FIRST_NAME}/"
echo "Caddy issues a certificate for every name in the list (${NAMES})"
echo "once it resolves to this server and ports 80/443 are open."
echo "Logs: journalctl -u portal -f"
