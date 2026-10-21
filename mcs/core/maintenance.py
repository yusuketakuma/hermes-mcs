#!/usr/bin/env python3
"""MCS housekeeping — backup, log rotation, read-only snapshot publish.

Split from run_check orchestration: these run at the end of every tick
and must each be independently failure-isolated by the caller.
"""
import errno
import glob
import json
import math
import os
import shutil
import sqlite3
import stat
import tempfile
import time
from pathlib import Path
from contextlib import closing, suppress
from time import monotonic

from snapshots import publish_snapshot as _publish_snapshot
from ledger import valid_mcs_db
from mcs_util import HOME
from safe_io import atomic_write, publish_tmp, fdopen_owned
from llm_availability import disk_floor_mb
from safe_io import read_regular_nofollow

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
# M7: a backup is a copy + quick_check of the whole ledger. It starts only
# when the time left before the caller's hard deadline covers the file at
# this conservative rate plus a margin; otherwise it is skipped with a
# recorded reason instead of being killed by the watchdog every tick.
BACKUP_RATE_BPS = 32 * 1024 * 1024
BACKUP_MARGIN_S = 5.0
BACKUP_STEP_PAGES = 256    # copy granularity the deadline is checked at
BACKUP_CHECK_SLACK_S = 5.0  # overrun of the same-day check before a replacement
# H4: the read-only snapshot is republished only when the ledger changed
# (cheap marker, see _content_signature) or the published copy is this old
SNAPSHOT_MAX_AGE_S = 3600
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
USAGE_KEEP_S = 7 * 86400        # jev_usage_today reads the JST day only
RETENTION_BATCH = 5000
# finished system alerts only: every other outbox kind doubles as dedupe
# or history evidence (semantic_notice delivery keys, signal cooldowns,
# urgent daily caps, digest supersession, attachment followups)
RETAINED_ALERT_KINDS = ("session_expired", "session_recovered", "run_failed",
                        "health_write_failed")
# held sends older than this that still pin expired attachments are
# surfaced in health (the files are kept: a release may still send them)
HELD_PIN_WARN_S = 30 * 86400


class MaintenanceError(RuntimeError):
    """Housekeeping failure carrying a short stable reason token
    (e.g. "backup_verify_failed"). Distinct from the adapter's MCSError
    — core must not depend on the ingest layer for an error type."""




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
    marker = _read_ok_marker(dest)
    return bool(marker) and marker == _stat_sig(dest)


def _valid(path: str, deadline: float | None) -> bool:
    """valid_mcs_db, bounded by ``deadline`` only when one is given."""
    if deadline is None:
        return valid_mcs_db(path)
    return valid_mcs_db(path, deadline=deadline)


def _publish_backup(db_path: str, tmp: str, dest: str,
                    deadline: float | None = None) -> None:
    """Copy, verify and publish a backup while owning its staging file.
    The copy is stepped so ``deadline`` abandons it mid-way instead of
    running past the watchdog."""
    def check_deadline(*_):
        if deadline is not None and monotonic() >= deadline:
            raise MaintenanceError("backup_deadline_exceeded")
    try:
        with closing(sqlite3.connect(
                Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(tmp)) as dst:
                # one read snapshot across the steps: a write by another
                # connection would otherwise restart the copy every step
                src.execute("BEGIN")
                src.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
                src.backup(dst, pages=BACKUP_STEP_PAGES, progress=check_deadline)
                dst.execute("PRAGMA journal_mode=DELETE")
        if not _valid(tmp, deadline):
            if deadline is not None and monotonic() >= deadline:
                raise MaintenanceError("backup_deadline_exceeded")
            raise MaintenanceError("backup_verify_failed")
        publish_tmp(tmp, dest, mode=0o600)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise


def _backup_need_mb(db_path: str) -> float:
    """Room for two ledger copies above the disk floor."""
    return os.path.getsize(db_path) * 2 / (1024 * 1024) + disk_floor_mb()


def backup_time_needed_s(db_path: str) -> float:
    """Seconds a copy + verify of this ledger is budgeted to take."""
    try:
        size = os.path.getsize(db_path)
    except OSError:
        size = 0
    return size / BACKUP_RATE_BPS + BACKUP_MARGIN_S


def _write_ok_marker(dest: str) -> None:
    sig = _stat_sig(dest)
    if sig:
        with suppress(OSError):   # marker is only a fast path
            atomic_write(dest + ".ok", lambda f: f.write(sig), mode=0o600)


def daily_backup(db_path: str, *, deadline: float | None = None):
    """One VERIFIED sqlite .backup per day — write to tmp, schema/quick_check,
    then atomic publish. A present-but-broken file must never block a
    fresh backup (Oracle B24). Returns "skipped_disk_low" without
    writing when the volume lacks room for two ledger copies above the
    disk floor — a backup must not be what fills the disk.

    ``deadline`` (time.monotonic) is the caller's hard stop, e.g. the
    watchdog kill time. Returns "skipped_deadline" without writing when
    the time left cannot cover the ledger at BACKUP_RATE_BPS: a backup
    the watchdog kills every tick would never complete (M7)."""
    os.makedirs(BACKUP_DIR, mode=0o700, exist_ok=True)
    os.chmod(BACKUP_DIR, 0o700)   # PHI store: never umask-loose
    stamp = time.strftime("%Y%m%d")
    dest = os.path.join(BACKUP_DIR, f"ledger-{stamp}.db")
    # the .ok marker (size+mtime_ns at publish time) skips the full
    # quick_check while the verified file is untouched; any change or a
    # missing marker falls back to validation (B24)
    if _verified_unchanged(dest):
        return
    # checking today's file must leave room to replace it: a broken copy
    # whose check eats the whole budget would otherwise never be replaced
    check_by = None if deadline is None \
        else deadline - backup_time_needed_s(db_path) - BACKUP_CHECK_SLACK_S
    if _valid(dest, check_by):
        # re-arm the fast path, or every later tick today re-runs the
        # full quick_check (a same-day backup made before the marker)
        _write_ok_marker(dest)
        return
    if deadline is not None and (
            deadline - monotonic() < backup_time_needed_s(db_path)):
        return "skipped_deadline"
    if shutil.disk_usage(BACKUP_DIR).free / (1024 * 1024) < _backup_need_mb(db_path):
        return "skipped_disk_low"
    # private from creation; prune_leftovers sweeps a copy a hard kill strands
    fd, tmp = tempfile.mkstemp(
        prefix=f"ledger-{stamp}-", suffix=".db.tmp", dir=BACKUP_DIR)
    os.close(fd)
    _publish_backup(db_path, tmp, dest, deadline=deadline)
    _write_ok_marker(dest)
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, "ledger-*.db")))
    for old in files[:-BACKUP_KEEP]:
        for path in (old, old + ".ok"):
            with suppress(OSError):
                os.unlink(path)
    prune_preupdate_backups()


def newest_backup_age_s() -> float | None:
    """Age of the newest daily backup; None when there is none."""
    stamps = []
    for path in glob.glob(os.path.join(BACKUP_DIR, "ledger-*.db")):
        with suppress(OSError):   # rotated away mid-scan
            stamps.append(os.path.getmtime(path))
    return time.time() - max(stamps) if stamps else None


LOCAL_STATUS_SCAN_MAX = 256      # directory entries one health read examines
LOCAL_STATUS_SCAN_S = 0.5
OK_MARKER_MAX = 256


class _MarkerUnreadable(Exception):
    pass


def _read_ok_marker(dest: str) -> str | None:
    """Read the bounded verification marker shared by backup and health."""
    try:
        marker = read_regular_nofollow(dest + ".ok", OK_MARKER_MAX, _MarkerUnreadable)
        if len(marker) <= OK_MARKER_MAX:
            return marker.decode("ascii").strip()
    except (OSError, ValueError, _MarkerUnreadable):
        pass
    return None


def local_backup_status(*, now: float) -> dict:
    """Read-only state of the newest daily backup for health: a bounded
    scan, lstat, and a bounded no-follow read of the .ok marker — no DB
    open, no quick_check, no path or error text."""
    def out(state, age=None):
        return {"state": state, "newest_age_s": age, "observed_at": now}
    try:
        stop = monotonic() + LOCAL_STATUS_SCAN_S
        newest = None
        with os.scandir(BACKUP_DIR) as entries:
            for n, entry in enumerate(entries):
                if n >= LOCAL_STATUS_SCAN_MAX or monotonic() > stop:
                    return out("unknown")      # cannot know which is newest
                if not (entry.name.startswith("ledger-") and entry.name.endswith(".db")):
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue                   # rotated away mid-scan
                if stat.S_ISREG(st.st_mode) and (newest is None or st.st_mtime > newest[1].st_mtime):
                    newest = (entry.path, st)
    except FileNotFoundError:
        return out("missing")
    except OSError:
        return out("unreadable")
    except Exception:
        return out("unknown")
    if newest is None:
        return out("missing")
    path, st = newest
    age = now - st.st_mtime
    if not (math.isfinite(age) and age >= 0):
        return out("unknown")
    verified = _read_ok_marker(path) == f"{st.st_size} {st.st_mtime_ns}"
    return out("verified" if verified else "unverified", age)


def backup_overdue() -> bool:
    """True when the newest daily backup is older than BACKUP_OVERDUE_S.
    No backup yet is not overdue: a fresh install gets its first one on
    the next tick that has budget left."""
    age = newest_backup_age_s()
    return age is not None and age > BACKUP_OVERDUE_S


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
    and every schema_bump `applied` entry, so successive rollbacks retain
    the DB copy for each schema boundary. Ordinary applied updates do not
    retain a separate copy."""
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
        for rec in held + [r for r in applied if r.get("schema_bump")]:
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
    (copytruncate): the writer's O_APPEND fd resumes at offset 0 (F21).
    A symlinked <name>.1 is refused and that log left unrotated; the other
    logs still rotate, then MaintenanceError reports the refusal."""
    refused = []
    for path in paths or _default_logs():
        try:
            if os.path.getsize(path) > LOG_MAX:
                with open(path, "rb") as src:
                    # owner-only regardless of umask, also for an older loose .1
                    fd = os.open(path + ".1", os.O_WRONLY | os.O_CREAT
                                 | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                    with fdopen_owned(fd, "wb") as dst:
                        os.fchmod(fd, 0o600)
                        shutil.copyfileobj(src, dst)
                with open(path, "w"):
                    pass
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


def _default_logs() -> list:
    """The fixed log targets, then every other data/*.log — standalone
    child logs data/<job>.log (extract-0, extract-2, cmd, lineworks,
    mcs_* ...) — and the LINE WORKS service stdout/stderr (M1).
    Discovered files are taken only as plain files: a symlink found by
    the glob is never followed."""
    data = os.path.join(HOME, "data")
    paths = list(_fixed_logs())
    seen = set(paths)
    found = sorted(glob.glob(os.path.join(glob.escape(data), "*.log")))
    found += [os.path.join(data, "lineworks_state", name)
              for name in ("service.stdout.log", "service.stderr.log")]
    for path in found:
        if path not in seen and _plain_file(path):
            seen.add(path)
            paths.append(path)
    return paths


def _fixed_logs() -> tuple:
    """Known log targets; evaluated per call (HOME may be patched)."""
    data = os.path.join(HOME, "data")
    return (LOGFILE,
            os.path.join(data, "extract_drain.log"),
            os.path.join(data, "extract_drain_2.log"),
            os.path.join(data, "extract_llm.log"),
            os.path.join(data, "semantic_drain.log"),
            # runtime_mode=standalone connector / launchd jobs
            os.path.join(data, "standalone.log"),
            os.path.join(data, "cron.log"),
            os.path.join(data, "llamacpp-restart.log"),
            # mcs_update.sh / org.mcs.recovery / local.mcs-int
            os.path.join(data, "update.log"),
            os.path.join(data, "recovery.log"),
            os.path.join(data, "cmd_int.log"),
            # ai.mcs.llamaserver plist (__HERMES_HOME__/logs):
            # Hermes mode ~/.hermes, standalone ~/.mcs
            os.path.join(os.path.expanduser("~/.hermes"),
                         "logs", "llamacpp.log"),
            os.path.join(HOME, "logs", "llamacpp.log"))


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
    and their SQLite sidecars a hard kill strands. ctime: a quarantine rename keeps the original
    mtime."""
    cutoff = time.time() - LEFTOVER_KEEP_S
    removed = 0
    for pattern in (os.path.join(HOME, "data", "attachments", ".download-*.part"),
                    os.path.join(HOME, "data", "cmd", "*.json.invalid"),
                    os.path.join(HOME, "data", "cmd_int", "*.json.invalid"),
                    *(os.path.join(directory, stage + sidecar)
                      for directory, stage in ((BACKUP_DIR, "*.db.tmp"),
                                               (SNAPSHOT_DIR, ".snapshot.*.tmp"))
                      for sidecar in ("", "-journal", "-wal", "-shm"))):
        for path in glob.glob(pattern):
            with suppress(OSError):
                if os.stat(path).st_ctime < cutoff:
                    os.unlink(path)
                    removed += 1
    return removed


def publish_snapshot(db_path: str, *, deadline: float | None = None) -> bool:
    """Regenerate the read-only snapshot consumed by the sandboxed CCO
    (backup -> tmp -> DELETE journal -> verify -> atomic rename)."""
    return bool(_publish_snapshot(db_path, SNAPSHOT_DIR, deadline=deadline))


# ---------- H4: publish the snapshot only when it would change ----------

def _content_signature(db_path: str) -> dict | None:
    """Cheap change marker of the ledger: schema_version plus MAX(rowid)
    of every rowid table (an index seek each, never a scan). Tick
    bookkeeping (runs) is left out so an idle tick reads as unchanged.
    Pure in-place updates by other processes do not move it; those
    reach readers through the snapshot_dirty flag (notification state)
    or the SNAPSHOT_MAX_AGE_S forced republish. None when unreadable —
    the caller then publishes (fail toward fresh)."""
    try:
        uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as con:
            con.execute("PRAGMA busy_timeout=5000")
            con.execute("BEGIN")
            sig = {"": con.execute("PRAGMA schema_version").fetchone()[0]}
            for name, sql in con.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
                text = (sql or "").upper()
                if (name in SNAPSHOT_SIG_IGNORED or "WITHOUT ROWID" in text
                        or text.startswith("CREATE VIRTUAL")):
                    continue
                sig[name] = con.execute(
                    f"SELECT MAX(rowid) FROM {_quote(name)}").fetchone()[0]
            return sig
    except (OSError, sqlite3.Error):
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


def refresh_snapshot(db_path: str, *, force: bool = False,
                     now: float | None = None,
                     deadline: float | None = None) -> str | None:
    """Publish the read-only snapshot when it would differ (H4).

    Every tick used to copy and quick_check the whole ledger (144 full
    copies a day). Now a publish happens when ``force`` (the notification
    snapshot_dirty flag), the content marker moved since the last
    publish, no published copy exists, or the copy is SNAPSHOT_MAX_AGE_S
    old — readers are never more than that behind. Returns 'published',
    'unchanged', or None when a needed publish failed. The marker is
    taken BEFORE the copy: a write landing in between only makes the
    next tick publish again."""
    now = time.time() if now is None else now
    sig = _content_signature(db_path)
    prev = _load_json(_snapshot_sig_path())
    dest = os.path.join(SNAPSHOT_DIR, "ledger-snapshot.db")
    if (not force and sig is not None and prev.get("sig") == sig
            and _recent(prev.get("published_at"), now, SNAPSHOT_MAX_AGE_S)
            and _plain_file(dest)):
        return "unchanged"
    if not publish_snapshot(db_path, deadline=deadline):
        return None
    if sig is None:
        with suppress(OSError):
            os.unlink(_snapshot_sig_path())
    else:
        text = json.dumps({"sig": sig, "published_at": now})
        with suppress(OSError):   # the marker is only a fast path
            os.makedirs(SNAPSHOT_DIR, mode=0o700, exist_ok=True)
            atomic_write(_snapshot_sig_path(), lambda f: f.write(text),
                         mode=0o600)
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
      receipts older than USAGE_KEEP_S.
    Kept on purpose: message_revisions (the edit audit trail), done/failed
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
    if "artifacts" in tables:
        out["drain_runs"] = _delete_batched(
            con, "artifacts", "kind='semantic_drain_run' AND created_at < ?",
            (now - DRAIN_RUN_KEEP_S,))
        out["usage_receipts"] = _delete_batched(
            con, "artifacts", "kind='semantic_usage' AND created_at < ?",
            (now - USAGE_KEEP_S,))
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
    (its transaction rolls back). Returns None when nothing was due."""
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
    con = sqlite3.connect(db_path, timeout=5)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        con.execute("PRAGMA foreign_keys=ON")
        if deadline is not None:
            con.set_progress_handler(
                lambda: int(monotonic() >= deadline), 10000)
        if daily:
            out["pruned"] = prune_history(con, now)
            con.execute("PRAGMA analysis_limit=1000")
            con.execute("PRAGMA optimize=0x10002")
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
    except sqlite3.OperationalError as e:
        if deadline is not None and monotonic() >= deadline:
            raise MaintenanceError("db_maintenance_deadline") from e
        raise
    finally:
        con.close()
        text = json.dumps(state)
        with suppress(OSError):
            atomic_write(DB_MAINT_STATE, lambda f: f.write(text), mode=0o600)
    return out
