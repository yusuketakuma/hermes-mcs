#!/usr/bin/env python3
"""List, fully verify and held-restore the local ledger backups and the home-care records copies.

``list`` reads names, stat and the verification marker only; ``--verify``
adds a readonly integrity_check, foreign_key_check, schema contract and
SHA-256 per generation. ``restore`` copies one explicit backup into a NEW
private directory with the awaiting_consent hold before the database, exactly
like the encrypted-bundle restore. Nothing here replaces or deletes the live
ledger or any backup, starts services, or releases delivery: adoption is
``mcs_restore.py plan -> approve -> resume`` and delivery stays ``hold_all``.
A home-care records copy (``homecare-*``) is checked for its own schema and
placed as ``homecare.db`` in a NEW directory without a hold; adopting it is
stopping the local chat screen and moving that file into ``data/`` by hand.
"""
from __future__ import annotations

import argparse
from contextlib import closing
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
from backup_names import backup_class  # noqa: E402
import held_restore  # noqa: E402
from llm_availability import disk_floor_mb  # noqa: E402
import local_backup  # noqa: E402
from mcs_util import file_sha256, has_sqlite_sidecars  # noqa: E402

_NAME = re.compile(r"(?:ledger|homecare)-\d{8}\.db(?:\.gz)?"
                   r"|(?:preupdate|patient-context-reset)-[0-9A-Za-z_-]+\.db(?:\.gz)?"
                   r"|homecare-preupdate-\d{8}-\d{6}-[0-9A-Za-z_]+\.db(?:\.gz)?")
_HOMECARE = ("homecare_daily", "homecare_preupdate")


def list_backups(*, verify: bool = False) -> list[dict]:
    """One row per generation; ``verify`` opens each copy readonly."""
    rows = []
    for name, kind, st in local_backup.generations():
        verified = local_backup.marker_matches(os.path.join(local_backup.BACKUP_DIR, name), st)
        row = {"name": name, "kind": kind, "bytes": st.st_size, "modified_at": st.st_mtime,
               "marker": "verified" if verified else "unverified"}
        if verify:
            check = _verify_homecare if kind in _HOMECARE else _verify
            row.update(check(Path(local_backup.BACKUP_DIR) / name))
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


def _verify_homecare(path: Path) -> dict:
    try:
        if has_sqlite_sidecars(path):
            raise backup_evidence.BackupError("backup_static_snapshot_required")
        before = file_sha256(path)
        migrations = _homecare_inventory(path)
        if file_sha256(path) != before:
            raise backup_evidence.BackupError("backup_source_changed")
    except backup_evidence.BackupError as exc:
        return {"check": backup_evidence.failure_code(exc)}
    except (OSError, sqlite3.Error, ValueError):
        return {"check": "backup_unreadable"}
    return {"check": "ok", "sha256": before, "migrations": migrations}


def _homecare_inventory(path: Path) -> dict[str, int]:
    """Readonly integrity_check and foreign_key_check of a static home-care
    copy; the applied migrations when it is one."""
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1",
                                 uri=True)) as db:
        if (db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
                or db.execute("PRAGMA foreign_key_check").fetchone() is not None):
            raise backup_evidence.BackupError("backup_integrity_failed")
        migrations = local_backup.homecare_migrations(db)
    if migrations is None:
        raise backup_evidence.BackupError("backup_homecare_schema_invalid")
    return migrations


def _source(name: str) -> Path:
    """Only a static backup the local rotation or updater wrote, by name."""
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        raise backup_evidence.BackupError("backup_local_name_invalid")
    source = Path(local_backup.BACKUP_DIR) / name
    if (source.is_symlink() or not source.is_file()
            or has_sqlite_sidecars(source)):
        raise backup_evidence.BackupError("backup_static_snapshot_required")
    return source


def restore(name: str, *, destination: str) -> dict:
    """Verify a frozen copy of one local backup, then publish it into a NEW directory
    (held for the ledger, plain ``homecare.db`` for the home-care records)."""
    source = _source(name)
    homecare = backup_class(name) in _HOMECARE
    target, pinned = held_restore.new_destination(
        destination, outside=Path(local_backup.BACKUP_DIR).resolve())
    with local_backup.materialize_backup(str(source)) as real:
        size = os.path.getsize(real)
        if shutil.disk_usage(target.parent).free / (1024 * 1024) < (
                2 * size / (1024 * 1024) + disk_floor_mb()):
            raise backup_evidence.BackupError("backup_restore_disk_low")
        with tempfile.TemporaryDirectory(prefix=".mcs-local-restore-", dir=target.parent) as work:
            plain = Path(work) / ("homecare.db" if homecare else "ledger.db")
            held_restore.copy_private(real, plain, size)
            source_sha256 = file_sha256(plain)
            if file_sha256(real) != source_sha256:
                raise backup_evidence.BackupError("backup_source_changed")
            if homecare:
                migrations = _homecare_inventory(plain)
                held_restore.publish_new(target, pinned, plain, "homecare.db", source_sha256, size)
                return {"v": 1, "sha256": source_sha256, "migrations": migrations,
                        "database": str(target / "homecare.db")}
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
