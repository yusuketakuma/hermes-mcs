"""The macOS full-fsync settings are in force before the ledger's first schema write."""
import ledger


def test_fullfsync_is_set_before_the_first_schema_write(tmp_path, monkeypatch):
    seen = {}
    original = ledger.Ledger._init

    def spy(self):
        seen["at_init"] = (self.db.execute("PRAGMA fullfsync").fetchone()[0],
                           self.db.execute("PRAGMA checkpoint_fullfsync").fetchone()[0])
        return original(self)
    monkeypatch.setattr(ledger.Ledger, "_init", spy)
    store = ledger.Ledger(str(tmp_path / "mcs.db"))
    try:
        assert seen["at_init"] == (1, 1)
        assert store.db.execute("PRAGMA fullfsync").fetchone()[0] == 1
    finally:
        store.close()
