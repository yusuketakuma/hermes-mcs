#!/bin/bash
# MCS durable-job drain (history/trickle) — hermes cron job
# (replaces local.mcs-deep.plist). Same output contract as mcs_check.sh:
# run log gets everything, stdout is an alert line on failure only.
set -u
PATH="$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH
LOG=__DATA__/mcs.log
PY=__PYTHON__
WATCHDOG_GRACE=__WATCHDOG_GRACE__
set -- --json --jobs-only
if [ "$WATCHDOG_GRACE" -gt 0 ]; then
  set -- "$@" --watchdog-grace "$WATCHDOG_GRACE"
fi

"$PY" -I __RUN_LOG_HELPER__ exec --data-dir __DATA__ -- \
  "$PY" __REPO__/mcs/ingest/run_check.py "$@"
rc=$?
if [ "$rc" -eq 0 ]; then
  "$PY" -I __RUN_LOG_HELPER__ exec --data-dir __DATA__ -- \
    "$PY" __REPO__/mcs/ops/mcs_ocr.py --run
  ocr_rc=$?
  if [ "$ocr_rc" -ne 0 ]; then
    printf 'mcs deep: attachment OCR exited %d — see %s\n' "$ocr_rc" "$LOG"
    exit "$ocr_rc"
  fi
fi
if [ "$rc" -ne 0 ] && [ "$rc" -ne 2 ]; then
  printf 'mcs deep: run_check exited %d — see %s\n' "$rc" "$LOG"
fi
exit "$rc"
