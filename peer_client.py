"""
Outbound half of the peer-to-peer replication between the two Pis.

Every call here is best-effort and runs on a daemon thread: a peer being down,
slow, or unreachable must never block or fail a user-facing request. The
receiving half is the /import, /api/peer-sync and /api/peer-sync-full routes in
noise_app.py; the database half is sync_db.py.
"""
import json
import threading
import urllib.request

from config import PEER_URL, IMPORT_KEY
from sync_db import apply_full_sync

# Cloudflare's edge bot-protection blocks urllib's default User-Agent
# ("Python-urllib/3.x") with a 1010 error before the request reaches the app,
# so every outbound peer call must send a normal-looking UA.
_PEER_UA = 'noise-meter/1.0'


def push_to_peer(sessions):
    """Push sessions to peer Pi in a background thread (best-effort)."""
    if not PEER_URL or not sessions:
        return

    def _do_push():
        payload = json.dumps({'sessions': sessions}, separators=(',', ':')).encode()
        req = urllib.request.Request(
            PEER_URL.rstrip('/') + '/import',
            data=payload,
            headers={'Content-Type': 'application/json',
                     'X-Import-Key': IMPORT_KEY,
                     'User-Agent': _PEER_UA},
            method='POST',
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                print(f"Peer sync: {resp.read().decode()}")
        except Exception as e:
            print(f"Peer sync failed: {e}")

    threading.Thread(target=_do_push, daemon=True).start()


def sync_event_to_peer(entity, action, data):
    """Push a single mutation to the peer Pi. Fire-and-forget."""
    if not PEER_URL or not IMPORT_KEY:
        return
    def _do():
        try:
            payload = json.dumps({'entity': entity, 'action': action, 'data': data},
                                 default=str).encode()
            req = urllib.request.Request(
                PEER_URL.rstrip('/') + '/api/peer-sync',
                data=payload,
                headers={'Content-Type': 'application/json',
                         'X-Import-Key': IMPORT_KEY,
                         'User-Agent': _PEER_UA},
                method='POST',
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print(f'Peer sync ({entity}/{action}) failed: {e}', flush=True)
    threading.Thread(target=_do, daemon=True).start()


def startup_sync_from_peer():
    """On startup, pull full state from peer and apply it (catches up on offline period)."""
    if not PEER_URL or not IMPORT_KEY:
        return
    def _do():
        import time
        time.sleep(8)  # let both Pis finish starting before attempting peer connection
        try:
            req = urllib.request.Request(
                PEER_URL.rstrip('/') + '/api/peer-sync-full',
                headers={'X-Import-Key': IMPORT_KEY, 'User-Agent': _PEER_UA},
                method='GET',
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            apply_full_sync(data)
            print('Startup peer sync: applied full payload from peer', flush=True)
        except Exception as e:
            print(f'Startup peer sync failed: {e}', flush=True)
    threading.Thread(target=_do, daemon=True).start()
