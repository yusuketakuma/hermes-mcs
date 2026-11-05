#!/bin/bash
# Saved attachments drain independently of collection and authentication.
set -u
PATH="$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH
exec __PYTHON__ -I __RUN_LOG_HELPER__ exec --data-dir __DATA__ -- \
  __PYTHON__ __REPO__/mcs/ops/mcs_ocr.py --run
