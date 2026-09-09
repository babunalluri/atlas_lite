#!/usr/bin/env bash
# Deploy Atlas Lite to the OCI pilot VM and optionally stop the full Atlas stack.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KEY="${ATLAS_OCI_KEY:-/Users/babunalluri/MyWork/atlas/keys/atlas-oci.key.key}"
HOST="${ATLAS_OCI_HOST:-opc@137.23.61.107}"
REMOTE_DIR="${ATLAS_LITE_OCI_DIR:-~/atlas_lite}"
STOP_ATLAS="${STOP_ATLAS:-1}"

SSH=(ssh -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes "$HOST")
RSYNC=(rsync -az --delete
  --exclude .venv/
  --exclude __pycache__/
  --exclude .git/
  --exclude .DS_Store
  --exclude data/
  -e "ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes")

echo "→ Syncing Atlas Lite to ${HOST}:${REMOTE_DIR}"
"${RSYNC[@]}" "$ROOT/" "${HOST}:${REMOTE_DIR}/"

echo "→ Ensuring kite credentials and data exist on host"
"${SSH[@]}" "test -f ${REMOTE_DIR}/kite_credentials || (echo 'missing kite_credentials on remote' >&2; exit 1)"

echo "→ Firewall: SSH + 3000 only (if firewalld is active)"
"${SSH[@]}" 'if systemctl is-active --quiet firewalld; then
  sudo firewall-cmd --permanent --remove-port=8090/tcp 2>/dev/null || true
  sudo firewall-cmd --permanent --remove-port=7777/tcp 2>/dev/null || true
  sudo firewall-cmd --permanent --remove-port=8080/tcp 2>/dev/null || true
  sudo firewall-cmd --permanent --add-port=3000/tcp 2>/dev/null || true
  sudo firewall-cmd --reload
fi'

echo "→ Ensuring data + kite_credentials ownership for container user (uid 1000)"
"${SSH[@]}" "mkdir -p ${REMOTE_DIR}/data/recordings && sudo chown -R 1000:1000 ${REMOTE_DIR}/data && sudo chown 1000:1000 ${REMOTE_DIR}/kite_credentials && sudo chmod 600 ${REMOTE_DIR}/kite_credentials"

echo "→ Building and starting Atlas Lite"
"${SSH[@]}" "cd ${REMOTE_DIR} && docker compose up -d --build"

if [[ "$STOP_ATLAS" == "1" ]]; then
  echo "→ Stopping full Atlas stack (docker compose down in ~/atlas)"
  "${SSH[@]}" 'cd ~/atlas && docker compose -f docker-compose.yml -f docker-compose.pilot.yml down'
fi

echo "→ Status"
"${SSH[@]}" "cd ${REMOTE_DIR} && docker compose ps && curl -fsS http://127.0.0.1:3000/health"
echo ""
echo "Atlas Lite → http://137.23.61.107:3000"
