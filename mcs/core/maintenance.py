#!/usr/bin/env python3
"""MCS housekeeping — log rotation, pruning, read-only snapshot publish, DB upkeep.

Split from run_check orchestration: these run at the end of every tick
and must each be independently failure-isolated by the caller.
"""
import errno
import fcntl
import glob
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import stat
import sys
import time
from pathlib import Path
from contextlib import closing, suppress
from time import monotonic

import snapshots
from snapshots import publish_snapshot as _publish_snapshot
from mcs_util import HOME
from safe_io import atomic_write, fdopen_owned
from maintenance_errors import MaintenanceError
import local_backup

SNAPSHOT_DIR = os.path.join(HOME, "data", "snapshots")
LOGFILE = os.path.join(HOME, "data", "mcs.log")
LOG_MAX = 5 * 1024 * 1024
ATTACHMENT_KEEP_S = 14 * 86400
PRUNE_BATCH = 100
LEFTOVER_KEEP_S = 86400
# H4: the read-only snapshot is republished only when the ledger changed
# (cheap marker, see _content_signature) or the published copy is this old
SNAPSHOT_MAX_AGE_S = 3600
# RC4: a change that only appended rows, and a background snapshot_dirty
# flag (extraction refresh, OCR, card redraw), waits this long after the last
# publish (about every other 10-minute tick) instead of copying ~1 GB each
# tick; 0 restores publish-on-every-change. Urgent changes skip the wait.
SNAPSHOT_MIN_INTERVAL_S = 900
# marker keys a human acts on right away (schema, patient display fields,
# requests and command receipts): any move publishes without the wait
SNAPSHOT_URGENT_KEYS = frozenset({"", "patients@content", "requests@updated_at",
                                  "command_receipts"})
SNAPSHOT_SIG_NAME = ".publish-sig.json"
# bookkeeping every tick writes; alone it never warrants a full copy
SNAPSHOT_SIG_IGNORED = frozenset({"runs", "snapshot_meta"})
# M3 + retention: daily SQLite upkeep, weekly FTS merge
DB_MAINT_STATE = os.path.join(HOME, "data", "db_maintenance.json")
DB_MAINT_INTERVAL_S = 86400
FTS_OPTIMIZE_INTERVAL_S = 7 * 86400
DB_MAINT_MIN_S = 30.0        # time left needed to start the daily pass
FTS_OPTIMIZE_MIN_S = 90.0    # ... and the weekly FTS merge
RETENTION_S = 90 * 86400     # runs / read_marks / finished system alerts
DRAIN_RUN_KEEP_S = 30 * 86400   # semantic_observe aggregates 14 days
CLOUD_USAGE_KEEP_S = 62 * 86400
USAGE_KEEP_S = 7 * 86400        # jev_usage_today reads the JST day only
RETENTION_BATCH = 5000
# finished system alerts only: every other outbox kind doubles as dedupe
# or history evidence (semantic_notice delivery keys, signal cooldowns,
# urgent daily caps, digest supersession, attachment followups)
RETAINED_ALERT_KINDS = ("session_expired", "session_recovered", "run_failed",
                        "health_write_failed", "health_watch_stalled")
# held sends older than this that still pin expired attachments are
# surfaced in health (the files are kept: a release may still send them)
HELD_PIN_WARN_S = 30 * 86400


def rotate_log(paths=None):
    """Rotate only the shared MCS runtime log during ordinary maintenance.

    Explicit old-utility paths retain the former >5MB -> <name>.1 contract:
    generation). The drainer logs are held open by resident launchd
    writers, so rename-rotation would strand them on the old inode —
    copy the content aside then truncate the live file in place
    (copytruncate): the writer's O_APPEND fd resumes at offset 0 (F21).
    A symlinked <name>.1 is refused and that log left unrotated; the other
    logs still rotate, then MaintenanceError reports the refusal. The live
    log itself is opened no-follow and only when lstat shows a regular
    file. Idempotent and cheap (one lstat per log), so it runs every tick."""
    if paths is None:
        import run_log
        try:
            return run_log.rotate(os.path.join(HOME, "data"), max_bytes=LOG_MAX)
        except FileNotFoundError:
            return None
        except (OSError, run_log.RunLogError):
            raise MaintenanceError("run_log_rotation_failed") from None
    # Explicit-path compatibility is retained for callers of the old utility;
    # production maintenance never discovers or truncates legacy/shared logs.
    refused = []
    for path in paths:
        try:
            st = os.lstat(path)
            # a symlinked or special live log is never read or truncated
            if not stat.S_ISREG(st.st_mode) or st.st_size <= LOG_MAX:
                continue
            live = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
            with fdopen_owned(live, "rb") as src:
                if not stat.S_ISREG(os.fstat(live).st_mode):
                    continue
                with suppress(FileNotFoundError):
                    mode = os.lstat(path + ".1").st_mode
                    if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
                        continue        # never write into a FIFO/device
                # owner-only regardless of umask, also for an older loose .1
                fd = os.open(path + ".1", os.O_WRONLY | os.O_CREAT
                             | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                with fdopen_owned(fd, "wb") as dst:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        continue
                    os.fchmod(fd, 0o600)
                    shutil.copyfileobj(src, dst)
                os.ftruncate(live, 0)
        except OSError as e:
            if e.errno == errno.ELOOP:   # a symlinked .1 is never followed
                refused.append(path)
    if refused:
        raise MaintenanceError("log_rotate_symlink_refused")


def _plain_file(path: str) -> bool:
    """A regular file that is not a symlink (lstat)."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def prune_attachments(db_path: str) -> int:
    """Delete downloaded attachment payloads older than 14 days —
    retention-bound the attachments/ directory. The row survives as state='pruned' with name/bytes/sha256/url
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
        # files. Their durable part journal owns those payloads until settled;
        # parts held by card revocation can never be sent and own nothing.
        keep_ids.update(r[0] for r in con.execute("""
          SELECT p.attachment_id FROM notification_render_parts p
          LEFT JOIN notification_renders r USING(delivery_id)
          WHERE p.kind='attachment_part' AND p.state IN ('pending','unknown','held')
            AND p.error_code IS NOT 'card_revoked'
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
    adapter's own cleanup), quarantined cmd/ and cmd_int/
    *.json.invalid files and the backup/snapshot staging copies (PHI)
    and their SQLite sidecars a hard kill strands, plus atomic-write
    temporaries (.atomic.*.tmp, .pub-*, .alias-*.tmp) in cmd_results/ and
    every *_render/ and *_state/ directory (render specs carry PHI).
    ctime: a quarantine rename keeps the original mtime."""
    cutoff = time.time() - LEFTOVER_KEEP_S
    removed = _prune_spool_temporaries(os.path.join(HOME, "data"), cutoff)
    patterns = [os.path.join(HOME, "data", "attachments", ".download-*.part"),
                os.path.join(HOME, "data", "cmd", "*.json.invalid"),
                os.path.join(HOME, "data", "cmd_int", "*.json.invalid")]
    patterns.extend(local_backup.staging_patterns())

    def prune(patterns):
        nonlocal removed
        for pattern in patterns:
            for path in glob.glob(pattern):
                with suppress(OSError):
                    if os.stat(path).st_ctime < cutoff:
                        os.unlink(path)
                        removed += 1

    prune(patterns)
    try:
        fd = os.open(os.path.join(SNAPSHOT_DIR, ".snapshot.lock"),
                     os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with fdopen_owned(fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            prune(os.path.join(SNAPSHOT_DIR, ".snapshot.*.tmp" + suffix)
                  for suffix in ("", "-journal", "-wal", "-shm"))
    except OSError:
        pass
    return removed


def _atomic_temporary(name: str) -> bool:
    return name.startswith(".pub-") or (
        name.endswith(".tmp") and name.startswith((".atomic.", ".alias-")))


def _prune_spool_temporaries(data: str, cutoff: float) -> int:
    """Stale atomic-write temporaries in data/cmd_results, data/*_render
    and data/*_state. Each directory is opened no-follow and pinned by
    fd; entries are checked and unlinked relative to that fd, so a
    symlinked spool directory (or entry) never reaches outside data/."""
    removed = 0
    try:
        names = os.listdir(data)
    except OSError:
        return 0
    for name in names:
        if name != "cmd_results" and not name.endswith(("_render", "_state")):
            continue
        try:
            fd = os.open(os.path.join(data, name),
                         os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            continue                     # symlink, not a directory, gone
        try:
            with os.scandir(fd) as entries:
                for entry in entries:
                    if not _atomic_temporary(entry.name):
                        continue
                    with suppress(OSError):
                        st = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                        if stat.S_ISREG(st.st_mode) and st.st_ctime < cutoff:
                            os.unlink(entry.name, dir_fd=fd)
                            removed += 1
        except OSError:
            pass
        finally:
            os.close(fd)
    return removed


def publish_snapshot(db_path: str, *, deadline: float | None = None, _lock_fd=None,
                     verification_context=None) -> bool:
    """Regenerate the read-only snapshot consumed by the sandboxed CCO
    (backup -> tmp -> DELETE journal -> verify -> atomic rename)."""
    if _lock_fd is not None:
        snapshots._reset_status()
        return bool(snapshots._publish_snapshot_locked(
            db_path, SNAPSHOT_DIR, _lock_fd, deadline=deadline,
            verification_context=verification_context))
    return bool(_publish_snapshot(db_path, SNAPSHOT_DIR, deadline=deadline,
                                  verification_context=verification_context))


# ---------- H4: publish the snapshot only when it would change ----------

class _SnapshotSignatureDeadline(Exception):
    pass


def _content_signature(db_path: str, *, deadline: float | None = None) -> dict | None:
    """Cheap change marker of the ledger: schema_version plus MAX(rowid)
    of every rowid table (an index seek each, never a scan). Tick
    bookkeeping (runs) is left out so an idle tick reads as unchanged.
    In-place edits of requests (MAX(updated_at)) and patient display
    fields (a digest of the small patients table) move it too; other
    in-place updates reach readers through the snapshot_dirty flag
    (notification state) or the SNAPSHOT_MAX_AGE_S forced republish. None when unreadable —
    the caller then publishes (fail toward fresh)."""
    def check_deadline():
        if deadline is not None and monotonic() >= deadline:
            raise _SnapshotSignatureDeadline

    try:
        check_deadline()
        uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        timeout = 5 if deadline is None else min(5, max(0, deadline - monotonic()))
        with closing(sqlite3.connect(uri, uri=True, timeout=timeout)) as con:
            if deadline is not None:
                con.set_progress_handler(lambda: int(monotonic() >= deadline), 1000)

            def execute(sql):
                check_deadline()
                if deadline is not None:
                    ms = int(min(5, max(0, deadline - monotonic())) * 1000)
                    con.execute(f"PRAGMA busy_timeout={ms}")
                    check_deadline()
                return con.execute(sql)

            execute("BEGIN")
            sig = {"": execute("PRAGMA schema_version").fetchone()[0]}
            for name, sql in execute(
                    "SELECT name, sql FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"):
                check_deadline()
                text = (sql or "").upper()
                if (name in SNAPSHOT_SIG_IGNORED or "WITHOUT ROWID" in text
                        or text.startswith("CREATE VIRTUAL")):
                    continue
                sig[name] = execute(
                    f"SELECT MAX(rowid) FROM {_quote(name)}").fetchone()[0]
            # small tables edited in place: a request status change or a
            # renamed patient must republish without waiting an hour
            if "requests" in sig:
                sig["requests@updated_at"] = execute(
                    "SELECT MAX(updated_at) FROM requests").fetchone()[0]
            if "patients" in sig:
                digest = hashlib.sha256()
                for row in execute(
                        "SELECT project_id,project_type,patient_name,disease,"
                        "station_name,url FROM patients ORDER BY project_id"):
                    check_deadline()
                    digest.update(json.dumps(row).encode())
                sig["patients@content"] = digest.hexdigest()
            check_deadline()
            return sig
    except (OSError, sqlite3.Error):
        check_deadline()
        return None


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _snapshot_sig_path() -> str:
    return os.path.join(SNAPSHOT_DIR, SNAPSHOT_SIG_NAME)


def _load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def _recent(at, now: float, interval: float) -> bool:
    return (isinstance(at, int | float) and not isinstance(at, bool)
            and 0 <= now - at < interval)


def _rows_appended_only(old, new: dict) -> bool:
    """True when every marker move since ``old`` is a table's MAX(rowid)
    growing and no urgent key moved. A table added or dropped, a lower or
    vanished MAX(rowid) (restore, rollback) or any other change is False."""
    if not isinstance(old, dict) or old.keys() != new.keys():
        return False
    for key, value in new.items():
        before = old[key]
        if before == value:
            continue
        if key in SNAPSHOT_URGENT_KEYS or type(value) is not int \
                or not (before is None or (type(before) is int and value > before)):
            return False
    return True


def _interval_defers(prev: dict, sig: dict, now: float) -> bool:
    """An append-only change (or none) within SNAPSHOT_MIN_INTERVAL_S of the
    last publish waits for the interval."""
    return (_recent(prev.get("published_at"), now, SNAPSHOT_MIN_INTERVAL_S)
            and _rows_appended_only(prev.get("sig"), sig))


def snapshot_publish_deferrable(db_path: str, *, now: float,
                                deadline: float | None = None) -> bool:
    """True when a tick publish at wall time ``now`` would still be held by
    the interval for what the ledger holds now; False (keep the publish
    reserve) when the copy or marker is missing, the signature is
    unreadable or late, or an urgent key already moved."""
    prev = _load_json(_snapshot_sig_path())
    if not _plain_file(os.path.join(SNAPSHOT_DIR, "ledger-snapshot.db")):
        return False
    try:
        sig = _content_signature(db_path, deadline=deadline)
    except _SnapshotSignatureDeadline:
        return False
    return sig is not None and _interval_defers(prev, sig, now)


def snapshot_published_age_s(now: float | None = None) -> float | None:
    """Seconds since the published copy was last replaced (its mtime);
    None when there is no published copy."""
    try:
        mtime = os.stat(os.path.join(SNAPSHOT_DIR, "ledger-snapshot.db")).st_mtime
    except OSError:
        return None
    return max(0.0, (time.time() if now is None else now) - mtime)


def _source_identity(db_path):
    """(st_dev, st_ino) of the source ledger; None when it cannot be read."""
    try:
        info = os.stat(db_path)
    except OSError:
        return None
    return [info.st_dev, info.st_ino]


def _signature_value_valid(key, value) -> bool:
    """One marker value in the type _content_signature gives that key. No bools and no
    floats where an int belongs: True == 1 and 1.0 == 1 must never match a marker."""
    if key == "":
        return type(value) is int and value >= 0                   # schema_version
    if key == "patients@content":
        return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None
    if key == "requests@updated_at":
        return (value is None or type(value) is int or type(value) is str
                or type(value) is float and math.isfinite(value))
    return value is None or type(value) is int and value >= 0       # a table's MAX(rowid)


def _same_signature_shape(stored, current) -> bool:
    """The stored failure signature has the current signature's keys and value types."""
    return (isinstance(stored, dict) and stored.keys() == current.keys()
            and all(_signature_value_valid(key, stored[key]) for key in stored))


def _commands_retry_wait(prev: dict, sig, db_path: str, now: float):
    """Seconds left before the commands worker may retry the publish that failed for
    this same change, or None when no valid failure applies (state missing or
    malformed, a future time, the source replaced, or a change that is more than
    appended rows). Only a background retry of the same change waits."""
    state = prev.get("commands_retry")
    if (not isinstance(state, dict) or sig is None
            or not _same_signature_shape(state.get("failed_sig"), sig)):
        return None
    failed_at, identity = state.get("failed_at"), state.get("source_identity")
    if (type(failed_at) not in (int, float) or not math.isfinite(failed_at) or failed_at < 0
            or not 0 <= now - failed_at < SNAPSHOT_MIN_INTERVAL_S
            or not isinstance(identity, list) or len(identity) != 2
            or any(type(part) is not int or part < 0 for part in identity)
            or _source_identity(db_path) != identity
            or not _rows_appended_only(state["failed_sig"], sig)):
        return None
    # rounded up to the millisecond: a deferral always reports a positive wait
    return math.ceil((SNAPSHOT_MIN_INTERVAL_S - (now - failed_at)) * 1000) / 1000


def _save_commands_retry(prev: dict, sig, identity, db_path: str, failed_at: float, lock_fd) -> None:
    """Remember a failed commands publish of this change. The success marker
    (sig, published_at) is kept as it is; nothing is written when the change,
    the source or the publication fence cannot be confirmed."""
    if (sig is None or identity is None or _source_identity(db_path) != identity
            or not snapshots._verified_publication_lock(SNAPSHOT_DIR, lock_fd)):
        return
    kept = {key: prev[key] for key in ("sig", "published_at") if key in prev}
    text = json.dumps({**kept, "commands_retry": {"failed_at": failed_at, "failed_sig": sig,
                                                  "source_identity": identity}})
    try:
        os.makedirs(SNAPSHOT_DIR, mode=0o700, exist_ok=True)
        atomic_write(_snapshot_sig_path(), lambda f: f.write(text), mode=0o600)
    except OSError:
        snapshots._status.failure.setdefault("retry_state", "retry_state_write_failed")


def refresh_snapshot(db_path: str, *, force: bool = False, dirty: bool = False,
                     publish: bool = True, now: float | None = None,
                     deadline: float | None = None,
                     verification_context=None, commands_retry: bool = False) -> str | None:
    """Publish the read-only snapshot when it would differ (H4).

    Every tick used to copy and quick_check the whole ledger (144 full
    copies a day). Now a publish happens when ``force``, an urgent marker
    key moved or any MAX(rowid) fell, no published copy or marker exists,
    or the copy is SNAPSHOT_MAX_AGE_S old. A change that only appended
    rows, or ``dirty`` (the notification snapshot_dirty flag: never
    'unchanged'), publishes once SNAPSHOT_MIN_INTERVAL_S has passed since
    the last publish and is 'deferred' before that. When the caller has released its
    publish reserve, publish=False defers any non-forced publication. The ages trigger another attempt, not a
    freshness guarantee when publication fails. Returns 'published',
    'unchanged', 'deferred', or None when a needed publish failed. The
    marker is taken BEFORE the copy: a write landing in between only
    makes the next tick publish again.

    ``commands_retry`` (the commands worker only): after its publish failed, a
    background retry of the same change (rows only appended since that
    attempt) waits SNAPSHOT_MIN_INTERVAL_S from the failure and returns
    'retry_deferred' without copying; an urgent change, ``force`` and every
    other caller publish as before."""
    snapshots._reset_status()
    total_started = time.perf_counter()
    if deadline is not None and monotonic() >= deadline:
        snapshots._fail("signature_deadline")
        return None
    with snapshots._publication_lock(SNAPSHOT_DIR) as lock_fd:
        if lock_fd is None:
            return None
        return _refresh_snapshot_locked(db_path, force=force, dirty=dirty, now=now, publish=publish,
                                        deadline=deadline,
                                        lock_fd=lock_fd, total_started=total_started,
                                        verification_context=verification_context,
                                        commands_retry=commands_retry)


def _refresh_snapshot_locked(db_path: str, *, force: bool, dirty: bool, now, deadline, lock_fd,
                             total_started, verification_context=None, commands_retry=False, publish=True):
    """Check freshness and save its marker under the same publication fence."""
    clock = time.time if now is None else (lambda: now)
    now = clock()
    signature_started = time.perf_counter()
    try:
        sig = _content_signature(db_path, deadline=deadline)
    except _SnapshotSignatureDeadline:
        snapshots._fail("signature_deadline")
        return None
    signature_s = time.perf_counter() - signature_started
    prev = _load_json(_snapshot_sig_path())
    dest = os.path.join(SNAPSHOT_DIR, "ledger-snapshot.db")
    if deadline is not None and monotonic() >= deadline:
        snapshots._fail("signature_deadline")
        return None
    skip = None
    if not force and sig is not None and _plain_file(dest):
        published_at = prev.get("published_at")
        if (not dirty and prev.get("sig") == sig
                and _recent(published_at, now, SNAPSHOT_MAX_AGE_S)):
            skip = "unchanged"
        elif _interval_defers(prev, sig, now):
            skip = "deferred"   # the marker stays: the wait counts from the last publish
    if skip is None and not force and not publish:
        skip = "deferred"   # caller kept no publish reserve; marker and dirty stay
    if skip is not None:
        if deadline is not None and monotonic() >= deadline:
            snapshots._fail("signature_deadline")
            return None
        return skip
    identity = _source_identity(db_path) if commands_retry else None
    # A stored failure that is not used for waiting is reported, never treated as proof.
    retry_note = None
    if commands_retry and not force:
        wait = _commands_retry_wait(prev, sig, db_path, now)
        if wait is None and "commands_retry" in prev:
            retry_note = "retry_state_not_applied"
        if wait is not None:
            snapshots._record_timing(retry_reason="snapshot_retry_cooldown", retry_after_s=wait,
                                     signature_s=round(signature_s, 3),
                                     total_s=round(time.perf_counter() - total_started, 3))
            return "retry_deferred"
    if deadline is not None:
        # The gate path reserves the reacquire wait plus publish tail, so
        # it needs the larger window before the copy may start.
        min_window = (snapshots.SNAPSHOT_MIN_WINDOW_GATE_S
                      if verification_context is not None
                      else snapshots.SNAPSHOT_MIN_WINDOW_S)
        if deadline - monotonic() < min_window:
            # Too little window left for copy + verify + tail: defer before
            # the copy so the attempt cannot burn the verify budget.
            gate = verification_context is not None
            snapshots._record_timing(defer_reason="window_insufficient",
                                     remaining_s=round(deadline - monotonic(), 3),
                                     gate=gate)
            # Deferred attempts otherwise leave no log trace: emit the same
            # fields so the window check's effect is observable in mcs.log.
            print(json.dumps({"event": "snapshot_window_deferred",
                              "pid": os.getpid(), "ts": time.time(),
                              "gate": gate,
                              "remaining_s": round(deadline - monotonic(), 3)}),
                  file=sys.stderr, flush=True)
            return "deferred"
    if not publish_snapshot(db_path, deadline=deadline, _lock_fd=lock_fd,
                            verification_context=verification_context):
        if commands_retry:
            if retry_note is not None:
                snapshots._status.failure.setdefault("retry_state", retry_note)
            _save_commands_retry(prev, sig, identity, db_path, clock(), lock_fd)
        return None
    marker_started = time.perf_counter()
    marker_state = "unavailable"
    if sig is None:
        with suppress(OSError):
            os.unlink(_snapshot_sig_path())
    else:
        text = json.dumps({"sig": sig, "published_at": now})
        try:
            os.makedirs(SNAPSHOT_DIR, mode=0o700, exist_ok=True)
            atomic_write(_snapshot_sig_path(), lambda f: f.write(text), mode=0o600)
        except OSError:
            pass  # Publication succeeded; its optional reuse marker did not.
        else:
            marker_state = "saved"
    if marker_state != "saved" and "commands_retry" in prev:
        retry_note = "retry_state_not_cleared"   # the old failure record stays until it expires
    if retry_note is not None:
        snapshots._record_timing(retry_state=retry_note)
    snapshots._record_timing(signature_s=round(signature_s, 3),
                             marker_s=round(time.perf_counter() - marker_started, 3),
                             marker_state=marker_state,
                             total_s=round(time.perf_counter() - total_started, 3))
    return "published"


# ---------- held sends pinning expired attachments ----------

def held_pinned_attachments(db, now: float | None = None) -> dict:
    """Attachments past ATTACHMENT_KEEP_S kept only because a HELD send
    (state='failed', next_try NULL) older than HELD_PIN_WARN_S still
    lists their post. prune_attachments keeps them — a release may still
    send them — so they would otherwise sit on disk unnoticed; this makes
    them visible. Counts only, never names or paths."""
    now = time.time() if now is None else now
    row = db.execute("""
      SELECT COUNT(DISTINCT a.attachment_id), COALESCE(SUM(a.bytes),0)
      FROM notify_outbox o, json_each(CASE WHEN json_valid(o.payload)
        THEN o.payload ELSE '{}' END, '$.message_ids') j
      JOIN attachments a ON a.message_id=j.value
      WHERE o.kind='new_messages' AND o.state='failed' AND o.next_try IS NULL
        AND o.created_at < ? AND a.local_path IS NOT NULL
        AND a.state IN ('downloaded','withdrawn','pruned')
        AND a.downloaded_at < ?""",
        (now - HELD_PIN_WARN_S, now - ATTACHMENT_KEEP_S)).fetchone()
    return {"count": row[0] or 0, "bytes": row[1] or 0}


# ---------- M3 + retention: daily database upkeep ----------

def _tables(con) -> set:
    return {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _delete_batched(con, table: str, where: str, params=()) -> int:
    """Delete matching rows in short write transactions."""
    total = 0
    while True:
        with con:
            n = con.execute(
                f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM "
                f"{table} WHERE {where} LIMIT {RETENTION_BATCH})",
                params).rowcount
        total += n
        if n < RETENTION_BATCH:
            return total


def _prune_semantic_usage(con, cutoff):
    """Keep unresolved summary requests and receipts with missing evidence."""
    from semantic_runtime import patient_summary_usage_prunable

    total, last_id = 0, 0
    while True:
        with con:
            if not con.in_transaction:
                con.execute("BEGIN IMMEDIATE")
            rows = con.execute("SELECT artifact_id,project_id,meta FROM artifacts "
                               "WHERE kind='semantic_usage' AND created_at<? AND artifact_id>? "
                               "ORDER BY artifact_id LIMIT ?", (cutoff, last_id, RETENTION_BATCH)).fetchall()
            removable = []
            for artifact_id, pid, raw in rows:
                if not patient_summary_usage_prunable(con, pid, raw):
                    continue
                removable.append(artifact_id)
            for artifact_id in removable:
                total += con.execute("DELETE FROM artifacts WHERE artifact_id=? AND kind='semantic_usage'",
                                     (artifact_id,)).rowcount
        if len(rows) < RETENTION_BATCH:
            return total
        last_id = rows[-1][0]


def _source_repair_cloud_pins(con, rows):
    """Pin only this deletion batch's source-repair receipts and ambiguous patient scopes."""
    from mcs_util import strict_json, valid_hash

    header_kind = 'patient_context_source_quality_repair_v1'
    slot_kind = 'patient_context_source_quality_repair_candidate_v1'
    receipt_kinds = {'cloud_usage_reserve', 'cloud_usage_result', 'cloud_usage_disposition'}

    def parsed(raw):
        if not isinstance(raw, str) or len(raw) > 32768:
            return None
        try:
            value = strict_json(raw)
        except (TypeError, ValueError, RecursionError, OverflowError):
            return None
        return value if isinstance(value, dict) else None

    def uuid_ok(value):
        return isinstance(value, str) and re.fullmatch('[0-9a-f]{32}', value) is not None

    if con.execute('SELECT 1 FROM artifacts WHERE kind IN (?,?) AND '
                   "(typeof(project_id)!='integer' OR project_id<=0) LIMIT 1", (header_kind, slot_kind)).fetchone():
        raise MaintenanceError('source_repair_retention_scope_invalid')
    old = [(row, parsed(row[3])) for row in rows if row[1] in receipt_kinds]
    has_repair = con.execute('SELECT 1 FROM artifacts WHERE kind IN (?,?) LIMIT 1', (header_kind, slot_kind)).fetchone() is not None
    unattributed_ids = {row[0] for row, meta in old if meta is None} if has_repair else set()
    if (any(not (type(row[2]) is int and row[2] > 0)
            and not (meta is not None and type(meta.get('project_id')) is int and meta['project_id'] > 0)
            for row, meta in old)
            and has_repair):
        raise MaintenanceError('source_repair_retention_scope_invalid')
    projects = {pid for row, meta in old for pid in (row[2], meta.get('project_id') if meta else None)
                if type(pid) is int and pid > 0}
    wanted_ids = {row[0] for row in rows if row[1] == 'cloud_usage_reserve'}
    batch_uuids = {meta['reservation_id'] for _, meta in old if meta is not None and uuid_ok(meta.get('reservation_id'))}
    referenced_uuids = set()
    if batch_uuids:
        # Adoption treats a UUID as global.  Keep conflicting scopes/results
        # as rejection evidence instead of deleting them back into a PASS.
        for (reservation_id,) in con.execute(
                "SELECT DISTINCT r.value FROM artifacts a, "
                "json_each(CASE WHEN json_valid(a.meta) THEN a.meta ELSE '{}' END) r "
                "WHERE a.kind=? AND r.key='reservation_id' AND r.type='text' "
                "AND r.value IN (SELECT value FROM json_each(?))", (slot_kind, json.dumps(sorted(batch_uuids)))):
            referenced_uuids.add(reservation_id)
    unattributed_ids.update(row[0] for row, meta in old
                            if meta is not None and uuid_ok(meta.get('reservation_id'))
                            and meta['reservation_id'] in referenced_uuids)
    if wanted_ids:
        for _, low, high in con.execute(
                "SELECT json_extract(meta,'$.reservation_artifact_id'),MIN(project_id),MAX(project_id) "
                'FROM artifacts WHERE kind=? AND CASE WHEN json_valid(meta) THEN '
                "json_extract(meta,'$.reservation_artifact_id') IN (SELECT value FROM json_each(?)) END "
                "GROUP BY json_extract(meta,'$.reservation_artifact_id')", (slot_kind, json.dumps(sorted(wanted_ids)))):
            if low != high:
                raise MaintenanceError('source_repair_retention_scope_invalid')
            projects.add(low)
    if not projects:
        return set(), unattributed_ids
    wanted_uuids = {(pid, meta['reservation_id']) for row, meta in old
                    if meta is not None and uuid_ok(meta.get('reservation_id'))
                    for pid in (row[2], meta.get('project_id')) if type(pid) is int and pid > 0}
    held, reserve_ids, reservations = set(), unattributed_ids, {}
    source_projects = set()
    query = ("SELECT a.kind,a.project_id,CASE WHEN typeof(a.meta)='text' AND length(a.meta)<=32768 THEN a.meta END "
             'FROM json_each(?) p CROSS JOIN artifacts a WHERE a.project_id=p.value AND a.kind IN (?,?)')
    for kind, pid, raw in con.execute(query, (json.dumps(sorted(projects)), header_kind, slot_kind)):
        source_projects.add(pid)
        meta = parsed(raw)
        if (meta is None or not valid_hash(meta.get('repair_input_key'))
                or 'project_id' in meta and (type(meta['project_id']) is not int or meta['project_id'] != pid)):
            held.add(pid)
            continue
        if kind == header_kind:
            if not con.execute('SELECT 1 FROM artifacts WHERE kind=? AND project_id=? AND CASE WHEN json_valid(meta) THEN '
                               "json_extract(meta,'$.repair_input_key')=? END LIMIT 1",
                               (slot_kind, pid, meta['repair_input_key'])).fetchone():
                held.add(pid)
            continue
        import original_source_keys
        source_key, artifact_id = meta.get('source_key'), meta.get('reservation_artifact_id')
        if (not ((type(source_key) is int and source_key > 0)
                 or original_source_keys.valid(source_key))
                or not valid_hash(meta.get('unit_key')) or type(artifact_id) is not int
                or not 0 < artifact_id < 2**63 or not uuid_ok(meta.get('reservation_id'))
                or not valid_hash(meta.get('request_fp'))):
            held.add(pid)
            continue
        reservation = (pid, meta['reservation_id'])
        if artifact_id not in wanted_ids and reservation not in wanted_uuids:
            continue
        if artifact_id in wanted_ids:
            reserve_ids.add(artifact_id)
        if reservation in wanted_uuids:
            if reservation in reservations and reservations[reservation] != meta['request_fp']:
                held.add(pid)
            reservations[reservation] = meta['request_fp']
        original = con.execute("SELECT kind,project_id,CASE WHEN typeof(meta)='text' AND length(meta)<=32768 "
                               'THEN meta END FROM artifacts WHERE artifact_id=?', (artifact_id,)).fetchone()
        if original is not None:
            saved = parsed(original[2])
            if (original[0] != 'cloud_usage_reserve' or original[1] != pid or saved is None
                    or any(saved.get(field) != meta[field] for field in ('reservation_id', 'request_fp', 'unit_key'))
                    or type(saved.get('project_id')) is not int or saved['project_id'] != pid):
                held.add(pid)
                continue
    for row, meta in old:
        row_scope = (row[2], meta.get('project_id') if meta else None)
        pid = next((scope for scope in row_scope if scope in source_projects), row[2])
        if pid not in source_projects:
            continue
        if meta is None or type(meta.get('project_id')) is not int or meta['project_id'] != row[2]:
            # Both scope assertions matter; retain this bounded batch row even if only its metadata identifies the patient.
            reserve_ids.add(row[0])
            held.add(pid)
        elif not uuid_ok(meta.get('reservation_id')) or not valid_hash(meta.get('request_fp')):
            held.add(pid)
        elif ((pid, meta['reservation_id']) in reservations
              and meta['request_fp'] != reservations[(pid, meta['reservation_id'])]):
            held.add(pid)
        elif row[1] in ('cloud_usage_result', 'cloud_usage_disposition') and (pid, meta['reservation_id']) in reservations:
            reserve_ids.add(row[0])
    return held, reserve_ids


def _attachment_cloud_pins(con, rows):
    """Keep adopted OCR receipts and unfinished OCR owners without granting adoption."""
    from mcs_util import strict_json

    def parsed(raw):
        try:
            value = strict_json(raw)
        except (TypeError, ValueError, RecursionError, OverflowError):
            return None
        return value if isinstance(value, dict) else None

    receipt_kinds = ('cloud_usage_reserve', 'cloud_usage_result', 'cloud_usage_disposition')
    batch = [(row, parsed(row[3])) for row in rows if row[1] in receipt_kinds]
    uuids = {meta['reservation_id'] for _, meta in batch if meta is not None
             and isinstance(meta.get('reservation_id'), str)}
    held, references, protected = set(), set(), set()
    has_ocr = con.execute("SELECT 1 FROM artifacts WHERE kind='attachment_extract_v1' OR "
        "kind='cloud_usage_reserve' AND CASE WHEN json_valid(meta) THEN "
        "json_extract(meta,'$.owner.source_type')='attachment_ocr' END LIMIT 1").fetchone() is not None
    for pid, raw in con.execute("SELECT project_id,CASE WHEN typeof(content)='text' AND length(content)<=4194304 "
            "THEN content END FROM artifacts WHERE kind='attachment_extract_v1'"):
        if type(pid) is not int or pid <= 0:
            raise MaintenanceError('attachment_inference_retention_scope_invalid')
        doc = parsed(raw)
        if doc is None:
            held.add(pid)
            continue
        if doc.get('stage') == 'local':
            continue
        ids = doc.get('reservation_artifact_ids')
        if (doc.get('stage') != 'cloud' or not isinstance(ids, list) or not 0 < len(ids) <= 512
                or any(type(item) is not int or not 0 < item < 2**63 for item in ids)):
            held.add(pid)
            continue
        references.update(ids)
    # Follow global UUID identity too: deleting a conflicting scope must not erase rejection evidence.
    for artifact_id, raw in con.execute("SELECT artifact_id,CASE WHEN typeof(meta)='text' AND length(meta)<=32768 "
            "THEN meta END FROM artifacts WHERE kind='cloud_usage_reserve' AND ("
            "artifact_id IN (SELECT value FROM json_each(?)) OR CASE WHEN json_valid(meta) THEN "
            "json_extract(meta,'$.reservation_id') IN (SELECT value FROM json_each(?)) END)",
            (json.dumps(sorted(references)), json.dumps(sorted(uuids)))):
        meta = parsed(raw)
        owner = meta.get('owner') if meta else None
        if artifact_id in references or isinstance(owner, dict) and owner.get('source_type') == 'attachment_ocr':
            references.add(artifact_id)
            if meta is not None and isinstance(meta.get('reservation_id'), str):
                protected.add(meta['reservation_id'])
    pinned = set()
    for row, meta in batch:
        owner = meta.get('owner') if meta else None
        if (row[0] in references or meta is None and has_ocr
                or meta is not None and (isinstance(meta.get('reservation_id'), str) and meta['reservation_id'] in protected
                    or isinstance(owner, dict) and owner.get('source_type') == 'attachment_ocr')):
            pinned.add(row[0])
    return held, pinned


def _context_scan_cloud_pins(con, rows):
    """Keep receipts that current patient-context scans, summaries and summary repairs still cite."""
    from inference_version import is_current, read_version
    from mcs_util import strict_json

    def parsed(raw):
        try:
            value = strict_json(raw)
        except (TypeError, ValueError, RecursionError, OverflowError):
            return None
        return value if isinstance(value, dict) else None

    def scope(value):
        return type(value) is int and value > 0

    def row_id(value):
        return type(value) is int and 0 < value < 2**63

    receipt_kinds = ('cloud_usage_reserve', 'cloud_usage_result', 'cloud_usage_disposition')
    summary_kind = 'patient_context_v1'
    repair_kind = 'patient_context_summary_quality_repair_v1'
    candidate_kind = 'patient_context_summary_quality_repair_candidate_v1'
    citing_kinds = ('patient_context_scan_chunk_v1', 'patient_context_scan_v1', summary_kind, repair_kind, candidate_kind)
    batch = [(row, parsed(row[3])) for row in rows if row[1] in receipt_kinds]
    if not batch:
        return set(), set()
    uuids = {meta['reservation_id'] for _, meta in batch if meta is not None
             and isinstance(meta.get('reservation_id'), str)}
    projects = {pid for row, meta in batch for pid in (row[2], meta.get('project_id') if meta else None) if scope(pid)}
    # A reservation UUID is global evidence: its owning patient's citations decide
    # every row carrying it, so a conflicting scope is not deleted back into a PASS.
    reserves = {}
    if uuids:
        for artifact_id, pid, reservation_id in con.execute(
                "SELECT artifact_id,project_id,json_extract(meta,'$.reservation_id') FROM artifacts "
                "WHERE kind='cloud_usage_reserve' AND CASE WHEN json_valid(meta) THEN "
                "json_extract(meta,'$.reservation_id') IN (SELECT value FROM json_each(?)) END",
                (json.dumps(sorted(uuids)),)):
            reserves[artifact_id] = reservation_id
            if scope(pid):
                projects.add(pid)
    held, citing, references, protected = set(), set(), set(), set()
    candidates, repaired, repair_refs = {}, set(), []
    # The currentness test runs here, not in SQL: current_meta_pred() silently
    # drops an unreadable row, which must hold its patient instead.
    query = ("SELECT a.artifact_id,a.kind,a.project_id,CASE WHEN typeof(a.meta)='text' AND length(a.meta)<=4194304 "
             'THEN a.meta END FROM json_each(?) p CROSS JOIN artifacts a WHERE a.project_id=p.value AND a.kind IN ('
             + ','.join('?' for _ in citing_kinds) + ')')
    for artifact_id, kind, pid, raw in con.execute(query, (json.dumps(sorted(projects)), *citing_kinds)):
        meta = parsed(raw)
        if meta is None or read_version(meta) is None:
            held.add(pid)
            continue
        if kind == candidate_kind:
            candidates[artifact_id] = (pid, meta.get('input_key'))
            continue
        if not is_current(meta):
            continue
        if kind == repair_kind:
            # The spent repair names its reservation directly; candidate content is never read here.
            if not row_id(meta.get('reservation_artifact_id')) or not isinstance(meta.get('reservation_id'), str):
                held.add(pid)
                continue
            references.add(meta['reservation_artifact_id'])
            protected.add(meta['reservation_id'])
            repaired.add((pid, meta.get('input_key')))
            citing.add(pid)
            continue
        if 'cloud_receipts' in meta:
            receipts = meta['cloud_receipts']
            if (type(receipts) is not list or not 1 <= len(receipts) <= 128
                    or any(type(ref) is not dict or not row_id(ref.get('artifact_id')) for ref in receipts)):
                held.add(pid)
                continue
            references.update(ref['artifact_id'] for ref in receipts)
            citing.add(pid)
        if kind == summary_kind and meta.get('quality_repair_ref') is not None:
            ref = meta['quality_repair_ref']
            if type(ref) is not dict or not row_id(ref.get('artifact_id')):
                held.add(pid)
            else:
                repair_refs.append((pid, ref['artifact_id']))
    for pid, artifact_id in repair_refs:
        owner, input_key = candidates.get(artifact_id, (None, None))
        if owner != pid or (pid, input_key) not in repaired:
            held.add(pid)   # a published repair whose reservation cannot be traced
    protected.update(reservation_id for artifact_id, reservation_id in reserves.items() if artifact_id in references)
    pinned = {row[0] for row, meta in batch
              if row[0] in references or meta is None and row[2] in citing
              or meta is not None and isinstance(meta.get('reservation_id'), str) and meta['reservation_id'] in protected}
    return held, pinned


def _mark_cloud_usage_pruned(con, cutoff):
    """Durably record how far usage was pruned, in the deleting transaction: a period
    budget starting before it cannot be counted even if the clock later moves back."""
    rows = con.execute("SELECT artifact_id,content FROM artifacts WHERE kind='cloud_usage_pruned_before' "
                       "ORDER BY artifact_id").fetchall()
    if not rows:
        con.execute("INSERT INTO artifacts(kind,content,created_at) VALUES('cloud_usage_pruned_before',?,?)",
                    (repr(float(cutoff)), time.time()))
        return
    try:
        marks = [float(content) for _, content in rows]
    except (TypeError, ValueError):
        return   # an unreadable mark keeps period budgets stopped; its lost range is never overwritten
    if len(rows) != 1 or not math.isfinite(marks[0]):
        return   # likewise for duplicates or a non-finite mark
    if marks[0] < cutoff:
        con.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (repr(float(cutoff)), rows[0][0]))


def _prune_local_replacement_records(con, pid, raw):
    """Delete the ``superseded_local`` records of one reservation that was just pruned.

    Local work may replace a paid request long after it was sent, so the record of that is
    younger than its reservation. It says nothing without the reservation and would otherwise
    stay behind as a disposition no reservation explains."""
    from ledger_schema import CLOUD_USAGE_KINDS_SQL, CLOUD_USAGE_RESERVATION_SQL
    from mcs_util import strict_json
    try:
        reservation_id = strict_json(raw).get('reservation_id')
    except (TypeError, ValueError, RecursionError, OverflowError, AttributeError):
        return 0
    if not isinstance(reservation_id, str):
        return 0
    return con.execute(
        f"DELETE FROM artifacts WHERE kind IN {CLOUD_USAGE_KINDS_SQL} AND kind='cloud_usage_disposition' "
        f"AND {CLOUD_USAGE_RESERVATION_SQL}=? AND project_id IS ? "
        "AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.disposition') END='superseded_local'",
        (reservation_id, pid)).rowcount


def _prune_cloud_usage(con, cutoff):
    """Delete expired cloud accounting, keeping evidence that repairs, attachment OCR and patient context still cite."""
    total, last_id = 0, 0
    kinds = ('cloud_usage_reserve', 'cloud_usage_result', 'cloud_usage_dryrun', 'cloud_compare_v1', 'cloud_usage_disposition')
    # Spending a patient-context reset carried over ages out with the rows it replaced.
    from cloud_usage_accounting import expired_carryover_ids
    with con:
        if not con.in_transaction:
            con.execute('BEGIN IMMEDIATE')
        expired = expired_carryover_ids(con, cutoff)
        if expired:
            _mark_cloud_usage_pruned(con, cutoff)
        for artifact_id in expired:
            total += con.execute('DELETE FROM artifacts WHERE artifact_id=?', (artifact_id,)).rowcount
    while True:
        with con:
            if not con.in_transaction:
                con.execute('BEGIN IMMEDIATE')
            rows = con.execute("SELECT artifact_id,kind,project_id,CASE WHEN typeof(meta)='text' AND length(meta)<=32768 "
                'THEN meta END FROM artifacts WHERE kind IN (' + ','.join('?' for _ in kinds)
                + ') AND created_at<? AND artifact_id>? ORDER BY artifact_id LIMIT ?',
                (*kinds, cutoff, last_id, RETENTION_BATCH)).fetchall()
            if not rows:
                return total
            held, pinned_ids = _source_repair_cloud_pins(con, rows)
            attachment_held, attachment_ids = _attachment_cloud_pins(con, rows)
            context_held, context_ids = _context_scan_cloud_pins(con, rows)
            held.update(attachment_held, context_held)
            pinned_ids.update(attachment_ids, context_ids)
            _mark_cloud_usage_pruned(con, cutoff)
            for artifact_id, kind, pid, raw in rows:
                if kind in ('cloud_usage_reserve', 'cloud_usage_result', 'cloud_usage_disposition') and (pid in held or artifact_id in pinned_ids):
                    continue
                deleted = con.execute('DELETE FROM artifacts WHERE artifact_id=? AND kind=?', (artifact_id, kind)).rowcount
                total += deleted
                if deleted and kind == 'cloud_usage_reserve':
                    total += _prune_local_replacement_records(con, pid, raw)
        if len(rows) < RETENTION_BATCH:
            return total
        last_id = rows[-1][0]


def _outbox_refs(con, tables: set) -> str:
    """NOT EXISTS guards for every column that can name an outbox event.

    Any table column named event_id or ending in '_event_id' (intent
    batches/cards, task reminders, renders.intent_event_id, tables added
    later) and the restore-hold events_json lists pin their rows."""
    guards = []
    for table in sorted(tables - {"notify_outbox"}):
        if table.startswith(("sqlite_", "messages_fts")):
            continue
        for col in [r[1] for r in con.execute(
                f"PRAGMA table_info({_quote(table)})")]:
            if col == "event_id" or col.endswith("_event_id"):
                guards.append(
                    f"NOT EXISTS(SELECT 1 FROM {_quote(table)} r "
                    f"WHERE r.{_quote(col)}=notify_outbox.event_id)")
    if "notification_restore_holds" in tables:
        guards.append(
            "NOT EXISTS(SELECT 1 FROM notification_restore_holds h, "
            "json_each(CASE WHEN json_valid(h.events_json) THEN h.events_json "
            "ELSE '[]' END) e WHERE e.value=notify_outbox.event_id)")
    return " AND ".join(guards) or "1"


def _prune_unsent_renders(con, cutoff: float, tables: set) -> int:
    """Delete superseded never-delivered renders with their parts and
    attempts, in short batches. Only renders with no external effect
    qualify: state not_sent/cancelled, every part and attempt not_sent,
    spec file already gone (spec_published=0), a later render on the
    card (or a card-less notice), and no intent binding or restore hold
    naming the render or its attempts."""
    need = {"notification_renders", "notification_render_parts",
            "notification_delivery_attempts", "notification_intent_cards",
            "notification_restore_holds"}
    if not need <= tables:
        return 0
    total = 0
    while True:
        ids = [r[0] for r in con.execute(
            """SELECT r.delivery_id FROM notification_renders r
               WHERE r.state IN ('not_sent','cancelled') AND r.updated_at < ?
                 AND r.spec_published=0
                 AND (r.card_id IS NULL OR EXISTS(
                   SELECT 1 FROM notification_renders s
                   WHERE s.card_id=r.card_id AND s.render_rev>r.render_rev))
                 AND NOT EXISTS(SELECT 1 FROM notification_render_parts p
                   WHERE p.delivery_id=r.delivery_id AND p.state!='not_sent')
                 AND NOT EXISTS(SELECT 1 FROM notification_delivery_attempts a
                   WHERE a.delivery_id=r.delivery_id AND a.state!='not_sent')
                 AND r.delivery_id NOT IN (SELECT delivery_id
                   FROM notification_intent_cards WHERE delivery_id IS NOT NULL)
                 AND r.delivery_id NOT IN (SELECT delivery_id
                   FROM notification_restore_holds WHERE delivery_id IS NOT NULL)
                 AND NOT EXISTS(SELECT 1 FROM notification_delivery_attempts a
                   JOIN notification_restore_holds h ON h.attempt_id=a.attempt_id
                   WHERE a.delivery_id=r.delivery_id)
               LIMIT ?""", (cutoff, PRUNE_BATCH))]
        if not ids:
            return total
        marks = ",".join("?" * len(ids))
        with con:
            for table in ("notification_render_parts",
                          "notification_delivery_attempts",
                          "notification_renders"):
                con.execute(f"DELETE FROM {table} WHERE delivery_id IN ({marks})", ids)
        total += len(ids)
        if len(ids) < PRUNE_BATCH:
            return total


def prune_history(con, now: float | None = None) -> dict:
    """Age-based retention for append-only bookkeeping.

    - runs older than RETENTION_S, keeping the newest row of every status
      (backup/signal freshness read MAX(finished_at) per status) and any
      'running' row;
    - read_marks older than RETENTION_S (was_marked only asks about the
      current snapshot timestamp);
    - finished system alerts (RETAINED_ALERT_KINDS, accepted/suppressed)
      older than RETENTION_S that no table or restore hold references;
    - semantic_drain_run metrics older than DRAIN_RUN_KEEP_S and Jev usage
      receipts older than USAGE_KEEP_S, retaining unresolved summary requests;
    - never-delivered renders older than RETENTION_S with their parts and
      attempts (_prune_unsent_renders), then view manifests older than
      RETENTION_S that are not their card's newest and that no render,
      acknowledgement or action token references.
    Kept on purpose: delivered renders and their parts/attempts (proof of
    what was shown, read by stats and reconcile), command_receipts
    (approval, stamp, dictionary and update provenance and command
    idempotency), notification_acknowledgements (human action record),
    thread_read_marks and message_reaction_actor_fetch (one current-state
    row per thread/message, not a log), message_revisions (the edit audit trail), done/failed
    fetch_jobs (dedupe and state markers: reply reconciliation, the
    semantic scheduler row, QC gating), every other outbox kind and every
    other artifact kind (fingerprint caches, evaluation id references)."""
    now = time.time() if now is None else now
    cutoff = now - RETENTION_S
    tables = _tables(con)
    out = {}
    if "runs" in tables:
        out["runs"] = _delete_batched(
            con, "runs", "started_at < ? AND status IS NOT 'running' "
            "AND run_id NOT IN (SELECT MAX(run_id) FROM runs GROUP BY status)",
            (cutoff,))
    if "read_marks" in tables:
        out["read_marks"] = _delete_batched(
            con, "read_marks", "marked_at < ?", (cutoff,))
    if "notify_outbox" in tables:
        kinds = ",".join("?" * len(RETAINED_ALERT_KINDS))
        out["alerts"] = _delete_batched(
            con, "notify_outbox",
            f"kind IN ({kinds}) AND state IN ('accepted','suppressed') "
            "AND created_at < ? AND COALESCE(updated_at, created_at) < ? "
            f"AND {_outbox_refs(con, tables)}",
            (*RETAINED_ALERT_KINDS, cutoff, cutoff))
    out["unsent_renders"] = _prune_unsent_renders(con, cutoff, tables)
    if {"notification_view_manifests", "notification_renders",
            "notification_acknowledgements",
            "notification_action_tokens"} <= tables:
        out["view_manifests"] = _delete_batched(
            con, "notification_view_manifests",
            "created_at < ? AND manifest_id < (SELECT MAX(m.manifest_id) "
            "FROM notification_view_manifests m "
            "WHERE m.card_id=notification_view_manifests.card_id) "
            "AND manifest_id NOT IN (SELECT manifest_id FROM notification_renders "
            "WHERE manifest_id IS NOT NULL) "
            "AND manifest_id NOT IN (SELECT manifest_id "
            "FROM notification_acknowledgements) "
            "AND manifest_id NOT IN (SELECT need_manifest_id "
            "FROM notification_action_tokens WHERE need_manifest_id IS NOT NULL)",
            (cutoff,))
    if "artifacts" in tables:
        out["drain_runs"] = _delete_batched(
            con, "artifacts", "kind='semantic_drain_run' AND created_at < ?",
            (now - DRAIN_RUN_KEEP_S,))
        out["usage_receipts"] = _prune_semantic_usage(con, now - USAGE_KEEP_S)
        out["cloud_usage_receipts"] = _prune_cloud_usage(con, now - CLOUD_USAGE_KEEP_S)
    return out


def db_maintenance(db_path: str, *, deadline: float | None = None,
                   now: float | None = None) -> dict | None:
    """Daily SQLite upkeep under the caller's run lock (M3).

    Once per DB_MAINT_INTERVAL_S: retention (prune_history), then
    `PRAGMA optimize` (bounded ANALYZE) and `wal_checkpoint(TRUNCATE)`.
    Once per FTS_OPTIMIZE_INTERVAL_S: an FTS5 'optimize' merge of
    messages_fts. ``deadline`` (time.monotonic) is the caller's hard stop:
    a pass that cannot start with enough time left is skipped and retried
    next tick, and a running statement is interrupted at the deadline
    (its transaction rolls back). Returns None when nothing was due.
    After a successful daily pass whose state was saved, it may also create the
    optional accounting order index (cloud_usage_order_index in the result)."""
    now = time.time() if now is None else now
    state = _load_json(DB_MAINT_STATE)
    daily = not _recent(state.get("daily_at"), now, DB_MAINT_INTERVAL_S)
    fts = not _recent(state.get("fts_at"), now, FTS_OPTIMIZE_INTERVAL_S)
    # a TRUNCATE a reader held off is retried every tick, not next day
    retry_ckpt = not daily and state.get("checkpoint_pending") is True
    if not (daily or fts or retry_ckpt):
        return None

    def left() -> float:
        return float("inf") if deadline is None else deadline - monotonic()

    if left() < DB_MAINT_MIN_S:
        return {"skipped": "deadline"}
    out: dict = {}
    retention_error = None
    saved = False
    con = sqlite3.connect(db_path, timeout=5)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        con.execute("PRAGMA foreign_keys=ON")
        if deadline is not None:
            con.set_progress_handler(
                lambda: int(monotonic() >= deadline), 10000)
        if daily:
            try:
                out["pruned"] = prune_history(con, now)
            except MaintenanceError as error:
                if str(error) != "source_repair_retention_scope_invalid":
                    raise
                # Preserve the ambiguous accounting evidence and report the
                # incomplete retention pass after independent upkeep runs.
                retention_error = error
            con.execute("PRAGMA analysis_limit=1000")
            con.execute("PRAGMA optimize=0x10002")
            if retention_error is None:
                state["daily_at"] = now
        if daily or retry_ckpt:
            # pending until its outcome is known: an interrupted or failed
            # TRUNCATE is retried next tick like a busy one
            state["checkpoint_pending"] = True
            # busy: a reader kept the WAL from being truncated (no error)
            busy, log, done = con.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            out["checkpoint"] = {"busy": busy, "log": log, "done": done}
            if not busy:
                state.pop("checkpoint_pending", None)
        if fts and "messages_fts" not in _tables(con):
            state["fts_at"] = now
        elif fts and left() < FTS_OPTIMIZE_MIN_S:
            out["fts"] = "deferred"
        elif fts:
            with con:
                con.execute("INSERT INTO messages_fts(messages_fts) "
                            "VALUES('optimize')")
            state["fts_at"] = now
            out["fts"] = "optimized"
        if retention_error is not None:
            raise retention_error
    except sqlite3.OperationalError as e:
        if deadline is not None and monotonic() >= deadline:
            raise MaintenanceError("db_maintenance_deadline") from e
        raise
    finally:
        con.close()
        text = json.dumps(state)
        with suppress(OSError):
            atomic_write(DB_MAINT_STATE, lambda f: f.write(text), mode=0o600)
            saved = True
    if daily:
        out["cloud_usage_order_index"] = (
            _cloud_usage_order_index(db_path, deadline) if saved else "state_unpersisted")
    return out


def _cloud_usage_order_index(db_path: str, deadline: float | None) -> str:
    """Create the optional accounting order index on its own bounded connection.

    Only after the daily pass and its state were saved, so a failed or interrupted
    build waits for the next daily pass instead of repeating every tick."""
    import ledger_schema
    if deadline is None or deadline - monotonic() < DB_MAINT_MIN_S:
        return "deadline"
    wait_s = min(5.0, max(0.0, deadline - monotonic()))
    con = sqlite3.connect(db_path, timeout=wait_s)
    try:
        con.execute(f"PRAGMA busy_timeout={int(wait_s * 1000)}")
        con.set_progress_handler(lambda: int(monotonic() >= deadline), 10000)
        try:
            return ledger_schema.install_cloud_usage_order_index(con, deadline=deadline)
        except sqlite3.OperationalError as error:
            if monotonic() >= deadline:
                return "deadline"
            if getattr(error, "sqlite_errorcode", 0) & 0xff in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                return "busy"
            raise
    finally:
        con.close()
