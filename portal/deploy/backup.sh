#!/usr/bin/env bash
# Daily database backup for the portal server.
#
# VACUUM INTO takes a consistent copy even while the grading worker is
# writing, so the portal never has to stop for a backup. Compression keeps a
# month of copies in a few megabytes; the oldest files are pruned.
#
# Installed as /usr/local/bin/portal-backup by setup_server.sh and run by
# /etc/cron.d/portal-backup. Test it once by hand; a backup that was never
# restored is only a hope.
set -euo pipefail

APP_DIR="${PORTAL_APP_DIR:-/opt/clasroom-autograder}"
DEST="${PORTAL_BACKUP_DIR:-/var/backups/portal}"
KEEP_DAYS="${PORTAL_BACKUP_KEEP_DAYS:-30}"
DB="${APP_DIR}/portal/data/portal.db"

if [ ! -f "$DB" ]; then
	echo "portal-backup: database not found at $DB" >&2
	exit 1
fi

mkdir -p "$DEST"
stamp="$(date -u +%Y%m%d-%H%M%S)"
target="${DEST}/portal-${stamp}.db"

"${APP_DIR}/.venv/bin/python" - "$DB" "$target" <<'PY'
import sqlite3
import sys

source, target = sys.argv[1], sys.argv[2]
connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
try:
    connection.execute("VACUUM INTO ?", (target,))
finally:
    connection.close()
PY

gzip -f "$target"
find "$DEST" -maxdepth 1 -type f -name 'portal-*.db.gz' -mtime "+${KEEP_DAYS}" -delete
echo "portal-backup: wrote ${target}.gz"
