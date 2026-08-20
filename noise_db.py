import hashlib
import logging
import os
import json
import sqlite3
import uuid

from nor140_format import SPECTRAL_TABLES, round_half_up
# The session LAeq must be the same number whether it was written by an import
# or by a recompute, so both call the one implementation. noise_parser imports
# nothing from here, so this direction is safe.
from noise_parser import _energy_avg_weighted, run_weight_s

DB_PATH = os.environ.get('NOISE_DB_PATH', '/home/flightdata/noise-meter/noise.db')


def percentile(sorted_vals, p):
    """The p-th percentile of an ascending list, linearly interpolated.

    Lives here because reports.py and assessments_db.py both need it and both
    already import from this module — assessments_db used to carry its own
    nearest-rank version, which disagreed with the reports' figure for the same
    run. LA90 is the level exceeded 90% of the time, i.e. percentile(s, 10).
    """
    n = len(sorted_vals)
    if not n:
        return None
    idx = (p / 100) * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    return round_half_up(
        sorted_vals[lo] + (idx - lo) * (sorted_vals[hi] - sorted_vals[lo]), 1)


def get_db():
    # timeout=15: seconds sqlite3 will retry (busy_timeout) before raising
    # "database is locked" — request threads, the weather-fetch thread and the
    # peer-push thread can all be writing around the same time.
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    # WAL: readers no longer block writers (or vice versa), which is what the
    # concurrent writers above actually need — the default rollback journal
    # serialises everyone. Idempotent: SQLite records the journal mode in the
    # database file itself, so this is a no-op after the first connection ever
    # made it. It does mean a running database is three files on disk
    # (noise.db, noise.db-wal, noise.db-shm) instead of one — a plain `cp` of
    # noise.db alone can miss committed data still sitting in -wal, so the
    # nightly backup (backup_db.py) uses sqlite3's own online backup API
    # instead, which checkpoints correctly regardless of journal mode.
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


class MigrationUnsafe(RuntimeError):
    """Raised when a schema change cannot be applied without losing meaning.

    Better than a bare IntegrityError from a failing index: it names the rows
    needing attention and leaves the database as it was.
    """


# ── Instrument serial ────────────────────────────────────────────────────────
# A session is identified by (date, instrument_serial): several NOR140 meters
# may measure on the same calendar date at different sites. The meter writes
# no serial into GLOB/PROF, so it is supplied from outside the data — the
# upload form, import_sdcard.py --serial, or `serial` beside `d` in a JSON
# payload — and a payload that names none is filed under this Pi's default,
# the `instrument_serial` app setting (else ''). That keeps an older peer, an
# older JSON export and the pre-WP8 two-Pi deployment importing unchanged.

def _default_serial(conn):
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key='instrument_serial'").fetchone()
    return (row[0] or '').strip() if row else ''


def default_serial():
    """The serial a payload/route/tombstone that names none is filed under."""
    conn = get_db()
    try:
        return _default_serial(conn)
    finally:
        conn.close()


def resolve_serial(serial, conn=None):
    """Normalise a caller-supplied serial: stripped, or the default when blank.

    Blank is treated the same as missing on purpose — an empty serial is not a
    different instrument, it is an instrument nobody named.
    """
    s = (serial or '').strip() if serial is not None else ''
    if s:
        return s
    return _default_serial(conn) if conn is not None else default_serial()


# ── F6 / WP10: replication identity for mutable hand-entered rows ───────────
#
# assessments, assessment_locations, assessment_runs and report_templates are
# edited on either Pi, so they need an identity the pair agrees on without
# talking. New rows mint a uuid4 (the WP9 generated_reports pattern). Rows
# that already existed at migration time get a *deterministic* uuid5 of stable
# replicated content, because both Pis hold the same rows (replicated by id
# until WP10) and independently-minted uids would twin every one of them on
# the first full sync. The seed fields are chosen to be identical on both Pis
# precisely because they replicated: created_at/name for assessments (both
# cross the wire and neither is rewritten on apply), the parent uid + label +
# sort_order for locations, the parent uid + session key + source_file for
# run links, and name + prompt hash for templates.
#
# Collision behaviour: two rows in ONE database whose seed fields coincide
# (e.g. two assessments created in the same second with the same name) are
# disambiguated deterministically by id order ('|dup1', '|dup2', …) — the ids
# replicated too, so both Pis assign the same suffixes. Across databases, a
# seed collision between rows that are genuinely the same record is the
# point; between rows that merely *look* the same (same name, same second,
# created independently on the two Pis before WP10) it would wrongly unify
# them — accepted: the sync events and 15-minute full-sync pulls make
# unsynced same-second twins vanishingly unlikely, and the unified row keeps
# one side's content rather than losing the record.

F6_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, 'noise-meter.ives.org.uk/f6')


# ── WP12: total-order LWW stamps ────────────────────────────────────────────
#
# datetime('now') is second-resolution, and the WP10 gate applied equal
# timestamps — so two Pis editing the same row in the same second swapped
# values on every exchange, forever, with no conflict recorded (F3). Since
# WP12 every LWW timestamp (updated_at on the uid-replicated tables, their
# uid tombstones' deleted_at, and the F4 metadata/tag stamps) is written at
# millisecond resolution, and every local mutation also records its writer
# (the Pi's PI_NAME). The replication gate orders on the string tuple
# (updated_at, writer), which is total: any two distinct writes resolve the
# same way on both Pis without talking.
#
# Transition semantics: stored second-resolution timestamps stay valid — as a
# string, 'HH:MM:SS' orders before any same-second 'HH:MM:SS.mmm' (the
# shorter string is a prefix of neither but differs first at '.', 0x2e, which
# is greater than end-of-string) — and a row without a writer compares with
# writer '', losing same-timestamp ties to any named writer.

LWW_NOW_SQL = "strftime('%Y-%m-%d %H:%M:%f','now')"


def local_writer():
    """The writer id stamped on local mutations: the Pi's PI_NAME.

    Read from the environment at call time, not import time, so the test
    suite's two Sides (one process, two databases) can be two writers."""
    return os.environ.get('PI_NAME', 'Pi')


def new_uid():
    """Identity for a row created here and now: uuid4, minted once."""
    return str(uuid.uuid4())


def uid_for_assessment(created_at, name):
    return str(uuid.uuid5(F6_NAMESPACE,
                          'assessment|%s|%s' % (created_at or '', name or '')))


def uid_for_location(assessment_uid, label, sort_order):
    return str(uuid.uuid5(F6_NAMESPACE, 'location|%s|%s|%s' % (
        assessment_uid or '', label or '',
        '' if sort_order is None else sort_order)))


def uid_for_assessment_run(assessment_uid, session_date, serial, source_file,
                           run_number):
    ident = source_file or ('run:%s' % ('' if run_number is None else run_number))
    return str(uuid.uuid5(F6_NAMESPACE, 'assessment_run|%s|%s|%s|%s' % (
        assessment_uid or '', session_date or '', serial or '', ident)))


def uid_for_template(name, prompt):
    return str(uuid.uuid5(F6_NAMESPACE, 'template|%s|%s' % (
        name or '', hashlib.sha256((prompt or '').encode()).hexdigest())))


def record_uid_tombstones(conn, table_name, uids, deleted_at=None):
    """Mark uid-keyed rows as deleted so the deletion replicates. Caller
    commits. Mirrors _record_tombstones for sessions: deleted_at is supplied
    when replaying a peer's tombstone so the original time propagates, and
    MAX() keeps the newest time when both Pis deleted the same row."""
    for u in uids:
        if not u:
            continue
        if deleted_at is None:
            # Millisecond deleted_at (WP12): the tombstone competes with edit
            # updated_at values in the LWW gate, so it carries the same
            # resolution. An exact tie (same millisecond) resolves delete-wins
            # on both sides — the edit is not strictly newer than the delete
            # here, and on the deleting side the local row is already gone —
            # so the pair still converges without a writer column.
            conn.execute(
                f'INSERT INTO deleted_uids (table_name, uid, deleted_at) '
                f'VALUES (?,?,{LWW_NOW_SQL}) '
                f'ON CONFLICT(table_name, uid) DO UPDATE SET deleted_at={LWW_NOW_SQL}',
                (table_name, u))
        else:
            conn.execute(
                'INSERT INTO deleted_uids (table_name, uid, deleted_at) VALUES (?,?,?) '
                'ON CONFLICT(table_name, uid) DO UPDATE SET '
                '  deleted_at=MAX(deleted_uids.deleted_at, excluded.deleted_at)',
                (table_name, u, deleted_at))


def _backfill_uids(conn, table, rows, seed_fn):
    """Assign uuid5(F6_NAMESPACE, seed_fn(row)) to each row (uid currently
    NULL), in id order, deterministically suffixing local duplicates. Both
    Pis hold the same rows in the same ids, so both compute the same uids."""
    used = {r[0] for r in conn.execute(
        f'SELECT uid FROM {table} WHERE uid IS NOT NULL').fetchall()}
    for row in rows:
        seed = seed_fn(row)
        cand, k = seed, 0
        while str(uuid.uuid5(F6_NAMESPACE, cand)) in used:
            k += 1
            cand = f'{seed}|dup{k}'
        u = str(uuid.uuid5(F6_NAMESPACE, cand))
        used.add(u)
        conn.execute(f'UPDATE {table} SET uid=? WHERE id=?', (u, row['id']))


# Column order for the sessions table when it has to be rebuilt. SQLite cannot
# replace UNIQUE(date) with UNIQUE(date, instrument_serial) in place.
_SESSION_COLUMNS = [
    ('id',                'INTEGER PRIMARY KEY'),
    ('date',              'TEXT NOT NULL'),
    ('instrument_serial', "TEXT NOT NULL DEFAULT ''"),
    ('run_count',         'INTEGER'),
    ('avg_laeq',          'REAL'),
    ('max_laeq',          'REAL'),
    ('recorder_name',     'TEXT'),
    ('location_label',    'TEXT'),
    ('postcode',          'TEXT'),
    ('lat',               'REAL'),
    ('lng',               'REAL'),
    ('notes',             'TEXT'),
    ('imported_at',       "TEXT DEFAULT (datetime('now'))"),
]


def _table_sql(conn, name):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type IN ('table','index') AND name=?",
        (name,)).fetchone()
    return (row[0] if row else '') or ''


def _rebuild_sessions_with_serial(conn, serial):
    """Replace UNIQUE(date) with UNIQUE(date, instrument_serial), keeping ids.

    Follows SQLite's documented table-rebuild procedure. Ids are copied so
    runs.session_id stays valid; every existing row is filed under `serial`
    (the Pi's default at migration time). foreign_keys is switched off for the
    duration and the switch is verified: with enforcement on, DROP TABLE
    sessions would cascade-delete every run. The PRAGMA is a silent no-op
    inside a transaction, which is why the caller commits first.
    """
    info = conn.execute('PRAGMA table_info(sessions)').fetchall()
    have = [r[1] for r in info]
    known = {c for c, _ in _SESSION_COLUMNS}
    extra = [(r[1], r[2] or '') for r in info if r[1] not in known]
    columns = _SESSION_COLUMNS + extra
    defs = ', '.join(f'{c} {t}' for c, t in columns)
    copy_cols = [c for c, _ in columns if c in have and c != 'instrument_serial']
    serial_expr = ("COALESCE(NULLIF(TRIM(instrument_serial), ''), ?)"
                   if 'instrument_serial' in have else '?')
    conn.commit()
    conn.execute('PRAGMA foreign_keys=off')
    if conn.execute('PRAGMA foreign_keys').fetchone()[0]:
        raise MigrationUnsafe(
            'could not disable foreign_keys for the sessions rebuild (a '
            'transaction is open); refusing, since DROP TABLE sessions would '
            'cascade to runs')
    try:
        conn.execute(f'CREATE TABLE sessions_new ({defs}, UNIQUE(date, instrument_serial))')
        conn.execute(
            f'INSERT INTO sessions_new ({",".join(copy_cols)}, instrument_serial) '
            f'SELECT {",".join(copy_cols)}, {serial_expr} FROM sessions', (serial,))
        conn.execute('DROP TABLE sessions')
        conn.execute('ALTER TABLE sessions_new RENAME TO sessions')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_sessions_date ON sessions(date)')
        conn.commit()
    finally:
        conn.execute('PRAGMA foreign_keys=on')


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

    # (date, instrument_serial) sessions. Rebuild when the column is missing
    # or the table-level UNIQUE still names the date alone; idempotent after.
    serial_default = _default_serial(conn)
    sess_sql = _table_sql(conn, 'sessions')
    if ('instrument_serial' not in existing
            or 'UNIQUE(date, instrument_serial)' not in sess_sql):
        _rebuild_sessions_with_serial(conn, serial_default)

    # Tombstones for deleted sessions. A missed upsert self-heals on the next
    # full sync, but a missed delete does not — the peer would keep the session
    # forever — so deletions are recorded and replayed rather than only pushed.
    # deleted_at uses datetime('now') (UTC) to match sessions.imported_at, which
    # apply_full_sync() compares it against. Keyed like sessions, on
    # (date, instrument_serial); an older table keyed on date alone is rebuilt
    # with every tombstone filed under the default serial.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS deleted_sessions (
            date              TEXT NOT NULL,
            instrument_serial TEXT NOT NULL DEFAULT '',
            deleted_at        TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (date, instrument_serial)
        )
    ''')
    ds_cols = {row[1] for row in conn.execute('PRAGMA table_info(deleted_sessions)').fetchall()}
    if 'instrument_serial' not in ds_cols:
        conn.executescript('''
            CREATE TABLE deleted_sessions_new (
                date              TEXT NOT NULL,
                instrument_serial TEXT NOT NULL DEFAULT '',
                deleted_at        TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (date, instrument_serial)
            );
        ''')
        conn.execute(
            'INSERT INTO deleted_sessions_new (date, instrument_serial, deleted_at) '
            'SELECT date, ?, deleted_at FROM deleted_sessions', (serial_default,))
        conn.execute('DROP TABLE deleted_sessions')
        conn.execute('ALTER TABLE deleted_sessions_new RENAME TO deleted_sessions')
    # Weather belongs to the session — its time and location — so it is keyed
    # like one: (date, instrument_serial). Keyed by date alone, two same-date
    # sessions from different meters at different sites shared one weather row,
    # and whichever fetch ran last won.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS weather (
            date              TEXT NOT NULL,
            instrument_serial TEXT NOT NULL DEFAULT '',
            wind_speed  REAL,
            wind_dir    REAL,
            temp_min    REAL,
            temp_max    REAL,
            precip      REAL,
            hourly_json TEXT,
            PRIMARY KEY (date, instrument_serial)
        )
    ''')
    wx_cols = {row[1] for row in conn.execute('PRAGMA table_info(weather)').fetchall()}
    if 'instrument_serial' not in wx_cols:
        # A date-keyed table (pre-WP9). Every existing row was fetched for a
        # session now filed under the default serial, so that is where the row
        # goes; date was the PRIMARY KEY, so the new key cannot collide.
        if 'date' not in wx_cols:
            raise MigrationUnsafe(
                'the weather table has neither instrument_serial nor date '
                'columns (%s); it cannot be re-keyed automatically' %
                sorted(wx_cols))
        keep = [c for c in ('wind_speed', 'wind_dir', 'temp_min', 'temp_max',
                            'precip', 'hourly_json') if c in wx_cols]
        keep_sql = (', ' + ', '.join(keep)) if keep else ''
        conn.executescript('''
            CREATE TABLE weather_new (
                date              TEXT NOT NULL,
                instrument_serial TEXT NOT NULL DEFAULT '',
                wind_speed  REAL,
                wind_dir    REAL,
                temp_min    REAL,
                temp_max    REAL,
                precip      REAL,
                hourly_json TEXT,
                PRIMARY KEY (date, instrument_serial)
            );
        ''')
        conn.execute(
            f'INSERT INTO weather_new (date, instrument_serial{keep_sql}) '
            f'SELECT date, ?{keep_sql} FROM weather', (serial_default,))
        conn.execute('DROP TABLE weather')
        conn.execute('ALTER TABLE weather_new RENAME TO weather')
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
            uid            TEXT,
            session_date   TEXT NOT NULL,
            instrument_serial TEXT NOT NULL DEFAULT '',
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
    # source_file / input_snapshot_json: report provenance (WP3). A report is
    # an evidence document, so it pins the run it was generated from by its
    # stable identity (the PROJ folder, not the position, which is renumbered
    # on every import) and carries a snapshot of every input it was rendered
    # from, so a later backfill or re-import cannot make it disagree with
    # itself.
    for col, typ in [('run_number', 'INTEGER'), ('run_label', 'TEXT'),
                     ('source_file', 'TEXT'), ('input_snapshot_json', 'TEXT')]:
        if col not in gr_cols:
            conn.execute(f'ALTER TABLE generated_reports ADD COLUMN {col} {typ}')
    if 'instrument_serial' not in gr_cols:
        # Every report stored before this column existed was generated from a
        # session that is now filed under the default serial.
        conn.execute("ALTER TABLE generated_reports ADD COLUMN instrument_serial "
                     "TEXT NOT NULL DEFAULT ''")
        conn.execute('UPDATE generated_reports SET instrument_serial=?', (serial_default,))
    if 'source_file' not in gr_cols:
        # Backfill through the run_number join. As with assessment_runs below,
        # this is the one moment the positional join is still trustworthy: the
        # numbers only go stale once a re-import shifts them, and this runs
        # before any such import can. Whole-session reports (run_number NULL)
        # have no single run to pin and stay NULL.
        conn.execute('''
            UPDATE generated_reports SET source_file = (
                SELECT r.source_file FROM runs r
                JOIN sessions s ON s.id = r.session_id
                WHERE s.date = generated_reports.session_date
                  AND r.run_number = generated_reports.run_number
            ) WHERE source_file IS NULL AND run_number IS NOT NULL
        ''')
    if 'uid' not in gr_cols:
        # WP9: generated reports replicate between the Pis, and the local
        # integer id cannot be the replication key — both sides assign their
        # own. The uid is minted once, at save time, and the peer upserts on
        # it. Pre-WP9 rows get a fresh uuid4 here, which means the two Pis
        # hold *different* uids for their pre-existing local rows — correct,
        # because they ARE different local rows (reports never replicated
        # before this), not two copies of one report.
        conn.execute('ALTER TABLE generated_reports ADD COLUMN uid TEXT')
    for (rid,) in conn.execute(
            'SELECT id FROM generated_reports WHERE uid IS NULL').fetchall():
        conn.execute('UPDATE generated_reports SET uid=? WHERE id=?',
                     (str(uuid.uuid4()), rid))
    conn.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS idx_generated_reports_uid
            ON generated_reports(uid)
    ''')
    if not conn.execute('SELECT COUNT(*) FROM report_templates').fetchone()[0]:
        # Imported here rather than at module scope: reports_db imports get_db
        # from this module, so a top-level import would be circular.
        from reports_db import DEFAULT_TEMPLATES
        for t in DEFAULT_TEMPLATES:
            conn.execute(
                'INSERT INTO report_templates (name, description, prompt, is_default) VALUES (?,?,?,?)',
                (t['name'], t['description'], t['prompt'], t['is_default'])
            )
    # WP13: the seed above only fires on an empty table, so a corrected default
    # prompt would never reach the Pis that seeded theirs months ago. This
    # rewrites the stored rows that still hold a previously shipped text and
    # leaves locally edited ones alone. It runs BEFORE the F6 uid backfill
    # below on purpose: the template uid is uuid5 of name + sha256(prompt), so
    # refreshing first makes a migrated database and a freshly seeded one
    # agree on the uid.
    from reports_db import refresh_default_templates
    refresh_default_templates(conn)
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
            -- With session_date, names the session: sessions are keyed on
            -- (date, instrument_serial) because several meters can measure
            -- on one calendar date.
            instrument_serial TEXT NOT NULL DEFAULT '',
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
    ''')

    # Order matters, and got this wrong once. The unique index used to be created
    # inside the schema script above, which referenced source_file before the
    # ALTER below had added it — so migrating any database predating the column
    # died with "no such column: source_file". Column, then backfill, then audit,
    # then rebuild, then index.
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
    if 'instrument_serial' not in ar_cols:
        # Links made before sessions carried a serial all point at sessions
        # now filed under the default one.
        conn.execute("ALTER TABLE assessment_runs ADD COLUMN instrument_serial "
                     "TEXT NOT NULL DEFAULT ''")
        conn.execute('UPDATE assessment_runs SET instrument_serial=?', (serial_default,))

    # Only now is it safe to look. A duplicated stable key cannot be migrated
    # automatically, and creating the index over it would fail with a bare
    # IntegrityError naming nothing. Refuse instead, and say which rows.
    dupes = conn.execute('''
        SELECT assessment_id, session_date, instrument_serial, source_file, COUNT(*) AS n
        FROM assessment_runs WHERE source_file IS NOT NULL
        GROUP BY assessment_id, session_date, instrument_serial, source_file HAVING n > 1
    ''').fetchall()
    if dupes:
        raise MigrationUnsafe(
            'assessment_runs holds %d duplicated stable key(s), so the unique '
            'index cannot be created. Run audit_assessment_run_keys(), resolve '
            'them, then restart. First few: %s'
            % (len(dupes), [tuple(d) for d in dupes][:5]))

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
                instrument_serial TEXT NOT NULL DEFAULT '',
                run_number      INTEGER,
                source_file     TEXT,
                conditions      TEXT,
                notes           TEXT
            );
            INSERT INTO assessment_runs_new
                (id, assessment_id, location_id, session_date, instrument_serial,
                 run_number, source_file, conditions, notes)
            SELECT id, assessment_id, location_id, session_date, instrument_serial,
                   run_number, source_file, conditions, notes FROM assessment_runs;
            DROP TABLE assessment_runs;
            ALTER TABLE assessment_runs_new RENAME TO assessment_runs;
        ''')
        conn.execute('PRAGMA foreign_keys=on')

    # The stable key is (assessment, date, serial, source_file). An index built
    # before the serial existed is replaced; CREATE ... IF NOT EXISTS alone
    # would keep the narrower one and reject a second meter's PROJ0001.
    if 'instrument_serial' not in _table_sql(conn, 'idx_assessment_runs_stable'):
        conn.execute('DROP INDEX IF EXISTS idx_assessment_runs_stable')
    conn.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS idx_assessment_runs_stable
            ON assessment_runs(assessment_id, session_date, instrument_serial, source_file)
            WHERE source_file IS NOT NULL
    ''')

    # source_file is the run's identity, so make the database enforce it. Two
    # runs sharing one within a session would make every stable-key join
    # ambiguous, and the audit would still have called it clean because it only
    # asked whether at least one run matched.
    run_dupes = conn.execute('''
        SELECT session_id, source_file, COUNT(*) AS n FROM runs
        WHERE source_file IS NOT NULL
        GROUP BY session_id, source_file HAVING n > 1
    ''').fetchall()
    if run_dupes:
        raise MigrationUnsafe(
            'runs holds %d duplicated (session, source_file) identit(ies), which '
            'the stable key cannot distinguish. Resolve them, then restart. '
            'First few: %s' % (len(run_dupes), [tuple(d) for d in run_dupes][:5]))
    conn.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_stable
            ON runs(session_id, source_file) WHERE source_file IS NOT NULL
    ''')

    # ── F6 / WP10: uid sync keys + LWW timestamps for the mutable tables ────
    # One ADD COLUMN per statement (SQLite). updated_at is left NULL for
    # pre-existing rows on purpose: NULL means "age unknown", and the LWW gate
    # in sync_db treats it as older than any real timestamp — so the first
    # timestamped edit anywhere wins over every untouched pre-WP10 copy.
    # (report_templates already has updated_at, with a non-NULL default.)
    for _tbl, _cols in (
        ('assessments',          ('uid TEXT', 'updated_at TEXT')),
        ('assessment_locations', ('uid TEXT', 'assessment_uid TEXT',
                                  'updated_at TEXT')),
        ('assessment_runs',      ('uid TEXT', 'assessment_uid TEXT',
                                  'location_uid TEXT', 'updated_at TEXT')),
        ('report_templates',     ('uid TEXT',)),
    ):
        _have = {r[1] for r in conn.execute(f'PRAGMA table_info({_tbl})').fetchall()}
        for _cd in _cols:
            if _cd.split()[0] not in _have:
                conn.execute(f'ALTER TABLE {_tbl} ADD COLUMN {_cd}')

    # Deterministic uid backfill, parents before children (the child seeds
    # embed the parent uid). See the F6 block above _SESSION_COLUMNS for why
    # these are content hashes rather than fresh uuid4s. Seed columns a
    # hand-built database might lack read as NULL rather than failing the
    # whole migration.
    def _sel(table, *cols):
        have = {r[1] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()}
        named = ', '.join(c if c in have else f'NULL AS {c}' for c in cols)
        return conn.execute(f'SELECT id, {named} FROM {table} WHERE uid IS NULL '
                            'ORDER BY id').fetchall()

    _backfill_uids(conn, 'assessments', _sel('assessments', 'created_at', 'name'),
        lambda r: 'assessment|%s|%s' % (r['created_at'] or '', r['name'] or ''))
    # uid-based FK references, populated from the local integer FKs — the ints
    # stay for local integrity (and the CASCADE/SET NULL behaviour), but every
    # replication payload and apply uses the uids exclusively.
    conn.execute('''
        UPDATE assessment_locations SET assessment_uid =
            (SELECT uid FROM assessments a WHERE a.id = assessment_locations.assessment_id)
        WHERE assessment_uid IS NULL''')
    _backfill_uids(conn, 'assessment_locations',
        _sel('assessment_locations', 'assessment_uid', 'label', 'sort_order'),
        lambda r: 'location|%s|%s|%s' % (r['assessment_uid'] or '', r['label'] or '',
                                         '' if r['sort_order'] is None else r['sort_order']))
    conn.execute('''
        UPDATE assessment_runs SET assessment_uid =
            (SELECT uid FROM assessments a WHERE a.id = assessment_runs.assessment_id)
        WHERE assessment_uid IS NULL''')
    conn.execute('''
        UPDATE assessment_runs SET location_uid =
            (SELECT uid FROM assessment_locations l WHERE l.id = assessment_runs.location_id)
        WHERE location_uid IS NULL AND location_id IS NOT NULL''')
    _backfill_uids(conn, 'assessment_runs',
        _sel('assessment_runs', 'assessment_uid', 'session_date',
             'instrument_serial', 'source_file', 'run_number'),
        lambda r: 'assessment_run|%s|%s|%s|%s' % (
            r['assessment_uid'] or '', r['session_date'] or '',
            r['instrument_serial'] or '',
            r['source_file'] or ('run:%s' % ('' if r['run_number'] is None
                                             else r['run_number']))))
    # Templates: name + prompt hash, so the three DEFAULT_TEMPLATES rows the
    # two Pis each seeded independently unify under one uid apiece, while a
    # user-written template (different prompt) stays its own record.
    _backfill_uids(conn, 'report_templates',
        _sel('report_templates', 'name', 'prompt'),
        lambda r: 'template|%s|%s' % (
            r['name'] or '',
            hashlib.sha256((r['prompt'] or '').encode()).hexdigest()))

    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_assessments_uid '
                 'ON assessments(uid)')
    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_assessment_locations_uid '
                 'ON assessment_locations(uid)')
    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_assessment_runs_uid '
                 'ON assessment_runs(uid)')
    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_report_templates_uid '
                 'ON report_templates(uid)')

    # ── WP12: total-order LWW (F3) and metadata/tag stamps (F4) ─────────────
    # Additive only. `writer` (the Pi's PI_NAME) is stamped beside updated_at
    # on every local mutation of the uid-replicated tables; the LWW gate
    # orders on the string tuple (updated_at, writer), so same-timestamp
    # writes from the two Pis resolve identically on both sides instead of
    # swapping forever. sessions.meta_updated_at/meta_writer and
    # runs.tag_updated_at/tag_writer bring the hand-edited session metadata
    # and run location tags (previously COALESCE-merged with no ordering at
    # all) under the same gate. All NULL for existing rows on purpose: NULL
    # means "never hand-edited since WP12" and always loses to a stamped edit
    # without generating a conflict.
    for _tbl, _cols in (
        ('assessments',          ('writer TEXT',)),
        ('assessment_locations', ('writer TEXT',)),
        ('assessment_runs',      ('writer TEXT',)),
        ('report_templates',     ('writer TEXT',)),
        ('sessions',             ('meta_updated_at TEXT', 'meta_writer TEXT')),
        ('runs',                 ('tag_updated_at TEXT', 'tag_writer TEXT')),
    ):
        _have = {r[1] for r in conn.execute(f'PRAGMA table_info({_tbl})').fetchall()}
        for _cd in _cols:
            if _cd.split()[0] not in _have:
                conn.execute(f'ALTER TABLE {_tbl} ADD COLUMN {_cd}')

    # Tombstones for uid-keyed deletes (assessments, locations, run links,
    # templates, generated reports). Without them a full sync from a Pi that
    # still holds a row resurrects it on the Pi that deleted it while the
    # holder was offline — the gap WP9 documented for reports.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS deleted_uids (
            table_name TEXT NOT NULL,
            uid        TEXT NOT NULL,
            deleted_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (table_name, uid)
        )
    ''')
    # Divergence made visible (F6): an incoming row older than the local edit
    # is skipped, and the skip lands here — latest occurrence per (table, uid),
    # so the table stays small. Read via GET /api/sync-conflicts.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS sync_conflicts (
            table_name        TEXT NOT NULL,
            uid               TEXT NOT NULL,
            local_updated_at  TEXT,
            remote_updated_at TEXT,
            seen_at           TEXT DEFAULT (datetime('now')),
            payload_json      TEXT,
            PRIMARY KEY (table_name, uid)
        )
    ''')

    conn.commit()


def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS sessions (
            id             INTEGER PRIMARY KEY,
            date           TEXT NOT NULL,
            instrument_serial TEXT NOT NULL DEFAULT '',
            run_count      INTEGER,
            avg_laeq       REAL,
            max_laeq       REAL,
            recorder_name  TEXT,
            location_label TEXT,
            postcode       TEXT,
            lat            REAL,
            lng            REAL,
            imported_at    TEXT DEFAULT (datetime('now')),
            UNIQUE(date, instrument_serial)
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


class ImportResult(int):
    """What import_sessions() returns: the imported-session count, as ever —
    an int subclass so every existing caller (arithmetic, jsonify, equality
    in the suite) keeps working — plus the deletion report an explicit prune
    produces. Deletion must never be silent: do_upload and /import read these
    to tell the operator exactly what a complete-date import removed.

    deleted_runs   — stored runs pruned because a complete_date session no
                     longer contained them.
    deleted_links  — assessment_runs rows that pointed at those runs, removed
                     (and uid-tombstoned) with them; an assessor must know
                     the packaging changed.
    deleted_run_files — [(date, source_file), ...] naming the pruned runs.
    """
    deleted_runs = 0
    deleted_links = 0
    deleted_run_files = ()


def import_sessions(sessions_data, metadata=None):
    """Import sessions. Per-session metadata/weather (as sent by
    get_sessions_since() for peer sync) takes precedence when present;
    the `metadata` kwarg is the fallback used by the single-session manual
    upload form, which has no per-session metadata of its own.

    Run identity. Runs are upserted on (session_id, source_file) — the PROJ
    folder the measurement came from, which is the meter's own name for it and
    the only key that survives a partial or re-ordered upload. Keyed on the
    position within the payload, as this used to be, a ZIP of just PROJ0004
    became "run 1" and overwrote PROJ0001. Payload rows that carry no
    source_file (JSON exports made before the column existed, or a peer on the
    older schema) fall back to the positional key, matching the row at that
    run_number rather than creating a duplicate; the stored source_file is
    kept through COALESCE.

    After the upsert, run_number is reassigned from the stored order for the
    whole session (by source_file, then start_time), so it is display metadata
    that always reflects what the database holds — and the session aggregates
    are recomputed from every stored run rather than copied from the payload,
    which may have been a partial upload.

    Deletion. A session dict may carry complete_date=True. Since WP11 no
    parse ever infers it (path shape cannot prove completeness — F1): it is
    set only by a caller that genuinely enumerated the card, i.e.
    import_sdcard.py walking the real card root, the upload form's explicit
    prune checkbox, or an /import payload whose top level carries
    "prune": true. Only then are stored runs absent from the payload deleted
    — and never one named in the session's skipped_files, which are runs the
    parser saw but could not read. Payloads without the flag (partial
    uploads, JSON, peer sync) only add or refresh.

    Pruning a run also removes, in the same transaction, every
    assessment_runs row pointing at it via the stable key (F5 — the links
    were left dangling before: stored but invisible in joins and silently
    missing from assessment exports), and writes a WP10 uid tombstone for
    each removed link so the deletion replicates and a peer's stale full
    sync cannot resurrect it. The counts come back on the ImportResult.
    """
    meta = metadata or {}
    conn = get_db()
    imported = 0
    dates = []
    deleted_run_files = []   # [(date, source_file)] pruned by complete_date
    deleted_links = 0        # assessment_runs rows removed with them
    try:
        for sess in sessions_data:
            date = sess['d']
            # The instrument. A payload that names none (older peer, older
            # JSON export, a form left blank) is filed under this Pi's default.
            serial = resolve_serial(sess.get('serial'), conn)
            projects = sess.get('projects', [])
            recorder_name  = sess.get('name', meta.get('recorder_name')) or None
            location_label = sess.get('loc',  meta.get('location_label')) or None
            postcode       = sess.get('post', meta.get('postcode')) or None
            lat            = sess.get('lat',  meta.get('lat'))
            lng            = sess.get('lng',  meta.get('lng'))
            notes          = sess.get('notes', meta.get('notes')) or None
            # run_count / avg_laeq / max_laeq are deliberately not taken from the
            # payload: they describe whatever was uploaded, not what is stored.
            # _recompute_session_aggregates() below fills them from the runs.
            conn.execute(
                'INSERT INTO sessions '
                '  (date, instrument_serial, recorder_name, location_label, postcode, lat, lng, notes) '
                'VALUES (?,?,?,?,?,?,?,?) '
                'ON CONFLICT(date, instrument_serial) DO UPDATE SET '
                '  recorder_name=COALESCE(excluded.recorder_name, sessions.recorder_name), '
                '  location_label=COALESCE(excluded.location_label, sessions.location_label), '
                '  postcode=COALESCE(excluded.postcode, sessions.postcode), '
                '  lat=COALESCE(excluded.lat, sessions.lat), '
                '  lng=COALESCE(excluded.lng, sessions.lng), '
                '  notes=COALESCE(excluded.notes, sessions.notes), '
                '  imported_at=datetime(\'now\')',
                (date, serial, recorder_name, location_label, postcode, lat, lng, notes)
            )
            wx = sess.get('wx')
            if wx:
                # 'hj' is the hourly blob, carried by a WP9+ peer; an older
                # payload has no such key. COALESCE both ways so a summary-only
                # payload can never erase an hourly series already stored here
                # (fetched locally, or received from the peer earlier).
                conn.execute(
                    'INSERT INTO weather (date, instrument_serial, wind_speed, wind_dir, '
                    '                     temp_min, temp_max, precip, hourly_json) '
                    'VALUES (?,?,?,?,?,?,?,?) '
                    'ON CONFLICT(date, instrument_serial) DO UPDATE SET '
                    '  wind_speed=excluded.wind_speed, wind_dir=excluded.wind_dir, '
                    '  temp_min=excluded.temp_min,     temp_max=excluded.temp_max, '
                    '  precip=excluded.precip, '
                    '  hourly_json=COALESCE(excluded.hourly_json, weather.hourly_json)',
                    (date, serial, wx.get('ws'), wx.get('wd'), wx.get('tn'),
                     wx.get('tx'), wx.get('pr'), wx.get('hj'))
                )
            # Importing a date supersedes any earlier deletion of it, so drop the
            # tombstone — otherwise a legitimate SD-card re-import would be deleted
            # again the next time a peer replayed its full sync payload.
            conn.execute('DELETE FROM deleted_sessions WHERE date=? AND instrument_serial=?',
                         (date, serial))
            sess_id = conn.execute(
                'SELECT id FROM sessions WHERE date=? AND instrument_serial=?',
                (date, serial)).fetchone()['id']

            touched = []   # run ids written by this payload
            for i, proj in enumerate(projects, 1):
                touched.append(_upsert_run(conn, sess_id, i, proj))

            if sess.get('complete_date') and touched:
                # The caller vouched for the whole date (explicit prune — see
                # the docstring), so anything stored for the date that was not
                # in the payload no longer exists on the card — except the
                # files the parser saw and could not read, which are still there.
                keep_files = [f for f in (sess.get('skipped_files') or []) if f]
                ph_ids = ','.join('?' * len(touched))
                sql = (f'SELECT id, source_file FROM runs '
                       f'WHERE session_id=? AND id NOT IN ({ph_ids})')
                args = [sess_id, *touched]
                if keep_files:
                    sql += f' AND (source_file IS NULL OR source_file NOT IN ({",".join("?" * len(keep_files))}))'
                    args += keep_files
                doomed = conn.execute(sql, args).fetchall()
                if doomed:
                    # F5: the links first, via the stable key, in this same
                    # transaction — with uid tombstones so the removal
                    # replicates instead of resurrecting (the exact treatment
                    # delete_session() gives its links). A legacy run with no
                    # source_file has no stable key to match; its links, if
                    # any, are what audit_assessment_run_keys() reports.
                    link_uids = []
                    for row in doomed:
                        if not row['source_file']:
                            continue
                        link_uids += [u[0] for u in conn.execute(
                            'SELECT uid FROM assessment_runs WHERE session_date=? '
                            'AND instrument_serial=? AND source_file=? '
                            'AND uid IS NOT NULL',
                            (date, serial, row['source_file'])).fetchall()]
                        deleted_links += conn.execute(
                            'DELETE FROM assessment_runs WHERE session_date=? '
                            'AND instrument_serial=? AND source_file=?',
                            (date, serial, row['source_file'])).rowcount
                    record_uid_tombstones(conn, 'assessment_runs', link_uids)
                    conn.execute(
                        f'DELETE FROM runs WHERE id IN ({",".join("?" * len(doomed))})',
                        [row['id'] for row in doomed])
                    deleted_run_files += [(date, row['source_file']) for row in doomed]

            _renumber_session_runs(conn, sess_id)
            dates.append(date)
            imported += 1
        if dates:
            _recompute_session_aggregates(conn, dates)
        conn.commit()
    finally:
        # One transaction per call: a constraint failure part-way through
        # leaves nothing behind, and the connection is released even then.
        conn.close()
    if deleted_run_files:
        logging.getLogger(__name__).info(
            'import pruned %d run(s) (%s) and %d assessment link(s)',
            len(deleted_run_files),
            ', '.join(f'{d}/{sf or "?"}' for d, sf in deleted_run_files),
            deleted_links)
    result = ImportResult(imported)
    result.deleted_runs = len(deleted_run_files)
    result.deleted_links = deleted_links
    result.deleted_run_files = tuple(deleted_run_files)
    return result


_PROF_COLS = [
    'prof_lafspl_json', 'prof_laeq_json', 'prof_lafmax_json',
    'prof_lae_json', 'prof_lapeak_json',
]


# Every run column written by import_sessions(), in INSERT order, paired with
# the payload key that feeds it. A key of None means the value is computed in
# _upsert_run(); `coalesce` marks the GLOB/PROF-derived columns that an older
# peer sends as NULL, which must never overwrite a value already backfilled.
_RUN_SCALAR_KEYS = (
    'lceq', 'lae', 'lce', 'lafmax', 'lcfmax', 'lafmin', 'lcfmin',
    'lasmax', 'lcsmax', 'lasmin', 'lcsmin', 'laieq', 'lcieq',
    'laimax', 'lcimax', 'laimin', 'lcimin', 'laie', 'lcie',
    'la_l01', 'la_l1', 'la_l5', 'la_l10', 'la_l50', 'la_l90', 'la_l95', 'la_l99',
    'lapeak', 'lcpeak',
    'lc_l01', 'lc_l1', 'lc_l5', 'lc_l10', 'lc_l50', 'lc_l90', 'lc_l95', 'lc_l99',
)
_RUN_SPEC_KEYS = tuple(col for col, _ in SPECTRAL_TABLES)
_RUN_COALESCE_COLS = (_RUN_SCALAR_KEYS + _RUN_SPEC_KEYS + tuple(_PROF_COLS)
                      + ('duration_s', 'full_scale', 'end_time'))
_RUN_PLAIN_COLS = (
    'start_time', 'n_samples', 'step', 'avg_laeq', 'min_laeq', 'max_laeq',
    'max_lcpeak', 'max_laimax', 'laeq_json', 'lafmax_json', 'laimax_json',
    'lcpeak_json',
)


def _run_row_values(proj):
    """Column -> value for one payload run, excluding the identity columns."""
    _g = proj.get

    def _json_or_none(key):
        v = _g(key)
        return json.dumps(v) if v else None

    vals = {
        'start_time': proj['start'], 'n_samples': proj['n'],
        'step': proj.get('step', 1),
        'avg_laeq': proj['avg'], 'min_laeq': proj['mn'], 'max_laeq': proj['mx'],
        'max_lcpeak': proj['pmx'], 'max_laimax': _g('pmxi'),
        'laeq_json':   json.dumps(_g('laeq_profile') or []),
        'lafmax_json': _json_or_none('lafmax_profile'),
        'laimax_json': _json_or_none('laimax_profile'),
        'lcpeak_json': json.dumps(_g('lcpeak_profile') or []),
    }
    for k in _RUN_SCALAR_KEYS:
        vals[k] = _g(k)
    for k in _RUN_SPEC_KEYS:
        vals[k] = _json_or_none(k)
    for k in _PROF_COLS:
        vals[k] = _json_or_none(k)
    vals['duration_s'] = _g('duration_s')
    vals['full_scale'] = _g('full_scale')
    vals['end_time'] = _g('end_time')
    return vals


def _upsert_sql(conflict):
    cols = ('session_id', 'run_number', 'source_file') + _RUN_PLAIN_COLS + _RUN_COALESCE_COLS
    sets = [f'{c}=excluded.{c}' for c in _RUN_PLAIN_COLS]
    # GLOB-derived scalar, spectral and PROF columns: COALESCE so that a push
    # from older code (which sends NULL) never overwrites a backfilled value.
    sets += [f'{c}=COALESCE(excluded.{c},runs.{c})' for c in _RUN_COALESCE_COLS]
    if conflict == 'source_file':
        target = 'ON CONFLICT(session_id, source_file) WHERE source_file IS NOT NULL'
        # run_number is left as stored; _renumber_session_runs() assigns it.
    else:
        target = 'ON CONFLICT(session_id, run_number)'
        # A legacy row names no source_file, so keep whatever the stored row
        # already knows about its identity.
        sets.append('source_file=COALESCE(excluded.source_file,runs.source_file)')
    return (f'INSERT INTO runs ({",".join(cols)}) VALUES ({",".join("?" * len(cols))}) '
            f'{target} DO UPDATE SET {", ".join(sets)}'), cols


_UPSERT_BY_FILE, _RUN_COLS = _upsert_sql('source_file')
_UPSERT_BY_POSITION, _ = _upsert_sql('run_number')


def _upsert_run(conn, sess_id, position, proj):
    """Insert or refresh one run; returns its row id.

    Keyed on source_file when the payload names one. A new row is inserted with
    run_number NULL — the table's UNIQUE(session_id, run_number) would otherwise
    reject it whenever another measurement already holds that position, which
    is exactly the partial-upload case — and _renumber_session_runs() gives it
    its number from the stored order afterwards.
    """
    vals = _run_row_values(proj)
    src = proj.get('source_file') or None
    if src:
        vals.update(session_id=sess_id, run_number=None, source_file=src)
        conn.execute(_UPSERT_BY_FILE, [vals[c] for c in _RUN_COLS])
        return conn.execute(
            'SELECT id FROM runs WHERE session_id=? AND source_file=?',
            (sess_id, src)).fetchone()['id']
    # Positional fallback for payloads that predate source_file.
    vals.update(session_id=sess_id, run_number=position, source_file=None)
    conn.execute(_UPSERT_BY_POSITION, [vals[c] for c in _RUN_COLS])
    return conn.execute(
        'SELECT id FROM runs WHERE session_id=? AND run_number=?',
        (sess_id, position)).fetchone()['id']


def _renumber_session_runs(conn, sess_id):
    """Assign run_number 1..n across a session from the stored order.

    Order is source_file (the meter's PROJnnnn, chronological on the card),
    then start_time for rows without one, then id. Numbers are cleared first:
    UNIQUE(session_id, run_number) is checked row by row, so a direct
    renumber collides with itself whenever a run moves up.
    """
    ids = [r['id'] for r in conn.execute(
        'SELECT id FROM runs WHERE session_id=? '
        'ORDER BY source_file IS NULL, source_file, start_time, id', (sess_id,))]
    conn.execute('UPDATE runs SET run_number=NULL WHERE session_id=?', (sess_id,))
    conn.executemany('UPDATE runs SET run_number=? WHERE id=?',
                     [(n, rid) for n, rid in enumerate(ids, 1)])


def _wx(row, hourly=False):
    """The compact wx dict for a sessions⋈weather row. hourly=True adds 'hj'
    (the raw hourly_json blob, ~24 rows × 4 series) — wanted by the peer-sync
    payload, where it replicates, but not by the session-browser payload,
    which only renders the summary chips."""
    if row['wind_speed'] is None and not (hourly and row['hourly_json']):
        return None
    d = {'ws': row['wind_speed'], 'wd': row['wind_dir'],
         'tn': row['temp_min'],   'tx': row['temp_max'], 'pr': row['precip']}
    if hourly:
        d['hj'] = row['hourly_json']
    return d


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
        'FROM sessions s LEFT JOIN weather w '
        '  ON s.date=w.date AND s.instrument_serial=w.instrument_serial '
        'ORDER BY s.date DESC, s.instrument_serial'
    ).fetchall()
    # Build (session_date, serial) -> list of assessment names (one query, not N)
    sess_assess_rows = conn.execute(
        'SELECT DISTINCT ar.session_date, ar.instrument_serial, a.name '
        'FROM assessment_runs ar JOIN assessments a ON a.id=ar.assessment_id'
    ).fetchall()
    sess_assessments = {}
    for row in sess_assess_rows:
        sess_assessments.setdefault(
            (row['session_date'], row['instrument_serial']), []).append(row['name'])
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
            '  ON ar.session_date=? AND ar.instrument_serial=? AND ('
            '    ar.source_file=r.source_file'
            '    OR (ar.source_file IS NULL AND ar.run_number=r.run_number)) '
            'LEFT JOIN assessment_locations al ON al.id=ar.location_id '
            'WHERE r.session_id=? '
            'GROUP BY r.id ORDER BY r.run_number',
            (sess['date'], sess['instrument_serial'], sess['id'])
        ).fetchall()
        result.append({
            'd':       sess['date'],
            'serial':  sess['instrument_serial'],
            'avg':     sess['avg_laeq'],
            'mx':      sess['max_laeq'],
            'name':    sess['recorder_name'],
            'loc':     sess['location_label'],
            'post':    sess['postcode'],
            'lat':     sess['lat'],
            'lng':     sess['lng'],
            'notes':   sess['notes'],
            'wx':      _wx(sess),
            'assmnts': sess_assessments.get((sess['date'], sess['instrument_serial']), []),
            'projects': [{
                **_run_to_dict(r, full=False),
                'loc_tag': r['location_tag'],
                'assess':  r['assess_locs'],
            } for r in runs],
        })
    conn.close()
    return {'sessions': result}


def save_weather(date, w, serial=None):
    """Store the weather for the (date, serial) session. `serial` None or
    blank means the default serial, which is what every pre-WP9 caller meant.
    A full overwrite, hourly_json included: this is the local-fetch path, and
    a fetch always produces the whole row."""
    conn = get_db()
    conn.execute(
        'INSERT INTO weather (date, instrument_serial, wind_speed, wind_dir, '
        '                     temp_min, temp_max, precip, hourly_json) '
        'VALUES (?,?,?,?,?,?,?,?) '
        'ON CONFLICT(date, instrument_serial) DO UPDATE SET '
        '  wind_speed=excluded.wind_speed, wind_dir=excluded.wind_dir, '
        '  temp_min=excluded.temp_min,     temp_max=excluded.temp_max, '
        '  precip=excluded.precip,         hourly_json=excluded.hourly_json',
        (date, resolve_serial(serial, conn), w.get('wind_speed'), w.get('wind_dir'),
         w.get('temp_min'), w.get('temp_max'),
         w.get('precip'), w.get('hourly_json'))
    )
    conn.commit()
    conn.close()


def get_weather(date, serial=None):
    conn = get_db()
    row = conn.execute(
        'SELECT * FROM weather WHERE date=? AND instrument_serial=?',
        (date, resolve_serial(serial, conn))).fetchone()
    conn.close()
    return dict(row) if row else None


def get_existing_dates(serial=None):
    """Return a set of session dates already in the database — for one
    instrument when `serial` is given (blank means the default serial), else
    across every instrument."""
    conn = get_db()
    if serial is None:
        rows = conn.execute('SELECT date FROM sessions').fetchall()
    else:
        rows = conn.execute('SELECT date FROM sessions WHERE instrument_serial=?',
                            (resolve_serial(serial, conn),)).fetchall()
    conn.close()
    return {r['date'] for r in rows}


def get_existing_run_starts():
    """Return dict mapping (date, serial) -> set of start_times already stored."""
    conn = get_db()
    rows = conn.execute(
        'SELECT s.date, s.instrument_serial, r.start_time '
        'FROM runs r JOIN sessions s ON r.session_id = s.id'
    ).fetchall()
    conn.close()
    result = {}
    for row in rows:
        result.setdefault((row['date'], row['instrument_serial']), set()).add(row['start_time'])
    return result


def get_sessions_since(since):
    """Return sessions imported/updated at or after `since`, in sync format.

    >= rather than > (WP12/F2): the puller's watermark is now this sender's
    own clock (the `server_now` /api/sync returns), so a session whose
    imported_at equals the watermark to the second sits exactly on the
    boundary — with strict >, it was never sent again. Re-sending a boundary
    session is harmless: import_sessions is a stable-key idempotent upsert."""
    conn = get_db()
    sessions = conn.execute(
        'SELECT s.*, w.wind_speed, w.wind_dir, w.temp_min, w.temp_max, w.precip, '
        '       w.hourly_json '
        'FROM sessions s LEFT JOIN weather w '
        '  ON s.date=w.date AND s.instrument_serial=w.instrument_serial '
        'WHERE s.imported_at >= ? ORDER BY s.date, s.instrument_serial', (since,)
    ).fetchall()
    result = []
    for sess in sessions:
        runs = conn.execute(
            'SELECT * FROM runs WHERE session_id=? ORDER BY run_number', (sess['id'],)
        ).fetchall()
        result.append({
            'd':     sess['date'],
            'serial': sess['instrument_serial'],
            'avg':   sess['avg_laeq'],
            'mx':    sess['max_laeq'],
            'name':  sess['recorder_name'],
            'loc':   sess['location_label'],
            'post':  sess['postcode'],
            'lat':   sess['lat'],
            'lng':   sess['lng'],
            'notes': sess['notes'],
            'wx':    _wx(sess, hourly=True),
            'projects': [_run_to_dict(r, full=True) for r in runs],
        })
    conn.close()
    return result


def get_all_sessions_list():
    """Return all sessions as dicts for the manage page (no run data)."""
    conn = get_db()
    rows = conn.execute(
        'SELECT date, instrument_serial, run_count, avg_laeq, max_laeq, '
        '       recorder_name, location_label, postcode, lat, lng, notes '
        'FROM sessions ORDER BY date DESC, instrument_serial'
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        # A DOM-safe handle for the row: the date alone no longer identifies a
        # session once two meters measured on the same day.
        d['key'] = d['date'] + (('-' + d['instrument_serial']) if d['instrument_serial'] else '')
        out.append(d)
    return out


def update_session_metadata(date, recorder_name, location_label, postcode, lat, lng,
                            notes=None, serial=None):
    """Set the session's hand-entered metadata, stamping meta_updated_at /
    meta_writer (WP12/F4) so the edit replicates under the LWW gate instead
    of the old unordered COALESCE merge. Returns the stamps so the caller can
    put them in the sync event it pushes to the peer."""
    conn = get_db()
    ts = conn.execute(f'SELECT {LWW_NOW_SQL}').fetchone()[0]
    writer = local_writer()
    conn.execute(
        'UPDATE sessions SET recorder_name=?, location_label=?, postcode=?, lat=?, lng=?, notes=?, '
        '  meta_updated_at=?, meta_writer=? '
        'WHERE date=? AND instrument_serial=?',
        (recorder_name or None, location_label or None,
         postcode or None, lat, lng, notes or None, ts, writer,
         date, resolve_serial(serial, conn))
    )
    conn.commit()
    conn.close()
    return {'meta_updated_at': ts, 'meta_writer': writer}


def _session_id(conn, date, serial):
    """The id of the (date, serial) session, or None. `serial` may be None or
    blank, meaning the default."""
    row = conn.execute(
        'SELECT id FROM sessions WHERE date=? AND instrument_serial=?',
        (date, resolve_serial(serial, conn))).fetchone()
    return row['id'] if row else None


def get_full_run_row(date, run_number, serial=None):
    """Return all columns for a single run (including spec_ and prof_ JSON columns).
    Adds 'session_date' and 'instrument_serial' so callers can build full
    datetimes from start_time and name the instrument in exports."""
    conn = get_db()
    serial = resolve_serial(serial, conn)
    sid = _session_id(conn, date, serial)
    if sid is None:
        conn.close()
        return None
    row = conn.execute(
        'SELECT * FROM runs WHERE session_id=? AND run_number=?',
        (sid, run_number)
    ).fetchone()
    conn.close()
    if row is None:
        return None
    result = dict(row)
    result['session_date'] = date
    result['instrument_serial'] = serial
    return result


def get_session_prof_lafspl(date, run_numbers=None, serial=None):
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
    sid = _session_id(conn, date, serial)
    if sid is None:
        conn.close()
        return []
    if run_numbers:
        ph = ','.join('?' * len(run_numbers))
        rows = conn.execute(
            f'SELECT prof_lafspl_json FROM runs WHERE session_id=? AND run_number IN ({ph})',
            (sid, *run_numbers)
        ).fetchall()
    else:
        rows = conn.execute(
            'SELECT prof_lafspl_json FROM runs WHERE session_id=?', (sid,)
        ).fetchall()
    conn.close()
    pooled = []
    for r in rows:
        if r['prof_lafspl_json']:
            pooled.extend(json.loads(r['prof_lafspl_json']))
    return pooled


def get_run_prof_laeq(date, run_number, serial=None):
    """Return the full 1-second LAeq series for one run, or [] if not stored.

    The session browser payload only carries the downsampled chart profile, which
    holds the maximum of each window. Anything counting samples against a
    threshold must read the real series instead — counting the expanded chart
    profile overstated time above 85 dB by up to the downsample step.
    """
    conn = get_db()
    row = conn.execute(
        'SELECT r.prof_laeq_json FROM runs r JOIN sessions s ON r.session_id = s.id '
        'WHERE s.date = ? AND s.instrument_serial = ? AND r.run_number = ?',
        (date, resolve_serial(serial, conn), run_number)).fetchone()
    conn.close()
    if not row or not row['prof_laeq_json']:
        return []
    return json.loads(row['prof_laeq_json'])


def get_run_prof_by_source(date, source_file, serial=None):
    """Return the stored 1-second PROF series for one run, keyed on the stable
    identity (`source_file`, e.g. 'PROJ0003') rather than its position.

    `run_number` is assigned from the session order and moves when a session is
    re-imported or a run is inserted; `source_file` is the NOR140 project folder
    and does not. Anything a page fetches after render must use the stable key,
    or a re-import between render and fetch silently swaps the measurement.

    Returns None when the run does not exist, so the caller can 404 rather than
    present an empty chart as if it were data. Returns a dict whose
    'prof_laeq'/'prof_lafspl' are [] when the run exists but the series was
    never stored (peer-synced sessions predating the PROF columns) — the caller
    must then say so rather than substituting the downsampled chart profile
    unlabelled.
    """
    conn = get_db()
    row = conn.execute(
        'SELECT r.run_number, r.n_samples, r.step, r.prof_laeq_json, r.prof_lafspl_json '
        'FROM runs r JOIN sessions s ON r.session_id = s.id '
        'WHERE s.date = ? AND s.instrument_serial = ? AND r.source_file = ?',
        (date, resolve_serial(serial, conn), source_file)).fetchone()
    conn.close()
    if row is None:
        return None
    return {
        'source_file': source_file,
        'run_number':  row['run_number'],
        'n':           row['n_samples'],
        'step':        row['step'],
        'prof_laeq':   json.loads(row['prof_laeq_json']) if row['prof_laeq_json'] else [],
        'prof_lafspl': json.loads(row['prof_lafspl_json']) if row['prof_lafspl_json'] else [],
    }


def update_run_location_tag(date, run_number, tag, serial=None, source_file=None):
    """Set a run's location tag. The run is named by source_file when given
    (its stable identity), else by its current run_number.

    Stamps tag_updated_at / tag_writer (WP12/F4) so the edit replicates under
    the LWW gate; returns the stamps for the caller's sync event."""
    conn = get_db()
    sid = _session_id(conn, date, serial)
    ts = conn.execute(f'SELECT {LWW_NOW_SQL}').fetchone()[0]
    writer = local_writer()
    if source_file:
        conn.execute('UPDATE runs SET location_tag=?, tag_updated_at=?, tag_writer=? '
                     'WHERE session_id=? AND source_file=?',
                     (tag or None, ts, writer, sid, source_file))
    else:
        conn.execute('UPDATE runs SET location_tag=?, tag_updated_at=?, tag_writer=? '
                     'WHERE session_id=? AND run_number=?',
                     (tag or None, ts, writer, sid, run_number))
    conn.commit()
    conn.close()
    return {'tag_updated_at': ts, 'tag_writer': writer}


def delete_session(date, serial=None):
    """Delete the (date, serial) session and its runs (cascaded via FK) plus
    its assessment assignments — assessment_runs.session_date is a plain text
    column, not a declared foreign key, so it can't cascade automatically.
    `serial` None or blank means the default serial, which is also how a
    peer's pre-WP8 delete event (date only) is applied.

    Records a tombstone so the deletion replicates to a peer that was offline
    when it happened. The session's weather row — (date, serial), the same key
    as the session itself since WP9 — is deliberately left in place: it is
    reference data rather than measurement data, and a re-import of the same
    card should find it still there rather than trigger a re-fetch. Only
    purge_sessions_before(), the bulk data-retention path, removes weather."""
    conn = get_db()
    serial = resolve_serial(serial, conn)
    # The links removed with the session get uid tombstones (F6): without
    # them, a full sync from a Pi still holding this session's assessment
    # links would re-create dangling rows here after the delete.
    ar_uids = [r[0] for r in conn.execute(
        'SELECT uid FROM assessment_runs WHERE session_date=? AND '
        'instrument_serial=? AND uid IS NOT NULL', (date, serial)).fetchall()]
    conn.execute('DELETE FROM assessment_runs WHERE session_date=? AND instrument_serial=?',
                 (date, serial))
    conn.execute('DELETE FROM sessions WHERE date=? AND instrument_serial=?', (date, serial))
    _record_tombstones(conn, [(date, serial)])
    record_uid_tombstones(conn, 'assessment_runs', ar_uids)
    conn.commit()
    conn.close()


def _record_tombstones(conn, keys, deleted_at=None):
    """Mark (date, serial) sessions as deleted. Caller commits. A bare date in
    `keys` means the default serial.

    deleted_at is supplied when replaying a peer's tombstone, so the original
    deletion time is preserved as it propagates; the MAX() keeps the newest
    time if the same session is deleted on both Pis."""
    for key in keys:
        date, serial = key if isinstance(key, (tuple, list)) else (key, None)
        serial = resolve_serial(serial, conn)
        if deleted_at is None:
            conn.execute(
                "INSERT INTO deleted_sessions (date, instrument_serial) VALUES (?,?) "
                "ON CONFLICT(date, instrument_serial) DO UPDATE SET deleted_at=datetime('now')",
                (date, serial))
        else:
            conn.execute(
                'INSERT INTO deleted_sessions (date, instrument_serial, deleted_at) VALUES (?,?,?) '
                'ON CONFLICT(date, instrument_serial) DO UPDATE SET '
                '  deleted_at=MAX(deleted_sessions.deleted_at, excluded.deleted_at)',
                (date, serial, deleted_at))


def get_session_tombstones():
    """Return [{date, serial, deleted_at}] for every deleted session."""
    conn = get_db()
    rows = conn.execute(
        'SELECT date, instrument_serial AS serial, deleted_at FROM deleted_sessions '
        'ORDER BY date, instrument_serial').fetchall()
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
        SELECT ar.id, ar.assessment_id, ar.session_date, ar.instrument_serial,
               ar.run_number, ar.source_file, a.name AS assessment_name
        FROM assessment_runs ar
        LEFT JOIN assessments a ON a.id = ar.assessment_id
        ORDER BY ar.session_date, ar.run_number
    ''').fetchall()]

    report = {'null_source_file': [], 'unmatched': [], 'duplicate': [], 'ambiguous': []}
    seen = {}
    for r in rows:
        if not r['source_file']:
            report['null_source_file'].append(r)
            continue
        # Count, don't just look: one match is healthy, none is unmatched, and
        # more than one means the stable key does not identify a single run.
        n_hits = conn.execute(
            'SELECT COUNT(*) FROM runs rn JOIN sessions se ON se.id = rn.session_id '
            'WHERE se.date=? AND se.instrument_serial=? AND rn.source_file=?',
            (r['session_date'], r['instrument_serial'], r['source_file'])).fetchone()[0]
        if n_hits == 0:
            report['unmatched'].append(r)
        elif n_hits > 1:
            report['ambiguous'].append(dict(r, matching_runs=n_hits))
        key = (r['assessment_id'], r['session_date'], r['instrument_serial'], r['source_file'])
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

    Deliberately uses the same formula as noise_parser.parse_zip(), so that a
    backfill and a re-import of the same data produce identical session rows.
    That formula is a *duration-weighted* energy average of the per-run LAeq
    (weight: the meter's duration_s, falling back to n_samples) — the same
    definition the reports have always used, now the only one. It was an
    equal-weight mean here and in parse_zip, so a day of mixed-length runs read
    differently in the session list than in its own report.

    Returns [(date, old_avg, new_avg)] for the sessions whose values changed.

    import_sessions() calls the connection-taking form below after every
    import, so the session row is derived from the stored runs rather than
    from the payload, which may have been a partial upload.
    """
    conn = get_db()
    changed = _recompute_session_aggregates(conn, dates)
    conn.commit()
    conn.close()
    return changed


def _recompute_session_aggregates(conn, dates=None):
    """recompute_session_aggregates() on a caller-owned connection; caller commits."""
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
            'SELECT avg_laeq, max_laeq, duration_s, n_samples FROM runs '
            'WHERE session_id=?', (s['id'],)
        ).fetchall()
        if not runs:
            continue
        weighted = [(r['avg_laeq'], run_weight_s(dict(r)))
                    for r in runs if r['avg_laeq'] is not None]
        maxes = [r['max_laeq'] for r in runs if r['max_laeq'] is not None]
        if not weighted:
            continue
        new_avg = round_half_up(_energy_avg_weighted(weighted), 2)
        new_max = round_half_up(max(maxes), 1) if maxes else s['max_laeq']
        if (s['avg_laeq'] != new_avg or s['max_laeq'] != new_max
                or s['run_count'] != len(runs)):
            conn.execute(
                'UPDATE sessions SET avg_laeq=?, max_laeq=?, run_count=? WHERE id=?',
                (new_avg, new_max, len(runs), s['id']))
            changed.append((s['date'], s['avg_laeq'], new_avg))
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
            f'SELECT * FROM sessions WHERE date IN ({ph}) ORDER BY date, instrument_serial', dates
        ).fetchall()
    else:
        sessions_rows = conn.execute(
            'SELECT * FROM sessions ORDER BY date, instrument_serial').fetchall()

    result = []
    for sess in sessions_rows:
        runs = conn.execute(
            'SELECT * FROM runs WHERE session_id=? ORDER BY run_number', (sess['id'],)
        ).fetchall()
        projects = [_run_to_dict(r, full=True) for r in runs]
        result.append({
            'd': sess['date'],
            'serial': sess['instrument_serial'],
            'avg': sess['avg_laeq'],
            'mx': sess['max_laeq'],
            'projects': projects,
        })
    conn.close()
    return result


def purge_sessions_before(before_date):
    """Delete all sessions (and associated data) older than before_date (YYYY-MM-DD)."""
    conn = get_db()
    keys = [(r[0], r[1]) for r in conn.execute(
        'SELECT date, instrument_serial FROM sessions WHERE date < ?',
        (before_date,)).fetchall()]
    old = sorted({d for d, _ in keys})
    if old:
        ph = ','.join('?' * len(old))
        ar_uids = [r[0] for r in conn.execute(
            f'SELECT uid FROM assessment_runs WHERE session_date IN ({ph}) '
            'AND uid IS NOT NULL', old).fetchall()]
        conn.execute(f'DELETE FROM assessment_runs WHERE session_date IN ({ph})', old)
        # Weather goes with the purge. The table is keyed (date, serial) since
        # WP9 (_migrate rebuilds any older variant before this can run), but
        # the purge is date-scoped by definition — every serial's session
        # before the cutoff is going — so deleting by date also sweeps up
        # rows whose session was individually deleted earlier (delete_session
        # keeps weather as reference data; the retention purge does not).
        conn.execute(f'DELETE FROM weather WHERE date IN ({ph})', old)
        conn.execute(
            f'DELETE FROM runs WHERE session_id IN (SELECT id FROM sessions WHERE date IN ({ph}))', old)
        conn.execute(f'DELETE FROM sessions WHERE date IN ({ph})', old)
        _record_tombstones(conn, keys)
        record_uid_tombstones(conn, 'assessment_runs', ar_uids)
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
