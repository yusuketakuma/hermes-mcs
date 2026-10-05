#!/usr/bin/env python3
"""Human consent for adopting a new-terminal backup, with all delivery held.

plan -> approve -> resume are separate process-safe steps. The local account
is the trust boundary, as for command_receipts: this is not authentication
against a compromised operator account. Approval never means clinical
verification, journal reconciliation, notification replay or service start.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import sys
from typing import TypedDict
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _mcs_path  # noqa: E402,F401
import mcs_backup  # noqa: E402
from mcs_requests import (  # noqa: E402
    _text, canonical, parse_command, payload_hash, valid_hash, valid_uuid,
)

PURPOSE = "ops.backup_restore_resume"
APPROVAL = "backup_restore_approval.json"
RESUMED = "backup_restore_resume.json"
BLOCKED = "backup_restore_blocked.json"
MARKER = "restore_pending.json"


class Binding(TypedDict):
    purpose: str
    destination: str
    destination_identity: dict[str, int]
    database_identity: dict[str, int]
    source_sha256: str
    plain_sha256: str
    report_sha256: str
    report_identity: dict[str, int]
    marker_sha256: str
    marker_identity: dict[str, int]


class Plan(TypedDict):
    binding: Binding
    plan_sha256: str


class ApprovalResult(TypedDict):
    receipt_sha256: str
    command_id: str


class ResumeResult(TypedDict):
    writers_resumed: bool
    delivery_policy: str
    command_id: str


class RestoreConsentError(ValueError):
    """A fail-closed reason token without private paths or database contents."""


def _identity(st):
    return {"device": st.st_dev, "inode": st.st_ino}


def _stamp(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


@contextmanager
def _directory(destination: str, *, lock=False):
    path = Path(destination)
    mcs_backup._private_directory(path)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    lock_fd = None
    try:
        if lock:
            lock_fd = os.open("run.lock", os.O_WRONLY | os.O_CREAT
                              | os.O_NOFOLLOW | os.O_NONBLOCK,
                              0o600, dir_fd=fd)
            _private_file(os.fstat(lock_fd))
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _same_directory(path, fd)
        yield path, fd
        _same_directory(path, fd)
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(fd)


def _same_directory(path, fd):
    mcs_backup._private_directory(path)
    if _identity(path.stat()) != _identity(os.fstat(fd)):
        raise RestoreConsentError("restore_destination_replaced")


def _private_file(st):
    if (not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) != 0o600
            or st.st_uid != os.getuid() or st.st_nlink != 1):
        raise RestoreConsentError("restore_private_file_required")


def _read(fd, name, *, database=False):
    opened = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=fd)
    with os.fdopen(opened, "rb") as stream:
        before = os.fstat(stream.fileno())
        _private_file(before)
        digest = hashlib.sha256()
        raw = b""
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
            if not database:
                raw += chunk
                if len(raw) > 65536:
                    raise RestoreConsentError("restore_record_too_large")
        after = os.fstat(stream.fileno())
        current = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if (_stamp(before) != _stamp(after) or _stamp(current) != _stamp(after)):
            raise RestoreConsentError("restore_file_changed")
    return (None if database else parse_command(raw),
            digest.hexdigest(), _identity(after))


def _binding(path, fd, source_sha256, *, static=True) -> Binding:
    if not valid_hash(source_sha256):
        raise RestoreConsentError("restore_source_sha256_required")
    report, report_hash, report_identity = _read(fd, "restore.json")
    marker, marker_hash, marker_identity = _read(fd, MARKER)
    if (not isinstance(report, dict) or set(report) != {
            "v", "sha256", "plain_sha256", "verified_at", "inventory",
            "restore_pending", "attachment_payloads_included"}
            or type(report["v"]) is not int or report["v"] != 1
            or report["sha256"] != source_sha256
            or not valid_hash(report["plain_sha256"])
            or report["restore_pending"] is not True
            or report["attachment_payloads_included"] is not False
            or type(report["verified_at"]) not in (int, float)
            or not math.isfinite(report["verified_at"])
            or report["verified_at"] <= 0
            or not isinstance(marker, dict) or set(marker) != {
                "v", "phase", "backup_path", "by", "at", "report_id"}
            or type(marker.get("v")) is not int or marker["v"] != 1
            or type(marker["at"]) not in (int, float)
            or not math.isfinite(marker["at"]) or marker["at"] <= 0
            or marker.get("by") != "mcs_backup_restore"
            or marker.get("phase") != "awaiting_consent"
            or marker.get("backup_path") is not None
            or marker.get("report_id") != source_sha256):
        raise RestoreConsentError("restore_metadata_mismatch")
    if static:
        for suffix in ("-wal", "-shm", "-journal"):
            try:
                os.stat("ledger.db" + suffix, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise RestoreConsentError("restore_static_database_required")
        _, digest, db_identity = _read(fd, "ledger.db", database=True)
        if digest != report["plain_sha256"]:
            raise RestoreConsentError("restore_database_hash_mismatch")
        if mcs_backup._inventory(path / "ledger.db") != report["inventory"]:
            raise RestoreConsentError("restore_inventory_mismatch")
    else:
        st = os.stat("ledger.db", dir_fd=fd, follow_symlinks=False)
        _private_file(st)
        db_identity = _identity(st)
    _same_directory(path, fd)
    return {
        "purpose": PURPOSE, "destination": str(path),
        "destination_identity": _identity(os.fstat(fd)),
        "database_identity": db_identity,
        "source_sha256": source_sha256,
        "plain_sha256": report["plain_sha256"],
        "report_sha256": report_hash, "report_identity": report_identity,
        "marker_sha256": marker_hash, "marker_identity": marker_identity,
    }


def plan(destination: str, *, source_sha256: str) -> Plan:
    """Read the exact static restoration to be reviewed, without writing it."""
    with _directory(destination) as (path, fd):
        binding = _binding(path, fd, source_sha256)
        return {"binding": binding, "plan_sha256": payload_hash(binding)}


def _human(confirm_human, actor, reason, custody_ref, delivery_policy):
    if (confirm_human is not True or not _text(actor, 120)
            or not _text(reason, 2000) or not _text(custody_ref, 1000)
            or delivery_policy != "hold_all"):
        raise RestoreConsentError("restore_explicit_human_policy_required")


def _publish(fd, name, value):
    # Fsynced bytes -> exclusive link -> directory fsync. Neither a partial
    # receipt nor an overwrite is visible; use the already pinned directory.
    temporary = ".backup-consent-" + uuid.uuid4().hex
    opened = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    try:
        with os.fdopen(opened, "wb") as stream:
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, name, src_dir_fd=fd, dst_dir_fd=fd,
                follow_symlinks=False)
    finally:
        os.unlink(temporary, dir_fd=fd)
    os.fsync(fd)


def approve(destination: str, *, source_sha256: str, plan_sha256: str,
            confirm_human: bool, actor: str, reason: str, custody_ref: str,
            delivery_policy: str) -> ApprovalResult:
    """Commit a dedicated human receipt; do not release even the writer hold."""
    _human(confirm_human, actor, reason, custody_ref, delivery_policy)
    with _directory(destination, lock=True) as (path, fd):
        binding = _binding(path, fd, source_sha256)
        if payload_hash(binding) != plan_sha256:
            raise RestoreConsentError("restore_plan_changed")
        command_id = str(uuid.uuid4())
        receipt = {
            "version": 1, "cmd": PURPOSE, "command_id": command_id,
            "human_confirmed": True, "actor": actor, "reason": reason,
            "custody_ref": custody_ref, "delivery_policy": delivery_policy,
            "binding": binding, "plan_sha256": plan_sha256,
            "outcome": "applied",
        }
        _publish(fd, APPROVAL, receipt)
        if _binding(path, fd, source_sha256) != binding:
            raise RestoreConsentError("restore_changed_during_approval")
        return {"receipt_sha256": payload_hash(receipt),
                "command_id": command_id}


def _receipt(fd, receipt_sha256):
    receipt, digest, _ = _read(fd, APPROVAL)
    if (not valid_hash(receipt_sha256) or digest != receipt_sha256
            or not isinstance(receipt, dict) or set(receipt) != {
                "version", "cmd", "command_id", "human_confirmed", "actor",
                "reason", "custody_ref", "delivery_policy", "binding",
                "plan_sha256", "outcome"}
            or type(receipt["version"]) is not int or receipt["version"] != 1
            or receipt["cmd"] != PURPOSE or receipt["outcome"] != "applied"
            or not valid_uuid(receipt["command_id"])
            or payload_hash(receipt["binding"]) != receipt["plan_sha256"]):
        raise RestoreConsentError("restore_bound_receipt_required")
    _human(receipt["human_confirmed"], receipt["actor"], receipt["reason"],
           receipt["custody_ref"], receipt["delivery_policy"])
    return receipt


def resume(destination: str, *, receipt_sha256: str, confirm_human: bool,
           actor: str, reason: str) -> ResumeResult:
    """Validate a separately pinned receipt and permit writers, never delivery."""
    with _directory(destination, lock=True) as (path, fd):
        if os.path.lexists(path / BLOCKED):
            raise RestoreConsentError("restore_resume_blocked")
        receipt = _receipt(fd, receipt_sha256)
        _human(confirm_human, actor, reason, receipt["custody_ref"],
               receipt["delivery_policy"])
        if actor != receipt["actor"] or reason != receipt["reason"]:
            raise RestoreConsentError("restore_human_receipt_mismatch")
        activation = {"v": 1, "purpose": PURPOSE,
                      "receipt_sha256": receipt_sha256,
                      "plan_sha256": receipt["plan_sha256"]}
        if os.path.lexists(path / RESUMED):
            # A repeated resume of the same decision is idempotent; it must
            # never leave a permanent BLOCKED marker behind.
            if _read(fd, RESUMED)[0] != activation:
                raise RestoreConsentError("restore_already_resumed")
            if _binding(path, fd, receipt["binding"]["source_sha256"],
                        static=False) != receipt["binding"]:
                raise RestoreConsentError("restore_receipt_stale")
            return {"writers_resumed": True, "delivery_policy": "hold_all",
                    "command_id": receipt["command_id"]}
        binding = _binding(path, fd, receipt["binding"]["source_sha256"])
        if binding != receipt["binding"]:
            raise RestoreConsentError("restore_receipt_stale")
        # Persist the hold BEFORE publishing the grant. Only this exact bound
        # decision removes it; failures and crashes retain it for owner review.
        _publish(fd, BLOCKED, {"v": 1, "purpose": PURPOSE})
        _publish(fd, RESUMED, activation)
        if _binding(path, fd, binding["source_sha256"]) != binding:
            raise RestoreConsentError("restore_changed_during_resume")
        os.unlink(BLOCKED, dir_fd=fd)
        os.fsync(fd)
        return {"writers_resumed": True, "delivery_policy": "hold_all",
                "command_id": receipt["command_id"]}


def writers_resumed(destination: str) -> bool:
    """Validate persistent adoption while allowing subsequent normal DB writes.

    Initial bytes are checked under run.lock at consent consumption. Thereafter
    the inode, destination and original immutable evidence stay pinned; rehashing
    a live WAL database would revoke consent on its first legitimate write.
    """
    try:
        with _directory(destination) as (path, fd):
            if os.path.lexists(path / BLOCKED):
                return False
            activation, _, _ = _read(fd, RESUMED)
            if (not isinstance(activation, dict) or set(activation) != {
                    "v", "purpose", "receipt_sha256", "plan_sha256"}
                    or type(activation["v"]) is not int or activation["v"] != 1
                    or activation["purpose"] != PURPOSE):
                return False
            receipt = _receipt(fd, activation["receipt_sha256"])
            return (receipt["plan_sha256"] == activation["plan_sha256"]
                    and _binding(path, fd, receipt["binding"]["source_sha256"],
                                 static=False) == receipt["binding"])
    except (OSError, ValueError, KeyError, TypeError, RecursionError, OverflowError,
            mcs_backup.BackupError, sqlite3.Error):
        return False


def main(argv: list[str] | None = None) -> int:
    """Explicit local CLI; no ambient destination, identity or custody policy."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("plan", "approve", "resume"):
        p = sub.add_parser(action)
        p.add_argument("--destination", required=True)
        if action != "resume":
            p.add_argument("--source-sha256", required=True)
        if action != "plan":
            p.add_argument("--confirm-human", action="store_true")
            p.add_argument("--actor", required=True)
            p.add_argument("--reason", required=True)
        if action == "approve":
            p.add_argument("--plan-sha256", required=True)
            p.add_argument("--custody-ref", required=True)
            p.add_argument("--delivery-policy", choices=("hold_all",), required=True)
        if action == "resume":
            p.add_argument("--receipt-sha256", required=True)
    args = vars(parser.parse_args(argv))
    action = args.pop("action")
    try:
        result = {"plan": plan, "approve": approve, "resume": resume}[action](**args)
    except (OSError, ValueError, KeyError, TypeError, RecursionError, OverflowError,
            mcs_backup.BackupError, sqlite3.Error) as exc:
        token = str(exc) if isinstance(exc, RestoreConsentError) else "restore_validation_failed"
        print(json.dumps({"ok": False, "error": token}))
        return 1
    print(json.dumps({"ok": True, **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
