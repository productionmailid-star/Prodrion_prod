#!/usr/bin/env bash
# Prodrion deploy / upgrade. Run as a sudo-capable user on the VPS.
#   ./deploy/deploy.sh [git-ref]
set -euo pipefail

APP_DIR=/opt/prodrion
APP_USER=prodrion
REF="${1:-production-hardening}"

echo "==> Updating checkout to ${REF}"
sudo -u "$APP_USER" git -C "$APP_DIR" fetch --all --prune
sudo -u "$APP_USER" git -C "$APP_DIR" checkout "$REF"
sudo -u "$APP_USER" git -C "$APP_DIR" pull --ff-only

echo "==> Installing dependencies"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "==> Applying schema / migrations"
sudo -u "$APP_USER" env $(grep -v '^#' /etc/prodrion/prodrion.env | xargs) \
    "$APP_DIR/.venv/bin/python" -c "from db import init_db; init_db()"

echo "==> Restarting services"
sudo systemctl restart prodrion-api prodrion-worker

sleep 2
sudo systemctl --no-pager --lines=0 status prodrion-api prodrion-worker
curl -fsS http://127.0.0.1:8000/health | head -c 400; echo
echo "==> Done."
