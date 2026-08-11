#!/usr/bin/env python3
"""
Pull new noise sessions from the peer Pi.
Run every 15 minutes via noise-sync.timer.
"""
import os
import json
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone

_env_file = Path(__file__).parent / '.env'
if _env_file.exists():
    with open(_env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, _, v = line.partition('=')
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))

from noise_db import init_db, import_sessions, get_last_sync_time, update_last_sync_time

PEER_URL = os.environ.get('PEER_URL', '')
PI_NAME  = os.environ.get('PI_NAME', 'Pi')

if not PEER_URL:
    print("No PEER_URL set — skipping sync")
    exit(0)

init_db()
since = get_last_sync_time()
synced_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')

print(f"Syncing from {PEER_URL} (since {since})")

try:
    url = f"{PEER_URL.rstrip('/')}/api/sync?since={since}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.loads(resp.read())

    sessions = data.get('sessions', [])
    peer_name = data.get('pi_name', 'peer')
    print(f"Received {len(sessions)} session(s) from {peer_name}")

    if sessions:
        n = import_sessions(sessions)
        print(f"Imported {n} session(s)")
    else:
        print("Nothing new")

    update_last_sync_time(synced_at)

except Exception as e:
    print(f"Sync failed: {e}")
    exit(1)
