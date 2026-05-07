#!/bin/bash
set -euo pipefail

REPO_DIR="/home/dennis/MusicRemixer"
SERVICE="music-remixer"

cd "$REPO_DIR"

echo "→ Pulling latest from origin..."
git pull origin main

echo "→ Syncing dependencies..."
/snap/bin/uv sync

echo "→ Restarting $SERVICE..."
sudo systemctl restart "$SERVICE"

echo "→ Status:"
sleep 2
sudo systemctl status "$SERVICE" --no-pager -l | head -12
echo "✓ Deploy complete"
