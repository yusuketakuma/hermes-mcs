"""Reason fields on human-confirmed request commands."""

import uuid

import pytest

import mcs_requests as requests
from test_mcs_features import _create, _source


def test_reason_is_recorded_for_create_and_update_and_replay(tmp_path):
    db = _source(tmp_path)
    create = _create(db, reason="人が確認した根拠")
    created = requests.apply_command(db, create)
    assert created["reason"] == "人が確認した根拠"
    assert requests.apply_command(db, create) == created

    update = {
        "cmd": "request.update", "version": 1,
        "command_id": str(uuid.uuid4()), "actor": "synthetic reviewer",
        "human_confirmed": True, "project_id": 1,
        "request_id": created["request_id"], "expected_revision": 1,
        "expected_source_hash": create["source_hash"],
        "patch": {"status": "in_progress"},
        "reason": "担当者が進捗を確認した",
    }
    updated = requests.apply_command(db, update)
    assert updated["reason"] == "担当者が進捗を確認した"

    without_reason = {**update, "command_id": str(uuid.uuid4()),
                      "expected_revision": 2,
                      "patch": {"status": "done"}}
    without_reason.pop("reason")
    omitted = requests.apply_command(db, without_reason)
    # the core enforces the approval record, not only the frontends
    assert omitted["outcome"] == "rejected"
    assert omitted["error"] == "reason_required"
    assert db.db.execute("SELECT status FROM requests").fetchone()[0] \
        == "in_progress"
    db.close()


@pytest.mark.parametrize("reason", [
    "", " \t", "x" * 2001, "contains\x00nul", None, 123,
])
def test_invalid_reason_is_rejected_without_creating_request(tmp_path, reason):
    db = _source(tmp_path)
    command = _create(db, reason=reason)
    receipt = requests.apply_command(db, command)
    assert receipt["outcome"] == "rejected"
    assert receipt["error"] == "bad_reason"
    assert receipt["reason"] is None
    assert db.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
    db.close()
