"""History diagnostics retain the refusal, paid evidence and no-resend contract."""
from contextlib import closing
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

import cloud_client
import cloud_inference
import cloud_policy
from ledger import Ledger
from test_cloud_response_recovery import legacy_cloud_routes, paid as paid


_SECRET = "synthetic-private-detail"
_READS = {
    "recover_response": "FROM artifacts WHERE kind=? AND project_id=? AND message_id IS ? AND ",
    "request_send_block_reason": "FROM artifacts WHERE kind IN (?,?) AND project_id=?",
}


class ReadFailure:
    def __init__(self, db, phase, error):
        self.db, self.phase, self.error = db, phase, error
        self.failures = 0

    def __getattr__(self, name):
        return getattr(self.db, name)

    def execute(self, sql, *args, **kwargs):
        if _READS[self.phase] in sql:
            self.failures += 1
            raise self.error
        return self.db.execute(sql, *args, **kwargs)


def _ask(provider, prompt="synthetic prompt"):
    return provider(prompt, deadline=time.monotonic() + 60, max_tokens=100)


def _history(ledger):
    return [tuple(row) for row in ledger.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")]


def _assert_refusal(provider, phase, prompt):
    with pytest.raises(cloud_client.CloudNotSent) as caught:
        _ask(provider, prompt)
    error = caught.value
    assert type(error) is cloud_client.CloudNotSent
    assert (error.kind, error.detail, error.retryable, error.status) == (
        "budget_exceeded", "cloud_unit_unresolved", False, 0)
    assert cloud_client.not_sent_reason(error) == "cloud_unit_unresolved"
    assert error.__cause__ is None and error.__suppress_context__
    assert provider.possibly_sent is (phase == "request_send_block_reason")
    assert provider.unresolved is (phase == "request_send_block_reason")
    assert not provider.completed_reservations and not provider.discarded
    assert provider.final_disposition is None


def _events(capsys):
    captured = capsys.readouterr()
    assert captured.out == ""
    assert _SECRET not in captured.out + captured.err
    records = [json.loads(line) for line in captured.err.splitlines()]
    for record in records:
        record.pop("pid")
        record.pop("ts")
    return records


@pytest.mark.parametrize("phase", _READS)
@pytest.mark.parametrize("existing_paid", [False, True])
@pytest.mark.parametrize("kind,code,name", [
    ("sqlite_error", sqlite3.SQLITE_BUSY, "SQLITE_BUSY"),
    ("sqlite_error", sqlite3.SQLITE_INTERRUPT, "SQLITE_INTERRUPT"),
    ("sqlite_error", sqlite3.SQLITE_IOERR_READ, "SQLITE_IOERR_READ"),
    ("sqlite_error", None, None),
    ("value_error", None, None),
    ("type_error", None, None),
    ("key_error", None, None),
    ("cloud_error", None, None),
])
def test_read_failure_logs_only_the_rejecting_phase_without_send_or_history_change(
        paid, monkeypatch, capsys, phase, existing_paid, kind, code, name):
    path, _cfg, calls, locks, _state, unit, _restart = paid
    errors = {"sqlite_error": sqlite3.OperationalError, "value_error": ValueError,
              "type_error": TypeError, "key_error": KeyError, "cloud_error": cloud_client.CloudError}
    error = errors[kind](_SECRET)
    if code is not None:
        error.sqlite_errorcode, error.sqlite_errorname = code, name
    with closing(Ledger(path)) as ledger:
        if existing_paid:
            with legacy_cloud_routes():
                _ask(unit(ledger))
        before, sent, slots = _history(ledger), len(calls), len(locks)
        provider = unit(ledger, owner=_SECRET)
        provider.unit_key = _SECRET if not existing_paid else provider.unit_key
        proxy = ReadFailure(ledger.db, phase, error)
        with monkeypatch.context() as patch:
            patch.setattr(ledger, "db", proxy)
            _assert_refusal(provider, phase, "synthetic prompt" if phase == "recover_response" else _SECRET)
        assert proxy.failures == 1
        assert len(calls) == sent and len(locks) == slots
        assert _history(ledger) == before
        assert not ledger.db.in_transaction
    expected = {"event": "cloud_history_read_error", "phase": phase, "error_category": kind}
    if kind == "sqlite_error":
        expected["sqlite_primary_code"] = code & 0xff if code is not None else "unknown"
    if kind == "value_error":
        expected["error_reason"] = "ValueError"
    assert _events(capsys) == [expected]


@pytest.mark.parametrize("code,name", [
    (True, "SQLITE_ERROR"), ("5", "SQLITE_BUSY"), (-1, "SQLITE_BUSY"),
    (sqlite3.SQLITE_BUSY | (127 << 8), "SQLITE_BUSY_UNKNOWN"),
    (sqlite3.SQLITE_BUSY, _SECRET), (sqlite3.SQLITE_BUSY, "SQLITE_LOCKED"),
])
def test_untrusted_sqlite_metadata_is_a_fixed_unknown(capsys, code, name):
    error = sqlite3.OperationalError(_SECRET)
    error.sqlite_errorcode, error.sqlite_errorname = code, name
    cloud_inference._history_read_error("recover_response", error)
    assert _events(capsys) == [{"event": "cloud_history_read_error", "phase": "recover_response",
                               "error_category": "sqlite_error", "sqlite_primary_code": "unknown"}]


@pytest.mark.parametrize("attribute", ["sqlite_errorcode", "sqlite_errorname"])
def test_unreadable_sqlite_metadata_and_private_class_are_not_formatted(capsys, attribute):
    def unreadable(_self):
        raise RuntimeError(_SECRET)

    def private_str(_self):
        pytest.fail("private exception formatted")

    private = type(_SECRET, (sqlite3.OperationalError,), {
        attribute: property(unreadable), "__str__": private_str})
    error = private(_SECRET)
    if attribute == "sqlite_errorname":
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    cloud_inference._history_read_error("request_send_block_reason", error)
    assert _events(capsys) == [{"event": "cloud_history_read_error", "phase": "request_send_block_reason",
                               "error_category": "sqlite_error", "sqlite_primary_code": "unknown"}]


@pytest.mark.parametrize("phase", [_SECRET, [_SECRET], type(_SECRET, (str,), {})("recover_response")])
def test_nonfixed_phase_is_never_logged(capsys, phase):
    cloud_inference._history_read_error(phase, ValueError(_SECRET))
    assert _events(capsys) == []


@pytest.mark.parametrize("detail", [
    "SyntheticSecret123", "合成の秘密", "synthetic_private_detail",
    "cloud_future_unknown", ["cloud_result_missing"],
])
def test_value_error_reason_is_a_fixed_whitelist(capsys, detail):
    cloud_inference._history_read_error("recover_response", ValueError(detail))
    assert _events(capsys) == [{"event": "cloud_history_read_error",
                               "phase": "recover_response", "error_category": "value_error",
                               "error_reason": "ValueError"}]


def test_value_error_subclass_is_not_formatted(capsys):
    class PrivateError(ValueError):
        def __str__(self):
            pytest.fail("private exception formatted")

    cloud_inference._history_read_error("recover_response", PrivateError("cloud_result_missing"))
    assert _events(capsys) == [{"event": "cloud_history_read_error",
                               "phase": "recover_response", "error_category": "value_error",
                               "error_reason": "ValueError"}]


@pytest.mark.parametrize("phase", _READS)
def test_log_failure_does_not_replace_safe_refusal_or_mutate_paid_history(paid, monkeypatch, capsys, phase):
    path, _cfg, calls, locks, _state, unit, _restart = paid
    def failed_log(*args, **kwargs):
        raise OSError(_SECRET)

    with closing(Ledger(path)) as ledger:
        with legacy_cloud_routes():
            _ask(unit(ledger))
        before, sent, slots = _history(ledger), len(calls), len(locks)
        provider = unit(ledger)
        with monkeypatch.context() as patch:
            patch.setattr(cloud_inference, "sys", SimpleNamespace(stderr=SimpleNamespace(write=failed_log)))
            patch.setattr(ledger, "db", ReadFailure(ledger.db, phase, sqlite3.OperationalError(_SECRET)))
            _assert_refusal(provider, phase, "synthetic prompt" if phase == "recover_response" else _SECRET)
        assert _history(ledger) == before and len(calls) == sent and len(locks) == slots
    assert _events(capsys) == []


@pytest.mark.parametrize("error_class,category", [(ValueError, "value_error"), (TypeError, "type_error"),
                                                (KeyError, "key_error")])
def test_paid_answer_proof_failure_remains_recoverable_without_resend(
        paid, monkeypatch, capsys, error_class, category):
    path, _cfg, calls, locks, _state, unit, _restart = paid
    with closing(Ledger(path)) as ledger:
        with legacy_cloud_routes():
            original = _ask(unit(ledger))
        before, sent, slots = _history(ledger), len(calls), len(locks)
        def failed_proof(*args, **kwargs):
            raise error_class(_SECRET)
        with monkeypatch.context() as patch:
            patch.setattr(cloud_policy, "_saved_answer", failed_proof)
            _assert_refusal(unit(ledger), "recover_response", "synthetic prompt")
        assert _history(ledger) == before and len(calls) == sent and len(locks) == slots
        recovered = _ask(unit(ledger))
        assert recovered.pop("reused_response") is True and recovered == original
        assert _history(ledger) == before and len(calls) == sent and len(locks) == slots
    expected = {"event": "cloud_history_read_error", "phase": "recover_response",
                "error_category": category}
    if error_class is ValueError:
        expected["error_reason"] = "ValueError"
    assert _events(capsys) == [expected]
