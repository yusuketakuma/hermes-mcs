"""Availability state keeps the borrowed database and original outage policy intact."""
import json
import sqlite3

import llm_availability as availability
import mcs_queries


def test_availability_owns_files_without_settling_borrowed_transaction(tmp_path, monkeypatch):
    db = sqlite3.connect(tmp_path / "ledger.db")
    try:
        db.execute("CREATE TABLE sentinel(value)")
        db.commit()
        db.execute("INSERT INTO sentinel VALUES ('synthetic pending')")
        monkeypatch.setattr(availability.time, "time", lambda: 1000.0)
        assert db.in_transaction
        assert availability.llm_health(db) == {"last_success": None, "down_since": None}
        for _ in range(3):
            availability.circuit_failure(db)
        assert availability.circuit_state_path(db) == tmp_path / "llm_circuit.json"
        assert availability.llm_health_path(db) == tmp_path / "llm_health.json"
        assert json.loads((tmp_path / "llm_circuit.json").read_text()) == {
            "failures": 0, "streak": 1, "open_until": 1240.0}
        assert availability.circuit_open_s(db) == 240.0
        assert availability.llm_health(db) == {"last_success": None, "down_since": 1000.0}
        monkeypatch.setattr(availability.time, "time", lambda: 1001.0)
        availability.circuit_success(db)
        assert not (tmp_path / "llm_circuit.json").exists()
        assert availability.llm_health(db) == {"last_success": 1001.0, "down_since": None}
        original = (tmp_path / "llm_health.json").read_bytes()
        monkeypatch.setattr(availability.time, "time", lambda: 1060.0)
        availability.circuit_success(db)
        assert (tmp_path / "llm_health.json").read_bytes() == original
        monkeypatch.setattr(availability.time, "time", lambda: 1061.0)
        availability.circuit_success(db)
        assert availability.llm_last_success(db) == 1061.0
        assert db.in_transaction
        assert db.execute("SELECT value FROM sentinel").fetchone() == ("synthetic pending",)
        db.rollback()
        assert db.execute("SELECT COUNT(*) FROM sentinel").fetchone() == (0,)
    finally:
        db.close()


def test_search_registration_borrows_a_readonly_connection(tmp_path):
    path = tmp_path / "search.db"
    writer = sqlite3.connect(path)
    writer.execute("CREATE TABLE sentinel(value)")
    writer.commit()
    writer.close()
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        mcs_queries.register_search_fold(db)
        mcs_queries.register_search_fold(db)
        assert db.execute("SELECT mcs_fold(?)", (" Ａ　B ",)).fetchone() == ("ab",)
        assert db.execute("SELECT mcs_fold(NULL)").fetchone() == (None,)
        assert not db.in_transaction
        assert db.execute("SELECT COUNT(*) FROM sentinel").fetchone() == (0,)
    finally:
        db.close()
