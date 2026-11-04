#!/usr/bin/env bash
# Backs up btc_agent/databases/trading.db to S3, or restores it from S3, so the portfolio survives the instance being
# restarted, stopped, or terminated and replaced.
#
#   db_backup.sh backup   # Snapshot the live database and upload it (run hourly by btc-agent-db-backup.timer, and on shutdown)
#   db_backup.sh restore  # On boot: if there's no local database, download the latest backup (btc-agent-db-restore.service)
#
# Each backup uploads two copies: <prefix>/trading.db (the latest, which restore uses) and a timestamped copy under
# <prefix>/history/ (kept for 30 days by the bucket's lifecycle rule; see README), so an older state can be recovered.
#
# Requires: the AWS CLI, Python 3 (for SQLite's online backup), and BACKUP_S3_URI set to e.g. s3://my-bucket/btc-agent
# (stored in Parameter Store as /btc-agent/BACKUP_S3_URI, so it arrives in /etc/btc-agent/env with the other keys).
# Runs as the btc-agent user; the instance role must allow s3:GetObject, s3:PutObject and s3:ListBucket (iam-policy.json).

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" # btc_agent/, from this script's location in btc_agent/deploy/
DB_PATH="${DB_PATH:-${APP_DIR}/databases/trading.db}"
PYTHON="${PYTHON:-$( [ -x "${APP_DIR}/../.venv/bin/python" ] && echo "${APP_DIR}/../.venv/bin/python" || echo python3 )}"

if [ -z "${BACKUP_S3_URI:-}" ]; then
    echo "BACKUP_S3_URI isn't set (e.g. s3://my-bucket/btc-agent); add it to Parameter Store as /btc-agent/BACKUP_S3_URI." >&2
    exit 1
fi
S3_PREFIX="${BACKUP_S3_URI%/}"
BUCKET="${S3_PREFIX#s3://}"; BUCKET="${BUCKET%%/*}"
KEY_PREFIX="${S3_PREFIX#s3://"${BUCKET}"}"; KEY_PREFIX="${KEY_PREFIX#/}"
LATEST_KEY="${KEY_PREFIX:+${KEY_PREFIX}/}trading.db"

TMP_DB="$(mktemp)"; ERROR_FILE="$(mktemp)"
trap 'rm -f "$TMP_DB" "$ERROR_FILE"' EXIT

# Snapshot $1 into $2 with SQLite's online backup API: a consistent copy even if the agent is writing at that moment
# (copying the file directly could catch it half-written)
snapshot() {
    "$PYTHON" - "$1" "$2" <<'EOF'
import sqlite3, sys
source, target = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
with target:
    source.backup(target)
source.close(); target.close()
EOF
}

# Exit non-zero unless $1 is an intact SQLite database containing the app's tables
check_db() {
    "$PYTHON" - "$1" <<'EOF'
import sqlite3, sys
from pathlib import Path
try:
    connection = sqlite3.connect(Path(sys.argv[1]).resolve().as_uri() + "?mode=ro", uri=True)
    result = connection.execute("PRAGMA integrity_check").fetchone()[0]
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
except sqlite3.DatabaseError as error:
    sys.exit(f"Database check failed: not a readable SQLite database ({error})")
missing = {"portfolio", "trades", "cash"} - tables
if result != "ok" or missing:
    sys.exit(f"Database check failed: integrity_check={result!r}, missing tables={sorted(missing)}")
EOF
}

backup() {
    if [ ! -f "$DB_PATH" ]; then
        echo "No database at ${DB_PATH} yet; nothing to back up."
        return 0
    fi
    local stamp
    snapshot "$DB_PATH" "$TMP_DB"
    check_db "$TMP_DB"
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    aws s3 cp --only-show-errors "$TMP_DB" "s3://${BUCKET}/${LATEST_KEY}"
    aws s3 cp --only-show-errors "$TMP_DB" "s3://${BUCKET}/${KEY_PREFIX:+${KEY_PREFIX}/}history/trading-${stamp}.db"
    echo "Backed up ${DB_PATH} to s3://${BUCKET}/${LATEST_KEY} (and history/trading-${stamp}.db)."
}

restore() {
    if [ -s "$DB_PATH" ]; then
        # Normal reboot or stop/start: the local database survived and is newer than any backup, so keep it
        echo "Local database ${DB_PATH} exists; keeping it (no restore needed)."
        return 0
    fi

    if ! aws s3api head-object --bucket "$BUCKET" --key "$LATEST_KEY" >/dev/null 2>"$ERROR_FILE"; then
        if grep -q "Not Found\|(404)" "$ERROR_FILE"; then
            echo "No backup at s3://${BUCKET}/${LATEST_KEY}; starting with a new database (first deployment)."
            return 0
        fi
        # Anything else (access denied, network, wrong region) must stop the app from starting: otherwise it would create a
        # fresh $10,000 portfolio and the next hourly backup would overwrite the real one in S3
        echo "Couldn't check for a backup at s3://${BUCKET}/${LATEST_KEY}, so the app won't start with an empty database:" >&2
        cat "$ERROR_FILE" >&2
        return 1
    fi

    aws s3 cp --only-show-errors "s3://${BUCKET}/${LATEST_KEY}" "$TMP_DB"
    check_db "$TMP_DB"
    mkdir -p "$(dirname "$DB_PATH")"
    cp "$TMP_DB" "${DB_PATH}.restoring" && mv "${DB_PATH}.restoring" "$DB_PATH" # Same-directory rename, so the database appears all at once
    chmod 640 "$DB_PATH"
    echo "Restored ${DB_PATH} from s3://${BUCKET}/${LATEST_KEY}."
}

case "${1:-}" in
    backup) backup ;;
    restore) restore ;;
    *) echo "Usage: $0 backup|restore" >&2; exit 2 ;;
esac
