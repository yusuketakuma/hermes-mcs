"""Read durable delivery evidence using the worker's journal parsing contract."""
from __future__ import annotations

import os
import math
import time
from notify_scope import SUPPORTED_TRANSPORTS

PHASES = ("claimed", "begin", "granted", "denied", "started", "result", "receipt")


class JournalReadLimit(ValueError):
    """A bounded observation cannot claim complete delivery evidence."""


def _journal_dirs(dirs: dict) -> list:
    return [dirs[t + "_state"] for t in SUPPORTED_TRANSPORTS]


def scan_journals(dirs: dict, *, deadline=None, max_bytes=None, max_rows=None) -> tuple[dict, bool]:
    """attempt_id -> {"rows": [...], "tainted": bool}, across every
    per-worker journal file in both transport state dirs.

    A file with an unparsable non-empty line is tainted: a torn tail
    is survivable, but a corrupt middle line could hide a 'started' or
    'result' row — absence-of-evidence verdicts are untrustworthy for
    any attempt recorded in it.
    """
    # Candidate bootstrap contains mcs/, not the transport adapters.
    from adapters.common.journal import parse_record

    if any(value is not None and (type(value) is not int or value <= 0)
           for value in (max_bytes, max_rows)) or (deadline is not None and (
               type(deadline) not in (int, float)
               or type(deadline) is float and not math.isfinite(deadline))):
        raise ValueError("bad_journal_budget")

    def check_deadline():
        if deadline is not None and time.monotonic() >= deadline:
            raise JournalReadLimit("journal_read_limit")

    out: dict[str, dict] = {}
    incomplete = False
    bytes_read = rows_read = 0
    for state_dir in _journal_dirs(dirs):
        check_deadline()
        names = sorted(n for n in os.listdir(state_dir)
                       if n.startswith("journal-") and n.endswith(".jsonl"))
        for name in names:
            check_deadline()
            file_rows: dict[str, list] = {}
            tainted = False
            # an OSError propagates: an inaccessible journal can contain an
            # effect lost by the restored DB, so the global restore hold stays
            with open(os.path.join(state_dir, name), "rb") as fh:
                while True:
                    check_deadline()
                    raw = fh.readline() if max_bytes is None else fh.readline(max_bytes - bytes_read + 1)
                    if not raw:
                        break
                    bytes_read += len(raw)
                    rows_read += 1
                    if (max_bytes is not None and bytes_read > max_bytes
                            or max_rows is not None and rows_read > max_rows):
                        raise JournalReadLimit("journal_read_limit")
                    terminated = raw.endswith(b"\n")
                    raw = raw.strip()
                    if not raw:
                        continue
                    if not terminated:
                        tainted = True
                    try:
                        row = parse_record(raw)
                    except (ValueError, RecursionError):
                        tainted = True
                        continue
                    if not isinstance(row, dict):
                        tainted = True
                        continue
                    if row.get("phase") not in PHASES:
                        tainted = True
                    aid = row.get("attempt_id")
                    if isinstance(aid, str) and aid:
                        file_rows.setdefault(aid, []).append(row)
                    else:
                        tainted = True
            incomplete = incomplete or tainted
            for aid, rows in file_rows.items():
                rec = out.setdefault(aid, {"rows": [], "tainted": False})
                rec["rows"].extend(rows)
                if tainted:
                    rec["tainted"] = True
    check_deadline()
    return out, incomplete
