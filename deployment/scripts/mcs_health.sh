#!/bin/bash
# MCS health watcher — independent supervised check that health.json
# keeps arriving fresh. Same output contract as mcs_check.sh: stdout
# carries one alert line on a non-OK transition, silent otherwise.
set -u
PATH="$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH
PY=__PYTHON__

"$PY" __REPO__/mcs/ingest/health_watch.py
