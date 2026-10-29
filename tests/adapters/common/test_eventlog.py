"""The shared adapter event logger keeps only allowlisted fields and code-shaped values."""
import json
import logging

import pytest

import mcs_util
import run_log

from adapters.common import eventlog


def _line(caplog, event, **fields):
    caplog.clear()
    eventlog.make_logger("mcs.test.eventlog")(event, **fields)
    (record,) = caplog.records
    assert record.name == "mcs.test.eventlog" and record.levelno == logging.INFO
    return record.getMessage()


def test_allowlist_drops_unknown_fields(caplog):
    caplog.set_level(logging.INFO, logger="mcs.test.eventlog")
    line = _line(caplog, "interaction_denied", reason="role_check",
                 actor="123456789", channel="987654321", text="body")
    assert json.loads(line) == {"event": "interaction_denied", "reason": "role_check"}
    assert line.startswith('{"event": ')


def test_every_allowed_field_passes_and_none_is_omitted(caplog):
    caplog.set_level(logging.INFO, logger="mcs.test.eventlog")
    fields = {name: "code_1" for name in eventlog.ALLOWED_FIELDS}
    assert json.loads(_line(caplog, "ev", **fields)) == {"event": "ev", **fields}
    assert json.loads(_line(caplog, "ev", error=None, action=None)) == {"event": "ev"}


def test_int_and_bool_pass_through(caplog):
    caplog.set_level(logging.INFO, logger="mcs.test.eventlog")
    got = json.loads(_line(caplog, "reconciled", receipt_republished=2, not_sent=0,
                           unknown=True, code=-15, failures=3))
    assert got == {"event": "reconciled", "receipt_republished": 2, "not_sent": 0,
                   "unknown": True, "code": -15, "failures": 3}
    assert got["unknown"] is True


def test_non_code_values_are_redacted(caplog):
    caplog.set_level(logging.INFO, logger="mcs.test.eventlog")
    got = json.loads(_line(
        caplog, "spec_rejected", error="Expecting value: line 1 column 1",
        reason="患者メモ", action="x" * 81, outcome=1.5, result=["a"],
        job="/tmp/private/path", delivery_id="d-1:2.3_x"))
    assert got == {"event": "spec_rejected", "error": "redacted", "reason": "redacted",
                   "action": "redacted", "outcome": "redacted", "result": "redacted",
                   "job": "redacted", "delivery_id": "d-1:2.3_x"}


def test_event_name_is_validated(caplog):
    caplog.set_level(logging.INFO, logger="mcs.test.eventlog")
    assert json.loads(_line(caplog, "bad event\nname")) == {"event": "redacted"}
    assert json.loads(_line(caplog, "")) == {"event": "redacted"}
    assert json.loads(_line(caplog, 7)) == {"event": "redacted"}
    assert json.loads(_line(caplog, "a" * 80)) == {"event": "a" * 80}


def test_one_line_per_event_with_percent_and_newlines_contained(caplog):
    caplog.set_level(logging.INFO, logger="mcs.test.eventlog")
    line = _line(caplog, "tick_failed", error="%s\n%d")
    assert "\n" not in line and json.loads(line)["error"] == "redacted"


def test_level_is_configurable(caplog):
    caplog.set_level(logging.DEBUG, logger="mcs.test.level")
    eventlog.make_logger("mcs.test.level", logging.WARNING)("ev")
    assert [r.levelno for r in caplog.records] == [logging.WARNING]


def test_standalone_runtime_diagnostics_survive(caplog):
    """mcs_standalone logs through this filter: the fields that locate a
    failed state write, a held update marker or a backoff stay readable."""
    caplog.set_level(logging.INFO, logger="mcs.test.eventlog")
    cases = [
        ("state_write_failed", {"file": "standalone-status.json", "code": "ENOSPC"}),
        ("update_marker_held", {"state": "held", "age_s": 42}),
        ("child_ended", {"job": "cmd_int", "code": 1, "failures": 2, "retry_in": 60}),
        ("child_ended", {"job": "tick", "adopted": True}),
        ("slot_catchup", {"job": "tick", "slot": 1760000000}),
        ("connector_ended", {"code": "timeout", "stopping": True}),
    ]
    for event, fields in cases:
        assert json.loads(_line(caplog, event, **fields)) == {"event": event, **fields}


@pytest.mark.parametrize("captured", [False, True])
def test_canonical_sink_receives_one_redacted_event(tmp_path, monkeypatch, caplog, captured):
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    monkeypatch.setattr(mcs_util, "HOME", str(tmp_path))
    monkeypatch.setenv("MCS_RUN_LOG_CAPTURED", "1" if captured else "0")
    caplog.set_level(logging.INFO, logger="mcs.test.eventlog")
    logger = logging.getLogger("mcs.test.eventlog")
    writes = []
    prepare = run_log._prepare
    def observed(*args, **kwargs):
        writes.append(True)
        return prepare(*args, **kwargs)
    monkeypatch.setattr(run_log, "_prepare", observed)
    class CapturedOutput(logging.Handler):
        def emit(self, record):
            # Model the existing capture drainer, whose appends must remain unconditional.
            run_log.append(data, (record.getMessage() + "\n").encode("utf-8"))
    handler = CapturedOutput()
    if captured:
        logger.addHandler(handler)
    try:
        line = _line(caplog, "synthetic_event", error="PRIVATE_CANARY text", text="dropped")
    finally:
        logger.removeHandler(handler)
    assert json.loads(line) == {"event": "synthetic_event", "error": "redacted"}
    assert (data / "mcs.log").read_text() == line + "\n"
    assert writes == [True]


@pytest.mark.parametrize("error_type", [OSError, run_log.RunLogError])
def test_canonical_append_failure_preserves_logger_and_fixed_error(tmp_path, monkeypatch, caplog, capsys, error_type):
    monkeypatch.setattr(mcs_util, "HOME", str(tmp_path))
    monkeypatch.delenv("MCS_RUN_LOG_CAPTURED", raising=False)
    caplog.set_level(logging.INFO, logger="mcs.test.eventlog")
    calls = []
    def fail(data, payload):
        calls.append((data, payload))
        raise error_type("PRIVATE_CANARY error")
    monkeypatch.setattr(run_log, "append", fail)
    line = _line(caplog, "synthetic_failure", error="PRIVATE_CANARY text")
    assert json.loads(line) == {"event": "synthetic_failure", "error": "redacted"}
    assert calls == [(str(tmp_path / "data"), (line + "\n").encode("utf-8"))]
    assert capsys.readouterr().err == "mcs_log_write_failed\n"
