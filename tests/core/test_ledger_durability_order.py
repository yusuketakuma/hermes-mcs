"""The macOS full-fsync settings are in force before the ledger's first schema write."""
import ledger
import ledger_schema


def test_fullfsync_is_set_before_the_first_schema_write(tmp_path, monkeypatch):
    seen = {}
    original = ledger_schema.initialize

    def spy(db, *, schema_version):
        seen["at_init"] = (db.execute("PRAGMA fullfsync").fetchone()[0],
                           db.execute("PRAGMA checkpoint_fullfsync").fetchone()[0])
        return original(db, schema_version=schema_version)
    monkeypatch.setattr(ledger_schema, "initialize", spy)
    store = ledger.Ledger(str(tmp_path / "mcs.db"))
    try:
        assert seen["at_init"] == (1, 1)
        assert store.db.execute("PRAGMA fullfsync").fetchone()[0] == 1
    finally:
        store.close()
