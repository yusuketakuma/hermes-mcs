"""Receipt lookups cannot escape the notification channel's project scope."""
import json

import ledger
import mcs_view
import pytest


@pytest.mark.parametrize("receipt", [
    {"actor": "discord:7", "project_id": 9, "before": {"title": "synthetic-private"}},
    {"kind": "refresh", "actor": "discord:7", "project_id": 9},
])
def test_projectless_lookup_does_not_leak_other_receipt_scope(tmp_path, receipt):
    db_path = tmp_path / "ledger.db"
    led = ledger.Ledger(str(db_path))
    try:
        with led.db:
            led.db.execute("INSERT INTO command_receipts VALUES(?,?,?,?,?,?,?)",
                           ("synthetic-command", "f" * 64, 9, None, "applied",
                            json.dumps(dict(receipt, outcome="applied")), 1))
        snapshots = tmp_path / "snapshots"
        snapshots.mkdir()
        ledger.publish_snapshot(str(db_path), str(snapshots))
        view = mcs_view.View(snapshots / "ledger-snapshot.db")
        try:
            result = view.notification_receipt("synthetic-command", context={
                "actor": "discord:7", "projects": [1], "operator": False})
            assert result["outcome"] == "rejected"
            assert "before" not in result
            assert "project_id" not in result
        finally:
            view.close()
    finally:
        led.db.close()
