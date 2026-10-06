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
from contextlib import closing, suppress

from ledger import publish_snapshot as _publish_snapshot, valid_mcs_db
from mcs_util import HOME, atomic_write, disk_floor_mb, publish_tmp

BACKUP_DIR = os.path.join(HOME, "data", "backups")
SNAPSHOT_DIR = os.path.join(HOME, "data", "snapshots")
LOGFILE = os.path.join(HOME, "data", "run.log")
BACKUP_KEEP = 7
BACKUP_OVERDUE_S = 2 * 86400
LOG_MAX = 5 * 1024 * 1024
ATTACHMENT_KEEP_S = 14 * 86400
PRUNE_BATCH = 100
LEFTOVER_KEEP_S = 86400
# an apply publishes its preupdate backup, then waits up to 20min for the
# run lock before recording backup_path in state; prune must not race it
PREUPDATE_GRACE_S = 86400


class MaintenanceError(RuntimeError):
    """Housekeeping failure carrying a short stable reason token
    (e.g. "backup_verify_failed"). Distinct from the adapter's MCSError
    — core must not depend on the ingest layer for an error type."""


def atomic_publish_text(path: str, text: str) -> None:
    """tmp -> fsync -> os.replace -> dir fsync: consumers either see the
    whole previous file or the whole new one — a mid-write crash never
    leaves a torn JSON behind (same contract as the backup chain)."""
    atomic_write(path, lambda f: f.write(text), tmp_prefix=".pub.")


def _stat_sig(path: str) -> str | None:
    """size + mtime_ns signature of ``path``; None when unreadable."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return f"{st.st_size} {st.st_mtime_ns}"


def _verified_unchanged(dest: str) -> bool:
    """True when ``dest`` still matches the signature recorded right
    after it was verified and published — lets a run skip re-running
    quick_check over an unchanged same-day backup."""
    try:
        with open(dest + ".ok", encoding="utf-8") as f:
            marker = f.read().strip()
    except (OSError, ValueError):
        return False
    return bool(marker) and marker == _stat_sig(dest)


def _publish_backup(db_path: str, tmp: str, dest: str) -> None:
    """Copy, verify and publish a backup while owning its staging file."""
    try:
        with closing(sqlite3.connect(
                Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(tmp)) as dst:
                src.backup(dst)
                dst.execute("PRAGMA journal_mode=DELETE")
        if not valid_mcs_db(tmp):
            raise MaintenanceError("backup_verify_failed")
        publish_tmp(tmp, dest, mode=0o600)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise


def _backup_need_mb(db_path: str) -> float:
    """Room for two ledger copies above the disk floor."""
    return os.path.getsize(db_path) * 2 / (1024 * 1024) + disk_floor_mb()


def daily_backup(db_path: str):
    """One VERIFIED sqlite .backup per day — write to tmp, schema/quick_check,
    then atomic publish. A present-but-broken file must never block a
    fresh backup (Oracle B24). Returns "skipped_disk_low" without
    writing when the volume lacks room for two ledger copies above the
    disk floor — a backup must not be what fills the disk."""
    os.makedirs(BACKUP_DIR, mode=0o700, exist_ok=True)
    os.chmod(BACKUP_DIR, 0o700)   # PHI store: never umask-loose
    stamp = time.strftime("%Y%m%d")
    dest = os.path.join(BACKUP_DIR, f"ledger-{stamp}.db")
    # the .ok marker (size+mtime_ns at publish time) skips the full
    # quick_check while the verified file is untouched; any change or a
    # missing marker falls back to validation (B24)
    if _verified_unchanged(dest) or valid_mcs_db(dest):
        return
    if shutil.disk_usage(BACKUP_DIR).free / (1024 * 1024) < _backup_need_mb(db_path):
        return "skipped_disk_low"
    tmp = dest + ".tmp"
    with suppress(FileNotFoundError):
        os.unlink(tmp)
    _publish_backup(db_path, tmp, dest)
    sig = _stat_sig(dest)
    if sig:
        with suppress(OSError):   # marker is only a fast path
            atomic_write(dest + ".ok", lambda f: f.write(sig), mode=0o600)
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, "ledger-*.db")))
    for old in files[:-BACKUP_KEEP]:
        for path in (old, old + ".ok"):
            with suppress(OSError):
                os.unlink(path)
    prune_preupdate_backups()


def backup_overdue() -> bool:
    """True when the newest daily backup is older than BACKUP_OVERDUE_S.
    No backup yet is not overdue: a fresh install gets its first one on
    the next tick that has budget left."""
    stamps = []
    for path in glob.glob(os.path.join(BACKUP_DIR, "ledger-*.db")):
        with suppress(OSError):   # rotated away mid-scan
            stamps.append(os.path.getmtime(path))
    return bool(stamps) and time.time() - max(stamps) > BACKUP_OVERDUE_S


def preupdate_backup(db_path: str) -> str:
    """Verified .backup before an apply — distinct 'preupdate-' prefix
    so the daily ledger-* rotation can never evict a rollback point
    (B3). Returns the published path."""
    os.makedirs(BACKUP_DIR, mode=0o700, exist_ok=True)
    os.chmod(BACKUP_DIR, 0o700)
    # same room check as daily_backup: apply bails on this before quiesce
    if shutil.disk_usage(BACKUP_DIR).free / (1024 * 1024) < _backup_need_mb(db_path):
        raise MaintenanceError("backup_disk_low")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    fd, tmp = tempfile.mkstemp(
        prefix=f"preupdate-{stamp}-", suffix=".db.tmp", dir=BACKUP_DIR)
    os.close(fd)
    dest = tmp[:-4]
    _publish_backup(db_path, tmp, dest)
    return dest


def prune_preupdate_backups(state_path: str | None = None) -> int:
    """Delete only preupdate-* backups NOT referenced by an actionable
    record — reference-based retention (B3): a flapping fetch loop can
    never push out the backup a rollback still needs. Actionable means
    the in-flight `applying` journal, a pending `restore_consent` hold,
    and the newest schema_bump `applied` entry (rollback restores the DB
    only for schema_bump entries; applied[] only grows, so referencing
    every entry kept a full ledger copy per update forever)."""
    import json
    state_path = state_path or os.path.join(
        HOME, "data", "update_state.json")
    referenced: set[str] = set()
    try:
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict) or not isinstance(state.get("applied"), list):
            return 0
        applied = list(state["applied"])
        held = [state[k] for k in ("applying", "restore_consent")
                if state.get(k) is not None]
        for rec in applied + held:
            if not isinstance(rec, dict):
                return 0
            bp = rec.get("backup_path")
            if bp is not None and (not isinstance(bp, str) or not bp.strip()):
                return 0
        newest_bump = next(
            (r for r in reversed(applied) if r.get("schema_bump")), None)
        # ponytail: only the newest bump is kept — rolling back across two
        # schema bumps needs the older copy too; keep all bumps if that matters
        for rec in held + [newest_bump or {}]:
            if isinstance(rec.get("backup_path"), str):
                referenced.add(os.path.abspath(rec["backup_path"]))
    except (OSError, ValueError, TypeError, RecursionError):
        # unreadable state => keep EVERYTHING (fail-safe, S17)
        return 0
    removed = 0
    for path in glob.glob(os.path.join(BACKUP_DIR, "preupdate-*.db")):
        if os.path.abspath(path) in referenced:
            continue
        with suppress(OSError):
            # fresh and not yet recorded: an apply may still be waiting
            # for the run lock before it writes backup_path to state
            if time.time() - os.path.getmtime(path) < PREUPDATE_GRACE_S:
                continue
        with suppress(OSError):
            os.unlink(path)
            removed += 1
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
                                       "extract_drain_2.log"),
                          os.path.join(HOME, "data", "extract_llm.log"),
                          os.path.join(HOME, "data",
                                       "semantic_drain.log"),
                          # runtime_mode=standalone connector / launchd jobs
                          os.path.join(HOME, "data", "standalone.log"),
                          os.path.join(HOME, "data", "cron.log"),
                          os.path.join(HOME, "data", "llamacpp-restart.log"),
                          # mcs_update.sh / org.mcs.recovery / local.mcs-int
                          os.path.join(HOME, "data", "update.log"),
                          os.path.join(HOME, "data", "recovery.log"),
                          os.path.join(HOME, "data", "cmd_int.log"),
                          # ai.mcs.llamaserver plist (__HERMES_HOME__/logs)
                          os.path.join(os.path.expanduser("~/.hermes"),
                                       "logs", "llamacpp.log")):
        with suppress(OSError):
            if os.path.getsize(path) > LOG_MAX:
                shutil.copyfile(path, path + ".1")
                with open(path, "w"):
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
    number of payloads whose cleanup completed. Missing payloads are
    retired without counting a deletion; failed cleanup keeps the path
    on a non-sendable row so the next run can retry."""
    cutoff = time.time() - ATTACHMENT_KEEP_S
    con = sqlite3.connect(db_path, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout=30000")
        keep_mids = {r[0] for r in con.execute("""
          SELECT DISTINCT value FROM notify_outbox,
            json_each(notify_outbox.payload, '$.message_ids')
          WHERE kind='new_messages' AND state IN ('pending','failed')
            AND json_valid(payload)""")}
        keep_ids = {r[0] for r in con.execute("""
          SELECT json_extract(payload,'$.attachment_id') FROM notify_outbox
          WHERE kind='attachment_followup' AND state IN ('pending','failed')
            AND json_valid(payload)""")}
        # An accepted card can still have unsent or uncertain companion
        # files. Their durable part journal owns those payloads until settled.
        keep_ids.update(r[0] for r in con.execute("""
          SELECT p.attachment_id FROM notification_render_parts p
          LEFT JOIN notification_renders r USING(delivery_id)
          WHERE p.kind='attachment_part' AND p.state IN ('pending','unknown','held')
            AND r.state IS NOT 'cancelled' AND p.attachment_id IS NOT NULL
        """))
        rows = [a for a in con.execute("""
          SELECT attachment_id, message_id, name, local_path
          FROM attachments
          WHERE state IN ('downloaded','withdrawn','pruned')
            AND local_path IS NOT NULL AND downloaded_at < ?""",
          (cutoff,)).fetchall()
          if a["message_id"] not in keep_mids
          and a["attachment_id"] not in keep_ids]
        pruned = 0
        failed = False
        root = Path(db_path).resolve().parent / "attachments"
        # short write transactions; files go only after the row stopped
        # advertising a sendable payload. Keep the cleanup path until
        # every unlink succeeds, including across a process crash.
        for i in range(0, len(rows), PRUNE_BATCH):
            batch = []
            for a in rows[i:i + PRUNE_BATCH]:
                cur = con.execute("""
                  UPDATE attachments SET
                    state=CASE WHEN state='withdrawn' THEN 'withdrawn' ELSE 'pruned' END
                  WHERE attachment_id=? AND local_path=?""",
                  (a["attachment_id"], a["local_path"]))
                if cur.rowcount:
                    batch.append(a)
            con.commit()
            for a in batch:
                try:
                    removed = _unlink_attachment(a["local_path"], root)
                except _ForeignPayload:
                    # Not ours to delete: retire the pointer without
                    # touching the file, and report it once — a retry
                    # could never succeed and would fail every run.
                    failed, removed = True, False
                except OSError:
                    failed = True
                    continue
                con.execute("UPDATE attachments SET local_path=NULL "
                            "WHERE attachment_id=? AND local_path=? "
                            "AND state IN ('pruned','withdrawn')",
                            (a["attachment_id"], a["local_path"]))
                con.commit()
                pruned += int(removed)
        if failed:
            raise MaintenanceError("attachment_prune_failed")
        return pruned
    finally:
        con.close()


class _ForeignPayload(OSError):
    """A stored path that is outside, or not a plain file in, the store."""


def _unlink_attachment(path: str, root: Path) -> bool:
    """Remove only owned payloads/aliases; keep the raw file until last."""
    payload = Path(path)
    if root.is_symlink():
        raise OSError("attachment_prune_root_invalid")   # retried: config fault
    if (not payload.is_absolute() or payload.parent.resolve() != root
            or payload.is_symlink() or payload.is_dir()):
        raise _ForeignPayload("attachment_prune_path_invalid")
    aliases = []
    if not os.path.splitext(path)[1]:
        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    if not entry.name.startswith(payload.name + "."):
                        continue
                    ext = entry.name[len(payload.name):]
                    if 1 < len(ext) <= 9 and ext[1:].isascii() and ext[1:].isalnum():
                        if entry.is_symlink():
                            raise OSError("attachment_prune_path_invalid")
                        if entry.is_file(follow_symlinks=False):
                            aliases.append(entry.path)
        except FileNotFoundError:
            pass
    removed = False
    for candidate in [*aliases, path]:
        try:
            os.unlink(candidate)
            removed = True
        except FileNotFoundError:
            pass
    return removed


def prune_leftovers() -> int:
    """Delete day-old download temp files (a hard kill skips the
    adapter's own cleanup) and quarantined cmd/ and cmd_int/
    *.json.invalid files. ctime: a quarantine rename keeps the original
    mtime."""
    cutoff = time.time() - LEFTOVER_KEEP_S
    removed = 0
    for pattern in (os.path.join(HOME, "data", "attachments", ".download-*.part"),
                    os.path.join(HOME, "data", "cmd", "*.json.invalid"),
                    os.path.join(HOME, "data", "cmd_int", "*.json.invalid")):
        for path in glob.glob(pattern):
            with suppress(OSError):
                if os.stat(path).st_ctime < cutoff:
                    os.unlink(path)
                    removed += 1
    return removed


def publish_snapshot(db_path: str) -> bool:
    """Regenerate the read-only snapshot consumed by the sandboxed CCO
    (backup -> tmp -> DELETE journal -> verify -> atomic rename)."""
    return bool(_publish_snapshot(db_path, SNAPSHOT_DIR))
