#!/usr/bin/env python3
"""
import_sdcard.py — NOR140 SD card importer
Run on Mac with the SD card inserted.

Usage:
  python3 import_sdcard.py                          # parse & print summary
  python3 import_sdcard.py --output noise_data.json # save JSON file
  python3 import_sdcard.py --push https://noise.ives.org.uk  # push to Pi
  python3 import_sdcard.py --push https://... --since 2026-08-01  # only new sessions
"""

import struct
import os
import json
import math
import sys
import argparse
import urllib.request
import urllib.error
from datetime import datetime

SD_ROOT    = os.environ.get('SD_ROOT', '/Volumes/NO LABEL/MEAS118')
IMPORT_KEY = os.environ.get('IMPORT_API_KEY', '')

CAP_LAEQ   = 130
CAP_PEAK   = 160
EXCLUDE_DIRS = {'000101'}


def bcd(b):
    return (b >> 4) * 10 + (b & 0xF)


def read_glob(path):
    with open(path, 'rb') as f:
        raw = f.read()
    o = 0x19
    yy, mo, dd, hh, mm, ss = (bcd(raw[o+i]) for i in range(6))
    return f"{2000+yy:04d}-{mo:02d}-{dd:02d}", f"{hh:02d}:{mm:02d}:{ss:02d}"


def read_prof(path):
    with open(path, 'rb') as f:
        raw = f.read()
    if len(raw) < 13:
        return []
    return [
        [struct.unpack_from('<H', raw, off + i * 2)[0] / 100 for i in range(5)]
        for off in range(3, len(raw) - 9, 10)
    ]


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def downsample(arr, step):
    return [max(arr[i:i + step]) for i in range(0, len(arr), step)]


def energy_avg(values):
    return 10 * math.log10(sum(10 ** (v / 10) for v in values) / len(values))


def parse_session(sess_dir):
    part_dir = os.path.join(sess_dir, 'PART0000')
    if not os.path.isdir(part_dir):
        return None

    projects = []
    for pname in sorted(os.listdir(part_dir)):
        pdir = os.path.join(part_dir, pname)
        if not os.path.isdir(pdir):
            continue
        gfiles = sorted(f for f in os.listdir(pdir) if f.startswith('GLOB'))
        pfiles = sorted(f for f in os.listdir(pdir) if f.startswith('PROF'))
        if not gfiles or not pfiles:
            continue

        date, start = read_glob(os.path.join(pdir, gfiles[0]))
        recs = read_prof(os.path.join(pdir, pfiles[0]))
        if not recs:
            continue

        laeq_raw   = [clamp(r[0], 30, CAP_LAEQ) for r in recs]
        lcpeak_raw = [clamp(r[4], 30, CAP_PEAK)  for r in recs]
        n    = len(recs)
        step = 1 if n <= 120 else 2 if n <= 300 else 5 if n <= 900 else 10

        # Sanity check — skip corrupted sessions
        if max(laeq_raw) > 200:
            continue

        leq = round(energy_avg(laeq_raw), 2)
        projects.append({
            'start':  start,
            'n':      n,
            'step':   step,
            'avg':    leq,
            'mn':     round(min(laeq_raw), 1),
            'mx':     round(max(laeq_raw), 1),
            'pmx':    round(max(lcpeak_raw), 1),
            'laeq':   [round(v, 1) for v in downsample(laeq_raw,   step)],
            'lcpeak': [round(v, 1) for v in downsample(lcpeak_raw, step)],
        })
    return projects or None


def parse_all(since=None):
    if not os.path.isdir(SD_ROOT):
        print(f"ERROR: SD card not found at {SD_ROOT}", file=sys.stderr)
        sys.exit(1)

    sessions = []
    for dname in sorted(os.listdir(SD_ROOT)):
        if dname in EXCLUDE_DIRS or not dname.isdigit() or len(dname) != 6:
            continue
        yy, mo, dd = int(dname[:2]), int(dname[2:4]), int(dname[4:6])
        date_str = f"20{yy:02d}-{mo:02d}-{dd:02d}"
        if since and date_str < since:
            continue

        projects = parse_session(os.path.join(SD_ROOT, dname))
        if not projects:
            continue

        avg = round(energy_avg([p['avg'] for p in projects]), 2)
        sessions.append({
            'd':        date_str,
            'avg':      avg,
            'mx':       round(max(p['mx'] for p in projects), 1),
            'projects': projects,
        })
    return sessions


def latest_date_on_pi(url):
    """Ask the Pi what its most recent session date is."""
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/api/data.json", timeout=10) as resp:
            data = json.loads(resp.read())
        sessions = data.get('sessions', [])
        return sessions[-1]['d'] if sessions else None
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

    if args.sd_root:
        global SD_ROOT
        SD_ROOT = args.sd_root

    since = args.since
    print(f"Reading SD card from {SD_ROOT}" + (f" (since {since})" if since else ""))
    sessions = parse_all(since=since)

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
            # Auto-detect what's already on the Pi and skip those sessions
            latest = latest_date_on_pi(url)
            to_send = [s for s in sessions if latest is None or s['d'] > latest]
            if not to_send:
                print(f"\n{url}: already up to date (latest: {latest})")
                continue
            print(f"\nPushing {len(to_send)} new session(s) to {url} (Pi has up to {latest or 'nothing'}) …")
            result = push_to_pi(to_send, url, key)
            print(f"Done: {result}")


if __name__ == '__main__':
    main()
