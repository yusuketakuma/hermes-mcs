"""Synthetic degraded notice receipt and delivery boundary regressions."""
import json

import pytest

import mcs_util
import semantic_notice
import semantic_policy
import semantic_send_gate as gate
from semantic_testkit import _cfg, _delivery_db, _message


@pytest.fixture
def world(tmp_path):
    db = _delivery_db(tmp_path, [_message()])
    cfg = _cfg("enforce")
    try:
        with db.db:
            db.db.execute("UPDATE notify_outbox SET created_at=0 WHERE kind='new_messages'")
        assert semantic_notice.emit_degraded(db, semantic_policy.semantic_config(cfg)[0]) == 1
        event = db.db.execute("SELECT * FROM notify_outbox WHERE kind='semantic_notice'").fetchone()
        yield db, cfg, event, json.loads(event["payload"])
    finally:
        db.close()


def _held(db, cfg, event, payload, reason):
    before = tuple(db.db.execute("SELECT state,attempts,progress FROM notify_outbox WHERE event_id=?",
                                 (event["event_id"],)).fetchone())
    with pytest.raises(gate.DeferredSend, match=reason):
        gate.semantic_gate(db.db, event, payload, cfg)
    with pytest.raises(gate.FreezeSend, match=reason):
        gate.semantic_gate(db.db, event, payload, cfg, in_progress=True)
    with pytest.raises(gate.DeferredSend, match=reason):
        gate.marker_source_gate(db.db, 1, payload, cfg=cfg)
    with pytest.raises(gate.FreezeSend, match=reason):
        gate.marker_source_gate(db.db, 1, payload, in_progress=True, cfg=cfg)
    assert tuple(db.db.execute("SELECT state,attempts,progress FROM notify_outbox WHERE event_id=?",
                               (event["event_id"],)).fetchone()) == before


def test_generated_notice_is_bound_and_deduplicated(world):
    db, cfg, event, payload = world
    gate.semantic_gate(db.db, event, payload, cfg)
    gate.marker_source_gate(db.db, 1, payload, cfg=cfg)
    assert semantic_notice.emit_degraded(db, semantic_policy.semantic_config(cfg)[0]) == 0
    assert db.db.execute("SELECT count(*) FROM artifacts WHERE kind='semantic_degraded_notice_receipt'").fetchone()[0] == 1


@pytest.mark.parametrize("change", ["legacy", "text", "text_and_hash", "targets", "source", "root",
                                     "policy", "fingerprint", "delivery_key", "kind_flag", "receipt_id"])
def test_payload_changes_and_legacy_are_held(world, change):
    db, cfg, event, payload = world
    if change == "legacy":
        payload.pop("degraded_receipt_id")
    elif change == "text":
        payload["text"] += " altered"
    elif change == "text_and_hash":
        payload["text"] += " altered"
        payload["notice_text_hash"] = mcs_util.payload_hash(payload["text"])
    elif change == "targets":
        payload["target_message_ids"] = [1, 2]
    elif change == "source":
        payload["src_event_id"] += 1
    elif change == "root":
        payload["root_id"] += 1
    elif change == "policy":
        payload["policy_fingerprint"] = "0" * 64
    elif change == "fingerprint":
        payload["fingerprint"] = "0" * 64
    elif change == "delivery_key":
        payload["delivery_key"] = "0" * 64
    elif change == "kind_flag":
        payload["degraded"] = False
    else:
        payload["degraded_receipt_id"] += 999
    _held(db, cfg, event, payload, "degraded_unverified")


@pytest.mark.parametrize("change", ["content", "duplicate_content", "duplicate_meta", "invalidated", "version"])
def test_receipt_corruption_is_held(world, change):
    db, cfg, event, payload = world
    receipt_id = payload["degraded_receipt_id"]
    row = db.db.execute("SELECT content,meta FROM artifacts WHERE artifact_id=?", (receipt_id,)).fetchone()
    content, meta = row["content"], row["meta"]
    if change == "content":
        obj = json.loads(content)
        obj["notice_text_hash"] = "0" * 64
        content = json.dumps(obj)
    elif change == "duplicate_content":
        content = content[:-1] + ', "root_id": 1}'
    elif change == "duplicate_meta":
        meta = meta[:-1] + ', "fingerprint": "duplicate"}'
    else:
        obj = json.loads(meta)
        obj["invalidated" if change == "invalidated" else "inference_engine_version"] = True
        meta = json.dumps(obj)
    with db.db:
        db.db.execute("UPDATE artifacts SET content=?,meta=? WHERE artifact_id=?", (content, meta, receipt_id))
    _held(db, cfg, event, payload, "degraded_unverified")


@pytest.mark.parametrize("progress", ['not-json', '[]', '{"sent":[],"sent":[]}',
                                      '{"sent":false}', '{"next":1,"sent":[]}',
                                      '{"next":0,"sent":[],"sending":0}', '{"x":NaN}', 0])
def test_unreadable_origin_progress_is_held(world, progress):
    db, cfg, event, payload = world
    with db.db:
        db.db.execute("UPDATE notify_outbox SET progress=? WHERE event_id=?", (progress, payload["src_event_id"]))
    _held(db, cfg, event, payload, "src_event_unverified")


def test_confirmed_origin_delivery_is_stale_or_frozen(world):
    db, cfg, event, payload = world
    with db.db:
        db.db.execute("UPDATE notify_outbox SET progress=? WHERE event_id=?",
                      (json.dumps({"sent": ["42"], "next": 1}), payload["src_event_id"]))
    with pytest.raises(gate.StaleSend, match="base_delivered"):
        gate.semantic_gate(db.db, event, payload, cfg)
    with pytest.raises(gate.FreezeSend, match="base_delivered"):
        gate.marker_source_gate(db.db, 1, payload, in_progress=True, cfg=cfg)


def test_source_change_between_gate_and_marker_is_stale(world):
    db, cfg, event, payload = world
    gate.semantic_gate(db.db, event, payload, cfg)
    db.save_messages([_message(body="完全合成の変更済み本文")], project_id=1)
    with pytest.raises(gate.StaleSend, match="stale_generation"):
        gate.marker_source_gate(db.db, 1, payload, cfg=cfg)
    with pytest.raises(gate.FreezeSend, match="stale_generation"):
        gate.marker_source_gate(db.db, 1, payload, in_progress=True, cfg=cfg)


def test_receipt_changed_between_gate_and_marker_is_held(world):
    db, cfg, event, payload = world
    gate.semantic_gate(db.db, event, payload, cfg)
    with db.db:
        db.db.execute("UPDATE artifacts SET content='{}' WHERE artifact_id=?", (payload["degraded_receipt_id"],))
    with pytest.raises(gate.DeferredSend, match="degraded_unverified"):
        gate.marker_source_gate(db.db, 1, payload, cfg=cfg)
    with pytest.raises(gate.FreezeSend, match="degraded_unverified"):
        gate.marker_source_gate(db.db, 1, payload, in_progress=True, cfg=cfg)


def test_another_receipt_does_not_hide_bound_notice(world):
    db, cfg, event, payload = world
    db.artifact_add("semantic_degraded_notice_receipt", "{}", project_id=1, message_id=1, meta={})
    gate.semantic_gate(db.db, event, payload, cfg)
    gate.marker_source_gate(db.db, 1, payload, cfg=cfg)


def test_receipt_from_another_project_is_rejected(world):
    from semantic_testkit import _patient

    db, cfg, event, payload = world
    row = db.db.execute("SELECT content,meta FROM artifacts WHERE artifact_id=?",
                        (payload["degraded_receipt_id"],)).fetchone()
    _patient(db, 2)
    db.save_messages([_message(mid=2, pid=2, unread=False)], project_id=2)
    payload["degraded_receipt_id"] = db.artifact_add(
        "semantic_degraded_notice_receipt", row["content"], project_id=2, message_id=2,
        meta=json.loads(row["meta"]))
    _held(db, cfg, event, payload, "degraded_unverified")


def test_current_policy_change_remains_stale(world):
    db, cfg, event, payload = world
    changed = _cfg("enforce", calibration_version="synthetic-changed")
    assert semantic_policy.policy_fingerprint(semantic_policy.semantic_config(changed)[0]) != payload["policy_fingerprint"]
    with pytest.raises(gate.StaleSend, match="policy_changed"):
        gate.semantic_gate(db.db, event, payload, changed)
    with pytest.raises(gate.FreezeSend, match="policy_changed"):
        gate.semantic_gate(db.db, event, payload, changed, in_progress=True)
    with pytest.raises(gate.StaleSend, match="policy_changed"):
        gate.marker_source_gate(db.db, 1, payload, cfg=changed)
    with pytest.raises(gate.FreezeSend, match="policy_changed"):
        gate.marker_source_gate(db.db, 1, payload, cfg=changed, in_progress=True)
    with pytest.raises(gate.DeferredSend, match="policy_unverified"):
        gate.marker_source_gate(db.db, 1, payload)


def test_forged_pass_with_empty_claims_cannot_be_published(tmp_path):
    import semantic_store
    from semantic_testkit import _summary

    db = _delivery_db(tmp_path, [_message()])
    try:
        _summary(db, claims=0)
        bundle = semantic_store.thread_bundle(db.db, 1, 1)
        fp, member = bundle["source_fingerprint"], bundle["members"][0]
        policy = semantic_policy.policy_fingerprint(semantic_policy.semantic_config(_cfg("enforce"))[0])
        summary = gate._latest_receipt(db.db, semantic_policy.KIND_SUMMARY, 1, 1)
        audit = gate._latest_receipt(db.db, semantic_policy.KIND_AUDIT, 1, 1)
        assert gate._claim_binding_current(db.db, 1, 1, fp, policy, member["revision"],
                                           summary, audit, member, "legacy")
        assert gate.current_summary(db.db, 1, 1, bundle, policy, fact_source="legacy") is None
    finally:
        db.close()


def test_receipt_and_outbox_rollback_together(tmp_path, monkeypatch):
    db = _delivery_db(tmp_path, [_message()])
    try:
        with db.db:
            db.db.execute("UPDATE notify_outbox SET created_at=0 WHERE kind='new_messages'")
        def fail(*args, **kwargs):
            raise RuntimeError("synthetic outbox failure")
        monkeypatch.setattr(db, "outbox_add_tx", fail)
        with pytest.raises(RuntimeError, match="synthetic outbox failure"):
            semantic_notice.emit_degraded(db, semantic_policy.semantic_config(_cfg("enforce"))[0])
        assert db.db.execute("SELECT count(*) FROM artifacts WHERE kind='semantic_degraded_notice_receipt'").fetchone()[0] == 0
        assert db.db.execute("SELECT count(*) FROM notify_outbox WHERE kind='semantic_notice'").fetchone()[0] == 0
    finally:
        db.close()


@pytest.mark.parametrize('progress', ['not-json', '{"next":0,"sent":[],"sent":[]}', '{}', None])
def test_attempted_origin_with_unverified_progress_is_held(world, progress):
    db, cfg, event, payload = world
    with db.db:
        db.db.execute("UPDATE notify_outbox SET state='failed',attempts=1,progress=? WHERE event_id=?",
                      (progress, payload['src_event_id']))
    _held(db, cfg, event, payload, 'src_event_unverified')


def test_proven_presend_failure_does_not_suppress_degraded_notice(world):
    db, cfg, event, payload = world
    progress = {'next': 0, 'sent': [], 'sending': None, 'fingerprint': '0' * 64}
    with db.db:
        db.db.execute("UPDATE notify_outbox SET state='failed',attempts=1,progress=? WHERE event_id=?",
                      (json.dumps(progress), payload['src_event_id']))
    gate.semantic_gate(db.db, event, payload, cfg)
    gate.marker_source_gate(db.db, 1, payload, cfg=cfg)


def test_proven_presend_failure_can_create_degraded_notice(tmp_path):
    db = _delivery_db(tmp_path, [_message()])
    try:
        progress = {'next': 0, 'sent': [], 'sending': None, 'fingerprint': '0' * 64}
        with db.db:
            db.db.execute("UPDATE notify_outbox SET created_at=0,state='failed',attempts=1,progress=? "
                          "WHERE kind='new_messages'", (json.dumps(progress),))
        assert semantic_notice.emit_degraded(db, semantic_policy.semantic_config(_cfg('enforce'))[0]) == 1
    finally:
        db.close()


def test_summary_becoming_ready_before_send_or_marker_suppresses_or_freezes(world):
    import semantic_store
    from semantic_testkit import _summary

    db, cfg, event, payload = world
    gate.semantic_gate(db.db, event, payload, cfg)
    _summary(db)
    bundle = semantic_store.thread_bundle(db.db, 1, payload['root_id'])
    assert gate.current_summary(db.db, 1, 1, bundle, payload['policy_fingerprint'],
                                fact_source=semantic_policy.semantic_config(cfg)[0]['fact_source']) is not None
    for check in (lambda progress: gate.semantic_gate(db.db, event, payload, cfg, in_progress=progress),
                  lambda progress: gate.marker_source_gate(db.db, 1, payload, cfg=cfg, in_progress=progress)):
        with pytest.raises(gate.StaleSend, match='summary_ready'):
            check(False)
        with pytest.raises(gate.FreezeSend, match='summary_ready'):
            check(True)


def test_summary_is_rechecked_inside_creation_transaction(tmp_path, monkeypatch):
    db = _delivery_db(tmp_path, [_message()])
    try:
        with db.db:
            db.db.execute("UPDATE notify_outbox SET created_at=0 WHERE kind='new_messages'")
        checks = []

        def summary(_db, _pid, _mid, _bundle, _policy, *, fact_source=None):
            checks.append(db.db.in_transaction)
            return {'artifact_id': 1} if db.db.in_transaction else None

        monkeypatch.setattr(gate, 'current_summary', summary)
        assert semantic_notice.emit_degraded(db, semantic_policy.semantic_config(_cfg('enforce'))[0]) == 0
        assert checks == [False, True]
        assert db.db.execute("SELECT count(*) FROM artifacts WHERE kind='semantic_degraded_notice_receipt'").fetchone()[0] == 0
    finally:
        db.close()


def test_degraded_summaries_require_every_target(monkeypatch):
    cfg = semantic_policy.semantic_config(_cfg('enforce'))[0]
    present = {1}
    monkeypatch.setattr(gate, 'current_summary', lambda _db, _pid, mid, _bundle, _policy, **_kw:
                        {'artifact_id': mid} if mid in present else None)
    assert not gate._degraded_summaries_ready(None, 1, {}, [1, 2], cfg)
    present.add(2)
    assert gate._degraded_summaries_ready(None, 1, {}, [1, 2], cfg)
