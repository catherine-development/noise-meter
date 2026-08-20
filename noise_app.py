#!/usr/bin/env python3
"""
NOR140 Sound Level Meter — Web Viewer
Runs on Raspberry Pi; data uploaded via web interface or import_sdcard.py
"""
import csv
import io
import logging
import os
import json
import sqlite3
import threading
from datetime import timedelta

# Configured once, as early as possible: every log call below this line — our
# own, and the import-time warnings from webauth and peer_client — goes
# through it. Under gunicorn this module is only ever imported, never run as
# __main__, so this can't live behind an `if __name__ == '__main__':` guard.
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(name)s: %(message)s')

# Imported before noise_db on purpose: importing config is what loads .env, and
# noise_db reads NOISE_DB_PATH at module scope.
from config import PI_NAME, IMPORT_KEY, UPLOAD_PASS

from flask import (Flask, render_template, request, jsonify, redirect,
                   url_for, flash, session as flask_session, make_response)

from noise_db import (init_db, import_sessions, get_all_sessions_json,
                      get_sessions_since, get_existing_dates, get_existing_run_starts,
                      get_all_sessions_list, update_session_metadata, delete_session,
                      save_weather,
                      update_run_location_tag, purge_sessions_before,
                      get_sessions_export_format, get_run_prof_by_source,
                      get_setting, set_setting, get_full_run_row,
                      default_serial, resolve_serial)
from sync_db import (get_import_log, get_full_sync_payload,
                     apply_full_sync, apply_sync_event, get_last_push_error)
from noise_parser import parse_zip, parse_files
from webauth import (AUTH_AVAILABLE, login_required, require_api_key,
                     login_or_api_key, check_upload_auth,
                     check_startup_security, csrf_protect, csrf_token)
from peer_client import push_to_peer, sync_event_to_peer, startup_sync_from_peer
from weather import fetch_weather_summary, fill_weather_gaps
import reports
import assessments
import helpdocs

log = logging.getLogger('noise.app')

if AUTH_AVAILABLE:
    # webauth put the flight tracker's directory on sys.path when it imported
    # login_required; these are only needed by the /login route below.
    from auth import (is_authorised_user, generate_otp, verify_otp,
                      send_otp_email, send_otp_sms, get_user_phone,
                      activate_user)

# Raises InsecureConfiguration if the auth module is missing and the operator
# has not set ALLOW_UNAUTHENTICATED=1. Deliberately before the app object is
# built: nothing should be able to serve a request from this module afterwards.
check_startup_security()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', os.urandom(24))
app.permanent_session_lifetime = timedelta(days=7)


def _env_flag(name, default=True):
    raw = os.environ.get(name)
    if raw is None or raw.strip() == '':
        return default
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


# SESSION_COOKIE_SECURE defaults on: both Pis are served over HTTPS through
# Cloudflare. It is an env flag rather than a constant because deploy_to_pis.sh
# documents plain-http LAN access on http://192.168.x.x:5001, where a Secure
# cookie is never sent and nobody can log in — such an instance must set
# SESSION_COOKIE_SECURE=0 and accept that the cookie crosses the LAN in clear.
app.config.update(
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=_env_flag('SESSION_COOKIE_SECURE', True),
)

# Uploads and /import bodies are read fully into memory and any ZIP among them
# is extracted in memory too (noise_parser.parse_zip / parse_files) — an
# unbounded request body is a memory-exhaustion vector on a Pi with 4-8 GB of
# RAM running other services besides this one. 64 MB comfortably covers a full
# SD-card ZIP with headroom; override in .env if a card ever needs more.
app.config['MAX_CONTENT_LENGTH'] = (
    int(os.environ.get('MAX_CONTENT_LENGTH_MB', '64')) * 1024 * 1024)


@app.errorhandler(413)
def _request_too_large(_e):
    """A request over MAX_CONTENT_LENGTH — readable rather than Werkzeug's
    bare "413 Request Entity Too Large" text page, and JSON for the machine
    clients (import_sdcard.py, the peer Pi, the folder-upload fetch() call)
    that would otherwise have to parse HTML to find out what happened. Renders
    the upload page inline instead of redirecting so the 413 status survives —
    a redirect response is always a 3xx, which would hide the failure from a
    client checking the status code."""
    limit_mb = app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024)
    msg = (f'Upload too large — the limit is {limit_mb} MB. Split the upload '
           f'into fewer sessions per pass, or raise MAX_CONTENT_LENGTH_MB in .env.')
    wants_json = (request.path.startswith('/import') or
                 request.path.startswith('/api/') or
                 request.headers.get('X-Requested-With') == 'XMLHttpRequest')
    if wants_json:
        return jsonify({'status': 'error', 'error': msg, 'message': msg}), 413
    flash(msg, 'error')
    return render_template('upload.html', pi_name=PI_NAME, log=get_import_log(),
                           needs_password=bool(UPLOAD_PASS),
                           last_push_error=get_last_push_error(),
                           upload_serial=_upload_serial_default()), 413


# init_db() (all migrations) and startup_sync_from_peer() used to run only
# under `if __name__ == '__main__':`, below. That is never reached under
# gunicorn, which imports this module and calls the WSGI `app` object
# directly — a fresh Pi would come up serving requests against a database with
# no tables at all. Running them here means every path that imports this
# module (gunicorn, `python3 noise_app.py`, the test suite's importlib
# import) gets a migrated database before the first request. Both are safe to
# repeat: init_db() is CREATE-TABLE-IF-NOT-EXISTS plus additive, guarded
# ALTERs, and startup_sync_from_peer() is a no-op whenever PEER_URL/
# IMPORT_API_KEY are unset — true for sync_peer.py and import_sdcard.py, which
# import noise_db directly and never import this module, and for the test
# suite, which never sets PEER_URL.
init_db()
startup_sync_from_peer()

# One hook for the whole app, blueprints included: every non-GET request that
# is not API-key authenticated must carry the session's CSRF token.
app.before_request(csrf_protect)
app.context_processor(lambda: {'csrf_token': csrf_token})

# Rate limiting. Optional at import: the Pis install from requirements.txt so
# it is there, but a bare venv (the test one, say) should degrade rather than
# fail to start. In-memory storage is correct here — one gunicorn worker.
try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    limiter = Limiter(get_remote_address, app=app, storage_uri='memory://',
                      default_limits=[])
except ImportError:
    limiter = None
    log.warning('Flask-Limiter is not installed — /login and /upload are not '
                'rate limited. pip install -r requirements.txt to enable it.')


def rate_limit(*args, **kwargs):
    """limiter.limit(), or a no-op when the package is absent."""
    if limiter is None:
        return lambda f: f
    return limiter.limit(*args, **kwargs)


app.register_blueprint(reports.bp)
app.register_blueprint(assessments.bp)
app.register_blueprint(helpdocs.bp)


# The Open-Meteo fetch and the gap-fill live in weather.py (WP9): weather is
# keyed per session, (date, instrument_serial), and sync_peer.py runs the same
# gap-fill after each 15-minute pull, so it must be importable without this
# whole app module. _auto_fetch_weather is the upload path's thread target.
_auto_fetch_weather = fill_weather_gaps


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
@rate_limit('10 per minute', methods=['POST'])
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

def _req_serial():
    """The instrument a session route is about: `serial` from the query
    string, the form body or a JSON body, else the default. Existing links and
    clients that name only the date therefore keep working."""
    raw = request.args.get('serial')
    if raw is None:
        raw = request.form.get('serial')
    if raw is None and request.is_json:
        raw = (request.get_json(silent=True) or {}).get('serial')
    return resolve_serial(raw)


def _redirect_serial(serial):
    """The serial to put in a redirect: blank when it is the default, so URLs
    stay date-shaped for the one-meter case."""
    return serial if serial != default_serial() else None


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


def _read_version():
    """The short commit hash deploy_to_pis.sh writes to VERSION after copying
    files, so /health can say what's actually running on a Pi. Read once at
    import time — a real change here always comes with a service restart
    (deploy_to_pis.sh's last step), so there is never a stale in-process value
    to worry about. 'dev' when there is no VERSION file, e.g. this worktree,
    or a checkout run directly with `python3 noise_app.py`."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'VERSION')) as f:
            return f.read().strip() or 'dev'
    except OSError:
        return 'dev'


APP_VERSION = _read_version()


@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'pi': PI_NAME, 'version': APP_VERSION})


# ── Upload page ────────────────────────────────────────────────────────────────

@app.route('/upload', methods=['GET'])
@login_required
def upload_page():
    log = get_import_log()
    needs_password = bool(UPLOAD_PASS)
    return render_template('upload.html', pi_name=PI_NAME, log=log,
                           needs_password=needs_password,
                           last_push_error=get_last_push_error(),
                           upload_serial=_upload_serial_default())


def _upload_serial_default():
    """What the upload form's serial field starts with: the serial last used
    on this Pi, else the instrument_serial setting."""
    return get_setting('last_upload_serial') or get_setting('instrument_serial', '') or ''


@app.route('/upload', methods=['POST'])
@rate_limit('30 per hour', methods=['POST'])
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

    # The instrument this card came from. One upload is one meter; the field
    # defaults to the last serial used here, and blank means the default
    # serial. Resolved before parsing so the payload pushed to the peer names
    # the serial this Pi actually stored it under.
    serial = resolve_serial(request.form.get('serial'))
    try:
        if is_folder_upload:
            pairs = [(f.filename, f.read()) for f in folder_files]
            parsed = parse_files(pairs, serial=serial)
        elif single_file and single_file.filename:
            raw   = single_file.read()
            fname = single_file.filename.lower()
            if fname.endswith('.zip'):
                parsed = parse_zip(raw, serial=serial)
            elif fname.endswith('.json'):
                data = json.loads(raw)
                parsed = data.get('sessions', [data] if 'd' in data else [])
                # A JSON export carries its own serial per session; one that
                # predates the field is filed under the form's serial.
                for sess in parsed:
                    if not (sess.get('serial') or '').strip():
                        sess['serial'] = serial
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

    # Pairs the parser found but could not turn into a run. Reported rather than
    # swallowed: "Added 1 session" with a PROJ folder silently missing is how a
    # corrupt file goes unnoticed until someone counts runs against the card.
    # (A .json upload is a plain list and has no report.)
    skipped_files = list(getattr(parsed, 'skipped', ()) or ())
    skip_note = ''
    if skipped_files:
        lines = [f"{s['path']}: {s['reason']}" for s in skipped_files[:8]]
        if len(skipped_files) > 8:
            lines.append(f'and {len(skipped_files) - 8} more')
        skip_note = (f' {len(skipped_files)} file pair(s) could not be read and were '
                     f'skipped: ' + '; '.join(lines))

    if not parsed:
        msg = 'No valid measurement sessions found.' + skip_note
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
        known = existing_starts.get((s['d'], resolve_serial(s.get('serial'))), set())
        return any(p['start'] not in known for p in s.get('projects', []))

    new_sessions = [s for s in parsed if _has_new_runs(s)]
    skipped = [s['d'] for s in parsed if not _has_new_runs(s)]

    if not new_sessions:
        msg = f'All {len(skipped)} session(s) already in database: {", ".join(skipped)}.'
        msg += skip_note
        if is_folder_upload:
            return _json_info(msg)
        flash(msg, 'info')
        return redirect(url_for('upload_page'))

    # Import sessions. Runs are keyed on their PROJ folder, so a partial upload
    # refreshes or adds runs and never overwrites a different one.
    import_sessions(new_sessions, metadata=metadata)
    if request.form.get('serial') is not None:
        set_setting('last_upload_serial', serial)

    # Push to peer Pi and fetch weather in background
    push_to_peer(new_sessions)
    threading.Thread(target=_auto_fetch_weather, args=(new_sessions,), daemon=True).start()

    msg = f'Added {len(new_sessions)} new session(s)'
    if skipped:
        msg += f' ({len(skipped)} already existed: {", ".join(skipped)})'
    msg += '.' + skip_note
    if skipped_files:
        # Stay on the upload page so the skip note is actually read: the
        # success path redirects to the index, which shows no flashes, and the
        # folder path's 'ok' status navigates away after a second.
        if is_folder_upload:
            return _json_info(msg)
        flash(msg, 'info')
        return redirect(url_for('upload_page'))
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
    w.writerow(['date', 'serial', 'location', 'recorder', 'postcode', 'lat', 'lng',
                'laeq_avg_db', 'lafmax_max_db', 'lcpeak_max_db', 'run_count', 'notes'])
    for s in sessions:
        projects = s.get('projects', [])
        lcpeak_max = round(max((p['pmx'] for p in projects), default=0), 1) if projects else ''
        w.writerow([
            s['d'],
            s.get('serial') or '',
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
    # The serial is shown per row only when the database holds more than one.
    serials = {s['instrument_serial'] for s in sessions}
    return render_template('manage.html', pi_name=PI_NAME,
                           sessions=sessions, open_date=open_date,
                           multi_serial=len(serials) > 1,
                           default_serial=default_serial(),
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
    serial = _req_serial()
    update_session_metadata(date, serial=serial, **meta)
    sync_event_to_peer('session_meta', 'upsert', {'date': date, 'serial': serial, **meta})
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'status': 'ok', 'date': date, 'serial': serial})
    flash(f'Session {date} updated.', 'success')
    return redirect(url_for('manage_page', open=_manage_key(date, serial)))


def _manage_key(date, serial):
    """The row handle manage.html uses — see get_all_sessions_list()."""
    return date + (('-' + serial) if serial else '')


@app.route('/session/<date>/run/<int:run_number>/tag', methods=['POST'])
@login_required
def edit_run_tag(date, run_number):
    body = request.json or {}
    tag = body.get('tag', '').strip().upper()
    serial = _req_serial()
    # source_file, when the page sends it, is the run's stable identity; the
    # number it was rendered with may be stale by the time this posts, and the
    # peer's numbering can differ while it is missing a run for the date.
    source_file = (body.get('source_file') or '').strip() or None
    update_run_location_tag(date, run_number, tag, serial=serial, source_file=source_file)
    sync_event_to_peer('run_tag', 'upsert', {
        'session_date': date, 'serial': serial, 'run_number': run_number,
        'source_file': source_file, 'location_tag': tag or None})
    return jsonify({'status': 'ok', 'tag': tag or None})


@app.route('/session/<date>/run/<int:run_number>/export/nor140/<report_type>')
@login_required
def export_nor140(date, run_number, report_type):
    if report_type not in ('GLOBAL', 'PROFILE'):
        return 'Invalid report type', 400
    from nor140_exporter import build_global_xlsx, build_profile_xlsx, export_filename
    run = get_full_run_row(date, run_number, serial=_req_serial())
    if run is None:
        return 'Run not found', 404
    # The session's own serial names the file; the setting only as a fallback
    # for a session filed under a blank one.
    serial = run.get('instrument_serial') or get_setting('instrument_serial', '')
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
    serial = _req_serial()
    delete_session(date, serial)
    sync_event_to_peer('session', 'delete', {'date': date, 'serial': serial})
    flash(f'Session {date} deleted.', 'success')
    return redirect(url_for('manage_page'))


# ── Machine-to-machine import (import_sdcard.py / peer sync) ──────────────────

@app.route('/import', methods=['GET'])
def import_redirect():
    return redirect(url_for('upload_page'))


@app.route('/import', methods=['POST'])
@require_api_key
def do_import():
    skipped = []
    if request.files.get('file'):
        raw = request.files['file'].read()
        if raw[:2] == b'PK':  # ZIP magic bytes
            # A ZIP names no instrument itself; ?serial= / form `serial` does,
            # else the default. JSON payloads carry `serial` per session.
            sessions = parse_zip(raw, serial=request.args.get('serial')
                                 or request.form.get('serial'))
            skipped = list(sessions.skipped)
        else:
            data = json.loads(raw)
            sessions = data.get('sessions', [data] if 'd' in data else [])
    else:
        data = request.get_json(force=True, silent=True) or {}
        sessions = data.get('sessions', [data] if 'd' in data else [])

    if not sessions:
        return jsonify({'error': 'no sessions found in payload',
                        'skipped': skipped}), 400

    try:
        n = import_sessions(sessions)
    except sqlite3.IntegrityError as e:
        # Nothing was written (import_sessions commits once, at the end), but
        # say what the database refused rather than 500 with a bare traceback —
        # the peer's push_to_peer() records this text as last_push_error.
        app.logger.warning('import rejected: %s', e)
        return jsonify({'error': f'import rejected: {e}', 'status': 'error'}), 409
    return jsonify({'imported': n, 'status': 'ok', 'skipped': skipped})


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
    if deleted:
        sync_event_to_peer('session', 'purge_before', {'before': before})
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
    # Per session: the (date, serial) session's own coordinates, stored under
    # the same (date, serial) key. The weather row replicates to the peer
    # inside the session sync payload.
    serial = _req_serial()
    all_data = get_all_sessions_json()['sessions']
    sess = next((s for s in all_data if s['d'] == date and s.get('serial') == serial), None)
    if not sess or sess.get('lat') is None or sess.get('lng') is None:
        return jsonify({'status': 'error', 'message': 'No GPS coordinates for this session — add them via Edit metadata first.'})
    try:
        w = fetch_weather_summary(date, sess['lat'], sess['lng'])
        save_weather(date, w, serial)
        return jsonify({'status': 'ok', 'wx': {
            'ws': w['wind_speed'], 'wd': w['wind_dir'],
            'tn': w['temp_min'],   'tx': w['temp_max'], 'pr': w['precip'],
        }})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/api/existing-dates')
@login_required
def api_existing_dates():
    """Dates already stored — for the instrument named by ?serial= (blank
    meaning the default), so a second meter's card for a date the first
    already covered is not filtered out of a folder upload; every
    instrument's dates when the parameter is absent."""
    serial = request.args.get('serial')
    return jsonify(sorted(get_existing_dates(serial)))


@app.route('/api/run/<date>/<source_file>/prof')
@login_required
def api_run_prof(date, source_file):
    """The stored 1-second PROF series for one run, for the run modal.

    The session browser payload carries only the downsampled chart profile,
    whose every point is the maximum of its window. Counting that against a
    threshold, binning it, or reading percentiles off it describes a series the
    meter never recorded: time above 85 dB is overstated by up to the step and
    the minimum is biased high. The modal fetches the real series from here.

    Keyed on source_file (the NOR140 project folder), not run_number, so a
    re-import between page render and fetch cannot swap the measurement under
    the modal. ~3,600 floats for a one-hour run.
    """
    prof = get_run_prof_by_source(date, source_file, serial=_req_serial())
    if prof is None:
        return jsonify({'status': 'error', 'message': 'Run not found'}), 404
    return jsonify({'status': 'ok', **prof})


if __name__ == '__main__':
    # init_db() and startup_sync_from_peer() already ran above, at import
    # time — this dev entry point just starts the server. Not used in
    # production: gunicorn (under systemd, see setup.sh) imports the module
    # and serves `app` directly, and never runs this block.
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
