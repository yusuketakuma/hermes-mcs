#!/usr/bin/env python3
"""Local backups of the ledger and the home-care records: verified daily rotations and pre-update copies.

Every copy is a stepped sqlite ``.backup`` into a private staging file, a
schema/quick_check, then an atomic publish into ``data/backups`` (0700).
The home-care records database (``data/homecare.db``) is copied only when
that file exists; it is never created here.
Health reads only names, stat and the ``.ok`` marker; nothing here opens
the live ledger for writing. Encrypted off-device bundles are ``mcs_backup``.
"""
import glob
import gzip
import json
import math
import os
import shutil
import sqlite3
import stat
import tempfile
import time
from contextlib import closing, contextmanager, suppress
from pathlib import Path
from time import monotonic

from backup_names import BACKUP_CLASSES, backup_class
from homecare_store import FOUNDATION as HOMECARE_FOUNDATION, default_path as homecare_default_path
from ledger import valid_mcs_db
from llm_availability import disk_floor_mb
from maintenance_errors import MaintenanceError
from mcs_util import HOME
from safe_io import atomic_write, publish_tmp, read_regular_nofollow

BACKUP_DIR = os.path.join(HOME, "data", "backups")
HOMECARE_DB = str(homecare_default_path(HOME))
BACKUP_KEEP = 3
BACKUP_OVERDUE_S = 2 * 86400
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
# gzip level 6 measured ~66 MB/s compress+read on a ledger backup; budget it
# at the copy rate so deadline estimates stay conservative.
COMPRESS_CHUNK = 1 << 20
BACKUP_CHECK_SLACK_S = 5.0  # overrun of the same-day check before a replacement


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
    """valid_mcs_db, bounded by ``deadline`` only when one is given.
    Compressed copies validate through a staged decompression."""
    with materialize_backup(path) as real:
        if deadline is None:
            return valid_mcs_db(real)
        return valid_mcs_db(real, deadline=deadline)


def homecare_migrations(db: sqlite3.Connection) -> dict[str, int] | None:
    """Highest applied migration per module, or None unless the core foundation is recorded."""
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "schema_migrations" not in tables or db.execute(
            "SELECT 1 FROM schema_migrations WHERE module='core' AND number=1 AND name=?",
            (HOMECARE_FOUNDATION,)).fetchone() is None:
        return None
    return {module: number for module, number in db.execute(
        "SELECT module, MAX(number) FROM schema_migrations GROUP BY module ORDER BY module")}


def _valid_homecare(path: str, deadline: float | None) -> bool:
    """A static home-care copy: quick_check ok and the core foundation migration recorded."""
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return False
    with materialize_backup(path) as real:
        return _valid_homecare_real(real, deadline)


def _valid_homecare_real(path: str, deadline: float | None) -> bool:
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro&immutable=1",
                                     uri=True)) as db:
            if deadline is not None:
                db.set_progress_handler(lambda: int(monotonic() >= deadline), 1000)
            return (db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
                    and homecare_migrations(db) is not None
                    and (deadline is None or monotonic() < deadline))
    except (OSError, sqlite3.DatabaseError):
        return False


def publish_backup(db_path: str, tmp: str, dest: str,
                    deadline: float | None = None, *, valid=_valid) -> None:
    """Copy, verify and publish a backup while owning its staging file.
    The copy is stepped so ``deadline`` abandons it mid-way instead of
    running past the watchdog."""
    def check_deadline(*_):
        if deadline is not None:
            if monotonic() >= deadline:
                raise MaintenanceError("backup_deadline_exceeded")
            if _ and _[0] in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                time.sleep(min(0.01, max(0, deadline - monotonic())))
                if monotonic() >= deadline:
                    raise MaintenanceError("backup_deadline_exceeded")

    def connection_budget(connection):
        if deadline is not None:
            check_deadline()
            milliseconds = int(min(5.0, max(0, deadline - monotonic())) * 1000)
            connection.execute(f"PRAGMA busy_timeout={milliseconds}")
            connection.set_progress_handler(lambda: int(monotonic() >= deadline), 1000)
    try:
        check_deadline()
        with closing(sqlite3.connect(
                Path(db_path).resolve().as_uri() + "?mode=ro", uri=True,
                **({} if deadline is None else {'timeout': min(5.0, max(0, deadline - monotonic()))}))) as src:
            with closing(sqlite3.connect(tmp, **({} if deadline is None else {'timeout': min(5.0, max(0, deadline - monotonic()))}))) as dst:
                connection_budget(src)
                connection_budget(dst)
                # one read snapshot across the steps: a write by another
                # connection would otherwise restart the copy every step
                src.execute("BEGIN")
                connection_budget(src)
                src.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
                src.backup(dst, pages=BACKUP_STEP_PAGES, progress=check_deadline,
                           **({} if deadline is None else {'sleep': 0.0}))
                connection_budget(dst)
                dst.execute("PRAGMA journal_mode=DELETE")
        if not valid(tmp, deadline):
            if deadline is not None and monotonic() >= deadline:
                raise MaintenanceError("backup_deadline_exceeded")
            raise MaintenanceError("backup_verify_failed")
        check_deadline()
        publish_tmp(tmp, dest, mode=0o600)
    except BaseException as error:
        with suppress(OSError):
            os.unlink(tmp)
        if deadline is not None and monotonic() >= deadline and isinstance(error, Exception):
            raise MaintenanceError("backup_deadline_exceeded") from None
        raise


def backup_need_mb(db_path: str) -> float:
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



def _compress_gz(src: str, dst: str, deadline: float | None = None) -> None:
    """Streaming gzip (level 6, mtime=0 as in mcs_backup) of ``src`` into
    ``dst``, private from creation; deadline-aware so a watchdog-bound copy
    can abandon mid-stream. Never compresses in place."""
    with open(src, "rb") as fin, open(dst, "xb") as fout:
        os.chmod(dst, 0o600)
        with gzip.GzipFile(filename="", mode="wb", fileobj=fout, mtime=0) as gz:
            while True:
                if deadline is not None and monotonic() >= deadline:
                    raise MaintenanceError("backup_deadline_exceeded")
                chunk = fin.read(COMPRESS_CHUNK)
                if not chunk:
                    break
                gz.write(chunk)


@contextmanager
def materialize_backup(path: str):
    """Yield a real uncompressed sqlite path for a backup copy: ``path``
    itself when uncompressed, else a private decompressed staging copy of a
    ``.gz`` backup, removed on exit. A stranded copy matches the staging
    sweep pattern, so a hard kill cannot leak it. Consumers of a backup file
    (restore, validation, loss reports) must use this rather than opening
    the stored path directly."""
    if not path.endswith(".gz"):
        yield path
        return
    fd, tmp = tempfile.mkstemp(
        prefix="materialize-", suffix=".db.tmp",
        dir=os.path.dirname(path) or BACKUP_DIR)
    try:
        with os.fdopen(fd, "wb") as fout, gzip.open(path, "rb") as fin:
            shutil.copyfileobj(fin, fout, COMPRESS_CHUNK)
        os.chmod(tmp, 0o600)
        yield tmp
    finally:
        with suppress(OSError):
            os.unlink(tmp)


def _compress_published(dest: str, deadline: float | None = None) -> str:
    """Compress one freshly published+marked backup in place: ``dest`` is
    replaced by ``dest.gz`` carrying its own .ok marker, and the uncompressed
    file is dropped. Crash-safe at every step: until the uncompressed copy is
    unlinked it remains a complete valid backup; the compressed copy reaches
    its final name only by atomic publish of a fully written stream."""
    gz_tmp = dest + ".gz.tmp"
    gz_dest = dest + ".gz"
    try:
        _compress_gz(dest, gz_tmp, deadline)
        publish_tmp(gz_tmp, gz_dest)
    except BaseException:
        with suppress(OSError):
            os.unlink(gz_tmp)
        raise
    _write_ok_marker(gz_dest)
    with suppress(OSError):
        os.unlink(dest)
    with suppress(OSError):
        os.unlink(dest + ".ok")
    return gz_dest

def daily_backup(db_path: str, *, deadline: float | None = None):
    """One VERIFIED sqlite .backup of the ledger per day — write to tmp,
    schema/quick_check, then atomic publish. A present-but-broken file must
    never block a fresh backup (Oracle B24). Returns "skipped_disk_low"
    without writing when the volume lacks room for two ledger copies above
    the disk floor — a backup must not be what fills the disk.

    ``deadline`` (time.monotonic) is the caller's hard stop, e.g. the
    watchdog kill time. Returns "skipped_deadline" without writing when
    the time left cannot cover the ledger at BACKUP_RATE_BPS: a backup
    the watchdog kills every tick would never complete (M7)."""
    outcome = _daily_copy(db_path, "ledger", _valid, deadline)
    if outcome == "published":
        prune_preupdate_backups()
        return None
    return outcome


def daily_homecare_backup(*, deadline: float | None = None):
    """The same verified daily copy of the home-care records database,
    only when that file exists; returns daily_backup's skip reasons."""
    if not os.path.isfile(HOMECARE_DB):
        return None
    outcome = _daily_copy(HOMECARE_DB, "homecare", _valid_homecare, deadline)
    return None if outcome == "published" else outcome


def _daily_copy(db_path: str, prefix: str, valid, deadline: float | None):
    """Publish today's ``<prefix>-YYYYMMDD.db`` and rotate to BACKUP_KEEP;
    "published", a skip reason, or None when today's copy is already good."""
    os.makedirs(BACKUP_DIR, mode=0o700, exist_ok=True)
    os.chmod(BACKUP_DIR, 0o700)   # PHI store: never umask-loose
    stamp = time.strftime("%Y%m%d")
    dest = os.path.join(BACKUP_DIR, f"{prefix}-{stamp}.db")
    # the stored copy is canonically compressed; a same-day uncompressed one
    # is a copy compression has not finished (or predates compression)
    stored = dest + ".gz" if os.path.isfile(dest + ".gz") else dest
    # the .ok marker (size+mtime_ns at publish time) skips the full
    # quick_check while the verified file is untouched; any change or a
    # missing marker falls back to validation (B24)
    if _verified_unchanged(stored):
        if stored == dest:
            # compression is an optimization over a complete valid copy:
            # a deadline stop only defers it to a later tick
            with suppress(MaintenanceError):
                _compress_published(dest, deadline)
        return None
    # checking today's file must leave room to replace it: a broken copy
    # whose check eats the whole budget would otherwise never be replaced
    check_by = None if deadline is None \
        else deadline - backup_time_needed_s(db_path) - BACKUP_CHECK_SLACK_S
    if valid(stored, check_by):
        # re-arm the fast path, or every later tick today re-runs the
        # full quick_check (a same-day backup made before the marker)
        _write_ok_marker(stored)
        if stored == dest:
            with suppress(MaintenanceError):
                _compress_published(dest, deadline)
        return None
    if deadline is not None and (
            deadline - monotonic() < backup_time_needed_s(db_path)):
        return "skipped_deadline"
    if shutil.disk_usage(BACKUP_DIR).free / (1024 * 1024) < backup_need_mb(db_path):
        return "skipped_disk_low"
    # private from creation; prune_leftovers sweeps a copy a hard kill strands
    fd, tmp = tempfile.mkstemp(
        prefix=f"{prefix}-{stamp}-", suffix=".db.tmp", dir=BACKUP_DIR)
    os.close(fd)
    publish_backup(db_path, tmp, dest, deadline=deadline, valid=valid)
    _write_ok_marker(dest)
    with suppress(MaintenanceError):
        _compress_published(dest, deadline)
    files = _daily_backups("daily" if prefix == "ledger" else "homecare_daily", prefix)
    for old in files[:-BACKUP_KEEP]:
        for path in (old, old + ".ok"):
            with suppress(OSError):
                os.unlink(path)
    return "published"


def _daily_backups(kind: str = "daily", prefix: str = "ledger") -> list[str]:
    """Only owned daily files contribute to rotation and freshness."""
    return sorted(path for path in glob.glob(os.path.join(BACKUP_DIR, f"{prefix}-*.db*"))
                  if backup_class(os.path.basename(path)) == kind)


def newest_backup_age_s() -> float | None:
    """Age of the newest daily backup; None when there is none."""
    stamps = []
    for path in _daily_backups():
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


def marker_matches(path: str, st: os.stat_result) -> bool:
    """True when the .ok marker records exactly this file's size and mtime."""
    return _read_ok_marker(path) == f"{st.st_size} {st.st_mtime_ns}"


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
                if backup_class(entry.name) != "daily":
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
    return out("verified" if marker_matches(path, st) else "unverified", age)


def backup_bytes_by_class() -> dict | None:
    """Bytes of regular files in data/backups by the owner that keeps them
    (daily rotation, its .ok markers, pre-update copies, patient-context
    reset copies, in-flight staging, hand-made copies). Names and stat only — no DB open. A number for
    health only: nothing here deletes. None when the scan is unbounded or
    unreadable; all zero when the directory is missing."""
    totals = dict.fromkeys(BACKUP_CLASSES, 0)
    stop = monotonic() + LOCAL_STATUS_SCAN_S
    try:
        with os.scandir(BACKUP_DIR) as entries:
            for n, entry in enumerate(entries):
                if n >= LOCAL_STATUS_SCAN_MAX or monotonic() > stop:
                    return None
                with suppress(FileNotFoundError):
                    st = entry.stat(follow_symlinks=False)
                    if stat.S_ISREG(st.st_mode):
                        totals[backup_class(entry.name)] += st.st_size
    except FileNotFoundError:
        return totals
    except OSError:
        return None
    # A scan finished past its budget is late, not a settled total.
    return None if monotonic() > stop else totals


def manual_backup_bytes() -> int | None:
    """Total bytes of regular files in data/backups that no rotation or
    prune owns (copies an operator made by hand). A number for health
    only: these are never deleted. None when the scan is unbounded or
    unreadable; 0 when the directory is missing."""
    totals = backup_bytes_by_class()
    return None if totals is None else totals["manual"]


def backup_overdue() -> bool:
    """True when the newest daily backup is older than BACKUP_OVERDUE_S.
    No backup yet is not overdue: a fresh install gets its first one on
    the next tick that has budget left."""
    age = newest_backup_age_s()
    return age is not None and age > BACKUP_OVERDUE_S


def preupdate_backup(db_path: str, *, deadline: float | None = None) -> str:
    """Verified .backup before an apply — distinct 'preupdate-' prefix
    so the daily ledger-* rotation can never evict a rollback point
    (B3). Returns the published path; an optional monotonic deadline bounds the copy."""
    return _stamped_copy(db_path, "preupdate", _valid, deadline)


def preupdate_homecare_backup(*, deadline: float | None = None) -> str | None:
    """Verified copy of the home-care records before an apply, only when that
    file exists. Returns the path or None; nothing is rotated here — the
    attempt discards its own copy when it fails and rotates when it succeeds."""
    if not os.path.isfile(HOMECARE_DB):
        return None
    return _stamped_copy(HOMECARE_DB, "homecare-preupdate", _valid_homecare, deadline)


def _homecare_preupdate_backups() -> list[str]:
    return sorted(path for path in glob.glob(os.path.join(BACKUP_DIR, "homecare-preupdate-*.db*"))
                  if backup_class(os.path.basename(path)) == "homecare_preupdate")


def discard_homecare_preupdate(path: str | None) -> None:
    """Remove the copy a failed apply attempt made, so it never takes the
    place of a copy that predates a successful update."""
    if path and os.path.abspath(path) in map(os.path.abspath, _homecare_preupdate_backups()):
        with suppress(OSError):
            os.unlink(path)


def rotate_homecare_preupdate() -> None:
    """After a successful apply, keep the newest BACKUP_KEEP home-care copies."""
    for old in _homecare_preupdate_backups()[:-BACKUP_KEEP]:
        with suppress(OSError):
            os.unlink(old)


def reset_backup(db_path: str) -> str:
    """Verified copy before a patient-context reset; its prefix keeps it out of every automatic rotation."""
    return _stamped_copy(db_path, "patient-context-reset", _valid, None)


def _stamped_copy(db_path: str, prefix: str, valid, deadline: float | None) -> str:
    """Publish one verified ``<prefix>-<time>-<random>.db``; the room and
    deadline are checked before anything is written."""
    if deadline is not None:
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise MaintenanceError("backup_deadline_invalid")
        if deadline - monotonic() < backup_time_needed_s(db_path):
            raise MaintenanceError("backup_deadline_exceeded")
    os.makedirs(BACKUP_DIR, mode=0o700, exist_ok=True)
    os.chmod(BACKUP_DIR, 0o700)
    # same room check as daily_backup: apply bails on this before quiesce
    if shutil.disk_usage(BACKUP_DIR).free / (1024 * 1024) < backup_need_mb(db_path):
        raise MaintenanceError("backup_disk_low")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    fd, tmp = tempfile.mkstemp(prefix=f"{prefix}-{stamp}-", suffix=".db.tmp", dir=BACKUP_DIR)
    os.close(fd)
    dest = tmp[:-4]
    publish_backup(db_path, tmp, dest, deadline=deadline, valid=valid)
    return dest


def prune_preupdate_backups(state_path: str | None = None) -> int:
    """Delete only preupdate-* backups NOT referenced by an actionable
    record — reference-based retention (B3): a flapping fetch loop can
    never push out the backup a rollback still needs. Actionable means
    the in-flight `applying` journal, a pending `restore_consent` hold,
    and every schema_bump `applied` entry, so successive rollbacks retain
    the DB copy for each schema boundary. Ordinary applied updates do not
    retain a separate copy."""
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
    for path in glob.glob(os.path.join(BACKUP_DIR, "preupdate-*.db*")):
        if backup_class(os.path.basename(path)) != "preupdate":
            continue
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
        with suppress(OSError):
            os.unlink(path + ".ok")
    return removed



def staging_patterns() -> list[str]:
    """Globs of the staging copies (and SQLite sidecars) a hard kill strands
    in the backup directory; prune_leftovers sweeps them once day-old."""
    return [os.path.join(BACKUP_DIR, "*.db.tmp" + suffix)
            for suffix in ("", "-journal", "-wal", "-shm")] + [
            os.path.join(BACKUP_DIR, "*.gz.tmp")]


_GENERATION_ORDER = ("daily", "preupdate", "patient_context_reset", "homecare_daily", "homecare_preupdate")


def generations() -> list[tuple[str, str, os.stat_result]]:
    """(name, kind, lstat) of the ledger's daily, pre-update and pre-reset backups and the home-care copies,
    in that kind order and newest name first within each. Names and stat only — no DB open."""
    found = []
    try:
        with os.scandir(BACKUP_DIR) as entries:
            for entry in entries:
                kind = backup_class(entry.name)
                if kind not in _GENERATION_ORDER:
                    continue
                with suppress(FileNotFoundError):   # rotated away mid-scan
                    st = entry.stat(follow_symlinks=False)
                    if stat.S_ISREG(st.st_mode):
                        found.append((entry.name, kind, st))
    except FileNotFoundError:
        return []
    found.sort(key=lambda item: item[0], reverse=True)
    return sorted(found, key=lambda item: _GENERATION_ORDER.index(item[1]))
