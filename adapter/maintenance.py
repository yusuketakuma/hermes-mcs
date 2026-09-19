#!/usr/bin/env python3
"""MCS housekeeping — backup, log rotation, read-only snapshot publish.

Split from run_check orchestration: these run at the end of every tick
and must each be independently failure-isolated by the caller.
"""
import glob
import os
import sqlite3
import time
from pathlib import Path

from ledger import publish_snapshot as _publish_snapshot, valid_mcs_db
from mcs_adapter import MCSError

HOME = os.path.expanduser("~/.mcs")
BACKUP_DIR = os.path.join(HOME, "data", "backups")
SNAPSHOT_DIR = os.path.join(HOME, "data", "snapshots")
LOGFILE = os.path.join(HOME, "data", "run.log")
BACKUP_KEEP = 7
LOG_MAX = 5 * 1024 * 1024


def daily_backup(db_path: str):
    """One VERIFIED sqlite .backup per day — write to tmp, schema/quick_check,
    then atomic publish. A present-but-broken file must never block a
    fresh backup (Oracle B24)."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d")
    dest = os.path.join(BACKUP_DIR, f"ledger-{stamp}.db")
    if valid_mcs_db(dest):
        return
    tmp = dest + ".tmp"
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    src = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro",
                          uri=True)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
    finally:
        dst.close()
        src.close()
    if not valid_mcs_db(tmp):
        os.unlink(tmp)
        raise MCSError("backup_verify_failed")
    os.replace(tmp, dest)
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, "ledger-*.db")))
    for old in files[:-BACKUP_KEEP]:
        try:
            os.unlink(old)
        except OSError:
            pass


def rotate_log():
    """Keep run.log bounded: >5MB -> run.log.1 (single generation)."""
    try:
        if os.path.getsize(LOGFILE) > LOG_MAX:
            os.replace(LOGFILE, LOGFILE + ".1")
    except OSError:
        pass


def publish_snapshot(db_path: str) -> bool:
    """Regenerate the read-only snapshot consumed by the sandboxed CCO
    (backup -> tmp -> DELETE journal -> verify -> atomic rename)."""
    return bool(_publish_snapshot(db_path, SNAPSHOT_DIR))
