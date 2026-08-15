"""
Single source of truth for NOR140-B binary format constants and decode logic.

Both GLOB scalars/spectra and PROF channel values decode as:
    dB = uint16_le / 128 - 20

This module is imported by backfill_glob.py, backfill_prof.py, and
nor140_exporter.py so that any correction to the decode constants propagates
everywhere automatically.
"""
import math

DB_SCALE = 128.0
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


def decode_value(raw_uint16, digits=2, clamp_min=None, clamp_max=None):
    """Decode a NOR140 uint16_le field to dB with half-up rounding.

    Half-up (rather than Python's default banker's rounding) matches the
    NOR140 hardware rounding convention, eliminating the known 0.1 dB
    discrepancy at x.x5 boundaries.
    """
    v = raw_uint16 / DB_SCALE - DB_OFFSET
    if clamp_min is not None:
        v = max(v, clamp_min)
    if clamp_max is not None:
        v = min(v, clamp_max)
    scale = 10 ** digits
    return math.floor(v * scale + 0.5) / scale
