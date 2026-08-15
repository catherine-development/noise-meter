#!/usr/bin/env python3
"""
backfill_glob.py — Re-parse GLOB spectral data and backfill new scalar columns.

Reads every GLOB file from the SD card ZIP, matches it to an existing run in the
database by date + source_file (PROJ folder), then does a targeted UPDATE of only
the GLOB-derived scalar columns.  No session metadata, location tags, assessment
assignments, or PROF-derived profile arrays are touched.

Also corrects avg_laeq where it was set from the old PROF energy-average (which
overestimates LAeq by 8–11 dB for impulsive sessions).

Usage:
    # On the Pi (ZIP already on the Pi):
    python3 backfill_glob.py /home/flightdata/MEAS118.zip

    # On Mac against a local copy of the DB:
    NOISE_DB_PATH=/path/to/noise.db python3 backfill_glob.py MEAS118.zip

    # Dry-run (print what would change, no writes):
    python3 backfill_glob.py MEAS118.zip --dry-run
"""
import os, re, struct, sys, zipfile, sqlite3

DB_PATH  = os.environ.get('NOISE_DB_PATH', '/home/flightdata/noise-meter/noise.db')
_DATE_RE = re.compile(r'^\d{6}$')
_PROJ_RE = re.compile(r'^PROJ', re.IGNORECASE)
_EXCLUDE = {'000101'}

# NOR140-B scalar metric offsets in the GLOB file (uint16 LE, decode = raw/128 - 20)
_GLOB_SCALAR_OFFSETS = {
    'laeq':   0x03cd, 'lceq':   0x03e7,
    'lae':    0x03d1, 'lce':    0x03eb,
    'laie':   0x03d3, 'lcie':   0x03ed,
    'laieq':  0x03cf, 'lcieq':  0x03e9,
    'lafmax': 0x03c1, 'lcfmax': 0x03db,
    'lafmin': 0x03c7, 'lcfmin': 0x03e1,
    'lasmax': 0x03c3, 'lcsmax': 0x03dd,
    'lasmin': 0x03c9, 'lcsmin': 0x03e3,
    'laimax': 0x03c5, 'lcimax': 0x03df,
    'laimin': 0x03cb, 'lcimin': 0x03e5,
    'la_l01': 0x0408, 'la_l1':  0x040a, 'la_l5':  0x040c,
    'la_l10': 0x040e, 'la_l50': 0x0410, 'la_l90': 0x0412,
    'la_l95': 0x0414, 'la_l99': 0x0416,
    'lapeak': 0x03d5,
    'lcpeak': 0x03ef,
    'lc_l01': 0x0418, 'lc_l1':  0x041a, 'lc_l5':  0x041c,
    'lc_l10': 0x041e, 'lc_l50': 0x0420, 'lc_l90': 0x0422,
    'lc_l95': 0x0424, 'lc_l99': 0x0426,
}

# Columns to backfill (all added in the spectral tables migration, except laeq which
# is used to correct avg_laeq where it was previously computed from PROF).
_SCALAR_COLS = [
    'lceq',   'lae',    'lce',
    'lafmax', 'lcfmax', 'lafmin', 'lcfmin',
    'lasmax', 'lcsmax', 'lasmin', 'lcsmin',
    'laieq',  'lcieq',  'laimax', 'lcimax', 'laimin', 'lcimin',
    'laie',   'lcie',
    'la_l01', 'la_l1',  'la_l5',
    'la_l10', 'la_l50', 'la_l90', 'la_l95', 'la_l99',
    'lapeak', 'lcpeak',
    'lc_l01', 'lc_l1',  'lc_l5',
    'lc_l10', 'lc_l50', 'lc_l90', 'lc_l95', 'lc_l99',
]


def _decode(raw):
    return raw / 128.0 - 20.0


def _bcd(b):
    return (b >> 4) * 10 + (b & 0xF)


def _glob_metrics(data):
    """Read manufacturer scalar metrics from GLOB binary. Returns dict or None."""
    try:
        o = 0x19
        mo, dd = _bcd(data[o + 1]), _bcd(data[o + 2])
        if not (1 <= mo <= 12 and 1 <= dd <= 31):
            return None
    except (IndexError, ValueError):
        return None
    m = {}
    for key, off in _GLOB_SCALAR_OFFSETS.items():
        if off + 1 < len(data):
            m[key] = round(_decode(struct.unpack_from('<H', data, off)[0]), 2)
    return m if m else None


def _yymmdd(iso_date):
    """'2026-08-12' → '260812'."""
    return iso_date[2:4] + iso_date[5:7] + iso_date[8:10]


def _index_zip(zip_path):
    """Return dict mapping (date_folder, proj_folder) → raw GLOB bytes."""
    index = {}
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if name.endswith('/'):
                continue
            parts = [p for p in name.replace('\\', '/').split('/') if p]
            fname = parts[-1].upper()
            if not fname.startswith('GLOB'):
                continue
            date_folder = proj_folder = None
            for part in parts[:-1]:
                if _DATE_RE.match(part) and part not in _EXCLUDE:
                    date_folder = part
                elif _PROJ_RE.match(part):
                    proj_folder = part.upper()
            if date_folder and proj_folder:
                index[(date_folder, proj_folder)] = zf.read(name)
    return index


def main():
    dry_run = '--dry-run' in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if not args:
        print(f'Usage: python3 {sys.argv[0]} /path/to/MEAS118.zip [--dry-run]')
        sys.exit(1)
    zip_path = args[0]

    print(f'ZIP:      {zip_path}')
    print(f'DB:       {DB_PATH}')
    print(f'Dry-run:  {dry_run}\n')

    index = _index_zip(zip_path)
    print(f'GLOB files in ZIP: {len(index)}')

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute('''
        SELECT r.id, r.run_number, r.source_file, r.avg_laeq, s.date
        FROM runs r
        JOIN sessions s ON r.session_id = s.id
        WHERE r.lapeak IS NULL
        ORDER BY s.date, r.run_number
    ''').fetchall()

    print(f'Runs to backfill:  {len(rows)}\n')
    if not rows:
        print('Nothing to do — all runs already have GLOB scalar data.')
        conn.close()
        return

    updated = skipped = not_found = parse_fail = 0

    for row in rows:
        date_folder  = _yymmdd(row['date'])
        proj_folder  = (row['source_file'] or '').strip().upper()
        key          = (date_folder, proj_folder)
        glob_data    = index.get(key)

        if glob_data is None:
            not_found += 1
            print(f'  NOT FOUND  {row["date"]}  {proj_folder}')
            continue

        metrics = _glob_metrics(glob_data)
        if not metrics:
            parse_fail += 1
            print(f'  PARSE FAIL {row["date"]}  {proj_folder}  '
                  f'(short GLOB? len={len(glob_data)})')
            continue

        # Build the SET clause — only new scalar cols + avg_laeq correction
        set_cols  = list(_SCALAR_COLS)
        set_vals  = [metrics.get(c) for c in _SCALAR_COLS]

        # Correct avg_laeq with GLOB-derived LAeq (more accurate than old PROF value)
        if metrics.get('laeq') is not None:
            set_cols.append('avg_laeq')
            set_vals.append(metrics['laeq'])

        set_clause = ', '.join(f'{c}=?' for c in set_cols)
        set_vals.append(row['id'])

        if dry_run:
            laeq_was  = row['avg_laeq']
            laeq_now  = metrics.get('laeq')
            la90      = metrics.get('la_l90')
            la10      = metrics.get('la_l10')
            print(f'  WOULD UPDATE  {row["date"]}  {proj_folder}  '
                  f'LAeq {laeq_was}→{laeq_now}  LA90={la90}  LA10={la10}')
        else:
            conn.execute(f'UPDATE runs SET {set_clause} WHERE id=?', set_vals)
            updated += 1

    if not dry_run:
        conn.commit()

    conn.close()
    verb = 'Would update' if dry_run else 'Updated'
    print(f'\n{verb}: {updated}  |  Not in ZIP: {not_found}  |  Parse fail: {parse_fail}')
    if not_found:
        print("(Not-in-ZIP runs may be from an older SD card not included in this ZIP.)")


if __name__ == '__main__':
    main()
