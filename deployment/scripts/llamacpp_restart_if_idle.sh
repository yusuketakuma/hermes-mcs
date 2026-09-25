#!/bin/bash
# 04:00 daily llama-server restart.
#
# Waits for a moment with no slot mid-request so in-flight MCS drains /
# gbrain nightly calls are not killed. The 24/7 extract drainers mean
# the server is rarely idle for long, but is_processing flips false in
# the ~1s gap between drainer calls — poll for up to 15 min for one of
# those gaps, then restart anyway: a killed call only costs one retry
# (drainers re-attempt), while never restarting lets memory fragment
# for weeks. The /slots key is "is_processing" (was mis-read as
# "processing" once — that variant always saw 0 busy and restarted
# mid-request every day).
set -u
LOG="$HOME/.hermes/logs/llamacpp-restart.log"
mkdir -p "$(dirname "$LOG")"
ts() { date -u +%FT%TZ; }

busy_slots() {
  /usr/bin/curl -s -m 5 http://127.0.0.1:8080/slots 2>/dev/null \
    | /usr/bin/python3 -c "import json,sys; print(sum(1 for s in json.load(sys.stdin) if s.get('is_processing')))" 2>/dev/null
}

deadline=$(( $(date +%s) + 900 ))   # wait up to 15 min for an idle gap
while :; do
  busy=$(busy_slots)
  if [ "$busy" = "" ]; then
    echo "$(ts) slots unreachable — restarting anyway" >> "$LOG"
    break
  fi
  if [ "$busy" -eq 0 ]; then
    echo "$(ts) idle — restarting" >> "$LOG"
    break
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "$(ts) still busy after 15m ($busy slot(s)) — restarting anyway" >> "$LOG"
    break
  fi
  sleep 10
done

# kickstart target: the loaded label differs per machine. Hermes-managed
# installs run ai.hermes.llamacpp; the repo template installs as
# ai.mcs.llamaserver (install.sh fallback). Whichever is loaded wins.
LABEL=""
for cand in ai.hermes.llamacpp ai.mcs.llamaserver; do
  if /bin/launchctl print "gui/$(id -u)/$cand" >/dev/null 2>&1; then
    LABEL="$cand"
    break
  fi
done
if [ -z "$LABEL" ]; then
  echo "$(ts) no llamacpp launchd agent loaded — restart skipped" >> "$LOG"
  printf 'llamacpp restart: no launchd agent loaded (ai.hermes.llamacpp / ai.mcs.llamaserver)\n'
  exit 1
fi
/bin/launchctl kickstart -k "gui/$(id -u)/$LABEL"
echo "$(ts) restarted $LABEL (kickstart -k)" >> "$LOG"
