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
from sync_db import pull_full_sync_from_peer, set_sync_state

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


def push_to_peer(sessions, prune=False, origin='operator-relay'):
    """Push sessions to peer Pi in a background thread (best-effort).

    prune=True carries the operator's explicit complete-date authorisation
    (WP11/F1) as the payload's top-level "prune": true, which the peer's
    /import requires before honouring any per-session complete_date — so a
    prune the operator asked for lands on both Pis instead of the peer's
    copy resurrecting the runs on the next sync pull.

    origin (WP14): every current caller relays a deliberate operator action —
    do_upload's immediate relay of a fresh import, /admin/push-to-peer's
    explicit resend — so the payload's top-level "origin" defaults to
    'operator-relay': the peer clears any session tombstone (the operator
    restored the date; both Pis must converge on restored) but applies
    metadata under the replay rules, never as a second stamped edit (the
    operator's edit was stamped once, on the Pi where it happened, and
    replicates through the session_meta LWW gate). A pre-WP14 peer ignores
    the key and treats the push as a plain operator import — exactly its
    behaviour today.

    Returns the thread (or None when there is nothing to do) so callers that
    need to know the outcome — tests, mainly — can join it. A failure is
    logged at WARNING and recorded in sync_state as last_push_error, which the
    upload page shows; a later success clears it. It used to print() and
    vanish, so a peer that silently refused every push looked healthy.
    """
    if not PEER_URL or not sessions:
        return None

    def _do_push():
        body = {'sessions': sessions, 'origin': origin}
        if prune:
            body['prune'] = True
        payload = json.dumps(body, separators=(',', ':')).encode()
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
    """On startup, pull the peer's syncable state and apply it (catches up on
    the offline period).

    Since WP14/F3 this is the same bounded pull the 15-minute tick makes —
    sync_db.pull_full_sync_from_peer(): light payload (report digests, not
    bodies), missing reports fetched by uid in chunks, old-peer fallback to
    the full payload. It was the one remaining unbounded transfer, re-shipping
    every report body on every restart, forever. The light call keeps this
    path's original 15 s timeout; the chunked report fetches have their own.
    """
    if not PEER_URL or not IMPORT_KEY:
        return
    def _do():
        import time
        time.sleep(8)  # let both Pis finish starting before attempting peer connection
        try:
            fetched = pull_full_sync_from_peer(PEER_URL, IMPORT_KEY,
                                               light_timeout=15, ua=_PEER_UA)
            log.info('Startup peer sync: applied light payload from peer%s',
                     f' ({fetched} report(s) fetched by uid)' if fetched else '')
        except Exception as e:
            log.warning('Startup peer sync failed: %s', e)
        # User sync (WP10 part B): same catch-up, for the flight-tracker
        # logins. Additive only; best-effort; counts logged, never emails —
        # see users_sync.py for the deliberate scope line on deactivation.
        try:
            from users_sync import apply_users
            ureq = urllib.request.Request(
                PEER_URL.rstrip('/') + '/api/users-sync',
                headers={'X-Import-Key': IMPORT_KEY, 'User-Agent': _PEER_UA},
                method='GET',
            )
            with urllib.request.urlopen(ureq, timeout=15) as resp:
                udata = json.loads(resp.read())
            applied = apply_users(udata.get('users', []))
            log.info('Startup user sync: %d new user(s), %d gap-fill(s)',
                     applied.get('inserted', 0), applied.get('filled', 0))
        except Exception as e:
            log.warning('Startup user sync failed: %s', e)
    threading.Thread(target=_do, daemon=True).start()
