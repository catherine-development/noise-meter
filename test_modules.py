#!/usr/bin/env python3
"""
Empirical tests for the noise-meter data layer, run against real temporary
SQLite databases built from the MEAS118 reference SD-card files.

These are deliberately not import-smoke tests: every case drives real data
through the real code path and checks values, because the bugs this codebase
has actually shipped (a placeholder count off by one, a percentile reading the
wrong channel, a sync path dropping metadata) all compiled cleanly and read
correctly.

Replication is tested with two genuinely independent copies of the data layer,
each bound to its own database file, so a "peer" really is a separate Pi.

    python3 test_modules.py [path/to/MEAS118]

Exits non-zero on the first failure.
"""
import importlib
import json
import os
import shutil
import sys
import tempfile

REPO = os.path.dirname(os.path.abspath(__file__))
MEAS_DEFAULT = os.path.join(REPO, 'MEAS118')
DATE_FOLDER = '260812'
SESSION_DATE = '2026-08-12'

_checks = 0


def check(cond, label, detail=''):
    global _checks
    _checks += 1
    if not cond:
        print(f'  FAIL  {label}')
        if detail:
            print(f'        {detail}')
        raise SystemExit(1)
    print(f'  ok    {label}' + (f'  ({detail})' if detail else ''))


def close(a, b, tol, label):
    check(a is not None and abs(a - b) <= tol, label, f'got {a!r}, expected {b} ±{tol}')


class Side:
    """One Pi: an independent import of the data layer bound to its own DB."""

    def __init__(self, db_path):
        self.db_path = db_path
        os.environ['NOISE_DB_PATH'] = db_path
        for name in ('noise_db', 'reports_db', 'assessments_db', 'sync_db', 'reports'):
            sys.modules.pop(name, None)
        self.db = importlib.import_module('noise_db')
        assert self.db.DB_PATH == db_path, self.db.DB_PATH
        self.sync = importlib.import_module('sync_db')
        self.assess = importlib.import_module('assessments_db')
        self.reports_db = importlib.import_module('reports_db')
        self.reports = importlib.import_module('reports')
        self.db.init_db()

    def sql(self, query, *params):
        conn = self.db.get_db()
        rows = conn.execute(query, params).fetchall()
        conn.close()
        return rows

    def exec(self, query, *params):
        conn = self.db.get_db()
        conn.execute(query, params)
        conn.commit()
        conn.close()

    def exec_nofk(self, query, *params):
        """Write with foreign keys OFF — how orphaned rows arise in the first place.
        noise_db.get_db() always enables them, so an orphan cannot be created
        through it; this reproduces the connection that caused the real ones."""
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.execute(query, params)
        conn.commit()
        conn.close()

    def has_session(self, date):
        return bool(self.sql('SELECT 1 FROM sessions WHERE date=?', date))

    def tombstones(self):
        return {r['date']: r['deleted_at'] for r in self.sql(
            'SELECT date, deleted_at FROM deleted_sessions')}

    def set_imported_at(self, date, ts):
        self.exec('UPDATE sessions SET imported_at=? WHERE date=?', ts, date)


REF_DIR = os.path.expanduser('~/Downloads/nortfr')

# Two things Nortfr emits that we cannot yet reproduce. Everything else must match
# exactly, so these are named narrowly rather than tolerated as a diff budget.
#   - the per-period marker column ('Pause', or the code 4), present only in the
#     2668-byte GLOB variant; its extra block is decoded only as far as the last
#     marked period, so the marked region cannot be rebuilt.
#   - Sensitivity, which varies per measurement (-26.9 / -27.0 / -27.4 seen) and
#     whose offset has not been located.
def _is_known_gap(sheet, row, col, gen, ref, ncols):
    if sheet == 'Summary' and gen is None and (ref == 'Pause' or ref == 4):
        return True                       # marker column
    if sheet == 'Setup' and row == 14:
        return True                       # Sensitivity
    return False


def check_references(meas_root, tmp):
    """Diff our export against every reference pair in ~/Downloads/nortfr."""
    import collections
    import glob as _glob
    import io
    import re as _re
    from openpyxl import load_workbook
    from nor140_exporter import build_global_xlsx, build_profile_xlsx
    from noise_parser import parse_files

    pairs = collections.defaultdict(dict)
    for p in _glob.glob(os.path.join(REF_DIR, '*.xlsx')):
        m = _re.search(r'_(\d{6})_(\d{4})_(GLOBAL|PROFILE)(-\d+)?\.xlsx$', os.path.basename(p))
        if m and not m.group(4):
            pairs[(m.group(1), int(m.group(2)))][m.group(3)] = p
    if not pairs:
        print('  SKIP — no Nortfr reference workbooks in ~/Downloads/nortfr')
        return

    def cells(obj):
        wb = load_workbook(io.BytesIO(obj) if isinstance(obj, bytes) else obj, data_only=True)
        out = {ws.title: [list(r) for r in ws.iter_rows(values_only=True)] for ws in wb.worksheets}
        wb.close()
        return out

    by_date = collections.defaultdict(list)
    for d, rn in sorted(pairs):
        by_date[d].append(rn)

    for date_folder in sorted(by_date):
        root = os.path.join(meas_root, date_folder, 'PART0000')
        if not os.path.isdir(root):
            print(f'  SKIP {date_folder} — not in MEAS118')
            continue
        side = Side(os.path.join(tmp, f'ref-{date_folder}.db'))
        files = []
        for proj in sorted(os.listdir(root)):
            d = os.path.join(root, proj)
            if os.path.isdir(d):
                for fn in sorted(os.listdir(d)):
                    with open(os.path.join(d, fn), 'rb') as fh:
                        files.append((f'{date_folder}/PART0000/{proj}/{fn}', fh.read()))
        side.db.import_sessions(parse_files(files))
        iso = f'20{date_folder[0:2]}-{date_folder[2:4]}-{date_folder[4:6]}'

        for rn in by_date[date_folder]:
            # Pair on source_file: run_number is a sequential import index and
            # diverges from the PROJ folder number on dates with gaps.
            rows = side.sql('SELECT r.* FROM runs r JOIN sessions s ON r.session_id=s.id '
                            'WHERE s.date=? AND UPPER(r.source_file)=?', iso, 'PROJ%04d' % rn)
            check(bool(rows), f'{date_folder} run {rn}: present in the DB')
            run = dict(rows[0])
            run['session_date'] = iso
            for label, built in (('GLOBAL', build_global_xlsx(run, '6899108')),
                                 ('PROFILE', build_profile_xlsx(run, '6899108'))):
                if label not in pairs[(date_folder, rn)]:
                    continue
                gen, ref = cells(built), cells(pairs[(date_folder, rn)][label])
                check(list(gen) == list(ref),
                      f'{date_folder}/{rn} {label}: sheet names and order match')
                unexpected = []
                for name in ref:
                    g, r_ = gen.get(name, []), ref[name]
                    for i in range(max(len(g), len(r_))):
                        gr = g[i] if i < len(g) else []
                        rr = r_[i] if i < len(r_) else []
                        for j in range(max(len(gr), len(rr))):
                            gv = gr[j] if j < len(gr) else None
                            rv = rr[j] if j < len(rr) else None
                            if gv == rv:
                                continue
                            if isinstance(gv, float) and isinstance(rv, float) and abs(gv - rv) < 1e-9:
                                continue
                            if _is_known_gap(name, i + 1, j + 1, gv, rv, len(rr)):
                                continue
                            unexpected.append(f'{name} r{i+1}c{j+1}: {gv!r} vs {rv!r}')
                check(not unexpected,
                      f'{date_folder}/{rn} {label}: no unexplained differences',
                      '; '.join(unexpected[:3]))


def read_sd_pairs(meas_root):
    """Read the real GLOB/PROF files for one date folder off the SD-card tree."""
    root = os.path.join(meas_root, DATE_FOLDER, 'PART0000')
    if not os.path.isdir(root):
        print(f'SKIP: no reference data at {root}')
        raise SystemExit(0)
    pairs = []
    for proj in sorted(os.listdir(root)):
        pdir = os.path.join(root, proj)
        if not os.path.isdir(pdir):
            continue
        for fn in sorted(os.listdir(pdir)):
            with open(os.path.join(pdir, fn), 'rb') as fh:
                pairs.append((f'{DATE_FOLDER}/PART0000/{proj}/{fn}', fh.read()))
    return pairs


META = {
    'recorder_name': 'Catherine Ives-Yim',
    'location_label': 'Reference site',
    'postcode': 'LS1 1AA', 'lat': 53.7997, 'lng': -1.5492,
    'notes': 'a note that must survive the hop',
}


def main(meas_root):
    pairs = read_sd_pairs(meas_root)
    tmp = tempfile.mkdtemp(prefix='noisetest-')
    sys.path.insert(0, REPO)
    try:
        from noise_parser import parse_files
        from nor140_format import SPECTRAL_TABLES
        sessions = parse_files(pairs)

        # ── 1. import_sessions() end to end ───────────────────────────────────
        print('\n1. import_sessions() from real SD-card binaries')
        a = Side(os.path.join(tmp, 'a.db'))
        n = a.db.import_sessions(sessions, metadata=META)
        check(n == len(sessions) == 1, 'one session imported', f'n={n}')

        data = a.db.get_all_sessions_json()['sessions']
        check(len(data) == 1, 'one session read back')
        sess = data[0]
        check(sess['d'] == SESSION_DATE, 'session date', sess['d'])
        n_runs = len(sess['projects'])
        check(n_runs == len(sessions[0]['projects']), 'run count round-trips',
              f'{n_runs} runs')
        check(sess['name'] == 'Catherine Ives-Yim', 'recorder_name stored')
        check(sess['post'] == 'LS1 1AA', 'postcode stored')

        # Run 9 against the Nortfr-confirmed reference values from NOR140_handoff.md.
        # This exercises parse -> INSERT -> SELECT for the GLOB scalar columns;
        # an INSERT placeholder miscount or column-order slip shows up here.
        run9 = a.db.get_full_run_row(SESSION_DATE, 9)
        check(run9 is not None, 'run 9 present')
        close(run9['avg_laeq'], 70.8, 0.05, 'run 9 LAeq  = 70.8 (Nortfr)')
        close(run9['lceq'],     78.7, 0.05, 'run 9 LCeq  = 78.7 (Nortfr)')
        close(run9['lapeak'],   97.4, 0.05, 'run 9 LApeak= 97.4 (Nortfr)')
        close(run9['lcpeak'],   98.6, 0.05, 'run 9 LCpeak= 98.6 (Nortfr)')
        close(run9['la_l10'],   74.8, 0.05, 'run 9 LA10  = 74.8 (Nortfr)')
        close(run9['la_l50'],   67.1, 0.05, 'run 9 LA50  = 67.1 (Nortfr)')
        close(run9['la_l90'],   57.1, 0.05, 'run 9 LA90  = 57.1 (Nortfr)')
        check(run9['n_samples'] == 900, 'run 9 is 900 s', str(run9['n_samples']))

        missing = [c for c, _ in SPECTRAL_TABLES if not run9.get(c)]
        check(not missing, 'all 18 spectral tables stored', f'missing: {missing}')
        for col, _ in SPECTRAL_TABLES:
            check(len(json.loads(run9[col])) == 36, f'{col} has 36 bands')
        for col in ('prof_lafspl_json', 'prof_laeq_json', 'prof_lafmax_json',
                    'prof_lae_json', 'prof_lapeak_json'):
            check(len(json.loads(run9[col])) == 900, f'{col} has 900 samples')

        # ── 2. LA10/LA90 pooled percentile ────────────────────────────────────
        print('\n2. pooled LA10/LA90 percentile across runs')
        pooled = a.db.get_session_prof_lafspl(SESSION_DATE, [9])
        check(len(pooled) == 900, 'run 9 pools 900 samples', str(len(pooled)))
        check(pooled == json.loads(run9['prof_lafspl_json']),
              'pooling reads PROF field 0 (LAFspl), not field 1 (LAeq,1s)')
        check(pooled != json.loads(run9['prof_laeq_json']),
              'LAFspl and LAeq,1s are genuinely different series')

        multi = a.db.get_session_prof_lafspl(SESSION_DATE, [9, 10])
        check(len(multi) == 1800, 'two runs pool 1800 samples', str(len(multi)))
        every = a.db.get_session_prof_lafspl(SESSION_DATE)
        check(len(every) == sum(p['n'] for p in sess['projects']),
              'no run_numbers pools every run', f'{len(every)} samples')

        # _percentile interpolates over an ascending list: LA90 is the level
        # exceeded 90% of the time, i.e. the 10th percentile.
        s = sorted(pooled)
        la90, la50, la10 = (a.reports._percentile(s, p) for p in (10, 50, 90))
        check(la90 < la50 < la10, 'LA90 < LA50 < LA10', f'{la90} / {la50} / {la10}')

        def naive(sorted_vals, p):
            idx = (p / 100) * (len(sorted_vals) - 1)
            lo = int(idx)
            hi = min(lo + 1, len(sorted_vals) - 1)
            return round(sorted_vals[lo] + (idx - lo) *
                         (sorted_vals[hi] - sorted_vals[lo]), 1)
        for p in (10, 50, 90):
            check(a.reports._percentile(s, p) == naive(s, p),
                  f'percentile p={p} matches independent computation')
        check(a.reports._percentile([], 50) is None, 'empty input returns None')
        check(a.reports._percentile([42.0], 90) == 42.0, 'single value returns itself')

        p9 = a.reports._percentile(sorted(a.db.get_session_prof_lafspl(SESSION_DATE, [9])), 90)
        p10 = a.reports._percentile(sorted(a.db.get_session_prof_lafspl(SESSION_DATE, [10])), 90)
        pooled_910 = a.reports._percentile(sorted(multi), 90)
        check(abs(pooled_910 - (p9 + p10) / 2) > 1e-9,
              'pooled LA10 differs from the mean of per-run LA10s',
              f'pooled={pooled_910}, mean-of-runs={round((p9 + p10) / 2, 1)}')

        # ── 3. delete_session() cascade ───────────────────────────────────────
        print('\n3. delete_session() cascade')
        aid = a.assess.create_assessment('Cascade test', standard='bs4142')
        loc = a.assess.add_assessment_location(aid, 'Loc A')
        a.assess.assign_runs(aid, loc, [(SESSION_DATE, 9), (SESSION_DATE, 10)])
        a.db.save_weather(SESSION_DATE, {
            'wind_speed': 7.2, 'wind_dir': 210.5, 'temp_min': 11.0,
            'temp_max': 19.4, 'precip': 0.2, 'hourly_json': '{}'})

        def n_of(side, table, where='', *p):
            return side.sql(f'SELECT COUNT(*) c FROM {table} {where}', *p)[0]['c']

        check(n_of(a, 'assessment_runs') == 2, 'two runs assigned')
        check(n_of(a, 'runs') == n_runs, 'runs present before delete', str(n_runs))

        a.db.delete_session(SESSION_DATE)

        check(n_of(a, 'sessions', 'WHERE date=?', SESSION_DATE) == 0, 'session row deleted')
        check(n_of(a, 'runs') == 0, 'runs cascaded via the sessions FK')
        check(n_of(a, 'assessment_runs') == 0,
              'assessment_runs deleted by hand (session_date is not a real FK)')
        check(n_of(a, 'assessments') == 1,
              'the assessment itself survives — only its run assignments go')
        check(n_of(a, 'assessment_locations') == 1, 'assessment locations survive')
        check(n_of(a, 'weather', 'WHERE date=?', SESSION_DATE) == 1,
              'weather row is intentionally NOT deleted (per-date data)')

        # ── 4. sync-shaped round trip ─────────────────────────────────────────
        print('\n4. peer-sync round trip preserves metadata and weather')
        a.db.import_sessions(sessions, metadata=META)
        a.db.save_weather(SESSION_DATE, {
            'wind_speed': 7.2, 'wind_dir': 210.5, 'temp_min': 11.0,
            'temp_max': 19.4, 'precip': 0.2, 'hourly_json': '{}'})
        payload = a.db.get_sessions_since('1970-01-01T00:00:00')
        check(len(payload) == 1, 'one session in the sync payload')
        check(payload[0]['wx'] is not None, 'weather included in payload')
        check(payload[0]['notes'] == META['notes'], 'notes included in payload')
        check(payload[0]['projects'][8].get('spec_lfeq') is not None,
              'payload carries spectral arrays (full=True)')

        b = Side(os.path.join(tmp, 'b.db'))
        b.db.import_sessions(payload)          # no metadata kwarg — as the peer does

        psess = b.db.get_all_sessions_json()['sessions'][0]
        check(psess['name'] == 'Catherine Ives-Yim', 'recorder_name survived the hop')
        check(psess['loc'] == 'Reference site', 'location_label survived')
        check(psess['post'] == 'LS1 1AA', 'postcode survived')
        check(psess['notes'] == META['notes'], 'notes survived')
        close(psess['lat'], 53.7997, 1e-9, 'lat survived')
        close(psess['lng'], -1.5492, 1e-9, 'lng survived')
        check(psess['wx'] is not None, 'weather survived the hop')
        close(psess['wx']['ws'], 7.2, 1e-9, 'wind speed survived')
        close(psess['wx']['tx'], 19.4, 1e-9, 'temp max survived')

        prun9 = b.db.get_full_run_row(SESSION_DATE, 9)
        close(prun9['avg_laeq'], 70.8, 0.05, 'peer run 9 LAeq intact')
        close(prun9['la_l90'],   57.1, 0.05, 'peer run 9 LA90 intact')
        check(len(json.loads(prun9['spec_lfeq'])) == 36, 'peer run 9 spectral table intact')
        check(len(json.loads(prun9['prof_lafspl_json'])) == 900,
              'peer run 9 PROF series intact — xlsx export works on the peer')
        check(b.db.get_session_prof_lafspl(SESSION_DATE, [9]) ==
              a.db.get_session_prof_lafspl(SESSION_DATE, [9]),
              'pooled LAFspl identical on both sides')

        # ── 5. deletions replicate ────────────────────────────────────────────
        print('\n5. session deletions replicate to the peer')
        baid = b.assess.create_assessment('Peer side', standard='bs4142')
        bloc = b.assess.add_assessment_location(baid, 'Loc B')
        b.assess.assign_runs(baid, bloc, [(SESSION_DATE, 9)])
        check(n_of(b, 'assessment_runs') == 1, 'peer has an assigned run')

        # 5a — a local delete leaves a tombstone and publishes it
        a.db.delete_session(SESSION_DATE)
        tomb = a.tombstones()
        check(SESSION_DATE in tomb, 'delete_session records a tombstone',
              f'deleted_at={tomb.get(SESSION_DATE)}')
        full = a.sync.get_full_sync_payload()
        check([t['date'] for t in full['deleted_sessions']] == [SESSION_DATE],
              'tombstone is carried in the full sync payload')

        # 5b — a peer that was offline catches up on the next full sync
        check(b.has_session(SESSION_DATE), 'peer still has the session beforehand')
        b.set_imported_at(SESSION_DATE, '2020-01-01 00:00:00')   # imported long ago
        b.sync.apply_full_sync(full)
        check(not b.has_session(SESSION_DATE),
              'offline peer deletes the session on full-sync catch-up')
        check(n_of(b, 'runs') == 0, 'peer runs cascaded')
        check(n_of(b, 'assessment_runs') == 0, 'peer assessment_runs cleared')
        check(n_of(b, 'assessments') == 1, 'peer assessment itself survives')
        check(b.tombstones().get(SESSION_DATE) == tomb[SESSION_DATE],
              'peer relays the tombstone with the original deletion time')

        # 5c — re-importing a date clears its tombstone
        b.db.import_sessions(payload)
        check(b.has_session(SESSION_DATE), 'peer re-imports the date')
        check(SESSION_DATE not in b.tombstones(),
              're-import clears the tombstone, so it will not be re-deleted')

        # 5d — a stale tombstone must not delete a newer local re-import
        b.set_imported_at(SESSION_DATE, '2030-01-01 00:00:00')   # newer than the delete
        b.sync.apply_full_sync(full)
        check(b.has_session(SESSION_DATE),
              'a re-import newer than the peer tombstone is NOT deleted')

        # 5e — the live event path deletes and re-tombstones
        b.set_imported_at(SESSION_DATE, '2020-01-01 00:00:00')
        b.sync.apply_sync_event('session', 'delete', {'date': SESSION_DATE})
        check(not b.has_session(SESSION_DATE), "apply_sync_event('session','delete') deletes")
        check(SESSION_DATE in b.tombstones(),
              'the replayed delete tombstones locally too, so it keeps propagating')

        # 5f — bulk purge replicates the same way
        print('\n6. purge_sessions_before() replicates')
        for side in (a, b):
            side.exec("INSERT OR REPLACE INTO sessions (date, run_count, avg_laeq, max_laeq) "
                      "VALUES ('2026-07-01', 1, 50.0, 60.0)")
            side.exec("INSERT OR REPLACE INTO sessions (date, run_count, avg_laeq, max_laeq) "
                      "VALUES ('2026-07-15', 1, 51.0, 61.0)")
            side.exec("INSERT OR REPLACE INTO sessions (date, run_count, avg_laeq, max_laeq) "
                      "VALUES ('2026-09-01', 1, 52.0, 62.0)")
            side.exec("UPDATE sessions SET imported_at='2020-01-01 00:00:00'")
        check(n_of(b, 'sessions') == 3, 'peer has three sessions before the purge')

        purged = a.db.purge_sessions_before('2026-08-01')
        check(sorted(purged) == ['2026-07-01', '2026-07-15'],
              'purge removes only dates before the cutoff', str(purged))
        atomb = a.tombstones()
        check(all(d in atomb for d in purged), 'purge tombstones every date it removed')
        check('2026-09-01' not in atomb, 'the surviving date is not tombstoned')

        b.sync.apply_sync_event('session', 'purge_before', {'before': '2026-08-01'})
        check(not b.has_session('2026-07-01') and not b.has_session('2026-07-15'),
              'purge event removes both dates on the peer')
        check(b.has_session('2026-09-01'), 'purge event leaves later sessions alone')
        check(all(d in b.tombstones() for d in purged),
              'peer tombstones the purged dates too')

        # and a peer that missed the event still catches up via full sync
        c = Side(os.path.join(tmp, 'c.db'))
        c.db.import_sessions(payload)
        c.exec("INSERT OR REPLACE INTO sessions (date, run_count, avg_laeq, max_laeq) "
               "VALUES ('2026-07-01', 1, 50.0, 60.0)")
        c.exec("UPDATE sessions SET imported_at='2020-01-01 00:00:00'")
        check(c.has_session('2026-07-01') and c.has_session(SESSION_DATE),
              'third Pi starts with both dates')
        c.sync.apply_full_sync(a.sync.get_full_sync_payload())
        check(not c.has_session('2026-07-01'), 'purged date removed on catch-up')
        check(not c.has_session(SESSION_DATE), 'deleted session removed on catch-up')

        # ── 6b. session aggregates and orphan cleanup ─────────────────────────
        print('\n6b. recompute_session_aggregates() and delete_orphaned_runs()')
        m = Side(os.path.join(tmp, 'maint.db'))
        m.db.import_sessions(sessions, metadata=META)
        good = m.sql('SELECT avg_laeq, max_laeq, run_count FROM sessions WHERE date=?',
                     SESSION_DATE)[0]
        import_avg = good['avg_laeq']

        # Reproduce the real defect: runs corrected, session left stale (the old
        # 0x0422 LAeq bug survived at session level ~8 dB high).
        m.exec('UPDATE sessions SET avg_laeq=83.58, max_laeq=1.0, run_count=99 WHERE date=?',
               SESSION_DATE)
        changed = m.db.recompute_session_aggregates([SESSION_DATE])
        fixed = m.sql('SELECT avg_laeq, max_laeq, run_count FROM sessions WHERE date=?',
                      SESSION_DATE)[0]
        check(len(changed) == 1 and changed[0][0] == SESSION_DATE,
              'recompute reports the changed session', str(changed))
        check(fixed['avg_laeq'] == import_avg,
              'recomputed avg_laeq equals what import produced',
              f'{fixed["avg_laeq"]} vs {import_avg}')
        check(fixed['max_laeq'] == good['max_laeq'], 'max_laeq restored')
        check(fixed['run_count'] == good['run_count'], 'run_count restored')
        check(m.db.recompute_session_aggregates([SESSION_DATE]) == [],
              'recompute is idempotent — second pass changes nothing')

        # Orphans: a run pointing at a session id that does not exist, plus one
        # with a NULL session_id, must both go; attached runs must survive.
        attached_before = n_of(m, 'runs', 'WHERE session_id IN (SELECT id FROM sessions)')
        m.exec_nofk('INSERT INTO runs (session_id, run_number, avg_laeq) VALUES (999999, 1, 50.0)')
        m.exec_nofk('INSERT INTO runs (session_id, run_number, avg_laeq) VALUES (NULL, 2, 51.0)')
        check(n_of(m, 'runs') == attached_before + 2, 'two orphan rows planted')
        removed = m.db.delete_orphaned_runs()
        check(removed == 2, 'delete_orphaned_runs removed exactly the orphans', str(removed))
        check(n_of(m, 'runs') == attached_before, 'attached runs untouched',
              str(attached_before))
        check(m.db.delete_orphaned_runs() == 0, 'idempotent — nothing left to remove')

        # ── 6c. report template integrity ─────────────────────────────────────
        print('\n6c. seed report templates are structurally valid')
        SECTIONS = ('executive_summary', 'methodology', 'results_narrative',
                    'compliance', 'conclusions', 'recommendations')
        for t in m.reports_db.DEFAULT_TEMPLATES:
            nm = t['name']
            # Without this token the prompt reaches Claude with no measurement
            # data in it and the report is confidently invented.
            check('{{session_data}}' in t['prompt'],
                  f'{nm}: has the {{{{session_data}}}} token')
            missing = [k for k in SECTIONS if k not in t['prompt']]
            check(not missing, f'{nm}: names all six report sections', str(missing))
        check(sum(t['is_default'] for t in m.reports_db.DEFAULT_TEMPLATES) == 1,
              'exactly one seed template is the default')
        # BS 8233:2014 Table 4 — bedrooms are 30 dB LAeq,8h at night, not 35.
        plan = next(t for t in m.reports_db.DEFAULT_TEMPLATES
                    if t['name'] == 'Planning Noise Assessment')
        check('30 dB LAeq,8h at night' in plan['prompt'],
              'Planning: bedroom night guideline is 30 dB LAeq,8h')
        check('bedrooms: 35 dB LAeq,8h night' not in plan['prompt'],
              'Planning: the old incorrect bedroom value is gone')
        check('rating level' in plan['prompt'],
              'Planning: BS 4142 rating level (not raw specific level) is required')

        # ── 7. NOR140 xlsx parity with every Nortfr reference present ─────────
        print('\n7. xlsx export matches the Nortfr references')
        check_references(meas_root, tmp)

        print(f'\nAll {_checks} checks passed.')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else MEAS_DEFAULT)
