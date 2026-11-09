#!/bin/bash
# Disabled without an explicit rendered owner opt-in (or --enable) AND
# scheduled:true in the explicit private policy. Unrendered values stay off.
# Explicit static snapshot or daily snapshot directory: no tick, services or pruning.
set -u
umask 077
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH

ENABLED=__BACKUP_ENABLED__
POLICY=__BACKUP_POLICY__
SNAPSHOT=__BACKUP_SNAPSHOT__
SNAPSHOTS=__BACKUP_SNAPSHOT_DIR__
if [ "${1:-}" = "--enable" ]; then
  shift
elif [ "$ENABLED" = "1" ]; then
  if [ -n "$SNAPSHOT" ]; then
    set -- --policy "$POLICY" --snapshot "$SNAPSHOT" "$@"
  else
    set -- --policy "$POLICY" --snapshot-dir "$SNAPSHOTS" "$@"
  fi
else
  exit 0
fi

# _rendered_scripts supplies shell-quoted values: do not quote them twice.
DATA=__DATA__
PY=__PYTHON__
REPO=__REPO__
if [ -e "$DATA/update_in_progress.marker" ]; then
  exit 0
fi

"$PY" -I __RUN_LOG_HELPER__ exec --data-dir "$DATA" -- \
  "$PY" "$REPO/mcs/ops/mcs_backup.py" offsite --scheduled --keychain \
  --state-dir "$DATA" "$@"
rc=$?
if [ "$rc" -ne 0 ]; then
  printf 'mcs offsite: backup or recovery verification requires attention (exit %d)\n' "$rc"
fi
exit "$rc"
