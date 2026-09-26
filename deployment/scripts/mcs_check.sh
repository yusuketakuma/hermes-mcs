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

# Night thinning (2026-09, data-driven): the run log shows essentially
# no new posts outside 07-21 JST, so 22:00-06:59 polls at a 20-minute
# cadence — under the 30-minute session-expiry limit, keeping the bearer
# alive without needing auto_login overnight.
case "$(date +%H)" in
  22|23|00|01|02|03|04|05|06)
    case "$(date +%M)" in
      00|20|40) ;;
      *) exit 0 ;;
    esac ;;
esac

"$PY" __REPO__/mcs/ingest/run_check.py --json --download-files --mark-read >>"$LOG" 2>&1
rc=$?
if [ "$rc" -ne 0 ] && [ "$rc" -ne 2 ]; then
  printf 'mcs check: run_check exited %d — see %s\n' "$rc" "$LOG"
elif [ "$rc" -eq 0 ]; then
  # F20: a partial run exits 0 — the per-subsystem state lives in
  # health.json. Surface the one state that risks silent data loss
  # (collection incomplete); capacity lag stays in health.json for
  # the monitor rather than alerting every tick.
  "$PY" - "__DATA__/health.json" <<'PYEOF' || true
import json, sys
try:
    h = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
if h.get("collection") == "incomplete":
    print("mcs check: collection incomplete — projects "
          + ",".join(str(p) for p in h.get("incomplete_projects", [])))
PYEOF
fi
exit "$rc"
