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

CAP_LAEQ = 130
CAP_PEAK = 145
_DB_OFFSET = 40.0  # NOR140 PROF stores (actual_dB + 40) * 100 as uint16
EXCLUDE  = {'000101'}  # factory test date

_DATE_RE = re.compile(r'^\d{6}$')
_PROJ_RE = re.compile(r'^PROJ', re.IGNORECASE)


def bcd(b):
    return (b >> 4) * 10 + (b & 0xF)


def _read_glob(data):
    """Extract ISO date and HH:MM:SS start time from a GLOB file."""
    o = 0x19
    yy, mo, dd, hh, mm, ss = (bcd(data[o + i]) for i in range(6))
    return f"20{yy:02d}-{mo:02d}-{dd:02d}", f"{hh:02d}:{mm:02d}:{ss:02d}"


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
        date, start = _read_glob(glob_data)
    except Exception:
        return None, None

    recs = _read_prof(prof_data)
    if not recs:
        return date, None

    laeq_raw   = [_clamp(r[0], 10, CAP_LAEQ) for r in recs]
    lcpeak_raw = [_clamp(r[4], 10, CAP_PEAK)  for r in recs]

    if max(laeq_raw) > 140:  # corrupted record
        return date, None

    n    = len(recs)
    step = 1 if n <= 120 else 2 if n <= 300 else 5 if n <= 900 else 10
    leq  = round(_energy_avg(laeq_raw), 2)

    return date, {
        'start':  start,
        'n':      n,
        'step':   step,
        'avg':    leq,
        'mn':     round(min(laeq_raw), 1),
        'mx':     round(max(laeq_raw), 1),
        'pmx':    round(max(lcpeak_raw), 1),
        'laeq':   [round(v, 1) for v in _downsample(laeq_raw,   step)],
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
        projects = [run for _, run in sorted(by_date[date])]
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
