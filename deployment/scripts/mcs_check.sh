#!/bin/bash
# MCS unread check — hermes cron job (replaces local.mcs-check.plist).
# Full run output appends to the MCS run log; stdout carries an alert line
# only on hard failure (empty stdout = silent, watchdog convention).
# Exit 2 = session expired: the MCS notifier already alerts via
# notify_system_target (deduped) — silent here to avoid 15-min spam.
set -u
PATH="$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH
LOG="__DATA__/run.log"
PY=__PYTHON__

"$PY" __REPO__/mcs/ingest/run_check.py --json --download-files >>"$LOG" 2>&1
rc=$?
if [ "$rc" -ne 0 ] && [ "$rc" -ne 2 ]; then
  printf 'mcs check: run_check exited %d — see %s\n' "$rc" "$LOG"
fi
exit "$rc"
