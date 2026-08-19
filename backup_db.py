#!/usr/bin/env python3
"""
Nightly SQLite backup for the noise-meter database.

Run by noise-backup.timer (daily, 03:00) via noise-backup.service, installed
by setup.sh. Uses sqlite3.Connection.backup() — SQLite's own online backup
API, the same thing the `sqlite3 ... ".backup"` CLI command drives — rather
than `cp`. get_db() puts the database in WAL mode (noise_db.py), which keeps
recently-committed pages in a separate `noise.db-wal` file until the next
checkpoint; a plain `cp` of `noise.db` alone can run mid-write and produce a
copy that looks intact but is quietly missing the newest data. The backup API
does not have that problem, and doesn't require the sqlite3 CLI to be
installed on the Pi at all — only the sqlite3 module already in the standard
library.

Restore (stop the app first, so nothing is writing to the file being replaced):

    sudo systemctl stop noise-app
    gunzip -k /home/flightdata/backups/noise-20260819.db.gz
    cp /home/flightdata/backups/noise-20260819.db /home/flightdata/noise-meter/noise.db
    rm -f /home/flightdata/noise-meter/noise.db-wal /home/flightdata/noise-meter/noise.db-shm
    sudo systemctl start noise-app

(The restored file starts a fresh WAL cycle on the next write, so any stale
-wal/-shm files next to the *old* database should not be carried over.)
"""
import glob
import gzip
import logging
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
log = logging.getLogger('noise.backup')

DB_PATH     = os.environ.get('NOISE_DB_PATH', '/home/flightdata/noise-meter/noise.db')
BACKUP_DIR  = os.environ.get('BACKUP_DIR', '/home/flightdata/backups')
PEER_SSH    = os.environ.get('PEER_SSH', '').strip()
RETAIN_DAYS = int(os.environ.get('BACKUP_RETAIN_DAYS', '14'))

PEER_BACKUP_PATH = '/home/flightdata/backups/peer'


def backup_once():
    """.backup() the live database to a fresh, gzip-compressed file. Returns
    the gzip path, or None if there was nothing to back up."""
    if not os.path.exists(DB_PATH):
        log.error('NOISE_DB_PATH %s does not exist — nothing to back up', DB_PATH)
        return None

    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d')
    raw_path = os.path.join(BACKUP_DIR, f'noise-{stamp}.db')
    gz_path = raw_path + '.gz'

    src = sqlite3.connect(DB_PATH)
    try:
        dst = sqlite3.connect(raw_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    with open(raw_path, 'rb') as f_in, gzip.open(gz_path, 'wb') as f_out:
        shutil.copyfileobj(f_in, f_out)
    os.remove(raw_path)
    log.info('Backed up %s -> %s', DB_PATH, gz_path)
    return gz_path


def rotate():
    """Delete local backups older than RETAIN_DAYS."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETAIN_DAYS)
    for path in glob.glob(os.path.join(BACKUP_DIR, 'noise-*.db.gz')):
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
        except OSError:
            continue
        if mtime < cutoff:
            try:
                os.remove(path)
                log.info('Rotated out old backup %s', path)
            except OSError as e:
                log.warning('Could not remove old backup %s: %s', path, e)


def push_to_peer(gz_path):
    """Best-effort copy of tonight's backup to the peer Pi's backups/peer/
    directory. Never fatal: a peer that is offline or unreachable must not
    fail the nightly job, and a backup that only exists locally is still a
    backup."""
    if not PEER_SSH or not gz_path:
        return
    try:
        subprocess.run(['ssh', PEER_SSH, 'mkdir', '-p', PEER_BACKUP_PATH],
                       check=True, timeout=30, capture_output=True)
        subprocess.run(['scp', '-q', gz_path, f'{PEER_SSH}:{PEER_BACKUP_PATH}/'],
                       check=True, timeout=120, capture_output=True)
        log.info('Copied %s to peer (%s:%s)', os.path.basename(gz_path),
                 PEER_SSH, PEER_BACKUP_PATH)
    except Exception as e:
        log.warning('Could not copy backup to peer %s: %s', PEER_SSH, e)


def main():
    gz_path = backup_once()
    rotate()
    push_to_peer(gz_path)


if __name__ == '__main__':
    main()
