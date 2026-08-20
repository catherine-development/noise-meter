"""
Peer-to-peer replication between the two Pis.

Covers the last-sync watermark, the full-state catch-up payload exchanged on
startup, the per-mutation events pushed after each edit, and the import log.

Split out of noise_db.py. The outbound HTTP side of the protocol lives in
peer_client.py; this module is only the database half.

Note that get_sessions_since() stays in noise_db: it is a core session/run read
that the sync protocol happens to call, not part of the protocol itself.
"""
from noise_db import (get_db, delete_session, purge_sessions_before, _record_tombstones,
                      resolve_serial)


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
        conn.execute('DELETE FROM assessment_runs WHERE session_date=? AND instrument_serial=?',
                     (date, serial))
        conn.execute('DELETE FROM sessions WHERE date=? AND instrument_serial=?', (date, serial))
        _record_tombstones(conn, [(date, serial)], deleted_at=deleted_at)


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
    import json
    raw = get_sync_state('last_push_error')
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return {'at': None, 'error': raw}


def get_full_sync_payload():
    """Return all syncable state for peer replication."""
    conn = get_db()
    assessments  = [dict(r) for r in conn.execute('SELECT * FROM assessments').fetchall()]
    locations    = [dict(r) for r in conn.execute('SELECT * FROM assessment_locations').fetchall()]
    assess_runs  = [dict(r) for r in conn.execute('SELECT * FROM assessment_runs').fetchall()]
    # Every session-keyed row carries `serial` beside its date; a receiver on
    # the older schema ignores the key, an older sender omits it and the
    # receiver files the row under its default serial.
    sess_meta    = [dict(r) for r in conn.execute(
        'SELECT date, instrument_serial AS serial, recorder_name, location_label, '
        'postcode, lat, lng, notes FROM sessions'
    ).fetchall()]
    run_tags     = [dict(r) for r in conn.execute(
        'SELECT s.date AS session_date, s.instrument_serial AS serial, '
        'r.run_number, r.source_file, r.location_tag '
        'FROM runs r JOIN sessions s ON r.session_id=s.id '
        'WHERE r.location_tag IS NOT NULL'
    ).fetchall()]
    deleted_sess = [dict(r) for r in conn.execute(
        'SELECT date, instrument_serial AS serial, deleted_at FROM deleted_sessions').fetchall()]
    # Weather is session-keyed reference data and replicates in full (WP9), so
    # a Pi that was offline when the other side fetched — or received — a row
    # catches up here. hourly_json rides along: ~24 rows × 4 series per date,
    # small beside the runs' own spectral payloads.
    weather = [dict(r) for r in conn.execute(
        'SELECT date, instrument_serial AS serial, wind_speed, wind_dir, '
        'temp_min, temp_max, precip, hourly_json FROM weather').fetchall()]
    # Generated reports replicate too — they are append-only evidence, keyed
    # across the pair by uid, never by the local integer id. Report
    # *templates* deliberately do not: they are mutable per-Pi state and need
    # the F6 conflict machinery before they can replicate safely.
    reports = [dict(r) for r in conn.execute(
        'SELECT * FROM generated_reports WHERE uid IS NOT NULL').fetchall()]
    conn.close()
    return {'assessments': assessments, 'assessment_locations': locations,
            'assessment_runs': assess_runs, 'sessions_meta': sess_meta,
            'run_tags': run_tags, 'deleted_sessions': deleted_sess,
            'weather': weather, 'generated_reports': reports}


def apply_full_sync(payload):
    """Upsert a full sync payload received from peer (startup catch-up)."""
    conn = get_db()
    # Deletions are applied before the upserts below so that session metadata
    # for a session the peer deleted is not re-applied to a row we are about
    # to remove.
    _apply_tombstones(conn, payload.get('deleted_sessions', []))
    for a in payload.get('assessments', []):
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
    for loc in payload.get('assessment_locations', []):
        conn.execute('''
            INSERT INTO assessment_locations
                (id,assessment_id,label,description,lat,lng,sort_order,notes)
            VALUES (:id,:assessment_id,:label,:description,:lat,:lng,:sort_order,:notes)
            ON CONFLICT(id) DO UPDATE SET
                label=excluded.label, description=excluded.description,
                lat=excluded.lat, lng=excluded.lng,
                sort_order=excluded.sort_order, notes=excluded.notes
        ''', loc)
    for ar in payload.get('assessment_runs', []):
        # A peer on the older schema sends no source_file. Default it so the
        # named parameter binds, and let COALESCE keep any value we already
        # hold — an old peer's silence must not erase the stable key.
        ar = _with_serial(conn, ar)
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
    for sm in payload.get('sessions_meta', []):
        sm = _with_serial(conn, sm, key='serial')
        # COALESCE: never overwrite existing non-null with null from peer
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
    for rt in payload.get('run_tags', []):
        _apply_run_tag(conn, rt, coalesce=True)
    for wx in payload.get('weather', []):
        _apply_weather(conn, wx)
    for gr in payload.get('generated_reports', []):
        _apply_generated_report(conn, gr)
    conn.commit()
    conn.close()


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

    template_id is carried as provenance only: report templates are per-Pi
    until F6, so the id may name a different (or no) template here;
    template_name is the value everything renders."""
    if not row.get('uid'):
        return   # a row from a pre-WP9 peer has no replication identity
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


def _apply_run_tag(conn, rt, coalesce=False):
    """Apply one run_tag row: session by (session_date, serial), run by
    source_file when the row carries one (the stable identity), else by
    run_number (older peer)."""
    rt = _with_serial(conn, rt, key='serial')
    rt.setdefault('source_file', None)
    set_sql = ('location_tag=COALESCE(:location_tag, location_tag)' if coalesce
               else 'location_tag=:location_tag')
    run_match = ('source_file=:source_file' if rt['source_file']
                 else 'run_number=:run_number')
    conn.execute(f'''
        UPDATE runs SET {set_sql}
        WHERE session_id=(SELECT id FROM sessions
                          WHERE date=:session_date AND instrument_serial=:serial)
          AND {run_match}
    ''', rt)


def apply_sync_event(entity, action, data):
    """Apply a single sync event pushed from the peer after a mutation."""
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
            conn.execute('''
                INSERT INTO assessments
                    (id,name,purpose,standard,address,postcode,lat,lng,client_ref,notes,created_at)
                VALUES (:id,:name,:purpose,:standard,:address,:postcode,:lat,:lng,:client_ref,:notes,:created_at)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, purpose=excluded.purpose, standard=excluded.standard,
                    address=excluded.address, postcode=excluded.postcode,
                    lat=excluded.lat, lng=excluded.lng,
                    client_ref=excluded.client_ref, notes=excluded.notes
            ''', data)
        elif action == 'delete':
            conn.execute('DELETE FROM assessments WHERE id=?', (data['id'],))
    elif entity == 'assessment_location':
        if action == 'upsert':
            conn.execute('''
                INSERT INTO assessment_locations
                    (id,assessment_id,label,description,lat,lng,sort_order,notes)
                VALUES (:id,:assessment_id,:label,:description,:lat,:lng,:sort_order,:notes)
                ON CONFLICT(id) DO UPDATE SET
                    label=excluded.label, description=excluded.description,
                    lat=excluded.lat, lng=excluded.lng,
                    sort_order=excluded.sort_order, notes=excluded.notes
            ''', data)
        elif action == 'delete':
            conn.execute('DELETE FROM assessment_locations WHERE id=?', (data['id'],))
    elif entity == 'assessment_run':
        if action == 'upsert':
            data = _with_serial(conn, data)   # older peer — see above
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
            ''', data)
        elif action == 'delete':
            conn.execute('DELETE FROM assessment_runs WHERE id=?', (data['id'],))
    elif entity == 'session_meta':
        if action == 'upsert':
            conn.execute('''
                UPDATE sessions SET recorder_name=:recorder_name, location_label=:location_label,
                    postcode=:postcode, lat=:lat, lng=:lng, notes=:notes
                WHERE date=:date AND instrument_serial=:serial
            ''', _with_serial(conn, data, key='serial'))
    elif entity == 'run_tag':
        if action == 'upsert':
            _apply_run_tag(conn, data)
    elif entity == 'generated_report':
        # Reports replicate by uid (append-only evidence). There is no
        # 'report_template' entity on purpose — templates stay per-Pi until
        # the F6 conflict machinery exists.
        if action == 'upsert':
            _apply_generated_report(conn, data)
        elif action == 'delete' and data.get('uid'):
            conn.execute('DELETE FROM generated_reports WHERE uid=?', (data['uid'],))
    conn.commit()
    conn.close()


def get_import_log(limit=20):
    conn = get_db()
    rows = conn.execute(
        'SELECT date, instrument_serial, run_count, avg_laeq, max_laeq, location_label, '
        'imported_at FROM sessions ORDER BY imported_at DESC LIMIT ?', (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
