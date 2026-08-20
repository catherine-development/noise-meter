"""
Assessments subsystem: BS 4142 / Noise Act assessment records, their
measurement locations, and the runs assigned to each location.

Split out of noise_db.py. Assessments reference sessions and runs by
(session_date, instrument_serial, source_file) — run_number as the legacy
fallback — but own their own tables entirely; nothing in the core session/run
measurement model depends on this module.

The assessments / assessment_locations / assessment_runs tables are still
created by noise_db._migrate(), which remains the single schema authority.
"""
import json

from nor140_format import round_half_up
from noise_db import (get_db, percentile, resolve_serial, new_uid,
                      record_uid_tombstones, LWW_NOW_SQL, local_writer)

# WP12 (F3): every local mutation stamps updated_at at millisecond resolution
# (LWW_NOW_SQL) and records the writing Pi (local_writer()). The replication
# gate in sync_db orders on the (updated_at, writer) tuple, so two Pis editing
# the same row in the same second no longer swap values on every exchange.


def _with_end(row):
    """Ensure the run row carries an end_time key (the meter's value, or None).

    No arithmetic fallback — see _run_to_dict in noise_db.
    """
    row.setdefault('end_time', None)
    return row


# Both standards divide the day at the same two clock times, so one set of
# boundaries serves both: 07:00 and 23:00.
_PERIOD_BOUNDARY_H = (7, 23)


def _hms_to_s(hms):
    """'HH:MM:SS' -> seconds since midnight, or None if it will not parse."""
    try:
        parts = [int(p) for p in str(hms).split(':')]
    except (ValueError, AttributeError):
        return None
    if len(parts) < 2:
        return None
    h, m = parts[0], parts[1]
    s = parts[2] if len(parts) > 2 else 0
    return h * 3600 + m * 60 + s


def _time_period(start_time, standard):
    """The assessment period a run's *start* falls in.

    BS 4142:2014+A1:2019 defines two periods only — day 07:00–23:00 and night
    23:00–07:00. This used to return a third, 'Evening' (19:00–23:00), which the
    standard does not define: evening is a Noise Act / planning-guidance idea,
    and reporting it as a BS 4142 period put a category in the evidence that the
    cited standard has no meaning for. The Noise Act split is unchanged.

    The period is decided by the start time alone; a run that crosses a boundary
    is flagged separately — see _spans_boundary().
    """
    try:
        h = int(start_time.split(':')[0])
    except Exception:
        return 'unknown'
    if standard == 'bs4142':
        return 'Day' if 7 <= h < 23 else 'Night'
    return 'Post 23:00' if (h >= 23 or h < 7) else 'Pre 23:00'


def _spans_boundary(start_time, end_time):
    """True when a run crosses 07:00 or 23:00, False when it does not.

    None when it cannot be known — i.e. the meter recorded no end time. There is
    deliberately no arithmetic fallback to start + n_samples (see _run_to_dict
    in noise_db): a run that was paused or stopped mid-period would be judged on
    a time it never ran to.

    Matters because _time_period() classifies on the start time alone, so a run
    from 22:30 to 23:30 is reported as a Day measurement while an hour of it is
    night. A run that ends exactly on a boundary has not crossed it.
    """
    if not end_time:
        return None
    s, e = _hms_to_s(start_time), _hms_to_s(end_time)
    if s is None or e is None:
        return None
    if e <= s:
        e += 86400          # ran through midnight
    for h in _PERIOD_BOUNDARY_H:
        b = h * 3600
        while b <= s:       # first occurrence of this boundary after the start
            b += 86400
        if b < e:           # strictly inside the run, so it was crossed
            return True
    return False


def create_assessment(name, purpose='', standard='noise_act', address='',
                      postcode='', lat=None, lng=None, client_ref='', notes=''):
    conn = get_db()
    cur = conn.execute(
        'INSERT INTO assessments (uid, name, purpose, standard, address, postcode, '
        f'  lat, lng, client_ref, notes, updated_at, writer) '
        f'VALUES (?,?,?,?,?,?,?,?,?,?,{LWW_NOW_SQL},?)',
        (new_uid(), name, purpose or None, standard, address or None, postcode or None,
         lat, lng, client_ref or None, notes or None, local_writer())
    )
    aid = cur.lastrowid
    conn.commit()
    conn.close()
    return aid


def list_assessments():
    conn = get_db()
    rows = conn.execute('''
        SELECT a.*,
               COUNT(DISTINCT al.id) AS loc_count,
               COUNT(ar.id)          AS run_count,
               MIN(ar.session_date)  AS date_from,
               MAX(ar.session_date)  AS date_to
        FROM assessments a
        LEFT JOIN assessment_locations al ON al.assessment_id = a.id
        LEFT JOIN assessment_runs ar ON ar.assessment_id = a.id
        GROUP BY a.id
        ORDER BY a.created_at DESC
    ''').fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_assessment(aid):
    conn = get_db()
    row = conn.execute('SELECT * FROM assessments WHERE id=?', (aid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_assessment(aid, name, purpose, standard, address, postcode,
                      lat, lng, client_ref, notes):
    conn = get_db()
    conn.execute(
        'UPDATE assessments SET name=?, purpose=?, standard=?, address=?, '
        f'  postcode=?, lat=?, lng=?, client_ref=?, notes=?, '
        f'  updated_at={LWW_NOW_SQL}, writer=? '
        'WHERE id=?',
        (name, purpose or None, standard, address or None, postcode or None,
         lat, lng, client_ref or None, notes or None, local_writer(), aid)
    )
    conn.commit()
    conn.close()


def delete_assessment(aid):
    """Delete an assessment (locations and run links cascade via FK) and
    tombstone its uid so the delete replicates and cannot be resurrected by a
    full sync from a Pi that still holds the row. The children need no
    tombstones of their own: the peer's apply deletes the parent by uid and
    its own FKs cascade, and an incoming child row whose parent uid is
    tombstoned is skipped. Returns {'uid', 'deleted_at'} for the sync event,
    or None if the row (or its uid) was absent."""
    conn = get_db()
    info = _delete_with_tombstone(conn, 'assessments', aid)
    conn.commit()
    conn.close()
    return info


def _delete_with_tombstone(conn, table, row_id):
    """Delete one row by local id, tombstoning its uid. Caller commits."""
    row = conn.execute(f'SELECT uid FROM {table} WHERE id=?', (row_id,)).fetchone()
    conn.execute(f'DELETE FROM {table} WHERE id=?', (row_id,))
    if not (row and row['uid']):
        return None
    record_uid_tombstones(conn, table, [row['uid']])
    ts = conn.execute(
        'SELECT deleted_at FROM deleted_uids WHERE table_name=? AND uid=?',
        (table, row['uid'])).fetchone()
    return {'uid': row['uid'], 'deleted_at': ts['deleted_at'] if ts else None}


def _uid_of(conn, table, row_id):
    """The uid of a local row, or None — the uid-based FK reference columns
    (assessment_uid, location_uid) are populated from the local integer FKs
    at write time so every replication payload can key on them."""
    if row_id is None:
        return None
    row = conn.execute(f'SELECT uid FROM {table} WHERE id=?', (row_id,)).fetchone()
    return row['uid'] if row else None


def add_assessment_location(assessment_id, label, description='', lat=None, lng=None, notes=''):
    conn = get_db()
    max_order = conn.execute(
        'SELECT COALESCE(MAX(sort_order), -1) FROM assessment_locations WHERE assessment_id=?',
        (assessment_id,)
    ).fetchone()[0]
    cur = conn.execute(
        'INSERT INTO assessment_locations '
        '  (uid, assessment_uid, assessment_id, label, description, lat, lng, '
        f'   sort_order, notes, updated_at, writer) '
        f'VALUES (?,?,?,?,?,?,?,?,?,{LWW_NOW_SQL},?)',
        (new_uid(), _uid_of(conn, 'assessments', assessment_id), assessment_id,
         label, description or None, lat, lng, max_order + 1, notes or None,
         local_writer())
    )
    loc_id = cur.lastrowid
    conn.commit()
    conn.close()
    return loc_id


def get_assessment_location(loc_id):
    conn = get_db()
    row = conn.execute('SELECT * FROM assessment_locations WHERE id=?', (loc_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_assessment_location(loc_id, label, description, lat, lng, notes):
    conn = get_db()
    conn.execute(
        'UPDATE assessment_locations SET label=?, description=?, lat=?, lng=?, '
        f'  notes=?, updated_at={LWW_NOW_SQL}, writer=? WHERE id=?',
        (label, description or None, lat, lng, notes or None, local_writer(), loc_id)
    )
    conn.commit()
    conn.close()


def delete_assessment_location(loc_id):
    """Run links keep their rows (location_id goes NULL via the FK); only the
    location itself is tombstoned. Returns {'uid','deleted_at'} or None."""
    conn = get_db()
    info = _delete_with_tombstone(conn, 'assessment_locations', loc_id)
    conn.commit()
    conn.close()
    return info


def assign_runs(assessment_id, location_id, run_pairs):
    """Link runs to a location. run_pairs items are (date, run_number),
    (date, run_number, source_file) or (date, run_number, source_file, serial);
    a missing or blank serial means the default instrument.

    The upsert targets source_file, not run_number. Keying writes on the
    position while reads followed source_file let two things go wrong once
    numbering shifted: re-assigning the same physical run at its new number
    inserted a duplicate, and assigning whatever had taken the old number
    silently rebound the existing link to a different measurement.
    """
    conn = get_db()
    a_uid = _uid_of(conn, 'assessments', assessment_id)
    loc_uid = _uid_of(conn, 'assessment_locations', location_id)
    touched = []
    for pair in run_pairs:
        date, run_num = pair[0], pair[1]
        source_file = pair[2] if len(pair) > 2 else None
        serial = resolve_serial(pair[3] if len(pair) > 3 else None, conn)
        if source_file:
            # Trust nothing from the client about identity: the stable key must
            # name a run that actually exists in the session being assigned.
            ok = conn.execute(
                'SELECT 1 FROM runs r JOIN sessions s ON s.id=r.session_id '
                'WHERE s.date=? AND s.instrument_serial=? AND r.source_file=?',
                (date, serial, source_file)).fetchone()
            if not ok:
                conn.close()
                raise ValueError(
                    'source_file %r is not a run in session %s (serial %r)'
                    % (source_file, date, serial))
        if not source_file:
            row = conn.execute(
                'SELECT r.source_file FROM runs r JOIN sessions s ON s.id=r.session_id '
                'WHERE s.date=? AND s.instrument_serial=? AND r.run_number=?',
                (date, serial, run_num)).fetchone()
            source_file = row['source_file'] if row else None

        if source_file:
            # Adopt a legacy row for the same position rather than inserting
            # beside it: it has no stable key, so it cannot conflict, and the
            # link would silently become two.
            legacy = conn.execute(
                'SELECT id FROM assessment_runs WHERE assessment_id=? AND session_date=? '
                'AND instrument_serial=? AND run_number=? AND source_file IS NULL',
                (assessment_id, date, serial, run_num)).fetchone()
            if legacy:
                conn.execute(
                    'UPDATE assessment_runs SET source_file=?, location_id=?, '
                    f'  location_uid=?, updated_at={LWW_NOW_SQL}, writer=? WHERE id=?',
                    (source_file, location_id, loc_uid, local_writer(), legacy['id']))
                touched.append(legacy['id'])
                continue
            conn.execute(
                'INSERT INTO assessment_runs '
                '  (uid, assessment_uid, location_uid, assessment_id, location_id, '
                '   session_date, instrument_serial, run_number, source_file, '
                f'   updated_at, writer) '
                f'VALUES (?,?,?,?,?,?,?,?,?,{LWW_NOW_SQL},?) '
                # The index is partial, so the conflict target has to repeat
                # its WHERE clause for SQLite to match it. The existing row
                # keeps its uid — identity is minted once.
                'ON CONFLICT(assessment_id, session_date, instrument_serial, source_file) '
                '  WHERE source_file IS NOT NULL '
                'DO UPDATE SET location_id=excluded.location_id, '
                '  location_uid=excluded.location_uid, '
                '  run_number=excluded.run_number, updated_at=excluded.updated_at, '
                '  writer=excluded.writer',
                (new_uid(), a_uid, loc_uid, assessment_id, location_id,
                 date, serial, run_num, source_file, local_writer())
            )
            row = conn.execute(
                'SELECT id FROM assessment_runs WHERE assessment_id=? AND session_date=? '
                'AND instrument_serial=? AND source_file=?',
                (assessment_id, date, serial, source_file)).fetchone()
            if row:
                touched.append(row['id'])
        else:
            # No stable key available (a run that never carried a source_file).
            # Fall back to the positional key, and do not create a second row.
            existing = conn.execute(
                'SELECT id FROM assessment_runs WHERE assessment_id=? AND session_date=? '
                'AND instrument_serial=? AND run_number=? AND source_file IS NULL',
                (assessment_id, date, serial, run_num)).fetchone()
            if existing:
                conn.execute(
                    'UPDATE assessment_runs SET location_id=?, location_uid=?, '
                    f'  updated_at={LWW_NOW_SQL}, writer=? WHERE id=?',
                    (location_id, loc_uid, local_writer(), existing['id']))
                touched.append(existing['id'])
            else:
                cur = conn.execute(
                    'INSERT INTO assessment_runs '
                    '  (uid, assessment_uid, location_uid, assessment_id, location_id, '
                    f'   session_date, instrument_serial, run_number, updated_at, writer) '
                    f'VALUES (?,?,?,?,?,?,?,?,{LWW_NOW_SQL},?)',
                    (new_uid(), a_uid, loc_uid, assessment_id, location_id,
                     date, serial, run_num, local_writer()))
                touched.append(cur.lastrowid)
    conn.commit()
    rows = [dict(r) for r in conn.execute(
        'SELECT * FROM assessment_runs WHERE id IN (%s)'
        % ','.join('?' * len(touched)), touched).fetchall()] if touched else []
    conn.close()
    return rows


def get_assessment_run(ar_id):
    conn = get_db()
    row = conn.execute('SELECT * FROM assessment_runs WHERE id=?', (ar_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_assessment_runs_by_pairs(assessment_id, pairs):
    """Return assessment_run rows for (session_date, run_number[, serial]) pairs."""
    conn = get_db()
    rows = []
    for pair in pairs:
        date, num = pair[0], pair[1]
        serial = resolve_serial(pair[2] if len(pair) > 2 else None, conn)
        r = conn.execute(
            'SELECT * FROM assessment_runs WHERE assessment_id=? AND session_date=? '
            'AND instrument_serial=? AND run_number=?',
            (assessment_id, date, serial, num)
        ).fetchone()
        if r:
            rows.append(dict(r))
    conn.close()
    return rows


def unassign_run(ar_id):
    """Returns {'uid','deleted_at'} for the sync event, or None."""
    conn = get_db()
    info = _delete_with_tombstone(conn, 'assessment_runs', ar_id)
    conn.commit()
    conn.close()
    return info


def update_assessment_run(ar_id, conditions, notes):
    conn = get_db()
    conn.execute(
        f'UPDATE assessment_runs SET conditions=?, notes=?, '
        f'  updated_at={LWW_NOW_SQL}, writer=? '
        'WHERE id=?',
        (conditions or None, notes or None, local_writer(), ar_id)
    )
    conn.commit()
    conn.close()


def get_assessment_detail(aid):
    conn = get_db()
    a = conn.execute('SELECT * FROM assessments WHERE id=?', (aid,)).fetchone()
    if not a:
        conn.close()
        return None
    assessment = dict(a)

    locs = conn.execute(
        'SELECT * FROM assessment_locations WHERE assessment_id=? ORDER BY sort_order, label',
        (aid,)
    ).fetchall()

    locations = []
    for loc in locs:
        loc_dict = dict(loc)
        assigned = conn.execute('''
            SELECT ar.id as ar_id, ar.conditions, ar.notes as run_notes,
                   r.run_number, r.start_time, r.end_time, r.n_samples, r.step,
                   r.avg_laeq, r.min_laeq, r.max_laeq, r.max_lcpeak, r.max_laimax,
                   r.location_tag, ar.session_date, ar.instrument_serial
            FROM assessment_runs ar
            JOIN sessions s ON s.date = ar.session_date
                           AND s.instrument_serial = ar.instrument_serial
            JOIN runs r ON r.session_id = s.id AND (
                 r.source_file = ar.source_file
                 OR (ar.source_file IS NULL AND r.run_number = ar.run_number))
            WHERE ar.assessment_id=? AND ar.location_id=?
            ORDER BY ar.session_date, r.start_time
        ''', (aid, loc['id'])).fetchall()
        loc_dict['runs'] = [_with_end(dict(r)) for r in assigned]
        locations.append(loc_dict)

    assessment['locations'] = locations

    unlocated = conn.execute('''
        SELECT ar.id as ar_id, ar.conditions, ar.notes as run_notes,
               r.run_number, r.start_time, r.end_time, r.n_samples, r.step,
               r.avg_laeq, r.min_laeq, r.max_laeq, r.max_lcpeak, r.max_laimax,
               r.location_tag, ar.session_date, ar.instrument_serial
        FROM assessment_runs ar
        JOIN sessions s ON s.date = ar.session_date
                       AND s.instrument_serial = ar.instrument_serial
        JOIN runs r ON r.session_id = s.id AND (
                 r.source_file = ar.source_file
                 OR (ar.source_file IS NULL AND r.run_number = ar.run_number))
        WHERE ar.assessment_id=? AND ar.location_id IS NULL
        ORDER BY ar.session_date, r.start_time
    ''', (aid,)).fetchall()
    assessment['unlocated_runs'] = [_with_end(dict(r)) for r in unlocated]

    conn.close()
    return assessment


def get_all_runs_for_assessment(assessment_id):
    conn = get_db()
    rows = conn.execute('''
        SELECT s.date as session_date, s.instrument_serial, s.location_label as session_loc,
               r.id as run_db_id, r.run_number, r.source_file, r.start_time, r.end_time,
               r.n_samples, r.step,
               r.avg_laeq, r.max_laeq, r.max_lcpeak, r.max_laimax, r.location_tag
        FROM runs r
        JOIN sessions s ON r.session_id = s.id
        ORDER BY s.date, s.instrument_serial, r.run_number
    ''').fetchall()

    ar_rows = conn.execute(
        'SELECT id as ar_id, session_date, instrument_serial, run_number, source_file, '
        'location_id FROM assessment_runs WHERE assessment_id=?', (assessment_id,)
    ).fetchall()
    # Keyed on source_file so the pool marks the run that is actually linked.
    # Keyed on run_number it marked whichever run had inherited the number,
    # which is what walked users into re-assigning the wrong measurement.
    assigned = {(ar['session_date'], ar['instrument_serial'], ar['source_file']): dict(ar)
                for ar in ar_rows if ar['source_file']}
    assigned_legacy = {(ar['session_date'], ar['instrument_serial'], ar['run_number']): dict(ar)
                       for ar in ar_rows if not ar['source_file']}

    loc_rows = conn.execute(
        'SELECT id, label FROM assessment_locations WHERE assessment_id=?',
        (assessment_id,)
    ).fetchall()
    loc_labels = {r['id']: r['label'] for r in loc_rows}

    conn.close()

    result = []
    for r in rows:
        row = _with_end(dict(r))
        ar = (assigned.get((r['session_date'], r['instrument_serial'], r['source_file']))
              or assigned_legacy.get((r['session_date'], r['instrument_serial'], r['run_number'])))
        row['ar_id'] = ar['ar_id'] if ar else None
        row['assigned_location_id'] = ar['location_id'] if ar else None
        row['assigned_location_label'] = (
            loc_labels.get(ar['location_id']) if ar and ar['location_id'] else None
        )
        result.append(row)
    return result


def prepare_assessment_report_data(aid):
    conn = get_db()
    a = conn.execute('SELECT * FROM assessments WHERE id=?', (aid,)).fetchone()
    if not a:
        conn.close()
        return None
    assessment = dict(a)

    locs = conn.execute(
        'SELECT * FROM assessment_locations WHERE assessment_id=? ORDER BY sort_order, label',
        (aid,)
    ).fetchall()

    locations_data = []
    for loc in locs:
        assigned = conn.execute('''
            SELECT ar.conditions, ar.notes as run_notes,
                   r.run_number, r.start_time, r.end_time, r.n_samples, r.step, r.duration_s,
                   r.avg_laeq, r.min_laeq, r.max_laeq, r.max_lcpeak, r.max_laimax,
                   r.la_l10, r.la_l50, r.la_l90, r.prof_lafspl_json, ar.session_date,
                   ar.instrument_serial
            FROM assessment_runs ar
            JOIN sessions s ON s.date = ar.session_date
                           AND s.instrument_serial = ar.instrument_serial
            JOIN runs r ON r.session_id = s.id AND (
                 r.source_file = ar.source_file
                 OR (ar.source_file IS NULL AND r.run_number = ar.run_number))
            WHERE ar.assessment_id=? AND ar.location_id=?
            ORDER BY ar.session_date, r.start_time
        ''', (aid, loc['id'])).fetchall()

        runs_data = []
        for r in assigned:
            # Prefer the meter's own percentiles. The fallback, for runs
            # imported before the GLOB scalars were decoded, is now the stored
            # 1-second LAFspl series through the same interpolating percentile()
            # the reports use. It was computed from laeq_json — the downsampled
            # *chart* profile, whose every point is the maximum of its window —
            # with a nearest-rank index, so the same run could be given one
            # LA10 in an assessment CSV and a different, lower one in its
            # report. Where there is no stored series, nothing is reported:
            # there is no honest estimate to be had from the chart profile.
            if r['la_l10'] is not None:
                la_stats = {
                    'la10': round_half_up(r['la_l10'], 1),
                    'la50': round_half_up(r['la_l50'], 1) if r['la_l50'] is not None else None,
                    'la90': round_half_up(r['la_l90'], 1) if r['la_l90'] is not None else None,
                }
            else:
                prof = json.loads(r['prof_lafspl_json']) if r['prof_lafspl_json'] else []
                la_stats = {'la10': None, 'la50': None, 'la90': None}
                if prof:
                    sv = sorted(prof)
                    # LA90 is the level exceeded 90% of the time = 10th percentile.
                    la_stats = {'la10': percentile(sv, 90),
                                'la50': percentile(sv, 50),
                                'la90': percentile(sv, 10)}
            runs_data.append({
                'date': r['session_date'],
                'serial': r['instrument_serial'],
                'start_time': r['start_time'],
                'end_time': r['end_time'],
                # Meter-stored duration; no arithmetic fallback — blank rather
                # than a plausible-looking wrong value when the meter didn't
                # record one (older imports). n_samples is the separate,
                # always-present 1-second record count.
                'duration_s': r['duration_s'],
                'n_samples': r['n_samples'],
                'time_period': _time_period(r['start_time'], assessment['standard']),
                # True/False, or None where the meter recorded no end time.
                'spans_boundary': _spans_boundary(r['start_time'], r['end_time']),
                'avg_laeq': round_half_up(r['avg_laeq'], 1) if r['avg_laeq'] is not None else None,
                'min_laeq': round_half_up(r['min_laeq'], 1) if r['min_laeq'] is not None else None,
                'max_laeq': round_half_up(r['max_laeq'], 1) if r['max_laeq'] is not None else None,
                'max_lcpeak': round_half_up(r['max_lcpeak'], 1) if r['max_lcpeak'] is not None else None,
                'max_laimax': round_half_up(r['max_laimax'], 1) if r['max_laimax'] is not None else None,
                'conditions': r['conditions'],
                'notes': r['run_notes'],
                **la_stats,
            })

        loc_lat = loc['lat'] if loc['lat'] is not None else assessment.get('lat')
        loc_lng = loc['lng'] if loc['lng'] is not None else assessment.get('lng')
        locations_data.append({
            'label': loc['label'],
            'description': loc['description'] or loc['label'],
            'lat': loc_lat,
            'lng': loc_lng,
            'notes': loc['notes'],
            'runs': runs_data,
        })

    conn.close()
    return {
        'assessment': {
            'name': assessment['name'],
            'purpose': assessment['purpose'],
            'standard': assessment['standard'],
            'address': assessment['address'],
            'postcode': assessment['postcode'],
            'client_ref': assessment['client_ref'],
            'notes': assessment['notes'],
        },
        'locations': locations_data,
    }
