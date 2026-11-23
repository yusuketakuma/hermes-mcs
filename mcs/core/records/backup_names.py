"""Owner classes of data/backups file names and the delivery-journal restore horizon they bound.

Names and stat only — nothing here opens a database or deletes a file.
The local backup rotation (``local_backup``) and the delivery workers'
journal compaction (``adapters/common/worker.py``) share this one reading,
and post-restore reconcile uses the same cap to decide when a restored
backup is older than the journal evidence kept for it.
"""
from __future__ import annotations

import os
import re

BACKUP_CLASSES = ("daily", "daily_ok", "preupdate", "patient_context_reset",
                  "homecare_daily", "homecare_daily_ok", "homecare_preupdate", "staging", "manual")
# Classes holding a whole copy of the ledger; the home-care records database is a separate file.
_LEDGER_COPIES = ("daily", "preupdate", "patient_context_reset", "manual")

_BACKUP_CLASS_PATTERNS = (("daily", re.compile(r"ledger-\d{8}\.db")),
                          ("daily_ok", re.compile(r"ledger-\d{8}\.db\.ok")),
                          ("preupdate", re.compile(r"preupdate-.*\.db")),
                          # reset_backup's copies: kept by the owner, never rotated
                          ("patient_context_reset", re.compile(r"patient-context-reset-.*\.db")),
                          # copies of the home-care records database (homecare.db)
                          ("homecare_daily", re.compile(r"homecare-\d{8}\.db")),
                          ("homecare_daily_ok", re.compile(r"homecare-\d{8}\.db\.ok")),
                          ("homecare_preupdate", re.compile(r"homecare-preupdate-.*\.db")),
                          ("staging", re.compile(r".*\.tmp(-journal|-wal|-shm)?")))

# Settled journal rows are kept back to the oldest restorable backup, but
# never longer than this: an owner-kept copy (reset, schema-bump preupdate,
# hand-made) must not stop compaction for good. A restore from a backup
# older than the cap cannot be reconciled from the journal and holds all.
JOURNAL_HORIZON_MAX_S = 90 * 86400.0


def backup_class(name: str) -> str:
    """The owner of one data/backups entry; unknown names are hand-made copies."""
    for owner, pattern in _BACKUP_CLASS_PATTERNS:
        if pattern.fullmatch(name):
            return owner
    return "manual"


def restorable(name: str) -> bool:
    """A whole ledger copy a restore could rewind to (no marker, staging or home-care file)."""
    return name.endswith(".db") and backup_class(name) in _LEDGER_COPIES


def journal_horizon(backup_dir: str, now: float) -> float | None:
    """mtime of the oldest restorable backup, capped at JOURNAL_HORIZON_MAX_S
    ago; the cap alone when there is none. None when the set is unreadable."""
    floor = now - JOURNAL_HORIZON_MAX_S
    try:
        names = [n for n in os.listdir(backup_dir) if restorable(n)]
    except FileNotFoundError:
        return floor
    except OSError:
        return None
    try:
        oldest = min((os.stat(os.path.join(backup_dir, n)).st_mtime for n in names),
                     default=floor)
    except OSError:
        return None
    return max(floor, oldest)


def predates_journal_horizon(backup_path, restored_at) -> bool:
    """True when a restored backup is older than the journal's capped horizon
    at restore time, so settled delivery evidence for it may be pruned. An
    unknown path or time cannot be dated and keeps the earlier reading."""
    if not isinstance(backup_path, str) or type(restored_at) not in (int, float):
        return False
    try:
        mtime = os.stat(backup_path).st_mtime
    except (OSError, ValueError):
        return False
    return mtime < restored_at - JOURNAL_HORIZON_MAX_S
