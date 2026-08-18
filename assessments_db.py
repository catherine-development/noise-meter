"""
Assessments subsystem: BS 4142 / Noise Act assessment records, their
measurement locations, and the runs assigned to each location.

Split out of noise_db.py. Assessments reference sessions and runs by
(session_date, run_number) but own their own tables entirely; nothing in the
core session/run measurement model depends on this module.

The assessments / assessment_locations / assessment_runs tables are still
created by noise_db._migrate(), which remains the single schema authority.
"""
import json

from noise_db import get_db


def _with_end(row):
    """Ensure the run row carries an end_time key (the meter's value, or None).

    No arithmetic fallback — see _run_to_dict in noise_db.
    """
    row.setdefault('end_time', None)
    return row


def _time_period(start_time, standard):
    try:
        h = int(start_time.split(':')[0])
    except Exception:
        return 'unknown'
    if standard == 'bs4142':
        if 7 <= h < 19:
            return 'Day'
        if 19 <= h < 23:
            return 'Evening'
        return 'Night'
    return 'Post 23:00' if (h >= 23 or h < 7) else 'Pre 23:00'


def create_assessment(name, purpose='', standard='noise_act', address='',
                      postcode='', lat=None, lng=None, client_ref='', notes=''):
    conn = get_db()
    cur = conn.execute(
        'INSERT INTO assessments (name, purpose, standard, address, postcode, '
        '  lat, lng, client_ref, notes) VALUES (?,?,?,?,?,?,?,?,?)',
        (name, purpose or None, standard, address or None, postcode or None,
         lat, lng, client_ref or None, notes or None)
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
        '  postcode=?, lat=?, lng=?, client_ref=?, notes=? WHERE id=?',
        (name, purpose or None, standard, address or None, postcode or None,
         lat, lng, client_ref or None, notes or None, aid)
    )
    conn.commit()
    conn.close()


def delete_assessment(aid):
    conn = get_db()
    conn.execute('DELETE FROM assessments WHERE id=?', (aid,))
    conn.commit()
    conn.close()


def add_assessment_location(assessment_id, label, description='', lat=None, lng=None, notes=''):
    conn = get_db()
    max_order = conn.execute(
        'SELECT COALESCE(MAX(sort_order), -1) FROM assessment_locations WHERE assessment_id=?',
        (assessment_id,)
    ).fetchone()[0]
    cur = conn.execute(
        'INSERT INTO assessment_locations '
        '  (assessment_id, label, description, lat, lng, sort_order, notes) '
        'VALUES (?,?,?,?,?,?,?)',
        (assessment_id, label, description or None, lat, lng, max_order + 1, notes or None)
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
        'UPDATE assessment_locations SET label=?, description=?, lat=?, lng=?, notes=? '
        'WHERE id=?',
        (label, description or None, lat, lng, notes or None, loc_id)
    )
    conn.commit()
    conn.close()


def delete_assessment_location(loc_id):
    conn = get_db()
    conn.execute('DELETE FROM assessment_locations WHERE id=?', (loc_id,))
    conn.commit()
    conn.close()


def assign_runs(assessment_id, location_id, run_pairs):
    """Link runs to a location. run_pairs items are (date, run_number) or
    (date, run_number, source_file).

    The upsert targets source_file, not run_number. Keying writes on the
    position while reads followed source_file let two things go wrong once
    numbering shifted: re-assigning the same physical run at its new number
    inserted a duplicate, and assigning whatever had taken the old number
    silently rebound the existing link to a different measurement.
    """
    conn = get_db()
    touched = []
    for pair in run_pairs:
        date, run_num = pair[0], pair[1]
        source_file = pair[2] if len(pair) > 2 else None
        if source_file:
            # Trust nothing from the client about identity: the stable key must
            # name a run that actually exists in the session being assigned.
            ok = conn.execute(
                'SELECT 1 FROM runs r JOIN sessions s ON s.id=r.session_id '
                'WHERE s.date=? AND r.source_file=?', (date, source_file)).fetchone()
            if not ok:
                conn.close()
                raise ValueError(
                    'source_file %r is not a run in session %s' % (source_file, date))
        if not source_file:
            row = conn.execute(
                'SELECT r.source_file FROM runs r JOIN sessions s ON s.id=r.session_id '
                'WHERE s.date=? AND r.run_number=?', (date, run_num)).fetchone()
            source_file = row['source_file'] if row else None

        if source_file:
            # Adopt a legacy row for the same position rather than inserting
            # beside it: it has no stable key, so it cannot conflict, and the
            # link would silently become two.
            legacy = conn.execute(
                'SELECT id FROM assessment_runs WHERE assessment_id=? AND session_date=? '
                'AND run_number=? AND source_file IS NULL',
                (assessment_id, date, run_num)).fetchone()
            if legacy:
                conn.execute(
                    'UPDATE assessment_runs SET source_file=?, location_id=? WHERE id=?',
                    (source_file, location_id, legacy['id']))
                touched.append(legacy['id'])
                continue
            conn.execute(
                'INSERT INTO assessment_runs '
                '  (assessment_id, location_id, session_date, run_number, source_file) '
                'VALUES (?,?,?,?,?) '
                # The index is partial, so the conflict target has to repeat
                # its WHERE clause for SQLite to match it.
                'ON CONFLICT(assessment_id, session_date, source_file) '
                '  WHERE source_file IS NOT NULL '
                'DO UPDATE SET location_id=excluded.location_id, '
                '  run_number=excluded.run_number',
                (assessment_id, location_id, date, run_num, source_file)
            )
            row = conn.execute(
                'SELECT id FROM assessment_runs WHERE assessment_id=? AND session_date=? '
                'AND source_file=?', (assessment_id, date, source_file)).fetchone()
            if row:
                touched.append(row['id'])
        else:
            # No stable key available (a run that never carried a source_file).
            # Fall back to the positional key, and do not create a second row.
            existing = conn.execute(
                'SELECT id FROM assessment_runs WHERE assessment_id=? AND session_date=? '
                'AND run_number=? AND source_file IS NULL',
                (assessment_id, date, run_num)).fetchone()
            if existing:
                conn.execute('UPDATE assessment_runs SET location_id=? WHERE id=?',
                             (location_id, existing['id']))
                touched.append(existing['id'])
            else:
                cur = conn.execute(
                    'INSERT INTO assessment_runs '
                    '  (assessment_id, location_id, session_date, run_number) '
                    'VALUES (?,?,?,?)', (assessment_id, location_id, date, run_num))
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
    """Return assessment_run rows for (session_date, run_number) pairs."""
    conn = get_db()
    rows = []
    for date, num in pairs:
        r = conn.execute(
            'SELECT * FROM assessment_runs WHERE assessment_id=? AND session_date=? AND run_number=?',
            (assessment_id, date, num)
        ).fetchone()
        if r:
            rows.append(dict(r))
    conn.close()
    return rows


def unassign_run(ar_id):
    conn = get_db()
    conn.execute('DELETE FROM assessment_runs WHERE id=?', (ar_id,))
    conn.commit()
    conn.close()


def update_assessment_run(ar_id, conditions, notes):
    conn = get_db()
    conn.execute(
        'UPDATE assessment_runs SET conditions=?, notes=? WHERE id=?',
        (conditions or None, notes or None, ar_id)
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
                   r.location_tag, ar.session_date
            FROM assessment_runs ar
            JOIN sessions s ON s.date = ar.session_date
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
               r.location_tag, ar.session_date
        FROM assessment_runs ar
        JOIN sessions s ON s.date = ar.session_date
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
        SELECT s.date as session_date, s.location_label as session_loc,
               r.id as run_db_id, r.run_number, r.source_file, r.start_time, r.end_time,
               r.n_samples, r.step,
               r.avg_laeq, r.max_laeq, r.max_lcpeak, r.max_laimax, r.location_tag
        FROM runs r
        JOIN sessions s ON r.session_id = s.id
        ORDER BY s.date, r.run_number
    ''').fetchall()

    ar_rows = conn.execute(
        'SELECT id as ar_id, session_date, run_number, source_file, location_id '
        'FROM assessment_runs WHERE assessment_id=?', (assessment_id,)
    ).fetchall()
    # Keyed on source_file so the pool marks the run that is actually linked.
    # Keyed on run_number it marked whichever run had inherited the number,
    # which is what walked users into re-assigning the wrong measurement.
    assigned = {(ar['session_date'], ar['source_file']): dict(ar)
                for ar in ar_rows if ar['source_file']}
    assigned_legacy = {(ar['session_date'], ar['run_number']): dict(ar)
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
        ar = (assigned.get((r['session_date'], r['source_file']))
              or assigned_legacy.get((r['session_date'], r['run_number'])))
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
                   r.run_number, r.start_time, r.end_time, r.n_samples, r.step,
                   r.avg_laeq, r.min_laeq, r.max_laeq, r.max_lcpeak, r.max_laimax,
                   r.la_l10, r.la_l50, r.la_l90, r.laeq_json, ar.session_date
            FROM assessment_runs ar
            JOIN sessions s ON s.date = ar.session_date
            JOIN runs r ON r.session_id = s.id AND (
                 r.source_file = ar.source_file
                 OR (ar.source_file IS NULL AND r.run_number = ar.run_number))
            WHERE ar.assessment_id=? AND ar.location_id=?
            ORDER BY ar.session_date, r.start_time
        ''', (aid, loc['id'])).fetchall()

        runs_data = []
        for r in assigned:
            # Prefer GLOB-derived percentiles; fall back to profile computation
            if r['la_l10'] is not None:
                la_stats = {
                    'la10': round(r['la_l10'], 1),
                    'la50': round(r['la_l50'], 1) if r['la_l50'] is not None else None,
                    'la90': round(r['la_l90'], 1) if r['la_l90'] is not None else None,
                }
            else:
                laeq_vals = json.loads(r['laeq_json']) if r['laeq_json'] else []
                la_stats = {}
                if laeq_vals:
                    sv = sorted(laeq_vals, reverse=True)
                    n = len(sv)
                    la_stats = {
                        'la10': round(sv[max(0, int(n * 0.1) - 1)], 1),
                        'la50': round(sv[max(0, int(n * 0.5) - 1)], 1),
                        'la90': round(sv[max(0, int(n * 0.9) - 1)], 1),
                    }
            runs_data.append({
                'date': r['session_date'],
                'start_time': r['start_time'],
                'end_time': r['end_time'],
                'duration_s': r['n_samples'],
                'time_period': _time_period(r['start_time'], assessment['standard']),
                'avg_laeq': round(r['avg_laeq'], 1) if r['avg_laeq'] is not None else None,
                'min_laeq': round(r['min_laeq'], 1) if r['min_laeq'] is not None else None,
                'max_laeq': round(r['max_laeq'], 1) if r['max_laeq'] is not None else None,
                'max_lcpeak': round(r['max_lcpeak'], 1) if r['max_lcpeak'] is not None else None,
                'max_laimax': round(r['max_laimax'], 1) if r['max_laimax'] is not None else None,
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
