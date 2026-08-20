"""
Ongoing replication of the flight tracker's login users between the two Pis.

The Pis' logins live in the flight tracker's own database
(/home/flightdata/flightdata/flights.db, table `users`), outside this repo
and with no replication of its own; a one-time manual merge was done on
2026-08-20. The Pis can only reach each other over HTTPS through the
Cloudflare tunnels with the X-Import-Key, so this app carries the rows:
GET /api/users-sync (noise_app) serves export_users(), and sync_peer.py /
peer_client.startup_sync_from_peer() pull it and call apply_users().

Deliberately ADDITIVE ONLY — no last-writer-wins, no deletes, and no
is_active propagation. A new user invited on one Pi appears on the other;
NULL name/phone gaps are filled from the peer; nothing else about an existing
row is ever touched. In particular, DEACTIVATING A USER IS A PER-PI MANUAL
ACT: is_active crosses the wire only inside brand-new rows, so a user
deactivated on one Pi REMAINS ACTIVE ON THE PEER until someone deactivates
them there too. That is a scope line, not an oversight — propagating
is_active (or deletes) safely needs conflict handling that belongs in the
flight tracker, which owns this table. Revisit when the flight tracker grows
its own replication.

Every function is a clean no-op when the database or its users table is
absent (the development Mac and the test suite have neither), and nothing
here ever creates the flight tracker's database file.

PII: emails and phone numbers transit the same authenticated HTTPS channel
as the rest of the replicated data. Do not log them — log counts only.
"""
import logging
import os
import sqlite3

log = logging.getLogger('noise.users_sync')

_DEFAULT_FLIGHTS_DB = '/home/flightdata/flightdata/flights.db'

# id is per-Pi (each side numbers its own rows); invited_by is a local id
# reference into the same table, so it would dangle on the peer. Neither
# crosses the wire.
_EXCLUDED_COLS = ('id', 'invited_by')

# The only columns ever changed on an EXISTING row, and only NULL → value.
_FILL_COLS = ('name', 'phone')


def flights_db_path():
    """Read at call time, not import time, so tests can point it elsewhere."""
    return os.environ.get('FLIGHTS_DB_PATH', _DEFAULT_FLIGHTS_DB)


def _open_users_db():
    """A connection to the flights DB, or None when it (or the users table)
    is absent. Never creates the file: sqlite3.connect() would happily mint
    an empty database where the flight tracker expects its own."""
    path = flights_db_path()
    if not path or not os.path.exists(path):
        return None
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
    if row is None:
        conn.close()
        return None
    return conn


def _user_columns(conn):
    return [r[1] for r in conn.execute('PRAGMA table_info(users)').fetchall()]


def export_users():
    """Every user row, minus the per-Pi columns. [] when there is no
    flights DB here (dev Mac, test suite). Column set is read from the live
    table so a flight-tracker schema change (new digest_* column, say)
    flows through without editing this module."""
    conn = _open_users_db()
    if conn is None:
        return []
    try:
        cols = [c for c in _user_columns(conn) if c not in _EXCLUDED_COLS]
        rows = conn.execute(
            f'SELECT {", ".join(cols)} FROM users ORDER BY email').fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def apply_users(rows):
    """Insert the peer's users that do not exist here (keyed on the UNIQUE
    email), then fill NULL name/phone on rows that do. Additive only — see
    the module docstring for why nothing else is touched. Returns
    {'inserted': n, 'filled': m}; a no-op {'inserted': 0, 'filled': 0} when
    the flights DB or its users table is absent."""
    result = {'inserted': 0, 'filled': 0}
    if not rows:
        return result
    conn = _open_users_db()
    if conn is None:
        return result
    try:
        local_cols = set(_user_columns(conn))
        for row in rows:
            email = row.get('email')
            if not email:
                continue
            keep = {k: v for k, v in row.items()
                    if k in local_cols and k not in _EXCLUDED_COLS}
            cols = sorted(keep)
            cur = conn.execute(
                f'INSERT OR IGNORE INTO users ({", ".join(cols)}) '
                f'VALUES ({", ".join(":" + c for c in cols)})', keep)
            if cur.rowcount:
                result['inserted'] += 1
                continue
            # Existing user: fill gaps only. COALESCE(local, peer) never
            # overwrites a value someone set here, and the WHERE clause
            # makes the count honest (an UPDATE that changes nothing does
            # not count as a fill).
            cur = conn.execute(
                'UPDATE users SET name=COALESCE(name, :name), '
                '  phone=COALESCE(phone, :phone) '
                'WHERE email=:email AND ('
                '  (name IS NULL AND :name IS NOT NULL) OR '
                '  (phone IS NULL AND :phone IS NOT NULL))',
                {'email': email, 'name': row.get('name'),
                 'phone': row.get('phone')})
            result['filled'] += cur.rowcount
        conn.commit()
        return result
    finally:
        conn.close()
