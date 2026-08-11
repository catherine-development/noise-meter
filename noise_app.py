#!/usr/bin/env python3
"""
NOR140 Sound Level Meter — Web Viewer
Runs on Raspberry Pi; data imported from SD card via import_sdcard.py
"""
import os
import json
import functools

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

from noise_db import init_db, import_sessions, get_all_sessions_json, get_import_log, get_sessions_since

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', os.urandom(24))

IMPORT_KEY = os.environ.get('IMPORT_API_KEY', '')
PI_NAME    = os.environ.get('PI_NAME', 'Pi')


def require_api_key(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        key = (request.headers.get('X-Import-Key') or
               request.form.get('api_key', '') or
               request.args.get('api_key', ''))
        if not IMPORT_KEY or key != IMPORT_KEY:
            if request.is_json or request.headers.get('X-Import-Key'):
                abort(403)
            flash('Invalid import key.', 'error')
            return redirect(url_for('upload_page'))
        return f(*args, **kwargs)
    return decorated


@app.route('/')
def index():
    data = get_all_sessions_json()
    data_json = json.dumps(data, separators=(',', ':'))
    return render_template('index.html', data_json=data_json, pi_name=PI_NAME)


@app.route('/api/data.json')
def api_data():
    return jsonify(get_all_sessions_json())


@app.route('/import', methods=['GET'])
def upload_page():
    log = get_import_log()
    return render_template('upload.html', pi_name=PI_NAME, log=log)


@app.route('/import', methods=['POST'])
@require_api_key
def do_import():
    # Accept either a JSON file upload or a raw JSON POST body
    if request.files.get('file'):
        raw = request.files['file'].read()
        data = json.loads(raw)
    else:
        data = request.get_json(force=True, silent=True) or {}

    sessions = data.get('sessions', [data] if 'd' in data else [])
    if not sessions:
        if request.is_json:
            return jsonify({'error': 'no sessions found in payload'}), 400
        flash('No sessions found in uploaded file.', 'error')
        return redirect(url_for('upload_page'))

    n = import_sessions(sessions)

    if request.is_json or request.headers.get('X-Import-Key'):
        return jsonify({'imported': n, 'status': 'ok'})
    flash(f'Imported {n} session(s) successfully.', 'success')
    return redirect(url_for('index'))


@app.route('/api/sync')
def api_sync():
    since = request.args.get('since', '1970-01-01T00:00:00')
    sessions = get_sessions_since(since)
    return jsonify({'sessions': sessions, 'count': len(sessions),
                    'pi_name': PI_NAME, 'since': since})


@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'pi': PI_NAME})


if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port, debug=False)
