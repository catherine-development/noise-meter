#!/usr/bin/env python3
"""
NOR140 Sound Level Meter — Web Viewer
Runs on Raspberry Pi; data uploaded via web interface or import_sdcard.py
"""
import os
import json
import functools
import urllib.request
import urllib.error
import threading

_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _v = _line.split('=', 1)
                if _k.strip() not in os.environ:
                    os.environ[_k.strip()] = _v.strip().strip("'\"")

from flask import Flask, render_template, request, jsonify, redirect, url_for, abort, flash

from noise_db import (init_db, import_sessions, get_all_sessions_json,
                      get_import_log, get_sessions_since, get_existing_dates)
from noise_parser import parse_zip

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', os.urandom(24))

IMPORT_KEY = os.environ.get('IMPORT_API_KEY', '')
UPLOAD_PASS = os.environ.get('UPLOAD_PASSWORD', '')
PI_NAME    = os.environ.get('PI_NAME', 'Pi')
PEER_URL   = os.environ.get('PEER_URL', '')


def _push_to_peer(sessions):
    """Push sessions to peer Pi in a background thread (best-effort)."""
    if not PEER_URL or not sessions:
        return

    def _do_push():
        payload = json.dumps({'sessions': sessions}, separators=(',', ':')).encode()
        req = urllib.request.Request(
            PEER_URL.rstrip('/') + '/import',
            data=payload,
            headers={'Content-Type': 'application/json', 'X-Import-Key': IMPORT_KEY},
            method='POST',
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                print(f"Peer sync: {resp.read().decode()}")
        except Exception as e:
            print(f"Peer sync failed: {e}")

    threading.Thread(target=_do_push, daemon=True).start()


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


def check_upload_auth():
    """Check upload password (from form or header). Returns True if authorised."""
    if not UPLOAD_PASS:
        return True  # no password set — open
    provided = (request.form.get('upload_password', '') or
                request.headers.get('X-Upload-Password', ''))
    return provided == UPLOAD_PASS


@app.route('/')
def index():
    data = get_all_sessions_json()
    data_json = json.dumps(data, separators=(',', ':'))
    return render_template('index.html', data_json=data_json, pi_name=PI_NAME)


@app.route('/api/data.json')
def api_data():
    return jsonify(get_all_sessions_json())


@app.route('/api/sync')
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
def upload_page():
    log = get_import_log()
    needs_password = bool(UPLOAD_PASS)
    return render_template('upload.html', pi_name=PI_NAME, log=log,
                           needs_password=needs_password)


@app.route('/upload', methods=['POST'])
def do_upload():
    if not check_upload_auth():
        flash('Incorrect password.', 'error')
        return redirect(url_for('upload_page'))

    f = request.files.get('file')
    if not f or not f.filename:
        flash('No file selected.', 'error')
        return redirect(url_for('upload_page'))

    raw = f.read()
    fname = f.filename.lower()

    # Parse uploaded file
    try:
        if fname.endswith('.zip'):
            parsed = parse_zip(raw)
        elif fname.endswith('.json'):
            data = json.loads(raw)
            parsed = data.get('sessions', [data] if 'd' in data else [])
        else:
            flash('Please upload a .zip of the SD card or a .json export.', 'error')
            return redirect(url_for('upload_page'))
    except Exception as e:
        flash(f'Could not read file: {e}', 'error')
        return redirect(url_for('upload_page'))

    if not parsed:
        flash('No valid measurement sessions found in the file.', 'error')
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

    # Duplicate check
    existing = get_existing_dates()
    new_sessions = [s for s in parsed if s['d'] not in existing]
    skipped = [s['d'] for s in parsed if s['d'] in existing]

    if not new_sessions:
        msg = f'All {len(skipped)} session(s) already in database: {", ".join(skipped)}.'
        flash(msg, 'info')
        return redirect(url_for('upload_page'))

    # Import new sessions
    import_sessions(new_sessions, metadata=metadata)

    # Push to peer Pi in background
    _push_to_peer(new_sessions)

    msg = f'Added {len(new_sessions)} new session(s)'
    if skipped:
        msg += f' ({len(skipped)} already existed: {", ".join(skipped)})'
    msg += '.'
    flash(msg, 'success')
    return redirect(url_for('index'))


# ── Machine-to-machine import (import_sdcard.py / peer sync) ──────────────────

@app.route('/import', methods=['GET'])
def import_redirect():
    return redirect(url_for('upload_page'))


@app.route('/import', methods=['POST'])
@require_api_key
def do_import():
    if request.files.get('file'):
        raw = request.files['file'].read()
        data = json.loads(raw)
    else:
        data = request.get_json(force=True, silent=True) or {}

    sessions = data.get('sessions', [data] if 'd' in data else [])
    if not sessions:
        return jsonify({'error': 'no sessions found in payload'}), 400

    n = import_sessions(sessions)
    return jsonify({'imported': n, 'status': 'ok'})


if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port, debug=False)
