"""Check stored attachment files against a published snapshot without changing anything."""
from __future__ import annotations

import errno
import os
import sqlite3
import stat
import sys
import time
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import _mcs_path  # noqa: F401

from ledger import LedgerReader
# mcs_repair owns the snapshot hash/binding contract shared with plan().
from mcs_repair import canonical_digest, hash_file, snapshot_binding

FORMAT = "mcs-attachment-audit/1"
# Only a confirmed absent, short/long or unreadable payload is a refetch
# candidate; anything we refused to open needs an operator's eyes first.
REFETCH = ("missing", "size_mismatch", "unreadable")
REVIEW = ("not_regular", "path_mismatch", "record_invalid")
REASONS = REFETCH + REVIEW
MAX_LIMIT = 100_000
MAX_SECONDS = 3600.0
COLUMNS = {"attachment_id", "message_id", "local_path", "bytes", "state"}
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _open_root(root):
    """Open an explicit directory one component at a time from ``/``.

    Each component is opened relative to its already-open parent with
    O_NOFOLLOW, so swapping any ancestor for a symlink mid-walk is refused
    instead of followed.
    """
    path = Path(os.path.normpath(Path(root).absolute()))
    fd = os.open("/", _DIR)
    try:
        for part in path.parts[1:]:
            child = os.open(part, _DIR, dir_fd=fd)
            os.close(fd)
            fd = child
    except OSError:
        os.close(fd)
        raise ValueError("attachment_root_refused") from None
    return path, fd


def _check(root, root_fd, row):
    """Classify one downloaded row; return (reason or None, observed bytes)."""
    aid, recorded, local = row["attachment_id"], row["bytes"], row["local_path"]
    if type(aid) is not int or aid <= 0 or type(recorded) is not int or recorded < 0 \
            or not isinstance(local, str) or not local:
        return "record_invalid", None
    # Never open a path the row does not place directly under the explicit
    # root: a different root would read as "missing" and is not ours to judge.
    if os.path.normpath(local) != str(root / str(aid)):
        return "path_mismatch", None
    try:
        fd = os.open(str(aid), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
    except FileNotFoundError:
        return "missing", None
    except OSError as e:
        return ("not_regular" if e.errno == errno.ELOOP else "unreadable"), None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return "not_regular", None
        if info.st_size != recorded:
            return "size_mismatch", info.st_size
        if recorded and not os.read(fd, 1):
            return "unreadable", info.st_size
        return None, info.st_size
    except OSError:
        return "unreadable", None
    finally:
        os.close(fd)


def audit(snapshot, attachment_root, *, limit=1000, max_seconds=30.0, after_id=0,
          clock=time.monotonic):
    """Return (summary, private) for at most ``limit`` downloaded rows.

    ``max_seconds`` bounds the SQL plus file checks, and separately each of
    the two snapshot hashes (worst case about three times ``max_seconds``).
    Running out during a hash refuses the whole result; running out in SQL or
    the scan yields a partial result whose unchecked count is None when it
    was never established. The summary carries counts and fixed
    reasons only; the private result adds ids, the continuation cursor and
    the refetch candidate plan, bound to the snapshot and root identity.
    """
    if type(limit) is not int or not 0 < limit <= MAX_LIMIT \
            or type(after_id) is not int or not 0 <= after_id < 2**63 \
            or type(max_seconds) not in (int, float) or not 0 < max_seconds <= MAX_SECONDS:
        raise ValueError("attachment_audit_bounds_invalid")
    before = hash_file(snapshot, deadline=clock() + max_seconds, clock=clock)
    scan_deadline = clock() + max_seconds
    root, root_fd = _open_root(attachment_root)
    counts = dict.fromkeys(REASONS, 0)
    candidates, checked, cursor, stop, pending, binding = [], 0, after_id, None, None, None
    try:
        info = os.fstat(root_fd)
        root_digest = canonical_digest(
            {"path": str(root), "dev": info.st_dev, "ino": info.st_ino})
        with closing(LedgerReader(str(snapshot))) as reader:
            db = reader.db
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA busy_timeout=0")
            db.execute("BEGIN")
            if db.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                raise ValueError("static_snapshot_required")
            if not COLUMNS <= {r[1] for r in db.execute("PRAGMA table_info(attachments)")}:
                raise ValueError("attachment_schema_unsupported")
            binding = snapshot_binding(db)
            db.set_progress_handler(lambda: int(clock() >= scan_deadline), 100)
            try:
                pending = db.execute(
                    "SELECT COUNT(*) FROM attachments WHERE state='downloaded' "
                    "AND attachment_id>?", (after_id,)).fetchone()[0]
                rows = db.execute(
                    "SELECT attachment_id,message_id,local_path,bytes FROM attachments "
                    "WHERE state='downloaded' AND attachment_id>? "
                    "ORDER BY attachment_id LIMIT ?", (after_id, limit)).fetchall()
            except sqlite3.OperationalError:
                if clock() < scan_deadline:
                    raise
                # interrupted by the deadline: nothing is known, not zero
                rows, stop = [], "time"
            for row in rows:
                if clock() >= scan_deadline:
                    stop = "time"
                    break
                reason, observed = _check(root, root_fd, row)
                checked += 1
                cursor = row["attachment_id"]
                if reason:
                    counts[reason] += 1
                    candidates.append({
                        "attachment_id": row["attachment_id"],
                        "message_id": row["message_id"], "reason": reason,
                        "recorded_bytes": row["bytes"], "observed_bytes": observed})
    finally:
        os.close(root_fd)
    if before != hash_file(snapshot, deadline=clock() + max_seconds, clock=clock):
        raise ValueError("snapshot_changed")
    if stop is None and checked < pending:
        stop = "limit"
    summary = {
        "format": FORMAT, "snapshot_sha256": before, "snapshot_binding": binding,
        "attachment_root_digest": root_digest, "checked": checked,
        "ok": checked - len(candidates), "counts": counts,
        "unchecked": None if pending is None else pending - checked,
        "partial": stop is not None, "stop_reason": stop,
        "refetch_candidates": sum(counts[r] for r in REFETCH),
        "review_candidates": sum(counts[r] for r in REVIEW),
        "db_changed": False, "files_changed": False, "execution_authorized": False,
        "warnings": ["downloaded_rows_only", "size_check_not_content_hash",
                     "snapshot_may_lag_live_files", "unchecked_rows_are_not_missing",
                     "single_blocking_io_call_not_interruptible"],
    }
    private = {
        **summary, "after_id": after_id,
        "next_after_id": cursor if stop is not None else None,
        "candidates": candidates,
        "plan": {
            "refetch": [c["attachment_id"] for c in candidates if c["reason"] in REFETCH],
            "operator_review": [c["attachment_id"] for c in candidates
                                if c["reason"] in REVIEW],
            # The collector never revives a 'downloaded' row, so a refetch
            # needs an owner decision and a separately approved route.
            "steps": ["confirm_root_is_the_configured_attachment_dir",
                      "rerun_with_next_after_id_until_not_partial",
                      "owner_decision_required_before_any_refetch",
                      "no_supported_route_resets_downloaded_rows"],
        },
    }
    return summary, private
