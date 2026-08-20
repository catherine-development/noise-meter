"""
Weather for measurement sessions: the Open-Meteo fetch and the gap-fill.

Split out of noise_app.py (WP9) so that sync_peer.py — the 15-minute pull
timer, a standalone script — can fill weather gaps after importing sessions
from the peer without importing the whole Flask app (which would start the
auth machinery and the startup sync thread just to make one HTTP GET).

Weather is keyed like the session it describes, (date, instrument_serial),
and replicates between the Pis inside the session sync payload. Each Pi may
also fetch for itself: both hit the same archive API with the same
coordinates, and save_weather() upserts, so a double fetch is idempotent.

noise_db is imported lazily inside each function rather than at module scope:
the test suite rebinds noise_db to a fresh database per Side, and a module-
level `from noise_db import …` would freeze the first binding.
"""
import json
import logging
import math
import urllib.parse
import urllib.request

log = logging.getLogger('noise.weather')


def fetch_weather_summary(date, lat, lng):
    """Call the Open-Meteo archive API and return the weather-row dict
    (summary scalars plus the raw hourly series as JSON). Raises on failure."""
    params = urllib.parse.urlencode({
        'latitude': lat, 'longitude': lng,
        'start_date': date, 'end_date': date,
        'hourly': 'temperature_2m,precipitation,wind_speed_10m,wind_direction_10m',
        'wind_speed_unit': 'mph',
        'timezone': 'Europe/London',
    })
    url = f'https://archive-api.open-meteo.com/v1/archive?{params}'
    req = urllib.request.Request(url, headers={'User-Agent': 'NOR140-noise-meter/1.0'})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    h = data.get('hourly', {})
    temps  = [v for v in h.get('temperature_2m', [])       if v is not None]
    winds  = [v for v in h.get('wind_speed_10m', [])       if v is not None]
    dirs   = [v for v in h.get('wind_direction_10m', [])   if v is not None]
    precip = [v for v in h.get('precipitation', [])        if v is not None]
    # Circular mean for wind direction
    wd = None
    if dirs:
        s = sum(math.sin(math.radians(d)) for d in dirs)
        c = sum(math.cos(math.radians(d)) for d in dirs)
        wd = round((math.degrees(math.atan2(s, c)) + 360) % 360, 1)
    return {
        'wind_speed': round(sum(winds) / len(winds), 1) if winds else None,
        'wind_dir':   wd,
        'temp_min':   round(min(temps), 1) if temps else None,
        'temp_max':   round(max(temps), 1) if temps else None,
        'precip':     round(sum(precip), 1) if precip else None,
        'hourly_json': json.dumps(h),
    }


def fill_weather_gaps(sessions, fetch=None):
    """Fetch weather for each payload session that has coordinates stored but
    no weather row yet. Best-effort: a failed fetch is logged and skipped.

    `sessions` is a list of payload dicts ({'d': date, 'serial': …, …} — the
    shape import_sessions() takes), naming which sessions to consider; the
    coordinates come from the stored session row, which is the source of
    truth by the time this runs (the upload form's metadata has been applied,
    and a synced session carries the sender's coordinates). Called after a
    local upload (noise_app), and after the 15-minute sync pull imports the
    peer's sessions (sync_peer.py) — so a Pi that received a session by sync
    *without* weather (the sender had no coordinates yet, or its own fetch
    had not run) fills the gap itself on the next pull.

    `fetch` defaults to fetch_weather_summary; the suite injects a stub.
    Returns the list of (date, serial) pairs actually filled.
    """
    import noise_db  # late import — see the module docstring
    if fetch is None:
        fetch = fetch_weather_summary
    filled = []
    for sess in sessions or []:
        date = sess.get('d')
        if not date:
            continue
        serial = noise_db.resolve_serial(sess.get('serial'))
        conn = noise_db.get_db()
        try:
            row = conn.execute(
                'SELECT lat, lng FROM sessions WHERE date=? AND instrument_serial=?',
                (date, serial)).fetchone()
        finally:
            conn.close()
        if row is None or row['lat'] is None or row['lng'] is None:
            continue
        if noise_db.get_weather(date, serial):
            continue  # already have it (fetched here, or replicated from the peer)
        try:
            w = fetch(date, row['lat'], row['lng'])
            noise_db.save_weather(date, w, serial)
            filled.append((date, serial))
            log.info('Weather fetched for %s (serial %s)', date, serial or '<default>')
        except Exception as e:
            log.warning('Weather fetch failed for %s (serial %s): %s',
                        date, serial or '<default>', e)
    return filled
