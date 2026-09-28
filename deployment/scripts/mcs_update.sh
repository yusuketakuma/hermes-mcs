#!/bin/bash
# MCS update check — hermes cron job (daily).
# exec-only: the script replaces itself with the updater process so a
# `services` re-render mid-run can never corrupt a partially-read
# script file (F17). Recovery when the updater itself is broken is the
# independent launchd watchdog org.mcs.recovery's job — deliberately no
# fallback here (a post-exec fallback is unreachable by definition).
set -u
PATH="$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH
exec __PYTHON__ __REPO__/mcs/ops/mcs_update.py check \
  >>__DATA__/update.log 2>&1
