#!/bin/bash
# MCS v3 release owner tick — scheduler boundary.
# exec-only: the script replaces itself with the updater process so a
# `services` re-render mid-run can never corrupt a partially-read script file.
# The bounded fleet jitter lives in the Python command, after exec.
set -u
PATH="$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH
exec __PYTHON__ -I __RUN_LOG_HELPER__ exec --data-dir __DATA__ -- \
  __PYTHON__ __REPO__/mcs/ops/mcs_update.py auto-tick --jitter 120
