"""Displayed summary adoption requires the exact committed human-operation receipt."""
import json

import pytest

import mcs_requests
import summary_review
from test_summary_review import _command, _setup


@pytest.mark.parametrize("corruption", [
    "missing", "rejected", "json", "adoption_artifact_id", "project_id",
    "message_id", "summary_artifact_id", "comparison_hash", "actor", "reason", "error",
])
def test_unverified_adoption_is_not_displayed_as_adopted(tmp_path, corruption):
    store, candidate_id = _setup(tmp_path)
    try:
        reviewed = summary_review.comparison(store.db, 1, 1, candidate_id)
        command = _command(
            "ops.adopt_summary", message_id=1, summary_artifact_id=candidate_id,
            comparison_hash=reviewed["comparison_hash"], reason="synthetic review")
        receipt = mcs_requests.apply_command(store, command)
        assert receipt["outcome"] == "applied"
        with store.db:
            if corruption == "missing":
                store.db.execute("DELETE FROM command_receipts WHERE command_id=?",
                                 (command["command_id"],))
            elif corruption == "rejected":
                store.db.execute("UPDATE command_receipts SET outcome='rejected' WHERE command_id=?",
                                 (command["command_id"],))
            else:
                if corruption != "json":
                    receipt[corruption] = (receipt[corruption] + 1
                                           if type(receipt[corruption]) is int else "synthetic-mismatch")
                raw = "[]" if corruption == "json" else json.dumps(receipt)
                store.db.execute("UPDATE command_receipts SET receipt_json=? WHERE command_id=?",
                                 (raw, command["command_id"]))
        result = summary_review.comparison(store.db, 1, 1, candidate_id)
        assert result["candidate"]["adopted"] is False
        assert result["adoptions"][0]["receipt_verified"] is False
        assert result["adoptions"][0]["current"] is False
    finally:
        store.close()
