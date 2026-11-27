"""Post-update inspection must never turn a missing ledger into success."""
import sqlite3
import ledger
import mcs_util
import update_execution


def _quiet_postcheck(monkeypatch):
    monkeypatch.setattr(update_execution, "_head_sha", lambda: "synthetic-sha")
    monkeypatch.setattr(update_execution, "_tree_clean", lambda: True)
    monkeypatch.setattr(update_execution, "_agent_pid", lambda label: 123)
    monkeypatch.setattr(update_execution, "_check_environment", lambda cfg: ([], []))
    monkeypatch.setattr(mcs_util, "load_config", lambda: {})


def test_postcheck_missing_or_zero_byte_ledger_is_not_created_or_accepted(tmp_path, monkeypatch):
    _quiet_postcheck(monkeypatch)
    path = tmp_path / "data" / "ledger.db"
    monkeypatch.setattr(update_execution, "LEDGER", str(path))

    assert "ledger_missing" in update_execution._postcheck({}, "synthetic-sha")
    assert not path.exists()

    path.parent.mkdir()
    path.write_bytes(b"")
    errors = update_execution._postcheck({}, "synthetic-sha")
    assert "ledger_invalid" in errors and path.read_bytes() == b""


def test_postcheck_accepts_a_valid_empty_ledger_and_schema_migration(tmp_path, monkeypatch):
    _quiet_postcheck(monkeypatch)
    path = tmp_path / "ledger.db"
    store = ledger.Ledger(str(path))
    store.close()
    size = path.stat().st_size
    assert size > 0
    monkeypatch.setattr(update_execution, "LEDGER", str(path))

    errors = update_execution._postcheck({}, "synthetic-sha")
    assert "ledger_missing" not in errors and "ledger_invalid" not in errors
    assert "ledger_open_failed" not in errors
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT 1 FROM messages LIMIT 0").fetchall() == []
