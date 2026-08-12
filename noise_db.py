import os
import json
import sqlite3

DB_PATH = os.environ.get('NOISE_DB_PATH', '/home/flightdata/noise-meter/noise.db')


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


_DEFAULT_TEMPLATES = [
    {
        'name': 'Local Government Enforcement',
        'description': 'Statutory nuisance evidence under EPA 1990, Noise Act 1996, BS 4142',
        'is_default': 1,
        'prompt': (
            "You are a qualified UK noise consultant producing a professional noise assessment "
            "report for submission to a local authority as evidence supporting a statutory noise "
            "nuisance investigation under the Environmental Protection Act 1990.\n\n"
            "{{session_data}}\n\n"
            "Produce a professional noise assessment report as a JSON object with exactly these keys:\n"
            "- \"executive_summary\": 2–3 sentence plain-English overview suitable for a non-specialist council officer (HTML)\n"
            "- \"methodology\": instrument (Norsonic NOR140 Class 1, IEC 61672-1:2013), parameters measured (LAeq, LA10, LA90, LCpeak), measurement approach, and any limitations (HTML)\n"
            "- \"results_narrative\": narrative of LAeq, LA10 (intrusive events), LA90 (background noise level), any significant peaks, time patterns; compare specific source level to background where identifiable (HTML)\n"
            "- \"compliance\": structured assessment against: (a) Environmental Protection Act 1990 s.79 — statutory nuisance test (unreasonable and substantial interference); (b) Noise Act 1996 — nighttime thresholds where measurements include 23:00–07:00 (35 dB LAeq indoors / 45 dB outside); (c) BS 4142:2014+A1:2019 — difference between specific noise level and background LA90 (+10 dB or above: significant adverse impact; +5 dB: likely adverse); (d) WHO Environmental Noise Guidelines for the European Region 2018. Use HTML tables where helpful.\n"
            "- \"conclusions\": whether measurements indicate a statutory nuisance and whether enforcement action is warranted under EPA 1990 s.80; state confidence level where appropriate (HTML)\n"
            "- \"recommendations\": further monitoring strategy, grounds for an abatement notice, evidence requirements for prosecution, referral pathways (HTML, use <ul> where appropriate)\n\n"
            "Respond with valid JSON only. No markdown fencing. No preamble."
        ),
    },
    {
        'name': 'Occupational Health',
        'description': 'Workplace noise under Control of Noise at Work Regulations 2005 / HSE action values',
        'is_default': 0,
        'prompt': (
            "You are a qualified UK noise consultant producing a professional occupational noise "
            "assessment report under the Control of Noise at Work Regulations 2005.\n\n"
            "{{session_data}}\n\n"
            "Produce a professional noise assessment report as a JSON object with exactly these keys:\n"
            "- \"executive_summary\": 2–3 sentence plain-English overview (HTML)\n"
            "- \"methodology\": instrument (Norsonic NOR140 Class 1, IEC 61672-1:2013), parameters measured, and approach (HTML)\n"
            "- \"results_narrative\": narrative of the per-run and session data — note any significant variability, peaks, and background noise level (LA90) (HTML)\n"
            "- \"compliance\": assessment against: (a) Control of Noise at Work Regulations 2005 — lower action value (80 dB LAeq,8h / 135 dB LCpeak), upper action value (85 dB LAeq,8h / 137 dB LCpeak), exposure limit value (87 dB LAeq,8h / 140 dB LCpeak); (b) BS 4142:2014+A1:2019 significance criteria relative to LA90 background; (c) WHO Environmental Noise Guidelines 2018 if applicable. Use HTML tables where helpful.\n"
            "- \"conclusions\": clear professional conclusions about occupational noise exposure and risk to hearing (HTML)\n"
            "- \"recommendations\": hearing protection requirements, engineering controls, audiometric testing, further monitoring (HTML, use <ul> where appropriate)\n\n"
            "Respond with valid JSON only. No markdown fencing. No preamble."
        ),
    },
    {
        'name': 'Planning Noise Assessment',
        'description': 'NPPF / BS 4142 / BS 8233 assessment for planning applications',
        'is_default': 0,
        'prompt': (
            "You are a qualified UK noise consultant producing a noise assessment report for "
            "submission in support of or in response to a planning application, in accordance with "
            "the National Planning Policy Framework (NPPF) and relevant British Standards.\n\n"
            "{{session_data}}\n\n"
            "Produce a professional noise assessment report as a JSON object with exactly these keys:\n"
            "- \"executive_summary\": 2–3 sentence overview of the noise environment and its relevance to the planning context (HTML)\n"
            "- \"methodology\": instrument (Norsonic NOR140 Class 1, IEC 61672-1:2013), parameters, and relationship to BS 4142:2014+A1:2019 and BS 8233:2014 (HTML)\n"
            "- \"results_narrative\": characterise the acoustic environment — dominant noise sources, LA90 background level, LA10, LAeq, any tonal or impulsive components (HTML)\n"
            "- \"compliance\": structured assessment against: (a) BS 4142:2014+A1:2019 — source-background assessment; (b) BS 8233:2014 — internal ambient noise criteria for residential use (living rooms: 35 dB LAeq,16h daytime / 30 dB LAeq,8h night; bedrooms: 35 dB LAeq,8h night); (c) WHO Environmental Noise Guidelines 2018; (d) NPPF 2023 para 185 — whether development would be adversely affected by or create unacceptable noise impact. Use HTML tables where helpful.\n"
            "- \"conclusions\": suitability of the site for proposed use, or impact of the proposed development on the surrounding noise climate (HTML)\n"
            "- \"recommendations\": mitigation measures, planning conditions, further survey requirements, or objection grounds (HTML, use <ul> where appropriate)\n\n"
            "Respond with valid JSON only. No markdown fencing. No preamble."
        ),
    },
]


def _migrate(conn):
    existing = {row[1] for row in conn.execute('PRAGMA table_info(sessions)').fetchall()}
    new_cols = [
        ('recorder_name',  'TEXT'),
        ('location_label', 'TEXT'),
        ('postcode',       'TEXT'),
        ('lat',            'REAL'),
        ('lng',            'REAL'),
        ('notes',          'TEXT'),
    ]
    for col, typ in new_cols:
        if col not in existing:
            conn.execute(f'ALTER TABLE sessions ADD COLUMN {col} {typ}')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS weather (
            date        TEXT PRIMARY KEY,
            wind_speed  REAL,
            wind_dir    REAL,
            temp_min    REAL,
            temp_max    REAL,
            precip      REAL,
            hourly_json TEXT
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS report_templates (
            id          INTEGER PRIMARY KEY,
            name        TEXT NOT NULL,
            description TEXT,
            prompt      TEXT NOT NULL,
            is_default  INTEGER DEFAULT 0,
            created_at  TEXT DEFAULT (datetime('now')),
            updated_at  TEXT DEFAULT (datetime('now'))
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS generated_reports (
            id             INTEGER PRIMARY KEY,
            session_date   TEXT NOT NULL,
            run_number     INTEGER,
            run_label      TEXT,
            template_id    INTEGER,
            template_name  TEXT,
            model          TEXT NOT NULL,
            thinking_level TEXT NOT NULL DEFAULT 'none',
            sections_json  TEXT NOT NULL,
            input_tokens   INTEGER,
            output_tokens  INTEGER,
            cost_usd       REAL,
            created_at     TEXT DEFAULT (datetime('now', 'localtime'))
        )
    ''')
    gr_cols = {row[1] for row in conn.execute('PRAGMA table_info(generated_reports)').fetchall()}
    for col, typ in [('run_number', 'INTEGER'), ('run_label', 'TEXT')]:
        if col not in gr_cols:
            conn.execute(f'ALTER TABLE generated_reports ADD COLUMN {col} {typ}')
    if not conn.execute('SELECT COUNT(*) FROM report_templates').fetchone()[0]:
        for t in _DEFAULT_TEMPLATES:
            conn.execute(
                'INSERT INTO report_templates (name, description, prompt, is_default) VALUES (?,?,?,?)',
                (t['name'], t['description'], t['prompt'], t['is_default'])
            )
    conn.commit()


def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS sessions (
            id             INTEGER PRIMARY KEY,
            date           TEXT UNIQUE NOT NULL,
            run_count      INTEGER,
            avg_laeq       REAL,
            max_laeq       REAL,
            recorder_name  TEXT,
            location_label TEXT,
            postcode       TEXT,
            lat            REAL,
            lng            REAL,
            imported_at    TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS runs (
            id          INTEGER PRIMARY KEY,
            session_id  INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
            run_number  INTEGER,
            start_time  TEXT,
            n_samples   INTEGER,
            step        INTEGER DEFAULT 1,
            avg_laeq    REAL,
            min_laeq    REAL,
            max_laeq    REAL,
            max_lcpeak  REAL,
            laeq_json   TEXT,
            lcpeak_json TEXT,
            UNIQUE(session_id, run_number)
        );
        CREATE TABLE IF NOT EXISTS sync_state (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_date ON sessions(date);
        CREATE INDEX IF NOT EXISTS idx_runs_session  ON runs(session_id);
    ''')
    conn.commit()
    _migrate(conn)
    conn.close()


def import_sessions(sessions_data, metadata=None):
    meta = metadata or {}
    conn = get_db()
    imported = 0
    for sess in sessions_data:
        date = sess['d']
        projects = sess.get('projects', [])
        conn.execute(
            'INSERT INTO sessions '
            '  (date, run_count, avg_laeq, max_laeq, recorder_name, location_label, postcode, lat, lng, notes) '
            'VALUES (?,?,?,?,?,?,?,?,?,?) '
            'ON CONFLICT(date) DO UPDATE SET '
            '  run_count=excluded.run_count, '
            '  avg_laeq=excluded.avg_laeq, '
            '  max_laeq=excluded.max_laeq, '
            '  recorder_name=COALESCE(excluded.recorder_name, sessions.recorder_name), '
            '  location_label=COALESCE(excluded.location_label, sessions.location_label), '
            '  postcode=COALESCE(excluded.postcode, sessions.postcode), '
            '  lat=COALESCE(excluded.lat, sessions.lat), '
            '  lng=COALESCE(excluded.lng, sessions.lng), '
            '  notes=COALESCE(excluded.notes, sessions.notes)',
            (date, len(projects), sess.get('avg', 0), sess.get('mx', 0),
             meta.get('recorder_name') or None,
             meta.get('location_label') or None,
             meta.get('postcode') or None,
             meta.get('lat') or None,
             meta.get('lng') or None,
             meta.get('notes') or None)
        )
        sess_id = conn.execute('SELECT id FROM sessions WHERE date=?', (date,)).fetchone()['id']
        for i, proj in enumerate(projects, 1):
            conn.execute(
                'INSERT INTO runs '
                '  (session_id, run_number, start_time, n_samples, step, '
                '   avg_laeq, min_laeq, max_laeq, max_lcpeak, laeq_json, lcpeak_json) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?) '
                'ON CONFLICT(session_id, run_number) DO UPDATE SET '
                '  start_time=excluded.start_time, n_samples=excluded.n_samples, '
                '  step=excluded.step, avg_laeq=excluded.avg_laeq, '
                '  min_laeq=excluded.min_laeq, max_laeq=excluded.max_laeq, '
                '  max_lcpeak=excluded.max_lcpeak, '
                '  laeq_json=excluded.laeq_json, lcpeak_json=excluded.lcpeak_json',
                (sess_id, i, proj['start'], proj['n'], proj.get('step', 1),
                 proj['avg'], proj['mn'], proj['mx'], proj['pmx'],
                 json.dumps(proj['laeq']), json.dumps(proj['lcpeak']))
            )
        imported += 1
    conn.commit()
    conn.close()
    return imported


def _wx(row):
    if row['wind_speed'] is None:
        return None
    return {'ws': row['wind_speed'], 'wd': row['wind_dir'],
            'tn': row['temp_min'],   'tx': row['temp_max'], 'pr': row['precip']}


def get_all_sessions_json():
    conn = get_db()
    sessions = conn.execute(
        'SELECT s.*, w.wind_speed, w.wind_dir, w.temp_min, w.temp_max, w.precip '
        'FROM sessions s LEFT JOIN weather w ON s.date=w.date ORDER BY s.date'
    ).fetchall()
    result = []
    for sess in sessions:
        runs = conn.execute(
            'SELECT * FROM runs WHERE session_id=? ORDER BY run_number', (sess['id'],)
        ).fetchall()
        result.append({
            'd':     sess['date'],
            'avg':   sess['avg_laeq'],
            'mx':    sess['max_laeq'],
            'name':  sess['recorder_name'],
            'loc':   sess['location_label'],
            'post':  sess['postcode'],
            'lat':   sess['lat'],
            'lng':   sess['lng'],
            'notes': sess['notes'],
            'wx':    _wx(sess),
            'projects': [{
                'start':   r['start_time'],
                'n':       r['n_samples'],
                'step':    r['step'],
                'avg':     r['avg_laeq'],
                'mn':      r['min_laeq'],
                'mx':      r['max_laeq'],
                'pmx':     r['max_lcpeak'],
                'laeq':    json.loads(r['laeq_json']),
                'lcpeak':  json.loads(r['lcpeak_json']),
            } for r in runs],
        })
    conn.close()
    return {'sessions': result}


def save_weather(date, w):
    conn = get_db()
    conn.execute(
        'INSERT INTO weather (date, wind_speed, wind_dir, temp_min, temp_max, precip, hourly_json) '
        'VALUES (?,?,?,?,?,?,?) '
        'ON CONFLICT(date) DO UPDATE SET '
        '  wind_speed=excluded.wind_speed, wind_dir=excluded.wind_dir, '
        '  temp_min=excluded.temp_min,     temp_max=excluded.temp_max, '
        '  precip=excluded.precip,         hourly_json=excluded.hourly_json',
        (date, w.get('wind_speed'), w.get('wind_dir'),
         w.get('temp_min'), w.get('temp_max'),
         w.get('precip'), w.get('hourly_json'))
    )
    conn.commit()
    conn.close()


def get_weather(date):
    conn = get_db()
    row = conn.execute('SELECT * FROM weather WHERE date=?', (date,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_existing_dates():
    """Return a set of session dates already in the database."""
    conn = get_db()
    rows = conn.execute('SELECT date FROM sessions').fetchall()
    conn.close()
    return {r['date'] for r in rows}


def get_existing_run_starts():
    """Return dict mapping date -> set of start_times already in the database."""
    conn = get_db()
    rows = conn.execute(
        'SELECT s.date, r.start_time FROM runs r JOIN sessions s ON r.session_id = s.id'
    ).fetchall()
    conn.close()
    result = {}
    for row in rows:
        result.setdefault(row['date'], set()).add(row['start_time'])
    return result


def get_report_templates():
    conn = get_db()
    rows = conn.execute('SELECT * FROM report_templates ORDER BY is_default DESC, id').fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_report_template(tid):
    conn = get_db()
    row = conn.execute('SELECT * FROM report_templates WHERE id=?', (tid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def save_report_template(name, description, prompt, is_default=0):
    conn = get_db()
    if is_default:
        conn.execute('UPDATE report_templates SET is_default=0')
    cur = conn.execute(
        'INSERT INTO report_templates (name, description, prompt, is_default) VALUES (?,?,?,?)',
        (name, description, prompt, 1 if is_default else 0)
    )
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


def update_report_template(tid, name, description, prompt, is_default=None):
    conn = get_db()
    if is_default:
        conn.execute('UPDATE report_templates SET is_default=0')
    fields = 'name=?, description=?, prompt=?, updated_at=datetime(\'now\')'
    params = [name, description, prompt]
    if is_default is not None:
        fields += ', is_default=?'
        params.append(1 if is_default else 0)
    params.append(tid)
    conn.execute(f'UPDATE report_templates SET {fields} WHERE id=?', params)
    conn.commit()
    conn.close()


def delete_report_template(tid):
    conn = get_db()
    conn.execute('DELETE FROM report_templates WHERE id=?', (tid,))
    conn.commit()
    conn.close()


def save_generated_report(session_date, template_id, template_name, model,
                          thinking_level, sections_json, input_tokens, output_tokens, cost_usd,
                          run_number=None, run_label=None):
    conn = get_db()
    cur = conn.execute(
        'INSERT INTO generated_reports '
        '  (session_date, run_number, run_label, template_id, template_name, model, thinking_level, '
        '   sections_json, input_tokens, output_tokens, cost_usd) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        (session_date, run_number, run_label, template_id, template_name, model, thinking_level,
         sections_json, input_tokens, output_tokens, cost_usd)
    )
    rid = cur.lastrowid
    conn.commit()
    conn.close()
    return rid


def get_generated_reports():
    conn = get_db()
    rows = conn.execute(
        'SELECT * FROM generated_reports ORDER BY created_at DESC'
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_generated_report(rid):
    conn = get_db()
    row = conn.execute('SELECT * FROM generated_reports WHERE id=?', (rid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_generated_report(rid):
    conn = get_db()
    conn.execute('DELETE FROM generated_reports WHERE id=?', (rid,))
    conn.commit()
    conn.close()


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


def get_sessions_since(since):
    """Return sessions imported/updated after `since` (ISO timestamp), in sync format."""
    conn = get_db()
    sessions = conn.execute(
        'SELECT s.*, w.wind_speed, w.wind_dir, w.temp_min, w.temp_max, w.precip '
        'FROM sessions s LEFT JOIN weather w ON s.date=w.date '
        'WHERE s.imported_at > ? ORDER BY s.date', (since,)
    ).fetchall()
    result = []
    for sess in sessions:
        runs = conn.execute(
            'SELECT * FROM runs WHERE session_id=? ORDER BY run_number', (sess['id'],)
        ).fetchall()
        result.append({
            'd':     sess['date'],
            'avg':   sess['avg_laeq'],
            'mx':    sess['max_laeq'],
            'name':  sess['recorder_name'],
            'loc':   sess['location_label'],
            'post':  sess['postcode'],
            'lat':   sess['lat'],
            'lng':   sess['lng'],
            'notes': sess['notes'],
            'wx':    _wx(sess),
            'projects': [{
                'start':   r['start_time'],
                'n':       r['n_samples'],
                'step':    r['step'],
                'avg':     r['avg_laeq'],
                'mn':      r['min_laeq'],
                'mx':      r['max_laeq'],
                'pmx':     r['max_lcpeak'],
                'laeq':    json.loads(r['laeq_json']),
                'lcpeak':  json.loads(r['lcpeak_json']),
            } for r in runs],
        })
    conn.close()
    return result


def get_all_sessions_list():
    """Return all sessions as dicts for the manage page (no run data)."""
    conn = get_db()
    rows = conn.execute(
        'SELECT date, run_count, avg_laeq, max_laeq, '
        '       recorder_name, location_label, postcode, lat, lng, notes '
        'FROM sessions ORDER BY date DESC'
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_session_metadata(date, recorder_name, location_label, postcode, lat, lng, notes=None):
    conn = get_db()
    conn.execute(
        'UPDATE sessions SET recorder_name=?, location_label=?, postcode=?, lat=?, lng=?, notes=? '
        'WHERE date=?',
        (recorder_name or None, location_label or None,
         postcode or None, lat, lng, notes or None, date)
    )
    conn.commit()
    conn.close()


def delete_session(date):
    conn = get_db()
    conn.execute('DELETE FROM sessions WHERE date=?', (date,))
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
