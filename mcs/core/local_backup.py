#!/usr/bin/env python3
"""Local ledger backups: the verified daily rotation and pre-update rollback copies.

Every copy is a stepped sqlite ``.backup`` into a private staging file, a
schema/quick_check, then an atomic publish into ``data/backups`` (0700).
Health reads only names, stat and the ``.ok`` marker; nothing here opens
the live ledger for writing. Encrypted off-device bundles are ``mcs_backup``.
"""
import glob
import json
import math
import os
import shutil
import sqlite3
import stat
import tempfile
import time
from contextlib import closing, suppress
from pathlib import Path
from time import monotonic

from backup_names import BACKUP_CLASSES, backup_class
from ledger import valid_mcs_db
from llm_availability import disk_floor_mb
from maintenance_errors import MaintenanceError
from mcs_util import HOME
from safe_io import atomic_write, publish_tmp, read_regular_nofollow

BACKUP_DIR = os.path.join(HOME, "data", "backups")
BACKUP_KEEP = 7
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
    """valid_mcs_db, bounded by ``deadline`` only when one is given."""
    if deadline is None:
        return valid_mcs_db(path)
    return valid_mcs_db(path, deadline=deadline)


def publish_backup(db_path: str, tmp: str, dest: str,
                    deadline: float | None = None) -> None:
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
        if not _valid(tmp, deadline):
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
    if shutil.disk_usage(BACKUP_DIR).free / (1024 * 1024) < backup_need_mb(db_path):
        return "skipped_disk_low"
    # private from creation; prune_leftovers sweeps a copy a hard kill strands
    fd, tmp = tempfile.mkstemp(
        prefix=f"ledger-{stamp}-", suffix=".db.tmp", dir=BACKUP_DIR)
    os.close(fd)
    publish_backup(db_path, tmp, dest, deadline=deadline)
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
    return out("verified" if marker_matches(path, st) else "unverified", age)


def backup_bytes_by_class() -> dict | None:
    """Bytes of regular files in data/backups by the owner that keeps them
    (daily rotation, its .ok markers, pre-update copies, in-flight staging,
    hand-made copies). Names and stat only — no DB open. A number for
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
    fd, tmp = tempfile.mkstemp(
        prefix=f"preupdate-{stamp}-", suffix=".db.tmp", dir=BACKUP_DIR)
    os.close(fd)
    dest = tmp[:-4]
    if deadline is None:
        publish_backup(db_path, tmp, dest)
    else:
        publish_backup(db_path, tmp, dest, deadline=deadline)
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



def staging_patterns() -> list[str]:
    """Globs of the staging copies (and SQLite sidecars) a hard kill strands
    in the backup directory; prune_leftovers sweeps them once day-old."""
    return [os.path.join(BACKUP_DIR, "*.db.tmp" + suffix)
            for suffix in ("", "-journal", "-wal", "-shm")]


def generations() -> list[tuple[str, str, os.stat_result]]:
    """(name, kind, lstat) of the regular daily then pre-update backups,
    newest name first within each kind. Names and stat only — no DB open."""
    found = []
    try:
        with os.scandir(BACKUP_DIR) as entries:
            for entry in entries:
                kind = backup_class(entry.name)
                if kind not in ("daily", "preupdate"):
                    continue
                with suppress(FileNotFoundError):   # rotated away mid-scan
                    st = entry.stat(follow_symlinks=False)
                    if stat.S_ISREG(st.st_mode):
                        found.append((entry.name, kind, st))
    except FileNotFoundError:
        return []
    found.sort(key=lambda item: item[0], reverse=True)
    return sorted(found, key=lambda item: item[1] != "daily")
