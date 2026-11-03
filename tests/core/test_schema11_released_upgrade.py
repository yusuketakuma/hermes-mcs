"""Schema 11 migration against the retained exact released v1.1.0 DDL."""
from contextlib import closing
import sqlite3

import ledger
import ledger_schema
import pytest
from test_supported_schema_upgrade import RELEASES, _historical, _records


@pytest.mark.parametrize("interrupted", [False, True], ids=["upgrade", "rollback-on-interruption"])
def test_schema11_preserves_released_v110_parts_and_adds_group_only_after_commit(tmp_path, interrupted):
    origin = next(row for row in RELEASES if row["release"] == "v1.1.0")
    assert origin["schema_version"] == 10
    path = _historical(tmp_path / "released-v110", origin)
    with closing(sqlite3.connect(path)) as db:
        db.executemany("INSERT INTO notification_render_parts(delivery_id,part_id,kind,idx,payload_sha256,"
                       "bytes,name,attachment_id,state,remote_id,error_code,attempt_id,updated_at) "
                       "VALUES('synthetic-delivery',?,'attachment_part',?,?,?,?,?,?,?,?,?,?)", [
            ("synthetic-unknown", 1, "a" * 64, 9, "unknown.pdf", 401, "unknown", "F_UNKNOWN", "transport", "attempt-unknown", 131),
            ("synthetic-delivered", 2, "b" * 64, 9, "delivered.pdf", 401, "delivered", "F_DELIVERED", None, "attempt-delivered", 132)])
        db.commit()
    before = _records(path)
    with closing(sqlite3.connect(path)) as db:
        original_parts = db.execute("SELECT * FROM notification_render_parts ORDER BY delivery_id,part_id").fetchall()
        assert db.execute("PRAGMA user_version").fetchone()[0] == 10
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            db.execute("INSERT INTO notification_render_parts(delivery_id,part_id,kind,idx,state) "
                       "VALUES('synthetic-group','file_group','file_group',0,'unknown')")
        db.rollback()
        if interrupted:
            def authorize(action, name, *rest):
                if action == sqlite3.SQLITE_DROP_TABLE and name == "notification_render_parts_v10":
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            db.set_authorizer(authorize)
            try:
                with pytest.raises(sqlite3.DatabaseError):
                    ledger_schema._migrate(db, schema_version=11, fts=False)
            finally:
                db.set_authorizer(None)
            assert db.execute("PRAGMA user_version").fetchone()[0] == 10
            with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
                db.execute("INSERT INTO notification_render_parts(delivery_id,part_id,kind,idx,state) "
                           "VALUES('synthetic-group','file_group','file_group',0,'unknown')")
            db.rollback()
    if interrupted:
        assert _records(path) == before
        return
    with closing(ledger.Ledger(str(path))) as store:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == ledger.SCHEMA_VERSION == 11
        assert [tuple(row) for row in store.db.execute(
            "SELECT * FROM notification_render_parts ORDER BY delivery_id,part_id")] == original_parts
        assert {row[1] for row in store.db.execute("PRAGMA index_list(notification_render_parts)")} >= {
            "idx_nparts_pending", "idx_nparts_state_delivery"}
        assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []
        store.db.execute("INSERT INTO notification_render_parts(delivery_id,part_id,kind,idx,state) "
                         "VALUES('synthetic-group','file_group','file_group',0,'unknown')")
        store.db.rollback()
