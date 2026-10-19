"""The recovery gate, the bounded backup inventory and the audit apply one
static-DB schema contract. Synthetic temp databases only."""
import sqlite3

import ledger_schema
from contextlib import closing

import pytest

import ledger
import ledger_audit
import mcs_backup

SOURCE_DDL = """
CREATE TABLE runs(run_id, started_at, finished_at, snapshot_ts, status, error);
CREATE TABLE patients(project_id, project_type, patient_name, disease,
                      station_name, url, last_seen);
CREATE TABLE messages(message_id, project_id, parent_id, sender_id, sender_name,
                      sender_type, profession, organization, posted_at,
                      body_html, reply_count, first_seen);
"""
NEW_ATTACHMENTS = ("CREATE TABLE attachments(attachment_id, message_id, file_id, name, url, "
                   "local_path, bytes, sha256, state, downloaded_at, created_at);")
LEGACY_ATTACHMENTS = ("CREATE TABLE attachments(message_id, file_id, name, url, "
                      "downloaded_path, first_seen);")


def _static(path, ddl, version=0):
    with closing(sqlite3.connect(path)) as db:
        db.executescript(ddl)
        db.execute(f"PRAGMA user_version={version}")
        db.execute("PRAGMA journal_mode=DELETE")
        db.commit()
    return str(path)


def _current(tmp_path):
    source = tmp_path / "live.db"
    writer = ledger.Ledger(str(source))
    writer.ensure_patient(1)
    writer.close()
    dest = tmp_path / "current.db"
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(dest)) as dst:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
    return str(dest)


def _backup_accepts(path):
    with closing(sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)) as db:
        try:
            mcs_backup._bounded_inventory_schema(db)
        except mcs_backup.BackupError:
            return False
    return True


def _audit_accepts_schema(path):
    checks = ledger_audit.audit_db(path)["checks"]
    return (checks.get("migration_interrupted", {}).get("count") == 0
            and "schema_unsupported" not in checks
            and all(c["count"] != 1 for code, c in checks.items()
                    if code.startswith("schema_")))


CASES = {
    "source_only_v0": (SOURCE_DDL, 0, True),
    "legacy_attachments": (SOURCE_DDL + LEGACY_ATTACHMENTS, 0, True),
    "legacy_attachments_missing_first_seen": (
        SOURCE_DDL + LEGACY_ATTACHMENTS.replace(", first_seen", ""), 0, False),
    "new_attachments_missing_sha256": (
        SOURCE_DDL + NEW_ATTACHMENTS.replace(" sha256,", ""), 0, False),
    "read_marks_id_requires_status": (
        SOURCE_DDL + "CREATE TABLE read_marks(id, project_id, snapshot_ts, marked_at);", 0, False),
    "read_marks_with_status": (
        SOURCE_DDL + "CREATE TABLE read_marks(id, project_id, snapshot_ts, marked_at, status);",
        0, True),
    "source_column_missing": (SOURCE_DDL.replace(", error)", ")"), 0, False),
    "v5_without_v5_tables": (SOURCE_DDL, 5, False),
    "interrupted_migration": (SOURCE_DDL + "CREATE TABLE attachments_v1(x);", 0, False),
    "future_version": (SOURCE_DDL, ledger.SCHEMA_VERSION + 1, False),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_gate_inventory_and_audit_agree_on_schema(tmp_path, case):
    ddl, version, expected = CASES[case]
    path = _static(tmp_path / "candidate.db", ddl, version)
    assert ledger.valid_mcs_db(path) is expected
    assert _backup_accepts(path) is expected
    assert _audit_accepts_schema(path) is expected


def test_current_schema_passes_every_consumer(tmp_path):
    path = _current(tmp_path)
    assert ledger.valid_mcs_db(path)
    assert _backup_accepts(path)
    assert _audit_accepts_schema(path)
    assert ledger_schema.static_required_tables(ledger.SCHEMA_VERSION) <= {
        *ledger_schema.STATIC_SOURCE_COLUMNS, *ledger_schema.STATIC_BASE_COLUMNS}


@pytest.mark.parametrize("ddl, fenced", [
    (NEW_ATTACHMENTS + "INSERT INTO attachments(attachment_id,message_id,file_id) "
     "VALUES (1,1,1),(2,1,1);", True),
    (LEGACY_ATTACHMENTS + "INSERT INTO attachments(message_id,file_id) VALUES (1,1),(1,1);",
     False),
])
def test_key_fence_applies_only_with_marker_column(tmp_path, ddl, fenced):
    path = _static(tmp_path / "candidate.db", SOURCE_DDL + ddl)
    assert ledger.valid_mcs_db(path) is not fenced
    assert _backup_accepts(path) is not fenced
    writer_path = tmp_path / "writer.db"
    writer_path.write_bytes((tmp_path / "candidate.db").read_bytes())
    if fenced:
        with pytest.raises(ledger.MigrationError, match="duplicate attachment keys"):
            ledger.Ledger(str(writer_path))
