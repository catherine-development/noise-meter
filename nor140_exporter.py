"""
Build NOR140-compatible GLOBAL and PROFILE xlsx files from a run row dict.

Usage:
    from nor140_exporter import build_global_xlsx, build_profile_xlsx
    wb_bytes = build_global_xlsx(run, serial='6899108')
    wb_bytes = build_profile_xlsx(run, serial='6899108')

`run` is a dict as returned by noise_db.get_full_run_row() (includes session_date).
"""

import io
import json
from datetime import datetime, timedelta

from openpyxl import Workbook

# 36 1/3-octave centre-frequency column labels, matching Nortfr exactly
_FREQ_LABELS = [
    '6.3 Hz', '8.0 Hz', '10 Hz', '12.5 Hz', '16 Hz', '20 Hz',
    '25 Hz', '31.5 Hz', '40 Hz', '50 Hz', '63 Hz', '80 Hz',
    '100 Hz', '125 Hz', '160 Hz', '200 Hz', '250 Hz', '315 Hz',
    '400 Hz', '500 Hz', '630 Hz', '800 Hz',
    '1.0 kHz', '1.25 kHz', '1.6 kHz', '2.0 kHz', '2.5 kHz', '3.15 kHz',
    '4.0 kHz', '5.0 kHz', '6.3 kHz', '8.0 kHz', '10.0 kHz',
    '12.5 kHz', '16.0 kHz', '20.0 kHz',
]

# 18 spectral sheets: sheet index 0–17 (Nortfr order)
_SPECTRAL_SHEETS = [
    ('LfF,99.0%_8',  'spec_lff_l99'),
    ('LfF,95.0%_7',  'spec_lff_l95'),
    ('LfF,90.0%_6',  'spec_lff_l90'),
    ('LfF,50.0%_5',  'spec_lff_l50'),
    ('LfF,10.0%_4',  'spec_lff_l10'),
    ('LfF,5.0%_3',   'spec_lff_l5'),
    ('LfF,1.0%_2',   'spec_lff_l1'),
    ('LfF,0.1%_1',   'spec_lff_l01'),
    ('LfIE',         'spec_lfie'),
    ('LfImin',       'spec_lfimin'),
    ('LfImax',       'spec_lfimax'),
    ('LfIeq',        'spec_lfieq'),
    ('LfSmin',       'spec_lfsmin'),   # capital S — matches Nortfr
    ('LfSmax',       'spec_lfsmax'),
    ('LfE',          'spec_lfe'),
    ('LfFmin',       'spec_lffmin'),   # capital F — matches Nortfr
    ('LfFmax',       'spec_lffmax'),
    ('Lfeq',         'spec_lfeq'),
]

# 16 LAF/LCF percentile scalar sheets: sheets 18–33 (descending order: 99%→0.1%)
_SCALAR_PCT_SHEETS = [
    ('LCF,99.0%_8', 'lc_l99'),
    ('LCF,95.0%_7', 'lc_l95'),
    ('LCF,90.0%_6', 'lc_l90'),
    ('LCF,50.0%_5', 'lc_l50'),
    ('LCF,10.0%_4', 'lc_l10'),
    ('LCF,5.0%_3',  'lc_l5'),
    ('LCF,1.0%_2',  'lc_l1'),
    ('LCF,0.1%_1',  'lc_l01'),
    ('LAF,99.0%_8', 'la_l99'),
    ('LAF,95.0%_7', 'la_l95'),
    ('LAF,90.0%_6', 'la_l90'),
    ('LAF,50.0%_5', 'la_l50'),
    ('LAF,10.0%_4', 'la_l10'),
    ('LAF,5.0%_3',  'la_l5'),
    ('LAF,1.0%_2',  'la_l1'),
    ('LAF,0.1%_1',  'la_l01'),
]

# 22 broadband scalar sheets: sheets 34–55
_SCALAR_BB_SHEETS = [
    ('LCpeak',  'lcpeak'),
    ('LCIE',    'lcie'),
    ('LCE',     'lce'),
    ('LCIeq',   'lcieq'),
    ('LCeq',    'lceq'),
    ('LCImin',  'lcimin'),
    ('LCSmin',  'lcsmin'),
    ('LCFmin',  'lcfmin'),
    ('LCImax',  'lcimax'),
    ('LCSmax',  'lcsmax'),
    ('LCFmax',  'lcfmax'),
    ('LApeak',  'lapeak'),
    ('LAIE',    'laie'),
    ('LAE',     'lae'),
    ('LAIeq',   'laieq'),
    ('LAeq',    'avg_laeq'),
    ('LAImin',  'laimin'),
    ('LASmin',  'lasmin'),
    ('LAFmin',  'lafmin'),
    ('LAImax',  'laimax'),
    ('LASmax',  'lasmax'),
    ('LAFmax',  'lafmax'),
]

# PROFILE channels: sheets 0–4
_PROF_SHEETS = [
    ('LApeak',  'prof_lapeak_json'),
    ('LAE',     'prof_lae_json'),
    ('LAFmax',  'prof_lafmax_json'),
    ('LAeq',    'prof_laeq_json'),
    ('LAFspl',  'prof_lafspl_json'),
]

# Summary sheet scalar column order
_SUMMARY_SCALARS = [
    ('LAFmax',  'lafmax'),  ('LASmax',  'lasmax'),  ('LAImax',  'laimax'),
    ('LAFmin',  'lafmin'),  ('LASmin',  'lasmin'),  ('LAImin',  'laimin'),
    ('LAeq',    'avg_laeq'), ('LAIeq',  'laieq'),   ('LAE',     'lae'),
    ('LAIE',    'laie'),    ('LApeak',  'lapeak'),
    ('LCFmax',  'lcfmax'),  ('LCSmax',  'lcsmax'),  ('LCImax',  'lcimax'),
    ('LCFmin',  'lcfmin'),  ('LCSmin',  'lcsmin'),  ('LCImin',  'lcimin'),
    ('LCeq',    'lceq'),    ('LCIeq',   'lcieq'),   ('LCE',     'lce'),
    ('LCIE',    'lcie'),    ('LCpeak',  'lcpeak'),
    ('LAF,0.1%_1', 'la_l01'), ('LAF,1.0%_2', 'la_l1'),  ('LAF,5.0%_3',  'la_l5'),
    ('LAF,10.0%_4','la_l10'), ('LAF,50.0%_5','la_l50'), ('LAF,90.0%_6', 'la_l90'),
    ('LAF,95.0%_7','la_l95'), ('LAF,99.0%_8','la_l99'),
    ('LCF,0.1%_1', 'lc_l01'), ('LCF,1.0%_2', 'lc_l1'),  ('LCF,5.0%_3',  'lc_l5'),
    ('LCF,10.0%_4','lc_l10'), ('LCF,50.0%_5','lc_l50'), ('LCF,90.0%_6', 'lc_l90'),
    ('LCF,95.0%_7','lc_l95'), ('LCF,99.0%_8','lc_l99'),
]


# ── Datetime / duration helpers ──────────────────────────────────────────────

def _dt(run):
    """Extract start datetime from a run dict.
    start_time may be a bare time ('23:27:36') or a full ISO datetime string.
    """
    st = run.get('start_time', '') or ''
    if len(st) <= 8 and ':' in st:
        date = run.get('session_date', '1970-01-01')
        st = f'{date}T{st}'
    for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%SZ'):
        try:
            return datetime.strptime(st, fmt)
        except ValueError:
            pass
    return datetime.fromisoformat(st.replace('Z', ''))


def _fmt_dt(dt):
    """Format datetime for DATA rows: (2026-08-12 23:27:36.000)"""
    return dt.strftime('(%Y-%m-%d %H:%M:%S.000)')


def _fmt_trig_header(dt):
    """Format datetime for the Trig time HEADER cell: (2026/8/12 23:27:36.0)"""
    return f'({dt.year}/{dt.month}/{dt.day} {dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}.0)'


def _fmt_duration(seconds):
    """Format duration as (H:M:S.0) — no zero-padding on any component."""
    total = int(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f'({h}:{m}:{s}.0)'


def _rv(value):
    """Round a dB value to 1 decimal, or return None."""
    return round(value, 1) if value is not None else None


# ── Sheet writers ─────────────────────────────────────────────────────────────

def _write_header(ws, n, duration_s, trig_dt, label=None, period_s=None):
    """Write 8-row header block (rows 0-7).

    Row structure (value in col 1, format hint in col 2):
      0  Period length       | period_dur | H:M:S.mS   (one period; = total for GLOBAL)
      1  Total periods       | n
      2  Before trigger      | 0
      3  After trigger       | n
      4  Trig time           | trig_hdr   | Y-Mo-D H:M:S.mS
      5  Effective duration  | total_dur  | H:M:S.mS   (whole run)
      6  (blank)
      7  (blank) or [None, None, None, label] for spectral sheets

    period_s: duration of one period in seconds (defaults to duration_s).
              Set to 1 for PROFILE sheets where each row = 1 second.
    """
    if period_s is None:
        period_s = duration_s
    period_dur = _fmt_duration(period_s)
    total_dur = _fmt_duration(duration_s)
    trig_hdr = _fmt_trig_header(trig_dt)
    ws.append(['Period length', period_dur, 'H:M:S.mS'])
    ws.append(['Total number of periods', n])
    ws.append(['Number of periods before trigger', 0])
    ws.append(['Number of periods after trigger', n])
    ws.append(['Trig time', trig_hdr, 'Y-Mo-D H:M:S.mS'])
    ws.append(['Measurement effective duration', total_dur, 'H:M:S.mS'])
    ws.append([])
    if label is not None:
        ws.append([None, None, None, label])
    else:
        ws.append([])


def _write_spectral_sheet(ws, label, spec_json, trig_dt, duration_s):
    """Write one spectral (36-band) sheet.

    Row 7: [None, None, None, label]
    Row 8: ['Period:', 'Time:', None, '6.3 Hz', '8.0 Hz', ...]
    Row 9: [0, datetime, None, val0, val1, ...]
    """
    _write_header(ws, 1, duration_s, trig_dt, label=label)
    ws.append(['Period:', 'Time:', None] + _FREQ_LABELS)
    vals = json.loads(spec_json) if isinstance(spec_json, str) else (spec_json or [])
    ws.append([0, _fmt_dt(trig_dt), None] + [_rv(v) for v in vals])


def _write_scalar_sheet(ws, label, value, trig_dt, duration_s):
    """Write one scalar sheet.

    Row 7: (blank)
    Row 8: ['Period:', 'Time:', None, label]
    Row 9: [0, datetime, None, value]
    """
    _write_header(ws, 1, duration_s, trig_dt, label=None)
    ws.append(['Period:', 'Time:', None, label])
    ws.append([0, _fmt_dt(trig_dt), None, _rv(value)])


def _write_prof_sheet(ws, label, prof_json, trig_dt):
    """Write one PROFILE 1-second time-series sheet.

    PROF data is always stored at 1-second resolution, so period = 1 s.
    Row 7: (blank)
    Row 8: ['Period:', 'Time:', None, label]
    Row 9+: [i, datetime_i, None, value_i]
    """
    vals = json.loads(prof_json) if isinstance(prof_json, str) else (prof_json or [])
    n = len(vals)
    _write_header(ws, n, n, trig_dt, label=None, period_s=1)  # one period = 1 s
    ws.append(['Period:', 'Time:', None, label])
    for i, v in enumerate(vals):
        t = trig_dt + timedelta(seconds=i)
        ws.append([i, _fmt_dt(t), None, _rv(v)])


def _write_global_summary(ws, run, trig_dt, duration_s):
    """Write GLOBAL Summary sheet.

    Structure (no standard header block):
      Row 0: ['Period:', 'Time:', 'Duration:', None, None, scalar_headers...]
      Row 1-2: blank
      Row 3: [0, datetime, None, None, None, scalar_vals...]
    """
    scalar_headers = [name for name, _ in _SUMMARY_SCALARS]
    ws.append(['Period:', 'Time:', 'Duration:', None, None] + scalar_headers)
    ws.append([])
    ws.append([])
    scalar_vals = [_rv(run.get(col)) for _, col in _SUMMARY_SCALARS]
    ws.append([0, _fmt_dt(trig_dt), None, None, None] + scalar_vals)


def _write_global_setup(ws, run, serial, trig_dt, duration_s):
    """Write GLOBAL Setup sheet."""
    date_str = trig_dt.strftime('%y%m%d')
    run_num = run.get('run_number', 1)
    filename = f'NOR140_{serial}_{date_str}_{run_num:04d}.NBF'
    end_dt = trig_dt + timedelta(seconds=duration_s)
    dur_str = _fmt_duration(duration_s)
    rows = [
        ['File version', 'v1.0/6.1.1.51'],
        ['Filename', f'{filename} ({_fmt_trig_header(trig_dt)} - {_fmt_trig_header(end_dt)})'],
        ['Report type', 'GLOBAL'],
        ['Bandwidth', '1/3 Octave'],
        ['Frequency range', '6.3 Hz - 20.0 kHz'],
        ['Period length', dur_str],
        ['Total number of periods', 1],
        [], [], [], [],
        ['Full scale', 130, 'dB'],
        ['Sensitivity', -26.9, 'dB'],
    ]
    for r in rows:
        ws.append(r)


def _write_profile_summary(ws, run, trig_dt):
    """Write PROFILE Summary sheet (same column structure as GLOBAL Summary)."""
    prof_json = run.get('prof_laeq_json') or run.get('prof_lafspl_json')
    n = len(json.loads(prof_json)) if prof_json else (run.get('n_samples', 1) or 1)
    duration_s = n  # 1 s per measurement
    scalar_headers = [name for name, _ in _SUMMARY_SCALARS]
    ws.append(['Period:', 'Time:', 'Duration:', None, None] + scalar_headers)
    ws.append([])
    ws.append([])
    scalar_vals = [_rv(run.get(col)) for _, col in _SUMMARY_SCALARS]
    ws.append([0, _fmt_dt(trig_dt), None, None, None] + scalar_vals)


def _write_profile_setup(ws, run, serial, trig_dt):
    """Write PROFILE Setup sheet."""
    prof_json = run.get('prof_laeq_json') or run.get('prof_lafspl_json')
    n = len(json.loads(prof_json)) if prof_json else (run.get('n_samples', 1) or 1)
    duration_s = n  # 1 s per measurement
    date_str = trig_dt.strftime('%y%m%d')
    run_num = run.get('run_number', 1)
    filename = f'NOR140_{serial}_{date_str}_{run_num:04d}.NBF'
    end_dt = trig_dt + timedelta(seconds=duration_s)
    rows = [
        ['File version', 'v1.0/6.1.1.51'],
        ['Filename', f'{filename} ({_fmt_trig_header(trig_dt)} - {_fmt_trig_header(end_dt)})'],
        ['Report type', 'PROFILE'],
        ['Period length', _fmt_duration(1)],  # PROF data is always 1-second
        ['Total number of periods', n],
        [], [], [], [],
        ['Full scale', 130, 'dB'],
        ['Sensitivity', -26.9, 'dB'],
    ]
    for r in rows:
        ws.append(r)


# ── Public API ────────────────────────────────────────────────────────────────

def build_global_xlsx(run, serial=''):
    """Build NOR140-style GLOBAL xlsx. Returns bytes."""
    trig_dt = _dt(run)
    n_samples = run.get('n_samples', 1) or 1
    # n_samples is the count of actual 1-second measurements; 'step' is a chart
    # downsampling factor and must NOT multiply the real measurement duration.
    duration_s = n_samples

    wb = Workbook()
    wb.remove(wb.active)

    for name, col in _SPECTRAL_SHEETS:
        ws = wb.create_sheet(name)
        _write_spectral_sheet(ws, name, run.get(col) or '[]', trig_dt, duration_s)

    for name, col in _SCALAR_PCT_SHEETS:
        ws = wb.create_sheet(name)
        _write_scalar_sheet(ws, name, run.get(col), trig_dt, duration_s)

    for name, col in _SCALAR_BB_SHEETS:
        ws = wb.create_sheet(name)
        _write_scalar_sheet(ws, name, run.get(col), trig_dt, duration_s)

    ws = wb.create_sheet('Summary')
    _write_global_summary(ws, run, trig_dt, duration_s)

    ws = wb.create_sheet('Setup')
    _write_global_setup(ws, run, serial, trig_dt, duration_s)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_profile_xlsx(run, serial=''):
    """Build NOR140-style PROFILE xlsx. Returns bytes."""
    trig_dt = _dt(run)

    wb = Workbook()
    wb.remove(wb.active)

    for name, col in _PROF_SHEETS:
        ws = wb.create_sheet(name)
        _write_prof_sheet(ws, name, run.get(col) or '[]', trig_dt)

    ws = wb.create_sheet('Summary')
    _write_profile_summary(ws, run, trig_dt)

    ws = wb.create_sheet('Setup')
    _write_profile_setup(ws, run, serial, trig_dt)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_filename(run, serial, report_type):
    """Return the canonical Nortfr filename for a run export."""
    trig_dt = _dt(run)
    date_str = trig_dt.strftime('%y%m%d')
    run_num = run.get('run_number', 1)
    return f'NOR140_{serial}_{date_str}_{run_num:04d}_{report_type}.xlsx'
