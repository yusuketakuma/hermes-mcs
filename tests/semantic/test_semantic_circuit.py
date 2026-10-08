"""Durable Jev circuit-breaker contracts (synthetic ledger only)."""

import json
from types import SimpleNamespace

from ledger import Ledger
import semantic_runtime as runtime


def _error(kind, *, retryable=True, status=None):
    return SimpleNamespace(
        kind=kind,
        retryable=retryable,
        status=status,
        detail="TOP_SECRET transport response",
    )


def _state(db):
    row = db.db.execute(
        "SELECT content FROM artifacts WHERE kind='semantic_circuit' "
        "ORDER BY artifact_id DESC LIMIT 1"
    ).fetchone()
    return None if row is None else json.loads(row["content"])


def test_circuit_persists_three_failures_and_expires_after_restart(tmp_path):
    path = tmp_path / "ledger.db"
    db = Ledger(str(path))

    assert not runtime.circuit_open(db, now=1000.0)
    assert not runtime.record_circuit_result(
        db, _error("transport"), now=1000.0)
    assert not runtime.record_circuit_result(
        db, _error("timeout"), now=1001.0)
    assert not runtime.circuit_open(db, now=1001.0)
    assert runtime.record_circuit_result(
        db, _error("rate_limited", status=429), now=1002.0)
    assert runtime.circuit_open(db, now=1002.1)

    state = _state(db)
    assert state["consecutive_failures"] == 3
    assert state["open_until"] == 1302.0
    assert "TOP_SECRET" not in json.dumps(state)
    db.close()

    reopened = Ledger(str(path))
    assert runtime.circuit_open(reopened, now=1301.9)
    assert not runtime.circuit_open(reopened, now=1302.0)
    # The first post-cooldown failure starts a fresh finite sequence.
    assert not runtime.record_circuit_result(
        reopened, _error("transport"), now=1302.0)
    assert _state(reopened)["consecutive_failures"] == 1
    reopened.close()


def test_only_retryable_jev_classes_count_and_success_resets(tmp_path):
    db = Ledger(str(tmp_path / "ledger.db"))

    ignored = (
        _error("no_api_key", retryable=False),
        _error("budget_exceeded", retryable=True),
        _error("protocol_error", retryable=True),
        _error("transport", retryable=False, status=503),
        _error("transport", retryable=True, status=400),
    )
    for error in ignored:
        assert not runtime.record_circuit_result(db, error, now=2000.0)
    assert _state(db) is None

    assert not runtime.record_circuit_result(
        db, _error("rate_limited", status=429), now=2001.0)
    assert not runtime.record_circuit_result(
        db, _error("transport", status=529), now=2002.0)
    assert runtime.record_circuit_result(
        db, _error("transport", status=500), now=2003.0)
    assert runtime.circuit_open(db, now=2003.1)

    # A successful Jev result clears both the open state and the count.
    assert not runtime.record_circuit_result(db, None, now=2004.0)
    assert not runtime.circuit_open(db, now=2004.0)
    assert _state(db)["consecutive_failures"] == 0
    db.close()


def test_malformed_latest_record_has_bounded_fail_closed_window(tmp_path):
    db = Ledger(str(tmp_path / "ledger.db"))
    db.artifact_add("semantic_circuit", "{malformed", model="jev")
    artifact_id = db.db.execute(
        "SELECT artifact_id FROM artifacts WHERE kind='semantic_circuit' "
        "ORDER BY artifact_id DESC LIMIT 1"
    ).fetchone()["artifact_id"]
    db.db.execute(
        "UPDATE artifacts SET created_at=? WHERE artifact_id=?",
        (3000.0, artifact_id),
    )
    db.db.commit()

    assert runtime.circuit_open(db, now=3299.9)
    assert not runtime.circuit_open(db, now=3300.0)
    # A valid success after expiry heals the malformed latest record.
    assert not runtime.record_circuit_result(db, None, now=3300.0)
    state = _state(db)
    assert state["consecutive_failures"] == 0
    assert state["open_until"] == 0.0
    db.close()


def test_payment_required_opens_and_reads_back_as_a_valid_state(tmp_path):
    # HTTP 402 is not retryable within a job, yet every call fails until
    # the account is settled: three in a row open the circuit, and the
    # persisted class reads back intact (not as a malformed record)
    import semantic_jev as jev
    db = Ledger(str(tmp_path / "ledger.db"))
    err = jev.JevError("payment_required", "http_402", status=402)
    assert not runtime.record_circuit_result(db, err, now=1000.0)
    assert not runtime.record_circuit_result(db, err, now=1001.0)
    assert runtime.record_circuit_result(db, err, now=1002.0)
    assert runtime.circuit_open(db, now=1002.1)
    state = _state(db)
    assert state["failure_class"] == "http_402"
    assert state["consecutive_failures"] == 3 and state["open_until"] == 1302.0
    # a success closes it again
    assert not runtime.record_circuit_result(db, None, now=1500.0)
    assert not runtime.circuit_open(db, now=1500.0)
    db.close()
