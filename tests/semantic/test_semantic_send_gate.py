"""Semantic outbox gates bind source, target, policy, and publication stage."""
import json

import pytest

import notifier
import semantic
from test_mcs_semantic import _cfg
from test_semantic_delivery import _db, _message, _patient, _semantic_event


def test_normal_gate_requires_source_target_revision_and_pass_summary(
        tmp_path, monkeypatch):
    db = _db(tmp_path, [_message()])
    monkeypatch.setattr(notifier, "_config", lambda: _cfg("enforce"))
    event = _semantic_event(db)
    payload = json.loads(event["payload"])

    notifier._semantic_gate(db, event, payload)

    missing_target = dict(payload)
    missing_target.pop("target_message_id")
    with pytest.raises(ValueError, match="payload_invalid"):
        notifier._semantic_gate(db, event, missing_target)

    wrong_revision = dict(payload, target_revision="old-revision")
    with pytest.raises(notifier._StaleSend, match="stale_generation"):
        notifier._semantic_gate(db, event, wrong_revision)

    other_source = db.outbox_add("new_messages", 2, {"message_ids": [1]})
    wrong_project = dict(payload, src_event_id=other_source)
    with pytest.raises(notifier._StaleSend, match="src_event_ineligible"):
        notifier._semantic_gate(db, event, wrong_project)

    summary = db.db.execute(
        "SELECT artifact_id,meta FROM artifacts "
        "WHERE kind='semantic_summary' AND message_id=1 "
        "ORDER BY artifact_id DESC LIMIT 1").fetchone()
    meta = json.loads(summary["meta"])
    meta["publication_mode"] = "shadow"
    db.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?",
                  (json.dumps(meta), summary["artifact_id"]))
    db.db.commit()
    with pytest.raises(notifier._StaleSend, match="summary_stale"):
        notifier._semantic_gate(db, event, payload)
    db.close()


def test_degraded_gate_requires_frozen_generation_and_unattempted_source(
        tmp_path, monkeypatch):
    db = _db(tmp_path, [_message()])
    monkeypatch.setattr(notifier, "_config", lambda: _cfg("enforce"))
    source_id = db.outbox_add("new_messages", 1, {"message_ids": [1]})
    bundle = semantic.thread_bundle(db, 1, 1)
    policy = semantic.policy_fingerprint(
        semantic.semantic_config(_cfg("enforce"))[0])
    payload = {
        "root_id": 1, "src_event_id": source_id,
        "fingerprint": bundle["source_fingerprint"],
        "policy_fingerprint": policy,
        "target_message_ids": [1], "degraded": True,
        "policy_version": semantic.POLICY_VERSION, "text": "保留通知",
    }
    notice_id = db.outbox_add("semantic_notice", 1, payload)
    event = db.db.execute(
        "SELECT * FROM notify_outbox WHERE event_id=?", (notice_id,)
    ).fetchone()

    notifier._semantic_gate(db, event, payload)

    missing_targets = dict(payload)
    missing_targets.pop("target_message_ids")
    with pytest.raises(ValueError, match="payload_invalid"):
        notifier._semantic_gate(db, event, missing_targets)

    db.db.execute(
        "UPDATE notify_outbox SET state='failed',attempts=1,progress=? "
        "WHERE event_id=?", (json.dumps({}), source_id))
    db.db.commit()
    with pytest.raises(notifier._StaleSend, match="base_delivered"):
        notifier._semantic_gate(db, event, payload)

    db.db.execute(
        "UPDATE notify_outbox SET state='pending',attempts=0,progress=? "
        "WHERE event_id=?", (json.dumps({}), source_id))
    db.db.commit()
    db.save_patient(_patient([_message(body="訂正された本文", unread=False)]),
                    notify=None)
    with pytest.raises(notifier._StaleSend, match="stale_generation"):
        notifier._semantic_gate(db, event, payload)
    db.close()


def test_raw_notice_ignores_shadow_publication(tmp_path, monkeypatch):
    db = _db(tmp_path, [_message()])
    monkeypatch.setattr(notifier, "_config", lambda: _cfg("enforce"))
    event = db.db.execute(
        "SELECT * FROM notify_outbox WHERE kind='new_messages'"
    ).fetchone()
    bundle = semantic.thread_bundle(db, 1, 1)
    policy = semantic.policy_fingerprint(
        semantic.semantic_config(_cfg("enforce"))[0])
    content = {"claims": [{"text": "shadow claim"}], "limitations": []}
    meta = {"fingerprint": bundle["source_fingerprint"],
            "policy_fingerprint": policy, "audit_status": "PASS",
            "publication_mode": "shadow",
            "target_revision": bundle["members"][0]["revision"]}
    db.artifact_add("semantic_summary", json.dumps(content), project_id=1,
                    message_id=1, model="Qwen3.5-9B", meta=meta)
    assert notifier._semantic_render_state(db, event) == ()
    assert "要約（自動検査済）" not in notifier._format_event(db, event)[0]

    meta["publication_mode"] = "enforce"
    db.artifact_add("semantic_summary", json.dumps(content), project_id=1,
                    message_id=1, model="Qwen3.5-9B", meta=meta)
    assert notifier._semantic_render_state(db, event)
    assert "要約（自動検査済）" in notifier._format_event(db, event)[0]
    db.close()
