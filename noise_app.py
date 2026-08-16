#!/usr/bin/env python3
"""
NOR140 Sound Level Meter — Web Viewer
Runs on Raspberry Pi; data uploaded via web interface or import_sdcard.py
"""
import csv
import io
import os
import json
import urllib.request
import threading
from datetime import timedelta

# Imported before noise_db on purpose: importing config is what loads .env, and
# noise_db reads NOISE_DB_PATH at module scope.
from config import PI_NAME, IMPORT_KEY, UPLOAD_PASS

from flask import (Flask, render_template, request, jsonify, redirect,
                   url_for, flash, session as flask_session, make_response)

from noise_db import (init_db, import_sessions, get_all_sessions_json,
                      get_sessions_since, get_existing_dates, get_existing_run_starts,
                      get_all_sessions_list, update_session_metadata, delete_session,
                      save_weather, get_weather,
                      update_run_location_tag, purge_sessions_before,
                      get_sessions_export_format,
                      get_setting, set_setting, get_full_run_row)
from sync_db import (get_import_log, get_full_sync_payload,
                     apply_full_sync, apply_sync_event)
from noise_parser import parse_zip, parse_files
from webauth import (AUTH_AVAILABLE, login_required, require_api_key,
                     login_or_api_key, check_upload_auth)
from peer_client import push_to_peer, sync_event_to_peer, startup_sync_from_peer
import reports
import assessments

if AUTH_AVAILABLE:
    # webauth put the flight tracker's directory on sys.path when it imported
    # login_required; these are only needed by the /login route below.
    from auth import (is_authorised_user, generate_otp, verify_otp,
                      send_otp_email, send_otp_sms, get_user_phone,
                      activate_user)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', os.urandom(24))
app.permanent_session_lifetime = timedelta(days=7)

app.register_blueprint(reports.bp)
app.register_blueprint(assessments.bp)


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
    push_to_peer(new_sessions)
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
    sync_event_to_peer('session_meta', 'upsert', {'date': date, **meta})
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'status': 'ok', 'date': date})
    flash(f'Session {date} updated.', 'success')
    return redirect(url_for('manage_page', open=date))


@app.route('/session/<date>/run/<int:run_number>/tag', methods=['POST'])
@login_required
def edit_run_tag(date, run_number):
    tag = (request.json or {}).get('tag', '').strip().upper()
    update_run_location_tag(date, run_number, tag)
    sync_event_to_peer('run_tag', 'upsert', {'session_date': date, 'run_number': run_number, 'location_tag': tag or None})
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
    push_to_peer(sessions)
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


if __name__ == '__main__':
    init_db()
    startup_sync_from_peer()
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
