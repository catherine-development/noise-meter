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

# noise_app refuses to start when the flight tracker's auth module is missing,
# which it always is on a development Mac. Sections 4d and 8 import the real
# app, so the suite declares itself a development instance. Section 8 checks
# that a *subprocess* without this variable is still refused.
os.environ.setdefault('ALLOW_UNAUTHENTICATED', '1')

# The CSRF token the suite's test clients present. Seeded straight into the
# session rather than scraped out of a rendered page.
SUITE_CSRF = 'suite-csrf-token'
CSRF_HDR = {'X-CSRF-Token': SUITE_CSRF}

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

        # The meter's own end time (GLOB 0x22). Stored, not derived: for run 9 it
        # happens to equal start + 900 s, but 250712/24 ends at 21:27:49 — which
        # matches neither its 304 periods nor its 299 s duration, so any
        # arithmetic on start time would report the wrong figure there.
        check(run9['end_time'] == '23:42:36', 'run 9 end time = 23:42:36',
              str(run9['end_time']))
        missing_end = [p['start'] for p in sess['projects'] if not p.get('end')]
        check(not missing_end, 'every run exposes an end time', str(missing_end))
        r9 = next(p for p in sess['projects'] if p['start'] == run9['start_time'])
        check(r9['end'] == '23:42:36', "run 9 'end' reaches the display layer",
              str(r9.get('end')))

        # The end-time field is sometimes left unset. The decoder cross-checks it
        # against the stored duration rather than blacklisting 00:00:00, since a
        # run can legitimately end at midnight.
        import glob as _glob
        from nor140_format import read_end_time
        _corpus = sorted(_glob.glob(os.path.join(meas_root, '**', 'GLOB*.DAT'),
                                   recursive=True))
        _rejected = [g for g in _corpus
                     if read_end_time(open(g, 'rb').read()) is None]
        check(len(_rejected) == 2, 'end-time guard rejects exactly the 2 unset fields',
              f'{len(_rejected)} of {len(_corpus)}')
        check(all(d in r for d in ('230830', '250711') for r in [' '.join(_rejected)]),
              'the rejected pair are the known 230830/250711 runs')

        # The 1069-byte GLOB variant pairs with a 4-channel, 8-byte PROF record.
        # Read as 10 bytes the whole series is misaligned, so these assertions
        # are the guard against that regressing: the decoded profile has to
        # reproduce the GLOB scalars exactly.
        import noise_parser as parser
        import collections
        from nor140_format import prof_record_size, read_duration_s
        _b = os.path.join(meas_root, '250823', 'PART0000', 'PROJ0002')
        if os.path.isdir(_b):
            _g = open(os.path.join(_b, 'GLOB0002.DAT'), 'rb').read()
            _p = open(os.path.join(_b, 'PROF0002.DAT'), 'rb').read()
            check(prof_record_size(len(_p) - 3, 894, 1069) == 8,
                  '1069-byte variant resolves to 8-byte records')

            _d, _r = parser._parse_session_files(_g, _p)
            check(_r['n'] == 895, '8-byte run has 895 records', str(_r['n']))
            close(parser._energy_avg(_r['prof_laeq_json']), 80.28, 0.005,
                  '8-byte profile LAeq reproduces the GLOB scalar')
            close(max(_r['prof_lapeak_json']), 102.83, 0.005,
                  '8-byte profile LApeak reproduces the GLOB scalar')
            check(_r['prof_lae_json'] is None,
                  '4-channel layout stores no LAE series rather than faking one')

        # prof_record_size() weighs two signals: the duration and the GLOB
        # variant. Both are derived before either is returned, so a file whose
        # signals disagree is refused rather than guessed at. The conflict case
        # is the one that regressed — the duration branch used to return first.
        check(prof_record_size(7160, 716, 1069) is None,
              'conflicting duration and GLOB variant fails closed')
        check(prof_record_size(7160, 3000, 1069) == 8,
              'paused 1069 variant resolves by variant when duration cannot')
        check(prof_record_size(7160, 3000, None) is None,
              'unknown variant with no duration match is unresolved')
        check(prof_record_size(9000, 900, 2653) == 10,
              'normal 2653 variant resolves to 10-byte records')
        check(prof_record_size(7160, None, 1069) == 8,
              'variant alone resolves when there is no duration')

        # Every historical file must still classify, and to the same size as
        # before: an unresolved file is skipped on import, so a regression here
        # silently drops runs.
        _sizes = collections.Counter()
        for _gg in _corpus:
            _pp = _gg.replace('GLOB', 'PROF')
            if not os.path.exists(_pp):
                continue
            _gd = open(_gg, 'rb').read()
            _sizes[prof_record_size(os.path.getsize(_pp) - 3,
                                    read_duration_s(_gd), len(_gd))] += 1
        check(_sizes[None] == 0, 'no corpus file is left unclassified',
              f'{_sizes[None]} unresolved')
        check(_sizes[10] == 473 and _sizes[8] == 54,
              'corpus classifies 473 ten-byte / 54 eight-byte', str(dict(_sizes)))

        # One example is not enough to pin a channel layout, so assert it over
        # every 8-byte file in the archive: ch2 must reproduce LAFmax and ch3
        # LApeak exactly, and ch1's energy average must be the stored LAeq.
        _short = []
        for _g8 in _corpus:
            _gd = open(_g8, 'rb').read()
            if len(_gd) != 1069:
                continue
            _pf = _g8.replace('GLOB', 'PROF')
            if not os.path.exists(_pf):
                continue
            _d, _rr = parser._parse_session_files(_gd, open(_pf, 'rb').read())
            if _rr:
                _short.append((_g8, _rr))
        check(len(_short) == 54, 'all 54 short-format runs parse', str(len(_short)))
        _bad_max = [g for g, r in _short
                    if r['lafmax'] is None or r['prof_lafmax_json'] is None
                    or abs(max(r['prof_lafmax_json']) - r['lafmax']) > 0.005]
        check(not _bad_max, 'ch2 max == GLOB LAFmax in all 54', str(_bad_max[:2]))
        _bad_peak = [g for g, r in _short
                     if r['lapeak'] is None
                     or abs(max(r['prof_lapeak_json']) - r['lapeak']) > 0.005]
        check(not _bad_peak, 'ch3 max == GLOB LApeak in all 54', str(_bad_peak[:2]))
        _bad_eq = [g for g, r in _short
                   if abs(parser._energy_avg(r['prof_laeq_json']) - r['avg']) > 0.05]
        check(len(_bad_eq) <= 2,
              'ch1 energy average == GLOB LAeq in at least 52 of 54',
              f'{len(_bad_eq)} outliers')
        _no_lae = [g for g, r in _short if r['prof_lae_json'] is not None]
        check(not _no_lae, 'no 8-byte run fabricates an LAE series')

        # ch0 vs ch1 cannot be told apart by energy average — they agree to two
        # decimals. The GLOB LAF percentiles are the independent discriminator:
        # they describe the LAFspl distribution, so ch0 must track them more
        # closely than ch1 does. Without this, a ch0/ch1 swap would pass silently.
        def _pct(series, frac):
            sv = sorted(series, reverse=True)
            return sv[max(0, int(len(sv) * frac) - 1)]

        _wrong = []
        for _g8, _rr in _short:
            _c0, _c1 = _rr['prof_lafspl_json'], _rr['prof_laeq_json']
            _ref = [_rr['la_l10'], _rr['la_l50'], _rr['la_l90']]
            if any(v is None for v in _ref):
                continue
            _e0 = sum(abs(_pct(_c0, f) - v) for f, v in zip((.1, .5, .9), _ref))
            _e1 = sum(abs(_pct(_c1, f) - v) for f, v in zip((.1, .5, .9), _ref))
            if _e0 >= _e1:
                _wrong.append((_g8.split('MEAS118/')[-1], round(_e0, 2), round(_e1, 2)))
        check(not _wrong,
              'ch0 tracks the GLOB LAF percentiles better than ch1 in all 54',
              str(_wrong[:2]))

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

        # pct85 must count the true 1-second series, not the expanded chart profile
        # (whose windows each hold a maximum, overstating time above the threshold).
        for rn in (5, 7):
            true_series = json.loads(a.db.get_full_run_row(SESSION_DATE, rn)['prof_laeq_json'])
            expected = round(100 * sum(1 for v in true_series if v >= 85) / len(true_series), 1)
            proj = sess['projects'][rn - 1]
            st = a.reports._run_stats(proj, true_laeq=a.db.get_run_prof_laeq(SESSION_DATE, rn))
            check(st['pct85'] == expected,
                  f'run {rn}: pct85 counts the true series', f"{st['pct85']} vs {expected}")
            inflated = a.reports._expand_run(proj)
            infl = round(100 * sum(1 for v in inflated if v >= 85) / len(inflated), 1)
            check(infl >= st['pct85'],
                  f'run {rn}: the old chart-profile count was higher ({infl} vs {st["pct85"]})')

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

        # The meter's end time must survive the hop. This regressed once because
        # _run_to_dict exported it as 'end' while import_sessions read
        # 'end_time', so the peer silently stored NULL. Run 1 is the case that
        # exposes it: 83 records but an 82 s duration, so any arithmetic
        # reconstruction lands a second late, at 12:09:53 rather than 12:09:52.
        arun1 = a.db.get_full_run_row(SESSION_DATE, 1)
        prun1 = b.db.get_full_run_row(SESSION_DATE, 1)
        check(arun1['end_time'] == '12:09:52', 'source run 1 end time',
              str(arun1['end_time']))
        check(prun1['end_time'] == arun1['end_time'],
              'meter end time survives peer sync', str(prun1['end_time']))
        check(prun1['n_samples'] == 83 and prun1['duration_s'] == 82,
              'run 1 is the count-minus-one case that arithmetic gets wrong')
        pdisp = next(pp for pp in psess['projects'] if pp['run_number'] == 1)
        check(pdisp['end'] == '12:09:52', "peer displays the meter's end time",
              str(pdisp.get('end')))
        check(pdisp.get('end_time') == '12:09:52',
              'peer re-exports end_time for onward sync')

        # ── 4b. assessment links survive a run-number shift ───────────────────
        print('\n4b. assessment links are keyed on the stable run identity')
        aid = a.assess.create_assessment('Shift test', standard='bs4142')
        lid = a.assess.add_assessment_location(aid, 'Boundary')
        a.assess.assign_runs(aid, lid, [(SESSION_DATE, 5)])
        before = a.assess.get_assessment_detail(aid)['locations'][0]['runs'][0]
        check(before['run_number'] == 5, 'link created against run 5')
        pinned_start = before['start_time']
        ar_src = a.sql('SELECT source_file FROM assessment_runs')[0]['source_file']
        check(ar_src is not None, 'link stored the stable source_file', str(ar_src))

        # Simulate what a re-import does when a previously unparseable run is
        # recovered: an extra run appears earlier in the session and every later
        # run shifts up one. Keyed on run_number alone, the link would silently
        # follow the number onto a different measurement.
        sid = a.sql('SELECT id FROM sessions WHERE date=?', SESSION_DATE)[0]['id']
        # Two steps: a single +1 pass collides with UNIQUE(session_id, run_number)
        # as SQLite rewrites row by row.
        a.exec('UPDATE runs SET run_number = run_number + 1000 '
               'WHERE session_id=? AND run_number >= 3', sid)
        a.exec('UPDATE runs SET run_number = run_number - 999 '
               'WHERE session_id=? AND run_number >= 1000', sid)
        a.exec("INSERT INTO runs (session_id, run_number, source_file, start_time, "
               "n_samples, step, avg_laeq) VALUES (?,3,'PROJ9999','20:00:00',60,1,50.0)", sid)

        after = a.assess.get_assessment_detail(aid)['locations'][0]['runs'][0]
        check(after['start_time'] == pinned_start,
              'link still resolves to the same physical run after the shift',
              f"{pinned_start} -> {after['start_time']}")
        check(after['run_number'] == 6,
              'and follows it to its new position', str(after['run_number']))
        moved = a.sql('SELECT start_time FROM runs WHERE session_id=? AND run_number=5',
                      sid)[0]['start_time']
        check(moved != pinned_start,
              'run 5 is now a different measurement — what the old key would have returned',
              f'{moved} vs {pinned_start}')

        # A peer on the older schema sends no source_file; that must not erase ours.
        a.sync.apply_sync_event('assessment_run', 'upsert', {
            'id': a.sql('SELECT id FROM assessment_runs')[0]['id'],
            'assessment_id': aid, 'location_id': lid, 'session_date': SESSION_DATE,
            'run_number': 6, 'conditions': 'dry', 'notes': ''})
        kept = a.sql('SELECT source_file FROM assessment_runs')[0]['source_file']
        check(kept == ar_src, "an old peer's payload does not erase source_file",
              str(kept))

        # The two write failures the read-side fix alone did not prevent: with
        # the upsert still keyed on run_number, re-assigning the same physical
        # run at its new number inserted a duplicate, and assigning whatever had
        # taken the old number silently rebound the link.
        a.assess.assign_runs(aid, lid, [(SESSION_DATE, 6)])
        rows = a.sql('SELECT run_number, source_file FROM assessment_runs')
        check(len(rows) == 1, 're-assigning the same run at its new number is not a duplicate',
              str([(r['run_number'], r['source_file']) for r in rows]))
        check(rows[0]['source_file'] == ar_src, 'and still points at the same measurement')
        check(rows[0]['run_number'] == 6, 'with run_number refreshed to the new position',
              str(rows[0]['run_number']))

        a.assess.assign_runs(aid, lid, [(SESSION_DATE, 5)])
        srcs = sorted(r['source_file'] for r in
                      a.sql('SELECT source_file FROM assessment_runs'))
        check(ar_src in srcs, 'assigning the old number does not rebind the original link',
              str(srcs))
        check(len(srcs) == 2, 'it creates a separate link for the other measurement',
              str(srcs))

        # The picker must mark the run that is actually linked, not whichever
        # run inherited the number — that is what led users into the above.
        pool = a.assess.get_all_runs_for_assessment(aid)
        marked = {r['source_file'] for r in pool
                  if r['ar_id'] and r['session_date'] == SESSION_DATE}
        check(ar_src in marked, 'picker marks the linked measurement', str(sorted(marked)))

        # The session card and the assessment detail must not disagree.
        card = [x for x in a.db.get_all_sessions_json()['sessions']
                if x['d'] == SESSION_DATE][0]
        labelled = {p['run_number'] for p in card['projects'] if p.get('assess')}
        detail_nums = {r['run_number'] for r in
                       a.assess.get_assessment_detail(aid)['locations'][0]['runs']}
        check(labelled == detail_nums,
              'session card and assessment detail agree on which runs are linked',
              f'card {sorted(labelled)} vs detail {sorted(detail_nums)}')

        a.assess.delete_assessment(aid)
        a.exec('DELETE FROM runs WHERE source_file=?', 'PROJ9999')
        a.exec('UPDATE runs SET run_number = run_number + 1000 '
               'WHERE session_id=? AND run_number >= 4', sid)
        a.exec('UPDATE runs SET run_number = run_number - 1001 '
               'WHERE session_id=? AND run_number >= 1000', sid)

        # ── 4ante. the import path and the shared decoders agree ─────────────
        print('\n4ante. no divergent copies of the binary decoders')
        # A duplicated decoder fails silently: the reference comparison exercises
        # the export path, while a different function writes the database. That
        # is how the -20 marker survived, and how impulse spectra above 130 dB
        # were dropped on import while the backfill kept them. This asserts the
        # two paths still agree over every table in the archive.
        import noise_parser as _np
        from nor140_format import (read_glob_spectrum as _rgs,
                                   read_glob_scalars as _rsc,
                                   read_start_datetime as _rsd)
        _div = _scal = _dt = 0
        for _gg in _corpus:
            _bd = open(_gg, 'rb').read()
            for _col, _off in a.db.SPECTRAL_TABLES:
                if _np._read_glob_spectrum(_bd, _off) != _rgs(_bd, _off):
                    _div += 1
            if _np._read_glob_scalars(_bd) != _rsc(_bd):
                _scal += 1
            if _rsd(_bd)[0] is None and len(_bd) > 0x20:
                _dt += 1
        check(_div == 0, 'spectrum decode identical on both paths', f'{_div} divergent')
        check(_scal == 0, 'scalar decode identical on both paths', f'{_scal} divergent')
        check(_dt == 0, 'every archive file yields a start datetime', f'{_dt} failed')

        # Impulse tables legitimately exceed CAP_LAEQ and must survive.
        _imp = 0
        for _gg in _corpus:
            _bd = open(_gg, 'rb').read()
            for _col, _off in a.db.SPECTRAL_TABLES:
                _t = _rgs(_bd, _off)
                if _t and max(_t) > 130.0:
                    _imp += 1
        check(_imp >= 3, 'spectra above the LAeq cap are kept, not discarded',
              f'{_imp} tables above 130 dB')

        # ── 4bis. the -20 dB no-data marker never reaches a report ────────────
        print('\n4bis. no-data marker is dropped at the decode boundary')
        r1 = a.db.get_full_run_row(SESSION_DATE, 1)
        check(r1['n_samples'] == 83, 'run 1 is short enough to lack a 0.1% percentile')
        check(r1['la_l01'] is None, 'unrecorded percentile stored as NULL, not -20',
              repr(r1['la_l01']))
        check(r1['la_l90'] is not None, 'recorded percentiles are unaffected',
              repr(r1['la_l90']))

        # A stored -20 must not survive into the BS 4142 figures: fed through as
        # an LA90 it produces a rating-level difference of about +81 dB.
        a.exec('UPDATE runs SET la_l90=-20.0, la_l50=-20.0 WHERE run_number=2')
        cleared = a.db.clear_sentinel_scalars()
        check(cleared.get('la_l90') == 1, 'maintenance helper clears a stored marker',
              str(cleared))
        check(a.db.get_full_run_row(SESSION_DATE, 2)['la_l90'] is None,
              'and leaves NULL behind')

        aid2 = a.assess.create_assessment('Sentinel', standard='bs4142')
        lid2 = a.assess.add_assessment_location(aid2, 'B')
        a.assess.assign_runs(aid2, lid2, [(SESSION_DATE, 2)])
        rd = a.assess.prepare_assessment_report_data(aid2)['locations'][0]['runs'][0]
        check(rd.get('la90') is None or rd['la90'] > -19.99,
              'report data carries no no-data marker', repr(rd.get('la90')))
        a.assess.delete_assessment(aid2)

        # ── 4d. the assign route, end to end ──────────────────────────────────
        print('\n4d. assignment over HTTP, not just the data layer')
        # The suite only ever called assign_runs() directly, so a 500 in the
        # route sailed past every check: the endpoint built three-element tuples
        # and handed them to a helper that unpacked two. assign_runs had already
        # committed, so the row landed, the request failed, and the peer was
        # never told.
        #
        # Its own Side, because constructing one rebinds sys.modules — the route
        # resolves assign_runs through whichever binding is current, so sharing a
        # Side with an earlier section writes to the wrong database.
        import importlib as _il
        rt = Side(os.path.join(tmp, 'route.db'))
        rt.db.import_sessions(sessions, metadata=META)
        _app = _il.import_module('noise_app').app
        _app.config['TESTING'] = True
        _client = _app.test_client()
        with _client.session_transaction() as _sess:
            _sess['user'] = 'test'
            _sess['logged_in'] = True
            _sess['_csrf_token'] = SUITE_CSRF   # every POST below is CSRF-checked

        aid4 = rt.assess.create_assessment('Route test')
        lid4 = rt.assess.add_assessment_location(aid4, 'L')
        src5 = rt.sql('SELECT r.source_file FROM runs r JOIN sessions s ON s.id=r.session_id '
                      'WHERE s.date=? AND r.run_number=5', SESSION_DATE)[0]['source_file']
        _resp = _client.post(f'/api/assessments/{aid4}/assign', headers=CSRF_HDR, json={
            'location_id': lid4,
            'runs': [{'date': SESSION_DATE, 'run_number': 5, 'source_file': src5}]})
        check(_resp.status_code == 200, 'assign route returns 200, not 500',
              f'{_resp.status_code}: {_resp.get_data(as_text=True)[:120]}')
        check(_resp.get_json().get('assigned') == 1, 'route reports one assignment',
              str(_resp.get_json()))

        # A stale page: the number moved between render and POST, but the stable
        # key the page carried still names the right measurement.
        sid4 = rt.sql('SELECT id FROM sessions WHERE date=?', SESSION_DATE)[0]['id']
        rt.exec('UPDATE runs SET run_number=run_number+1000 WHERE session_id=? AND run_number>=3', sid4)
        rt.exec('UPDATE runs SET run_number=run_number-999 WHERE session_id=? AND run_number>=1000', sid4)
        _resp = _client.post(f'/api/assessments/{aid4}/assign', headers=CSRF_HDR, json={
            'location_id': lid4,
            'runs': [{'date': SESSION_DATE, 'run_number': 5, 'source_file': src5}]})
        check(_resp.status_code == 200, 'a stale run number still assigns cleanly',
              str(_resp.status_code))
        _srcs = [r['source_file'] for r in
                 rt.sql('SELECT source_file FROM assessment_runs WHERE assessment_id=?', aid4)]
        check(_srcs == [src5], 'the stable key won over the stale position', str(_srcs))

        # A source_file that is not in this session is refused, not guessed at.
        _resp = _client.post(f'/api/assessments/{aid4}/assign', headers=CSRF_HDR, json={
            'location_id': lid4,
            'runs': [{'date': SESSION_DATE, 'run_number': 5, 'source_file': 'PROJ7777'}]})
        check(_resp.status_code == 400, 'a foreign source_file is rejected',
              str(_resp.status_code))

        # ── 4c. migration audit on hostile inputs ─────────────────────────────
        print('\n4c. migration audit flags links it cannot safely migrate')
        aid3 = a.assess.create_assessment('Audit test')
        lid3 = a.assess.add_assessment_location(aid3, 'L')
        a.assess.assign_runs(aid3, lid3, [(SESSION_DATE, 2)])
        clean = a.db.audit_assessment_run_keys()
        check(not any(clean.values()), 'a healthy table audits clean',
              str({k: len(v) for k, v in clean.items()}))

        # A link whose stable key names a run that is not there — what an
        # already-shifted database looks like after a naive backfill.
        a.exec("UPDATE assessment_runs SET source_file='PROJ8888' "
               'WHERE assessment_id=?', aid3)
        rep = a.db.audit_assessment_run_keys()
        check(len(rep['unmatched']) == 1, 'audit reports an unresolvable stable key',
              str(len(rep['unmatched'])))

        # A legacy row that never received one.
        a.exec('UPDATE assessment_runs SET source_file=NULL WHERE assessment_id=?', aid3)
        rep = a.db.audit_assessment_run_keys()
        check(len(rep['null_source_file']) == 1, 'audit reports a missing stable key',
              str(len(rep['null_source_file'])))
        # ...and such a row must still be assignable without creating a second.
        a.assess.assign_runs(aid3, lid3, [(SESSION_DATE, 2)])
        n_legacy = len(a.sql('SELECT id FROM assessment_runs WHERE assessment_id=?', aid3))
        check(n_legacy == 1, 'a legacy NULL-key link is updated, not duplicated',
              str(n_legacy))
        a.assess.delete_assessment(aid3)

        # The positional UNIQUE must be gone, replaced by the stable one.
        ddl = a.sql("SELECT sql FROM sqlite_master WHERE name='assessment_runs'")[0]['sql']
        check('UNIQUE(assessment_id, session_date, run_number)' not in ddl,
              'the positional UNIQUE is dropped')
        idx = a.sql("SELECT name FROM sqlite_master WHERE type='index' "
                    "AND name='idx_assessment_runs_stable'")
        check(len(idx) == 1, 'the stable partial unique index exists')

        # The client half of the stable key is JavaScript, so nothing above can
        # exercise it — which is precisely how it was silently lost once: an edit
        # that never landed, while the brief claimed the protection existed.
        _tpl = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 'templates', 'assessments.html'), encoding='utf-8').read()
        _keyline = [l for l in _tpl.split('\n') if 'const key = ' in l]
        check(len(_keyline) == 1 and 'r.source_file' in _keyline[0],
              'the pool key carries source_file, not just the position',
              _keyline[0].strip() if _keyline else 'not found')
        check('const [date, rn, srcFile] = key.split' in _tpl,
              'and the click handler destructures all three')
        check('source_file: srcFile || null' in _tpl,
              'and posts source_file to the server')

        # ── 4e. migration from hostile starting schemas ───────────────────────
        print('\n4e. migration on databases unlike the ones it was written against')
        import sqlite3 as _sq, importlib as _il2
        _BASE = ("CREATE TABLE assessments(id INTEGER PRIMARY KEY, name TEXT);"
                 "CREATE TABLE assessment_locations(id INTEGER PRIMARY KEY, assessment_id INTEGER);"
                 "CREATE TABLE sessions(id INTEGER PRIMARY KEY, date TEXT UNIQUE);"
                 "CREATE TABLE runs(id INTEGER PRIMARY KEY, session_id INTEGER,"
                 "  run_number INTEGER, source_file TEXT);")

        def _build(name, extra):
            path = os.path.join(tmp, name)
            cx = _sq.connect(path); cx.executescript(_BASE + extra); cx.commit(); cx.close()
            return path

        def _migrate_only(path):
            """Run the migration against `path` without disturbing the Side bindings."""
            prev = os.environ.get('NOISE_DB_PATH')
            os.environ['NOISE_DB_PATH'] = path
            for _m in ('noise_db', 'reports_db', 'assessments_db', 'sync_db', 'reports'):
                sys.modules.pop(_m, None)
            try:
                _nd = _il2.import_module('noise_db')
                _nd.init_db()
                return None
            except Exception as exc:
                return exc
            finally:
                if prev:
                    os.environ['NOISE_DB_PATH'] = prev
                for _m in ('noise_db', 'reports_db', 'assessments_db', 'sync_db', 'reports'):
                    sys.modules.pop(_m, None)

        # (a) a database predating the source_file column. The index used to be
        # created in the schema script, before the ALTER that adds the column.
        _p = _build('old_schema.db', """
            CREATE TABLE assessment_runs(id INTEGER PRIMARY KEY,
              assessment_id INTEGER NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
              location_id INTEGER, session_date TEXT NOT NULL, run_number INTEGER NOT NULL,
              conditions TEXT, notes TEXT,
              UNIQUE(assessment_id, session_date, run_number));
            INSERT INTO sessions(id,date) VALUES(1,'2026-08-12');
            INSERT INTO runs(id,session_id,run_number,source_file) VALUES(1,1,1,'PROJ0001');
            INSERT INTO assessments(id,name) VALUES(1,'A');
            INSERT INTO assessment_runs(id,assessment_id,session_date,run_number)
              VALUES(1,1,'2026-08-12',1);""")
        _err = _migrate_only(_p)
        check(_err is None, 'pre-source_file schema migrates', repr(_err))
        _cx = _sq.connect(_p)
        check(_cx.execute('SELECT source_file FROM assessment_runs').fetchone()[0] == 'PROJ0001',
              'and the existing link is backfilled')
        check('UNIQUE(assessment_id, session_date, run_number)' not in
              _cx.execute("SELECT sql FROM sqlite_master WHERE name='assessment_runs'").fetchone()[0],
              'and the positional UNIQUE is dropped')
        _cx.close()

        # (b) duplicated stable keys must refuse, not fail with a bare
        # IntegrityError naming nothing.
        _p = _build('dupe_keys.db', """
            CREATE TABLE assessment_runs(id INTEGER PRIMARY KEY,
              assessment_id INTEGER NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
              location_id INTEGER, session_date TEXT NOT NULL, run_number INTEGER,
              source_file TEXT, conditions TEXT, notes TEXT);
            INSERT INTO assessments(id,name) VALUES(1,'A');
            INSERT INTO assessment_runs(id,assessment_id,session_date,run_number,source_file)
              VALUES(1,1,'2026-08-12',5,'PROJ0005'),(2,1,'2026-08-12',6,'PROJ0005');""")
        _err = _migrate_only(_p)
        check(type(_err).__name__ == 'MigrationUnsafe',
              'duplicated stable keys refuse the migration', repr(_err))
        check('audit_assessment_run_keys' in str(_err),
              'and the message says how to resolve it')

        # (c) two runs sharing one source_file make the key ambiguous.
        _p = _build('dupe_runs.db', """
            CREATE TABLE assessment_runs(id INTEGER PRIMARY KEY,
              assessment_id INTEGER NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
              location_id INTEGER, session_date TEXT NOT NULL, run_number INTEGER,
              source_file TEXT, conditions TEXT, notes TEXT);
            INSERT INTO sessions(id,date) VALUES(1,'2026-08-12');
            INSERT INTO runs(id,session_id,run_number,source_file)
              VALUES(1,1,1,'PROJ0001'),(2,1,2,'PROJ0001');""")
        _err = _migrate_only(_p)
        check(type(_err).__name__ == 'MigrationUnsafe',
              'duplicated run identities refuse the migration', repr(_err))

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

        # The Noise Act permitted level depends on the underlying sound level and is
        # assessed indoors; there is no single number to compare an external LAeq to.
        lge = next(t for t in m.reports_db.DEFAULT_TEMPLATES
                   if t['name'] == 'Local Government Enforcement')
        check('35 dB LAeq indoors' not in lge['prompt'],
              'LGE: the old fixed 35/45 dB Noise Act figures are gone')
        check('underlying level' in lge['prompt'] and '34 dB LAeq' in lge['prompt'],
              'LGE: permitted level stated as depending on the underlying level')
        check('dwelling' in lge['prompt'], 'LGE: says it is assessed inside the dwelling')

        # Workplace action values are a daily personal exposure, not a measured LAeq.
        occ = next(t for t in m.reports_db.DEFAULT_TEMPLATES
                   if t['name'] == 'Occupational Health')
        check('LEP,d' in occ['prompt'], 'Occupational: uses LEP,d')
        check('dB LAeq,8h /' not in occ['prompt'],
              'Occupational: no longer calls the action values LAeq,8h')

        # ── 7. NOR140 xlsx parity with every Nortfr reference present ─────────
        print('\n7. xlsx export matches the Nortfr references')
        check_references(meas_root, tmp)

        # ── 8. WP4/F9 — CSV headers name the channel actually written ─────────
        # Reuses the Flask test client and 'rt' Side set up in section 4d — its
        # DB has the real SD-card sessions imported and noise_app is already
        # bound to it (the first import of noise_app in this process binds its
        # `from noise_db import ...` names to whichever noise_db module is
        # current at that moment, which was rt's).
        print('\n8. CSV export headers match their contents (F9)')

        resp = _client.get('/export/sessions.csv')
        check(resp.status_code == 200, 'sessions CSV downloads', resp.status_code)
        sess_header = resp.get_data(as_text=True).split('\r\n')[0].split(',')
        check('lafmax_max_db' in sess_header,
              'sessions CSV: lafmax_max_db header present (was laeq_max_db, held LAFmax)',
              sess_header)
        check('laeq_max_db' not in sess_header,
              'sessions CSV: stale laeq_max_db header gone', sess_header)
        check('laeq_avg_db' in sess_header,
              'sessions CSV: laeq_avg_db kept (WP5 changes its definition, not its name)',
              sess_header)

        aid8 = rt.assess.create_assessment('F9 CSV test')
        lid8 = rt.assess.add_assessment_location(aid8, 'L')
        rt.assess.assign_runs(aid8, lid8, [(SESSION_DATE, 9)])
        resp = _client.get(f'/export/assessment/{aid8}.csv')
        check(resp.status_code == 200, 'assessment CSV downloads', resp.status_code)
        lines = resp.get_data(as_text=True).split('\r\n')
        assess_header = lines[0].split(',')
        check('lafmin_db' in assess_header and 'lafmax_db' in assess_header,
              'assessment CSV: lafmin_db/lafmax_db headers present '
              '(were min_laeq_db/max_laeq_db, held LAFmin/LAFmax)', assess_header)
        check('min_laeq_db' not in assess_header and 'max_laeq_db' not in assess_header,
              'assessment CSV: stale min/max_laeq_db headers gone', assess_header)
        check('n_samples' in assess_header,
              'assessment CSV: n_samples header present (was mislabelled duration_s)',
              assess_header)
        check('duration_s' in assess_header,
              'assessment CSV: duration_s is now the real meter-stored value', assess_header)
        row1 = lines[1].split(',')
        check(len(row1) == len(assess_header),
              'assessment CSV: data row width matches header',
              f'{len(row1)} vs {len(assess_header)}')
        rt.assess.delete_assessment(aid8)

        # Per-run CSV headers are built client-side in index.html — assert the
        # source names the real fields (grep, since it's JS, not testable via
        # the Python data layer).
        idx_html = open(os.path.join(REPO, 'templates', 'index.html'), encoding='utf-8').read()
        run_hdr_line = [l for l in idx_html.split('\n') if "'run', 'start_time'" in l]
        check(len(run_hdr_line) == 1, 'exportSessionRuns header row found once',
              str(len(run_hdr_line)))
        hl = run_hdr_line[0]
        check('lafmin_db' in hl and 'lafmax_db' in hl,
              'exportSessionRuns: lafmin_db/lafmax_db headers present '
              '(were laeq_min_db/laeq_max_db, held LAFmin/LAFmax)', hl.strip())
        check('laeq_min_db' not in hl and 'laeq_max_db' not in hl,
              'exportSessionRuns: stale laeq_min_db/laeq_max_db gone', hl.strip())
        check("'duration_s'" in hl,
              'exportSessionRuns: duration_s header present (was p.n, the record count)',
              hl.strip())

        run_csv_fn = idx_html.split('function exportRunCSV(')[1].split('\nfunction ')[0]
        check("'lae_db'" in run_csv_fn,
              'exportRunCSV: lae_db header present (was laimax_db, holds the LAE channel)',
              run_csv_fn[:300])
        check("'lapeak_db'" in run_csv_fn,
              'exportRunCSV: lapeak_db header present (was lcpeak_db, holds LApeak, not true LCpeak)')
        check("'laimax_db'" not in run_csv_fn, 'exportRunCSV: stale laimax_db header gone')
        check("'lcpeak_db'" not in run_csv_fn, 'exportRunCSV: stale lcpeak_db header gone')

        # ── 9. security: startup, CSRF, API key, rate limiting ────────────────
        # Everything here drives the real app through the Flask test client.
        # `rt` (section 4d) is the Side noise_app is bound to: the module was
        # imported while that binding was current and captured its functions by
        # value, so the app reads and writes route.db no matter which Side was
        # constructed since.
        import logging as _lg
        import subprocess as _sp
        _wa = importlib.import_module('webauth')
        _napp = importlib.import_module('noise_app')
        _sapp = _napp.app

        print('\n9a. the app refuses to start with authentication missing')
        # The Mac has no flight-tracker auth module, which is exactly the
        # condition that used to make every page public.
        check(_wa.AUTH_AVAILABLE is False,
              'the auth module is absent here, as on any dev machine')
        _saved_allow = os.environ.pop('ALLOW_UNAUTHENTICATED', None)
        try:
            try:
                _wa.check_startup_security()
                _refused = False
            except _wa.InsecureConfiguration:
                _refused = True
            check(_refused, 'check_startup_security() refuses without the override')
        finally:
            if _saved_allow is not None:
                os.environ['ALLOW_UNAUTHENTICATED'] = _saved_allow
        _wa.check_startup_security()   # with the override set: must not raise
        check(True, 'and starts when ALLOW_UNAUTHENTICATED=1 is set')

        # The decisive check: a real interpreter importing the real module.
        # A refusal that only exists in a helper nobody calls is not a refusal.
        _env = dict(os.environ)
        _env.pop('ALLOW_UNAUTHENTICATED', None)
        _env['NOISE_DB_PATH'] = os.path.join(tmp, 'refuse.db')
        _proc = _sp.run([sys.executable, '-c', 'import noise_app'], cwd=REPO,
                        env=_env, capture_output=True, text=True)
        check(_proc.returncode != 0, 'importing noise_app without the override exits non-zero',
              f'rc={_proc.returncode}')
        check('InsecureConfiguration' in _proc.stderr,
              'and says why', _proc.stderr.strip().split('\n')[-1][:100])

        # An empty UPLOAD_PASSWORD leaves the upload form open; that must be
        # said out loud at startup rather than discovered later.
        _seen = []

        class _Capture(_lg.Handler):
            def emit(self, record):
                _seen.append(record)

        _cap = _Capture()
        _lg.getLogger('noise.webauth').addHandler(_cap)
        try:
            _wa.check_startup_security()
        finally:
            _lg.getLogger('noise.webauth').removeHandler(_cap)
        check(_wa.UPLOAD_PASS == '', 'no upload password is set in this environment')
        check(any(r.levelno == _lg.WARNING and 'UPLOAD_PASSWORD' in r.getMessage()
                  for r in _seen), 'an empty UPLOAD_PASSWORD is warned about at startup')
        check(any(r.levelno == _lg.ERROR for r in _seen),
              'and running unauthenticated is logged at ERROR')

        print('\n9b. session cookie flags')
        check(_sapp.config['SESSION_COOKIE_SAMESITE'] == 'Lax', 'SameSite=Lax')
        check(_sapp.config['SESSION_COOKIE_HTTPONLY'] is True, 'HttpOnly')
        check(_sapp.config['SESSION_COOKIE_SECURE'] is True,
              'Secure (default on; SESSION_COOKIE_SECURE=0 for plain-http LAN use)')

        print('\n9c. CSRF on cookie-authenticated state changes')
        _anon = _sapp.test_client()
        with _anon.session_transaction() as _s:
            _s['user'] = 'test'
            _s['logged_in'] = True          # logged in, but no token
        _r = _anon.post(f'/session/{SESSION_DATE}/edit',
                        data={'recorder_name': 'Mallory'})
        check(_r.status_code == 403, 'a logged-in POST with no token is refused',
              str(_r.status_code))
        _name_now = rt.sql('SELECT recorder_name FROM sessions WHERE date=?',
                           SESSION_DATE)[0]['recorder_name']
        check(_name_now != 'Mallory', 'and the write did not land', repr(_name_now))

        _r = _anon.post(f'/session/{SESSION_DATE}/edit',
                        data={'recorder_name': 'Mallory', 'csrf_token': 'not-the-token'})
        check(_r.status_code == 403, 'a wrong token is refused too', str(_r.status_code))

        _good = _sapp.test_client()
        with _good.session_transaction() as _s:
            _s['user'] = 'test'
            _s['logged_in'] = True
            _s['_csrf_token'] = SUITE_CSRF
        _r = _good.post(f'/session/{SESSION_DATE}/edit', headers=dict(
            CSRF_HDR, **{'X-Requested-With': 'XMLHttpRequest'}),
            data={'recorder_name': 'Catherine Ives-Yim'})
        check(_r.status_code == 200, 'the token in the X-CSRF-Token header is accepted',
              f'{_r.status_code}: {_r.get_data(as_text=True)[:80]}')
        _r = _good.post(f'/session/{SESSION_DATE}/edit',
                        data={'recorder_name': 'Catherine Ives-Yim',
                              'csrf_token': SUITE_CSRF})
        check(_r.status_code != 403, 'the token in a form field is accepted too',
              str(_r.status_code))
        check(rt.sql('SELECT recorder_name FROM sessions WHERE date=?',
                     SESSION_DATE)[0]['recorder_name'] == 'Catherine Ives-Yim',
              'and that write did land')
        check(_anon.get('/health').status_code == 200, 'GET is not affected')

        print('\n9d. the API key: header or form, never the query string')
        _saved_key = _wa.IMPORT_KEY
        _wa.IMPORT_KEY = 'a-long-random-import-key'
        try:
            _r = _anon.get(f'/api/sync?api_key={_wa.IMPORT_KEY}')
            check(_r.status_code == 403, 'a key in the query string is not accepted',
                  str(_r.status_code))
            _r = _anon.get('/api/sync', headers={'X-Import-Key': _wa.IMPORT_KEY})
            check(_r.status_code == 200, 'the same key in the header is',
                  str(_r.status_code))
            _r = _anon.get('/api/sync', headers={'X-Import-Key': 'a-long-random-import-kez'})
            check(_r.status_code == 403, 'a near-miss key is refused', str(_r.status_code))
            # An API-key request carries no cookie to ride on, so it is CSRF
            # exempt — import_sdcard.py and the peer Pi never see a page.
            _r = _anon.post('/import', headers={'X-Import-Key': _wa.IMPORT_KEY},
                            json={'sessions': []})
            check(_r.status_code == 400 and 'no sessions' in _r.get_data(as_text=True),
                  'an API-key POST needs no CSRF token', str(_r.status_code))
            _r = _anon.post('/import', json={'sessions': []})
            check(_r.status_code == 403, 'and without the key it is still refused',
                  str(_r.status_code))
        finally:
            _wa.IMPORT_KEY = _saved_key

        print('\n9e. the templates carry the token (the client half is not Python)')
        _tdir = os.path.join(REPO, 'templates')

        def _tpl_text(name):
            return open(os.path.join(_tdir, name), encoding='utf-8').read()

        for _name in ('index.html', 'manage.html', 'upload.html',
                      'assessments.html', 'reports.html', 'report.html', 'login.html'):
            check("{% include '_csrf.html' %}" in _tpl_text(_name),
                  f'{_name}: includes the CSRF partial')
        _partial = _tpl_text('_csrf.html')
        check("name=\"csrf-token\"" in _partial and 'X-CSRF-Token' in _partial,
              'the partial publishes the token and sets the fetch header')
        for _name, _n in (('manage.html', 3), ('login.html', 3), ('upload.html', 1)):
            _txt = _tpl_text(_name)
            _forms = _txt.lower().count('<form ')
            _fields = _txt.count('name="csrf_token"')
            check(_fields == _n == _forms,
                  f'{_name}: every posting form has a hidden csrf_token field',
                  f'{_fields} fields / {_forms} forms')
        for _name in os.listdir(_tdir):
            if _name.endswith('.html'):
                check('?api_key=' not in _tpl_text(_name),
                      f'{_name}: no API key in a query string')

        # A template that fails to render is a 500 on a page nothing else in
        # the suite visits, so every page is fetched and the token looked for.
        for _path in ('/', '/manage', '/upload', '/login', '/assessments', '/reports'):
            _r = _good.get(_path)
            check(_r.status_code == 200, f'GET {_path} renders', str(_r.status_code))
            check('name="csrf-token"' in _r.get_data(as_text=True),
                  f'GET {_path} publishes the token')

        print('\n9f. rate limiting on /login')
        if _napp.limiter is None:
            print('  --    Flask-Limiter not installed in this venv; limit not exercised')
        else:
            _lim = _sapp.test_client()
            with _lim.session_transaction() as _s:
                _s['_csrf_token'] = SUITE_CSRF
            _codes = [_lim.post('/login', headers=CSRF_HDR,
                                data={'step': 'email', 'email': f'a{i}@example.com',
                                      'csrf_token': SUITE_CSRF}).status_code
                      for i in range(11)]
            check(_codes[:10] == [200] * 10, 'ten login attempts a minute are allowed',
                  str(_codes[:10]))
            check(_codes[10] == 429, 'the eleventh is rate limited', str(_codes[10]))
            check(_lim.get('/login').status_code == 200,
                  'but the login page itself is still servable — the limit is POST only')

        print(f'\nAll {_checks} checks passed.')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else MEAS_DEFAULT)
