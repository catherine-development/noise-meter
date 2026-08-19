#!/bin/bash
# Deploy noise-meter to both Pis and restart the service.
# Run from Mac. Uses local network IPs (faster than Cloudflare tunnel).
#
# First deploy:     bash deploy_to_pis.sh --setup
# Updates:          bash deploy_to_pis.sh
# One Pi only:       bash deploy_to_pis.sh --pi Catherine
# Skip the tests:    bash deploy_to_pis.sh --skip-tests   (not recommended)

set -e

GLADYS="flightdata@192.168.1.116"
CATHERINE="flightdata@ssh-catherine.ives.org.uk"
REMOTE_DIR="/home/flightdata/noise-meter"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"
TEST_PY="/tmp/nm-venv/bin/python3"
SETUP=false
SKIP_TESTS=false
PI_FILTER=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --setup) SETUP=true; shift ;;
        --skip-tests) SKIP_TESTS=true; shift ;;
        --pi) PI_FILTER="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

if [ -n "$PI_FILTER" ] && [ "$PI_FILTER" != "Catherine" ] && [ "$PI_FILTER" != "Gladys" ]; then
    echo "ERROR: --pi must be Catherine or Gladys, got '$PI_FILTER'"
    exit 1
fi

# ── Test gate ────────────────────────────────────────────────────────────────
# A failure here means the code that is about to be copied onto two Pis
# serving live measurements is known-broken. --skip-tests exists for the case
# where the test venv itself is unavailable, not as a routine bypass.
if $SKIP_TESTS; then
    echo "WARNING: --skip-tests set — deploying WITHOUT running the test suite."
else
    if [ ! -x "$TEST_PY" ]; then
        echo "ERROR: test interpreter not found at $TEST_PY."
        echo "Set it up, or pass --skip-tests to deploy without running the suite (not recommended)."
        exit 1
    fi
    echo "Running the test suite ($TEST_PY test_modules.py)..."
    if ! (cd "$LOCAL_DIR" && "$TEST_PY" test_modules.py); then
        echo ""
        echo "ERROR: test suite failed — aborting deploy. Fix it, or pass --skip-tests to override."
        exit 1
    fi
    echo ""
fi

# ── File list ────────────────────────────────────────────────────────────────
# Every file git tracks, rather than a hand-maintained list that silently
# stops matching reality the first time someone adds a file and forgets to
# update this script. .gitignore already keeps MEAS118/, *.zip and .env out
# of `git ls-files`; the filter below is defence in depth, not the primary
# mechanism, in case any of those are ever force-added.
VERSION="$(git -C "$LOCAL_DIR" rev-parse --short HEAD)"
FILELIST="$(mktemp)"
trap 'rm -f "$FILELIST"' EXIT
git -C "$LOCAL_DIR" ls-files \
    | grep -vE '^\.claude/|^MEAS118|\.zip$|^\.env$' \
    > "$FILELIST"

deploy_to() {
    local HOST=$1
    local PI=$2
    echo ""
    echo "── Deploying to $PI ($HOST) ──"

    # Create remote directory if needed
    ssh "$HOST" "mkdir -p $REMOTE_DIR"

    # Copy every tracked file in one pass, preserving the templates/, docs/
    # etc. subdirectory structure — a streamed tar instead of a scp loop plus
    # a separate list of directories to copy recursively.
    # COPYFILE_DISABLE stops macOS bsdtar embedding AppleDouble xattr headers
    # that GNU tar on the Pi warns about on every file.
    COPYFILE_DISABLE=1 tar -C "$LOCAL_DIR" -cf - -T "$FILELIST" | ssh "$HOST" "tar -xf - -C $REMOTE_DIR"

    # Record what's actually running, for /health and for whoever is
    # debugging "which commit is this?" at 11pm.
    ssh "$HOST" "echo $VERSION > $REMOTE_DIR/VERSION"

    # Copy .env if it exists locally (first deploy only)
    if $SETUP && [ -f "$LOCAL_DIR/.env" ]; then
        scp -q "$LOCAL_DIR/.env" "$HOST:$REMOTE_DIR/.env"
        echo "  Copied .env"
    fi

    if $SETUP; then
        echo "  Running setup.sh on $PI..."
        ssh "$HOST" "cd $REMOTE_DIR && bash setup.sh --pi $PI"
    else
        # Gladys uses the flight-tracker's venv; Catherine uses system Python.
        # Always reconcile dependencies on every deploy, not just --setup, so
        # a package added to requirements.txt can never silently go missing
        # on one Pi while present on the other.
        if [ "$PI" = "Gladys" ]; then
            PIP="/home/flightdata/flightdata/venv/bin/pip"
        else
            PIP="pip3"
        fi
        echo "  Syncing dependencies..."
        ssh "$HOST" "$PIP install -r $REMOTE_DIR/requirements.txt --quiet 2>/dev/null || $PIP install --break-system-packages -r $REMOTE_DIR/requirements.txt --quiet"
        echo "  Restarting noise-app service..."
        ssh "$HOST" "sudo systemctl restart noise-app"
        ssh "$HOST" "sudo systemctl status noise-app --no-pager | head -5"
    fi

    echo "  Done ✓ ($PI now at $VERSION)"
}

if [ -z "$PI_FILTER" ] || [ "$PI_FILTER" = "Gladys" ]; then
    deploy_to "$GLADYS" "Gladys"
fi
if [ -z "$PI_FILTER" ] || [ "$PI_FILTER" = "Catherine" ]; then
    deploy_to "$CATHERINE" "Catherine"
fi

echo ""
if [ -z "$PI_FILTER" ]; then
    echo "Both Pis updated to $VERSION."
    echo "  Gladys:    http://192.168.1.116:5001"
    echo "  Catherine: http://192.168.1.138:5001"
else
    echo "$PI_FILTER updated to $VERSION."
fi
