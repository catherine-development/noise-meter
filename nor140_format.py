"""
Single source of truth for NOR140-B binary format constants, offsets, and decode logic.

Both GLOB scalars/spectra and PROF channel values decode as:
    dB = uint16_le / 128 - 20

This module is imported by noise_parser.py, backfill_glob.py, backfill_prof.py,
and nor140_exporter.py so that format knowledge (offsets, decode formula,
rounding convention) lives in exactly one place.
"""
import struct
import math

DB_SCALE  = 128.0
DB_OFFSET = 20.0
FLOOR_DB  = 20.0
CAP_LAEQ  = 130.0
CAP_PEAK  = 145.0

FREQ_LABELS = [
    '6.3 Hz',  '8.0 Hz',  '10 Hz',   '12.5 Hz', '16 Hz',   '20 Hz',
    '25 Hz',   '31.5 Hz', '40 Hz',   '50 Hz',   '63 Hz',   '80 Hz',
    '100 Hz',  '125 Hz',  '160 Hz',  '200 Hz',  '250 Hz',  '315 Hz',
    '400 Hz',  '500 Hz',  '630 Hz',  '800 Hz',
    '1.0 kHz', '1.25 kHz','1.6 kHz', '2.0 kHz', '2.5 kHz', '3.15 kHz',
    '4.0 kHz', '5.0 kHz', '6.3 kHz', '8.0 kHz', '10.0 kHz',
    '12.5 kHz','16.0 kHz','20.0 kHz',
]
N_BANDS = len(FREQ_LABELS)

# GLOB-file scalar metric byte offsets (uint16 LE at each offset).
# Column names match the `runs` table schema exactly.
GLOB_SCALAR_OFFSETS = {
    'lafmax': 0x03c1, 'lasmax': 0x03c3, 'laimax': 0x03c5,
    'lafmin': 0x03c7, 'lasmin': 0x03c9, 'laimin': 0x03cb,
    'laeq':   0x03cd, 'laieq':  0x03cf, 'lae':    0x03d1, 'laie':   0x03d3,
    'lapeak': 0x03d5,
    'lcfmax': 0x03db, 'lcsmax': 0x03dd, 'lcimax': 0x03df,
    'lcfmin': 0x03e1, 'lcsmin': 0x03e3, 'lcimin': 0x03e5,
    'lceq':   0x03e7, 'lcieq':  0x03e9, 'lce':    0x03eb, 'lcie':   0x03ed,
    'lcpeak': 0x03ef,
    'la_l01': 0x0408, 'la_l1':  0x040a, 'la_l5':  0x040c,
    'la_l10': 0x040e, 'la_l50': 0x0410, 'la_l90': 0x0412,
    'la_l95': 0x0414, 'la_l99': 0x0416,
    'lc_l01': 0x0418, 'lc_l1':  0x041a, 'lc_l5':  0x041c,
    'lc_l10': 0x041e, 'lc_l50': 0x0420, 'lc_l90': 0x0422,
    'lc_l95': 0x0424, 'lc_l99': 0x0426,
}

# 18 Nortfr-labelled 1/3-octave spectral tables (36 bands each).
# Column names match the `runs` table schema exactly (note: 'lff_' for the
# percentile tables, not 'lf_' — this must stay consistent everywhere, since
# a naming drift here previously caused 8 of the 18 tables to go unsaved on
# direct SD-card upload while backfill_glob.py stored them under the right name).
SPECTRAL_TABLES = [
    ('spec_lfeq',    0x0428), ('spec_lffmax',  0x0470), ('spec_lffmin',  0x04b8),
    ('spec_lfe',     0x0500), ('spec_lfsmax',  0x0548), ('spec_lfsmin',  0x0590),
    ('spec_lfieq',   0x05d8), ('spec_lfimax',  0x0620), ('spec_lfimin',  0x0668),
    ('spec_lfie',    0x06b0),
    ('spec_lff_l01', 0x06f8), ('spec_lff_l1',  0x0764), ('spec_lff_l5',  0x07d0),
    ('spec_lff_l10', 0x083c), ('spec_lff_l50', 0x08a8), ('spec_lff_l90', 0x0914),
    ('spec_lff_l95', 0x0980), ('spec_lff_l99', 0x09ec),
]

# PROF file: 5 channels/second, 10 bytes/record, starting at byte offset 3.
# Field order: LAFspl, LAeq,1s, LAFmax,1s, LAE,1s, LApeak,1s.
PROF_RECORD_OFFSET = 3
PROF_RECORD_SIZE = 10
PROF_N_CHANNELS = 5


def bcd(b):
    return (b >> 4) * 10 + (b & 0xF)


def decode_raw(raw_uint16):
    """Decode a NOR140 uint16_le field to dB, with no rounding or clamping."""
    return raw_uint16 / DB_SCALE - DB_OFFSET


def round_half_up(value, digits=1):
    """Round using half-up (not banker's) rounding, matching NOR140 hardware convention."""
    scale = 10 ** digits
    return math.floor(value * scale + 0.5) / scale


def decode_value(raw_uint16, digits=2, clamp_min=None, clamp_max=None):
    """Decode a NOR140 uint16_le field to dB with half-up rounding and optional clamping."""
    v = decode_raw(raw_uint16)
    if clamp_min is not None:
        v = max(v, clamp_min)
    if clamp_max is not None:
        v = min(v, clamp_max)
    return round_half_up(v, digits)


def read_glob_scalars(data, digits=None):
    """Decode all GLOB scalar metrics. Returns dict of key -> float (missing keys are out of range).
    digits=None returns raw (unrounded) values; pass an int to round at read time."""
    out = {}
    for key, off in GLOB_SCALAR_OFFSETS.items():
        if off + 1 < len(data):
            raw = struct.unpack_from('<H', data, off)[0]
            out[key] = decode_raw(raw) if digits is None else decode_value(raw, digits=digits)
    return out


def read_glob_spectrum(data, offset, digits=None):
    """Decode one 36-band spectral table at `offset`. Returns list[36] or None if truncated."""
    end = offset + N_BANDS * 2
    if len(data) < end:
        return None
    return [
        decode_raw(struct.unpack_from('<H', data, offset + i * 2)[0]) if digits is None
        else decode_value(struct.unpack_from('<H', data, offset + i * 2)[0], digits=digits)
        for i in range(N_BANDS)
    ]


def read_glob_spectral_tables(data, digits=None):
    """Decode all 18 spectral tables. Returns dict of spec_col -> list[36] (or None if truncated)."""
    return {col: read_glob_spectrum(data, offset, digits=digits) for col, offset in SPECTRAL_TABLES}


def read_prof_records(data):
    """Return list of [5 x raw uint16] per second from a PROF binary, undecoded."""
    if len(data) < PROF_RECORD_OFFSET + 10:
        return []
    return [
        [struct.unpack_from('<H', data, off + i * 2)[0] for i in range(PROF_N_CHANNELS)]
        for off in range(PROF_RECORD_OFFSET, len(data) - 9, PROF_RECORD_SIZE)
    ]
