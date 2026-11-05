"""Fixed error metadata never exposes messages or changes carry settlement control."""
import extract_llm_resident
import extract_llm_runner
import json
import sqlite3

import pytest

import cloud_backlog as backlog
from test_cloud_backlog import carry_worker as carry_worker


@pytest.mark.parametrize("code", sorted(backlog._RUNTIME_ERROR_CODES))
def test_runtime_error_reports_only_an_exact_known_reason(code):
    assert backlog._exception_observation(RuntimeError(code)) == {
        "class": "RuntimeError", "error_code": code}


@pytest.mark.parametrize("error", [
    RuntimeError(),
    RuntimeError("synthetic-private-error-detail"),
    RuntimeError("extract_insert_unverified /synthetic/private/path"),
    RuntimeError("extract_insert_unverified", "synthetic-private-error-detail"),
    RuntimeError(["extract_insert_unverified"]),
    ValueError("extract_insert_unverified"),
    sqlite3.OperationalError("synthetic-private-error-detail"),
])
def test_unknown_error_arguments_are_never_reported(error):
    assert backlog._exception_observation(error) == {"class": type(error).__name__}


def test_error_observation_never_formats_exception_or_argument():
    class PrivateArgument:
        def __str__(self):
            pytest.fail("private argument formatted")

    class PrivateError(RuntimeError):
        def __str__(self):
            pytest.fail("private exception formatted")

    assert backlog._exception_observation(RuntimeError(PrivateArgument())) == {"class": "RuntimeError"}
    assert backlog._exception_observation(PrivateError("extract_insert_unverified")) == {"class": "PrivateError"}


def test_real_sqlite_extended_error_uses_standard_identity_only():
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE synthetic(value TEXT UNIQUE)")
        db.execute("INSERT INTO synthetic VALUES('synthetic-private-error-detail')")
        with pytest.raises(sqlite3.IntegrityError) as caught:
            db.execute("INSERT INTO synthetic VALUES('synthetic-private-error-detail')")
    assert backlog._exception_observation(caught.value) == {
        "class": "IntegrityError", "sqlite_errorcode": sqlite3.SQLITE_CONSTRAINT_UNIQUE,
        "sqlite_errorname": "SQLITE_CONSTRAINT_UNIQUE"}


@pytest.mark.parametrize("code,name", [
    (sqlite3.SQLITE_BUSY, "SQLITE_BUSY"),
    (sqlite3.SQLITE_IOERR_READ, "SQLITE_IOERR_READ"),
    (sqlite3.SQLITE_BUSY, "synthetic-private-error-detail"),
    (True, "SQLITE_ERROR"),
    ("5", "SQLITE_BUSY"),
    (sqlite3.SQLITE_BUSY, "SQLITE_LOCKED"),
    (sqlite3.SQLITE_CREATE_TABLE, "SQLITE_CREATE_TABLE"),
    (999999, "SQLITE_UNKNOWN"),
])
def test_sqlite_code_and_name_must_match_a_known_error_constant(code, name):
    error = sqlite3.OperationalError("synthetic-private-error-detail")
    error.sqlite_errorcode, error.sqlite_errorname = code, name
    expected = {"class": "OperationalError"}
    if type(code) is int and name in ("SQLITE_BUSY", "SQLITE_IOERR_READ") and getattr(sqlite3, name) == code:
        expected.update(sqlite_errorcode=code, sqlite_errorname=name)
    assert backlog._exception_observation(error) == expected


def test_unreadable_sqlite_metadata_keeps_original_error_class():
    class MetadataError(sqlite3.OperationalError):
        @property
        def sqlite_errorcode(self):
            raise RuntimeError("synthetic-private-error-detail")

    assert backlog._exception_observation(MetadataError()) == {"class": "MetadataError"}


@pytest.mark.parametrize("site", ["extract", "semantic", "carry", "exit_carry"])
@pytest.mark.parametrize("known", [False, True])
def test_all_worker_exception_events_share_fixed_diagnostics_without_changing_carry(
        carry_worker, monkeypatch, capsys, site, known):
    import extract_llm
    import semantic_drain

    state, _ = carry_worker
    item, attempts, delays = object(), [], []

    def fail():
        raise RuntimeError("extract_insert_unverified" if known else "synthetic-private-error-detail")

    def extract(*args, **kwargs):
        if site == "extract":
            state.alive = False
            fail()
        if site in ("carry", "exit_carry"):
            state.carry = [item]
            if site == "exit_carry":
                state.memory = False
            return {"done": 1, "carry": state.carry}
        return {"done": 1}

    def semantic(*args, **kwargs):
        state.alive = False
        fail()

    def settle(ledger, carry, **kwargs):
        assert carry is state.carry and carry == [item]
        assert kwargs["preserve_on_fence_loss"] is True and kwargs["final"] is False
        attempts.append(True)
        if site == "exit_carry" and len(attempts) == 1:
            return {}, 0, set()
        fail()

    def sleep(seconds):
        delays.append(seconds)
        if site == "carry":
            state.alive = False

    monkeypatch.setattr(extract_llm, "run_pending", extract)
    monkeypatch.setattr(extract_llm_resident, 'run_pending', extract_llm.run_pending)
    monkeypatch.setattr(extract_llm, "_settle_carry", settle)
    monkeypatch.setattr(extract_llm_resident, '_settle_carry', extract_llm._settle_carry)
    monkeypatch.setattr(extract_llm_runner, '_settle_carry', extract_llm._settle_carry)
    monkeypatch.setattr(semantic_drain, "run_due", semantic)
    monkeypatch.setattr(backlog.time, "sleep", sleep)
    backlog.worker(991)
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    errors = [record for record in records if record["event"] in (
        "cloud_backlog_error", "cloud_carry_settle_error")]
    assert len(errors) == 1 and state.closed
    error = errors[0]
    assert error["class"] == "RuntimeError"
    if known:
        assert error["error_code"] == "extract_insert_unverified"
    else:
        assert "error_code" not in error
    if site in ("carry", "exit_carry"):
        assert state.carry == [item]  # Diagnostics cannot discard retained paid work.
        assert len(attempts) == (2 if site == "exit_carry" else 1)
        assert error["event"] == "cloud_carry_settle_error"
        if site == "carry":
            assert error["retry_s"] == 2 and error["failures"] == 1 and delays == [2]
        else:
            assert "retry_s" not in error and not delays
    else:
        assert error["event"] == "cloud_backlog_error" and error["phase"] == site
        assert delays == [10] and not attempts
    assert "synthetic-private-error-detail" not in json.dumps(records)
