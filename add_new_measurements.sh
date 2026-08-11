#!/bin/bash
# Run this after plugging in the NOR140 SD card to push new sessions to both Pis.
cd "$(dirname "$0")"
source .env 2>/dev/null || true

python3 import_sdcard.py \
    --push https://noise-catherine.ives.org.uk \
    --push https://noise-gladys.ives.org.uk

echo ""
echo "Done. View at https://noise-catherine.ives.org.uk"
