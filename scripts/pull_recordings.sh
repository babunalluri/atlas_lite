#!/usr/bin/env bash
# Move the server's tick recordings to this laptop: copy, verify name + size, then delete on the
# server only verified files dated before today (IST). Today's files stay on the server (still being written).
# Runs after the morning token refresh (scripts/kite_get_access_token.py); safe to run by hand.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KEY="${ATLAS_OCI_KEY:-/Users/babunalluri/MyWork/OCI/atlas-oci.key.key}"
HOST="${ATLAS_OCI_HOST:-opc@137.23.61.107}"
REMOTE="atlas_lite/data/recordings"
LOCAL="${ROOT}/data/recordings"
SSH=(ssh -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=30 "$HOST")
TODAY="$(TZ=Asia/Kolkata date +%Y-%m-%d)"

echo "$(date '+%F %T') pull start (keep server files from ${TODAY})"
mkdir -p "$LOCAL"
rsync -az -e "ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=30" "${HOST}:${REMOTE}/" "${LOCAL}/"

remote_list="$("${SSH[@]}" "cd ${REMOTE} && find . -maxdepth 1 -type f -name '*.gz' -printf '%f %s\n'")"
delete=()
while read -r name size; do
  [[ -z "${name:-}" ]] && continue
  day="$(grep -oE '20[0-9]{2}-[0-9]{2}-[0-9]{2}' <<<"$name" | head -1 || true)"
  [[ -z "$day" || ! "$day" < "$TODAY" ]] && continue
  if [[ -f "${LOCAL}/${name}" && "$(stat -f %z "${LOCAL}/${name}")" == "$size" ]]; then
    delete+=("$name")
  else
    echo "not verified, kept on server: ${name}"
  fi
done <<<"$remote_list"

if ((${#delete[@]})); then
  printf '%s\n' "${delete[@]}" | "${SSH[@]}" "cd ${REMOTE} && xargs -r sudo rm -f --"
fi
echo "$(date '+%F %T') pull done: moved ${#delete[@]} file(s); local $(du -sh "$LOCAL" | cut -f1)"
