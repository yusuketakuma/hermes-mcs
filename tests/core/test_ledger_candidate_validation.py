"""Static recovery validation accepts migratable generations, not partial DBs."""

import sqlite3

import pytest

from ledger import Ledger, SCHEMA_VERSION, valid_mcs_db


@pytest.mark.parametrize("version", range(SCHEMA_VERSION + 1))
def test_migratable_older_backup_remains_valid(tmp_path, version):
    path = tmp_path / "backup.db"
    db = Ledger(str(path))
    db.ensure_patient(1)
    db.close()

    con = sqlite3.connect(path)
    if version < 5:
        # The older schema may lack tables supplied by the migration.
        for table in ("fetch_jobs", "requests", "command_receipts",
                      "snapshot_meta"):
            con.execute(f"DROP TABLE {table}")
    con.execute(f"PRAGMA user_version={version}")
    con.execute("PRAGMA journal_mode=DELETE")
    con.close()

    assert valid_mcs_db(str(path))
    migrated = Ledger(str(path))
    assert migrated.db.execute(
        "PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert migrated.db.execute(
        "SELECT COUNT(*) FROM patients").fetchone()[0] == 1
    migrated.close()


@pytest.mark.parametrize("version", [0, 4, 5, SCHEMA_VERSION])
def test_partial_three_table_database_is_not_a_recovery_candidate(
        tmp_path, version):
    path = tmp_path / "partial.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE runs(run_id INTEGER PRIMARY KEY);
        CREATE TABLE patients(project_id INTEGER PRIMARY KEY);
        CREATE TABLE messages(message_id INTEGER PRIMARY KEY);
    """)
    con.execute(f"PRAGMA user_version={version}")
    con.close()

    assert not valid_mcs_db(str(path))


def test_versioned_backup_requires_complete_base_tables(tmp_path):
    path = tmp_path / "missing-jobs.db"
    db = Ledger(str(path))
    db.close()
    con = sqlite3.connect(path)
    con.execute("DROP TABLE fetch_jobs")
    con.execute("PRAGMA journal_mode=DELETE")
    con.close()

    assert not valid_mcs_db(str(path))
