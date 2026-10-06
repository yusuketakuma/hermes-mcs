"""Reference approval requires an unambiguous receipt for the exact command."""
import json

import pytest

import mcs_refstats
import mcs_requests
from test_mcs_refstats import _approve_req, _capture, _db, _pending_hash


@pytest.mark.parametrize("corruption", ["duplicate", "column_hash", "json_hash", "both_hash"])
def test_ambiguous_or_conflicting_receipt_cannot_verify_reference(tmp_path, capsys, corruption):
    store = _db(tmp_path)
    try:
        _capture(tmp_path)
        command = _approve_req("base", _pending_hash(tmp_path))
        receipt = mcs_requests.apply_command(store, command)
        assert receipt["outcome"] == "applied"
        artifact_id = receipt["refstat_artifact_id"]
        content = json.loads(store.db.execute(
            "SELECT content FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()[0])
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
        assert not mcs_refstats._receipt_applied(store.db, content, artifact_id)
    finally:
        store.close()
    capsys.readouterr()


def test_maximum_unicode_actor_reason_still_verifies_reference(tmp_path, capsys):
    store = _db(tmp_path)
    try:
        _capture(tmp_path)
        command = _approve_req("base", _pending_hash(tmp_path), actor="😀" * 120, reason="😀" * 2000)
        receipt = mcs_requests.apply_command(store, command)
        assert receipt["outcome"] == "applied"
        artifact_id = receipt["refstat_artifact_id"]
        content = json.loads(store.db.execute(
            "SELECT content FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()[0])
        assert mcs_refstats._receipt_applied(store.db, content, artifact_id)
    finally:
        store.close()
    capsys.readouterr()
