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
import json, os, re, sys, zipfile, sqlite3
from nor140_format import (bcd, read_glob_scalars, read_glob_spectral_tables,
                           read_duration_s, read_full_scale, read_end_time)
# This script rewrites runs.avg_laeq, so the session row that aggregates it has
# to be rebuilt afterwards or it keeps a stale value indefinitely.
from noise_db import recompute_session_aggregates

DB_PATH  = os.environ.get('NOISE_DB_PATH', '/home/flightdata/noise-meter/noise.db')
_DATE_RE = re.compile(r'^\d{6}$')
_PROJ_RE = re.compile(r'^PROJ', re.IGNORECASE)
_EXCLUDE = {'000101'}

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


def _glob_metrics(data):
    """Read manufacturer scalar metrics from GLOB binary. Returns dict or None."""
    try:
        o = 0x19
        mo, dd = bcd(data[o + 1]), bcd(data[o + 2])
        if not (1 <= mo <= 12 and 1 <= dd <= 31):
            return None
    except (IndexError, ValueError):
        return None
    m = read_glob_scalars(data, digits=2)
    return m if m else None


def _read_spectra(data):
    """Read all 18 spectral tables. Returns dict of col_name → JSON string (or None)."""
    tables = read_glob_spectral_tables(data, digits=2)
    return {col: (json.dumps(bands) if bands is not None else None) for col, bands in tables.items()}


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
        WHERE r.spec_lfeq IS NULL
        ORDER BY s.date, r.run_number
    ''').fetchall()

    print(f'Runs to backfill:  {len(rows)}\n')
    if not rows:
        print('Nothing to do — all runs already have spectral data.')
        conn.close()
        return

    updated = skipped = not_found = parse_fail = 0
    touched_dates = set()

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

        spectra = _read_spectra(glob_data)

        # Header fields read straight from the GLOB. These are only written on
        # fresh import, so rows that predate each column stay NULL until a
        # backfill runs. end_time in particular is not derivable — see
        # nor140_format.END_TIME_OFFSET.
        header = {
            'duration_s': read_duration_s(glob_data),
            'full_scale': read_full_scale(glob_data),
            'end_time':   read_end_time(glob_data),
        }

        # Build the SET clause — scalar cols + spectral JSON cols + avg_laeq correction
        set_cols  = list(_SCALAR_COLS)
        set_vals  = [metrics.get(c) for c in _SCALAR_COLS]

        for col, val in spectra.items():
            set_cols.append(col)
            set_vals.append(val)

        for col, val in header.items():
            if val is not None:
                set_cols.append(col)
                set_vals.append(val)

        # Correct avg_laeq with GLOB-derived LAeq (more accurate than old PROF value)
        if metrics.get('laeq') is not None:
            set_cols.append('avg_laeq')
            set_vals.append(metrics['laeq'])

        set_clause = ', '.join(f'{c}=?' for c in set_cols)
        set_vals.append(row['id'])

        touched_dates.add(row['date'])
        if dry_run:
            laeq_was  = row['avg_laeq']
            laeq_now  = metrics.get('laeq')
            la90      = metrics.get('la_l90')
            la10      = metrics.get('la_l10')
            print(f'  WOULD UPDATE  {row["date"]}  {proj_folder}  '
                  f'LAeq {laeq_was}→{laeq_now}  LA90={la90}  LA10={la10}  '
                  f'end={header["end_time"]}')
        else:
            conn.execute(f'UPDATE runs SET {set_clause} WHERE id=?', set_vals)
            updated += 1

    if not dry_run:
        conn.commit()

    conn.close()
    verb = 'Would update' if dry_run else 'Updated'
    print(f'\n{verb}: {updated}  |  Not in ZIP: {not_found}  |  Parse fail: {parse_fail}')

    # Rebuild the session rows for every date whose runs changed. Without this the
    # session keeps the LAeq computed at original import — for the 2026-08-12
    # session that was 83.58 dB against a true 74.85 dB, the old 0x0422 bug
    # surviving at session level long after the runs were corrected.
    if dry_run:
        print(f'Would recompute session aggregates for {len(touched_dates)} date(s).')
    elif touched_dates:
        changed = recompute_session_aggregates(sorted(touched_dates))
        print(f'Session aggregates recomputed: {len(changed)} changed')
        for date, old, new in changed:
            print(f'  {date}  avg_laeq {old} → {new}')
    if not_found:
        print("(Not-in-ZIP runs may be from an older SD card not included in this ZIP.)")


if __name__ == '__main__':
    main()
