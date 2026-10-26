"""Synthetic storage regressions for writer ownership, audit and rollback copies."""
import hashlib
import json
import os
import sqlite3
from contextlib import closing

import pytest

import maintenance
from ingest_testkit import _ledger, _message
from ledger import Ledger, SCHEMA_VERSION


@pytest.mark.parametrize("transaction", ["implicit", "deferred", "savepoint"])
def test_upsert_reads_current_message_under_writer_lock_and_keeps_rollback(
        tmp_path, transaction):
    with closing(_ledger(tmp_path)) as ledger:
        ledger.save_messages([_message(1, body="synthetic original")])
        conn = ledger.db
        path = conn.execute("PRAGMA database_list").fetchone()[2]
        with closing(sqlite3.connect(path, timeout=0)) as other:
            if transaction == "deferred":
                conn.execute("BEGIN")
                conn.execute("SELECT COUNT(*) FROM messages").fetchone()
            elif transaction == "savepoint":
                conn.execute("SAVEPOINT caller")
            observed = []

            class ReadProbe:
                def __getattr__(self, name):
                    return getattr(conn, name)

                def execute(self, sql, *args):
                    if " ".join(sql.upper().split()) == "SELECT * FROM MESSAGES WHERE MESSAGE_ID=?":
                        with pytest.raises(sqlite3.OperationalError, match="locked"):
                            other.execute("UPDATE messages SET body_html='synthetic competitor' "
                                          "WHERE message_id=1")
                        other.rollback()
                        observed.append(True)
                    return conn.execute(sql, *args)

            ledger.db = ReadProbe()
            try:
                assert ledger._upsert_message(_message(1, body="synthetic edit")) == 0
            finally:
                ledger.db = conn
            assert observed == [True]
            assert conn.in_transaction
            assert [r[0] for r in conn.execute(
                "SELECT content_hash FROM message_revisions ORDER BY seq")] == [
                    hashlib.sha256(body.encode()).hexdigest()
                    for body in ("synthetic original", "synthetic edit")]
            if transaction == "savepoint":
                conn.execute("ROLLBACK TO caller")
                conn.execute("RELEASE caller")
            else:
                conn.rollback()
            assert conn.execute("SELECT body_html FROM messages").fetchone()[0] == "synthetic original"
            assert conn.execute("SELECT COUNT(*) FROM message_revisions").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM artifacts WHERE kind='inference_dependency_loss'").fetchone()[0] == 0


@pytest.mark.parametrize("project_id", [1, 2])
def test_replace_invalidates_parent_audit_only_when_relation_can_change(tmp_path, project_id):
    path = tmp_path / "ledger.db"
    with closing(Ledger(str(path))) as ledger:
        ledger.save_messages([_message(1)])
        ledger.artifact_add("synthetic", "{}", message_id=1, project_id=1)
        with ledger.db:
            ledger.db.execute("INSERT INTO attachments(message_id) VALUES(1)")
    # An independent SQLite writer uses the default recursive_triggers=off.
    with closing(sqlite3.connect(path)) as raw, raw:
        assert raw.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        raw.execute("INSERT OR REPLACE INTO messages(message_id,project_id) VALUES(1,?)",
                    (project_id,))
        assert raw.execute("SELECT COUNT(*) FROM ledger_relation_audit").fetchone()[0] == (
            2 if project_id == 1 else 0)
    with closing(Ledger(str(path))) as ledger:
        guards = {row[0]: tuple(row[1:]) for row in ledger.db.execute(
            "SELECT relation,mode,existing_count FROM ledger_relation_guards")}
        assert guards["artifacts"] == (("enforce", 0) if project_id == 1 else ("shadow", 1))
        assert guards["attachments"] == ("enforce", 0)
        assert ledger.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_existing_schema_installs_missing_replace_guard_on_reopen(tmp_path):
    path = tmp_path / "ledger.db"
    with closing(Ledger(str(path))) as ledger, ledger.db:
        ledger.db.execute("DROP TRIGGER relation_audit_messages_replace")
    with closing(Ledger(str(path))) as ledger:
        assert ledger.db.execute("SELECT name FROM sqlite_master WHERE type='trigger' "
                                 "AND name='relation_audit_messages_replace'").fetchone()
        assert ledger.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_prune_retains_each_schema_boundary_but_releases_ordinary_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(tmp_path))
    records = []
    for number, bump in enumerate((True, True, False)):
        backup = tmp_path / f"preupdate-{number}.db"
        backup.write_bytes(b"synthetic backup")
        os.utime(backup, (1, 1))
        records.append({"schema_bump": bump, "backup_path": str(backup)})
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"applied": records}), encoding="utf-8")
    assert maintenance.prune_preupdate_backups(str(state)) == 1
    assert [os.path.exists(record["backup_path"]) for record in records] == [True, True, False]
