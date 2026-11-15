"""Fixed error metadata never exposes messages or changes carry settlement control."""
import sqlite3

import pytest

import cloud_backlog as backlog


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


