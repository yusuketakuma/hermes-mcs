"""Round-two receipt checks cover JSON ambiguity and command digest divergence."""
import json

import pytest

import mcs_requests
import summary_review
from test_summary_review import _command, _setup


@pytest.mark.parametrize("corruption", ["duplicate", "column_hash", "json_hash", "both_hash"])
def test_ambiguous_or_conflicting_receipt_cannot_verify_adoption(tmp_path, corruption):
    store, summary_id = _setup(tmp_path)
    try:
        comparison = summary_review.comparison(store.db, 1, 1, summary_id)
        command = _command("ops.adopt_summary", message_id=1, summary_artifact_id=summary_id,
                           comparison_hash=comparison["comparison_hash"], reason="synthetic review")
        receipt = mcs_requests.apply_command(store, command)
        assert receipt["outcome"] == "applied"
        raw = json.dumps(receipt)
        if corruption == "duplicate":
            raw = '{"outcome":"rejected",' + raw[1:]
        if corruption in ("json_hash", "both_hash"):
            receipt["payload_hash"] = "f" * 64
            raw = json.dumps(receipt)
        with store.db:
            store.db.execute("UPDATE command_receipts SET receipt_json=? WHERE command_id=?",
                             (raw, command["command_id"]))
            if corruption in ("column_hash", "both_hash"):
                store.db.execute("UPDATE command_receipts SET payload_hash=? WHERE command_id=?",
                                 ("f" * 64, command["command_id"]))
        result = summary_review.comparison(store.db, 1, 1, summary_id)
        assert not result["candidate"]["adopted"]
        assert not result["adoptions"][0]["receipt_verified"]
    finally:
        store.close()


def test_maximum_unicode_actor_reason_still_verifies_adoption(tmp_path):
    store, summary_id = _setup(tmp_path)
    try:
        comparison = summary_review.comparison(store.db, 1, 1, summary_id)
        command = _command("ops.adopt_summary", message_id=1, summary_artifact_id=summary_id,
                           comparison_hash=comparison["comparison_hash"],
                           actor="😀" * 120, reason="😀" * 2000)
        receipt = mcs_requests.apply_command(store, command)
        assert receipt["outcome"] == "applied"
        result = summary_review.comparison(store.db, 1, 1, summary_id)
        assert result["candidate"]["adopted"]
        assert result["adoptions"][0]["receipt_verified"]
    finally:
        store.close()
