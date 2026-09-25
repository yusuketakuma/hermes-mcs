#!/usr/bin/env python3
"""MCS housekeeping — backup, log rotation, read-only snapshot publish.

Split from run_check orchestration: these run at the end of every tick
and must each be independently failure-isolated by the caller.
"""
import glob
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from ledger import publish_snapshot as _publish_snapshot, valid_mcs_db

HOME = os.path.expanduser("~/.mcs")
BACKUP_DIR = os.path.join(HOME, "data", "backups")
SNAPSHOT_DIR = os.path.join(HOME, "data", "snapshots")
LOGFILE = os.path.join(HOME, "data", "run.log")
BACKUP_KEEP = 7
LOG_MAX = 5 * 1024 * 1024
ATTACHMENT_KEEP_S = 14 * 86400


class MaintenanceError(RuntimeError):
    """Housekeeping failure carrying a short stable reason token
    (e.g. "backup_verify_failed"). Distinct from the adapter's MCSError
    — core must not depend on the ingest layer for an error type."""


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
        raise MaintenanceError("backup_verify_failed")
    os.replace(tmp, dest)
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, "ledger-*.db")))
    for old in files[:-BACKUP_KEEP]:
        try:
            os.unlink(old)
        except OSError:
            pass
    prune_preupdate_backups()


def preupdate_backup(db_path: str) -> str:
    """Verified .backup before an apply — distinct 'preupdate-' prefix
    so the daily ledger-* rotation can never evict a rollback point
    (B3). Returns the published path."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    fd, tmp = tempfile.mkstemp(
        prefix=f"preupdate-{stamp}-", suffix=".db.tmp", dir=BACKUP_DIR)
    os.close(fd)
    dest = tmp[:-4]
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
        raise MaintenanceError("backup_verify_failed")
    os.replace(tmp, dest)
    return dest


def prune_preupdate_backups(state_path: str | None = None) -> int:
    """Delete only preupdate-* backups NOT referenced by an actionable
    applying/applied record — reference-based retention (B3): a flapping
    fetch loop can never push out the backup a rollback still needs."""
    import json
    state_path = state_path or os.path.join(
        HOME, "data", "update_state.json")
    referenced: set[str] = set()
    try:
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
        records = list(state.get("applied") or [])
        if state.get("applying"):
            records.append(state["applying"])
        for rec in records:
            bp = rec.get("backup_path") if isinstance(rec, dict) else None
            if isinstance(bp, str):
                referenced.add(os.path.abspath(bp))
    except (OSError, json.JSONDecodeError, TypeError):
        # unreadable state => keep EVERYTHING (fail-safe, S17)
        return 0
    removed = 0
    for path in glob.glob(os.path.join(BACKUP_DIR, "preupdate-*.db")):
        if os.path.abspath(path) in referenced:
            continue
        try:
            os.unlink(path)
            removed += 1
        except OSError:
            pass
    return removed


def rotate_log(paths=None):
    """Keep the run/drain logs bounded: >5MB -> <name>.1 (single
    generation). The drainer logs are held open by resident launchd
    writers, so rename-rotation would strand them on the old inode —
    copy the content aside then truncate the live file in place
    (copytruncate): the writer's O_APPEND fd resumes at offset 0 (F21)."""
    for path in paths or (LOGFILE,
                          os.path.join(HOME, "data", "extract_drain.log"),
                          os.path.join(HOME, "data",
                                       "extract_drain_rt.log"),
                          os.path.join(HOME, "data", "extract_llm.log"),
                          os.path.join(HOME, "data",
                                       "semantic_drain.log")):
        try:
            if os.path.getsize(path) > LOG_MAX:
                shutil.copyfile(path, path + ".1")
                with open(path, "w"):
                    pass
        except OSError:
            pass


def prune_attachments(db_path: str) -> int:
    """Delete downloaded attachment payloads older than 14 days —
    retention-bound the unbounded attachments/ dir (10GB in the first
    week). The row survives as state='pruned' with name/bytes/sha256/url
    intact so views still record that the attachment existed; only the
    payload goes. 'pending'/'failed' rows are untouched — a failed
    download keeps its retry path and never ages out mid-retry.

    F12: notify_flush aliases each file to `<path><ext>` for upload —
    the alias is part of the same asset and is unlinked too. Files
    still referenced by an unsent notification are kept. Returns the
    number of payloads actually deleted."""
    cutoff = time.time() - ATTACHMENT_KEEP_S
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        keep_mids = {r[0] for r in con.execute("""
          SELECT DISTINCT value FROM notify_outbox,
            json_each(notify_outbox.payload, '$.message_ids')
          WHERE kind='new_messages' AND state IN ('pending','failed')
            AND json_valid(payload)""")}
        keep_ids = {r[0] for r in con.execute("""
          SELECT json_extract(payload,'$.attachment_id') FROM notify_outbox
          WHERE kind='attachment_followup' AND state IN ('pending','failed')
            AND json_valid(payload)""")}
        rows = con.execute("""
          SELECT attachment_id, message_id, name, local_path
          FROM attachments
          WHERE state IN ('downloaded','withdrawn')
            AND local_path IS NOT NULL AND downloaded_at < ?""",
          (cutoff,)).fetchall()
        pruned = 0
        for a in rows:
            if a["message_id"] in keep_mids or a["attachment_id"] in keep_ids:
                continue
            path = a["local_path"]
            try:
                if path and os.path.isfile(path):
                    os.unlink(path)
                # notify_flush._media_path hardlink/copy alias — same asset
                if path and not os.path.splitext(path)[1]:
                    for alias in glob.glob(glob.escape(path) + ".*"):
                        ext = alias[len(path):]
                        if (1 < len(ext) <= 9 and ext[1:].isascii()
                                and ext[1:].isalnum()
                                and os.path.isfile(alias)):
                            os.unlink(alias)
            except OSError:
                continue   # unlink failed — retry next tick
            con.execute("""
              UPDATE attachments SET
                state=CASE WHEN state='withdrawn' THEN 'withdrawn' ELSE 'pruned' END,
                local_path=NULL
              WHERE attachment_id=?""", (a["attachment_id"],))
            pruned += 1
        con.commit()
        return pruned
    finally:
        con.close()


def publish_snapshot(db_path: str) -> bool:
    """Regenerate the read-only snapshot consumed by the sandboxed CCO
    (backup -> tmp -> DELETE journal -> verify -> atomic rename)."""
    return bool(_publish_snapshot(db_path, SNAPSHOT_DIR))
