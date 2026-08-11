#!/bin/bash
# Deploy noise-meter app on a Pi running the LBA flight tracker.
# Run as flightdata user (not root) — uses existing infrastructure.
#
# Usage:
#   bash setup.sh           # auto-detects Pi (Catherine or Gladys)
#   bash setup.sh --pi Catherine
#   bash setup.sh --pi Gladys

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
APP_DIR="/home/flightdata/noise-meter"
PI_NAME="${PI_NAME:-}"

# Parse args
while [[ $# -gt 0 ]]; do
    case $1 in
        --pi) PI_NAME="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

# Auto-detect Pi from hostname if not set
if [ -z "$PI_NAME" ]; then
    HOST=$(hostname)
    if echo "$HOST" | grep -qi "catherine"; then
        PI_NAME="Catherine"
    elif echo "$HOST" | grep -qi "gladys"; then
        PI_NAME="Gladys"
    else
        echo "Cannot auto-detect Pi name from hostname '$HOST'."
        echo "Run with: bash setup.sh --pi Catherine   or   --pi Gladys"
        exit 1
    fi
fi

echo "==========================================="
echo "Noise Monitor — deploying on $PI_NAME"
echo "==========================================="

if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: .env not found — copy .env.example to .env and fill it in"
    exit 1
fi

# Pick correct python for this Pi
if [ "$PI_NAME" = "Gladys" ]; then
    PYTHON="/home/flightdata/flightdata/venv/bin/python3"
    PIP="/home/flightdata/flightdata/venv/bin/pip"
else
    PYTHON="/usr/bin/python3"
    PIP="pip3"
fi

echo "Python: $PYTHON"

# Install Flask (likely already present)
echo "Checking dependencies..."
$PIP install flask --quiet 2>/dev/null || $PIP install --break-system-packages flask --quiet

# Install systemd service
SERVICE_FILE="/etc/systemd/system/noise-app.service"
echo "Installing systemd service..."
sudo tee "$SERVICE_FILE" > /dev/null << EOF
[Unit]
Description=NOR140 Noise Monitor Web App
After=network.target

[Service]
Type=simple
User=flightdata
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$PYTHON $APP_DIR/noise_app.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable noise-app
sudo systemctl restart noise-app

# Peer sync timer (runs every 15 minutes)
sudo tee /etc/systemd/system/noise-sync.service > /dev/null << EOF
[Unit]
Description=NOR140 Noise Peer Sync
After=network.target

[Service]
Type=oneshot
User=flightdata
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$PYTHON $APP_DIR/sync_peer.py
EOF

sudo tee /etc/systemd/system/noise-sync.timer > /dev/null << EOF
[Unit]
Description=NOR140 Noise Peer Sync Timer

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable noise-sync.timer
sudo systemctl start noise-sync.timer

echo ""
sleep 2
sudo systemctl status noise-app --no-pager | head -12
echo ""
echo "==========================================="
echo "Done! Visit http://$(hostname).local:5001"
echo "==========================================="
