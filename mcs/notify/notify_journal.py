"""Read durable delivery evidence using the worker's journal parsing contract."""
from __future__ import annotations

import os
from adapters.common.journal import PHASES, parse_record
from notify_scope import SUPPORTED_TRANSPORTS


def _journal_dirs(dirs: dict) -> list:
    return [dirs[t + "_state"] for t in SUPPORTED_TRANSPORTS]


def scan_journals(dirs: dict) -> tuple[dict, bool]:
    """attempt_id -> {"rows": [...], "tainted": bool}, across every
    per-worker journal file in both transport state dirs.

    A file with an unparsable non-empty line is tainted: a torn tail
    is survivable, but a corrupt middle line could hide a 'started' or
    'result' row — absence-of-evidence verdicts are untrustworthy for
    any attempt recorded in it.
    """
    out: dict[str, dict] = {}
    incomplete = False
    for state_dir in _journal_dirs(dirs):
        names = sorted(n for n in os.listdir(state_dir)
                       if n.startswith("journal-") and n.endswith(".jsonl"))
        for name in names:
            file_rows: dict[str, list] = {}
            tainted = False
            # an OSError propagates: an inaccessible journal can contain an
            # effect lost by the restored DB, so the global restore hold stays
            with open(os.path.join(state_dir, name), "rb") as fh:
                for raw in fh:
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
    return out, incomplete
