"""Synthetic relation guards, activation audits and legacy compatibility."""
import sqlite3
from contextlib import closing

import pytest

from ledger import Ledger, SCHEMA_VERSION, valid_mcs_db
from ledger_audit import guard_status
from ingest_testkit import _message, _att, _unread_patient


@pytest.fixture
def ledger(tmp_path):
    with closing(Ledger(str(tmp_path / "ledger.db"))) as db:
        db.save_messages([_message(1), _message(2, project_id=2)])
        yield db


@pytest.mark.parametrize("foreign_keys", [0, 1])
@pytest.mark.parametrize("sql", [
    "INSERT INTO artifacts(message_id) VALUES(999)",
    "INSERT INTO artifacts(message_id,project_id) VALUES(1,2)",
    "UPDATE artifacts SET message_id=999",
    "UPDATE artifacts SET project_id=2",
    "INSERT INTO attachments(message_id) VALUES(999)",
    "INSERT INTO attachments(message_id) VALUES(NULL)",
    "UPDATE attachments SET message_id=999",
    "UPDATE attachments SET message_id=NULL",
])
def test_invalid_insert_and_related_update_rejected(ledger, tmp_path, foreign_keys, sql):
    # Given: valid children and a separate raw connection.
    ledger.artifact_add("synthetic", "{}", message_id=1, project_id=1)
    with ledger.db:
        ledger.db.execute("INSERT INTO attachments(message_id,file_id) VALUES(1,'file')")
    with closing(sqlite3.connect(tmp_path / "ledger.db")) as db:
        db.execute(f"PRAGMA foreign_keys={foreign_keys}")
        # When / Then: SQL clients cannot bypass the relation contract.
        with pytest.raises(sqlite3.IntegrityError, match="g1:"):
            db.execute(sql)


@pytest.mark.parametrize(("mid", "pid"), [(None, None), (None, 999), (1, None), (1, 1)])
def test_nullable_artifact_contract_is_retained(ledger, mid, pid):
    # Given / When: project-wide and legacy message-scoped artifacts.
    aid = ledger.artifact_add("synthetic", "{}", message_id=mid, project_id=pid)
    # Then.
    assert tuple(ledger.db.execute(
        "SELECT message_id,project_id FROM artifacts WHERE artifact_id=?", (aid,)
    ).fetchone()) == (mid, pid)


def test_artifact_project_cannot_match_unknown_message_project(ledger):
    # Given.
    with ledger.db:
        ledger.db.execute("INSERT INTO messages(message_id,project_id) VALUES(3,NULL)")
    # When / Then.
    with pytest.raises(sqlite3.IntegrityError):
        ledger.artifact_add("synthetic", "{}", message_id=3, project_id=1)


def test_real_save_paths_and_attachment_upsert(ledger):
    # Given: a root with a not-yet-fetched parent, and reply attachments.
    root = _message(3, parent_id=999)
    root.attachments = [_att("root-file")]
    reply = _message(4, parent_id=3)
    reply.attachments = [_att("reply-file")]
    root.replies = [reply]
    patient = _unread_patient(1)
    patient.messages = [root]
    # When.
    ledger.save_patient(patient)
    ledger.save_messages([root])
    ledger.artifact_add("patient_rollup", "{}", project_id=1)
    # Then.
    assert ledger.db.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 2
    assert tuple(ledger.db.execute(
        "SELECT mode,existing_count,shadow_count FROM ledger_relation_guards "
        "WHERE relation='attachments'").fetchone()) == ("enforce", 0, 0)


@pytest.mark.parametrize("relation", ["artifacts", "attachments"])
def test_dirty_relation_opens_in_shadow_without_repair(tmp_path, relation):
    # Given: deliberately disable ONLY the relation's guards to plant old damage.
    path = tmp_path / "ledger.db"
    with closing(Ledger(str(path))) as db:
        with db.db:
            for suffix in ("ins", "upd"):
                db.db.execute(f"DROP TRIGGER g1_{relation}_msg_{suffix}")
            db.db.execute(f"INSERT INTO {relation}(message_id) VALUES(999)")
    # When.
    with closing(Ledger(str(path))) as db:
        # Then: dirty historical rows remain; only that relation is shadow.
        assert tuple(db.db.execute(
            "SELECT mode,existing_count,shadow_count FROM ledger_relation_guards "
            "WHERE relation=?", (relation,)).fetchone()) == ("shadow", 1, 0)
        assert db.db.execute(
            "SELECT mode FROM ledger_relation_guards WHERE relation!=?",
            (relation,)).fetchone()[0] == "enforce"
        with db.db:
            db.db.execute(f"INSERT INTO {relation}(message_id) VALUES(998)")
            db.db.execute(f"UPDATE {relation} SET message_id=997 WHERE message_id=998")
        assert db.db.execute(
            "SELECT shadow_count FROM ledger_relation_guards WHERE relation=?",
            (relation,)).fetchone()[0] == 2
        assert db.db.execute(f"SELECT COUNT(*) FROM {relation}").fetchone()[0] == 2
    with closing(Ledger(str(path))) as db:
        assert tuple(db.db.execute(
            "SELECT mode,existing_count,shadow_count FROM ledger_relation_guards "
            "WHERE relation=?", (relation,)).fetchone()) == ("shadow", 2, 2)


def test_shadow_observations_prevent_unreviewed_promotion(tmp_path):
    # Given: the source arrived later, but a shadow violation was observed.
    path = tmp_path / "ledger.db"
    with closing(Ledger(str(path))) as db:
        with db.db:
            db.db.execute("UPDATE ledger_relation_guards SET mode='shadow' "
                          "WHERE relation='attachments'")
            db.db.execute("INSERT INTO attachments(message_id) VALUES(1)")
        db.save_messages([_message(1)])
    # When.
    with closing(Ledger(str(path))) as db:
        # Then: zero current violations is insufficient to erase shadow evidence.
        assert tuple(db.db.execute(
            "SELECT mode,existing_count,shadow_count FROM ledger_relation_guards "
            "WHERE relation='attachments'").fetchone()) == ("shadow", 0, 1)


def test_clean_existing_database_starts_shadow_and_does_not_self_promote(tmp_path):
    path = tmp_path / "ledger.db"
    with closing(Ledger(str(path))) as db:
        db.save_messages([_message(1)])
        with db.db:
            for table in ("artifacts", "attachments"):
                for suffix in ("ins", "upd"):
                    db.db.execute(f"DROP TRIGGER g1_{table}_msg_{suffix}")
            db.db.execute("DROP TABLE ledger_relation_guards")
    for _ in range(2):
        with closing(Ledger(str(path))) as db:
            assert [tuple(row) for row in db.db.execute(
                "SELECT mode,existing_count,shadow_count FROM ledger_relation_guards"
            )] == [("shadow", 0, 0), ("shadow", 0, 0)]


def test_shadow_evidence_is_visible_to_health_and_snapshot_status(ledger, tmp_path, monkeypatch):
    import run_check
    from views_testkit import _view

    with ledger.db:
        ledger.db.execute(
            "UPDATE ledger_relation_guards SET mode='shadow' WHERE relation='attachments'")
        ledger.db.execute("INSERT INTO attachments(message_id) VALUES(999)")
    expected = guard_status(ledger.db)
    assert expected["state"] == "known"
    assert next(row for row in expected["relations"] if row["relation"] == "attachments") == {
        "relation": "attachments", "mode": "shadow", "existing_count": 0, "shadow_count": 1}
    monkeypatch.setattr(run_check, "_prev_health", lambda: {})
    monkeypatch.setattr(run_check, "_free_mb", lambda: 10000)
    health = run_check._health(ledger, {"errors": []}, "ok")
    assert health["ledger_guards"] == expected
    assert health["overall"] == "degraded"
    assert "ledger_relation_violations" in health["state_reasons"]
    view = _view(ledger, tmp_path)
    try:
        assert view.read("status")["ledger_guards"] == expected
    finally:
        view.close()


def test_legacy_snapshot_has_unknown_guard_evidence():
    with closing(sqlite3.connect(":memory:")) as db:
        assert guard_status(db) == {"state": "unknown", "relations": []}


@pytest.mark.parametrize("version", [1, 5, 6, SCHEMA_VERSION])
def test_supported_database_reopen_preserves_guards_and_storage(tmp_path, version):
    # Given: a synthetic migratable ledger with high water and FTS content.
    path = tmp_path / "ledger.db"
    with closing(Ledger(str(path))) as db:
        db.save_messages([_message(1, body="synthetic searchable")])
        with db.db:
            db.db.execute("INSERT INTO artifacts(artifact_id,message_id) VALUES(50,1)")
            db.db.execute("DELETE FROM artifacts")
        if version == 1:
            db.db.executescript("""
                DROP TABLE attachments;
                CREATE TABLE attachments(file_id TEXT PRIMARY KEY,message_id INTEGER,
                  name TEXT,url TEXT,downloaded_path TEXT,first_seen REAL);
                INSERT INTO attachments VALUES('legacy',1,'synthetic','','',1);
            """)
        db.db.execute(f"PRAGMA user_version={version}")
    # When.
    for _ in range(2):
        with closing(Ledger(str(path))) as db:
            # Then.
            assert db.artifact_add("synthetic", "{}", message_id=1) > 50
            assert db.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            assert db.db.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'g1_%'").fetchone()[0] == 4
            if db._fts:
                assert db.db.execute(
                    "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'searchable'"
                ).fetchone()[0] == 1
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA journal_mode=DELETE")
    assert valid_mcs_db(str(path))


def test_unrelated_updates_of_old_rows_are_not_rejected(ledger):
    # Given: old invalid data, planted with only artifact guards removed.
    with ledger.db:
        for suffix in ("ins", "upd"):
            ledger.db.execute(f"DROP TRIGGER g1_artifacts_msg_{suffix}")
        ledger.db.execute("INSERT INTO artifacts(message_id) VALUES(999)")
    ledger._install_relation_guards()
    # When.
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET content='synthetic update'")
    # Then.
    assert ledger.db.execute(
        "SELECT shadow_count FROM ledger_relation_guards WHERE relation='artifacts'"
    ).fetchone()[0] == 0
