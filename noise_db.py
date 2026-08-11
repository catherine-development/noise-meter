import os
import json
import sqlite3

DB_PATH = os.environ.get('NOISE_DB_PATH', '/home/flightdata/noise-meter/noise.db')


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS sessions (
            id          INTEGER PRIMARY KEY,
            date        TEXT UNIQUE NOT NULL,
            run_count   INTEGER,
            avg_laeq    REAL,
            max_laeq    REAL,
            imported_at TEXT DEFAULT (datetime('now'))
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
        CREATE INDEX IF NOT EXISTS idx_sessions_date ON sessions(date);
        CREATE INDEX IF NOT EXISTS idx_runs_session  ON runs(session_id);
    ''')
    conn.commit()
    conn.close()


def import_sessions(sessions_data):
    conn = get_db()
    imported = 0
    for sess in sessions_data:
        date = sess['d']
        projects = sess.get('projects', [])
        conn.execute(
            'INSERT INTO sessions (date, run_count, avg_laeq, max_laeq) VALUES (?,?,?,?) '
            'ON CONFLICT(date) DO UPDATE SET '
            '  run_count=excluded.run_count, '
            '  avg_laeq=excluded.avg_laeq, '
            '  max_laeq=excluded.max_laeq',
            (date, len(projects), sess.get('avg', 0), sess.get('mx', 0))
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


def get_all_sessions_json():
    conn = get_db()
    sessions = conn.execute('SELECT * FROM sessions ORDER BY date').fetchall()
    result = []
    for sess in sessions:
        runs = conn.execute(
            'SELECT * FROM runs WHERE session_id=? ORDER BY run_number', (sess['id'],)
        ).fetchall()
        result.append({
            'd': sess['date'],
            'avg': sess['avg_laeq'],
            'mx': sess['max_laeq'],
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


def get_import_log(limit=20):
    conn = get_db()
    rows = conn.execute(
        'SELECT date, run_count, avg_laeq, max_laeq, imported_at '
        'FROM sessions ORDER BY imported_at DESC LIMIT ?', (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
