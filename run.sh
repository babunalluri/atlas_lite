#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

export KITE_CREDENTIALS_PATH="${KITE_CREDENTIALS_PATH:-./kite_credentials}"
export ATLAS_LITE_PORT="${ATLAS_LITE_PORT:-8090}"
export ATLAS_LITE_LOG_LEVEL="${ATLAS_LITE_LOG_LEVEL:-WARNING}"
export ATLAS_LITE_RECORD="${ATLAS_LITE_RECORD:-1}"
export ATLAS_LITE_RECORD_START="${ATLAS_LITE_RECORD_START:-09:00}"
export ATLAS_LITE_RECORD_END="${ATLAS_LITE_RECORD_END:-15:35}"
export ATLAS_LITE_RECORD_WEEKDAYS_ONLY="${ATLAS_LITE_RECORD_WEEKDAYS_ONLY:-1}"

echo "Atlas Lite → http://localhost:${ATLAS_LITE_PORT}"
echo "Kite creds → ${KITE_CREDENTIALS_PATH}"
exec .venv/bin/python -m atlas_lite
