"""Human adoption of current Loop candidates through request receipts."""

import json
import sqlite3
import time
import uuid

import pytest

import ledger
import mcs_requests as requests
import mcs_view
import semantic
import semantic_loops
from test_mcs_semantic import _cfg, _message, _seeded
from test_semantic_loop_generations import _pending_fact


def _candidate_setup(tmp_path):
    db = _seeded(tmp_path)
    cfg, _ = semantic.semantic_config(_cfg())
    policy = semantic.policy_fingerprint(cfg)
    db.artifact_add("semantic_policy", policy)
    bundle = semantic.thread_bundle(db, 1, 1, [1])
    semantic_loops.update_loops(
        db, 1, bundle, {1: _pending_fact(bundle["members"][0])},
        None, cfg, time.monotonic() + 30)
    row = db.artifacts("loop_candidate", project_id=1)[0]
    source_hash = db.db.execute(
        "SELECT content_hash FROM messages WHERE project_id=1 AND message_id=1"
    ).fetchone()[0]
    return db, row, bundle, policy, source_hash


def _create(db, row, bundle, policy, source_hash, **extra):
    return {
        "version": 1, "cmd": "request.create", "command_id": str(uuid.uuid4()),
        "actor": "synthetic reviewer", "human_confirmed": True,
        "project_id": 1, "source_message_id": 1, "source_hash": source_hash,
        "title": "human adopted loop", "reason": "人が同一対象として確認",
        "loop_ref": {
            "artifact_id": row["artifact_id"],
            "source_fingerprint": bundle["source_fingerprint"],
            "policy_fingerprint": policy,
            "match_confirmed": True,
        },
        **extra,
    }


def test_current_loop_adoption_links_once_and_keeps_candidate_immutable(tmp_path):
    db, row, bundle, policy, source_hash = _candidate_setup(tmp_path)
    original_candidate = row["content"]
    req = _create(db, row, bundle, policy, source_hash)

    created = requests.apply_command(db, req)
    replay = requests.apply_command(db, req)
    assert created["outcome"] == "applied"
    assert created == replay
    assert created["loop_ref"]["artifact_id"] == row["artifact_id"]
    assert db.db.execute(
        "SELECT COUNT(*) FROM artifacts WHERE kind='request_loop_link'"
    ).fetchone()[0] == 1
    link = json.loads(db.db.execute(
        "SELECT content FROM artifacts WHERE kind='request_loop_link'"
    ).fetchone()[0])
    assert link["request_id"] == created["request_id"]
    assert link["loop_artifact_id"] == row["artifact_id"]
    assert db.db.execute(
        "SELECT content FROM artifacts WHERE artifact_id=?", (row["artifact_id"],)
    ).fetchone()[0] == original_candidate

    update = {
        "version": 1, "cmd": "request.update", "command_id": str(uuid.uuid4()),
        "actor": req["actor"], "human_confirmed": True, "project_id": 1,
        "request_id": created["request_id"], "expected_revision": 1,
        "expected_source_hash": source_hash,
        "patch": {"status": "in_progress"}, "reason": req["reason"],
        "loop_ref": req["loop_ref"],
    }
    updated = requests.apply_command(db, update)
    assert updated["outcome"] == "applied"
    assert db.db.execute(
        "SELECT status,revision FROM requests WHERE request_id=?",
        (created["request_id"],)).fetchone()[0:2] == ("in_progress", 2)
    assert db.db.execute(
        "SELECT COUNT(*) FROM artifacts WHERE kind='loop_candidate'"
    ).fetchone()[0] == 1
    db.close()


def test_stale_loop_rejected_and_link_failure_rolls_back(tmp_path):
    db, row, bundle, policy, source_hash = _candidate_setup(tmp_path)
    req = _create(db, row, bundle, policy, source_hash)
    db.save_messages([_message(1, body=bundle["members"][0]["body_original"] + "追記しました。")])
    changed_hash = db.db.execute(
        "SELECT content_hash FROM messages WHERE project_id=1 AND message_id=1"
    ).fetchone()[0]
    stale = {**req, "command_id": str(uuid.uuid4()), "source_hash": changed_hash}
    rejected = requests.apply_command(db, stale)
    assert rejected["outcome"] == "rejected"
    assert rejected["error"] == "loop_source_stale"
    assert db.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0

    # Rebuild a current candidate, then deny only the link artifact INSERT.
    cfg, _ = semantic.semantic_config(_cfg())
    current = semantic.thread_bundle(db, 1, 1, [1])
    semantic_loops.update_loops(
        db, 1, current, {1: _pending_fact(current["members"][0])},
        None, cfg, time.monotonic() + 30)
    latest = db.artifacts("loop_candidate", project_id=1)[-1]
    current_policy = db.db.execute(
        "SELECT content FROM artifacts WHERE kind='semantic_policy' "
        "ORDER BY artifact_id DESC LIMIT 1").fetchone()[0]
    current_hash = db.db.execute(
        "SELECT content_hash FROM messages WHERE project_id=1 AND message_id=1"
    ).fetchone()[0]
    rollback_req = _create(
        db, latest, current, current_policy, current_hash,
        command_id=str(uuid.uuid4()))

    db.db.set_authorizer(
        lambda action, table, *rest:
        sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_INSERT and table == "artifacts"
        else sqlite3.SQLITE_OK)
    with pytest.raises(sqlite3.DatabaseError):
        requests.apply_command(db, rollback_req)
    db.db.set_authorizer(None)
    assert db.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
    assert db.db.execute("SELECT COUNT(*) FROM command_receipts").fetchone()[0] == 1
    db.close()


def test_loop_adoption_requires_exact_original_quote(tmp_path):
    db, row, bundle, policy, source_hash = _candidate_setup(tmp_path)
    try:
        for evidence_change in (None, {"quote": "not the original quote"},
                                {"end_codepoint": 2**63}, {"message_id": True}):
            candidate = json.loads(row["content"])
            candidate["origin"]["evidence"] = (
                None if evidence_change is None else
                {**candidate["origin"]["evidence"], **evidence_change})
            with db.db:
                db.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?",
                              (json.dumps(candidate), row["artifact_id"]))
            receipt = requests.apply_command(
                db, _create(db, row, bundle, policy, source_hash))
            assert receipt["error"] == "loop_evidence_invalid"
        assert db.db.execute("SELECT count(*) FROM requests").fetchone()[0] == 0
        assert not db.artifacts("request_loop_link", project_id=1)
        snapshot = ledger.publish_snapshot(str(tmp_path / "ledger.db"), str(tmp_path / "snap"))
        view = mcs_view.View(snapshot)
        try:
            candidate = view.read("loops", project=1)["items"][0]
            assert candidate["current"] and not candidate["adoption_eligible"]
        finally:
            view.close()
    finally:
        db.close()
