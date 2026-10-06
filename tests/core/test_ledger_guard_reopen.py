"""Reopening a ledger skips the relation recount only when no decision can change."""
import sqlite3
from contextlib import closing

import pytest

import ledger as ledger_mod
from ledger import Ledger
from ingest_testkit import _message


def _open_traced(path):
    """Open a Ledger; return its recount statements and guard write-path entries."""
    statements: list[str] = []
    real = ledger_mod.sqlite3.connect

    def connect(*args, **kwargs):
        db = real(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    ledger_mod.sqlite3.connect = connect
    try:
        db = Ledger(str(path))
    finally:
        ledger_mod.sqlite3.connect = real
    db.db.set_trace_callback(None)
    counts = [s for s in statements if "SELECT COUNT(*) FROM" in s
              and "NOT EXISTS" in s]
    writes = sum("CREATE TABLE IF NOT EXISTS ledger_relation_guards" in s
                 for s in statements)
    return db, counts, writes


def _guards(db):
    return [tuple(r) for r in db.db.execute(
        "SELECT relation,mode,existing_count,shadow_count "
        "FROM ledger_relation_guards ORDER BY relation")]


def test_enforced_reopen_skips_recount_and_writer_lock(tmp_path):
    path = tmp_path / "ledger.db"
    with closing(Ledger(str(path))) as db:
        db.save_messages([_message(1)])
        db.artifact_add("synthetic", "{}", message_id=1, project_id=1)
        before = _guards(db)
    db, counts, immediate = _open_traced(path)
    with closing(db):
        assert counts == []
        assert immediate == 0
        assert _guards(db) == before == [
            ("artifacts", "enforce", 0, 0), ("attachments", "enforce", 0, 0)]
        with pytest.raises(sqlite3.IntegrityError, match="g1:"):
            db.db.execute("INSERT INTO attachments(message_id) VALUES(999)")


@pytest.mark.parametrize("orphaning", [
    "DELETE FROM messages WHERE message_id=1",
    "UPDATE messages SET project_id=2 WHERE message_id=1",
    "UPDATE messages SET message_id=5 WHERE message_id=1",
])
def test_parent_side_orphan_still_demotes_on_next_open(tmp_path, orphaning):
    # Given: enforced children, then a raw parent-side change orphans them.
    path = tmp_path / "ledger.db"
    with closing(Ledger(str(path))) as db:
        db.save_messages([_message(1)])
        db.artifact_add("synthetic", "{}", message_id=1, project_id=1)
        with db.db:
            db.db.execute("INSERT INTO attachments(message_id) VALUES(1)")
    with closing(sqlite3.connect(path)) as raw:
        with raw:
            raw.execute(orphaning)
    # When.
    db, counts, _ = _open_traced(path)
    with closing(db):
        # Then: same decision as an unconditional recount.
        assert counts
        modes = dict((r[0], r[1:]) for r in _guards(db))
        assert modes["artifacts"] == ("shadow", 1, 0)
        if not orphaning.startswith("UPDATE messages SET project_id"):
            assert modes["attachments"] == ("shadow", 1, 0)


def test_unrelated_message_updates_keep_fast_path(tmp_path):
    path = tmp_path / "ledger.db"
    with closing(Ledger(str(path))) as db:
        db.save_messages([_message(1)])
        db.save_messages([_message(1, body="synthetic edit"), _message(2)])
    db, counts, immediate = _open_traced(path)
    with closing(db):
        assert (counts, immediate) == ([], 0)


def test_shadow_reopen_counts_without_writer_lock_until_evidence_changes(tmp_path):
    # Given: a dirty shadow relation (planted orphan).
    path = tmp_path / "ledger.db"
    with closing(Ledger(str(path))) as db:
        with db.db:
            for suffix in ("ins", "upd"):
                db.db.execute(f"DROP TRIGGER g1_attachments_msg_{suffix}")
            db.db.execute("INSERT INTO attachments(message_id) VALUES(999)")
    with closing(Ledger(str(path))) as db:
        assert dict((r[0], r[1:]) for r in _guards(db))["attachments"] == ("shadow", 1, 0)
    # When: unchanged evidence -> read-only recount, no writer lock.
    db, counts, immediate = _open_traced(path)
    with closing(db):
        assert counts and immediate == 0
        db.save_messages([_message(999)])
    # When: evidence changed -> writer lock, existing_count refreshed, still shadow.
    db, counts, immediate = _open_traced(path)
    with closing(db):
        assert immediate == 1
        assert dict((r[0], r[1:]) for r in _guards(db))["attachments"] == ("shadow", 0, 0)


def test_manual_mode_change_and_missing_trigger_force_audit(tmp_path):
    path = tmp_path / "ledger.db"
    with closing(Ledger(str(path))) as db:
        with db.db:
            db.db.execute("DROP TRIGGER relation_audit_messages_del")
    db, counts, immediate = _open_traced(path)
    with closing(db):
        assert counts and immediate == 1
        with db.db:   # owner flips mode without the audit marker
            db.db.execute("UPDATE ledger_relation_guards SET mode='enforce' "
                          "WHERE relation='artifacts'")
            db.db.execute("UPDATE ledger_relation_audit SET mode='shadow' "
                          "WHERE relation='artifacts'")
    db, counts, immediate = _open_traced(path)
    with closing(db):
        assert counts and immediate == 1


def test_migration_open_always_audits(tmp_path):
    path = tmp_path / "ledger.db"
    with closing(Ledger(str(path))) as db:
        db.db.execute(f"PRAGMA user_version={ledger_mod.SCHEMA_VERSION - 1}")
    db, counts, immediate = _open_traced(path)
    with closing(db):
        assert counts and immediate == 1
