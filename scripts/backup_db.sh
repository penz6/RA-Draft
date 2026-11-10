#!/usr/bin/env bash
# Daily SQLite-consistent backup of the live RA-Draft Docker database.
# Run as the pangolin user, which already has permission to use Docker.
set -Eeuo pipefail
umask 077

BACKUP_DIR="${BACKUP_DIR:-/srv/ra_draft/backups}"
STAMP="$(date -u +%Y-%m-%d_%H%M%S)"
BACKUP_FILE="${BACKUP_DIR}/ra_draft_${STAMP}.db.gz"

mkdir -p "$BACKUP_DIR"

# Prevent a scheduled and a manual backup from running simultaneously.
exec 9>"${BACKUP_DIR}/.ra_draft_backup.lock"
if ! flock -n 9; then
  echo 'ERROR: Another RA-Draft backup is already running.' >&2
  exit 1
fi

# Compose labels survive container recreation, unlike a container ID.
CONTAINER="$(docker ps \
  --filter 'label=com.docker.compose.project=ra_draft' \
  --filter 'label=com.docker.compose.service=ra-draft' \
  --format '{{.ID}}')"
if [[ -z "$CONTAINER" || "$CONTAINER" == *$'\n'* ]]; then
  echo 'ERROR: Expected exactly one running RA-Draft application container.' >&2
  exit 1
fi

RAW_TMP="$(mktemp "${BACKUP_DIR}/.ra_draft_XXXXXX.db")"
GZ_TMP="${RAW_TMP}.gz"
trap 'rm -f "$RAW_TMP" "$GZ_TMP"' EXIT

# Create an online SQLite snapshot inside the running container and stream it
# to the host. Never copy the live .db file directly: it may be in WAL mode.
# No Docker TTY is allocated, so stdout contains only the binary DB snapshot.
docker exec "$CONTAINER" python -c '
import contextlib
import os
import shutil
import sqlite3
import sys
import tempfile

source = os.environ.get("DATABASE_PATH", "/data/ra_draft.db")
if not os.path.isfile(source):
    raise SystemExit("ERROR: Live RA-Draft database not found: " + source)

with tempfile.TemporaryDirectory(prefix="ra-draft-backup-") as directory:
    snapshot = os.path.join(directory, "snapshot.db")
    with contextlib.closing(sqlite3.connect("file:" + source + "?mode=ro", uri=True)) as original:
        with contextlib.closing(sqlite3.connect(snapshot)) as backup:
            original.backup(backup)
    with contextlib.closing(sqlite3.connect(snapshot)) as verified:
        if verified.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SystemExit("ERROR: SQLite snapshot failed integrity check")
    with open(snapshot, "rb") as stream:
        shutil.copyfileobj(stream, sys.stdout.buffer, length=1024 * 1024)
' > "$RAW_TMP"

# Verify the complete streamed file on the VPS before publishing a backup.
test -s "$RAW_TMP"
python3 - "$RAW_TMP" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as db:
    if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise SystemExit('ERROR: Streamed SQLite backup failed integrity check')
PY

gzip -9 -c "$RAW_TMP" > "$GZ_TMP"
gzip -t "$GZ_TMP"
mv "$GZ_TMP" "$BACKUP_FILE"

# Remove completed archives older than seven days, never partial files.
find "$BACKUP_DIR" -maxdepth 1 -type f \
  -name 'ra_draft_*.db.gz' -mmin +10080 -delete

echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] Backup successful: $BACKUP_FILE"
