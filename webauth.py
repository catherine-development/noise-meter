"""
Authentication, API-key and CSRF machinery shared by the app and its blueprints.

The real login implementation is the flight tracker's `auth` module, which lives
outside this repo; the sys.path entry below is what makes it importable. If it
is missing, `login_required` still degrades to a no-op — the blueprints need a
decorator at import time — but the app refuses to start unless the operator has
explicitly asked for an unauthenticated instance with ALLOW_UNAUTHENTICATED=1.
Failing open was the old behaviour and it made every page and every API on two
publicly-reachable hostnames world-readable whenever the import broke.

Only the decorators and checks live here. The /login and /logout routes stay in
noise_app.py — the blueprints need the decorators at import time, but nothing
needs the routes.
"""
import functools
import hmac
import logging
import os
import secrets
import sys

from flask import request, abort, session as flask_session

from config import IMPORT_KEY, UPLOAD_PASS

log = logging.getLogger('noise.webauth')

# Shared auth module from flight tracker
sys.path.insert(0, '/home/flightdata/flightdata')
try:
    from auth import login_required
    AUTH_AVAILABLE = True
except ImportError as _exc:
    AUTH_AVAILABLE = False
    _AUTH_IMPORT_ERROR = str(_exc)
    log.error('auth module could not be imported (%s) — every route would be '
              'public. Set ALLOW_UNAUTHENTICATED=1 only for local development.',
              _AUTH_IMPORT_ERROR)

    def login_required(f):
        return f


class InsecureConfiguration(RuntimeError):
    """The app would start with authentication switched off."""


def allow_unauthenticated():
    """Read afresh each call so tests can toggle it without reimporting."""
    return os.environ.get('ALLOW_UNAUTHENTICATED', '').strip() in ('1', 'true', 'yes')


def check_startup_security():
    """Refuse to start an app that has no authentication behind it.

    Called from noise_app at import time rather than from this module's import,
    so the blueprints can still import `login_required` (and so the failure is
    raised once, by the thing that actually serves requests).
    """
    if not AUTH_AVAILABLE:
        if not allow_unauthenticated():
            raise InsecureConfiguration(
                'The flight-tracker auth module could not be imported, so no '
                'route would require a login. Refusing to start. Fix the auth '
                'install, or set ALLOW_UNAUTHENTICATED=1 for a development '
                'instance that is not reachable from the internet.')
        log.error('Starting with authentication DISABLED (ALLOW_UNAUTHENTICATED=1). '
                  'Every page and API on this instance is public.')
    if not UPLOAD_PASS:
        # Deliberate configuration (Catherine, 2026-08-20): uploads are gated by
        # the flight-data login system alone. UPLOAD_PASSWORD remains available
        # as an optional second gate for anyone who wants it.
        log.info('UPLOAD_PASSWORD not set — uploads are gated by login only.')


# ── API key ───────────────────────────────────────────────────────────────────
#
# Header or form body only. A key in the query string ends up in access logs,
# in Cloudflare's request log and in Referer headers, so ?api_key= is no longer
# read at all — the three Python clients (peer_client, sync_peer, import_sdcard)
# have always sent the X-Import-Key header.

def presented_api_key():
    key = request.headers.get('X-Import-Key')
    if key:
        return key
    return request.form.get('api_key', '') or ''


def has_valid_api_key():
    key = presented_api_key()
    if not IMPORT_KEY or not key:
        return False
    return hmac.compare_digest(key, IMPORT_KEY)


def require_api_key(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not has_valid_api_key():
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
        if has_valid_api_key():
            return f(*args, **kwargs)
        return login_required(f)(*args, **kwargs)
    return decorated


def check_upload_auth():
    """Check upload password (from form or header). Returns True if authorised."""
    if not UPLOAD_PASS:
        return True  # no password set — open; check_startup_security() warns
    provided = (request.form.get('upload_password', '') or
                request.headers.get('X-Upload-Password', ''))
    return bool(provided) and hmac.compare_digest(provided, UPLOAD_PASS)


# ── CSRF ──────────────────────────────────────────────────────────────────────
#
# Cookie-authenticated state-changing routes (delete a session, purge, edit
# metadata, tag a run, the assessments and reports CRUD) were reachable by any
# page the logged-in browser happened to visit. A per-session token, checked in
# a before_request hook, closes that. Requests carrying a valid API key are
# exempt: they are not cookie-authenticated, so there is nothing to ride on.

CSRF_SESSION_KEY = '_csrf_token'
CSRF_FORM_FIELD = 'csrf_token'
CSRF_HEADER = 'X-CSRF-Token'
SAFE_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS', 'TRACE'})


def csrf_token():
    """The session's token, minted on first use — which is the GET that renders
    the form, so the token is already in the cookie by the time it posts."""
    token = flask_session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        flask_session[CSRF_SESSION_KEY] = token
    return token


def csrf_protect():
    """app.before_request hook. Returns None to let the request through."""
    if request.method in SAFE_METHODS:
        return None
    if has_valid_api_key():
        return None
    expected = flask_session.get(CSRF_SESSION_KEY)
    presented = request.headers.get(CSRF_HEADER) or request.form.get(CSRF_FORM_FIELD, '') or ''
    if not expected or not presented or not hmac.compare_digest(presented, expected):
        log.warning('CSRF check failed: %s %s', request.method, request.path)
        abort(403, description='CSRF token missing or invalid. Reload the page and retry.')
    return None
