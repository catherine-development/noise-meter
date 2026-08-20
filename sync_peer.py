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
import urllib.parse
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
from sync_db import (get_last_sync_time, update_last_sync_time, request_since,
                     pull_full_sync_from_peer, record_peer_clock_skew,
                     CLOCK_SKEW_WARN_S)
from users_sync import apply_users
from weather import fill_weather_gaps

PEER_URL  = os.environ.get('PEER_URL', '')
PI_NAME   = os.environ.get('PI_NAME', 'Pi')
IMPORT_KEY = os.environ.get('IMPORT_API_KEY', '')

if not PEER_URL:
    log.info('No PEER_URL set — skipping sync')
    exit(0)

init_db()
# The request's `since` (WP12/F2): the stored watermark minus a 300 s safety
# overlap, reformatted to the sender's imported_at convention. Re-receiving
# the overlap is harmless — import_sessions is a stable-key idempotent
# upsert — and the overlap absorbs commit-vs-clock ordering races on the
# sender. The watermark itself is stored from the peer's own `server_now`
# below, not from this Pi's wall clock, so clock skew between the pair can no
# longer strand a session permanently behind the watermark.
since = request_since(get_last_sync_time())
synced_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')

log.info('Syncing from %s (since %s)', PEER_URL, since)

try:
    url = f"{PEER_URL.rstrip('/')}/api/sync?since={urllib.parse.quote(since)}"
    # Cloudflare's edge bot-protection blocks urllib's default User-Agent
    # ("Python-urllib/3.x") with a 1010 error before the request even reaches
    # the app — a normal-looking UA is required to get through the tunnel.
    req = urllib.request.Request(url, headers={
        'X-Import-Key': IMPORT_KEY,
        'User-Agent': 'noise-meter-sync/1.0',
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    # Peer clock skew (WP14/F4) — measured against the response's server_now
    # while it is fresh. Observability only: every LWW comparison is
    # wall-clock ordered, so a slow clock's genuinely-later edit loses; this
    # is the monitoring hook that makes that hazard visible (in sync_state
    # and /health) before it bites. An old peer sends no server_now → None,
    # and the last stored measurement stands, dated by its own timestamp.
    skew = record_peer_clock_skew(data.get('server_now'))
    if skew is not None and abs(skew) > CLOCK_SKEW_WARN_S:
        log.warning('Peer clock skew is %.1f s (local minus peer). LWW '
                    'ordering is wall-clock: with this much skew, edits made '
                    'genuinely later on the slower Pi can lose. Check '
                    'chrony/NTP on both Pis.', skew)

    sessions = data.get('sessions', [])
    peer_name = data.get('pi_name', 'peer')
    log.info('Received %d session(s) from %s', len(sessions), peer_name)

    if sessions:
        # origin='peer' (WP14/F1+F2): this is mechanical replay — it may not
        # overwrite a stamped metadata edit outside the LWW gate, and it may
        # not resurrect a tombstoned session unless the payload's imported_at
        # proves the peer's copy postdates the deletion.
        n = import_sessions(sessions, origin='peer')
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

    # Sender-issued watermark (WP12/F2): store the PEER's clock — the one its
    # imported_at values are stamped by — not our own. A peer still on the old
    # code sends no server_now; fall back to the old local-clock behaviour
    # (never worse than before WP12, and self-heals when the peer upgrades).
    update_last_sync_time(data.get('server_now') or synced_at)

    # Catch-up for hand-entered data (assessments, session metadata, weather,
    # tombstones, report digests). The live mirror of these is a
    # fire-and-forget event: if this Pi was unreachable when the peer applied
    # an edit, that event is gone. The full payload is idempotent and
    # LWW-gated (see sync_db), so pulling it on every 15-minute tick — not
    # only at service startup — bounds the divergence window to one tick
    # instead of one restart. The pull itself — light payload, digest diff,
    # chunked report fetch, old-peer fallback — is
    # sync_db.pull_full_sync_from_peer() since WP14/F3, shared with the
    # startup sync in peer_client.py, which used to fetch the full unbounded
    # payload. Best-effort: a failure here must not fail the sync run that
    # already imported the measurement data.
    try:
        fetched = pull_full_sync_from_peer(PEER_URL, IMPORT_KEY,
                                           light_timeout=30,
                                           ua='noise-meter-sync/1.0')
        if fetched:
            log.info('Full-sync catch-up applied (%d report(s) fetched by uid)',
                     fetched)
        else:
            log.info('Full-sync catch-up applied')
    except Exception as e:
        log.warning('Full-sync catch-up failed (will retry next tick): %s', e)

    # User sync (WP10 part B): the flight-tracker logins live outside this
    # repo with no replication of their own, so this app carries them over
    # the same authenticated channel. Additive only — new users and NULL
    # name/phone gap-fills; deactivation stays a per-Pi manual act (see
    # users_sync.py). Best-effort like the weather gap-fill; a Pi with no
    # flights DB applies nothing and a peer with none serves an empty list.
    # PII: log counts only, never emails.
    try:
        users_req = urllib.request.Request(
            f"{PEER_URL.rstrip('/')}/api/users-sync",
            headers={'X-Import-Key': IMPORT_KEY,
                     'User-Agent': 'noise-meter-sync/1.0'})
        with urllib.request.urlopen(users_req, timeout=30) as resp:
            users_payload = json.loads(resp.read())
        applied = apply_users(users_payload.get('users', []))
        if applied['inserted'] or applied['filled']:
            log.info('User sync: %d new user(s), %d gap-fill(s)',
                     applied['inserted'], applied['filled'])
    except Exception as e:
        log.warning('User sync failed (will retry next tick): %s', e)

except Exception as e:
    log.error('Sync failed: %s', e)
    exit(1)
