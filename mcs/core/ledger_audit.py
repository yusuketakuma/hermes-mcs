"""Bounded count-only integrity audit of an explicit static SQLite ledger."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Literal, TypedDict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _mcs_path  # noqa: F401

from ledger import LedgerReader, SCHEMA_VERSION


class AuditCheck(TypedDict):
    status: Literal["ok", "violation", "unknown"]
    count: int | None


class AuditReport(TypedDict):
    ok: bool
    complete: bool
    schema_version: int | None
    checks: dict[str, AuditCheck]


class GuardRelation(TypedDict):
    relation: str
    mode: str
    existing_count: int
    shadow_count: int


class GuardReport(TypedDict):
    state: Literal["known", "unknown"]
    relations: list[GuardRelation]


def guard_status(db: sqlite3.Connection) -> GuardReport:
    """Read bounded initialization/committed-shadow evidence, not a new audit."""
    unknown: GuardReport = {"state": "unknown", "relations": []}
    try:
        rows = db.execute(
            "SELECT relation,mode,existing_count,shadow_count FROM ledger_relation_guards "
            "WHERE relation IN ('artifacts','attachments') ORDER BY relation").fetchall()
    except sqlite3.Error:
        return unknown
    if len(rows) != 2:
        return unknown
    relations: list[GuardRelation] = []
    for relation, mode, existing, shadow in rows:
        if (mode not in ("shadow", "enforce") or type(existing) is not int
                or type(shadow) is not int or existing < 0 or shadow < 0):
            return unknown
        relations.append({"relation": relation, "mode": mode,
                          "existing_count": existing, "shadow_count": shadow})
    return {"state": "known", "relations": relations}


# Source columns that recovery cannot reconstruct (valid_mcs_db contract).
_SOURCE = {
    "runs": "run_id started_at finished_at snapshot_ts status error",
    "patients": "project_id project_type patient_name disease station_name url last_seen",
    "messages": "message_id project_id parent_id sender_id sender_name sender_type "
                "profession organization posted_at body_html reply_count first_seen",
}
_BASE = {
    "attachments": "message_id file_id name url",
    "read_marks": "project_id snapshot_ts marked_at",
    "notify_outbox": "event_id kind project_id payload state attempts next_try "
                     "accepted_ref created_at updated_at",
    "artifacts": "artifact_id kind project_id message_id content model meta created_at",
    "fetch_jobs": "job_id kind project_id message_id parent_id payload state attempts "
                  "next_try created_at updated_at",
    "message_metadata": "message_id source content checked_at last_error",
}
# Dependencies are fixed identifiers, never input or names read from the DB.
_COUNTS = (
    ("attachment_message_missing", {"attachments": "message_id", "messages": "message_id"},
     "SELECT COUNT(*) FROM attachments a WHERE NOT EXISTS "
     "(SELECT 1 FROM messages m WHERE m.message_id=a.message_id)"),
    ("artifact_message_missing", {"artifacts": "message_id", "messages": "message_id"},
     "SELECT COUNT(*) FROM artifacts a WHERE a.message_id IS NOT NULL AND NOT EXISTS "
     "(SELECT 1 FROM messages m WHERE m.message_id=a.message_id)"),
    ("artifact_project_mismatch",
     {"artifacts": "message_id project_id", "messages": "message_id project_id"},
     "SELECT COUNT(*) FROM artifacts a JOIN messages m ON m.message_id=a.message_id "
     "WHERE a.project_id IS NOT NULL AND a.project_id IS NOT m.project_id"),
    ("attachment_key_duplicate", {"attachments": "message_id file_id"},
     "SELECT COUNT(*) FROM (SELECT 1 FROM attachments GROUP BY message_id,file_id "
     "HAVING COUNT(*)>1)"),
    ("read_mark_key_duplicate", {"read_marks": "project_id snapshot_ts"},
     "SELECT COUNT(*) FROM (SELECT 1 FROM read_marks GROUP BY project_id,snapshot_ts "
     "HAVING COUNT(*)>1)"),
    ("message_body_state_invalid", {"messages": "body_state"},
     "SELECT COUNT(*) FROM messages WHERE body_state IS NOT NULL AND body_state "
     "NOT IN ('snippet','full','unknown','deleted')"),
    ("message_reply_count_invalid", {"messages": "reply_count"},
     "SELECT COUNT(*) FROM messages WHERE reply_count IS NOT NULL AND "
     "(typeof(reply_count)!='integer' OR reply_count<0)"),
)
# No FK-like requirements for queue IDs, parents, requests, receipts or restore
# holds: these can legitimately refer to rows that have not been fetched or
# no longer exist. Stale artifact hashes are also not structural corruption.


def audit_db(path: str | Path, *, max_steps: int = 10_000_000) -> AuditReport:
    """Audit without a writer, migration, config lookup or data-bearing output.

    Uses LedgerReader's mode=ro connection and one read transaction. Only static
    journal=DELETE candidates are supported; live WAL databases are unknown.
    max_steps is the audit-query VM budget (100-step granularity); incomplete
    checks have count=None, never a misleading zero. Integrity count is 0/1
    (failure presence), foreign-key count is violating rows, duplicate counts
    are duplicate key groups; other counts are violating rows. This is not a
    repair plan or a claim that ingestion/history is complete. Reader setup uses
    its existing 30-second SQLite timeout; audit queries never wait on locks.
    SQLite's readonly integrity_check does not verify every CHECK constraint;
    the explicit queries below cover only the evidenced stable invariants.
    """
    if type(max_steps) is not int or not 100 <= max_steps <= 1_000_000_000:
        raise ValueError("audit_budget_invalid")
    checks: dict[str, AuditCheck] = {}
    report: AuditReport = {
        "ok": False, "complete": False, "schema_version": None, "checks": checks,
    }

    def record(code: str, count: int | None) -> None:
        checks[code] = {"status": "unknown" if count is None else
                        "violation" if count else "ok", "count": count}

    remaining = max_steps
    finished = False

    def progress() -> int:
        nonlocal remaining
        remaining -= 100
        return int(remaining < 0)

    try:
        if not Path(path).is_file():
            record("db_unavailable", None)
            return report
        with closing(LedgerReader(str(path))) as reader:
            db = reader.db
            db.set_progress_handler(progress, 100)
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA busy_timeout=0")
            db.execute("BEGIN")
            if db.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                record("static_database_required", None)
                return report
            version = db.execute("PRAGMA user_version").fetchone()[0]
            report["schema_version"] = version
            # Never expose PRAGMA error strings, table names, rowids or IDs.
            record("sqlite_integrity", int(
                db.execute("PRAGMA integrity_check(1)").fetchone()[0] != "ok"))
            if checks["sqlite_integrity"]["count"]:
                return report
            if not 0 <= version <= SCHEMA_VERSION:
                record("schema_unsupported", None)
                return report
            record("migration_interrupted", db.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                "AND name IN ('attachments_v1','read_marks_v1')").fetchone()[0])
            columns = {}
            for table in (*_SOURCE, *_BASE):
                columns[table] = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
            for table, fields in {**_SOURCE, **_BASE}.items():
                required = set(fields.split())
                mandatory = table in _SOURCE or (
                    version >= 5 and table != "message_metadata") or (
                    version >= 8 and table == "message_metadata")
                if table == "attachments" and columns[table]:
                    required.update(
                        "attachment_id local_path bytes sha256 state downloaded_at created_at"
                        .split() if "attachment_id" in columns[table] else
                        "downloaded_path first_seen".split())
                if table == "read_marks" and "id" in columns[table]:
                    required.add("status")
                record(f"schema_{table}", 0 if required <= columns[table] else
                       1 if mandatory or columns[table] else None)
            record("sqlite_foreign_key", db.execute(
                "SELECT COUNT(*) FROM pragma_foreign_key_check").fetchone()[0])
            for code, dependencies, sql in _COUNTS:
                if not all(set(fields.split()) <= columns[table]
                           for table, fields in dependencies.items()):
                    record(code, None)
                    continue
                record(code, db.execute(sql).fetchone()[0])
            for table in ("attachments", "notify_outbox", "fetch_jobs"):
                code = f"{table}_attempts_invalid"
                record(code, db.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE attempts IS NOT NULL AND "
                    "(typeof(attempts)!='integer' OR attempts<0)").fetchone()[0]
                    if "attempts" in columns[table] else None)
            finished = True
    except (OSError, ValueError):
        record("db_unavailable", None)
    except sqlite3.DatabaseError as error:
        code = getattr(error, "sqlite_errorcode", None)
        if remaining < 0:
            record("audit_budget_exceeded", None)
        # Python 3.10 has no code metadata: only its undifferentiated
        # DatabaseError is unreadable; operational/subclass errors stay unknown.
        elif (code is None and type(error) is sqlite3.DatabaseError) or (
            code is not None and code in (
                getattr(sqlite3, "SQLITE_CORRUPT", None),
                getattr(sqlite3, "SQLITE_NOTADB", None),
            )
        ):
            record("sqlite_unreadable", 1)
        else:
            record("sqlite_query_unavailable", None)
    report["complete"] = finished and all(
        c["status"] != "unknown" for c in checks.values())
    report["ok"] = report["complete"] and all(
        c["status"] == "ok" for c in checks.values())
    return report


def main(argv: list[str] | None = None) -> int:
    """Print safe JSON; exit 0 for a complete clean audit, otherwise 1."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--max-steps", type=int, default=10_000_000)
    args = parser.parse_args(argv)
    if not 100 <= args.max_steps <= 1_000_000_000:
        parser.error("audit_budget_invalid")
    report = audit_db(args.db, max_steps=args.max_steps)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
