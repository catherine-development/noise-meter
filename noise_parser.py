"""
Parse NOR140 binary files from a ZIP upload or filesystem path.
Used server-side on the Pi.
"""
import struct
import math
import zipfile
import io
import os

CAP_LAEQ  = 130
CAP_PEAK  = 160
EXCLUDE   = {'000101'}


def bcd(b):
    return (b >> 4) * 10 + (b & 0xF)


def _read_glob(data):
    o = 0x19
    yy, mo, dd, hh, mm, ss = (bcd(data[o + i]) for i in range(6))
    return f"20{yy:02d}-{mo:02d}-{dd:02d}", f"{hh:02d}:{mm:02d}:{ss:02d}"


def _read_prof(data):
    if len(data) < 13:
        return []
    return [
        [struct.unpack_from('<H', data, off + i * 2)[0] / 100 for i in range(5)]
        for off in range(3, len(data) - 9, 10)
    ]


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _downsample(arr, step):
    return [max(arr[i:i + step]) for i in range(0, len(arr), step)]


def _energy_avg(values):
    return 10 * math.log10(sum(10 ** (v / 10) for v in values) / len(values))


def _parse_session_files(glob_data, prof_data):
    """Parse one GLOB+PROF pair into a run dict, or None if invalid."""
    try:
        date, start = _read_glob(glob_data)
    except Exception:
        return None, None

    recs = _read_prof(prof_data)
    if not recs:
        return date, None

    laeq_raw   = [_clamp(r[0], 30, CAP_LAEQ) for r in recs]
    lcpeak_raw = [_clamp(r[4], 30, CAP_PEAK)  for r in recs]

    if max(laeq_raw) > 200:  # corrupted
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


def parse_zip(zip_bytes):
    """
    Parse a ZIP of an SD card MEAS118 folder (or a single date folder).
    Returns list of session dicts: [{d, avg, mx, projects:[...]}, ...]
    """
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    names = zf.namelist()

    # Build a lookup: path → bytes
    files = {n: zf.read(n) for n in names if not n.endswith('/')}

    # Group GLOB/PROF pairs by (date_folder, part, proj)
    # Paths may be: MEAS118/260810/PART0000/PROJ0001/GLOB0001.DAT
    #           or: 260810/PART0000/PROJ0001/GLOB0001.DAT
    #           or: PART0000/PROJ0001/GLOB0001.DAT  (single session)
    sessions = {}  # date_str -> {proj_key -> {glob, prof}}

    for path, data in files.items():
        parts = [p for p in path.replace('\\', '/').split('/') if p]
        fname = parts[-1].upper()
        if not (fname.startswith('GLOB') or fname.startswith('PROF')):
            continue

        # Find the date folder (6 digits) and proj folder
        date_folder = None
        proj_folder = None
        for p in parts:
            if p.isdigit() and len(p) == 6 and p not in EXCLUDE:
                date_folder = p
            if p.upper().startswith('PROJ'):
                proj_folder = p.upper()

        if not proj_folder:
            continue

        # If no date folder found, treat it as a single-session upload
        if not date_folder:
            date_folder = '__upload__'

        key = (date_folder, proj_folder)
        sessions.setdefault(key, {})
        if fname.startswith('GLOB'):
            sessions[key]['glob'] = data
        elif fname.startswith('PROF'):
            sessions[key]['prof'] = data

    # Build session list grouped by date
    by_date = {}
    for (date_folder, proj_folder), pair in sorted(sessions.items()):
        if 'glob' not in pair or 'prof' not in pair:
            continue
        date, run = _parse_session_files(pair['glob'], pair['prof'])
        if not date or not run:
            continue
        by_date.setdefault(date, []).append((proj_folder, run))

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
