#!/bin/bash
# MCS semantic/QC backlog catch-up — hermes cron job (nightly window).
#
# extract_llm backlog is covered 24/7 by the two launchd drainers
# (shard 0/2 on slot 0, shard 1/2 polite-lending slot 1), so this
# window now drains the DOWNSTREAM queue: extract_qc + semantic jobs
# via semantic_drain --drain, with calls pinned to slot 1
# (MCS_LLM_SLOT=1) — safe inside this dead-of-night window; a rare RT
# call shares the slot queue. The drain loop takes the run lock only
# per ~2-min iteration, so a 15-min tick is never starved.
# Window 22:30→03:30 keeps the queue quiet before the 04:00
# idle-guarded llama restart. stdout stays silent on success
# (watchdog convention); batch lines go to semantic_drain.log.
set -u
PATH="$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH
PY=__PYTHON__

# quiesce guard: while an update owns the process lifecycle this
# launcher must never respawn drainers (S8)
if [ -f __DATA__/update_in_progress.marker ]; then
  exit 0
fi
DRAIN=__REPO__/mcs/semantic/semantic_drain.py
LOG=__DATA__/semantic_drain.log
# Hermes cron のスクリプトタイムアウト既定は
# cron.script_timeout_seconds=3600s。drain の業務上限は内部で 14h だが、
# cron 配下で走るこのランチャは timeout 未満に収めないと毎回 kill される。
WINDOW_S=3300

# gap-fill: if the extract drainer died, cover shard 0/2 on slot 0 too
if ! pgrep -f "extract_llm.py --all" >/dev/null 2>&1; then
  "$PY" __REPO__/mcs/extract/v4/extract_llm.py --all --workers 1 \
    --shard 0/2 --slot 0 --stop-after "$WINDOW_S" \
    >>__DATA__/extract_drain.log 2>&1 &
fi

MCS_LLM_SLOT=1 "$PY" "$DRAIN" --drain \
  --stop-after "$WINDOW_S" >>"$LOG" 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then
  printf 'mcs llm catch-up: semantic_drain exited %d — see %s\n' "$rc" "$LOG"
fi
exit "$rc"
