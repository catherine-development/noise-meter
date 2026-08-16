"""
Authentication decorators shared by the app and its blueprints.

The real implementation is the flight tracker's `auth` module, which lives
outside this repo; the sys.path entry below is what makes it importable. If it
is missing, login_required degrades to a no-op so the app still starts.

Only the decorators live here. The /login and /logout routes stay in
noise_app.py — the blueprints need the decorators at import time, but nothing
needs the routes.
"""
import functools
import sys

from flask import request, abort

from config import IMPORT_KEY, UPLOAD_PASS

# Shared auth module from flight tracker
sys.path.insert(0, '/home/flightdata/flightdata')
try:
    from auth import login_required
    AUTH_AVAILABLE = True
except ImportError:
    AUTH_AVAILABLE = False
    def login_required(f):
        return f


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
