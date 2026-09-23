#!/bin/bash
# MCS durable-job drain (history/trickle) — hermes cron job
# (replaces local.mcs-deep.plist). Same output contract as mcs_check.sh:
# run log gets everything, stdout is an alert line on failure only.
set -u
PATH="$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH
LOG="__DATA__/run.log"
PY=__PYTHON__

"$PY" __REPO__/mcs/ingest/run_check.py --json --jobs-only >>"$LOG" 2>&1
rc=$?
if [ "$rc" -ne 0 ] && [ "$rc" -ne 2 ]; then
  printf 'mcs deep: run_check exited %d — see %s\n' "$rc" "$LOG"
fi
exit "$rc"
