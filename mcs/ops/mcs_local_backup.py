#!/usr/bin/env python3
"""List, fully verify and held-restore the local daily, pre-update and pre-reset ledger backups.

``list`` reads names, stat and the verification marker only; ``--verify``
adds a readonly integrity_check, foreign_key_check, schema contract and
SHA-256 per generation. ``restore`` copies one explicit backup into a NEW
private directory with the awaiting_consent hold before the database, exactly
like the encrypted-bundle restore. Nothing here replaces or deletes the live
ledger or any backup, starts services, or releases delivery: adoption is
``mcs_restore.py plan -> approve -> resume`` and delivery stays ``hold_all``.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _mcs_path  # noqa: E402,F401
import backup_evidence  # noqa: E402
import held_restore  # noqa: E402
from llm_availability import disk_floor_mb  # noqa: E402
import local_backup  # noqa: E402
from mcs_util import file_sha256, has_sqlite_sidecars  # noqa: E402

_NAME = re.compile(r"ledger-\d{8}\.db|(?:preupdate|patient-context-reset)-[0-9A-Za-z_-]+\.db")


def list_backups(*, verify: bool = False) -> list[dict]:
    """One row per generation; ``verify`` opens each copy readonly."""
    rows = []
    for name, kind, st in local_backup.generations():
        verified = local_backup.marker_matches(os.path.join(local_backup.BACKUP_DIR, name), st)
        row = {"name": name, "kind": kind, "bytes": st.st_size, "modified_at": st.st_mtime,
               "marker": "verified" if verified else "unverified"}
        if verify:
            row.update(_verify(Path(local_backup.BACKUP_DIR) / name))
        rows.append(row)
    return rows


def _verify(path: Path) -> dict:
    try:
        if has_sqlite_sidecars(path):
            raise backup_evidence.BackupError("backup_static_snapshot_required")
        before = file_sha256(path)
        inventory = backup_evidence.inventory_snapshot(path)
        if file_sha256(path) != before:
            raise backup_evidence.BackupError("backup_source_changed")
    except backup_evidence.BackupError as exc:
        return {"check": backup_evidence.failure_code(exc)}
    except (OSError, sqlite3.Error, ValueError):
        return {"check": "backup_unreadable"}
    return {"check": "ok", "sha256": before, "schema": inventory["schema"],
            "last_successful_run": inventory["last_successful_run"]}


def _source(name: str) -> Path:
    """Only a static backup the local rotation or updater wrote, by name."""
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        raise backup_evidence.BackupError("backup_local_name_invalid")
    source = Path(local_backup.BACKUP_DIR) / name
    if (source.is_symlink() or not source.is_file()
            or has_sqlite_sidecars(source)):
        raise backup_evidence.BackupError("backup_static_snapshot_required")
    return source


def restore(name: str, *, destination: str) -> held_restore.Verification:
    """Verify a frozen copy of one local backup, then publish it held into a NEW directory."""
    source = _source(name)
    target, pinned = held_restore.new_destination(
        destination, outside=Path(local_backup.BACKUP_DIR).resolve())
    size = source.stat().st_size
    if shutil.disk_usage(target.parent).free / (1024 * 1024) < (
            2 * size / (1024 * 1024) + disk_floor_mb()):
        raise backup_evidence.BackupError("backup_restore_disk_low")
    with tempfile.TemporaryDirectory(prefix=".mcs-local-restore-", dir=target.parent) as work:
        plain = Path(work) / "ledger.db"
        held_restore.copy_private(source, plain, size)
        source_sha256 = file_sha256(plain)
        if file_sha256(source) != source_sha256:
            raise backup_evidence.BackupError("backup_source_changed")
        inventory = backup_evidence.inventory_snapshot(plain)
        report = held_restore.report(source_sha256, source_sha256, inventory,
                                     restore_pending=True)
        return held_restore.place(target, pinned, plain, report, size)


def main(argv: list[str] | None = None) -> int:
    """Local CLI; restore takes an explicit backup name and NEW destination."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    listing = sub.add_parser("list", help="local generations, newest first")
    listing.add_argument("--verify", action="store_true",
                         help="readonly integrity, schema and SHA-256 per generation")
    placing = sub.add_parser("restore", help="held copy into a NEW private directory")
    placing.add_argument("--backup", required=True, help="file name in data/backups")
    placing.add_argument("--destination", required=True,
                         help="NEW directory whose parent is private 0700")
    args = parser.parse_args(argv)
    try:
        if args.action == "list":
            rows = list_backups(verify=args.verify)
            print(json.dumps({"backups": rows}, sort_keys=True, allow_nan=False))
            return 1 if args.verify and any(r["check"] != "ok" for r in rows) else 0
        result = restore(args.backup, destination=args.destination)
    except backup_evidence.BackupError as exc:
        print(json.dumps({"error": backup_evidence.failure_code(exc)}))
        return 1
    except (OSError, ValueError, TypeError, sqlite3.Error, RecursionError):
        print(json.dumps({"error": "backup_io_or_policy_failed"}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
