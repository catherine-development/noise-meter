import math
import os
import json
import sqlite3

from nor140_format import SPECTRAL_TABLES

DB_PATH = os.environ.get('NOISE_DB_PATH', '/home/flightdata/noise-meter/noise.db')


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def _migrate(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS app_settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
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
    # Tombstones for deleted sessions. A missed upsert self-heals on the next
    # full sync, but a missed delete does not — the peer would keep the session
    # forever — so deletions are recorded and replayed rather than only pushed.
    # deleted_at uses datetime('now') (UTC) to match sessions.imported_at, which
    # apply_full_sync() compares it against.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS deleted_sessions (
            date       TEXT PRIMARY KEY,
            deleted_at TEXT DEFAULT (datetime('now'))
        )
    ''')
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
    run_cols = {row[1] for row in conn.execute('PRAGMA table_info(runs)').fetchall()}
    _run_migrations = [
        ('lafmax_json',  'TEXT'), ('laimax_json', 'TEXT'), ('max_laimax', 'REAL'),
        ('location_tag', 'TEXT'), ('source_file', 'TEXT'),
        # GLOB-derived scalar metrics (all 18 spectral tables → A/C broadband)
        ('lceq',   'REAL'), ('lae',    'REAL'), ('lce',    'REAL'),
        ('lafmax', 'REAL'), ('lcfmax', 'REAL'), ('lafmin', 'REAL'), ('lcfmin', 'REAL'),
        ('lasmax', 'REAL'), ('lcsmax', 'REAL'), ('lasmin', 'REAL'), ('lcsmin', 'REAL'),
        ('laieq',  'REAL'), ('lcieq',  'REAL'), ('laimax', 'REAL'), ('lcimax', 'REAL'),
        ('laimin', 'REAL'), ('lcimin', 'REAL'), ('laie',   'REAL'), ('lcie',   'REAL'),
        ('la_l01', 'REAL'), ('la_l1',  'REAL'), ('la_l5',  'REAL'),
        ('la_l10', 'REAL'), ('la_l50', 'REAL'), ('la_l90', 'REAL'),
        ('la_l95', 'REAL'), ('la_l99', 'REAL'),
        ('lapeak', 'REAL'), ('lcpeak', 'REAL'),
        ('lc_l01', 'REAL'), ('lc_l1',  'REAL'), ('lc_l5',  'REAL'),
        ('lc_l10', 'REAL'), ('lc_l50', 'REAL'), ('lc_l90', 'REAL'),
        ('lc_l95', 'REAL'), ('lc_l99', 'REAL'),
        # 1/3-octave spectral arrays (JSON, 36 floats each; NULL for 1069-byte GLOBs)
        ('spec_lfeq',    'TEXT'), ('spec_lffmax',  'TEXT'), ('spec_lffmin',  'TEXT'),
        ('spec_lfe',     'TEXT'), ('spec_lfsmax',  'TEXT'), ('spec_lfsmin',  'TEXT'),
        ('spec_lfieq',   'TEXT'), ('spec_lfimax',  'TEXT'), ('spec_lfimin',  'TEXT'),
        ('spec_lfie',    'TEXT'),
        ('spec_lff_l01', 'TEXT'), ('spec_lff_l1',  'TEXT'), ('spec_lff_l5',  'TEXT'),
        ('spec_lff_l10', 'TEXT'), ('spec_lff_l50', 'TEXT'), ('spec_lff_l90', 'TEXT'),
        ('spec_lff_l95', 'TEXT'), ('spec_lff_l99', 'TEXT'),
        # Full 1-second PROF time series (JSON arrays; NULL until backfilled)
        ('prof_lafspl_json', 'TEXT'), ('prof_laeq_json',   'TEXT'),
        ('prof_lafmax_json', 'TEXT'), ('prof_lae_json',    'TEXT'),
        ('prof_lapeak_json', 'TEXT'),
        # Effective duration as stored by the meter (GLOB 0x03bd). Not always
        # equal to n_samples: a run stopped mid-period writes a final partial
        # record, so the count can exceed the elapsed time.
        ('duration_s', 'INTEGER'),
        # Measurement range in dB (GLOB 0x004a); varies per measurement.
        ('full_scale', 'INTEGER'),
        # Stored measurement end time (GLOB 0x22, BCD). Not derivable — see
        # nor140_format.END_TIME_OFFSET.
        ('end_time', 'TEXT'),
    ]
    for col, typ in _run_migrations:
        if col not in run_cols:
            conn.execute(f'ALTER TABLE runs ADD COLUMN {col} {typ}')
    gr_cols = {row[1] for row in conn.execute('PRAGMA table_info(generated_reports)').fetchall()}
    for col, typ in [('run_number', 'INTEGER'), ('run_label', 'TEXT')]:
        if col not in gr_cols:
            conn.execute(f'ALTER TABLE generated_reports ADD COLUMN {col} {typ}')
    if not conn.execute('SELECT COUNT(*) FROM report_templates').fetchone()[0]:
        # Imported here rather than at module scope: reports_db imports get_db
        # from this module, so a top-level import would be circular.
        from reports_db import DEFAULT_TEMPLATES
        for t in DEFAULT_TEMPLATES:
            conn.execute(
                'INSERT INTO report_templates (name, description, prompt, is_default) VALUES (?,?,?,?)',
                (t['name'], t['description'], t['prompt'], t['is_default'])
            )
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS assessments (
            id          INTEGER PRIMARY KEY,
            name        TEXT NOT NULL,
            purpose     TEXT,
            standard    TEXT DEFAULT 'noise_act',
            address     TEXT,
            postcode    TEXT,
            lat         REAL,
            lng         REAL,
            client_ref  TEXT,
            notes       TEXT,
            created_at  TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS assessment_locations (
            id              INTEGER PRIMARY KEY,
            assessment_id   INTEGER NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
            label           TEXT NOT NULL,
            description     TEXT,
            lat             REAL,
            lng             REAL,
            sort_order      INTEGER DEFAULT 0,
            notes           TEXT
        );
        CREATE TABLE IF NOT EXISTS assessment_runs (
            id              INTEGER PRIMARY KEY,
            assessment_id   INTEGER NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
            location_id     INTEGER REFERENCES assessment_locations(id) ON DELETE SET NULL,
            session_date    TEXT NOT NULL,
            -- Positional index of the run within its session. Kept for peer
            -- compatibility and as a fallback, but it is NOT a stable identity:
            -- recovering a previously unparseable run shifts every later number
            -- on that date, silently re-pointing assessment links. source_file
            -- is the stable key and is preferred wherever both are present.
            run_number      INTEGER,
            source_file     TEXT,
            conditions      TEXT,
            notes           TEXT
        );
        -- The stable identity. Partial, because legacy rows may still carry a
        -- NULL source_file and must not collide with one another.
        CREATE UNIQUE INDEX IF NOT EXISTS idx_assessment_runs_stable
            ON assessment_runs(assessment_id, session_date, source_file)
            WHERE source_file IS NOT NULL;
    ''')

    ar_sql = (conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='assessment_runs'"
    ).fetchone() or [''])[0] or ''
    if 'UNIQUE(assessment_id, session_date, run_number)' in ar_sql:
        # Rebuild to drop the positional UNIQUE. Reads prefer source_file, so
        # leaving run_number as the arbiter of writes let an upsert rebind a
        # link to a different measurement once numbering shifted.
        conn.execute('PRAGMA foreign_keys=off')
        conn.executescript('''
            CREATE TABLE assessment_runs_new (
                id              INTEGER PRIMARY KEY,
                assessment_id   INTEGER NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
                location_id     INTEGER REFERENCES assessment_locations(id) ON DELETE SET NULL,
                session_date    TEXT NOT NULL,
                run_number      INTEGER,
                source_file     TEXT,
                conditions      TEXT,
                notes           TEXT
            );
            INSERT INTO assessment_runs_new
                (id, assessment_id, location_id, session_date, run_number,
                 source_file, conditions, notes)
            SELECT id, assessment_id, location_id, session_date, run_number,
                   source_file, conditions, notes FROM assessment_runs;
            DROP TABLE assessment_runs;
            ALTER TABLE assessment_runs_new RENAME TO assessment_runs;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_assessment_runs_stable
                ON assessment_runs(assessment_id, session_date, source_file)
                WHERE source_file IS NOT NULL;
        ''')
        conn.execute('PRAGMA foreign_keys=on')

    ar_cols = {row[1] for row in conn.execute('PRAGMA table_info(assessment_runs)').fetchall()}
    if 'source_file' not in ar_cols:
        conn.execute('ALTER TABLE assessment_runs ADD COLUMN source_file TEXT')
        # Backfill through the run_number join, which is still correct at this
        # point: the numbers only go stale once a re-import shifts them, and
        # this runs before any such import can.
        conn.execute('''
            UPDATE assessment_runs SET source_file = (
                SELECT r.source_file FROM runs r
                JOIN sessions s ON s.id = r.session_id
                WHERE s.date = assessment_runs.session_date
                  AND r.run_number = assessment_runs.run_number
            ) WHERE source_file IS NULL
        ''')

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
    """Import sessions. Per-session metadata/weather (as sent by
    get_sessions_since() for peer sync) takes precedence when present;
    the `metadata` kwarg is the fallback used by the single-session manual
    upload form, which has no per-session metadata of its own."""
    meta = metadata or {}
    conn = get_db()
    imported = 0
    for sess in sessions_data:
        date = sess['d']
        projects = sess.get('projects', [])
        recorder_name  = sess.get('name', meta.get('recorder_name')) or None
        location_label = sess.get('loc',  meta.get('location_label')) or None
        postcode       = sess.get('post', meta.get('postcode')) or None
        lat            = sess.get('lat',  meta.get('lat'))
        lng            = sess.get('lng',  meta.get('lng'))
        notes          = sess.get('notes', meta.get('notes')) or None
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
            '  notes=COALESCE(excluded.notes, sessions.notes), '
            '  imported_at=datetime(\'now\')',
            (date, len(projects), sess.get('avg', 0), sess.get('mx', 0),
             recorder_name, location_label, postcode, lat, lng, notes)
        )
        wx = sess.get('wx')
        if wx:
            conn.execute(
                'INSERT INTO weather (date, wind_speed, wind_dir, temp_min, temp_max, precip, hourly_json) '
                'VALUES (?,?,?,?,?,?,?) '
                'ON CONFLICT(date) DO UPDATE SET '
                '  wind_speed=excluded.wind_speed, wind_dir=excluded.wind_dir, '
                '  temp_min=excluded.temp_min,     temp_max=excluded.temp_max, '
                '  precip=excluded.precip',
                (date, wx.get('ws'), wx.get('wd'), wx.get('tn'), wx.get('tx'), wx.get('pr'), None)
            )
        # Importing a date supersedes any earlier deletion of it, so drop the
        # tombstone — otherwise a legitimate SD-card re-import would be deleted
        # again the next time a peer replayed its full sync payload.
        conn.execute('DELETE FROM deleted_sessions WHERE date=?', (date,))
        sess_id = conn.execute('SELECT id FROM sessions WHERE date=?', (date,)).fetchone()['id']
        for i, proj in enumerate(projects, 1):
            _g = proj.get
            conn.execute(
                'INSERT INTO runs '
                '  (session_id, run_number, start_time, n_samples, step, '
                '   avg_laeq, min_laeq, max_laeq, max_lcpeak, max_laimax, '
                '   laeq_json, lafmax_json, laimax_json, lcpeak_json, source_file, '
                '   lceq, lae, lce, lafmax, lcfmax, lafmin, lcfmin, '
                '   lasmax, lcsmax, lasmin, lcsmin, laieq, lcieq, '
                '   laimax, lcimax, laimin, lcimin, laie, lcie, '
                '   la_l01, la_l1, la_l5, la_l10, la_l50, la_l90, la_l95, la_l99, '
                '   lapeak, lcpeak, '
                '   lc_l01, lc_l1, lc_l5, lc_l10, lc_l50, lc_l90, lc_l95, lc_l99, '
                '   spec_lfeq, spec_lffmax, spec_lffmin, spec_lfe, '
                '   spec_lfsmax, spec_lfsmin, spec_lfieq, spec_lfimax, spec_lfimin, spec_lfie, '
                '   spec_lff_l01, spec_lff_l1, spec_lff_l5, spec_lff_l10, '
                '   spec_lff_l50, spec_lff_l90, spec_lff_l95, spec_lff_l99, '
                '   prof_lafspl_json, prof_laeq_json, prof_lafmax_json, '
                '   prof_lae_json, prof_lapeak_json, duration_s, full_scale, end_time) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
                'ON CONFLICT(session_id, run_number) DO UPDATE SET '
                '  start_time=excluded.start_time, n_samples=excluded.n_samples, '
                '  step=excluded.step, avg_laeq=excluded.avg_laeq, '
                '  min_laeq=excluded.min_laeq, max_laeq=excluded.max_laeq, '
                '  max_lcpeak=excluded.max_lcpeak, max_laimax=excluded.max_laimax, '
                '  laeq_json=excluded.laeq_json, lafmax_json=excluded.lafmax_json, '
                '  laimax_json=excluded.laimax_json, lcpeak_json=excluded.lcpeak_json, '
                '  source_file=excluded.source_file, '
                # GLOB-derived scalar columns: COALESCE so that a push from older
                # code (which sends NULL) never overwrites an already-backfilled value.
                '  lceq=COALESCE(excluded.lceq,runs.lceq), '
                '  lae=COALESCE(excluded.lae,runs.lae), '
                '  lce=COALESCE(excluded.lce,runs.lce), '
                '  lafmax=COALESCE(excluded.lafmax,runs.lafmax), '
                '  lcfmax=COALESCE(excluded.lcfmax,runs.lcfmax), '
                '  lafmin=COALESCE(excluded.lafmin,runs.lafmin), '
                '  lcfmin=COALESCE(excluded.lcfmin,runs.lcfmin), '
                '  lasmax=COALESCE(excluded.lasmax,runs.lasmax), '
                '  lcsmax=COALESCE(excluded.lcsmax,runs.lcsmax), '
                '  lasmin=COALESCE(excluded.lasmin,runs.lasmin), '
                '  lcsmin=COALESCE(excluded.lcsmin,runs.lcsmin), '
                '  laieq=COALESCE(excluded.laieq,runs.laieq), '
                '  lcieq=COALESCE(excluded.lcieq,runs.lcieq), '
                '  laimax=COALESCE(excluded.laimax,runs.laimax), '
                '  lcimax=COALESCE(excluded.lcimax,runs.lcimax), '
                '  laimin=COALESCE(excluded.laimin,runs.laimin), '
                '  lcimin=COALESCE(excluded.lcimin,runs.lcimin), '
                '  laie=COALESCE(excluded.laie,runs.laie), '
                '  lcie=COALESCE(excluded.lcie,runs.lcie), '
                '  la_l01=COALESCE(excluded.la_l01,runs.la_l01), '
                '  la_l1=COALESCE(excluded.la_l1,runs.la_l1), '
                '  la_l5=COALESCE(excluded.la_l5,runs.la_l5), '
                '  la_l10=COALESCE(excluded.la_l10,runs.la_l10), '
                '  la_l50=COALESCE(excluded.la_l50,runs.la_l50), '
                '  la_l90=COALESCE(excluded.la_l90,runs.la_l90), '
                '  la_l95=COALESCE(excluded.la_l95,runs.la_l95), '
                '  la_l99=COALESCE(excluded.la_l99,runs.la_l99), '
                '  lapeak=COALESCE(excluded.lapeak,runs.lapeak), '
                '  lcpeak=COALESCE(excluded.lcpeak,runs.lcpeak), '
                '  lc_l01=COALESCE(excluded.lc_l01,runs.lc_l01), '
                '  lc_l1=COALESCE(excluded.lc_l1,runs.lc_l1), '
                '  lc_l5=COALESCE(excluded.lc_l5,runs.lc_l5), '
                '  lc_l10=COALESCE(excluded.lc_l10,runs.lc_l10), '
                '  lc_l50=COALESCE(excluded.lc_l50,runs.lc_l50), '
                '  lc_l90=COALESCE(excluded.lc_l90,runs.lc_l90), '
                '  lc_l95=COALESCE(excluded.lc_l95,runs.lc_l95), '
                '  lc_l99=COALESCE(excluded.lc_l99,runs.lc_l99), '
                '  spec_lfeq=COALESCE(excluded.spec_lfeq,runs.spec_lfeq), '
                '  spec_lffmax=COALESCE(excluded.spec_lffmax,runs.spec_lffmax), '
                '  spec_lffmin=COALESCE(excluded.spec_lffmin,runs.spec_lffmin), '
                '  spec_lfe=COALESCE(excluded.spec_lfe,runs.spec_lfe), '
                '  spec_lfsmax=COALESCE(excluded.spec_lfsmax,runs.spec_lfsmax), '
                '  spec_lfsmin=COALESCE(excluded.spec_lfsmin,runs.spec_lfsmin), '
                '  spec_lfieq=COALESCE(excluded.spec_lfieq,runs.spec_lfieq), '
                '  spec_lfimax=COALESCE(excluded.spec_lfimax,runs.spec_lfimax), '
                '  spec_lfimin=COALESCE(excluded.spec_lfimin,runs.spec_lfimin), '
                '  spec_lfie=COALESCE(excluded.spec_lfie,runs.spec_lfie), '
                '  spec_lff_l01=COALESCE(excluded.spec_lff_l01,runs.spec_lff_l01), '
                '  spec_lff_l1=COALESCE(excluded.spec_lff_l1,runs.spec_lff_l1), '
                '  spec_lff_l5=COALESCE(excluded.spec_lff_l5,runs.spec_lff_l5), '
                '  spec_lff_l10=COALESCE(excluded.spec_lff_l10,runs.spec_lff_l10), '
                '  spec_lff_l50=COALESCE(excluded.spec_lff_l50,runs.spec_lff_l50), '
                '  spec_lff_l90=COALESCE(excluded.spec_lff_l90,runs.spec_lff_l90), '
                '  spec_lff_l95=COALESCE(excluded.spec_lff_l95,runs.spec_lff_l95), '
                '  spec_lff_l99=COALESCE(excluded.spec_lff_l99,runs.spec_lff_l99), '
                '  prof_lafspl_json=COALESCE(excluded.prof_lafspl_json,runs.prof_lafspl_json), '
                '  prof_laeq_json=COALESCE(excluded.prof_laeq_json,runs.prof_laeq_json), '
                '  prof_lafmax_json=COALESCE(excluded.prof_lafmax_json,runs.prof_lafmax_json), '
                '  prof_lae_json=COALESCE(excluded.prof_lae_json,runs.prof_lae_json), '
                '  prof_lapeak_json=COALESCE(excluded.prof_lapeak_json,runs.prof_lapeak_json), '
                '  duration_s=COALESCE(excluded.duration_s,runs.duration_s), '
                '  full_scale=COALESCE(excluded.full_scale,runs.full_scale), '
                '  end_time=COALESCE(excluded.end_time,runs.end_time)',
                (sess_id, i, proj['start'], proj['n'], proj.get('step', 1),
                 proj['avg'], proj['mn'], proj['mx'], proj['pmx'], _g('pmxi'),
                 json.dumps(_g('laeq_profile') or []),
                 json.dumps(_g('lafmax_profile')) if _g('lafmax_profile') else None,
                 json.dumps(_g('laimax_profile')) if _g('laimax_profile') else None,
                 json.dumps(_g('lcpeak_profile') or []),
                 _g('source_file'),
                 _g('lceq'), _g('lae'), _g('lce'),
                 _g('lafmax'), _g('lcfmax'), _g('lafmin'), _g('lcfmin'),
                 _g('lasmax'), _g('lcsmax'), _g('lasmin'), _g('lcsmin'),
                 _g('laieq'), _g('lcieq'), _g('laimax'), _g('lcimax'),
                 _g('laimin'), _g('lcimin'), _g('laie'), _g('lcie'),
                 _g('la_l01'), _g('la_l1'), _g('la_l5'),
                 _g('la_l10'), _g('la_l50'), _g('la_l90'), _g('la_l95'), _g('la_l99'),
                 _g('lapeak'), _g('lcpeak'),
                 _g('lc_l01'), _g('lc_l1'), _g('lc_l5'),
                 _g('lc_l10'), _g('lc_l50'), _g('lc_l90'), _g('lc_l95'), _g('lc_l99'),
                 json.dumps(_g('spec_lfeq'))    if _g('spec_lfeq')    else None,
                 json.dumps(_g('spec_lffmax'))  if _g('spec_lffmax')  else None,
                 json.dumps(_g('spec_lffmin'))  if _g('spec_lffmin')  else None,
                 json.dumps(_g('spec_lfe'))     if _g('spec_lfe')     else None,
                 json.dumps(_g('spec_lfsmax'))  if _g('spec_lfsmax')  else None,
                 json.dumps(_g('spec_lfsmin'))  if _g('spec_lfsmin')  else None,
                 json.dumps(_g('spec_lfieq'))   if _g('spec_lfieq')   else None,
                 json.dumps(_g('spec_lfimax'))  if _g('spec_lfimax')  else None,
                 json.dumps(_g('spec_lfimin'))  if _g('spec_lfimin')  else None,
                 json.dumps(_g('spec_lfie'))    if _g('spec_lfie')    else None,
                 json.dumps(_g('spec_lff_l01')) if _g('spec_lff_l01') else None,
                 json.dumps(_g('spec_lff_l1'))  if _g('spec_lff_l1')  else None,
                 json.dumps(_g('spec_lff_l5'))  if _g('spec_lff_l5')  else None,
                 json.dumps(_g('spec_lff_l10')) if _g('spec_lff_l10') else None,
                 json.dumps(_g('spec_lff_l50')) if _g('spec_lff_l50') else None,
                 json.dumps(_g('spec_lff_l90')) if _g('spec_lff_l90') else None,
                 json.dumps(_g('spec_lff_l95')) if _g('spec_lff_l95') else None,
                 json.dumps(_g('spec_lff_l99')) if _g('spec_lff_l99') else None,
                 json.dumps(_g('prof_lafspl_json')) if _g('prof_lafspl_json') else None,
                 json.dumps(_g('prof_laeq_json'))   if _g('prof_laeq_json')   else None,
                 json.dumps(_g('prof_lafmax_json'))  if _g('prof_lafmax_json') else None,
                 json.dumps(_g('prof_lae_json'))    if _g('prof_lae_json')    else None,
                 json.dumps(_g('prof_lapeak_json')) if _g('prof_lapeak_json') else None,
                 _g('duration_s'), _g('full_scale'), _g('end_time'))
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


_PROF_COLS = [
    'prof_lafspl_json', 'prof_laeq_json', 'prof_lafmax_json',
    'prof_lae_json', 'prof_lapeak_json',
]


def _run_to_dict(r, full=False):
    """Build the per-run/project dict shared by get_all_sessions_json(),
    get_sessions_since(), and get_sessions_export_format().

    full=True adds the spec_*/prof_* JSON arrays needed to regenerate a
    NOR140 xlsx export without local SD-card backfill — used for peer sync
    and the NOR140 import/export format, but not the (lightweight) session
    browser payload, which already has everything it needs for charts via
    the *_profile fields.
    """
    d = {
        'run_number': r['run_number'],
        'source_file': r['source_file'],
        'start':   r['start_time'],
        # The meter's own end time, or nothing. There is deliberately no
        # arithmetic fallback: start + n_samples is wrong for any run that was
        # paused or stopped mid-period, and a plausible-looking wrong time in
        # evidence is worse than a blank. Both fields are exported because peer
        # sync reads 'end_time' while the templates read 'end'.
        'end':      r['end_time'],
        'end_time': r['end_time'],
        'n':       r['n_samples'],
        'step':    r['step'],
        'duration_s': r['duration_s'],
        'full_scale': r['full_scale'],
        'avg':     r['avg_laeq'],
        'mn':      r['min_laeq'],
        'mx':      r['max_laeq'],
        'pmx':     r['max_lcpeak'],
        'pmxi':    r['max_laimax'],
        'laeq_profile':   json.loads(r['laeq_json']) if r['laeq_json'] else [],
        'lafmax_profile': json.loads(r['lafmax_json']) if r['lafmax_json'] else None,
        'laimax_profile': json.loads(r['laimax_json']) if r['laimax_json'] else None,
        'lcpeak_profile': json.loads(r['lcpeak_json']) if r['lcpeak_json'] else [],
        'lceq':   r['lceq'],   'lae':    r['lae'],    'lce':    r['lce'],
        'lafmax': r['lafmax'], 'lcfmax': r['lcfmax'], 'lafmin': r['lafmin'], 'lcfmin': r['lcfmin'],
        'lasmax': r['lasmax'], 'lcsmax': r['lcsmax'], 'lasmin': r['lasmin'], 'lcsmin': r['lcsmin'],
        'laieq':  r['laieq'],  'lcieq':  r['lcieq'],
        'laimax': r['laimax'], 'lcimax': r['lcimax'], 'laimin': r['laimin'], 'lcimin': r['lcimin'],
        'laie':   r['laie'],   'lcie':   r['lcie'],
        'lapeak': r['lapeak'], 'lcpeak': r['lcpeak'],
        'la_l01': r['la_l01'], 'la_l1':  r['la_l1'],  'la_l5':  r['la_l5'],
        'la_l10': r['la_l10'], 'la_l50': r['la_l50'], 'la_l90': r['la_l90'],
        'la_l95': r['la_l95'], 'la_l99': r['la_l99'],
        'lc_l01': r['lc_l01'], 'lc_l1':  r['lc_l1'],  'lc_l5':  r['lc_l5'],
        'lc_l10': r['lc_l10'], 'lc_l50': r['lc_l50'], 'lc_l90': r['lc_l90'],
        'lc_l95': r['lc_l95'], 'lc_l99': r['lc_l99'],
    }
    if full:
        for col, _ in SPECTRAL_TABLES:
            d[col] = json.loads(r[col]) if r[col] else None
        for col in _PROF_COLS:
            d[col] = json.loads(r[col]) if r[col] else None
    return d


def get_all_sessions_json():
    conn = get_db()
    sessions = conn.execute(
        'SELECT s.*, w.wind_speed, w.wind_dir, w.temp_min, w.temp_max, w.precip '
        'FROM sessions s LEFT JOIN weather w ON s.date=w.date ORDER BY s.date DESC'
    ).fetchall()
    # Build session_date -> list of assessment names (one query, not N)
    sess_assess_rows = conn.execute(
        'SELECT DISTINCT ar.session_date, a.name '
        'FROM assessment_runs ar JOIN assessments a ON a.id=ar.assessment_id'
    ).fetchall()
    sess_assessments = {}
    for row in sess_assess_rows:
        sess_assessments.setdefault(row['session_date'], []).append(row['name'])
    result = []
    for sess in sessions:
        runs = conn.execute(
            'SELECT r.*, '
            # COALESCE alone turned the LEFT JOIN's NULL into '?', so every run
            # came back marked as assessed. The CASE keeps '?' for its intended
            # meaning — linked but with no location — and yields NULL otherwise.
            "  GROUP_CONCAT(CASE WHEN ar.id IS NOT NULL "
            "                    THEN COALESCE(al.label,'?') END, ',') AS assess_locs "
            'FROM runs r '
            'LEFT JOIN assessment_runs ar '
            '  ON ar.session_date=? AND ('
            '    ar.source_file=r.source_file'
            '    OR (ar.source_file IS NULL AND ar.run_number=r.run_number)) '
            'LEFT JOIN assessment_locations al ON al.id=ar.location_id '
            'WHERE r.session_id=? '
            'GROUP BY r.id ORDER BY r.run_number',
            (sess['date'], sess['id'])
        ).fetchall()
        result.append({
            'd':       sess['date'],
            'avg':     sess['avg_laeq'],
            'mx':      sess['max_laeq'],
            'name':    sess['recorder_name'],
            'loc':     sess['location_label'],
            'post':    sess['postcode'],
            'lat':     sess['lat'],
            'lng':     sess['lng'],
            'notes':   sess['notes'],
            'wx':      _wx(sess),
            'assmnts': sess_assessments.get(sess['date'], []),
            'projects': [{
                **_run_to_dict(r, full=False),
                'loc_tag': r['location_tag'],
                'assess':  r['assess_locs'],
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
            'projects': [_run_to_dict(r, full=True) for r in runs],
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


def get_full_run_row(date, run_number):
    """Return all columns for a single run (including spec_ and prof_ JSON columns).
    Adds 'session_date' so callers can build full datetimes from start_time."""
    conn = get_db()
    sess = conn.execute('SELECT id FROM sessions WHERE date=?', (date,)).fetchone()
    if not sess:
        conn.close()
        return None
    row = conn.execute(
        'SELECT * FROM runs WHERE session_id=? AND run_number=?',
        (sess['id'], run_number)
    ).fetchone()
    conn.close()
    if row is None:
        return None
    result = dict(row)
    result['session_date'] = date
    return result


def get_session_prof_lafspl(date, run_numbers=None):
    """Return the pooled list of 1-second LAFspl values across the given runs
    (or all runs, if run_numbers is None) of a session.

    Used for computing a true LA10/LA90 percentile of the combined measurement
    distribution when reporting across multiple runs — pooling actual samples
    is correct where averaging each run's own percentile is not (percentiles
    don't compose linearly across sub-samples of different sizes).

    Uses PROF field 0 (LAFspl, Fast-time-weighted SPL), not field 1 (LAeq,1s
    energy average) — LA10/LA90 statistical descriptors are, by convention and
    per the NOR140 handoff notes, derived from the Fast-weighted instantaneous
    level, not the per-second energy average. Note this still won't exactly
    reproduce the meter's own LA10/LA90 (computed from its internal sub-period
    detector state), only approximate it closely — same caveat the handoff
    notes make for LAFmin vs min(PROF field 0).
    """
    conn = get_db()
    sess = conn.execute('SELECT id FROM sessions WHERE date=?', (date,)).fetchone()
    if not sess:
        conn.close()
        return []
    if run_numbers:
        ph = ','.join('?' * len(run_numbers))
        rows = conn.execute(
            f'SELECT prof_lafspl_json FROM runs WHERE session_id=? AND run_number IN ({ph})',
            (sess['id'], *run_numbers)
        ).fetchall()
    else:
        rows = conn.execute(
            'SELECT prof_lafspl_json FROM runs WHERE session_id=?', (sess['id'],)
        ).fetchall()
    conn.close()
    pooled = []
    for r in rows:
        if r['prof_lafspl_json']:
            pooled.extend(json.loads(r['prof_lafspl_json']))
    return pooled


def get_run_prof_laeq(date, run_number):
    """Return the full 1-second LAeq series for one run, or [] if not stored.

    The session browser payload only carries the downsampled chart profile, which
    holds the maximum of each window. Anything counting samples against a
    threshold must read the real series instead — counting the expanded chart
    profile overstated time above 85 dB by up to the downsample step.
    """
    conn = get_db()
    row = conn.execute(
        'SELECT r.prof_laeq_json FROM runs r JOIN sessions s ON r.session_id = s.id '
        'WHERE s.date = ? AND r.run_number = ?', (date, run_number)).fetchone()
    conn.close()
    if not row or not row['prof_laeq_json']:
        return []
    return json.loads(row['prof_laeq_json'])


def update_run_location_tag(date, run_number, tag):
    conn = get_db()
    conn.execute(
        'UPDATE runs SET location_tag=? '
        'WHERE session_id=(SELECT id FROM sessions WHERE date=?) AND run_number=?',
        (tag or None, date, run_number)
    )
    conn.commit()
    conn.close()


def delete_session(date):
    """Delete a session and its runs (cascaded via FK) plus its assessment
    assignments — assessment_runs.session_date is a plain text column, not a
    declared foreign key, so it can't cascade automatically.

    Records a tombstone so the deletion replicates to a peer that was offline
    when it happened. Weather is deliberately left in place: it is keyed by
    date, not by session, and is reference data rather than measurement data."""
    conn = get_db()
    conn.execute('DELETE FROM assessment_runs WHERE session_date=?', (date,))
    conn.execute('DELETE FROM sessions WHERE date=?', (date,))
    _record_tombstones(conn, [date])
    conn.commit()
    conn.close()


def _record_tombstones(conn, dates, deleted_at=None):
    """Mark session dates as deleted. Caller commits.

    deleted_at is supplied when replaying a peer's tombstone, so the original
    deletion time is preserved as it propagates; the MAX() keeps the newest
    time if the same date is deleted on both Pis."""
    for date in dates:
        if deleted_at is None:
            conn.execute(
                "INSERT INTO deleted_sessions (date) VALUES (?) "
                "ON CONFLICT(date) DO UPDATE SET deleted_at=datetime('now')", (date,))
        else:
            conn.execute(
                'INSERT INTO deleted_sessions (date, deleted_at) VALUES (?,?) '
                'ON CONFLICT(date) DO UPDATE SET '
                '  deleted_at=MAX(deleted_sessions.deleted_at, excluded.deleted_at)',
                (date, deleted_at))


def get_session_tombstones():
    """Return [{date, deleted_at}] for every deleted session."""
    conn = get_db()
    rows = conn.execute(
        'SELECT date, deleted_at FROM deleted_sessions ORDER BY date').fetchall()
    conn.close()
    return [dict(r) for r in rows]


def clear_sentinel_scalars():
    """Null out stored scalars holding the meter's -20 dB no-data marker.

    New imports drop it at the decode boundary (nor140_format.NO_DATA_DB), but
    rows written before that change still hold -20.0, which passes every
    "is not None" check on its way into a report. Returns {column: rows_cleared}.
    """
    cols = [
        'avg_laeq', 'min_laeq', 'max_laeq',
        'lceq', 'lae', 'lce', 'lafmax', 'lcfmax', 'lafmin', 'lcfmin',
        'lasmax', 'lcsmax', 'lasmin', 'lcsmin', 'laieq', 'lcieq',
        'laimax', 'lcimax', 'laimin', 'lcimin', 'laie', 'lcie',
        'lapeak', 'lcpeak',
        'la_l01', 'la_l1', 'la_l5', 'la_l10', 'la_l50', 'la_l90', 'la_l95', 'la_l99',
        'lc_l01', 'lc_l1', 'lc_l5', 'lc_l10', 'lc_l50', 'lc_l90', 'lc_l95', 'lc_l99',
    ]
    conn = get_db()
    present = {row[1] for row in conn.execute('PRAGMA table_info(runs)').fetchall()}
    cleared = {}
    for col in cols:
        if col not in present:
            continue
        n = conn.execute(
            f'SELECT COUNT(*) FROM runs WHERE {col} <= -19.99').fetchone()[0]
        if n:
            conn.execute(f'UPDATE runs SET {col}=NULL WHERE {col} <= -19.99')
            cleared[col] = n
    conn.commit()
    conn.close()
    return cleared


def audit_assessment_run_keys():
    """Report every assessment_run whose stable key is missing or ambiguous.

    Run before tightening the constraints: a link with no source_file, or one
    that no longer resolves to a run, cannot be migrated automatically and has
    to be looked at. Returns a dict of lists, empty when the table is clean.
    """
    conn = get_db()
    rows = [dict(r) for r in conn.execute('''
        SELECT ar.id, ar.assessment_id, ar.session_date, ar.run_number,
               ar.source_file, a.name AS assessment_name
        FROM assessment_runs ar
        LEFT JOIN assessments a ON a.id = ar.assessment_id
        ORDER BY ar.session_date, ar.run_number
    ''').fetchall()]

    report = {'null_source_file': [], 'unmatched': [], 'duplicate': []}
    seen = {}
    for r in rows:
        if not r['source_file']:
            report['null_source_file'].append(r)
            continue
        hit = conn.execute(
            'SELECT 1 FROM runs rn JOIN sessions se ON se.id = rn.session_id '
            'WHERE se.date=? AND rn.source_file=?',
            (r['session_date'], r['source_file'])).fetchone()
        if not hit:
            report['unmatched'].append(r)
        key = (r['assessment_id'], r['session_date'], r['source_file'])
        if key in seen:
            report['duplicate'].append(r)
        seen[key] = r['id']
    conn.close()
    return report


def recompute_session_aggregates(dates=None):
    """Rebuild sessions.avg_laeq / max_laeq / run_count from the runs they own.

    The backfill scripts correct run-level values (backfill_glob.py rewrites
    runs.avg_laeq with the GLOB-derived LAeq) but historically left the session
    row untouched, so a session could sit ~8 dB high — the old 0x0422 LAeq bug
    preserved at session level long after the runs were fixed.

    Deliberately uses the same formula as noise_parser.parse_zip(), an
    equal-weight energy average of the per-run LAeq, so that a backfill and a
    re-import of the same data produce identical session rows. (Reports compute
    a duration-weighted session LAeq separately, which is the more correct
    figure when runs differ in length; this column exists for the session list
    and CSV export.)

    Returns [(date, old_avg, new_avg)] for the sessions whose values changed.
    """
    conn = get_db()
    if dates:
        ph = ','.join('?' * len(dates))
        sess = conn.execute(
            f'SELECT id, date, avg_laeq, max_laeq, run_count FROM sessions '
            f'WHERE date IN ({ph})', tuple(dates)).fetchall()
    else:
        sess = conn.execute(
            'SELECT id, date, avg_laeq, max_laeq, run_count FROM sessions').fetchall()

    changed = []
    for s in sess:
        runs = conn.execute(
            'SELECT avg_laeq, max_laeq FROM runs WHERE session_id=?', (s['id'],)
        ).fetchall()
        if not runs:
            continue
        laeqs = [r['avg_laeq'] for r in runs if r['avg_laeq'] is not None]
        maxes = [r['max_laeq'] for r in runs if r['max_laeq'] is not None]
        if not laeqs:
            continue
        new_avg = round(10 * math.log10(
            sum(10 ** (v / 10) for v in laeqs) / len(laeqs)), 2)
        new_max = round(max(maxes), 1) if maxes else s['max_laeq']
        if (s['avg_laeq'] != new_avg or s['max_laeq'] != new_max
                or s['run_count'] != len(runs)):
            conn.execute(
                'UPDATE sessions SET avg_laeq=?, max_laeq=?, run_count=? WHERE id=?',
                (new_avg, new_max, len(runs), s['id']))
            changed.append((s['date'], s['avg_laeq'], new_avg))
    conn.commit()
    conn.close()
    return changed


def delete_orphaned_runs():
    """Delete run rows whose session no longer exists.

    These accumulate when sessions are deleted through a connection that did not
    enable `PRAGMA foreign_keys`, so the ON DELETE CASCADE never fires. They are
    unreachable — every read path inner-joins through sessions — but they carry
    the full spectral and profile JSON, so they waste real space.

    Returns the number of rows deleted.
    """
    conn = get_db()
    cur = conn.execute(
        'DELETE FROM runs WHERE session_id IS NULL '
        'OR session_id NOT IN (SELECT id FROM sessions)')
    n = cur.rowcount
    conn.commit()
    conn.close()
    return n


def get_sessions_export_format(dates=None):
    """Return sessions in the NOR140 import format for a given list of dates (or all)."""
    conn = get_db()
    if dates:
        ph = ','.join('?' * len(dates))
        sessions_rows = conn.execute(
            f'SELECT * FROM sessions WHERE date IN ({ph}) ORDER BY date', dates
        ).fetchall()
    else:
        sessions_rows = conn.execute('SELECT * FROM sessions ORDER BY date').fetchall()

    result = []
    for sess in sessions_rows:
        runs = conn.execute(
            'SELECT * FROM runs WHERE session_id=? ORDER BY run_number', (sess['id'],)
        ).fetchall()
        projects = [_run_to_dict(r, full=True) for r in runs]
        result.append({
            'd': sess['date'],
            'avg': sess['avg_laeq'],
            'mx': sess['max_laeq'],
            'projects': projects,
        })
    conn.close()
    return result


def purge_sessions_before(before_date):
    """Delete all sessions (and associated data) older than before_date (YYYY-MM-DD)."""
    conn = get_db()
    old = [r[0] for r in conn.execute(
        'SELECT date FROM sessions WHERE date < ?', (before_date,)).fetchall()]
    if old:
        ph = ','.join('?' * len(old))
        conn.execute(f'DELETE FROM assessment_runs WHERE session_date IN ({ph})', old)
        # weather table schema varies: some instances key by session_id, others by date
        weather_cols = {r[1] for r in conn.execute('PRAGMA table_info(weather)').fetchall()}
        if 'session_id' in weather_cols:
            conn.execute(
                f'DELETE FROM weather WHERE session_id IN '
                f'(SELECT id FROM sessions WHERE date IN ({ph}))', old)
        elif 'date' in weather_cols:
            conn.execute(f'DELETE FROM weather WHERE date IN ({ph})', old)
        conn.execute(
            f'DELETE FROM runs WHERE session_id IN (SELECT id FROM sessions WHERE date IN ({ph}))', old)
        conn.execute(f'DELETE FROM sessions WHERE date IN ({ph})', old)
        _record_tombstones(conn, old)
        conn.commit()
    conn.close()
    return old


def get_setting(key, default=None):
    conn = get_db()
    row = conn.execute('SELECT value FROM app_settings WHERE key=?', (key,)).fetchone()
    conn.close()
    return row['value'] if row else default


def set_setting(key, value):
    conn = get_db()
    conn.execute(
        'INSERT INTO app_settings (key, value) VALUES (?,?) '
        'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
        (key, value)
    )
    conn.commit()
    conn.close()
