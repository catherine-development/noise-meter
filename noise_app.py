#!/usr/bin/env python3
"""
NOR140 Sound Level Meter — Web Viewer
Runs on Raspberry Pi; data uploaded via web interface or import_sdcard.py
"""
import csv
import io
import math
import os
import sys
import json
import functools
import urllib.request
import urllib.error
import threading
from datetime import timedelta, datetime

_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _v = _line.split('=', 1)
                if _k.strip() not in os.environ:
                    os.environ[_k.strip()] = _v.strip().strip("'\"")

from flask import (Flask, render_template, request, jsonify, redirect,
                   url_for, abort, flash, session as flask_session, make_response)

from noise_db import (init_db, import_sessions, get_all_sessions_json,
                      get_import_log, get_sessions_since, get_existing_dates, get_existing_run_starts,
                      get_all_sessions_list, update_session_metadata, delete_session,
                      save_weather, get_weather,
                      update_run_location_tag,
                      create_assessment, list_assessments, get_assessment, update_assessment,
                      delete_assessment, add_assessment_location, update_assessment_location,
                      delete_assessment_location, assign_runs, unassign_run, update_assessment_run,
                      get_assessment_detail, get_all_runs_for_assessment,
                      prepare_assessment_report_data, purge_sessions_before,
                      get_full_sync_payload, apply_full_sync, apply_sync_event,
                      get_assessment_location, get_assessment_run,
                      get_assessment_runs_by_pairs,
                      get_sessions_export_format,
                      get_setting, set_setting,
                      get_full_run_row, get_session_prof_lafspl)
from reports_db import (get_report_templates, get_report_template, save_report_template,
                        update_report_template, delete_report_template,
                        save_generated_report, get_generated_reports,
                        get_generated_report, delete_generated_report)
from noise_parser import parse_zip, parse_files

# Shared auth module from flight tracker
sys.path.insert(0, '/home/flightdata/flightdata')
try:
    from auth import (is_authorised_user, generate_otp, verify_otp,
                      send_otp_email, send_otp_sms, get_user_phone,
                      activate_user, login_required)
    AUTH_AVAILABLE = True
except ImportError:
    AUTH_AVAILABLE = False
    def login_required(f):
        return f

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', os.urandom(24))
app.permanent_session_lifetime = timedelta(days=7)

IMPORT_KEY = os.environ.get('IMPORT_API_KEY', '')
UPLOAD_PASS = os.environ.get('UPLOAD_PASSWORD', '')
PI_NAME    = os.environ.get('PI_NAME', 'Pi')
PEER_URL   = os.environ.get('PEER_URL', '')


def _fetch_weather_for_session(date, lat, lng):
    """Call Open-Meteo archive API and return summary dict. Raises on failure."""
    import math
    import urllib.parse
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


def _auto_fetch_weather(sessions):
    """Background thread: fetch weather for any new sessions that have coordinates."""
    all_data = get_all_sessions_json()['sessions']
    sess_map = {s['d']: s for s in all_data}
    for sess in sessions:
        date = sess['d']
        s = sess_map.get(date, sess)
        if s.get('lat') is None or s.get('lng') is None:
            continue
        if get_weather(date):
            continue  # already have it
        try:
            w = _fetch_weather_for_session(date, s['lat'], s['lng'])
            save_weather(date, w)
            print(f"Weather fetched for {date}")
        except Exception as e:
            print(f"Weather fetch failed for {date}: {e}")


def _push_to_peer(sessions):
    """Push sessions to peer Pi in a background thread (best-effort)."""
    if not PEER_URL or not sessions:
        return

    def _do_push():
        payload = json.dumps({'sessions': sessions}, separators=(',', ':')).encode()
        req = urllib.request.Request(
            PEER_URL.rstrip('/') + '/import',
            data=payload,
            headers={'Content-Type': 'application/json',
                     'X-Import-Key': IMPORT_KEY,
                     'User-Agent': _PEER_UA},
            method='POST',
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                print(f"Peer sync: {resp.read().decode()}")
        except Exception as e:
            print(f"Peer sync failed: {e}")

    threading.Thread(target=_do_push, daemon=True).start()


_PEER_UA = 'noise-meter/1.0'

def _sync_event_to_peer(entity, action, data):
    """Push a single mutation to the peer Pi. Fire-and-forget."""
    if not PEER_URL or not IMPORT_KEY:
        return
    def _do():
        try:
            payload = json.dumps({'entity': entity, 'action': action, 'data': data},
                                 default=str).encode()
            req = urllib.request.Request(
                PEER_URL.rstrip('/') + '/api/peer-sync',
                data=payload,
                headers={'Content-Type': 'application/json',
                         'X-Import-Key': IMPORT_KEY,
                         'User-Agent': _PEER_UA},
                method='POST',
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print(f'Peer sync ({entity}/{action}) failed: {e}', flush=True)
    threading.Thread(target=_do, daemon=True).start()


def _startup_sync_from_peer():
    """On startup, pull full state from peer and apply it (catches up on offline period)."""
    if not PEER_URL or not IMPORT_KEY:
        return
    def _do():
        import time
        time.sleep(8)  # let both Pis finish starting before attempting peer connection
        try:
            req = urllib.request.Request(
                PEER_URL.rstrip('/') + '/api/peer-sync-full',
                headers={'X-Import-Key': IMPORT_KEY, 'User-Agent': _PEER_UA},
                method='GET',
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            apply_full_sync(data)
            print('Startup peer sync: applied full payload from peer', flush=True)
        except Exception as e:
            print(f'Startup peer sync failed: {e}', flush=True)
    threading.Thread(target=_do, daemon=True).start()


def require_api_key(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        key = (request.headers.get('X-Import-Key') or
               request.form.get('api_key', '') or
               request.args.get('api_key', ''))
        if not IMPORT_KEY or key != IMPORT_KEY:
            abort(403)
        return f(*args, **kwargs)
    return decorated


def login_or_api_key(f):
    """Allow either a valid browser session or a valid X-Import-Key header —
    for endpoints normal logged-in page JS calls, that also need to be
    reachable non-interactively (e.g. import_sdcard.py checking what's
    already on a Pi before pushing new sessions)."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        key = (request.headers.get('X-Import-Key') or
               request.form.get('api_key', '') or
               request.args.get('api_key', ''))
        if IMPORT_KEY and key == IMPORT_KEY:
            return f(*args, **kwargs)
        return login_required(f)(*args, **kwargs)
    return decorated


def check_upload_auth():
    """Check upload password (from form or header). Returns True if authorised."""
    if not UPLOAD_PASS:
        return True  # no password set — open
    provided = (request.form.get('upload_password', '') or
                request.headers.get('X-Upload-Password', ''))
    return provided == UPLOAD_PASS


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    if flask_session.get('authenticated'):
        return redirect(url_for('index'))

    def rt(step, **kw):
        return render_template('login.html', pi_name=PI_NAME, step=step, **kw)

    if request.method == 'GET':
        return rt('email', error=None)

    step = request.form.get('step')
    email = request.form.get('email', '').lower().strip()

    if step == 'email':
        if not AUTH_AVAILABLE:
            return rt('email', error='Auth system unavailable — check server logs.')
        if not is_authorised_user(email):
            return rt('email', error='That email is not registered. Contact the administrator.')
        phone = get_user_phone(email)
        if phone:
            return rt('method', email=email, phone=phone, error=None)
        else:
            code = generate_otp(email)
            send_otp_email(email, code)
            return rt('otp', email=email, method='email', destination=email, error=None)

    elif step == 'send':
        method = request.form.get('method', 'email')
        code = generate_otp(email)
        if method == 'sms':
            phone = get_user_phone(email)
            if phone and send_otp_sms(phone, code):
                masked = phone[:4] + '****' + phone[-3:]
                return rt('otp', email=email, method='SMS', destination=masked, error=None)
            else:
                return rt('method', email=email, phone=get_user_phone(email),
                          error='Failed to send SMS. Try email instead.')
        else:
            send_otp_email(email, code)
            return rt('otp', email=email, method='email', destination=email, error=None)

    elif step == 'otp':
        code = request.form.get('code', '').strip()
        if verify_otp(email, code):
            activate_user(email)
            flask_session.permanent = True
            flask_session['authenticated'] = True
            flask_session['user_email'] = email
            return redirect(url_for('index'))
        return rt('otp', email=email, method='', destination='',
                  error='Invalid or expired code. Please try again.')

    return rt('email', error=None)


@app.route('/logout')
def logout():
    flask_session.clear()
    return redirect(url_for('login'))


# ── Main routes ───────────────────────────────────────────────────────────────

@app.route('/')
@login_required
def index():
    data = get_all_sessions_json()
    return render_template('index.html', data=data, pi_name=PI_NAME)


@app.route('/api/data.json')
@login_or_api_key
def api_data():
    return jsonify(get_all_sessions_json())


@app.route('/api/sync')
@require_api_key
def api_sync():
    since = request.args.get('since', '1970-01-01T00:00:00')
    sessions = get_sessions_since(since)
    return jsonify({'sessions': sessions, 'count': len(sessions),
                    'pi_name': PI_NAME, 'since': since})


@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'pi': PI_NAME})


# ── Upload page ────────────────────────────────────────────────────────────────

@app.route('/upload', methods=['GET'])
@login_required
def upload_page():
    log = get_import_log()
    needs_password = bool(UPLOAD_PASS)
    return render_template('upload.html', pi_name=PI_NAME, log=log,
                           needs_password=needs_password)


@app.route('/upload', methods=['POST'])
@login_required
def do_upload():
    if not check_upload_auth():
        flash('Incorrect password.', 'error')
        return redirect(url_for('upload_page'))

    # Folder upload: browser sends individual files with relative paths
    folder_files = request.files.getlist('files')
    single_file  = request.files.get('file')

    is_folder_upload = bool(folder_files)

    def _json_error(msg):
        return jsonify({'status': 'error', 'message': msg}), 400

    def _json_ok(msg):
        return jsonify({'status': 'ok', 'message': msg})

    def _json_info(msg):
        return jsonify({'status': 'info', 'message': msg})

    try:
        if is_folder_upload:
            pairs = [(f.filename, f.read()) for f in folder_files]
            parsed = parse_files(pairs)
        elif single_file and single_file.filename:
            raw   = single_file.read()
            fname = single_file.filename.lower()
            if fname.endswith('.zip'):
                parsed = parse_zip(raw)
            elif fname.endswith('.json'):
                data = json.loads(raw)
                parsed = data.get('sessions', [data] if 'd' in data else [])
            else:
                flash('Please upload a .zip of the SD card or a .json export.', 'error')
                return redirect(url_for('upload_page'))
        else:
            flash('No file selected.', 'error')
            return redirect(url_for('upload_page'))
    except Exception as e:
        if is_folder_upload:
            return _json_error(f'Could not read files: {e}')
        flash(f'Could not read file: {e}', 'error')
        return redirect(url_for('upload_page'))

    if not parsed:
        msg = 'No valid measurement sessions found.'
        if is_folder_upload:
            return _json_error(msg)
        flash(msg, 'error')
        return redirect(url_for('upload_page'))

    # Collect optional metadata
    def _float(key):
        try: return float(request.form.get(key, '').strip()) or None
        except (ValueError, TypeError): return None

    metadata = {
        'recorder_name':  request.form.get('recorder_name', '').strip() or None,
        'location_label': request.form.get('location_label', '').strip() or None,
        'postcode':       request.form.get('postcode', '').strip().upper() or None,
        'lat':            _float('lat'),
        'lng':            _float('lng'),
    }

    # Duplicate check at the run level (start_time per date) so new runs on an
    # existing date are not skipped.
    existing_starts = get_existing_run_starts()

    def _has_new_runs(s):
        known = existing_starts.get(s['d'], set())
        return any(p['start'] not in known for p in s.get('projects', []))

    new_sessions = [s for s in parsed if _has_new_runs(s)]
    skipped = [s['d'] for s in parsed if not _has_new_runs(s)]

    if not new_sessions:
        msg = f'All {len(skipped)} session(s) already in database: {", ".join(skipped)}.'
        if is_folder_upload:
            return _json_info(msg)
        flash(msg, 'info')
        return redirect(url_for('upload_page'))

    # Import sessions — ON CONFLICT upserts handle existing runs safely
    import_sessions(new_sessions, metadata=metadata)

    # Push to peer Pi and fetch weather in background
    _push_to_peer(new_sessions)
    threading.Thread(target=_auto_fetch_weather, args=(new_sessions,), daemon=True).start()

    msg = f'Added {len(new_sessions)} new session(s)'
    if skipped:
        msg += f' ({len(skipped)} already existed: {", ".join(skipped)})'
    msg += '.'
    if is_folder_upload:
        return _json_ok(msg)
    flash(msg, 'success')
    return redirect(url_for('index'))


# ── CSV export ────────────────────────────────────────────────────────────────

@app.route('/export/sessions.csv')
@login_required
def export_sessions_csv():
    from flask import Response
    data = get_all_sessions_json()
    sessions = data['sessions']

    # Apply the same filters as the client-side filter bar
    text     = request.args.get('q', '').strip().lower()
    date_from = request.args.get('from', '')
    date_to   = request.args.get('to', '')
    avg_min   = request.args.get('avg_min', '')
    max_min   = request.args.get('max_min', '')

    def _matches(s):
        if text:
            hay = ' '.join(filter(None, [s.get('name'), s.get('loc'), s.get('post'), s.get('notes')])).lower()
            if text not in hay:
                return False
        if date_from and s['d'] < date_from:
            return False
        if date_to and s['d'] > date_to:
            return False
        if avg_min:
            try:
                if s['avg'] < float(avg_min): return False
            except (ValueError, TypeError):
                pass
        if max_min:
            try:
                if s['mx'] < float(max_min): return False
            except (ValueError, TypeError):
                pass
        return True

    sessions = [s for s in sessions if _matches(s)]

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['date', 'location', 'recorder', 'postcode', 'lat', 'lng',
                'laeq_avg_db', 'laeq_max_db', 'lcpeak_max_db', 'run_count', 'notes'])
    for s in sessions:
        projects = s.get('projects', [])
        lcpeak_max = round(max((p['pmx'] for p in projects), default=0), 1) if projects else ''
        w.writerow([
            s['d'],
            s.get('loc') or '',
            s.get('name') or '',
            s.get('post') or '',
            s.get('lat') or '',
            s.get('lng') or '',
            s['avg'],
            s['mx'],
            lcpeak_max,
            len(projects),
            s.get('notes') or '',
        ])

    output = buf.getvalue()
    return Response(
        output,
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename="noise-sessions.csv"'}
    )


# ── Manage page ───────────────────────────────────────────────────────────────

@app.route('/manage')
@login_required
def manage_page():
    sessions = get_all_sessions_list()
    open_date = request.args.get('open')
    return render_template('manage.html', pi_name=PI_NAME,
                           sessions=sessions, open_date=open_date,
                           instrument_serial=get_setting('instrument_serial', ''))


@app.route('/settings/instrument', methods=['POST'])
@login_required
def save_instrument_settings():
    serial = request.form.get('instrument_serial', '').strip()
    set_setting('instrument_serial', serial)
    flash('Instrument settings saved.', 'success')
    return redirect(url_for('manage_page'))


@app.route('/session/<date>/edit', methods=['POST'])
@login_required
def edit_session(date):
    def _float(key):
        try: return float(request.form.get(key, '').strip()) or None
        except (ValueError, TypeError): return None

    meta = dict(
        recorder_name  = request.form.get('recorder_name', '').strip(),
        location_label = request.form.get('location_label', '').strip(),
        postcode       = request.form.get('postcode', '').strip().upper(),
        lat            = _float('lat'),
        lng            = _float('lng'),
        notes          = request.form.get('notes', '').strip(),
    )
    update_session_metadata(date, **meta)
    _sync_event_to_peer('session_meta', 'upsert', {'date': date, **meta})
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'status': 'ok', 'date': date})
    flash(f'Session {date} updated.', 'success')
    return redirect(url_for('manage_page', open=date))


@app.route('/session/<date>/run/<int:run_number>/tag', methods=['POST'])
@login_required
def edit_run_tag(date, run_number):
    tag = (request.json or {}).get('tag', '').strip().upper()
    update_run_location_tag(date, run_number, tag)
    _sync_event_to_peer('run_tag', 'upsert', {'session_date': date, 'run_number': run_number, 'location_tag': tag or None})
    return jsonify({'status': 'ok', 'tag': tag or None})


@app.route('/session/<date>/run/<int:run_number>/export/nor140/<report_type>')
@login_required
def export_nor140(date, run_number, report_type):
    if report_type not in ('GLOBAL', 'PROFILE'):
        return 'Invalid report type', 400
    from nor140_exporter import build_global_xlsx, build_profile_xlsx, export_filename
    run = get_full_run_row(date, run_number)
    if run is None:
        return 'Run not found', 404
    serial = get_setting('instrument_serial', '')
    if not run.get('spec_lfeq'):
        return ('Spectral data not available for this session. '
                'It was received via peer sync and has not been locally backfilled. '
                'Re-run backfill_glob.py and backfill_prof.py on this Pi to enable xlsx export.', 409)
    if report_type == 'GLOBAL':
        data = build_global_xlsx(run, serial)
    else:
        data = build_profile_xlsx(run, serial)
    fname = export_filename(run, serial, report_type)
    safe_fname = fname.replace('"', '').replace('\r', '').replace('\n', '')
    response = make_response(data)
    response.headers['Content-Type'] = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    response.headers['Content-Disposition'] = f'attachment; filename="{safe_fname}"'
    return response


@app.route('/session/<date>/delete', methods=['POST'])
@login_required
def delete_session_route(date):
    delete_session(date)
    flash(f'Session {date} deleted.', 'success')
    return redirect(url_for('manage_page'))


# ── Machine-to-machine import (import_sdcard.py / peer sync) ──────────────────

@app.route('/import', methods=['GET'])
def import_redirect():
    return redirect(url_for('upload_page'))


@app.route('/import', methods=['POST'])
@require_api_key
def do_import():
    if request.files.get('file'):
        raw = request.files['file'].read()
        if raw[:2] == b'PK':  # ZIP magic bytes
            sessions = parse_zip(raw)
        else:
            data = json.loads(raw)
            sessions = data.get('sessions', [data] if 'd' in data else [])
    else:
        data = request.get_json(force=True, silent=True) or {}
        sessions = data.get('sessions', [data] if 'd' in data else [])

    if not sessions:
        return jsonify({'error': 'no sessions found in payload'}), 400

    n = import_sessions(sessions)
    return jsonify({'imported': n, 'status': 'ok'})


@app.route('/api/peer-sync', methods=['POST'])
@require_api_key
def api_peer_sync():
    """Receive a single mutation event from the peer Pi."""
    data = request.get_json(silent=True) or {}
    entity = data.get('entity', '')
    action = data.get('action', '')
    payload = data.get('data', {})
    if entity and action:
        apply_sync_event(entity, action, payload)
    return jsonify({'ok': True})


@app.route('/api/peer-sync-full', methods=['GET'])
@require_api_key
def api_peer_sync_full():
    """Return full syncable state so peer can catch up after being offline."""
    return jsonify(get_full_sync_payload())


@app.route('/admin/purge-before', methods=['POST'])
@require_api_key
def admin_purge_before():
    """Temporary admin endpoint: delete all sessions before a given date."""
    before = request.args.get('before') or (request.get_json(silent=True) or {}).get('before', '')
    if not before or len(before) != 10:
        return jsonify({'error': 'provide before=YYYY-MM-DD'}), 400
    deleted = purge_sessions_before(before)
    return jsonify({'deleted': len(deleted), 'dates': deleted})


@app.route('/admin/push-to-peer', methods=['POST'])
@require_api_key
def admin_push_to_peer():
    """Push specific sessions (by date) or all sessions to peer Pi."""
    body = request.get_json(silent=True) or {}
    dates = body.get('dates') or request.args.getlist('date') or None
    sessions = get_sessions_export_format(dates=dates)
    if not sessions:
        return jsonify({'error': 'no matching sessions found'}), 404
    _push_to_peer(sessions)
    return jsonify({'pushed': len(sessions), 'dates': [s['d'] for s in sessions]})


@app.route('/session/<date>/fetch-weather', methods=['POST'])
@login_required
def fetch_weather_route(date):
    all_data = get_all_sessions_json()['sessions']
    sess = next((s for s in all_data if s['d'] == date), None)
    if not sess or sess.get('lat') is None or sess.get('lng') is None:
        return jsonify({'status': 'error', 'message': 'No GPS coordinates for this session — add them via Edit metadata first.'})
    try:
        w = _fetch_weather_for_session(date, sess['lat'], sess['lng'])
        save_weather(date, w)
        return jsonify({'status': 'ok', 'wx': {
            'ws': w['wind_speed'], 'wd': w['wind_dir'],
            'tn': w['temp_min'],   'tx': w['temp_max'], 'pr': w['precip'],
        }})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/api/existing-dates')
@login_required
def api_existing_dates():
    return jsonify(sorted(get_existing_dates()))


# ── Noise assessment report ────────────────────────────────────────────────────

def _percentile(sorted_vals, p):
    n = len(sorted_vals)
    if not n:
        return None
    idx = (p / 100) * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    return round(sorted_vals[lo] + (idx - lo) * (sorted_vals[hi] - sorted_vals[lo]), 1)


def _energy_avg_db(values):
    if not values:
        return None
    return round(10 * math.log10(sum(10 ** (v / 10) for v in values) / len(values)), 1)


def _expand_run(proj):
    """Expand the downsampled LAeq,1s profile back to approximate per-second values.
    Uses laeq_profile (PROF field 1 = LAeq,1s).  Use GLOB-derived scalar metrics for
    absolute accuracy; the profile is used for chart shape and pct85 only."""
    step = proj.get('step', 1)
    las = []
    for v in proj.get('laeq_profile', []):
        las.extend([v] * step)
    return las[:proj.get('n', len(las))]


def _run_stats(proj):
    """Compute statistical suite for one run.

    Prefers GLOB-derived scalar metrics (instrument-computed, accurate) over profile-
    derived estimates.  Falls back to profile stats only when GLOB values are absent
    (pre-update imports).  Note: profile values are LAS (slow-weighted SPL), so
    profile-derived LAeq/percentile estimates are ~8–11 dB high for impulsive sessions.
    """
    las_full = _expand_run(proj)
    stats = {'n': proj.get('n'), 'pmx': proj.get('pmx')}
    if las_full:
        s = sorted(las_full)
        n = len(s)
        stats.update({
            'leq':   _energy_avg_db(las_full),        # fallback only — overridden below
            'la10':  _percentile(s, 90),
            'la50':  _percentile(s, 50),
            'la90':  _percentile(s, 10),
            'lmax':  max(s),
            'lmin':  min(s),
            'pct85': round(100 * sum(1 for v in las_full if v >= 85) / n, 1),
        })
    # Override with GLOB-derived scalars where available
    if proj.get('avg')    is not None: stats['leq']  = round(proj['avg'], 1)
    if proj.get('la_l10') is not None: stats['la10'] = round(proj['la_l10'], 1)
    if proj.get('la_l50') is not None: stats['la50'] = round(proj['la_l50'], 1)
    if proj.get('la_l90') is not None: stats['la90'] = round(proj['la_l90'], 1)
    if proj.get('lafmax') is not None: stats['lmax'] = round(proj['lafmax'], 1)
    if proj.get('mn')     is not None: stats['lmin'] = round(proj['mn'], 1)
    return stats


# ── Reports ───────────────────────────────────────────────────────────────────

_MODEL_PRICING = {
    'claude-haiku-4-5-20251001': (0.80, 4.00),
    'claude-sonnet-5':           (3.00, 15.00),
    'claude-opus-5':             (15.00, 75.00),
}


def _build_session_data_block(sess, run_rows, all_laeq, total_duration_s):
    """Return the formatted session data text injected into report prompts."""
    loc_parts = [sess.get('loc'), sess.get('post')]
    location_str = ', '.join(p for p in loc_parts if p) or 'Not recorded'
    wx = sess.get('wx') or {}

    def wind_dir(deg):
        if deg is None:
            return ''
        dirs = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']
        return dirs[round(deg / 45) % 8]

    wx_parts = []
    if wx.get('ws') is not None:
        wx_parts.append(f"{wx['ws']} mph {wind_dir(wx.get('wd'))}")
    if wx.get('tn') is not None and wx.get('tx') is not None:
        wx_parts.append(f"temp {wx['tn']}–{wx['tx']} °C")
    if wx.get('pr'):
        wx_parts.append(f"precip {wx['pr']} mm")
    wx_str = ', '.join(wx_parts) or 'Not available'

    # Prefer GLOB-derived per-run scalars (already in run_rows via _run_stats).
    # Session LAeq = duration-weighted energy avg of per-run LAeq values.
    run_leq_ns = [(r['leq'], r.get('n') or 1) for r in run_rows if r.get('leq') is not None]
    if run_leq_ns:
        total_n = sum(n for _, n in run_leq_ns)
        session_leq = round(10 * math.log10(
            sum(n * 10 ** (leq / 10) for leq, n in run_leq_ns) / total_n
        ), 1)
    else:
        session_leq = _energy_avg_db(all_laeq) if all_laeq else None
    # LA10/LA90: true percentile of every run's pooled 1-second LAFspl samples
    # (Fast-weighted SPL — the statistical-descriptor convention, matching the
    # meter's own LAF percentiles), not an average of each run's own percentile
    # (percentiles don't compose linearly across sub-samples of different
    # sizes), and not LAeq,1s (a different, energy-averaged quantity). Falls
    # back to PROF-expanded percentiles only for older sessions without full
    # 1s data.
    pooled_lafspl = get_session_prof_lafspl(sess['d'], [r['run'] for r in run_rows])
    if pooled_lafspl:
        s = sorted(pooled_lafspl)
        session_la90 = _percentile(s, 10)
        session_la10 = _percentile(s, 90)
    else:
        session_la90 = _percentile(sorted(all_laeq), 10) if all_laeq else None
        session_la10 = _percentile(sorted(all_laeq), 90) if all_laeq else None
    run_lmaxs = [r['lmax'] for r in run_rows if r.get('lmax') is not None]
    session_lmax = max(run_lmaxs) if run_lmaxs else (max(all_laeq) if all_laeq else None)
    session_pmx  = max((r.get('pmx', 0) or 0 for r in run_rows), default=None)

    run_lines = [
        f"  Run {r['run']} ({r['start']}, {r['n']} s): "
        f"LAeq {r.get('leq')} dB | LA10 {r.get('la10')} | LA50 {r.get('la50')} | "
        f"LA90 {r.get('la90')} | LAmax {r.get('lmax')} | LCpeak {r.get('pmx')} | "
        f"Time≥85dB {r.get('pct85')}%"
        for r in run_rows
    ]

    gps_str = f"{sess['lat']}, {sess['lng']}" if sess.get('lat') and sess.get('lng') else 'Not recorded'
    scope = f"Run {run_rows[0]['run']} only" if len(run_rows) == 1 else f"Full session ({len(run_rows)} runs)"
    stat_label = f"RUN {run_rows[0]['run']} STATISTICS" if len(run_rows) == 1 else "SESSION STATISTICS"
    return (
        f"Instrument: Norsonic NOR140 Class 1 precision sound level meter (IEC 61672-1:2013).\n"
        f"Measurement date: {sess['d']}\n"
        f"Report scope: {scope}\n"
        f"Location: {location_str}\n"
        f"Recorder: {sess.get('name') or 'Not recorded'}\n"
        f"GPS: {gps_str}\n"
        f"Notes: {sess.get('notes') or 'None'}\n"
        f"Weather: {wx_str}\n\n"
        f"{stat_label}:\n"
        f"  Total duration: {total_duration_s} seconds\n"
        f"  LAeq: {session_leq} dB(A)\n"
        f"  LA10: {session_la10} dB(A)\n"
        f"  LA90: {session_la90} dB(A)\n"
        f"  LAmax: {session_lmax} dB(A)\n"
        f"  LCpeak max: {session_pmx} dB(C)\n\n"
        f"PER-RUN BREAKDOWN:\n" + '\n'.join(run_lines)
    ), {
        'session_leq': session_leq, 'session_la10': session_la10,
        'session_la90': session_la90, 'session_lmax': session_lmax,
        'session_pmx': session_pmx, 'wx': wx, 'wx_str': wx_str,
    }


_REPORT_TOOL = {
    'name': 'submit_report',
    'description': 'Submit the completed noise assessment report sections as HTML',
    'input_schema': {
        'type': 'object',
        'properties': {
            'executive_summary': {'type': 'string', 'description': 'HTML content'},
            'methodology':       {'type': 'string', 'description': 'HTML content'},
            'results_narrative': {'type': 'string', 'description': 'HTML content'},
            'compliance':        {'type': 'string', 'description': 'HTML content'},
            'conclusions':       {'type': 'string', 'description': 'HTML content'},
            'recommendations':   {'type': 'string', 'description': 'HTML content'},
        },
        'required': ['executive_summary', 'methodology', 'results_narrative',
                     'compliance', 'conclusions', 'recommendations'],
    },
}


def _call_claude(prompt, model, thinking_level):
    """Call Claude and return (sections_dict, input_tokens, output_tokens, cost_usd)."""
    ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
    if not ANTHROPIC_KEY:
        raise RuntimeError('ANTHROPIC_API_KEY not set')

    import anthropic as _anthropic
    client = _anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    # Strip any old JSON-output instruction from the template prompt — tool_use handles that now
    for phrase in (
        'Respond with valid JSON only. No markdown fencing. No preamble.',
        'No markdown fencing. No preamble.',
    ):
        prompt = prompt.replace(phrase, '').strip()

    kwargs = dict(
        model=model,
        max_tokens=16000,
        tools=[_REPORT_TOOL],
        tool_choice={'type': 'tool', 'name': 'submit_report'},
        messages=[{'role': 'user', 'content': prompt}],
    )
    if thinking_level == 'standard':
        kwargs['thinking']      = {'type': 'adaptive'}
        kwargs['output_config'] = {'effort': 'medium'}
    elif thinking_level == 'extended':
        kwargs['thinking']      = {'type': 'adaptive'}
        kwargs['output_config'] = {'effort': 'high'}

    message = client.messages.create(**kwargs)

    # tool_use block follows any thinking blocks; SDK already parsed the JSON for us
    tool_use = next(
        (b for b in message.content if getattr(b, 'type', '') == 'tool_use'),
        None,
    )
    if tool_use is None:
        raise RuntimeError('No tool_use block in Claude response')
    sections = tool_use.input  # dict — no JSON parsing needed, no escaping issues

    u = message.usage
    tok_in  = getattr(u, 'input_tokens', 0)
    tok_out = getattr(u, 'output_tokens', 0)
    price_in, price_out = _MODEL_PRICING.get(model, (3.0, 15.0))
    cost_usd = (tok_in * price_in + tok_out * price_out) / 1_000_000
    return sections, tok_in, tok_out, round(cost_usd, 4)


def _prepare_session_for_report(date, run_number=None):
    """Fetch session + compute stats. run_number=None means all runs.
    Returns (sess, run_rows, all_laeq, total_s) or None."""
    all_sessions = get_all_sessions_json()['sessions']
    sess = next((s for s in all_sessions if s['d'] == date), None)
    if not sess:
        return None
    all_projects = sess.get('projects', [])
    if run_number is not None:
        if run_number < 1 or run_number > len(all_projects):
            return None
        projects = [all_projects[run_number - 1]]
        base_num = run_number
    else:
        projects = all_projects
        base_num = None
    run_rows, all_laeq = [], []
    for i, proj in enumerate(projects, 1):
        rn = base_num if base_num is not None else i
        st = _run_stats(proj)
        run_rows.append({'run': rn, 'start': proj['start'], **st})
        all_laeq.extend(_expand_run(proj))
    total_s = sum(p.get('n', 0) for p in projects)
    return sess, run_rows, all_laeq, total_s


@app.route('/reports')
@login_required
def reports_page():
    all_sessions = get_all_sessions_json()['sessions']
    sessions_with_runs = [
        {
            'd': s['d'],
            'runs': [{'n': i + 1, 'start': p['start'], 'dur': p.get('n', 0)}
                     for i, p in enumerate(s.get('projects', []))],
        }
        for s in reversed(all_sessions)
    ]
    templates = get_report_templates()
    history = get_generated_reports()
    return render_template(
        'reports.html',
        pi_name=PI_NAME,
        sessions_with_runs=sessions_with_runs,
        templates=templates,
        history=history,
    )


@app.route('/api/report-templates', methods=['GET'])
@login_required
def api_list_templates():
    return jsonify(get_report_templates())


@app.route('/api/report-templates', methods=['POST'])
@login_required
def api_create_template():
    data = request.get_json(force=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name required'}), 400
    tid = save_report_template(
        name=name,
        description=(data.get('description') or '').strip(),
        prompt=(data.get('prompt') or '').strip(),
        is_default=bool(data.get('is_default')),
    )
    return jsonify({'id': tid})


@app.route('/api/report-templates/<int:tid>', methods=['PUT'])
@login_required
def api_update_template(tid):
    data = request.get_json(force=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name required'}), 400
    update_report_template(
        tid=tid,
        name=name,
        description=(data.get('description') or '').strip(),
        prompt=(data.get('prompt') or '').strip(),
        is_default=data.get('is_default'),
    )
    return jsonify({'ok': True})


@app.route('/api/report-templates/<int:tid>', methods=['DELETE'])
@login_required
def api_delete_template(tid):
    delete_report_template(tid)
    return jsonify({'ok': True})


@app.route('/api/generate-report', methods=['POST'])
@login_required
def api_generate_report():
    data = request.get_json(force=True) or {}
    date           = data.get('session_date', '').strip()
    template_id    = data.get('template_id')
    model          = data.get('model', 'claude-sonnet-5')
    thinking_level = data.get('thinking_level', 'none')
    run_number     = data.get('run_number')  # int or None = all runs
    if run_number is not None:
        run_number = int(run_number)

    if not date:
        return jsonify({'error': 'session_date required'}), 400

    result = _prepare_session_for_report(date, run_number)
    if not result:
        return jsonify({'error': f'Session {date} not found'}), 404
    sess, run_rows, all_laeq, total_s = result

    tmpl = get_report_template(template_id) if template_id else None
    if not tmpl:
        templates = get_report_templates()
        tmpl = next((t for t in templates if t['is_default']), templates[0] if templates else None)
    if not tmpl:
        return jsonify({'error': 'No report templates configured'}), 400

    session_data_block, _ = _build_session_data_block(sess, run_rows, all_laeq, total_s)
    prompt = tmpl['prompt'].replace('{{session_data}}', session_data_block)

    try:
        sections, tok_in, tok_out, cost_usd = _call_claude(prompt, model, thinking_level)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    run_label = f"Run {run_number}" if run_number else "All runs"
    rid = save_generated_report(
        session_date=date,
        template_id=tmpl['id'],
        template_name=tmpl['name'],
        model=model,
        thinking_level=thinking_level,
        sections_json=json.dumps(sections),
        input_tokens=tok_in,
        output_tokens=tok_out,
        cost_usd=cost_usd,
        run_number=run_number,
        run_label=run_label,
    )
    print(f"Report generated [{date}] template={tmpl['name']} model={model} "
          f"thinking={thinking_level}: {tok_in}/{tok_out} tokens ≈ ${cost_usd:.4f}")
    return jsonify({'report_id': rid, 'cost_usd': cost_usd,
                    'input_tokens': tok_in, 'output_tokens': tok_out})


@app.route('/reports/<int:rid>')
@login_required
def view_report(rid):
    stored = get_generated_report(rid)
    if not stored:
        abort(404)

    sections = json.loads(stored['sections_json'])
    date = stored['session_date']

    result = _prepare_session_for_report(date, stored.get('run_number'))
    if result:
        sess, run_rows, all_laeq, total_s = result
        _, stats = _build_session_data_block(sess, run_rows, all_laeq, total_s)
    else:
        sess, run_rows, stats = {'d': date}, [], {}
        total_s = 0

    usage_info = {
        'input_tokens':  stored.get('input_tokens'),
        'output_tokens': stored.get('output_tokens'),
        'cost_usd':      stored.get('cost_usd'),
        'model':         stored.get('model'),
        'thinking_level': stored.get('thinking_level'),
        'template_name': stored.get('template_name'),
    }

    return render_template(
        'report.html',
        date=date,
        pi_name=PI_NAME,
        sess=sess,
        run_rows=run_rows,
        session_leq=stats.get('session_leq'),
        session_la10=stats.get('session_la10'),
        session_la90=stats.get('session_la90'),
        session_lmax=stats.get('session_lmax'),
        session_pmx=stats.get('session_pmx'),
        total_duration_s=total_s,
        wx=stats.get('wx', {}),
        wx_str=stats.get('wx_str', ''),
        sections=sections,
        usage_info=usage_info,
        generated_at=stored.get('created_at', ''),
    )


@app.route('/api/generated-reports', methods=['GET'])
@login_required
def api_list_reports():
    return jsonify(get_generated_reports())


@app.route('/api/generated-reports/<int:rid>', methods=['DELETE'])
@login_required
def api_delete_report(rid):
    delete_generated_report(rid)
    return jsonify({'ok': True})


@app.route('/assessments')
@login_required
def assessments_page():
    return render_template('assessments.html', pi_name=PI_NAME)


@app.route('/api/assessments', methods=['GET'])
@login_required
def api_get_assessments():
    return jsonify(list_assessments())


@app.route('/api/assessments', methods=['POST'])
@login_required
def api_create_assessment():
    data = request.json or {}
    aid = create_assessment(
        name=data.get('name', 'Untitled'),
        purpose=data.get('purpose', ''),
        standard=data.get('standard', 'noise_act'),
        address=data.get('address', ''),
        postcode=data.get('postcode', ''),
        lat=data.get('lat'),
        lng=data.get('lng'),
        client_ref=data.get('client_ref', ''),
        notes=data.get('notes', ''),
    )
    _sync_event_to_peer('assessment', 'upsert', get_assessment(aid))
    return jsonify({'id': aid})


@app.route('/api/assessments/<int:aid>', methods=['GET'])
@login_required
def api_get_assessment(aid):
    detail = get_assessment_detail(aid)
    if not detail:
        abort(404)
    runs = get_all_runs_for_assessment(aid)
    return jsonify({'assessment': detail, 'runs': runs})


@app.route('/api/assessments/<int:aid>', methods=['PUT'])
@login_required
def api_update_assessment(aid):
    data = request.json or {}
    update_assessment(
        aid,
        name=data.get('name', ''),
        purpose=data.get('purpose', ''),
        standard=data.get('standard', 'noise_act'),
        address=data.get('address', ''),
        postcode=data.get('postcode', ''),
        lat=data.get('lat'),
        lng=data.get('lng'),
        client_ref=data.get('client_ref', ''),
        notes=data.get('notes', ''),
    )
    _sync_event_to_peer('assessment', 'upsert', get_assessment(aid))
    return jsonify({'status': 'ok'})


@app.route('/api/assessments/<int:aid>', methods=['DELETE'])
@login_required
def api_delete_assessment(aid):
    delete_assessment(aid)
    _sync_event_to_peer('assessment', 'delete', {'id': aid})
    return jsonify({'status': 'ok'})


@app.route('/api/assessments/<int:aid>/locations', methods=['POST'])
@login_required
def api_add_location(aid):
    data = request.json or {}
    loc_id = add_assessment_location(
        assessment_id=aid,
        label=data.get('label', ''),
        description=data.get('description', ''),
        lat=data.get('lat'),
        lng=data.get('lng'),
        notes=data.get('notes', ''),
    )
    _sync_event_to_peer('assessment_location', 'upsert', get_assessment_location(loc_id))
    return jsonify({'id': loc_id})


@app.route('/api/assessments/<int:aid>/locations/<int:loc_id>', methods=['PUT'])
@login_required
def api_update_location(aid, loc_id):
    data = request.json or {}
    update_assessment_location(
        loc_id,
        label=data.get('label', ''),
        description=data.get('description', ''),
        lat=data.get('lat'),
        lng=data.get('lng'),
        notes=data.get('notes', ''),
    )
    _sync_event_to_peer('assessment_location', 'upsert', get_assessment_location(loc_id))
    return jsonify({'status': 'ok'})


@app.route('/api/assessments/<int:aid>/locations/<int:loc_id>', methods=['DELETE'])
@login_required
def api_delete_location(aid, loc_id):
    delete_assessment_location(loc_id)
    _sync_event_to_peer('assessment_location', 'delete', {'id': loc_id})
    return jsonify({'status': 'ok'})


@app.route('/api/assessments/<int:aid>/assign', methods=['POST'])
@login_required
def api_assign_runs(aid):
    data = request.json or {}
    location_id = data.get('location_id')
    runs = data.get('runs', [])
    pairs = [(r['date'], r['run_number']) for r in runs]
    assign_runs(aid, location_id, pairs)
    for row in get_assessment_runs_by_pairs(aid, pairs):
        _sync_event_to_peer('assessment_run', 'upsert', row)
    return jsonify({'status': 'ok', 'assigned': len(pairs)})


@app.route('/api/assessment-runs/<int:ar_id>', methods=['DELETE'])
@login_required
def api_unassign_run(ar_id):
    unassign_run(ar_id)
    _sync_event_to_peer('assessment_run', 'delete', {'id': ar_id})
    return jsonify({'status': 'ok'})


@app.route('/api/assessment-runs/<int:ar_id>', methods=['PUT'])
@login_required
def api_update_ar(ar_id):
    data = request.json or {}
    update_assessment_run(ar_id, data.get('conditions', ''), data.get('notes', ''))
    _sync_event_to_peer('assessment_run', 'upsert', get_assessment_run(ar_id))
    return jsonify({'status': 'ok'})


@app.route('/export/assessment/<int:aid>.csv')
@login_required
def export_assessment_csv(aid):
    data = prepare_assessment_report_data(aid)
    if not data:
        abort(404)
    output = io.StringIO()
    w = csv.writer(output)
    w.writerow(['sub_location', 'description', 'date', 'start_time', 'duration_s',
                'time_period', 'avg_laeq_db', 'min_laeq_db', 'max_laeq_db',
                'la10_db', 'la90_db', 'max_lcpeak_db', 'max_laimax_db',
                'conditions', 'notes'])
    for loc in data['locations']:
        for r in loc['runs']:
            w.writerow([
                loc['label'], loc['description'] or '', r['date'], r['start_time'],
                r['duration_s'], r['time_period'],
                r.get('avg_laeq', ''), r.get('min_laeq', ''), r.get('max_laeq', ''),
                r.get('la10', ''), r.get('la90', ''),
                r.get('max_lcpeak', ''), r.get('max_laimax', ''),
                r.get('conditions', '') or '', r.get('notes', '') or '',
            ])
    aname = data['assessment']['name'].replace(' ', '_').replace('"', '').replace('\r', '').replace('\n', '')[:40]
    return app.response_class(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="assessment_{aname}.csv"'},
    )


if __name__ == '__main__':
    init_db()
    _startup_sync_from_peer()
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
