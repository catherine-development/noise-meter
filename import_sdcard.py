#!/usr/bin/env python3
"""
import_sdcard.py — NOR140 SD card importer
Run on Mac with the SD card inserted.

Usage:
  python3 import_sdcard.py                          # parse & print summary
  python3 import_sdcard.py --output noise_data.json # save JSON file
  python3 import_sdcard.py --push https://noise.ives.org.uk  # push to Pi
  python3 import_sdcard.py --push https://... --since 2026-08-01  # only new sessions

Parsing is delegated entirely to noise_parser.parse_files() — the same
canonical, Nortfr-verified decoder used for web uploads and the Pi backfill
scripts — so SD-card imports carry full spectral/1-second-profile fidelity
and never drift out of sync with the rest of the app's NOR140 format logic.
"""

import os
import json
import sys
import argparse
import urllib.request
import urllib.error

from noise_parser import parse_files

SD_ROOT    = os.environ.get('SD_ROOT', '/Volumes/NO LABEL/MEAS118')
IMPORT_KEY = os.environ.get('IMPORT_API_KEY', '')
EXCLUDE_DIRS = {'000101'}


def _collect_files(sd_root, since=None):
    """Walk the SD card tree and collect (relative_path, bytes) for every
    GLOB/PROF file, in the layout noise_parser.parse_files() expects:
    YYMMDD/PART0000/PROJnnnn/{GLOB,PROF}nnnn.DAT
    """
    pairs = []
    for dname in sorted(os.listdir(sd_root)):
        if dname in EXCLUDE_DIRS or not dname.isdigit() or len(dname) != 6:
            continue
        yy, mo, dd = int(dname[:2]), int(dname[2:4]), int(dname[4:6])
        date_str = f"20{yy:02d}-{mo:02d}-{dd:02d}"
        if since and date_str < since:
            continue
        part_dir = os.path.join(sd_root, dname, 'PART0000')
        if not os.path.isdir(part_dir):
            continue
        for pname in sorted(os.listdir(part_dir)):
            pdir = os.path.join(part_dir, pname)
            if not os.path.isdir(pdir):
                continue
            for fname in sorted(os.listdir(pdir)):
                up = fname.upper()
                if up.startswith('GLOB') or up.startswith('PROF'):
                    with open(os.path.join(pdir, fname), 'rb') as f:
                        pairs.append((f'{dname}/PART0000/{pname}/{fname}', f.read()))
    return pairs


def parse_all(sd_root=None, since=None):
    root = sd_root or SD_ROOT
    if not os.path.isdir(root):
        print(f"ERROR: SD card not found at {root}", file=sys.stderr)
        sys.exit(1)
    pairs = _collect_files(root, since=since)
    if not pairs:
        return []
    return parse_files(pairs)


def latest_date_on_pi(url, key=None):
    """Ask the Pi what its most recent session date is.
    /api/data.json requires either a browser session or an API key — send
    the import key so this works non-interactively. Also needs a normal
    User-Agent: Cloudflare's edge bot-protection blocks urllib's default
    ("Python-urllib/3.x") with a 1010 error before the request reaches the app.
    """
    try:
        req = urllib.request.Request(
            f"{url.rstrip('/')}/api/data.json",
            headers={'X-Import-Key': key or IMPORT_KEY, 'User-Agent': 'noise-meter-sync/1.0'},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        sessions = data.get('sessions', [])
        # max(), not sessions[-1] or sessions[0] — don't couple to the API's
        # current sort order (get_all_sessions_json() sorts descending, so
        # sessions[-1] was silently returning the OLDEST date, not the latest).
        return max(s['d'] for s in sessions) if sessions else None
    except Exception:
        return None


def push_to_pi(sessions, url, key):
    payload = json.dumps({'sessions': sessions}, separators=(',', ':')).encode()
    req = urllib.request.Request(
        url.rstrip('/') + '/import',
        data=payload,
        headers={
            'Content-Type':  'application/json',
            'X-Import-Key':  key,
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description='Import NOR140 SD card data')
    parser.add_argument('--output',  help='Save JSON to this file')
    parser.add_argument('--push',    help='POST data to this Pi URL (repeat for multiple)', action='append', default=[])
    parser.add_argument('--key',     help='Import API key (or set IMPORT_API_KEY env var)')
    parser.add_argument('--since',   help='Only import sessions on/after YYYY-MM-DD')
    parser.add_argument('--sd-root', help='SD card root (default: /Volumes/NO LABEL/MEAS118)')
    args = parser.parse_args()

    sd_root = args.sd_root or SD_ROOT
    since = args.since
    print(f"Reading SD card from {sd_root}" + (f" (since {since})" if since else ""))
    sessions = parse_all(sd_root=sd_root, since=since)

    # Pairs the parser found but could not read. Printed before anything else
    # so a corrupt PROJ folder is never mistaken for a day with one run fewer.
    skipped = list(getattr(sessions, 'skipped', ()) or ())
    if skipped:
        print(f"\nWARNING: {len(skipped)} GLOB/PROF pair(s) skipped:", file=sys.stderr)
        for sk in skipped:
            print(f"  {sk['path']}: {sk['reason']}", file=sys.stderr)

    if not sessions:
        print("No sessions found.")
        return

    print(f"\nFound {len(sessions)} session(s):")
    for s in sessions:
        print(f"  {s['d']}  {len(s['projects'])} run(s)  LAeq avg {s['avg']:.1f} dB  max {s['mx']:.1f} dB")

    if args.output:
        with open(args.output, 'w') as f:
            json.dump({'sessions': sessions}, f, separators=(',', ':'))
        print(f"\nSaved to {args.output}")

    if args.push:
        key = args.key or IMPORT_KEY
        if not key:
            print("ERROR: provide --key or set IMPORT_API_KEY env var", file=sys.stderr)
            sys.exit(1)
        for url in args.push:
            # Auto-detect what's already on the Pi and skip older sessions.
            # >= not > : a same-day session may have gained new runs since
            # the Pi's last push (e.g. two SD card imports on one day) —
            # resending it is harmless since import_sessions() upserts.
            latest = latest_date_on_pi(url, key)
            to_send = [s for s in sessions if latest is None or s['d'] >= latest]
            if not to_send:
                print(f"\n{url}: already up to date (latest: {latest})")
                continue
            print(f"\nPushing {len(to_send)} new session(s) to {url} (Pi has up to {latest or 'nothing'}) …")
            result = push_to_pi(to_send, url, key)
            print(f"Done: {result}")


if __name__ == '__main__':
    main()
