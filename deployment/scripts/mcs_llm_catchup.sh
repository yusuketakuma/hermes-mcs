#!/bin/bash
# Bounded retry maintenance; resident workers process both queues 24/7.
set -u
PATH="$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH
PY=__PYTHON__
if [ -f __DATA__/update_in_progress.marker ]; then
  exit 0
fi
"$PY" -I __RUN_LOG_HELPER__ exec --data-dir __DATA__ -- \
  "$PY" __REPO__/mcs/extract/llm/extract_llm.py --revive-failed
rc=$?
"$PY" -I __RUN_LOG_HELPER__ exec --data-dir __DATA__ -- \
  "$PY" __REPO__/mcs/semantic/semantic_drain.py --revive-failed
sem_rc=$?
if [ "$rc" -ne 0 ] || [ "$sem_rc" -ne 0 ]; then
  printf 'mcs retry maintenance: extract=%d semantic=%d\n' "$rc" "$sem_rc"
  exit 1
fi
