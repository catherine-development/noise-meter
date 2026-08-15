"""
Backfill full 1-second PROF time series into the `runs` table.

Reads PROF*.DAT files from a MEAS118.zip and stores the five 1-second channels
(LAFspl, LAeq, LAFmax, LAE, LApeak) as JSON arrays in:
  prof_lafspl_json, prof_laeq_json, prof_lafmax_json, prof_lae_json, prof_lapeak_json

Usage:
    python3 backfill_prof.py /path/to/MEAS118.zip
    python3 backfill_prof.py /path/to/MEAS118.zip --dry-run
"""
import json, os, re, struct, sys, zipfile, sqlite3

DB_PATH  = os.environ.get('NOISE_DB_PATH', '/home/flightdata/noise-meter/noise.db')
_DATE_RE = re.compile(r'^\d{6}$')
_PROJ_RE = re.compile(r'^PROJ', re.IGNORECASE)
EXCLUDE  = {'000101'}

_FLOOR_DB = 20.0
_CAP_LAEQ = 130.0
_CAP_PEAK = 145.0


def _decode(raw):
    return raw / 128.0 - 20.0


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _read_prof(data):
    """Return list of [LAFspl, LAeq, LAFmax, LAE, LApeak] per second."""
    if len(data) < 13:
        return []
    return [
        [_decode(struct.unpack_from('<H', data, off + i * 2)[0]) for i in range(5)]
        for off in range(3, len(data) - 9, 10)
    ]


def _yymmdd(iso_date):
    """'2026-08-12' → '260812'."""
    return iso_date[2:4] + iso_date[5:7] + iso_date[8:10]


def _index_zip(zip_path):
    """Build (date_folder, proj_folder) → PROF bytes from any-level MEAS118.zip."""
    index = {}
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if name.endswith('/'):
                continue
            parts = [p for p in name.replace('\\', '/').split('/') if p]
            fname = parts[-1].upper()
            if not fname.startswith('PROF'):
                continue
            date_folder = proj_folder = None
            for part in parts[:-1]:
                if _DATE_RE.match(part) and part not in EXCLUDE:
                    date_folder = part
                elif _PROJ_RE.match(part.upper()):
                    proj_folder = part.upper()
            if proj_folder is None:
                stem = fname.split('.')[0]
                digits = stem[4:]
                proj_folder = f'PROJ{digits}' if digits.isdigit() else 'PROJ0000'
            if date_folder:
                index[(date_folder, proj_folder)] = zf.read(name)
    return index


def main():
    args    = [a for a in sys.argv[1:] if not a.startswith('--')]
    dry_run = '--dry-run' in sys.argv

    if not args:
        print(f'Usage: python3 {sys.argv[0]} /path/to/MEAS118.zip [--dry-run]')
        sys.exit(1)
    zip_path = args[0]

    print(f'ZIP:      {zip_path}')
    print(f'DB:       {DB_PATH}')
    print(f'Dry-run:  {dry_run}\n')

    index = _index_zip(zip_path)
    print(f'PROF files in ZIP: {len(index)}')

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute('''
        SELECT r.id, r.run_number, r.source_file, r.n_samples, s.date
        FROM runs r
        JOIN sessions s ON r.session_id = s.id
        WHERE r.prof_lafspl_json IS NULL
        ORDER BY s.date, r.run_number
    ''').fetchall()

    print(f'Runs to backfill:  {len(rows)}\n')
    if not rows:
        print('Nothing to do — all runs already have 1s PROF data.')
        conn.close()
        return

    updated = not_found = parse_fail = 0

    for row in rows:
        date_folder = _yymmdd(row['date'])
        proj_folder = (row['source_file'] or '').strip().upper()
        key         = (date_folder, proj_folder)
        prof_data   = index.get(key)

        if prof_data is None:
            not_found += 1
            print(f'  NOT FOUND  {row["date"]}  {proj_folder}')
            continue

        recs = _read_prof(prof_data)
        if not recs:
            parse_fail += 1
            print(f'  PARSE FAIL {row["date"]}  {proj_folder}  (len={len(prof_data)})')
            continue

        lafspl = [round(_clamp(r[0], _FLOOR_DB, _CAP_LAEQ), 1) for r in recs]
        laeq   = [round(_clamp(r[1], _FLOOR_DB, _CAP_LAEQ), 1) for r in recs]
        lafmax = [round(_clamp(r[2], _FLOOR_DB, _CAP_LAEQ), 1) for r in recs]
        lae    = [round(_clamp(r[3], _FLOOR_DB, _CAP_LAEQ), 1) for r in recs]
        lapeak = [round(_clamp(r[4], _FLOOR_DB, _CAP_PEAK),  1) for r in recs]

        if dry_run:
            print(f'  WOULD UPDATE  {row["date"]}  {proj_folder}  n={len(recs)}')
        else:
            conn.execute(
                'UPDATE runs SET '
                '  prof_lafspl_json=?, prof_laeq_json=?, prof_lafmax_json=?, '
                '  prof_lae_json=?, prof_lapeak_json=? '
                'WHERE id=?',
                (json.dumps(lafspl), json.dumps(laeq), json.dumps(lafmax),
                 json.dumps(lae), json.dumps(lapeak), row['id'])
            )
            updated += 1

    if not dry_run:
        conn.commit()

    conn.close()
    verb = 'Would update' if dry_run else 'Updated'
    print(f'\n{verb}: {updated}  |  Not in ZIP: {not_found}  |  Parse fail: {parse_fail}')
    if not_found:
        print('(Not-in-ZIP runs may be from an older SD card not included in this ZIP.)')


if __name__ == '__main__':
    main()
