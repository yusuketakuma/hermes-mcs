"""Corrupt receipt rows cannot block or substitute bound restore consent."""
import json

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
