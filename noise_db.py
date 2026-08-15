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
    ]
    for col, typ in _run_migrations:
        if col not in run_cols:
            conn.execute(f'ALTER TABLE runs ADD COLUMN {col} {typ}')
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
            run_number      INTEGER NOT NULL,
            conditions      TEXT,
            notes           TEXT,
            UNIQUE(assessment_id, session_date, run_number)
        );
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
            '  notes=COALESCE(excluded.notes, sessions.notes), '
            '  imported_at=datetime(\'now\')',
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
                '   prof_lae_json, prof_lapeak_json) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
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
                '  prof_lapeak_json=COALESCE(excluded.prof_lapeak_json,runs.prof_lapeak_json)',
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
                 json.dumps(_g('prof_lapeak_json')) if _g('prof_lapeak_json') else None)
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
            "  GROUP_CONCAT(COALESCE(al.label,'?'), ',') AS assess_locs "
            'FROM runs r '
            'LEFT JOIN assessment_runs ar '
            '  ON ar.session_date=? AND ar.run_number=r.run_number '
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
                'start':   r['start_time'],
                'n':       r['n_samples'],
                'step':    r['step'],
                'avg':     r['avg_laeq'],
                'mn':      r['min_laeq'],
                'mx':      r['max_laeq'],
                'pmx':     r['max_lcpeak'],
                'pmxi':    r['max_laimax'],
                'laeq_profile':   json.loads(r['laeq_json']),
                'lafmax_profile': json.loads(r['lafmax_json']) if r['lafmax_json'] else None,
                'laimax_profile': json.loads(r['laimax_json']) if r['laimax_json'] else None,
                'lcpeak_profile': json.loads(r['lcpeak_json']),
                'lceq':   r['lceq'],   'lae':    r['lae'],    'lce':    r['lce'],
                'lafmax': r['lafmax'], 'lcfmax': r['lcfmax'], 'lafmin': r['lafmin'], 'lcfmin': r['lcfmin'],
                'lasmax': r['lasmax'], 'lcsmax': r['lcsmax'], 'lasmin': r['lasmin'], 'lcsmin': r['lcsmin'],
                'laieq':  r['laieq'],  'lcieq':  r['lcieq'],
                'laimax': r['laimax'], 'lcimax': r['lcimax'], 'laimin': r['laimin'], 'lcimin': r['lcimin'],
                'laie':   r['laie'],   'lcie':   r['lcie'],
                'la_l01': r['la_l01'], 'la_l1':  r['la_l1'],  'la_l5':  r['la_l5'],
                'la_l10': r['la_l10'], 'la_l50': r['la_l50'], 'la_l90': r['la_l90'],
                'la_l95': r['la_l95'], 'la_l99': r['la_l99'],
                'lc_l10': r['lc_l10'], 'lc_l50': r['lc_l50'], 'lc_l90': r['lc_l90'],
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
                'run_number': r['run_number'],
                'source_file': r['source_file'],
                'start':   r['start_time'],
                'n':       r['n_samples'],
                'step':    r['step'],
                'avg':     r['avg_laeq'],
                'mn':      r['min_laeq'],
                'mx':      r['max_laeq'],
                'pmx':     r['max_lcpeak'],
                'pmxi':    r['max_laimax'],
                'laeq_profile':   json.loads(r['laeq_json']),
                'lafmax_profile': json.loads(r['lafmax_json']) if r['lafmax_json'] else None,
                'laimax_profile': json.loads(r['laimax_json']) if r['laimax_json'] else None,
                'lcpeak_profile': json.loads(r['lcpeak_json']),
                'lceq':   r['lceq'],   'lae':    r['lae'],    'lce':    r['lce'],
                'lafmax': r['lafmax'], 'lcfmax': r['lcfmax'], 'lafmin': r['lafmin'], 'lcfmin': r['lcfmin'],
                'lasmax': r['lasmax'], 'lcsmax': r['lcsmax'], 'lasmin': r['lasmin'], 'lcsmin': r['lcsmin'],
                'laieq':  r['laieq'],  'lcieq':  r['lcieq'],
                'laimax': r['laimax'], 'lcimax': r['lcimax'], 'laimin': r['laimin'], 'lcimin': r['lcimin'],
                'laie':   r['laie'],   'lcie':   r['lcie'],
                'la_l01': r['la_l01'], 'la_l1':  r['la_l1'],  'la_l5':  r['la_l5'],
                'la_l10': r['la_l10'], 'la_l50': r['la_l50'], 'la_l90': r['la_l90'],
                'la_l95': r['la_l95'], 'la_l99': r['la_l99'],
                'lc_l10': r['lc_l10'], 'lc_l50': r['lc_l50'], 'lc_l90': r['lc_l90'],
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
    conn = get_db()
    conn.execute('DELETE FROM sessions WHERE date=?', (date,))
    conn.commit()
    conn.close()


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
        projects = []
        for r in runs:
            projects.append({
                'run_number': r['run_number'],
                'source_file': r['source_file'],
                'start': r['start_time'],
                'n': r['n_samples'],
                'step': r['step'],
                'avg': r['avg_laeq'],
                'mn': r['min_laeq'],
                'mx': r['max_laeq'],
                'pmx': r['max_lcpeak'],
                'pmxi': r['max_laimax'],
                'laeq_profile':   json.loads(r['laeq_json'])   if r['laeq_json']   else [],
                'lafmax_profile': json.loads(r['lafmax_json'])  if r['lafmax_json'] else [],
                'laimax_profile': json.loads(r['laimax_json'])  if r['laimax_json'] else [],
                'lcpeak_profile': json.loads(r['lcpeak_json'])  if r['lcpeak_json'] else [],
                'lceq':   r['lceq'],   'lae':    r['lae'],    'lce':    r['lce'],
                'lafmax': r['lafmax'], 'lcfmax': r['lcfmax'], 'lafmin': r['lafmin'], 'lcfmin': r['lcfmin'],
                'lasmax': r['lasmax'], 'lcsmax': r['lcsmax'], 'lasmin': r['lasmin'], 'lcsmin': r['lcsmin'],
                'laieq':  r['laieq'],  'lcieq':  r['lcieq'],
                'laimax': r['laimax'], 'lcimax': r['lcimax'], 'laimin': r['laimin'], 'lcimin': r['lcimin'],
                'laie':   r['laie'],   'lcie':   r['lcie'],
                'la_l01': r['la_l01'], 'la_l1':  r['la_l1'],  'la_l5':  r['la_l5'],
                'la_l10': r['la_l10'], 'la_l50': r['la_l50'], 'la_l90': r['la_l90'],
                'la_l95': r['la_l95'], 'la_l99': r['la_l99'],
                'lc_l10': r['lc_l10'], 'lc_l50': r['lc_l50'], 'lc_l90': r['lc_l90'],
            })
        laeqs = [p['avg'] for p in projects if p['avg'] is not None]
        result.append({
            'd': sess['date'],
            'avg': round(sum(laeqs) / len(laeqs), 1) if laeqs else 0,
            'mx': max(laeqs) if laeqs else 0,
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
        conn.commit()
    conn.close()
    return old


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


# ── Assessments ───────────────────────────────────────────────────────────────

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
    conn = get_db()
    for date, run_num in run_pairs:
        conn.execute(
            'INSERT INTO assessment_runs (assessment_id, location_id, session_date, run_number) '
            'VALUES (?,?,?,?) ON CONFLICT(assessment_id, session_date, run_number) '
            'DO UPDATE SET location_id=excluded.location_id',
            (assessment_id, location_id, date, run_num)
        )
    conn.commit()
    conn.close()


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
                   r.run_number, r.start_time, r.n_samples, r.step,
                   r.avg_laeq, r.min_laeq, r.max_laeq, r.max_lcpeak, r.max_laimax,
                   r.location_tag, ar.session_date
            FROM assessment_runs ar
            JOIN sessions s ON s.date = ar.session_date
            JOIN runs r ON r.session_id = s.id AND r.run_number = ar.run_number
            WHERE ar.assessment_id=? AND ar.location_id=?
            ORDER BY ar.session_date, r.start_time
        ''', (aid, loc['id'])).fetchall()
        loc_dict['runs'] = [dict(r) for r in assigned]
        locations.append(loc_dict)

    assessment['locations'] = locations

    unlocated = conn.execute('''
        SELECT ar.id as ar_id, ar.conditions, ar.notes as run_notes,
               r.run_number, r.start_time, r.n_samples, r.step,
               r.avg_laeq, r.min_laeq, r.max_laeq, r.max_lcpeak, r.max_laimax,
               r.location_tag, ar.session_date
        FROM assessment_runs ar
        JOIN sessions s ON s.date = ar.session_date
        JOIN runs r ON r.session_id = s.id AND r.run_number = ar.run_number
        WHERE ar.assessment_id=? AND ar.location_id IS NULL
        ORDER BY ar.session_date, r.start_time
    ''', (aid,)).fetchall()
    assessment['unlocated_runs'] = [dict(r) for r in unlocated]

    conn.close()
    return assessment


def get_all_runs_for_assessment(assessment_id):
    conn = get_db()
    rows = conn.execute('''
        SELECT s.date as session_date, s.location_label as session_loc,
               r.id as run_db_id, r.run_number, r.start_time, r.n_samples, r.step,
               r.avg_laeq, r.max_laeq, r.max_lcpeak, r.max_laimax, r.location_tag
        FROM runs r
        JOIN sessions s ON r.session_id = s.id
        ORDER BY s.date, r.run_number
    ''').fetchall()

    ar_rows = conn.execute(
        'SELECT id as ar_id, session_date, run_number, location_id '
        'FROM assessment_runs WHERE assessment_id=?', (assessment_id,)
    ).fetchall()
    assigned = {(ar['session_date'], ar['run_number']): dict(ar) for ar in ar_rows}

    loc_rows = conn.execute(
        'SELECT id, label FROM assessment_locations WHERE assessment_id=?',
        (assessment_id,)
    ).fetchall()
    loc_labels = {r['id']: r['label'] for r in loc_rows}

    conn.close()

    result = []
    for r in rows:
        row = dict(r)
        key = (r['session_date'], r['run_number'])
        ar = assigned.get(key)
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
                   r.run_number, r.start_time, r.n_samples, r.step,
                   r.avg_laeq, r.min_laeq, r.max_laeq, r.max_lcpeak, r.max_laimax,
                   r.la_l10, r.la_l50, r.la_l90, r.laeq_json, ar.session_date
            FROM assessment_runs ar
            JOIN sessions s ON s.date = ar.session_date
            JOIN runs r ON r.session_id = s.id AND r.run_number = ar.run_number
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
