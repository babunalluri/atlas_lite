#!/usr/bin/env bash
# Deploy Atlas Lite to the OCI pilot VM and optionally stop the full Atlas stack.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KEY="${ATLAS_OCI_KEY:-/Users/babunalluri/MyWork/atlas/keys/atlas-oci.key.key}"
HOST="${ATLAS_OCI_HOST:-opc@137.23.61.107}"
REMOTE_DIR="${ATLAS_LITE_OCI_DIR:-~/atlas_lite}"
STOP_ATLAS="${STOP_ATLAS:-1}"

SSH=(ssh -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes "$HOST")
SCP=(scp -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes)
RSYNC=(rsync -az --delete
  --exclude .venv/
  --exclude __pycache__/
  --exclude .pytest_cache/
  --exclude .git/
  --exclude .DS_Store
  --exclude data/
  --exclude kite_credentials
  --exclude llm_credentials
  --exclude .env
  -e "ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes")

echo "→ Syncing Atlas Lite to ${HOST}:${REMOTE_DIR}"
"${RSYNC[@]}" "$ROOT/" "${HOST}:${REMOTE_DIR}/"

echo "→ Ensuring kite credentials and data exist on host"
if [[ ! -f "${ROOT}/kite_credentials" ]]; then
  echo "missing local kite_credentials — cannot refresh remote token" >&2
  exit 1
fi
# Always push kite_credentials (rsync excludes it). Local file is the fresh token source.
echo "→ Copying kite_credentials to remote"
"${SCP[@]}" "${ROOT}/kite_credentials" "${HOST}:${REMOTE_DIR}/kite_credentials"
"${SSH[@]}" "test -f ${REMOTE_DIR}/kite_credentials || (echo 'missing kite_credentials on remote' >&2; exit 1)"

# llm_credentials: never rsync (secrets). Copy once from local if remote missing.
if "${SSH[@]}" "test -f ${REMOTE_DIR}/llm_credentials"; then
  echo "→ Remote llm_credentials already present (left untouched)"
elif [[ -f "${ROOT}/llm_credentials" ]]; then
  echo "→ Copying llm_credentials to remote (first time)"
  "${SCP[@]}" "${ROOT}/llm_credentials" "${HOST}:${REMOTE_DIR}/llm_credentials"
else
  echo "missing llm_credentials locally and on remote — create one before deploy" >&2
  exit 1
fi

# Agent write token persisted in remote .env (compose reads it; never rsynced).
"${SSH[@]}" "cd ${REMOTE_DIR} && if [[ ! -f .env ]] || ! grep -q '^ATLAS_LITE_API_TOKEN=.' .env 2>/dev/null; then
  tok=\$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
  if [[ -f .env ]]; then
    grep -v '^ATLAS_LITE_API_TOKEN=' .env > .env.tmp || true
    mv .env.tmp .env
  fi
  echo \"ATLAS_LITE_API_TOKEN=\$tok\" >> .env
  chmod 600 .env
  echo \"generated ATLAS_LITE_API_TOKEN in ${REMOTE_DIR}/.env\"
else
  echo '→ ATLAS_LITE_API_TOKEN already set in remote .env'
fi"

echo "→ Firewall: SSH + 3000 only (if firewalld is active)"
"${SSH[@]}" 'if systemctl is-active --quiet firewalld; then
  sudo firewall-cmd --permanent --remove-port=8090/tcp 2>/dev/null || true
  sudo firewall-cmd --permanent --remove-port=7777/tcp 2>/dev/null || true
  sudo firewall-cmd --permanent --remove-port=8080/tcp 2>/dev/null || true
  sudo firewall-cmd --permanent --add-port=3000/tcp 2>/dev/null || true
  sudo firewall-cmd --reload
fi'

echo "→ Ensuring data + credentials ownership for container user (uid 1000)"
"${SSH[@]}" "mkdir -p ${REMOTE_DIR}/data/recordings \
  && sudo chown -R 1000:1000 ${REMOTE_DIR}/data \
  && sudo chown 1000:1000 ${REMOTE_DIR}/kite_credentials ${REMOTE_DIR}/llm_credentials \
  && sudo chmod 600 ${REMOTE_DIR}/kite_credentials ${REMOTE_DIR}/llm_credentials"
if "${SSH[@]}" "test -f ${REMOTE_DIR}/.env"; then
  "${SSH[@]}" "sudo chmod 600 ${REMOTE_DIR}/.env || chmod 600 ${REMOTE_DIR}/.env"
fi

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
echo "Agent POSTs need header X-Atlas-Token = ATLAS_LITE_API_TOKEN from ${REMOTE_DIR}/.env"
