"""
Parse NOR140 binary data from a ZIP upload.
Handles ZIPs from any folder level of the SD card:
  - Full SD card root (contains MEAS118/)
  - MEAS118/ folder
  - Date folder (YYMMDD/)
  - PART0000/ folder
  - PROJ folder (PROJnnnn/ with two DAT files inside)
  - Flat ZIP of just the two DAT files from one session
"""
import struct
import math
import zipfile
import io
import re

FLOOR_DB = 20     # NOR140 lower measurement limit — values below this are instrument noise floor
CAP_LAEQ = 130
CAP_PEAK = 145
_DB_OFFSET = 40.0  # NOR140 PROF stores (actual_dB + 40) * 100 as uint16
_SPECTRUM_OFFSET = 0x0428
_SPECTRUM_SCALE = 128.0
_SPECTRUM_DB_OFFSET = 20.0
_THIRD_OCTAVE_FREQS = (
    6.3, 8.0, 10.0, 12.5, 16.0, 20.0, 25.0, 31.5, 40.0, 50.0, 63.0, 80.0,
    100.0, 125.0, 160.0, 200.0, 250.0, 315.0, 400.0, 500.0, 630.0, 800.0,
    1000.0, 1250.0, 1600.0, 2000.0, 2500.0, 3150.0, 4000.0, 5000.0,
    6300.0, 8000.0, 10000.0, 12500.0, 16000.0, 20000.0,
)
EXCLUDE  = {'000101'}  # factory test date

_DATE_RE = re.compile(r'^\d{6}$')
_PROJ_RE = re.compile(r'^PROJ', re.IGNORECASE)


def bcd(b):
    return (b >> 4) * 10 + (b & 0xF)


def _freq_weight_a(freq_hz):
    f2 = freq_hz * freq_hz
    ra = (
        (12200.0 ** 2) * f2 * f2
        / (
            (f2 + 20.6 ** 2)
            * math.sqrt((f2 + 107.7 ** 2) * (f2 + 737.9 ** 2))
            * (f2 + 12200.0 ** 2)
        )
    )
    return 20.0 * math.log10(ra) + 2.0


def _freq_weight_c(freq_hz):
    f2 = freq_hz * freq_hz
    rc = (12200.0 ** 2) * f2 / ((f2 + 20.6 ** 2) * (f2 + 12200.0 ** 2))
    return 20.0 * math.log10(rc) + 0.06


def _spectrum_total(levels, weight_fn):
    return 10.0 * math.log10(
        sum(10.0 ** ((level + weight_fn(freq)) / 10.0)
            for freq, level in zip(_THIRD_OCTAVE_FREQS, levels))
    )


def _read_glob_spectrum(data, offset=_SPECTRUM_OFFSET):
    end = offset + len(_THIRD_OCTAVE_FREQS) * 2
    if len(data) < end:
        return []
    levels = [
        struct.unpack_from('<H', data, offset + i * 2)[0] / _SPECTRUM_SCALE - _SPECTRUM_DB_OFFSET
        for i in range(len(_THIRD_OCTAVE_FREQS))
    ]
    if not all(-20.0 <= v <= CAP_LAEQ for v in levels):
        return []
    return levels


def _read_glob(data):
    """Extract ISO date, HH:MM:SS start time, and global spectrum-derived levels."""
    o = 0x19
    yy, mo, dd, hh, mm, ss = (bcd(data[o + i]) for i in range(6))
    date = f"20{yy:02d}-{mo:02d}-{dd:02d}"
    start = f"{hh:02d}:{mm:02d}:{ss:02d}"
    lfeq_spectrum = _read_glob_spectrum(data)
    metrics = {}
    if lfeq_spectrum:
        metrics['lfeq_spectrum'] = lfeq_spectrum
        metrics['laeq'] = _spectrum_total(lfeq_spectrum, _freq_weight_a)
        metrics['lceq'] = _spectrum_total(lfeq_spectrum, _freq_weight_c)
    return date, start, metrics


def _read_prof(data):
    """Return list of [LAeq, f1, f2, f3, LCpeak] per second.
    NOR140 stores (actual_dB + 40) * 100 as uint16 LE; we subtract the offset here."""
    if len(data) < 13:
        return []
    return [
        [struct.unpack_from('<H', data, off + i * 2)[0] / 100 - _DB_OFFSET for i in range(5)]
        for off in range(3, len(data) - 9, 10)
    ]


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _downsample(arr, step):
    return [max(arr[i:i + step]) for i in range(0, len(arr), step)]


def _energy_avg(values):
    return 10 * math.log10(sum(10 ** (v / 10) for v in values) / len(values))


def _parse_session_files(glob_data, prof_data):
    """Parse one GLOB+PROF pair. Returns (date_str, run_dict) or (None, None)."""
    try:
        date, start, glob_metrics = _read_glob(glob_data)
    except Exception:
        return None, None

    recs = _read_prof(prof_data)
    if not recs:
        return date, None

    laeq_raw   = [_clamp(r[0], FLOOR_DB, CAP_LAEQ) for r in recs]
    lafmax_raw = [_clamp(r[1], FLOOR_DB, CAP_LAEQ) for r in recs]
    laimax_raw = [_clamp(r[2], FLOOR_DB, CAP_LAEQ) for r in recs]
    lcpeak_raw = [_clamp(r[4], FLOOR_DB, CAP_PEAK)  for r in recs]

    if max(laeq_raw) > 140:  # corrupted record
        return date, None

    n      = len(recs)
    step   = 1 if n <= 120 else 2 if n <= 300 else 5 if n <= 900 else 10

    # NOR140 global LAeq is reconstructed from the GLOB Lfeq 1/3-octave spectrum.
    # The older 0x0422 per-minute interpretation reads unrelated result-matrix cells.
    if 'laeq' in glob_metrics:
        leq = round(glob_metrics['laeq'], 2)
    else:
        leq = round(_energy_avg(laeq_raw), 2)

    return date, {
        'start':  start,
        'n':      n,
        'step':   step,
        'avg':    leq,
        'lc_eq':  round(glob_metrics['lceq'], 2) if 'lceq' in glob_metrics else None,
        'mn':     round(min(laeq_raw), 1),
        'mx':     round(max(laeq_raw), 1),
        'pmx':    round(max(lcpeak_raw), 1),
        'pmxf':   round(max(lafmax_raw), 1),
        'pmxi':   round(max(laimax_raw), 1),
        'laeq':   [round(v, 1) for v in _downsample(laeq_raw,   step)],
        'lafmax': [round(v, 1) for v in _downsample(lafmax_raw, step)],
        'laimax': [round(v, 1) for v in _downsample(laimax_raw, step)],
        'lcpeak': [round(v, 1) for v in _downsample(lcpeak_raw, step)],
    }


def _classify_filename(fname):
    """
    Return 'glob' or 'prof' if the filename is a GLOB/PROF data file, else None.
    Accepts GLOB0001.DAT, GLOB0001.DATA, glob0001.dat, etc.
    """
    up = fname.upper()
    stem = up.split('.')[0]  # strip extension — accept any .DAT / .DATA / etc.
    if stem.startswith('GLOB'):
        return 'glob'
    if stem.startswith('PROF'):
        return 'prof'
    return None


def _proj_key_from_filename(fname):
    """
    Derive a PROJ sort key from a GLOB/PROF filename when no PROJ folder is
    present in the path. E.g. 'GLOB0001.DAT' → 'PROJ0001'.
    Falls back to 'PROJ0000'.
    """
    up = fname.upper()
    stem = up.split('.')[0]
    digits = stem[4:]  # strip 'GLOB' or 'PROF'
    return f"PROJ{digits}" if digits.isdigit() else 'PROJ0000'


def parse_files(file_pairs):
    """
    Parse NOR140 data from a list of (relative_path, bytes) tuples.
    Used for folder uploads where the browser sends files individually.
    Assembles an in-memory ZIP so the same parse_zip logic applies.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for path, data in file_pairs:
            zf.writestr(path, data)
    return parse_zip(buf.getvalue())


def parse_zip(zip_bytes):
    """
    Parse a ZIP of NOR140 SD card data from any folder level.

    The SD card structure is:
      MEAS118/ → YYMMDD/ → PART0000/ → PROJnnnn/ → GLOBnnnn.DAT + PROFnnnn.DAT

    The ZIP may start at any level: SD root, MEAS118, a date folder, PART folder,
    a single PROJ folder, or even a flat ZIP of just the two DAT files.

    Returns a list of session dicts: [{d, avg, mx, projects:[...]}, ...]
    """
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    files = {n: zf.read(n) for n in zf.namelist() if not n.endswith('/')}

    # Collect GLOB/PROF pairs keyed by (date_folder_or_sentinel, proj_key).
    # date_folder: a 6-digit string from the path, or '__from_glob__' if absent.
    # proj_key:    a 'PROJnnnn' string from the path, or synthesised from filename.
    pairs = {}  # (date_folder, proj_key) -> {'glob': bytes, 'prof': bytes}

    for path, data in files.items():
        parts = [p for p in path.replace('\\', '/').split('/') if p]
        fname = parts[-1]
        kind = _classify_filename(fname)
        if kind is None:
            continue

        # Walk path parts to find date folder and PROJ folder.
        date_folder = None
        proj_folder = None
        for part in parts[:-1]:  # exclude the filename itself
            up = part.upper()
            if _DATE_RE.match(part) and part not in EXCLUDE:
                date_folder = part
            elif _PROJ_RE.match(up):
                proj_folder = up

        # If no PROJ folder in path, synthesise one from the filename number.
        if proj_folder is None:
            proj_folder = _proj_key_from_filename(fname)

        date_key = date_folder if date_folder else '__from_glob__'
        key = (date_key, proj_folder)
        pairs.setdefault(key, {})
        pairs[key][kind] = data

    # Parse each complete GLOB+PROF pair and group by resolved date.
    by_date = {}
    for (date_key, proj_folder), pair in sorted(pairs.items()):
        if 'glob' not in pair or 'prof' not in pair:
            continue
        date, run = _parse_session_files(pair['glob'], pair['prof'])
        if not date or not run:
            continue
        if date in ('2001-01-01', '2000-01-01'):  # factory/unset dates
            continue
        by_date.setdefault(date, []).append((proj_folder, run))

    # Assemble into per-day session dicts.
    result = []
    for date in sorted(by_date):
        projects = [{**run, 'source_file': pf} for pf, run in sorted(by_date[date])]
        if not projects:
            continue
        avg = round(_energy_avg([p['avg'] for p in projects]), 2)
        result.append({
            'd':        date,
            'avg':      avg,
            'mx':       round(max(p['mx'] for p in projects), 1),
            'projects': projects,
        })

    return result
