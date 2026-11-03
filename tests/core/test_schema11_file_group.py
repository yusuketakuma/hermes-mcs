"""Schema 11 preserves part receipts and rolls interrupted upgrades back."""
import sqlite3

import ledger
import ledger_schema
import pytest


def test_v10_upgrade_preserves_all_part_columns(tmp_path):
    path = str(tmp_path / "synthetic.db")
    store = ledger.Ledger(path)
    row = ("synthetic-delivery", "attach:0001", "attachment_part", 2,
           "a" * 64, 9, "synthetic.pdf", 1, "unknown", "F_SYNTHETIC",
           "timeout", "synthetic-attempt", 123.0)
    store.db.execute("INSERT INTO notification_render_parts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
    store.db.execute("PRAGMA user_version=10")
    store.db.commit()
    store.close()
    upgraded = ledger.Ledger(path)
    try:
        assert tuple(upgraded.db.execute("SELECT * FROM notification_render_parts").fetchone()) == row
        assert upgraded.db.execute("PRAGMA user_version").fetchone()[0] == 11
        assert {r[1] for r in upgraded.db.execute("PRAGMA index_list(notification_render_parts)")} >= {
            "idx_nparts_pending", "idx_nparts_state_delivery"}
    finally:
        upgraded.close()


def test_interrupted_upgrade_rolls_back_table_and_version(tmp_path):
    path = str(tmp_path / "synthetic.db")
    store = ledger.Ledger(path)
    db = store.db
    db.execute("PRAGMA user_version=10")
    db.execute("INSERT INTO notification_render_parts(delivery_id,part_id,kind,idx,state) "
               "VALUES('synthetic','card','card',0,'unknown')")
    db.commit()
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
    assert db.execute("SELECT state FROM notification_render_parts").fetchone()[0] == "unknown"
    assert db.execute("SELECT 1 FROM sqlite_master WHERE name='notification_render_parts_v10'").fetchone() is None
    store.close()
