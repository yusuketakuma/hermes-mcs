"""Synthetic contracts for assist-summary comparison and adoption."""
import semantic_store
import semantic_transport

import json
import uuid

import pytest

import mcs_requests
import summary_review
from inference_version import CURRENT_VERSION, VERSION_KEY
from ledger import Ledger
from semantic_testkit import _message


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


def _setup(tmp_path, *, publication_mode="assist", audit_status="PASS", provenance=None):
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
    bundle = semantic_store.thread_bundle(db.db, 1, 1)
    policy = "a" * 64
    db.artifact_add("semantic_policy", policy)
    db.artifact_add(
        "extract_llm", json.dumps({"summary": "旧い要約", "points": ["旧い点"]}),
        project_id=1, message_id=1, model="local",
        meta={"hash": source_hash, VERSION_KEY: CURRENT_VERSION},
    )
    candidate_id = db.artifact_add(
        "semantic_summary",
        json.dumps({"target_message_id": 1, "claims": [{
            "section": "status", "text": "新しい監査済み要約",
            "claim_kind": "reported_fact",
        }], "limitations": []}, ensure_ascii=False),
        project_id=1, message_id=1, model=semantic_transport.LLM_MODEL,
        meta={"fingerprint": bundle["source_fingerprint"],
              "policy_fingerprint": policy,
              "target_revision": bundle["members"][0]["revision"],
              "audit_status": audit_status,
              "publication_mode": publication_mode,
              **({"provider_provenance": provenance} if provenance else {})},
    )
    return db, candidate_id


def test_local_model_change_makes_local_summary_not_adoptable(tmp_path, monkeypatch):
    db, candidate_id = _setup(tmp_path)
    assert summary_review.comparison(db.db, 1, 1, candidate_id)["adoptable"] is True
    monkeypatch.setattr(semantic_transport, "LLM_MODEL", "synthetic-local-b")
    reviewed = summary_review.comparison(db.db, 1, 1, candidate_id)
    assert reviewed["candidate"]["current"] is False
    assert reviewed["adoptable"] is False
    assert "candidate_producer_stale" in reviewed["reasons"]
    rejected = mcs_requests.apply_command(db, _command(
        "ops.adopt_summary", message_id=1, summary_artifact_id=candidate_id,
        comparison_hash=reviewed["comparison_hash"], reason="古い要約は採用しない"))
    assert rejected["outcome"] == "rejected"
    db.close()


def test_local_model_change_after_adoption_clears_current_adoption(tmp_path, monkeypatch):
    db, candidate_id = _setup(tmp_path)
    reviewed = summary_review.comparison(db.db, 1, 1, candidate_id)
    applied = mcs_requests.apply_command(db, _command(
        "ops.adopt_summary", message_id=1, summary_artifact_id=candidate_id,
        comparison_hash=reviewed["comparison_hash"], reason="採用する"))
    assert applied["outcome"] == "applied"
    adopted = summary_review.comparison(db.db, 1, 1, candidate_id)
    assert adopted["candidate"]["adopted"] is True
    monkeypatch.setattr(semantic_transport, "LLM_MODEL", "synthetic-local-b")
    changed = summary_review.comparison(db.db, 1, 1, candidate_id)
    assert changed["candidate"]["current"] is False
    assert changed["candidate"]["adopted"] is False
    assert changed["adoptions"] and not any(a["current"] for a in changed["adoptions"])
    db.close()


def test_local_model_change_keeps_cloud_summary_adoptable(tmp_path, monkeypatch):
    db, candidate_id = _setup(
        tmp_path, provenance={"providers": ["anthropic"], "complete": True})
    monkeypatch.setattr(semantic_transport, "LLM_MODEL", "synthetic-local-b")
    reviewed = summary_review.comparison(db.db, 1, 1, candidate_id)
    assert reviewed["candidate"]["current"] is True
    assert reviewed["adoptable"] is True
    assert "candidate_producer_stale" not in reviewed["reasons"]
    db.close()


@pytest.mark.parametrize("cloud,changed,expected", [
    (False, False, "PASS"), (False, True, "STALE"), (True, True, "PASS")])
def test_view_shows_summary_stale_after_local_model_change(
        tmp_path, monkeypatch, cloud, changed, expected):
    import ledger
    import mcs_view
    db, _ = _setup(tmp_path, provenance=(
        {"providers": ["anthropic"], "complete": True} if cloud else None))
    if changed:
        monkeypatch.setattr(semantic_transport, "LLM_MODEL", "synthetic-local-b")
    snapshot = ledger.publish_snapshot(
        str(tmp_path / "ledger.db"), str(tmp_path / "snapshots"))
    db.close()
    view = mcs_view.View(snapshot)
    try:
        row = view.read("semantic", project=1, message_id=1)["semantic"]["semantic_summary"][0]
        assert row["effective_status"] == expected
    finally:
        view.close()


def _drifting_screen(tmp_path, monkeypatch):
    """One screen: 3 local rows made by model A + 1 cloud row; the configured model drifts A→B."""
    import ledger
    import mcs_view
    db, _ = _setup(tmp_path)
    created_model = semantic_transport.LLM_MODEL
    fingerprint = semantic_store.thread_bundle(db.db, 1, 1)["source_fingerprint"]
    for kind in ("semantic_facts", "semantic_facts_v4"):
        db.artifact_add(kind, "{}", project_id=1, message_id=1, model=created_model,
                        meta={"fingerprint": fingerprint, "policy_fingerprint": "a" * 64,
                              "technical_status": "PASS"})
    db.artifact_add("semantic_facts", "{}", project_id=1, message_id=1, model="cloud-synthetic",
                    meta={"fingerprint": fingerprint, "technical_status": "PASS",
                          "provider_provenance": {"providers": ["anthropic"], "complete": True}})
    snapshot = ledger.publish_snapshot(
        str(tmp_path / "ledger.db"), str(tmp_path / "snapshots"))
    db.close()
    models = iter([created_model] + ["synthetic-local-b"] * 200)
    calls = []

    def drifting_model(*_a, **_k):
        calls.append(1)
        return next(models)

    monkeypatch.setattr(semantic_transport, "llm_model", drifting_model)
    return mcs_view.View(snapshot), created_model, calls


def _statuses(out, created_model):
    rows = [r for items in out.values() for r in items]
    local = [r["effective_status"] for r in rows if r["model"] == created_model]
    cloud = [r["effective_status"] for r in rows if r["model"] == "cloud-synthetic"]
    return local, cloud


def test_view_reads_local_model_once_so_one_screen_is_consistent(tmp_path, monkeypatch):
    view, created_model, calls = _drifting_screen(tmp_path, monkeypatch)
    try:
        local, cloud = _statuses(view.read("semantic", project=1, message_id=1)["semantic"],
                                 created_model)
        assert len(local) >= 3 and len(set(local)) == 1
        assert cloud == ["PASS"]
        assert len(calls) == 1
    finally:
        view.close()


def test_view_without_model_scope_would_split_one_screen(tmp_path, monkeypatch):
    import mcs_view
    view, created_model, calls = _drifting_screen(tmp_path, monkeypatch)
    try:
        out = mcs_view.View._semantic.__wrapped__(view, 1, 1)["semantic"]
        local, _cloud = _statuses(out, created_model)
        assert len(set(local)) > 1 and len(calls) > 1
    finally:
        view.close()


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
            meta["fingerprint"] = semantic_store.thread_bundle(db.db, 1, 1)["source_fingerprint"]
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


# Round-two receipt checks cover JSON ambiguity and command digest divergence.
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
