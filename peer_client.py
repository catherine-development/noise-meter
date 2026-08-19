"""
Outbound half of the peer-to-peer replication between the two Pis.

Every call here is best-effort and runs on a daemon thread: a peer being down,
slow, or unreachable must never block or fail a user-facing request. The
receiving half is the /import, /api/peer-sync and /api/peer-sync-full routes in
noise_app.py; the database half is sync_db.py.
"""
import json
import logging
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone

from config import PEER_URL, IMPORT_KEY
from sync_db import apply_full_sync, set_sync_state

log = logging.getLogger(__name__)

# Cloudflare's edge bot-protection blocks urllib's default User-Agent
# ("Python-urllib/3.x") with a 1010 error before the request reaches the app,
# so every outbound peer call must send a normal-looking UA.
_PEER_UA = 'noise-meter/1.0'


def _describe_error(e):
    """One line naming what went wrong, including the body of an HTTP error —
    a 500 from the peer's /import carries the exception text, which is the
    only place a failed replication used to be visible at all."""
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = e.read().decode(errors='replace')[:300]
        except Exception:
            body = ''
        return f'HTTP {e.code} from peer: {body or e.reason}'
    return f'{type(e).__name__}: {e}'


def push_to_peer(sessions):
    """Push sessions to peer Pi in a background thread (best-effort).

    Returns the thread (or None when there is nothing to do) so callers that
    need to know the outcome — tests, mainly — can join it. A failure is
    logged at WARNING and recorded in sync_state as last_push_error, which the
    upload page shows; a later success clears it. It used to print() and
    vanish, so a peer that silently refused every push looked healthy.
    """
    if not PEER_URL or not sessions:
        return None

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
        dates = ', '.join(s.get('d', '?') for s in sessions)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                log.info('Peer push of %s: %s', dates, resp.read().decode(errors='replace'))
            set_sync_state('last_push_error', None)
        except Exception as e:
            msg = _describe_error(e)
            log.warning('Peer push of %s failed: %s', dates, msg)
            try:
                set_sync_state('last_push_error', json.dumps({
                    'at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                    'dates': dates,
                    'error': msg,
                }))
            except Exception as e2:   # the database, not the peer, is the problem
                log.error('Could not record the peer push failure: %s', e2)

    t = threading.Thread(target=_do_push, daemon=True)
    t.start()
    return t


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
            log.warning('Peer sync (%s/%s) failed: %s', entity, action, e)
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
            log.info('Startup peer sync: applied full payload from peer')
        except Exception as e:
            log.warning('Startup peer sync failed: %s', e)
    threading.Thread(target=_do, daemon=True).start()
