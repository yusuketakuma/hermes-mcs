"""Corrupt stored coverage floors cannot strand a confirmed scan or create work."""
import pytest

import mcs_requests
from ledger import Ledger
from test_mcs_operations import _command


@pytest.mark.parametrize("floor", ["synthetic-invalid", b"synthetic-invalid", float("inf"),
                                  float("-inf"), -2])
def test_corrupt_stored_floor_returns_rejected_receipt_without_new_job(tmp_path, floor):
    store = Ledger(str(tmp_path / "synthetic.db"))
    try:
        store.ensure_patient(1)
        with store.db:
            store.db.execute("UPDATE patients SET history_floor=? WHERE project_id=1", (floor,))
        result = mcs_requests.apply_command(store, _command("ops.scan", days=14, pages=5))
        assert result["outcome"] == "rejected" and result["error"] == "invalid_history_floor"
        assert store.db.execute("SELECT COUNT(*) FROM fetch_jobs").fetchone()[0] == 0
        assert store.db.execute("SELECT COUNT(*) FROM command_receipts").fetchone()[0] == 1
    finally:
        store.close()


@pytest.mark.parametrize("floor,expected", [(None, "applied"), (0, "applied"),
                                          (-1, "rejected"), (1e12, "applied")])
def test_existing_floor_sentinels_and_finite_future_remain_compatible(tmp_path, floor, expected):
    store = Ledger(str(tmp_path / "synthetic.db"))
    try:
        store.ensure_patient(1)
        with store.db:
            store.db.execute("UPDATE patients SET history_floor=? WHERE project_id=1", (floor,))
        result = mcs_requests.apply_command(store, _command("ops.scan", days=14, pages=5))
        assert result["outcome"] == expected
        if floor == -1:
            assert result["error"] == "already_floored"
    finally:
        store.close()
