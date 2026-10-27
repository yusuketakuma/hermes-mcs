"""Synthetic SQLite transaction and error boundaries for source binding reads."""
import sqlite3

import pytest

import inference_dependency_loss as loss


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE marker(value INTEGER)")
    yield connection
    connection.close()


def test_success_and_normal_failure_keep_caller_owned_writes(db, monkeypatch):
    db.execute("INSERT INTO marker VALUES (7)")
    monkeypatch.setattr(loss, "_source_bindings_page", lambda *_: ["synthetic"])
    assert loss.source_bindings(db, [(1, 1)]) == ["synthetic"]
    error = ValueError("synthetic source failure")

    def fail(*_):
        raise error

    monkeypatch.setattr(loss, "_source_bindings_page", fail)
    monkeypatch.setattr(loss, "source_binding", fail)
    with pytest.raises(ValueError) as caught:
        loss.source_bindings(db, [(1, 1)])
    assert caught.value is error
    assert db.in_transaction and db.execute("SELECT value FROM marker").fetchall() == [(7,)]
    db.rollback()
    assert db.execute("SELECT COUNT(*) FROM marker").fetchone()[0] == 0


def test_success_does_not_hide_release_failure(db, monkeypatch):
    monkeypatch.setattr(loss, "_source_bindings_page", lambda *_: ["synthetic"])
    db.set_authorizer(lambda action, arg1, arg2, *_: sqlite3.SQLITE_DENY
                      if action == sqlite3.SQLITE_SAVEPOINT and arg1 == "RELEASE"
                      and arg2 == "source_bindings_read" else sqlite3.SQLITE_OK)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            loss.source_bindings(db, [(1, 1)])
    finally:
        db.set_authorizer(None)
        db.rollback()


@pytest.mark.parametrize("code", [None, sqlite3.SQLITE_INTERRUPT, sqlite3.SQLITE_FULL,
                                  sqlite3.SQLITE_IOERR | (3 << 8)])
@pytest.mark.parametrize("error_type", [sqlite3.OperationalError, sqlite3.DatabaseError])
def test_auto_rollback_preserves_original_error_without_replay(db, monkeypatch, code, error_type):
    db.execute("INSERT INTO marker VALUES (7)")
    error = error_type("synthetic SQLite failure")
    if code is not None:
        error.sqlite_errorcode = code
    replay = []

    def fail(connection, _pairs):
        connection.rollback()  # Simulate SQLite discarding the entire transaction.
        raise error

    monkeypatch.setattr(loss, "_source_bindings_page", fail)
    monkeypatch.setattr(loss, "source_binding", lambda *_: replay.append(True))
    with pytest.raises(error_type) as caught:
        loss.source_bindings(db, [(1, 1)])
    assert caught.value is error and getattr(caught.value, "sqlite_errorcode", None) == code
    assert not replay and not db.in_transaction
    assert db.execute("SELECT COUNT(*) FROM marker").fetchone()[0] == 0
