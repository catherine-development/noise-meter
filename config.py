"""
Environment configuration shared by the app and its blueprints.

Importing this module loads .env into os.environ without overriding anything
already set in the real environment, then exposes the app-level settings.

Import order matters: this must be imported before noise_db, which reads
NOISE_DB_PATH at module scope. noise_app.py imports it first for that reason.

Nothing here imports from the rest of the app, so the blueprints can depend on
it without creating a cycle.
"""
import os

_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _v = _line.split('=', 1)
                if _k.strip() not in os.environ:
                    os.environ[_k.strip()] = _v.strip().strip("'\"")

IMPORT_KEY  = os.environ.get('IMPORT_API_KEY', '')
UPLOAD_PASS = os.environ.get('UPLOAD_PASSWORD', '')
PI_NAME     = os.environ.get('PI_NAME', 'Pi')
PEER_URL    = os.environ.get('PEER_URL', '')
