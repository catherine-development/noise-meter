#!/usr/bin/env python3
"""
Pull new noise sessions from the peer Pi.
Run every 15 minutes via noise-sync.timer.
"""
import logging
import os
import json
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone

# Standalone script run by noise-sync.timer (systemd), not imported by
# noise_app — its own basicConfig, going to stderr, which journald captures
# just as it did print()'s stdout.
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
log = logging.getLogger('noise.sync_peer')

_env_file = Path(__file__).parent / '.env'
if _env_file.exists():
    with open(_env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, _, v = line.partition('=')
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))

from noise_db import init_db, import_sessions
from sync_db import get_last_sync_time, update_last_sync_time
from weather import fill_weather_gaps

PEER_URL  = os.environ.get('PEER_URL', '')
PI_NAME   = os.environ.get('PI_NAME', 'Pi')
IMPORT_KEY = os.environ.get('IMPORT_API_KEY', '')

if not PEER_URL:
    log.info('No PEER_URL set — skipping sync')
    exit(0)

init_db()
since = get_last_sync_time()
synced_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')

log.info('Syncing from %s (since %s)', PEER_URL, since)

try:
    url = f"{PEER_URL.rstrip('/')}/api/sync?since={since}"
    # Cloudflare's edge bot-protection blocks urllib's default User-Agent
    # ("Python-urllib/3.x") with a 1010 error before the request even reaches
    # the app — a normal-looking UA is required to get through the tunnel.
    req = urllib.request.Request(url, headers={
        'X-Import-Key': IMPORT_KEY,
        'User-Agent': 'noise-meter-sync/1.0',
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())

    sessions = data.get('sessions', [])
    peer_name = data.get('pi_name', 'peer')
    log.info('Received %d session(s) from %s', len(sessions), peer_name)

    if sessions:
        n = import_sessions(sessions)
        log.info('Imported %d session(s)', n)
        # Weather gap-fill (WP9): a session can arrive without a weather row —
        # the sender had no coordinates yet, or its own fetch had not run.
        # Fill the gap from here with our own fetch (same archive API, same
        # coordinates; the upsert makes a double fetch harmless). Best-effort:
        # weather must never fail the sync run that already imported the data.
        try:
            filled = fill_weather_gaps(sessions)
            if filled:
                log.info('Weather gap-fill: fetched %d row(s): %s',
                         len(filled), filled)
        except Exception as e:
            log.warning('Weather gap-fill failed: %s', e)
    else:
        log.info('Nothing new')

    update_last_sync_time(synced_at)

except Exception as e:
    log.error('Sync failed: %s', e)
    exit(1)
