"""
Peer-to-peer replication between the two Pis.

Covers the last-sync watermark, the full-state catch-up payload exchanged on
startup, the per-mutation events pushed after each edit, and the import log.

Split out of noise_db.py. The outbound HTTP side of the protocol lives in
peer_client.py; this module is only the database half.

Since WP10 (F6) the mutable hand-entered tables — assessments,
assessment_locations, assessment_runs, report_templates — replicate by uid,
never by the local integer id: both Pis accept edits, and id-keyed upserts
collided silently whenever the pair created rows apart. Every uid-keyed apply
is gated by last-writer-wins; a stale incoming row is skipped and recorded in
sync_conflicts so divergence is visible rather than silent. Deletes carry uid
tombstones (deleted_uids) so a full sync from a Pi that still holds a row
cannot resurrect it. Events from a pre-WP10 peer (no uid in the payload) fall
back to the old id-keyed apply, so a mixed-version pair degrades to the old
behaviour rather than breaking.

Since WP12 the LWW order is *total*: timestamps are millisecond-resolution
(noise_db.LWW_NOW_SQL) and every local mutation also records its writer (the
Pi's PI_NAME), and the gate compares the string tuple (updated_at, writer) —
strictly greater applies, strictly smaller skips into sync_conflicts, exactly
equal is a no-op (the same write replayed). Second-resolution stamps applied
equal timestamps, so two Pis editing one row in the same second swapped
values on every exchange, forever, with no conflict recorded (F3). Session
metadata and run location tags — previously merged with an unordered COALESCE
that oscillated the same way (F4) — go through the same gate now, keyed
date|serial (conflict table_name 'session_meta') and date|serial|source_file
('run_tag'). Old-format rows (second-resolution, no writer) stay valid: as
strings they order before any same-second millisecond stamp, and their
missing writer compares as ''.

WP12 also owns the measurement-session watermark (F2): /api/sync returns
`server_now` — the *sender's* database clock, the same one that stamps its
imported_at — and sync_peer.py stores that instead of its own wall clock, so
clock skew between the Pis can no longer make a session fall permanently
behind the watermark. And the 15-minute full-sync tick ships generated
reports as a uid digest rather than their full bodies (F8b); the missing ones
are fetched by uid through GET /api/reports-sync.

Note that get_sessions_since() stays in noise_db: it is a core session/run read
that the sync protocol happens to call, not part of the protocol itself.
"""
import json
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from noise_db import (get_db, delete_session, purge_sessions_before, _record_tombstones,
                      resolve_serial, record_uid_tombstones, LWW_NOW_SQL,
                      uid_for_assessment, uid_for_location, uid_for_assessment_run)


def _apply_tombstones(conn, tombstones):
    """Replay a peer's session deletions locally. Caller commits.

    A tombstone is ignored when our own copy of the session was imported after
    the peer deleted it: that means the date was legitimately re-imported here
    (e.g. off the SD card), and the peer will pick it up again on its next
    /api/sync pull. Both timestamps come from SQLite's datetime('now'), so a
    string comparison is chronological.

    A tombstone names the session by (date, serial); one from a pre-WP8 peer
    carries the date alone and applies to the default serial only.
    """
    for ts in tombstones:
        date = ts.get('date')
        deleted_at = ts.get('deleted_at')
        if not date or not deleted_at:
            continue
        serial = resolve_serial(ts.get('serial'), conn)
        row = conn.execute(
            'SELECT imported_at FROM sessions WHERE date=? AND instrument_serial=?',
            (date, serial)).fetchone()
        if row is not None and row['imported_at'] and row['imported_at'] >= deleted_at:
            continue  # re-imported here after the peer's delete — keep ours
        # The links deleted with the session get uid tombstones, mirroring
        # delete_session(): otherwise the next full sync from a third copy
        # (or a not-yet-caught-up peer) re-creates dangling link rows.
        ar_uids = [r[0] for r in conn.execute(
            'SELECT uid FROM assessment_runs WHERE session_date=? AND '
            'instrument_serial=? AND uid IS NOT NULL', (date, serial)).fetchall()]
        conn.execute('DELETE FROM assessment_runs WHERE session_date=? AND instrument_serial=?',
                     (date, serial))
        conn.execute('DELETE FROM sessions WHERE date=? AND instrument_serial=?', (date, serial))
        _record_tombstones(conn, [(date, serial)], deleted_at=deleted_at)
        record_uid_tombstones(conn, 'assessment_runs', ar_uids, deleted_at=deleted_at)


def get_last_sync_time():
    conn = get_db()
    row = conn.execute("SELECT value FROM sync_state WHERE key='last_sync'").fetchone()
    conn.close()
    return row['value'] if row else '1970-01-01T00:00:00'


def update_last_sync_time(ts):
    conn = get_db()
    conn.execute(
        "INSERT INTO sync_state (key, value) VALUES ('last_sync', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (ts,)
    )
    conn.commit()
    conn.close()


def db_now():
    """The database clock, second resolution — the same clock and format
    (datetime('now'), UTC, space-separated) that stamps sessions.imported_at.

    /api/sync returns this as `server_now` so the pulling peer can watermark
    on the *sender's* clock rather than its own (F2): with the receiver's
    wall clock as the watermark, any skew between the Pis left a window of
    sessions whose imported_at was already behind the watermark the moment it
    was stored — and, because the watermark only moves forward, they were
    missed permanently (the full-sync payload does not carry measurement
    sessions, so there was no later recovery)."""
    conn = get_db()
    v = conn.execute("SELECT datetime('now')").fetchone()[0]
    conn.close()
    return v


# The safety overlap subtracted from the stored watermark on every pull.
# Re-receiving a session is harmless — import_sessions is a stable-key
# idempotent upsert — so five re-sent minutes buy tolerance of commit-versus-
# clock ordering races on the sender for free.
SYNC_OVERLAP_S = 300


def request_since(watermark, overlap_s=SYNC_OVERLAP_S):
    """The `since` to put in the /api/sync request: the stored watermark
    minus the safety overlap, in the sender's imported_at format.

    Accepts both watermark formats — 'YYYY-MM-DDTHH:MM:SS' (what sync_peer
    stored before WP12: its own wall clock, ISO 'T'-separated) and
    'YYYY-MM-DD HH:MM:SS[.SSS]' (a peer's server_now). The output is always
    space-separated, because that is what SQLite's datetime('now') writes
    into imported_at and the comparison is a plain string compare — the old
    'T'-separated since compared *greater* than every same-day imported_at
    ('T' > ' '), which silently excluded same-day sessions from the pull.
    An unparseable watermark is returned unchanged (better a lossless echo
    than a crash in the sync loop)."""
    for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S',
                '%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S'):
        try:
            dt = datetime.strptime(watermark, fmt)
            break
        except (ValueError, TypeError):
            continue
    else:
        return watermark
    return (dt - timedelta(seconds=overlap_s)).strftime('%Y-%m-%d %H:%M:%S')


def get_sync_state(key, default=None):
    conn = get_db()
    row = conn.execute('SELECT value FROM sync_state WHERE key=?', (key,)).fetchone()
    conn.close()
    return row['value'] if row else default


def set_sync_state(key, value):
    """Upsert one sync_state row; value=None deletes it."""
    conn = get_db()
    if value is None:
        conn.execute('DELETE FROM sync_state WHERE key=?', (key,))
    else:
        conn.execute(
            'INSERT INTO sync_state (key, value) VALUES (?, ?) '
            'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))
    conn.commit()
    conn.close()


def get_last_push_error():
    """The most recent failed push_to_peer(), as {'at', 'error'}, or None.

    Recorded by peer_client.push_to_peer() and cleared on the next success, so
    the upload page can say that the last upload did not reach the peer — it
    used to print() the failure to a log nobody reads.
    """
    raw = get_sync_state('last_push_error')
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return {'at': None, 'error': raw}


def get_full_sync_payload(light=False):
    """Return all syncable state for peer replication.

    The uid-keyed tables carry BOTH their uids (what a WP10 receiver applies
    by, exclusively) and their local integer ids (what a pre-WP10 receiver
    still applies by — it ignores the uid keys, which is the documented
    degraded mixed-version behaviour, not a breakage).

    light=True (WP12/F8b, the 15-minute tick): generated reports — the one
    bulky append-only table, each row carrying its full sections_json and
    input_snapshot_json — are sent as {uid, created_at} digests under
    'generated_reports_digest' instead of full rows; the puller diffs the
    digest against its own uids and fetches only what it is missing through
    GET /api/reports-sync. The startup sync keeps the full payload (rare, so
    bounded). An old puller never asks for light mode and keeps getting the
    full rows."""
    conn = get_db()
    assessments  = [dict(r) for r in conn.execute('SELECT * FROM assessments').fetchall()]
    locations    = [dict(r) for r in conn.execute('SELECT * FROM assessment_locations').fetchall()]
    assess_runs  = [dict(r) for r in conn.execute('SELECT * FROM assessment_runs').fetchall()]
    # Every session-keyed row carries `serial` beside its date; a receiver on
    # the older schema ignores the key, an older sender omits it and the
    # receiver files the row under its default serial. The meta_*/tag_* LWW
    # stamps (WP12/F4) ride along the same way: an older receiver's named-
    # parameter UPDATE simply never binds them.
    sess_meta    = [dict(r) for r in conn.execute(
        'SELECT date, instrument_serial AS serial, recorder_name, location_label, '
        'postcode, lat, lng, notes, meta_updated_at, meta_writer FROM sessions'
    ).fetchall()]
    run_tags     = [dict(r) for r in conn.execute(
        'SELECT s.date AS session_date, s.instrument_serial AS serial, '
        'r.run_number, r.source_file, r.location_tag, '
        'r.tag_updated_at, r.tag_writer '
        'FROM runs r JOIN sessions s ON r.session_id=s.id '
        'WHERE r.location_tag IS NOT NULL OR r.tag_updated_at IS NOT NULL'
    ).fetchall()]
    deleted_sess = [dict(r) for r in conn.execute(
        'SELECT date, instrument_serial AS serial, deleted_at FROM deleted_sessions').fetchall()]
    # uid tombstones (F6): deletes of the uid-keyed tables keep propagating
    # through the full payload, exactly like session tombstones. An old
    # receiver ignores the key.
    deleted_uids = [dict(r) for r in conn.execute(
        'SELECT table_name, uid, deleted_at FROM deleted_uids').fetchall()]
    # Weather is session-keyed reference data and replicates in full (WP9), so
    # a Pi that was offline when the other side fetched — or received — a row
    # catches up here. hourly_json rides along: ~24 rows × 4 series per date,
    # small beside the runs' own spectral payloads.
    weather = [dict(r) for r in conn.execute(
        'SELECT date, instrument_serial AS serial, wind_speed, wind_dir, '
        'temp_min, temp_max, precip, hourly_json FROM weather').fetchall()]
    # Generated reports replicate (WP9) — append-only evidence, keyed across
    # the pair by uid, never by the local integer id. Report templates
    # replicate too since WP10, uid-keyed with LWW like the other mutable
    # hand-entered tables. Templates are small and stay full-bodied even in
    # light mode; reports are the payload that grew without bound (F8b).
    templates = [dict(r) for r in conn.execute(
        'SELECT * FROM report_templates WHERE uid IS NOT NULL').fetchall()]
    payload = {'assessments': assessments, 'assessment_locations': locations,
               'assessment_runs': assess_runs, 'sessions_meta': sess_meta,
               'run_tags': run_tags, 'deleted_sessions': deleted_sess,
               'deleted_uids': deleted_uids, 'weather': weather,
               'report_templates': templates}
    if light:
        payload['generated_reports_digest'] = [dict(r) for r in conn.execute(
            'SELECT uid, created_at FROM generated_reports WHERE uid IS NOT NULL'
        ).fetchall()]
    else:
        payload['generated_reports'] = [dict(r) for r in conn.execute(
            'SELECT * FROM generated_reports WHERE uid IS NOT NULL').fetchall()]
    conn.close()
    return payload


def apply_full_sync(payload):
    """Upsert a full sync payload received from peer (startup catch-up and
    every 15-minute tick)."""
    conn = get_db()
    # Deletions are applied before the upserts below so that (a) metadata for
    # a session the peer deleted is not re-applied to a row we are about to
    # remove, and (b) an upsert of a row the peer deleted is refused by its
    # fresh tombstone rather than resurrecting it.
    _apply_tombstones(conn, payload.get('deleted_sessions', []))
    _apply_uid_tombstones(conn, payload.get('deleted_uids', []))
    for a in payload.get('assessments', []):
        _apply_assessment(conn, a)
    for loc in payload.get('assessment_locations', []):
        _apply_location(conn, loc)
    for ar in payload.get('assessment_runs', []):
        _apply_assessment_run(conn, ar)
    for t in payload.get('report_templates', []):
        _apply_report_template(conn, t)
    for sm in payload.get('sessions_meta', []):
        _apply_session_meta(conn, sm, coalesce=True)
    for rt in payload.get('run_tags', []):
        _apply_run_tag(conn, rt, coalesce=True)
    for wx in payload.get('weather', []):
        _apply_weather(conn, wx)
    for gr in payload.get('generated_reports', []):
        _apply_generated_report(conn, gr)
    conn.commit()
    conn.close()


# ── F6: uid-keyed apply with LWW and conflict surfacing ──────────────────────

# The tables replication may touch by uid — a whitelist, because the table
# name arrives inside tombstone payloads and is interpolated into SQL.
_LWW_TABLES = ('assessments', 'assessment_locations', 'assessment_runs',
               'report_templates')
_UID_TABLES = _LWW_TABLES + ('generated_reports',)

_ASSESSMENT_COLS = ('uid', 'name', 'purpose', 'standard', 'address', 'postcode',
                    'lat', 'lng', 'client_ref', 'notes', 'created_at',
                    'updated_at', 'writer')
_LOCATION_COLS = ('uid', 'assessment_uid', 'label', 'description', 'lat', 'lng',
                  'sort_order', 'notes', 'updated_at', 'writer')
_ARUN_COLS = ('uid', 'assessment_uid', 'location_uid', 'session_date',
              'instrument_serial', 'run_number', 'source_file', 'conditions',
              'notes', 'updated_at', 'writer')
_TEMPLATE_COLS = ('uid', 'name', 'description', 'prompt', 'is_default',
                  'created_at', 'updated_at', 'writer')


def _now(conn):
    # Millisecond, matching every other LWW timestamp (WP12) — this is the
    # deleted_at fallback for a tombstone that arrives without one.
    return conn.execute(f'SELECT {LWW_NOW_SQL}').fetchone()[0]


def _tombstone_time(conn, table, uid):
    row = conn.execute(
        'SELECT deleted_at FROM deleted_uids WHERE table_name=? AND uid=?',
        (table, uid)).fetchone()
    return row['deleted_at'] if row else None


def _record_conflict(conn, table, uid, local_updated_at, remote_updated_at, payload):
    """One row per (table, uid), always the latest occurrence — the table is a
    visibility surface (GET /api/sync-conflicts), not a journal."""
    conn.execute('''
        INSERT INTO sync_conflicts
            (table_name, uid, local_updated_at, remote_updated_at, seen_at, payload_json)
        VALUES (?,?,?,?,datetime('now'),?)
        ON CONFLICT(table_name, uid) DO UPDATE SET
            local_updated_at=excluded.local_updated_at,
            remote_updated_at=excluded.remote_updated_at,
            seen_at=excluded.seen_at,
            payload_json=excluded.payload_json
    ''', (table, uid, local_updated_at, remote_updated_at,
          json.dumps(payload, default=str)))


def _clear_conflict(conn, table, uid):
    conn.execute('DELETE FROM sync_conflicts WHERE table_name=? AND uid=?',
                 (table, uid))


def _lww_key(updated_at, writer):
    """The total LWW order (WP12): the string tuple (updated_at, writer).

    NULLs compare as '' — so a never-edited row loses to any real edit, an
    old-format row (second-resolution timestamp, no writer) orders before any
    same-second millisecond stamp ('HH:MM:SS' < 'HH:MM:SS.mmm' as strings —
    the first difference is end-of-string vs '.'), and its missing writer
    loses same-timestamp ties to any named one."""
    return ((updated_at or ''), (writer or ''))


def _lww_should_apply(conn, table, uid, incoming_updated, incoming_writer,
                      payload):
    """The last-writer-wins gate for one incoming uid-keyed row.

    - uid tombstoned here: skip, unless the incoming edit is strictly newer
      than the delete — then the edit wins, the tombstone goes, and the row
      resurrects (the peer converges the same way when our row reaches it).
      (Delete-vs-edit compares timestamps alone; an exact tie is delete-wins
      on both sides, so no writer is needed to converge.)
    - both rows present: compare _lww_key tuples. Strictly greater applies
      (clearing any stale conflict entry); strictly smaller skips and records
      the loser in sync_conflicts; exactly equal is the same write replayed —
      a no-op that also clears the conflict entry, since the pair holds the
      same write. (Pre-WP12 the gate applied equal timestamps, which is what
      made two same-second edits swap on every exchange forever, F3.)
    - transition case: an equal timestamp whose incoming writer is empty is a
      pre-WP12 peer echoing our own row back without the writer column it
      does not store — a no-op, not a divergence, so no conflict is recorded.
    """
    ts = _tombstone_time(conn, table, uid)
    if ts is not None:
        if (incoming_updated or '') > ts:
            conn.execute('DELETE FROM deleted_uids WHERE table_name=? AND uid=?',
                         (table, uid))
        else:
            return False
    row = conn.execute(f'SELECT updated_at, writer FROM {table} WHERE uid=?',
                       (uid,)).fetchone()
    if row is not None:
        local = _lww_key(row['updated_at'], row['writer'])
        incoming = _lww_key(incoming_updated, incoming_writer)
        if incoming == local:
            _clear_conflict(conn, table, uid)
            return False
        if incoming < local:
            if incoming[0] != local[0] or incoming[1]:
                _record_conflict(conn, table, uid, row['updated_at'],
                                 incoming_updated, payload)
            return False
    _clear_conflict(conn, table, uid)
    return True


def _ensure_assessment(conn, a_uid):
    """Local id for an assessment uid. Events are fire-and-forget threads, so
    a child can arrive before its parent: unseen parents get a stub row the
    real upsert later fills (its updated_at is NULL, so anything beats it).
    Returns None when the uid is tombstoned here — the delete wins, and the
    caller must drop the child rather than resurrect the parent."""
    row = conn.execute('SELECT id FROM assessments WHERE uid=?', (a_uid,)).fetchone()
    if row:
        return row['id']
    if _tombstone_time(conn, 'assessments', a_uid) is not None:
        return None
    cur = conn.execute(
        "INSERT INTO assessments (uid, name) VALUES (?, '(pending sync)')", (a_uid,))
    return cur.lastrowid


def _ensure_location(conn, l_uid, local_assessment_id):
    """Like _ensure_assessment. None when tombstoned — the run link then keeps
    a NULL location, matching the local ON DELETE SET NULL behaviour."""
    row = conn.execute('SELECT id FROM assessment_locations WHERE uid=?',
                       (l_uid,)).fetchone()
    if row:
        return row['id']
    if _tombstone_time(conn, 'assessment_locations', l_uid) is not None:
        return None
    cur = conn.execute(
        'INSERT INTO assessment_locations (uid, assessment_id, label) '
        "VALUES (?, ?, '(pending sync)')", (l_uid, local_assessment_id))
    return cur.lastrowid


def _adopt_uid(conn, table, row_id, uid):
    """Give an id-keyed row from a pre-WP10 peer the deterministic uid its
    sender's own migration will compute, so the copies unify instead of
    twinning when the peer upgrades. Leaves an existing uid alone; if the
    deterministic uid already names another local row, this one stays NULL
    rather than failing the whole apply."""
    if row_id is None or not uid:
        return
    try:
        conn.execute(f'UPDATE {table} SET uid=? WHERE id=? AND uid IS NULL',
                     (uid, row_id))
    except sqlite3.IntegrityError:
        pass


def _apply_assessment(conn, a):
    """Upsert one peer assessment. uid-keyed with LWW when the sender is on
    WP10; an id-keyed row from an older peer falls back to the old apply."""
    a = dict(a)
    if not a.get('uid'):
        return _apply_assessment_legacy(conn, a)
    for c in _ASSESSMENT_COLS:
        a.setdefault(c, None)
    a['name'] = a['name'] or ''
    if not _lww_should_apply(conn, 'assessments', a['uid'], a['updated_at'],
                             a.get('writer'), a):
        return
    conn.execute('''
        INSERT INTO assessments
            (uid,name,purpose,standard,address,postcode,lat,lng,client_ref,notes,
             created_at,updated_at,writer)
        VALUES (:uid,:name,:purpose,:standard,:address,:postcode,:lat,:lng,
                :client_ref,:notes,:created_at,:updated_at,:writer)
        ON CONFLICT(uid) DO UPDATE SET
            name=excluded.name, purpose=excluded.purpose, standard=excluded.standard,
            address=excluded.address, postcode=excluded.postcode,
            lat=excluded.lat, lng=excluded.lng,
            client_ref=excluded.client_ref, notes=excluded.notes,
            updated_at=excluded.updated_at, writer=excluded.writer
    ''', {c: a[c] for c in _ASSESSMENT_COLS})


def _apply_assessment_legacy(conn, a):
    """Pre-WP10 peer: keyed on the local id — the pair's ids agree until they
    diverge, which is exactly the status quo this package replaces. Last
    payload wins, as before."""
    for c in ('id', 'name', 'purpose', 'standard', 'address', 'postcode', 'lat',
              'lng', 'client_ref', 'notes', 'created_at'):
        a.setdefault(c, None)
    conn.execute('''
        INSERT INTO assessments
            (id,name,purpose,standard,address,postcode,lat,lng,client_ref,notes,created_at)
        VALUES (:id,:name,:purpose,:standard,:address,:postcode,:lat,:lng,:client_ref,:notes,:created_at)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name, purpose=excluded.purpose, standard=excluded.standard,
            address=excluded.address, postcode=excluded.postcode,
            lat=excluded.lat, lng=excluded.lng,
            client_ref=excluded.client_ref, notes=excluded.notes
    ''', a)
    _adopt_uid(conn, 'assessments', a['id'],
               uid_for_assessment(a['created_at'], a['name']))


def _apply_location(conn, loc):
    loc = dict(loc)
    if not loc.get('uid') or not loc.get('assessment_uid'):
        return _apply_location_legacy(conn, loc)
    aid = _ensure_assessment(conn, loc['assessment_uid'])
    if aid is None:
        return   # parent deleted here — the straggler child loses
    for c in _LOCATION_COLS:
        loc.setdefault(c, None)
    loc['label'] = loc['label'] or ''
    if not _lww_should_apply(conn, 'assessment_locations', loc['uid'],
                             loc['updated_at'], loc.get('writer'), loc):
        return
    loc['_aid'] = aid
    conn.execute('''
        INSERT INTO assessment_locations
            (uid,assessment_uid,assessment_id,label,description,lat,lng,sort_order,
             notes,updated_at,writer)
        VALUES (:uid,:assessment_uid,:_aid,:label,:description,:lat,:lng,:sort_order,
                :notes,:updated_at,:writer)
        ON CONFLICT(uid) DO UPDATE SET
            assessment_uid=excluded.assessment_uid,
            assessment_id=excluded.assessment_id,
            label=excluded.label, description=excluded.description,
            lat=excluded.lat, lng=excluded.lng,
            sort_order=excluded.sort_order, notes=excluded.notes,
            updated_at=excluded.updated_at, writer=excluded.writer
    ''', {**{c: loc[c] for c in _LOCATION_COLS}, '_aid': aid})


def _apply_location_legacy(conn, loc):
    for c in ('id', 'assessment_id', 'label', 'description', 'lat', 'lng',
              'sort_order', 'notes'):
        loc.setdefault(c, None)
    conn.execute('''
        INSERT INTO assessment_locations
            (id,assessment_id,label,description,lat,lng,sort_order,notes)
        VALUES (:id,:assessment_id,:label,:description,:lat,:lng,:sort_order,:notes)
        ON CONFLICT(id) DO UPDATE SET
            label=excluded.label, description=excluded.description,
            lat=excluded.lat, lng=excluded.lng,
            sort_order=excluded.sort_order, notes=excluded.notes
    ''', loc)
    parent = conn.execute('SELECT uid FROM assessments WHERE id=?',
                          (loc['assessment_id'],)).fetchone()
    a_uid = parent['uid'] if parent else None
    conn.execute('UPDATE assessment_locations SET assessment_uid=? '
                 'WHERE id=? AND assessment_uid IS NULL', (a_uid, loc['id']))
    _adopt_uid(conn, 'assessment_locations', loc['id'],
               uid_for_location(a_uid, loc['label'], loc['sort_order']))


def _apply_assessment_run(conn, ar):
    ar = dict(ar)
    if not ar.get('uid') or not ar.get('assessment_uid'):
        return _apply_assessment_run_legacy(conn, ar)
    aid = _ensure_assessment(conn, ar['assessment_uid'])
    if aid is None:
        return   # parent deleted here
    lid = None
    if ar.get('location_uid'):
        lid = _ensure_location(conn, ar['location_uid'], aid)
        if lid is None:
            ar['location_uid'] = None   # location deleted here → SET NULL semantics
    for c in _ARUN_COLS:
        ar.setdefault(c, None)
    ar['instrument_serial'] = resolve_serial(ar.get('instrument_serial'), conn)
    # Both Pis can assign the same run to the same assessment while apart,
    # each minting its own uid for the one physical link. The stable key
    # (assessment, date, serial, source_file) says they are the same row;
    # both sides converge on the lexically smaller uid without talking.
    if ar['source_file']:
        ex = conn.execute(
            'SELECT id, uid, updated_at FROM assessment_runs WHERE assessment_id=? '
            'AND session_date=? AND instrument_serial=? AND source_file=?',
            (aid, ar['session_date'], ar['instrument_serial'],
             ar['source_file'])).fetchone()
        if ex and ex['uid'] is None:
            # a legacy row for the same link: adopt the incoming identity
            conn.execute('UPDATE assessment_runs SET uid=? WHERE id=?',
                         (ar['uid'], ex['id']))
        elif ex and ex['uid'] != ar['uid']:
            if ar['uid'] < ex['uid']:
                conn.execute('UPDATE assessment_runs SET uid=? WHERE id=?',
                             (ar['uid'], ex['id']))
                _clear_conflict(conn, 'assessment_runs', ex['uid'])
            else:
                # our uid wins the tie; the peer re-keys its copy the same
                # way when our row reaches it. Made visible, not silent.
                _record_conflict(conn, 'assessment_runs', ex['uid'],
                                 ex['updated_at'], ar['updated_at'], ar)
                return
    if not _lww_should_apply(conn, 'assessment_runs', ar['uid'],
                             ar['updated_at'], ar.get('writer'), ar):
        return
    params = {**{c: ar[c] for c in _ARUN_COLS}, '_aid': aid, '_lid': lid}
    try:
        conn.execute('''
            INSERT INTO assessment_runs
                (uid,assessment_uid,location_uid,assessment_id,location_id,
                 session_date,instrument_serial,run_number,source_file,conditions,
                 notes,updated_at,writer)
            VALUES (:uid,:assessment_uid,:location_uid,:_aid,:_lid,:session_date,
                    :instrument_serial,:run_number,:source_file,:conditions,
                    :notes,:updated_at,:writer)
            ON CONFLICT(uid) DO UPDATE SET
                assessment_uid=excluded.assessment_uid,
                location_uid=excluded.location_uid,
                assessment_id=excluded.assessment_id,
                location_id=excluded.location_id,
                run_number=excluded.run_number,
                source_file=COALESCE(excluded.source_file, assessment_runs.source_file),
                conditions=excluded.conditions, notes=excluded.notes,
                updated_at=excluded.updated_at, writer=excluded.writer
        ''', params)
    except sqlite3.IntegrityError:
        # the stable-key index refused (two links under different uids whose
        # source_files now collide) — surface it rather than fail the sync
        _record_conflict(conn, 'assessment_runs', ar['uid'], None,
                         ar['updated_at'], ar)


def _apply_assessment_run_legacy(conn, ar):
    """A peer on the older schema sends no uid (and possibly no source_file
    or serial). Same id-keyed apply as before; COALESCE keeps any stored
    source_file — an old peer's silence must not erase the stable key."""
    ar = _with_serial(conn, ar)
    for c in ('id', 'assessment_id', 'location_id', 'session_date',
              'run_number', 'conditions', 'notes'):
        ar.setdefault(c, None)
    conn.execute('''
        INSERT INTO assessment_runs
            (id,assessment_id,location_id,session_date,instrument_serial,run_number,
             source_file,conditions,notes)
        VALUES (:id,:assessment_id,:location_id,:session_date,:instrument_serial,:run_number,
                :source_file,:conditions,:notes)
        ON CONFLICT(id) DO UPDATE SET
            location_id=excluded.location_id,
            source_file=COALESCE(excluded.source_file, assessment_runs.source_file),
            conditions=excluded.conditions, notes=excluded.notes
    ''', ar)
    parent = conn.execute('SELECT uid FROM assessments WHERE id=?',
                          (ar['assessment_id'],)).fetchone()
    a_uid = parent['uid'] if parent else None
    conn.execute('''
        UPDATE assessment_runs SET
            assessment_uid=COALESCE(assessment_uid, ?),
            location_uid=COALESCE(location_uid,
                (SELECT uid FROM assessment_locations l WHERE l.id=location_id))
        WHERE id=?''', (a_uid, ar['id']))
    _adopt_uid(conn, 'assessment_runs', ar['id'], uid_for_assessment_run(
        a_uid, ar['session_date'], ar['instrument_serial'],
        ar['source_file'], ar['run_number']))


def _apply_report_template(conn, t):
    """Templates replicate by uid since WP10 — there is no legacy fallback
    because no older peer ever sent one."""
    t = dict(t)
    if not t.get('uid'):
        return
    for c in _TEMPLATE_COLS:
        t.setdefault(c, None)
    t['name'] = t['name'] or ''
    t['prompt'] = t['prompt'] or ''
    if not _lww_should_apply(conn, 'report_templates', t['uid'],
                             t['updated_at'], t.get('writer'), t):
        return
    conn.execute('''
        INSERT INTO report_templates
            (uid,name,description,prompt,is_default,created_at,updated_at,writer)
        VALUES (:uid,:name,:description,:prompt,:is_default,:created_at,
                :updated_at,:writer)
        ON CONFLICT(uid) DO UPDATE SET
            name=excluded.name, description=excluded.description,
            prompt=excluded.prompt, is_default=excluded.is_default,
            updated_at=excluded.updated_at, writer=excluded.writer
    ''', {c: t[c] for c in _TEMPLATE_COLS})


def _apply_uid_tombstones(conn, tombs):
    """Replay a peer's uid deletes. Caller commits.

    A local row edited *after* the delete survives and is recorded as a
    conflict — the newer edit re-propagates and clears the peer's tombstone
    through _lww_should_apply, so both sides converge on the resurrected row.
    Everything else is deleted (assessment children cascade via FK), and the
    tombstone is stored locally so the delete keeps propagating. Replay-safe:
    a tombstone for a uid already gone just refreshes deleted_at via MAX."""
    for t in tombs:
        table, uid = t.get('table_name'), t.get('uid')
        if table not in _UID_TABLES or not uid:
            continue
        deleted_at = t.get('deleted_at') or _now(conn)
        if table in _LWW_TABLES:
            row = conn.execute(f'SELECT id, updated_at FROM {table} WHERE uid=?',
                               (uid,)).fetchone()
            if row is not None and (row['updated_at'] or '') > deleted_at:
                _record_conflict(conn, table, uid, row['updated_at'], deleted_at,
                                 {'deleted_at': deleted_at, 'action': 'delete'})
                continue   # the local edit is newer than the delete — keep it
            if row is not None:
                conn.execute(f'DELETE FROM {table} WHERE id=?', (row['id'],))
        else:   # generated_reports: append-only, no updated_at to weigh
            conn.execute('DELETE FROM generated_reports WHERE uid=?', (uid,))
        record_uid_tombstones(conn, table, [uid], deleted_at=deleted_at)


def _apply_uid_delete(conn, table, data):
    """One delete event. uid-keyed from a WP10 peer; an old peer names only
    the local id — applied as before (ids agree until the pair diverges,
    which is the pre-WP10 status quo the fallback deliberately preserves)."""
    if data.get('uid'):
        _apply_uid_tombstones(conn, [{'table_name': table, 'uid': data['uid'],
                                      'deleted_at': data.get('deleted_at')}])
    elif data.get('id') is not None:
        conn.execute(f'DELETE FROM {table} WHERE id=?', (data['id'],))


def get_sync_conflicts():
    """Rows a peer tried to change here with an older edit (or delete),
    newest first — the visibility surface behind GET /api/sync-conflicts."""
    conn = get_db()
    rows = conn.execute(
        'SELECT table_name, uid, local_updated_at, remote_updated_at, seen_at, '
        'payload_json FROM sync_conflicts ORDER BY seen_at DESC, table_name, uid'
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


_WEATHER_VALUE_COLS = ('wind_speed', 'wind_dir', 'temp_min', 'temp_max',
                       'precip', 'hourly_json')


def _apply_weather(conn, wx):
    """Upsert one peer weather row, keyed (date, serial). Caller commits.

    COALESCE per column: full-sync is a catch-up, not an authority, so a
    NULL from the peer (it never fetched, or an old-format sender without
    hourly_json) must not erase a value already held here."""
    if not wx.get('date'):
        return
    wx = _with_serial(conn, wx, key='serial')
    for c in _WEATHER_VALUE_COLS:
        wx.setdefault(c, None)
    sets = ', '.join(f'{c}=COALESCE(excluded.{c}, weather.{c})'
                     for c in _WEATHER_VALUE_COLS)
    conn.execute(f'''
        INSERT INTO weather (date, instrument_serial, {", ".join(_WEATHER_VALUE_COLS)})
        VALUES (:date, :serial, {", ".join(":" + c for c in _WEATHER_VALUE_COLS)})
        ON CONFLICT(date, instrument_serial) DO UPDATE SET {sets}
    ''', wx)


# Every generated_reports column except the local integer id, which never
# crosses the wire: each Pi numbers its own rows, and the uid is the identity
# the pair agrees on.
_REPORT_COLS = ('uid', 'session_date', 'instrument_serial', 'run_number',
                'run_label', 'template_id', 'template_name', 'model',
                'thinking_level', 'sections_json', 'input_tokens',
                'output_tokens', 'cost_usd', 'created_at', 'source_file',
                'input_snapshot_json')


def _apply_generated_report(conn, row):
    """Upsert one peer report by uid — never by local id. Caller commits.

    template_id crosses the wire as provenance only: the id may name a
    different (or no) template here even now that templates replicate;
    template_name is the value everything renders."""
    if not row.get('uid'):
        return   # a row from a pre-WP9 peer has no replication identity
    if _tombstone_time(conn, 'generated_reports', row['uid']) is not None:
        return   # deleted here — a straggler copy must not resurrect it (F6)
    row = dict(row)
    row['instrument_serial'] = resolve_serial(row.get('instrument_serial'), conn)
    for c in _REPORT_COLS:
        row.setdefault(c, None)
    sets = ', '.join(f'{c}=excluded.{c}' for c in _REPORT_COLS if c != 'uid')
    conn.execute(f'''
        INSERT INTO generated_reports ({", ".join(_REPORT_COLS)})
        VALUES ({", ".join(":" + c for c in _REPORT_COLS)})
        ON CONFLICT(uid) DO UPDATE SET {sets}
    ''', {c: row[c] for c in _REPORT_COLS})


def _with_serial(conn, row, key='instrument_serial'):
    """Copy of a peer row with its serial normalised: missing or blank (an
    older peer) becomes this Pi's default. Also defaults source_file, which
    a peer on the older schema does not send."""
    row = dict(row)
    row[key] = resolve_serial(row.get(key), conn)
    row.setdefault('source_file', None)
    return row


def _apply_session_meta(conn, sm, coalesce=False):
    """Apply one peer sessions_meta row (hand-entered session metadata),
    keyed (date, serial). Caller commits.

    Under the LWW gate since WP12 (F4): a stamped row (meta_updated_at set —
    a hand edit made on WP12 code) compares by the (meta_updated_at,
    meta_writer) tuple against the local stamps; the winner overwrites all
    six fields *including NULLs* (a hand edit that cleared a field must clear
    it here too), the loser is skipped into sync_conflicts (table_name
    'session_meta', uid 'date|serial'). An unstamped row (an old peer, or a
    row nobody hand-edited since WP12) keeps the pre-WP12 behaviour against
    an unstamped local row — COALESCE-merge from the full payload, plain
    overwrite from an event — but always loses to a stamped local edit,
    without a conflict: unstamped data has no ordering claim to record.

    A (date, serial) with no session here is a no-op: metadata rides the
    measurement payload, which is what creates the row."""
    sm = _with_serial(conn, sm, key='serial')
    for c in ('recorder_name', 'location_label', 'postcode', 'lat', 'lng',
              'notes', 'meta_updated_at', 'meta_writer'):
        sm.setdefault(c, None)
    row = conn.execute(
        'SELECT meta_updated_at, meta_writer FROM sessions '
        'WHERE date=:date AND instrument_serial=:serial', sm).fetchone()
    if row is None:
        return
    uid = '%s|%s' % (sm['date'], sm['serial'])
    if sm['meta_updated_at'] is None:
        if row['meta_updated_at'] is not None:
            return   # a stamped local edit beats unstamped data; no conflict
        if coalesce:
            # Pre-WP12 full-sync semantics: never overwrite non-null with null.
            conn.execute('''
                UPDATE sessions SET
                    recorder_name=COALESCE(:recorder_name, recorder_name),
                    location_label=COALESCE(:location_label, location_label),
                    postcode=COALESCE(:postcode, postcode),
                    lat=COALESCE(:lat, lat),
                    lng=COALESCE(:lng, lng),
                    notes=COALESCE(:notes, notes)
                WHERE date=:date AND instrument_serial=:serial
            ''', sm)
        else:
            conn.execute('''
                UPDATE sessions SET recorder_name=:recorder_name,
                    location_label=:location_label, postcode=:postcode,
                    lat=:lat, lng=:lng, notes=:notes
                WHERE date=:date AND instrument_serial=:serial
            ''', sm)
        return
    if row['meta_updated_at'] is not None:
        local = _lww_key(row['meta_updated_at'], row['meta_writer'])
        incoming = _lww_key(sm['meta_updated_at'], sm['meta_writer'])
        if incoming == local:
            _clear_conflict(conn, 'session_meta', uid)
            return
        if incoming < local:
            _record_conflict(conn, 'session_meta', uid, row['meta_updated_at'],
                             sm['meta_updated_at'], sm)
            return
    _clear_conflict(conn, 'session_meta', uid)
    conn.execute('''
        UPDATE sessions SET recorder_name=:recorder_name,
            location_label=:location_label, postcode=:postcode,
            lat=:lat, lng=:lng, notes=:notes,
            meta_updated_at=:meta_updated_at, meta_writer=:meta_writer
        WHERE date=:date AND instrument_serial=:serial
    ''', sm)


def _apply_run_tag(conn, rt, coalesce=False):
    """Apply one run_tag row: session by (session_date, serial), run by
    source_file when the row carries one (the stable identity), else by
    run_number (older peer).

    Same LWW gate as _apply_session_meta since WP12 (F4), on the run's
    (tag_updated_at, tag_writer): a stamped winner sets the tag — or clears
    it, which the old COALESCE full-sync merge could never propagate — and a
    stamped loser lands in sync_conflicts (table_name 'run_tag', uid
    'date|serial|source_file'). Unstamped rows keep the pre-WP12 behaviour
    against unstamped local rows and lose silently to stamped ones."""
    rt = _with_serial(conn, rt, key='serial')
    rt.setdefault('source_file', None)
    for c in ('location_tag', 'tag_updated_at', 'tag_writer'):
        rt.setdefault(c, None)
    run_match = ('source_file=:source_file' if rt['source_file']
                 else 'run_number=:run_number')
    row = conn.execute(f'''
        SELECT r.id, r.tag_updated_at, r.tag_writer FROM runs r
        JOIN sessions s ON s.id=r.session_id
        WHERE s.date=:session_date AND s.instrument_serial=:serial
          AND r.{run_match}
    ''', rt).fetchone()
    if row is None:
        return
    uid = '%s|%s|%s' % (rt['session_date'], rt['serial'],
                        rt['source_file'] or 'run:%s' % rt.get('run_number'))
    if rt['tag_updated_at'] is None:
        if row['tag_updated_at'] is not None:
            return   # a stamped local edit beats unstamped data; no conflict
        set_sql = ('location_tag=COALESCE(:location_tag, location_tag)'
                   if coalesce else 'location_tag=:location_tag')
        conn.execute(f'UPDATE runs SET {set_sql} WHERE id=:_rid',
                     {**rt, '_rid': row['id']})
        return
    if row['tag_updated_at'] is not None:
        local = _lww_key(row['tag_updated_at'], row['tag_writer'])
        incoming = _lww_key(rt['tag_updated_at'], rt['tag_writer'])
        if incoming == local:
            _clear_conflict(conn, 'run_tag', uid)
            return
        if incoming < local:
            _record_conflict(conn, 'run_tag', uid, row['tag_updated_at'],
                             rt['tag_updated_at'], rt)
            return
    _clear_conflict(conn, 'run_tag', uid)
    conn.execute(
        'UPDATE runs SET location_tag=?, tag_updated_at=?, tag_writer=? '
        'WHERE id=?',
        (rt['location_tag'], rt['tag_updated_at'], rt['tag_writer'], row['id']))


def apply_sync_event(entity, action, data):
    """Apply a single sync event pushed from the peer after a mutation.

    The uid-keyed entities accept both shapes: a WP10 peer sends uids (and
    LWW timestamps), an older peer sends bare local ids — the fallback keeps
    a mixed-version pair on the old id-keyed behaviour instead of breaking."""
    # Session deletions run through the same noise_db functions the local
    # routes use, so they record their own tombstone here too and will keep
    # propagating if this Pi has a peer of its own that is currently offline.
    # They take their own connection, hence the early return.
    if entity == 'session':
        if action == 'delete' and data.get('date'):
            delete_session(data['date'], data.get('serial'))
        elif action == 'purge_before' and data.get('before'):
            purge_sessions_before(data['before'])
        return

    conn = get_db()
    if entity == 'assessment':
        if action == 'upsert':
            _apply_assessment(conn, data)
        elif action == 'delete':
            _apply_uid_delete(conn, 'assessments', data)
    elif entity == 'assessment_location':
        if action == 'upsert':
            _apply_location(conn, data)
        elif action == 'delete':
            _apply_uid_delete(conn, 'assessment_locations', data)
    elif entity == 'assessment_run':
        if action == 'upsert':
            _apply_assessment_run(conn, data)
        elif action == 'delete':
            _apply_uid_delete(conn, 'assessment_runs', data)
    elif entity == 'report_template':
        # Replicated since WP10 (uid + LWW). An older peer never sends this
        # entity, and sent none of these events to begin with.
        if action == 'upsert':
            _apply_report_template(conn, data)
        elif action == 'delete':
            _apply_uid_delete(conn, 'report_templates', data)
    elif entity == 'session_meta':
        if action == 'upsert':
            _apply_session_meta(conn, data)
    elif entity == 'run_tag':
        if action == 'upsert':
            _apply_run_tag(conn, data)
    elif entity == 'generated_report':
        # Reports replicate by uid (append-only evidence). Deletes tombstone
        # the uid (F6) so a later full sync cannot resurrect the row.
        if action == 'upsert':
            _apply_generated_report(conn, data)
        elif action == 'delete' and data.get('uid'):
            _apply_uid_tombstones(conn, [{'table_name': 'generated_reports',
                                          'uid': data['uid'],
                                          'deleted_at': data.get('deleted_at')}])
    conn.commit()
    conn.close()


# ── F8b: bounded report shipping ─────────────────────────────────────────────

# The most report rows GET /api/reports-sync serves per request. The puller
# chunks its uid list at the same size, so a well-behaved peer never hits the
# cap; a longer list is truncated rather than refused, and the rest arrives on
# the next request (or next tick).
REPORTS_SYNC_MAX = 100


def report_uids_to_fetch(digest):
    """Diff a peer's generated_reports digest ({uid, created_at} rows) against
    what is stored here: the uids to fetch — missing locally, or present with
    a different created_at (a corrected copy). Locally tombstoned uids are
    never fetched: the delete won, and the apply would refuse the row anyway —
    skipping them here just stops the tick re-downloading a deleted report's
    body forever."""
    conn = get_db()
    local = {r['uid']: r['created_at'] for r in conn.execute(
        'SELECT uid, created_at FROM generated_reports WHERE uid IS NOT NULL')}
    dead = {r['uid'] for r in conn.execute(
        "SELECT uid FROM deleted_uids WHERE table_name='generated_reports'")}
    conn.close()
    out = []
    for d in digest:
        uid = d.get('uid')
        if not uid or uid in dead:
            continue
        if uid not in local or (d.get('created_at') is not None
                                and d['created_at'] != local[uid]):
            out.append(uid)
    return out


def get_reports_by_uids(uids):
    """Full generated_reports rows for the named uids, capped at
    REPORTS_SYNC_MAX — the read behind GET /api/reports-sync."""
    uids = [u for u in uids if u][:REPORTS_SYNC_MAX]
    if not uids:
        return []
    conn = get_db()
    rows = conn.execute(
        f'SELECT * FROM generated_reports WHERE uid IN ({",".join("?" * len(uids))})',
        uids).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def apply_report_rows(rows):
    """Apply full report rows fetched by uid (the light-mode counterpart of
    the full payload's generated_reports list). Returns the rows applied."""
    conn = get_db()
    n = 0
    for r in rows:
        if r.get('uid'):
            _apply_generated_report(conn, r)
            n += 1
    conn.commit()
    conn.close()
    return n


def pull_full_sync_from_peer(peer_url, import_key, light_timeout=30,
                             ua='noise-meter/1.0'):
    """Pull the peer's full-sync state the bounded way and apply it (WP14/F3
    made this shared: sync_peer.py's 15-minute tick and peer_client's startup
    sync used to duplicate it, and the startup copy still fetched the full
    unbounded payload — the one transfer left that grew with every report).

    GET /api/peer-sync-full?light=1 (generated reports as {uid, created_at}
    digests), apply it, then fetch only the reports the digest shows missing
    through GET /api/reports-sync, chunked at REPORTS_SYNC_MAX with the
    fetches' own 30 s timeout — `light_timeout` covers only the light call
    (the tick passes 30 s, the startup sync keeps its old 15 s). Old-peer
    fallback: a peer that predates light mode ignores the parameter and
    returns the full payload, which apply_full_sync() applies as before (no
    digest key, nothing extra to fetch). Returns the number of report rows
    fetched by uid. Exceptions propagate — both callers are best-effort and
    log rather than fail their run."""
    base = peer_url.rstrip('/')
    headers = {'X-Import-Key': import_key, 'User-Agent': ua}
    req = urllib.request.Request(base + '/api/peer-sync-full?light=1',
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=light_timeout) as resp:
        payload = json.loads(resp.read())
    apply_full_sync(payload)
    digest = payload.get('generated_reports_digest')
    fetched = 0
    if digest is not None:
        need = report_uids_to_fetch(digest)
        for i in range(0, len(need), REPORTS_SYNC_MAX):
            chunk = need[i:i + REPORTS_SYNC_MAX]
            rep_req = urllib.request.Request(
                base + '/api/reports-sync?uids='
                + urllib.parse.quote(','.join(chunk)),
                headers=headers)
            with urllib.request.urlopen(rep_req, timeout=30) as resp:
                fetched += apply_report_rows(
                    json.loads(resp.read()).get('reports', []))
    return fetched


# ── F4 (WP14): peer clock skew — observability, not correction ───────────────

# |skew| above this is logged as a WARNING by the sync tick. 30 s is far
# beyond healthy NTP (chrony holds the Pis within milliseconds) but well
# inside what matters: every LWW comparison in this file is wall-clock
# ordered, so a Pi whose clock runs slow loses edits it genuinely made later.
CLOCK_SKEW_WARN_S = 30


def record_peer_clock_skew(server_now, local_now=None):
    """Measure and store the peer clock skew from /api/sync's `server_now`.

    skew_s = local UTC now minus the peer's server_now (positive: the peer's
    clock is behind ours). Coarse by design — it includes the response's
    transfer time and second resolution — because its job is monitoring, not
    correction: full logical clocks are out of scope, LWW correctness
    degrades with skew, and this number (stored in sync_state as
    peer_clock_skew_s, surfaced by /health) is how an operator sees that
    hazard growing before it bites. Returns the skew in seconds, or None for
    an absent/unparseable server_now (an old peer sends none) — in which
    case the last stored measurement is left in place, dated by its 'at'."""
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
        try:
            peer_dt = datetime.strptime(server_now, fmt)
            break
        except (ValueError, TypeError):
            continue
    else:
        return None
    local = local_now or datetime.now(timezone.utc).replace(tzinfo=None)
    skew = round((local - peer_dt).total_seconds(), 1)
    set_sync_state('peer_clock_skew_s', json.dumps({
        'skew_s': skew, 'at': local.strftime('%Y-%m-%d %H:%M:%S')}))
    return skew


def get_peer_clock_skew():
    """The last stored skew measurement as {'skew_s': float, 'at': ts}, or
    None when no tick has measured one yet (or the peer is pre-WP12 and
    sends no server_now)."""
    raw = get_sync_state('peer_clock_skew_s')
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def get_import_log(limit=20):
    conn = get_db()
    rows = conn.execute(
        'SELECT date, instrument_serial, run_count, avg_laeq, max_laeq, location_label, '
        'imported_at FROM sessions ORDER BY imported_at DESC LIMIT ?', (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
