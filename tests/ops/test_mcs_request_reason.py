"""Reason fields on human-confirmed request commands."""

import uuid
import json

import pytest

import mcs_requests as requests
from ops_testkit import _create, _source


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


@pytest.mark.parametrize("kind", ["extract_v1", "extract_llm"])
@pytest.mark.parametrize("field", ["content", "meta"])
def test_deep_latest_extraction_hides_suggestions_without_history_fallback(tmp_path, kind, field):
    db = _source(tmp_path)
    try:
        source = db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
        content = {"requests": [{"kind": "confirm", "ctx": "synthetic-v1",
                                  "to": "synthetic-recipient", "action": "synthetic-llm"}]}
        meta = {"hash": source["content_hash"]}
        db.artifact_add(kind, json.dumps(content), project_id=1, message_id=1, meta=meta)
        assert any(item["extraction_kind"] == kind for item in requests.candidates(db.db, source))
        latest = db.artifact_add(kind, json.dumps(content), project_id=1, message_id=1, meta=meta)
        raw = "[" * 1500 + "0" + "]" * 1500
        db.db.execute(f"UPDATE artifacts SET {field}=? WHERE artifact_id=?", (raw, latest))
        before = db.db.total_changes
        result = requests.candidates(db.db, source)
        assert all(item["extraction_kind"] != kind for item in result)
        assert db.db.total_changes == before
    finally:
        db.close()
