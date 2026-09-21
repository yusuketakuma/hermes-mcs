"""Synthetic contracts for assist-summary comparison and adoption."""

import json
import uuid

import pytest

import mcs_requests
import semantic
import summary_review
from ledger import Ledger
from test_mcs_semantic import _message


def _command(cmd, project_id=1, **fields):
    return {
        "version": 1,
        "cmd": cmd,
        "command_id": str(uuid.uuid4()),
        "actor": "synthetic-reviewer",
        "human_confirmed": True,
        "project_id": project_id,
        **fields,
    }


def _setup(tmp_path, *, publication_mode="assist", audit_status="PASS"):
    db = Ledger(str(tmp_path / "ledger.db"))
    db.save_patient(
        type("Patient", (), {
            "project_id": 1, "project_type": "medical",
            "patient_name": "患者", "disease": "", "station_name": "",
            "url": "https://example.invalid/p/1",
            "fetch_state": "complete", "fetch_reason": None,
            "messages": [_message()],
        })(),
        notify={"source": "unread"},
    )
    source_hash = db.db.execute(
        "SELECT content_hash FROM messages WHERE message_id=1"
    ).fetchone()[0]
    bundle = semantic.thread_bundle(db, 1, 1)
    policy = "a" * 64
    db.artifact_add("semantic_policy", policy)
    db.artifact_add(
        "extract_llm", json.dumps({"summary": "旧い要約", "points": ["旧い点"]}),
        project_id=1, message_id=1, model="local",
        meta={"hash": source_hash},
    )
    candidate_id = db.artifact_add(
        "semantic_summary",
        json.dumps({"target_message_id": 1, "claims": [{
            "section": "status", "text": "新しい監査済み要約",
            "claim_kind": "reported_fact",
        }], "limitations": []}, ensure_ascii=False),
        project_id=1, message_id=1, model="local",
        meta={"fingerprint": bundle["source_fingerprint"],
              "policy_fingerprint": policy,
              "target_revision": bundle["members"][0]["revision"],
              "audit_status": audit_status,
              "publication_mode": publication_mode},
    )
    return db, candidate_id


def test_comparison_adoption_is_replayed_without_outbox_mutation(tmp_path):
    db, candidate_id = _setup(tmp_path)
    before_outbox = db.db.execute(
        "SELECT COUNT(*) FROM notify_outbox"
    ).fetchone()[0]
    reviewed = summary_review.comparison(db.db, 1, 1, candidate_id)
    assert reviewed["adoptable"] is True
    assert "旧い要約" in reviewed["diff"]
    assert "新しい監査済み要約" in reviewed["diff"]
    assert reviewed["comparison_hash"]
    assert reviewed["candidate"]["adopted"] is False

    req = _command(
        "ops.adopt_summary", message_id=1,
        summary_artifact_id=candidate_id,
        comparison_hash=reviewed["comparison_hash"],
        reason="人が比較して採用を確認",
    )
    first = mcs_requests.apply_command(db, req)
    replay = mcs_requests.apply_command(db, req)
    assert first == replay
    assert first["outcome"] == "applied"
    assert first["adoption_artifact_id"] > 0
    assert db.db.execute(
        "SELECT COUNT(*) FROM artifacts WHERE kind='semantic_adoption'"
    ).fetchone()[0] == 1
    assert db.db.execute(
        "SELECT COUNT(*) FROM notify_outbox"
    ).fetchone()[0] == before_outbox
    adopted = summary_review.comparison(db.db, 1, 1, candidate_id)
    assert adopted["candidate"]["adopted"] is True
    assert len(adopted["adoptions"]) == 1
    db.close()


def test_stale_and_nonassist_or_paused_candidates_cannot_be_adopted(tmp_path):
    db, candidate_id = _setup(tmp_path, publication_mode="shadow")
    reviewed = summary_review.comparison(db.db, 1, 1, candidate_id)
    assert reviewed["adoptable"] is False
    assert "candidate_not_assist" in reviewed["reasons"]

    req = _command(
        "ops.adopt_summary", message_id=1,
        summary_artifact_id=candidate_id,
        comparison_hash=reviewed["comparison_hash"],
        reason="shadowは採用しない",
    )
    rejected = mcs_requests.apply_command(db, req)
    assert rejected["outcome"] == "rejected"
    assert rejected["error"] == "summary_not_adoptable"

    db.db.execute(
        "UPDATE artifacts SET meta=? WHERE artifact_id=?",
        (json.dumps({"fingerprint": reviewed["candidate"]["meta"]["fingerprint"],
                     "policy_fingerprint": reviewed["candidate"]["meta"]["policy_fingerprint"],
                     "target_revision": reviewed["candidate"]["meta"]["target_revision"],
                     "audit_status": "PASS", "publication_mode": "assist"}),
         candidate_id),
    )
    db.db.commit()
    current = summary_review.comparison(db.db, 1, 1, candidate_id)
    assert current["adoptable"] is True
    db.save_messages([_message(body="原文が変わりました")])
    stale = summary_review.comparison(db.db, 1, 1, candidate_id)
    assert stale["adoptable"] is False
    assert "baseline_stale" in stale["reasons"]
    assert "candidate_source_stale" in stale["reasons"]
    db.close()


def test_paused_summary_rejected_and_malformed_candidate_is_fail_closed(tmp_path):
    db, candidate_id = _setup(tmp_path)
    reviewed = summary_review.comparison(db.db, 1, 1, candidate_id)
    db.artifact_add("semantic_control", "paused", project_id=1,
                    meta={"command_id": str(uuid.uuid4()), "actor": "test"})
    paused = summary_review.comparison(db.db, 1, 1, candidate_id)
    assert paused["adoptable"] is False
    assert "paused" in paused["reasons"]

    db.db.execute(
        "UPDATE artifacts SET content=? WHERE artifact_id=?",
        (json.dumps({"claims": [{"text": "candidate"}],
                     "target_message_id": True}), candidate_id),
    )
    db.db.commit()
    malformed_target = summary_review.comparison(db.db, 1, 1, candidate_id)
    assert malformed_target["adoptable"] is False
    assert "candidate_target_mismatch" in malformed_target["reasons"]

    db.db.execute(
        "UPDATE artifacts SET content=? WHERE artifact_id=?",
        (json.dumps({"claims": "invalid", "target_message_id": 1}), candidate_id),
    )
    db.db.commit()
    with pytest.raises(ValueError, match="candidate_malformed"):
        summary_review.comparison(db.db, 1, 1, candidate_id)
    assert reviewed["comparison_hash"]
    db.close()


def test_partial_context_cannot_be_adopted_even_with_matching_pass_artifact(tmp_path):
    db, candidate_id = _setup(tmp_path)
    try:
        with db.db:
            db.db.execute("UPDATE messages SET reply_count=1 WHERE message_id=1")
            meta = json.loads(db.db.execute("SELECT meta FROM artifacts WHERE artifact_id=?",
                                          (candidate_id,)).fetchone()[0])
            meta["fingerprint"] = semantic.thread_bundle(db, 1, 1)["source_fingerprint"]
            db.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?",
                          (json.dumps(meta), candidate_id))
        reviewed = summary_review.comparison(db.db, 1, 1, candidate_id)
        assert reviewed["reasons"] == ["context_incomplete"]
        assert not reviewed["adoptable"]
        command = _command("ops.adopt_summary", message_id=1,
                           summary_artifact_id=candidate_id,
                           comparison_hash=reviewed["comparison_hash"], reason="Human review")
        receipt = mcs_requests.apply_command(db, command)
        assert receipt["error"] == "summary_not_adoptable"
        assert not db.artifacts("semantic_adoption", project_id=1)
    finally:
        db.close()
