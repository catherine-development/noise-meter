#!/bin/bash
# Deploy noise-meter to both Pis and restart the service.
# Run from Mac. Uses local network IPs (faster than Cloudflare tunnel).
#
# First deploy:  bash deploy_to_pis.sh --setup
# Updates:       bash deploy_to_pis.sh

set -e

GLADYS="flightdata@192.168.1.116"
CATHERINE="flightdata@ssh-catherine.ives.org.uk"
REMOTE_DIR="/home/flightdata/noise-meter"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"
SETUP=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --setup) SETUP=true; shift ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

# Files to deploy (add any new files here)
FILES=(
    noise_app.py
    noise_db.py
    reports_db.py
    noise_parser.py
    nor140_format.py
    nor140_exporter.py
    backfill_glob.py
    backfill_prof.py
    sync_peer.py
    requirements.txt
    setup.sh
)
DIRS=(
    templates
    static
)

deploy_to() {
    local HOST=$1
    local PI=$2
    echo ""
    echo "── Deploying to $PI ($HOST) ──"

    # Create remote directory if needed
    ssh "$HOST" "mkdir -p $REMOTE_DIR"

    # Copy files
    for f in "${FILES[@]}"; do
        [ -f "$LOCAL_DIR/$f" ] && scp -q "$LOCAL_DIR/$f" "$HOST:$REMOTE_DIR/$f"
    done
    for d in "${DIRS[@]}"; do
        [ -d "$LOCAL_DIR/$d" ] && scp -qr "$LOCAL_DIR/$d" "$HOST:$REMOTE_DIR/"
    done

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

    echo "  Done ✓"
}

deploy_to "$GLADYS"    "Gladys"
deploy_to "$CATHERINE" "Catherine"

echo ""
echo "Both Pis updated."
echo "  Gladys:    http://192.168.1.116:5001"
echo "  Catherine: http://192.168.1.138:5001"
