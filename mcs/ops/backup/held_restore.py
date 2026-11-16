"""Place a verified ledger copy into a NEW private directory under a consent hold.

Shared by the encrypted-bundle restore (``mcs_backup restore``) and the local
backup restore (``mcs_local_backup restore``). The awaiting_consent marker and
``restore.json`` are durable before ``ledger.db`` appears, and the database is
linked exclusively: no existing ledger is ever replaced or removed. Adoption
is ``mcs_restore plan -> approve -> resume``; delivery stays held.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import time
from typing import TypedDict

import backup_evidence
from mcs_util import file_sha256
import notify_spool
from safe_io import atomic_write, create_exclusive, fdopen_owned, fsync_dir

CHUNK = 65536


class Verification(TypedDict):
    v: int
    sha256: str
    plain_sha256: str
    verified_at: float
    inventory: backup_evidence.Inventory
    restore_pending: bool
    attachment_payloads_included: bool


def report(source_sha256: str, plain_sha256: str, inventory: backup_evidence.Inventory, *,
           restore_pending: bool) -> Verification:
    """Build the verification report; mcs_restore fences its exact key set."""
    return {"v": 1, "sha256": source_sha256, "plain_sha256": plain_sha256,
            "verified_at": time.time(), "inventory": inventory,
            "restore_pending": restore_pending,
            "attachment_payloads_included": False}


def copy_private(source: Path, target: Path, limit: int) -> None:
    """Bound even a changing/untrusted source; never follow symlinks or FIFOs."""
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with fdopen_owned(fd, "rb") as src:
        st = os.fstat(src.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            raise backup_evidence.BackupError("backup_copy_size_or_type_invalid")
        fd = create_exclusive(target, 0o600, nofollow=False)
        with fdopen_owned(fd, "wb") as dst:
            remaining = limit
            while chunk := src.read(min(CHUNK, remaining + 1)):
                if len(chunk) > remaining:
                    raise backup_evidence.BackupError("backup_copy_limit_exceeded")
                dst.write(chunk)
                remaining -= len(chunk)


def new_destination(destination: str, *, outside: Path) -> tuple[Path, os.stat_result]:
    """Check a not-yet-existing canonical target whose parent is private;
    return it with the parent's pinned identity."""
    target = Path(destination)
    if (not target.is_absolute() or target.resolve() != target
            or os.path.lexists(target) or target.name in ("", ".", "..")
            or target.is_relative_to(outside)):
        raise backup_evidence.BackupError("backup_restore_requires_new_local_directory")
    backup_evidence.require_private_directory(target.parent)
    return target, target.parent.stat()


def place(target: Path, pinned: os.stat_result, plain: Path, verification: Verification,
          limit: int) -> Verification:
    """Publish ``plain`` as ``target/ledger.db`` behind the consent hold."""
    parent = target.parent
    backup_evidence.require_private_directory(parent)
    current = parent.stat()
    if (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
        raise backup_evidence.BackupError("backup_restore_parent_changed")
    target.mkdir(mode=0o700)
    published = False
    staging = target / (".restore-" + os.urandom(16).hex())
    try:
        notify_spool.mark_restored(
            str(target), by="mcs_backup_restore", phase="awaiting_consent",
            report_id=verification["sha256"])
        atomic_write(str(target / "restore.json"),
                     lambda stream: json.dump(verification, stream, sort_keys=True),
                     mode=0o600)
        # Copy within the NEW destination filesystem, then link exclusively:
        # neither os.replace nor a cross-device move can overwrite a ledger.
        copy_private(plain, staging, limit)
        with staging.open("rb") as stream:
            os.fsync(stream.fileno())
        if file_sha256(staging) != verification["plain_sha256"]:
            raise backup_evidence.BackupError("backup_restore_copy_mismatch")
        os.link(staging, target / "ledger.db")
        published = True
        staging.unlink()
        fsync_dir(target)
        return verification
    except (OSError, backup_evidence.BackupError):
        # A concurrent ledger collision belongs to somebody else. Keep
        # both it and the consent hold; never delete it as our cleanup.
        if not published and os.path.lexists(target / "ledger.db"):
            staging.unlink(missing_ok=True)
        else:
            shutil.rmtree(target)
        raise
