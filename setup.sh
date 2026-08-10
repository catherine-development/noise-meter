#!/bin/bash
# First-boot setup for NOR140 Noise Monitor Pi
# Usage: sudo ./setup.sh
#
# Before running:
#   1. Copy .env.example to .env
#   2. Edit .env with your PI_NAME, SECRET_KEY, IMPORT_API_KEY

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: Run with sudo"
    exit 1
fi

if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: .env file not found — copy .env.example to .env and fill it in first"
    exit 1
fi

export $(grep -v '^#' "$ENV_FILE" | grep -v '^$' | xargs)
PI_NAME="${PI_NAME:-Pi}"

echo "==========================================="
echo "NOR140 Noise Monitor - Pi Setup"
echo "==========================================="
echo "Pi name: $PI_NAME"
echo ""

# Create system user and data directory
echo "Creating noisedata user and directory..."
id -u noisedata &>/dev/null || useradd -r -m -d /home/noisedata -s /bin/bash noisedata
mkdir -p /home/noisedata/noisedata
chown -R noisedata:noisedata /home/noisedata

# Copy app files
echo "Installing app files..."
APP_DIR=/home/noisedata/noisedata
cp "$SCRIPT_DIR/noise_app.py"    "$APP_DIR/"
cp "$SCRIPT_DIR/noise_db.py"     "$APP_DIR/"
cp "$SCRIPT_DIR/.env"            "$APP_DIR/"
cp -r "$SCRIPT_DIR/templates"    "$APP_DIR/"
cp -r "$SCRIPT_DIR/static"       "$APP_DIR/" 2>/dev/null || true
chown -R noisedata:noisedata "$APP_DIR"

# Install Python dependencies
echo "Installing Python packages..."
pip3 install flask flask-limiter --quiet

# Install systemd service
echo "Installing systemd service..."
cp "$SCRIPT_DIR/deploy/systemd/noise-app.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable noise-app.service
systemctl restart noise-app.service

echo ""
echo "==========================================="
echo "Setup complete!"
echo "  Web app: http://$(hostname).local:5001"
echo "  Import:  http://$(hostname).local:5001/import"
echo "==========================================="
