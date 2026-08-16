#!/usr/bin/env python3
"""
Empirical tests for the noise-meter data layer, run against a real temporary
SQLite database built from the MEAS118 reference SD-card files.

These are deliberately not import-smoke tests: every case drives real data
through the real code path and checks values, because the bugs this codebase
has actually shipped (a placeholder count off by one, a percentile reading the
wrong channel, a sync path dropping metadata) all compiled cleanly and read
correctly.

    python3 test_modules.py [path/to/MEAS118]

Exits non-zero on the first failure.
"""
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


def main(meas_root):
    pairs = read_sd_pairs(meas_root)
    tmp = tempfile.mkdtemp(prefix='noisetest-')
    os.environ['NOISE_DB_PATH'] = os.path.join(tmp, 'noise.db')
    sys.path.insert(0, REPO)
    try:
        import noise_db
        import assessments_db
        import sync_db
        import reports
        from noise_parser import parse_files

        # ── 1. import_sessions() end to end ───────────────────────────────────
        print('\n1. import_sessions() from real SD-card binaries')
        sessions = parse_files(pairs)
        noise_db.init_db()
        n = noise_db.import_sessions(sessions, metadata={
            'recorder_name': 'Catherine Ives-Yim',
            'location_label': 'Reference site',
            'postcode': 'LS1 1AA', 'lat': 53.7997, 'lng': -1.5492,
        })
        check(n == len(sessions) == 1, 'one session imported', f'n={n}')

        data = noise_db.get_all_sessions_json()['sessions']
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
        run9 = noise_db.get_full_run_row(SESSION_DATE, 9)
        check(run9 is not None, 'run 9 present')
        close(run9['avg_laeq'], 70.8, 0.05, 'run 9 LAeq  = 70.8 (Nortfr)')
        close(run9['lceq'],     78.7, 0.05, 'run 9 LCeq  = 78.7 (Nortfr)')
        close(run9['lapeak'],   97.4, 0.05, 'run 9 LApeak= 97.4 (Nortfr)')
        close(run9['lcpeak'],   98.6, 0.05, 'run 9 LCpeak= 98.6 (Nortfr)')
        close(run9['la_l10'],   74.8, 0.05, 'run 9 LA10  = 74.8 (Nortfr)')
        close(run9['la_l50'],   67.1, 0.05, 'run 9 LA50  = 67.1 (Nortfr)')
        close(run9['la_l90'],   57.1, 0.05, 'run 9 LA90  = 57.1 (Nortfr)')
        check(run9['n_samples'] == 900, 'run 9 is 900 s', str(run9['n_samples']))

        # All 18 spectral tables and all 5 PROF series must survive the INSERT.
        from nor140_format import SPECTRAL_TABLES
        missing = [c for c, _ in SPECTRAL_TABLES if not run9.get(c)]
        check(not missing, 'all 18 spectral tables stored', f'missing: {missing}')
        for col, _ in SPECTRAL_TABLES:
            check(len(json.loads(run9[col])) == 36, f'{col} has 36 bands')
        for col in ('prof_lafspl_json', 'prof_laeq_json', 'prof_lafmax_json',
                    'prof_lae_json', 'prof_lapeak_json'):
            check(len(json.loads(run9[col])) == 900, f'{col} has 900 samples')

        # ── 2. LA10/LA90 pooled percentile ────────────────────────────────────
        print('\n2. pooled LA10/LA90 percentile across runs')
        pooled = noise_db.get_session_prof_lafspl(SESSION_DATE, [9])
        check(len(pooled) == 900, 'run 9 pools 900 samples', str(len(pooled)))
        check(pooled == json.loads(run9['prof_lafspl_json']),
              'pooling reads PROF field 0 (LAFspl), not field 1 (LAeq,1s)')
        check(pooled != json.loads(run9['prof_laeq_json']),
              'LAFspl and LAeq,1s are genuinely different series')

        multi = noise_db.get_session_prof_lafspl(SESSION_DATE, [9, 10])
        check(len(multi) == 1800, 'two runs pool 1800 samples', str(len(multi)))
        every = noise_db.get_session_prof_lafspl(SESSION_DATE)
        check(len(every) == sum(p['n'] for p in sess['projects']),
              'no run_numbers pools every run', f'{len(every)} samples')

        # _percentile is an interpolating percentile over an ascending list:
        # LA90 is the level exceeded 90% of the time = the 10th percentile.
        s = sorted(pooled)
        la90 = reports._percentile(s, 10)
        la50 = reports._percentile(s, 50)
        la10 = reports._percentile(s, 90)
        check(la90 < la50 < la10, 'LA90 < LA50 < LA10', f'{la90} / {la50} / {la10}')

        def naive(sorted_vals, p):
            idx = (p / 100) * (len(sorted_vals) - 1)
            lo = int(idx)
            hi = min(lo + 1, len(sorted_vals) - 1)
            return round(sorted_vals[lo] + (idx - lo) *
                         (sorted_vals[hi] - sorted_vals[lo]), 1)
        for p in (10, 50, 90):
            check(reports._percentile(s, p) == naive(s, p),
                  f'percentile p={p} matches independent computation')
        check(reports._percentile([], 50) is None, 'empty input returns None')
        check(reports._percentile([42.0], 90) == 42.0, 'single value returns itself')

        # Pooling real samples must not equal averaging each run's own
        # percentile — percentiles do not compose linearly across sub-samples.
        p9 = reports._percentile(sorted(noise_db.get_session_prof_lafspl(SESSION_DATE, [9])), 90)
        p10 = reports._percentile(sorted(noise_db.get_session_prof_lafspl(SESSION_DATE, [10])), 90)
        pooled_910 = reports._percentile(sorted(multi), 90)
        check(abs(pooled_910 - (p9 + p10) / 2) > 1e-9,
              'pooled LA10 differs from the mean of per-run LA10s',
              f'pooled={pooled_910}, mean-of-runs={round((p9 + p10) / 2, 1)}')

        # ── 3. delete_session() cascade ───────────────────────────────────────
        print('\n3. delete_session() cascade')
        aid = assessments_db.create_assessment('Cascade test', standard='bs4142')
        loc = assessments_db.add_assessment_location(aid, 'Loc A')
        assessments_db.assign_runs(aid, loc, [(SESSION_DATE, 9), (SESSION_DATE, 10)])
        noise_db.save_weather(SESSION_DATE, {
            'wind_speed': 7.2, 'wind_dir': 210.5, 'temp_min': 11.0,
            'temp_max': 19.4, 'precip': 0.2, 'hourly_json': '{}'})

        conn = noise_db.get_db()
        def count(sql, *a):
            return conn.execute(sql, a).fetchone()[0]
        check(count('SELECT COUNT(*) FROM assessment_runs') == 2, 'two runs assigned')
        before_runs = count('SELECT COUNT(*) FROM runs')
        check(before_runs == n_runs, 'runs present before delete', str(before_runs))
        conn.close()

        noise_db.delete_session(SESSION_DATE)

        conn = noise_db.get_db()
        check(count('SELECT COUNT(*) FROM sessions WHERE date=?', SESSION_DATE) == 0,
              'session row deleted')
        check(count('SELECT COUNT(*) FROM runs') == 0,
              'runs cascaded via the sessions FK')
        check(count('SELECT COUNT(*) FROM assessment_runs') == 0,
              'assessment_runs deleted by hand (session_date is not a real FK)')
        check(count('SELECT COUNT(*) FROM assessments') == 1,
              'the assessment itself survives — only its run assignments go')
        check(count('SELECT COUNT(*) FROM assessment_locations') == 1,
              'assessment locations survive')
        # Documented current behaviour, not an assertion that it is desirable:
        # weather is keyed by date and is left in place by delete_session.
        wx_left = count('SELECT COUNT(*) FROM weather WHERE date=?', SESSION_DATE)
        check(wx_left == 1, 'weather row is intentionally NOT deleted (per-date data)')
        conn.close()

        # ── 4. sync-shaped round trip ─────────────────────────────────────────
        print('\n4. peer-sync round trip preserves metadata and weather')
        # Rebuild the source DB, then replicate it into a second one exactly the
        # way sync_peer.py does: get_sessions_since() -> import_sessions().
        noise_db.import_sessions(sessions, metadata={
            'recorder_name': 'Catherine Ives-Yim',
            'location_label': 'Reference site',
            'postcode': 'LS1 1AA', 'lat': 53.7997, 'lng': -1.5492,
            'notes': 'a note that must survive the hop',
        })
        noise_db.save_weather(SESSION_DATE, {
            'wind_speed': 7.2, 'wind_dir': 210.5, 'temp_min': 11.0,
            'temp_max': 19.4, 'precip': 0.2, 'hourly_json': '{}'})
        payload = sync_db_since(noise_db, '1970-01-01T00:00:00')
        check(len(payload) == 1, 'one session in the sync payload')
        check(payload[0]['wx'] is not None, 'weather included in payload')
        check(payload[0]['notes'] == 'a note that must survive the hop',
              'notes included in payload')
        check(payload[0]['projects'][8].get('spec_lfeq') is not None,
              'payload carries spectral arrays (full=True)')

        peer_db = os.path.join(tmp, 'peer.db')
        peer = load_fresh_noise_db(peer_db)
        peer.init_db()
        peer.import_sessions(payload)          # no metadata kwarg — as the peer does

        psess = peer.get_all_sessions_json()['sessions'][0]
        check(psess['name'] == 'Catherine Ives-Yim', 'recorder_name survived the hop')
        check(psess['loc'] == 'Reference site', 'location_label survived')
        check(psess['post'] == 'LS1 1AA', 'postcode survived')
        check(psess['notes'] == 'a note that must survive the hop', 'notes survived')
        close(psess['lat'], 53.7997, 1e-9, 'lat survived')
        close(psess['lng'], -1.5492, 1e-9, 'lng survived')
        check(psess['wx'] is not None, 'weather survived the hop')
        close(psess['wx']['ws'], 7.2, 1e-9, 'wind speed survived')
        close(psess['wx']['tx'], 19.4, 1e-9, 'temp max survived')

        prun9 = peer.get_full_run_row(SESSION_DATE, 9)
        close(prun9['avg_laeq'], 70.8, 0.05, 'peer run 9 LAeq intact')
        close(prun9['la_l90'],   57.1, 0.05, 'peer run 9 LA90 intact')
        check(len(json.loads(prun9['spec_lfeq'])) == 36,
              'peer run 9 spectral table intact')
        check(len(json.loads(prun9['prof_lafspl_json'])) == 900,
              'peer run 9 PROF series intact — xlsx export works on the peer')
        check(peer.get_session_prof_lafspl(SESSION_DATE, [9]) ==
              noise_db.get_session_prof_lafspl(SESSION_DATE, [9]),
              'pooled LAFspl identical on both sides')

        print(f'\nAll {_checks} checks passed.')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def sync_db_since(noise_db, since):
    return noise_db.get_sessions_since(since)


def load_fresh_noise_db(db_path):
    """Import a second, independent noise_db bound to a different database."""
    import importlib
    import noise_db as _nd
    os.environ['NOISE_DB_PATH'] = db_path
    for name in ('noise_db', 'reports_db', 'assessments_db', 'sync_db'):
        if name in sys.modules:
            del sys.modules[name]
    peer = importlib.import_module('noise_db')
    assert peer.DB_PATH == db_path, peer.DB_PATH
    return peer


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else MEAS_DEFAULT)
