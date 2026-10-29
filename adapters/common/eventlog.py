"""Shared adapter event logger that writes one allowlisted JSON object per line.

Adapters log fixed event codes, never message bodies, names, user or
channel IDs, exception strings, paths or credentials. The filter here is
the backstop: unknown field names are dropped, strings outside a short
code alphabet become ``"redacted"`` and ``None`` values are omitted.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import re

ALLOWED_FIELDS = frozenset({
    "error", "reason", "action", "outcome", "result", "command_id",
    "delivery_id", "rows", "receipt_republished", "not_sent", "unknown",
    "job", "code", "failures",
    # standalone runtime supervision: fixed codes and counts, no content
    "file", "retry_in", "state", "age_s", "slot", "stopping", "adopted",
})
_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,80}")
REDACTED = "redacted"


def _value(value):
    if isinstance(value, bool | int):
        return value
    if isinstance(value, str) and _CODE.fullmatch(value):
        return value
    return REDACTED


def render(event, fields) -> str:
    """Return the JSON line for one event after the allowlist and redaction."""
    record = {"event": _value(event) if isinstance(event, str) else REDACTED}
    for key in sorted(k for k in fields if k in ALLOWED_FIELDS):
        if fields[key] is not None:
            record[key] = _value(fields[key])
    return json.dumps(record, ensure_ascii=True)


def make_logger(name: str, level: int = logging.INFO):
    """Return ``log(event, **fields)`` writing filtered JSON lines to ``name``."""
    logger = logging.getLogger(name)

    def log(event, **fields) -> None:
        record = render(event, fields)
        logger.log(level, "%s", record)
        from .mcs_api import bootstrap
        bootstrap()
        import run_log
        if not run_log.is_captured():
            from mcs_util import HOME
            try:
                run_log.append(os.path.join(HOME, "data"), (record + "\n").encode("utf-8"))
            except (OSError, run_log.RunLogError):
                # Preserve the operation and existing logger; expose only a fixed code.
                print("mcs_log_write_failed", file=sys.stderr)
    return log
