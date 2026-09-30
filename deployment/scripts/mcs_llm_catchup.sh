#!/bin/bash
# Bounded retry maintenance; resident workers process both queues 24/7.
set -u
PATH="$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH
PY=__PYTHON__
if [ -f __DATA__/update_in_progress.marker ]; then
  exit 0
fi
"$PY" __REPO__/mcs/extract/v4/extract_llm.py --revive-failed >>__DATA__/extract_drain.log 2>&1
rc=$?
"$PY" __REPO__/mcs/semantic/semantic_drain.py --revive-failed >>__DATA__/semantic_drain.log 2>&1
sem_rc=$?
if [ "$rc" -ne 0 ] || [ "$sem_rc" -ne 0 ]; then
  printf 'mcs retry maintenance: extract=%d semantic=%d\n' "$rc" "$sem_rc"
  exit 1
fi
