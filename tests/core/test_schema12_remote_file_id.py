"""Schema 12 keeps legacy identities and binds new Discord file IDs durably."""
from contextlib import closing
import sqlite3

import ledger
import ledger_schema
import pytest
from test_supported_schema_upgrade import RELEASES, _historical, _records


COLUMNS = ("delivery_id,part_id,kind,idx,payload_sha256,bytes,name,attachment_id,"
           "state,remote_id,error_code,attempt_id,updated_at")
ROWS = [
    ("synthetic-identity", "attach:0001", "attachment_part", 2,
     "a" * 64, 19, "synthetic-one.pdf", 401, "unknown", "991", "timeout", "attempt-one", 123.5),
    ("synthetic-identity", "attach:0002", "attachment_part", 3,
     "b" * 64, 23, "synthetic-two.png", 402, "delivered", "992", None, "attempt-two", 124.5),
]


def legacy_parts(tmp_path, version):
    origin = next(row for row in RELEASES if row["release"] == "v1.1.0")
    path = _historical(tmp_path / f"legacy-{version}", origin)
    with closing(sqlite3.connect(path)) as db:
        if version == 11:
            # Synthetic v11 delta only; the retained released v10 DDL stays untouched.
            old = db.execute("SELECT sql FROM sqlite_master WHERE name='notification_render_parts'").fetchone()[0]
            new = old.replace("'attachment_part'", "'attachment_part','file_group'")
            assert new != old and "remote_file_id" not in new
            indexes = [r[0] for r in db.execute("SELECT sql FROM sqlite_master WHERE type='index' "
                       "AND tbl_name='notification_render_parts' AND sql IS NOT NULL")]
            before = db.execute("SELECT * FROM notification_render_parts").fetchall()
            db.execute("DROP TABLE notification_render_parts")
            db.execute(new)
            db.executemany(f"INSERT INTO notification_render_parts({COLUMNS}) VALUES({','.join('?' * 13)})", before)
            for sql in indexes:
                db.execute(sql)
            db.execute("PRAGMA user_version=11")
        db.executemany(f"INSERT INTO notification_render_parts({COLUMNS}) VALUES({','.join('?' * 13)})", ROWS)
        db.commit()
        assert len(db.execute("PRAGMA table_info(notification_render_parts)").fetchall()) == 13
    return path


@pytest.mark.parametrize("version", [10, 11])
def test_upgrade_preserves_all_thirteen_columns_and_remote_id(tmp_path, version):
    path = legacy_parts(tmp_path, version)
    with closing(sqlite3.connect(path)) as db:
        before = db.execute(f"SELECT {COLUMNS} FROM notification_render_parts ORDER BY delivery_id,part_id").fetchall()
    with closing(ledger.Ledger(str(path))) as store:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 12
        after = [tuple(row) for row in store.db.execute(
            "SELECT * FROM notification_render_parts ORDER BY delivery_id,part_id")]
        assert after == [(*row, None) for row in before]
        store.db.execute("UPDATE notification_render_parts SET remote_file_id='993' "
                         "WHERE delivery_id='synthetic-identity' AND part_id='attach:0002'")
        store.db.commit()
    with closing(ledger.Ledger(str(path))) as reopened:
        result = reopened.db.execute("SELECT state,remote_id,remote_file_id FROM notification_render_parts "
                                     "WHERE delivery_id='synthetic-identity' AND part_id='attach:0002'").fetchone()
        assert tuple(result) == ("delivered", "992", "993")
        assert reopened.db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("version", [10, 11])
def test_alter_failure_rolls_back_whole_upgrade_and_version(tmp_path, version):
    path = legacy_parts(tmp_path, version)
    before = _records(path)
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        def authorize(action, database, name, *_rest):
            if action == sqlite3.SQLITE_ALTER_TABLE and name == "notification_render_parts":
                # v10 first renames the old table; reject only the later additive ALTER.
                authorize.calls += 1
                if version == 11 or authorize.calls == 2:
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        authorize.calls = 0
        db.set_authorizer(authorize)
        try:
            with pytest.raises(sqlite3.DatabaseError):
                ledger_schema._migrate(db, schema_version=12, fts=False)
        finally:
            db.set_authorizer(None)
        assert authorize.calls == (2 if version == 10 else 1)
        assert db.execute("PRAGMA user_version").fetchone()[0] == version
        assert not db.in_transaction
        assert "remote_file_id" not in {row[1] for row in db.execute("PRAGMA table_info(notification_render_parts)")}
    assert _records(path) == before


def test_old_writer_rejects_v12_before_any_migration(tmp_path, monkeypatch):
    path = tmp_path / "synthetic-v12.db"
    ledger.Ledger(str(path)).close()
    before = _records(path)
    monkeypatch.setattr(ledger, "SCHEMA_VERSION", 11)
    monkeypatch.setattr(ledger_schema, "initialize", lambda *_a, **_k: pytest.fail("old writer reached migration"))
    with pytest.raises(ledger_schema.MigrationError, match="unsupported schema version 12"):
        ledger.Ledger(str(path))
    assert _records(path) == before
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 12
