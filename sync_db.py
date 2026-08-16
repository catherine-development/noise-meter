"""
Peer-to-peer replication between the two Pis.

Covers the last-sync watermark, the full-state catch-up payload exchanged on
startup, the per-mutation events pushed after each edit, and the import log.

Split out of noise_db.py. The outbound HTTP side of the protocol lives in
peer_client.py; this module is only the database half.

Note that get_sessions_since() stays in noise_db: it is a core session/run read
that the sync protocol happens to call, not part of the protocol itself.
"""
from noise_db import get_db


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


def get_full_sync_payload():
    """Return all syncable state for peer replication."""
    conn = get_db()
    assessments  = [dict(r) for r in conn.execute('SELECT * FROM assessments').fetchall()]
    locations    = [dict(r) for r in conn.execute('SELECT * FROM assessment_locations').fetchall()]
    assess_runs  = [dict(r) for r in conn.execute('SELECT * FROM assessment_runs').fetchall()]
    sess_meta    = [dict(r) for r in conn.execute(
        'SELECT date, recorder_name, location_label, postcode, lat, lng, notes FROM sessions'
    ).fetchall()]
    run_tags     = [dict(r) for r in conn.execute(
        'SELECT s.date AS session_date, r.run_number, r.location_tag '
        'FROM runs r JOIN sessions s ON r.session_id=s.id '
        'WHERE r.location_tag IS NOT NULL'
    ).fetchall()]
    conn.close()
    return {'assessments': assessments, 'assessment_locations': locations,
            'assessment_runs': assess_runs, 'sessions_meta': sess_meta, 'run_tags': run_tags}


def apply_full_sync(payload):
    """Upsert a full sync payload received from peer (startup catch-up)."""
    conn = get_db()
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
        conn.execute('''
            INSERT INTO assessment_runs
                (id,assessment_id,location_id,session_date,run_number,conditions,notes)
            VALUES (:id,:assessment_id,:location_id,:session_date,:run_number,:conditions,:notes)
            ON CONFLICT(id) DO UPDATE SET
                location_id=excluded.location_id,
                conditions=excluded.conditions, notes=excluded.notes
        ''', ar)
    for sm in payload.get('sessions_meta', []):
        # COALESCE: never overwrite existing non-null with null from peer
        conn.execute('''
            UPDATE sessions SET
                recorder_name=COALESCE(:recorder_name, recorder_name),
                location_label=COALESCE(:location_label, location_label),
                postcode=COALESCE(:postcode, postcode),
                lat=COALESCE(:lat, lat),
                lng=COALESCE(:lng, lng),
                notes=COALESCE(:notes, notes)
            WHERE date=:date
        ''', sm)
    for rt in payload.get('run_tags', []):
        conn.execute('''
            UPDATE runs SET location_tag=COALESCE(:location_tag, location_tag)
            WHERE session_id=(SELECT id FROM sessions WHERE date=:session_date)
              AND run_number=:run_number
        ''', rt)
    conn.commit()
    conn.close()


def apply_sync_event(entity, action, data):
    """Apply a single sync event pushed from the peer after a mutation."""
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
            conn.execute('''
                INSERT INTO assessment_runs
                    (id,assessment_id,location_id,session_date,run_number,conditions,notes)
                VALUES (:id,:assessment_id,:location_id,:session_date,:run_number,:conditions,:notes)
                ON CONFLICT(id) DO UPDATE SET
                    location_id=excluded.location_id,
                    conditions=excluded.conditions, notes=excluded.notes
            ''', data)
        elif action == 'delete':
            conn.execute('DELETE FROM assessment_runs WHERE id=?', (data['id'],))
    elif entity == 'session_meta':
        if action == 'upsert':
            conn.execute('''
                UPDATE sessions SET recorder_name=:recorder_name, location_label=:location_label,
                    postcode=:postcode, lat=:lat, lng=:lng, notes=:notes
                WHERE date=:date
            ''', data)
    elif entity == 'run_tag':
        if action == 'upsert':
            conn.execute('''
                UPDATE runs SET location_tag=:location_tag
                WHERE session_id=(SELECT id FROM sessions WHERE date=:session_date)
                  AND run_number=:run_number
            ''', data)
    conn.commit()
    conn.close()


def get_import_log(limit=20):
    conn = get_db()
    rows = conn.execute(
        'SELECT date, run_count, avg_laeq, max_laeq, location_label, imported_at '
        'FROM sessions ORDER BY imported_at DESC LIMIT ?', (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
