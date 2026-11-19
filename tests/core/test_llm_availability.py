"""Availability state keeps the borrowed database and original outage policy intact."""
import json
import sqlite3

import pytest

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
        assert availability.llm_health(db)["last_success"] == 1061.0
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


def test_a_cooldown_or_down_stamp_ahead_of_the_clock_never_parks_the_lane(tmp_path, monkeypatch):
    # No call runs while the breaker is open, so nothing would ever clear a
    # stamp left ahead of a stepped-back clock or written by a damaged file.
    db = sqlite3.connect(tmp_path / "ledger.db")
    try:
        now = [1_000_000.0]
        monkeypatch.setattr(availability.time, "time", lambda: now[0])
        for _ in range(availability._CIRCUIT_TRIP):
            availability.circuit_failure(db)
        assert 0 < availability.circuit_open_s(db) <= availability._CIRCUIT_MAX_S
        assert availability.llm_down_for_s(db) == 0
        now[0] -= 86400                                   # the clock steps back a day
        assert availability.circuit_open_s(db) == 0.0
        assert availability.llm_down_for_s(db) is None    # unknown, not "down for 0 s"
        availability.circuit_failure(db)                  # the next real failure restamps it
        assert availability.llm_down_since(db) == now[0]
        now[0] += 3600
        assert availability.llm_down_for_s(db) == 3600
        availability.circuit_state_path(db).write_text(json.dumps({"open_until": now[0] + 1e9}))
        assert availability.circuit_open_s(db) == 0.0
    finally:
        db.close()


def test_critical_memory_pressure_reports_once_and_unknown_never_defers(monkeypatch, capsys):
    import subprocess
    levels = iter(["4\n", "4\n", "2\n", "garbage\n"])
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, next(levels), "")

    monkeypatch.setattr(availability.sys, "platform", "darwin")
    monkeypatch.setattr(availability.subprocess, "run", fake_run)
    monkeypatch.setattr(availability, "_pressure_critical_last", False)
    assert availability.memory_pressure_critical()
    assert availability.memory_pressure_critical()
    assert not availability.memory_pressure_critical()  # WARN is not critical
    assert not availability.memory_pressure_critical()  # unreadable adds no deferral
    assert calls[0] == ["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"]
    events = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert [(e["event"], e["reason"]) for e in events] == [
        ("memory_pressure_deferral", "memory_pressure_critical"),
        ("memory_pressure_cleared", "memory_pressure_critical")]

    def failing_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 2)

    monkeypatch.setattr(availability.subprocess, "run", failing_run)
    assert not availability.memory_pressure_critical()
    monkeypatch.setattr(availability.sys, "platform", "linux")
    monkeypatch.setattr(availability.subprocess, "run", lambda *a, **k: pytest.fail("probed off darwin"))
    assert not availability.memory_pressure_critical()
