"""Corrupt receipt rows cannot block or substitute bound restore consent."""
import json
import sqlite3

import mcs_update
import pytest
from ops_testkit import _load, _receipts_db


@pytest.mark.parametrize("scanner", ["update", "watchdog"])
@pytest.mark.parametrize("has_bound_consent", [False, True])
def test_deep_corrupt_receipt_preserves_bound_consent(
        tmp_path, monkeypatch, scanner, has_bound_consent):
    report = {"report_id": "synthetic-report", "backup_sha256": "a" * 64,
              "backup_schema": 9}
    path = tmp_path / "ledger.db"
    con = _receipts_db(path)
    if has_bound_consent:
        receipt = {"cmd": "ops.restore_approve", "scheduled": True, **report}
        con.execute(
            "INSERT INTO command_receipts(command_id,outcome,receipt_json,"
            "processed_at) VALUES(?,?,?,?)",
            ("bound-consent", "applied", json.dumps(receipt), 1))
    con.execute(
        "INSERT INTO command_receipts(command_id,outcome,receipt_json,"
        "processed_at) VALUES(?,?,?,?)",
        ("corrupt", "applied", "[" * 10000 + "0" + "]" * 10000, 2))
    con.commit()
    con.close()
    module = mcs_update if scanner == "update" else _load()
    monkeypatch.setattr(module, "LEDGER", str(path))
    read_consent = (module._restore_consent if scanner == "update"
                    else module._consent_for)
    assert read_consent(report) == ("bound-consent" if has_bound_consent else None)


_REPORT = {"report_id": "synthetic-report", "backup_sha256": "a" * 64,
           "backup_schema": 1}
_BOUND = {"cmd": "ops.restore_approve", "scheduled": True, **_REPORT}


def _consent(tmp_path, monkeypatch, scanner, rows):
    path = tmp_path / "ledger.db"
    con = _receipts_db(path)
    for n, (cid, raw) in enumerate(rows, 1):
        con.execute(
            "INSERT INTO command_receipts(command_id,outcome,receipt_json,"
            "processed_at) VALUES(?,?,?,?)", (cid, "applied", raw, n))
    con.commit()
    con.close()
    module = mcs_update if scanner == "update" else _load()
    monkeypatch.setattr(module, "LEDGER", str(path))
    read = module._restore_consent if scanner == "update" else module._consent_for
    return read(_REPORT)


_INVALID = {
    "error_set": json.dumps({**_BOUND, "error": "denied"}),
    "command_id_mismatch": json.dumps({**_BOUND, "command_id": "other"}),
    "outcome_rejected": json.dumps({**_BOUND, "outcome": "rejected"}),
    "schema_bool": json.dumps({**_BOUND, "backup_schema": True}),
    "schema_text": json.dumps({**_BOUND, "backup_schema": "1"}),
    "not_scheduled": json.dumps({**_BOUND, "scheduled": False}),
    "duplicate_key": json.dumps(_BOUND)[:-1] + ', "report_id": "synthetic-report"}',
    "nan_constant": json.dumps(_BOUND)[:-1] + ', "x": NaN}',
    "oversized": json.dumps({**_BOUND, "pad": "x" * 20000}),
    "not_object": json.dumps([_BOUND]),
}


@pytest.mark.parametrize("scanner", ["update", "watchdog"])
@pytest.mark.parametrize("case", sorted(_INVALID))
def test_both_scanners_refuse_the_same_invalid_consent(
        tmp_path, monkeypatch, scanner, case):
    assert _consent(tmp_path, monkeypatch, scanner,
                    [("cid-1", _INVALID[case])]) is None


@pytest.mark.parametrize("scanner", ["update", "watchdog"])
def test_newer_invalid_row_never_masks_or_replaces_a_bound_consent(
        tmp_path, monkeypatch, scanner):
    rows = [("bound-consent", json.dumps(_BOUND))]
    rows += [(f"bad-{name}", raw) for name, raw in sorted(_INVALID.items())]
    assert _consent(tmp_path, monkeypatch, scanner, rows) == "bound-consent"


@pytest.mark.parametrize("scanner", ["update", "watchdog"])
def test_historical_receipt_with_matching_command_id_still_counts(
        tmp_path, monkeypatch, scanner):
    raw = json.dumps({**_BOUND, "command_id": "cid-7", "outcome": "applied",
                      "error": None, "reason": "synthetic"})
    assert _consent(tmp_path, monkeypatch, scanner, [("cid-7", raw)]) == "cid-7"


# ---------- streaming scan: same answer, bounded memory, closed handle ----------

class _Cursor:
    """Yields the real rows, then fails like an unreadable page."""

    def __init__(self, rows, fail):
        self._rows, self._fail = rows, fail

    def __iter__(self):
        yield from self._rows
        if self._fail:
            raise sqlite3.DatabaseError("synthetic unreadable page")

    def fetchall(self):
        return list(self)


def _fake_connect(monkeypatch, module, real_path, *, fail):
    closed = []
    real = sqlite3.connect

    class Con:
        def __init__(self):
            self._con = real(real_path)

        def execute(self, sql, *args):
            return _Cursor(self._con.execute(sql, *args).fetchall(), fail)

        def close(self):
            closed.append(True)
            self._con.close()
    monkeypatch.setattr(module.sqlite3, "connect", lambda *a, **k: Con())
    return closed


@pytest.mark.parametrize("scanner", ["update", "watchdog"])
@pytest.mark.parametrize("fail", [False, True])
def test_consent_after_a_later_unreadable_row_stays_unapproved(
        tmp_path, monkeypatch, scanner, fail):
    path = tmp_path / "ledger.db"
    con = _receipts_db(path)
    con.execute("INSERT INTO command_receipts(command_id,outcome,receipt_json,"
                "processed_at) VALUES(?,?,?,?)", ("bound", "applied", json.dumps(_BOUND), 9))
    con.execute("INSERT INTO command_receipts(command_id,outcome,receipt_json,"
                "processed_at) VALUES(?,?,?,?)", ("older", "applied", "{}", 1))
    con.commit()
    con.close()
    module = mcs_update if scanner == "update" else _load()
    closed = _fake_connect(monkeypatch, module, str(path), fail=fail)
    read = module._restore_consent if scanner == "update" else module._consent_for
    assert read(_REPORT) == (None if fail else "bound")   # fail closed, as fetchall did
    assert closed == [True]


@pytest.mark.parametrize("fail", [False, True])
def test_pending_scan_fails_closed_and_closes_on_unreadable_row(tmp_path, monkeypatch, fail):
    path = tmp_path / "ledger.db"
    con = _receipts_db(path)
    con.commit()
    con.close()
    closed = _fake_connect(monkeypatch, mcs_update, str(path), fail=fail)
    assert mcs_update.scan_pending_approvals({"executed": {}}) == ([], [])
    assert closed == [True]


def test_large_receipt_tables_are_streamed_not_materialised(tmp_path, monkeypatch):
    import tracemalloc
    import uuid
    path = tmp_path / "ledger.db"
    con = _receipts_db(path)
    filler = json.dumps({"cmd": "notification", "scheduled": False,
                         "result": {"text": "x" * 400}})
    con.executemany("INSERT INTO command_receipts(command_id,outcome,receipt_json,"
                    "processed_at) VALUES(?,?,?,?)",
                    ((uuid.uuid4().hex, "applied", filler, n + 10) for n in range(40000)))
    con.execute("INSERT INTO command_receipts(command_id,outcome,receipt_json,"
                "processed_at) VALUES(?,?,?,?)", ("bound", "applied", json.dumps(_BOUND), 1))
    con.commit()
    con.close()
    recover = _load()
    for module in (mcs_update, recover):
        monkeypatch.setattr(module, "LEDGER", str(path))
    for scan, expected in ((lambda: mcs_update._restore_consent(_REPORT), "bound"),
                           (lambda: recover._consent_for(_REPORT), "bound"),
                           (lambda: mcs_update.scan_pending_approvals({"executed": {}}),
                            ([], []))):
        tracemalloc.start()
        try:
            assert scan() == expected
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert peak < 5_000_000          # ~20 MB of rows are never held at once
