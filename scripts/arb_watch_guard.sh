#!/usr/bin/env bash
# Host cron guard for the NSE vs BSE gap watcher (read-only, paper only): if the watcher is not
# running inside the atlas-lite container (container restart, deploy), start it again. Does nothing
# after UNTIL. Installed for the research week with:
#   */10 9-15 * * 1-5 /home/opc/atlas_lite/scripts/arb_watch_guard.sh
# Remove with `crontab -e`.
set -u
UNTIL="${ARB_UNTIL:-2026-11-06}"
LOG="$HOME/atlas_lite/data/arb/guard.log"
[[ "$(date +%F)" > "$UNTIL" ]] && exit 0
docker inspect -f '{{.State.Running}}' atlas-lite 2>/dev/null | grep -q true || exit 0
if docker exec atlas-lite sh -c 'for p in /proc/[0-9]*; do case "$(tr "\000" " " < "$p/cmdline" 2>/dev/null)" in "python scripts/nse_bse_gap_watch.py"*) exit 0;; esac; done; exit 1'; then
  exit 0
fi
docker exec -d atlas-lite sh -c "python scripts/nse_bse_gap_watch.py --until $UNTIL >> /app/data/arb/watch.log 2>&1"
echo "$(date '+%F %T') watcher was not running; started it (--until $UNTIL)" >> "$LOG"
