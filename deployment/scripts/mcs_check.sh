#!/bin/bash
# MCS unread check — hermes cron job (replaces local.mcs-check.plist).
# Full run output appends to the MCS run log; stdout carries an alert line
# only on hard failure (empty stdout = silent, watchdog convention).
# Exit 2 = session expired: the MCS notifier already alerts via
# notify_system_target (deduped) — silent here to avoid 15-min spam.
set -u
PATH="$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH
LOG=__DATA__/run.log
PY=__PYTHON__

export MCS_LLM_SLOT=1

"$PY" __REPO__/mcs/ingest/run_check.py --json --download-files --mark-read >>"$LOG" 2>&1
rc=$?
if [ "$rc" -ne 0 ] && [ "$rc" -ne 2 ]; then
  printf 'mcs check: run_check exited %d — see %s\n' "$rc" "$LOG"
elif [ "$rc" -eq 0 ]; then
  # F20: a partial run exits 0 — the per-subsystem state lives in
  # health.json. Surface the one state that risks silent data loss
  # (collection incomplete); capacity lag stays in health.json for
  # the monitor rather than alerting every tick.
  "$PY" - __DATA__/health.json <<'PYEOF' || true
import json, sys
try:
    h = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
if h.get("collection") == "incomplete":
    # name the cause: unread-fetch gaps and/or stalled history_head
    # backfills; an empty list is never printed
    why = [label + ",".join(str(p) for p in pids)
           for label, pids in (("projects ", h.get("incomplete_projects")),
                               ("stalled=", h.get("coverage_stalled")))
           if pids]
    print("mcs check: collection incomplete — " + " ".join(why))
PYEOF
fi
exit "$rc"
