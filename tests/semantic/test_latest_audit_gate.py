"""Synthetic latest-receipt and frozen-notice publication boundaries."""
import json

import pytest

import mcs_util
import semantic_policy
import semantic_send_gate as gate
import semantic_store
from inference_version import CURRENT_VERSION, VERSION_KEY
from semantic_testkit import _cfg, _delivery_db, _message, _patient, _semantic_event


@pytest.fixture
def world(tmp_path):
    db = _delivery_db(tmp_path, [_message()])
    try:
        event = _semantic_event(db)
        payload = json.loads(event["payload"])
        bundle = semantic_store.thread_bundle(db.db, 1, 1)
        fp, revision, policy = bundle["source_fingerprint"], bundle["members"][0]["revision"], payload["policy_fingerprint"]
        facts = [{"fact_id": "synthetic-fact", "statement": "合成事実"}]
        meta = {VERSION_KEY: CURRENT_VERSION, "schema": semantic_policy.SCHEMA_VERSION,
                "fingerprint": fp, "policy_fingerprint": policy, "target_revision": revision,
                "publication_mode": "enforce", "audit_status": "PASS"}
        facts_id = db.artifact_add(semantic_policy.KIND_FACTS, json.dumps({"facts": facts}),
                                   project_id=1, message_id=1, meta=meta)
        summary = {"audit_status": "PASS", "claims": [{"text": "合成の要約"}], "limitations": []}
        summary_id = db.artifact_add(semantic_policy.KIND_SUMMARY, json.dumps(summary),
                                     project_id=1, message_id=1, meta=meta)
        audit = {"status": "PASS", "findings": [], "target_message_id": 1}
        binding = mcs_util.payload_hash({"project_id": 1, "message_id": 1, "fingerprint": fp,
                                        "policy": policy, "revision": revision,
                                        "claims": summary["claims"], "facts": facts})
        audit_meta = {**meta, "summary_artifact_id": summary_id,
                      "summary_hash": mcs_util.payload_hash(summary),
                      "audit_content_hash": mcs_util.payload_hash(audit), "claim_input_binding": binding}
        audit_id = db.artifact_add(semantic_policy.KIND_AUDIT, json.dumps(audit),
                                   project_id=1, message_id=1, meta=audit_meta)
        payload.update(summary_artifact_id=summary_id, summary_hash=mcs_util.payload_hash(summary),
                       notice_text_hash=mcs_util.payload_hash(payload["text"]))
        payload["delivery_key"] = mcs_util.payload_hash({"kind": "semantic_notice", "root": payload["root_id"],
            "fp": fp, "mid": 1, "policy": policy})
        fields = ("root_id", "target_message_id", "src_event_id", "fingerprint", "policy_fingerprint",
                  "summary_artifact_id", "summary_hash", "notice_text_hash", "delivery_key")
        receipt_id = db.artifact_add("semantic_notice_receipt", json.dumps({key: payload[key] for key in fields}),
                                    project_id=1, message_id=1,
                                    meta={VERSION_KEY: CURRENT_VERSION, "fingerprint": fp,
                                          "policy_fingerprint": policy})
        payload["notice_receipt_id"] = receipt_id
        yield db, event, payload, bundle, {"summary": summary_id, "audit": audit_id, "facts": facts_id,
                                           "notice": receipt_id}
    finally:
        db.close()


def _check_hold(world):
    db, event, payload, bundle, _ = world
    before = tuple(db.db.execute("SELECT state,attempts,progress FROM notify_outbox WHERE event_id=?",
                                 (event["event_id"],)).fetchone())
    assert gate.current_summary(db.db, 1, 1, bundle, payload["policy_fingerprint"]) is None
    with pytest.raises(gate.DeferredSend, match="summary_unverified"):
        gate.semantic_gate(db.db, event, payload, _cfg("enforce"))
    with pytest.raises(gate.FreezeSend, match="summary_unverified"):
        gate.semantic_gate(db.db, event, payload, _cfg("enforce"), in_progress=True)
    assert tuple(db.db.execute("SELECT state,attempts,progress FROM notify_outbox WHERE event_id=?",
                               (event["event_id"],)).fetchone()) == before


def test_valid_latest_pair_is_visible_and_can_send(world):
    db, event, payload, bundle, ids = world
    summary = gate.current_summary(db.db, 1, 1, bundle, payload["policy_fingerprint"])
    assert summary is not None and summary["artifact_id"] == ids["summary"]
    gate.semantic_gate(db.db, event, payload, _cfg("enforce"))


def test_shared_pass_check_is_mode_bound_and_can_reuse_bundle(world):
    db, _, payload, bundle, ids = world
    revision = payload["target_revision"]
    args = (db.db, 1, 1, payload["fingerprint"], payload["policy_fingerprint"], revision)
    assert gate.passed_summary_current(*args, "enforce", bundle=bundle, fact_source="legacy")
    assert gate.passed_summary_current(*args, "enforce")
    assert not gate.passed_summary_current(*args, "shadow", bundle=bundle)
    with db.db:
        for artifact_id in (ids["summary"], ids["audit"]):
            db.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.publication_mode','shadow') WHERE artifact_id=?",
                          (artifact_id,))
    assert gate.passed_summary_current(*args, "shadow", bundle=bundle, fact_source="legacy")
    assert not gate.passed_summary_current(*args, "enforce", bundle=bundle)


def test_canonical_fact_document_reconstructs_the_audited_input(tmp_path):
    from semantic_testkit import _canonical_cfg, _drain, _PassJev, _seeded_two

    db = _seeded_two(tmp_path)
    try:
        assert _drain(db, jev=_PassJev())["done"] == 1
        bundle = semantic_store.thread_bundle(db.db, 1, 1)
        scfg = semantic_policy.semantic_config(_canonical_cfg())[0]
        policy = semantic_policy.policy_fingerprint(scfg)
        for mid in (1, 2):
            assert gate.current_summary(db.db, 1, mid, bundle, policy, fact_source="canonical") is not None
            assert gate.current_summary(db.db, 1, mid, bundle, policy) is not None
        row = db.artifacts(semantic_policy.KIND_FACTS_V2, message_id=1)[-1]
        with db.db:
            db.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.invalidated',1) WHERE artifact_id=?",
                          (row["artifact_id"],))
        assert gate.current_summary(db.db, 1, 1, bundle, policy, fact_source="canonical") is None
    finally:
        db.close()


@pytest.mark.parametrize("status", ["PASS", "SKIPPED"])
@pytest.mark.parametrize("tamper", ["audit_invalidated", "audit_negative", "audit_other_doc", "projection_invalidated"])
def test_canonical_latest_fact_audit_and_projection_gate_notice(tmp_path, monkeypatch, status, tamper):
    import time
    import semantic_drain
    import semantic_jev
    import semantic_v4
    from semantic_testkit import _canonical_cfg, _drain, _llm_v2, _PassJev, _seeded_two

    db = _seeded_two(tmp_path)
    try:
        cfg = _canonical_cfg()
        if status == "SKIPPED":
            cfg["semantic"]["daily_request_budget"] = 0
            monkeypatch.setattr(semantic_jev, "api_key", lambda *args, **kwargs: None)
            result = semantic_drain.run_due(db, cfg, {"errors": []}, time.monotonic() + 300, llm_fn=_llm_v2)
        else:
            result = _drain(db, jev=_PassJev())
        assert result["done"] == 1
        bundle = semantic_store.thread_bundle(db.db, 1, 1, [1])
        scfg = semantic_policy.semantic_config(cfg)[0]
        policy = semantic_policy.policy_fingerprint(scfg)
        assert gate.current_summary(db.db, 1, 1, bundle, policy, fact_source="canonical") is not None
        event = db.db.execute("SELECT * FROM notify_outbox WHERE kind='semantic_notice' "
                              "AND json_extract(payload,'$.target_message_id')=1 ORDER BY event_id DESC LIMIT 1").fetchone()
        assert event is not None
        payload = json.loads(event["payload"])
        gate.semantic_gate(db.db, event, payload, cfg)
        kind = (semantic_v4.KIND_V4_SKIPPED if status == "SKIPPED" else semantic_v4.KIND_V4) \
            if tamper == "projection_invalidated" else semantic_policy.KIND_FACT_AUDIT
        row = db.artifacts(kind, message_id=1)[-1]
        meta, content = json.loads(row["meta"]), json.loads(row["content"])
        if tamper in ("projection_invalidated", "audit_invalidated"):
            meta["invalidated"] = True
        elif tamper == "audit_negative":
            content.update(status="NEEDS_REVIEW", evaluated=True, findings=[{"code": "fact_contradicts"}])
            meta["audit_status"] = "NEEDS_REVIEW"
        else:
            meta["doc_hash"] = "0" * 64
        with db.db:
            db.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                          "SELECT kind,project_id,message_id,?,model,?,created_at FROM artifacts WHERE artifact_id=?",
                          (json.dumps(content), json.dumps(meta), row["artifact_id"]))
        assert gate.current_summary(db.db, 1, 1, bundle, policy, fact_source="canonical") is None
        with pytest.raises(gate.DeferredSend, match="summary_unverified"):
            gate.semantic_gate(db.db, event, payload, cfg)
        with pytest.raises(gate.FreezeSend, match="summary_unverified"):
            gate.semantic_gate(db.db, event, payload, cfg, in_progress=True)
    finally:
        db.close()


@pytest.mark.parametrize("tamper", ["missing", "version", "schema", "source", "policy", "revision",
                                   "mode", "invalidated", "stale", "summary_id", "summary_hash",
                                   "audit_hash", "binding", "target", "status", "negative",
                                   "duplicate_meta", "duplicate_content", "nonfinite"])
def test_unverifiable_latest_audit_holds_without_reviving_history(world, tamper):
    db, _, _, _, ids = world
    row = db.db.execute("SELECT content,meta FROM artifacts WHERE artifact_id=?", (ids["audit"],)).fetchone()
    content, meta = json.loads(row["content"]), json.loads(row["meta"])
    if tamper == "missing":
        meta.pop("summary_artifact_id")
    elif tamper in ("version", "schema", "source", "policy", "revision", "mode", "invalidated", "stale",
                    "summary_id", "summary_hash", "audit_hash", "binding"):
        key = {"version": VERSION_KEY, "schema": "schema", "source": "fingerprint", "policy": "policy_fingerprint",
               "revision": "target_revision", "mode": "publication_mode", "invalidated": "invalidated",
               "stale": "stale", "summary_id": "summary_artifact_id", "summary_hash": "summary_hash",
               "audit_hash": "audit_content_hash", "binding": "claim_input_binding"}[tamper]
        meta[key] = True if tamper in ("invalidated", "stale", "summary_id") else "changed"
    elif tamper == "target":
        content["target_message_id"] = 2
    elif tamper == "status":
        content["status"] = "PENDING"
    elif tamper == "negative":
        content["findings"] = [{"code": "claim_contradicts"}]
        meta["audit_content_hash"] = mcs_util.payload_hash(content)
    raw_content, raw_meta = json.dumps(content), json.dumps(meta)
    if tamper == "duplicate_meta":
        raw_meta = raw_meta[:-1] + ', "audit_status":"PASS"}'
    elif tamper == "duplicate_content":
        raw_content = raw_content[:-1] + ', "status":"PASS"}'
    elif tamper == "nonfinite":
        raw_meta = raw_meta[:-1] + ', "unused": 1e999}'
    with db.db:
        db.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                      "SELECT kind,project_id,message_id,?,model,?,created_at FROM artifacts WHERE artifact_id=?",
                      (raw_content, raw_meta, ids["audit"]))
    _check_hold(world)


@pytest.mark.parametrize("tamper", ["missing", "duplicate", "invalidated", "new_summary", "facts", "other_project"])
def test_summary_and_fact_binding_fail_closed(world, tamper):
    db, _, _, _, ids = world
    if tamper == "other_project":
        _patient(db, 2)
        db.save_messages([_message(mid=2, pid=2, unread=False)], project_id=2)
    with db.db:
        if tamper == "missing":
            db.db.execute("DELETE FROM artifacts WHERE kind=? AND project_id=1 AND message_id=1",
                          (semantic_policy.KIND_AUDIT,))
        elif tamper == "duplicate":
            db.db.execute("UPDATE artifacts SET content=substr(content,1,length(content)-1)||? WHERE artifact_id=?",
                          (', "audit_status":"PASS"}', ids["summary"]))
        elif tamper == "invalidated":
            db.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.invalidated',1) WHERE artifact_id=?",
                          (ids["summary"],))
        elif tamper == "new_summary":
            db.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                          "SELECT kind,project_id,message_id,content,model,meta,created_at FROM artifacts WHERE artifact_id=?",
                          (ids["summary"],))
        elif tamper == "facts":
            db.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?",
                          (json.dumps({"facts": [{"statement": "変更済み"}]}), ids["facts"]))
        else:
            db.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                          "SELECT kind,2,2,content,model,meta,created_at FROM artifacts WHERE artifact_id=?",
                          (ids["facts"],))
            db.db.execute("DELETE FROM artifacts WHERE kind=? AND project_id=1 AND message_id=1",
                          (semantic_policy.KIND_FACTS,))
    _check_hold(world)


def test_legacy_missing_pair_is_hidden_from_raw_notice(world, monkeypatch):
    import notify_flush

    db, _, _, _, ids = world
    with db.db:
        db.db.execute("UPDATE artifacts SET meta=json_remove(meta,'$.claim_input_binding') WHERE artifact_id=?",
                      (ids["audit"],))
    raw = db.db.execute("SELECT * FROM notify_outbox WHERE kind='new_messages' ORDER BY event_id DESC LIMIT 1").fetchone()
    monkeypatch.setattr(notify_flush, "_config", lambda: _cfg("enforce"))
    assert gate.semantic_render_state(db.db, raw, _cfg("enforce")) == ()
    assert "要約（自動検査済）" not in notify_flush._format_event(db, raw)[0]
    _check_hold(world)


def test_unverified_legacy_notice_is_parked_without_suppression_or_send(world, monkeypatch):
    import notify_flush
    import notify_sender

    db, event, payload, _, ids = world
    with db.db:
        db.db.execute("UPDATE notify_outbox SET payload=?,next_try=0 WHERE event_id=?",
                      (json.dumps(payload), event["event_id"]))
        db.db.execute("UPDATE artifacts SET meta=json_remove(meta,'$.claim_input_binding') WHERE artifact_id=?",
                      (ids["audit"],))
    monkeypatch.setattr(notify_flush, "_config", lambda: _cfg("enforce"))
    monkeypatch.setattr(mcs_util, "hermes_exe", lambda *a: "/bin/sh")
    monkeypatch.setattr(notify_sender, "target", lambda *a: "channel")
    monkeypatch.setattr(notify_sender, "send", lambda *a, **kw: pytest.fail("unverified notice was sent"))
    result = notify_flush.flush(db)
    saved = db.db.execute("SELECT state,progress FROM notify_outbox WHERE event_id=?", (event["event_id"],)).fetchone()
    assert result["parked"] == 1 and saved["state"] == "pending"
    assert not json.loads(saved["progress"] or "{}").get("sent")


@pytest.mark.parametrize("field,value", [("summary_artifact_id", None), ("summary_artifact_id", True),
                                          ("summary_artifact_id", 999999), ("summary_hash", None),
                                          ("summary_hash", "0" * 64), ("notice_text_hash", None),
                                          ("notice_text_hash", "0" * 64), ("text", "変更済み"),
                                          ("text", None), ("text", "\ud800")])
def test_frozen_notice_must_name_the_verified_pair(world, field, value):
    db, event, payload, _, _ = world
    payload = {**payload, field: value}
    with pytest.raises(gate.DeferredSend, match="summary_unverified"):
        gate.semantic_gate(db.db, event, payload, _cfg("enforce"))
    with pytest.raises(gate.FreezeSend, match="summary_unverified"):
        gate.semantic_gate(db.db, event, payload, _cfg("enforce"), in_progress=True)


def test_text_and_hash_changed_together_do_not_match_sealed_receipt(world):
    db, event, payload, _, _ = world
    changed = {**payload, "text": "書換えた合成通知", "notice_text_hash": mcs_util.payload_hash("書換えた合成通知")}
    with pytest.raises(gate.DeferredSend, match="summary_unverified"):
        gate.semantic_gate(db.db, event, changed, _cfg("enforce"))
    with pytest.raises(gate.FreezeSend, match="summary_unverified"):
        gate.semantic_gate(db.db, event, changed, _cfg("enforce"), in_progress=True)


@pytest.mark.parametrize("tamper", ["missing", "kind", "project", "target", "version", "fingerprint",
                                   "policy", "invalidated", "content", "duplicate_content", "duplicate_meta"])
def test_notice_receipt_is_checked_by_its_exact_id(world, tamper):
    db, event, payload, _, ids = world
    row = db.db.execute("SELECT content,meta FROM artifacts WHERE artifact_id=?", (ids["notice"],)).fetchone()
    content, meta = json.loads(row["content"]), json.loads(row["meta"])
    if tamper == "missing":
        payload = {**payload, "notice_receipt_id": None}
    elif tamper == "kind":
        with db.db:
            db.db.execute("UPDATE artifacts SET kind=? WHERE artifact_id=?", ("other", ids["notice"]))
    elif tamper in ("project", "target"):
        project_id = 2 if tamper == "project" else 1
        if project_id == 2:
            _patient(db, 2)
        db.save_messages([_message(mid=2, pid=project_id, unread=False)], project_id=project_id)
        receipt_id = db.artifact_add("semantic_notice_receipt", json.dumps(content),
                                     project_id=project_id, message_id=2, meta=meta)
        payload = {**payload, "notice_receipt_id": receipt_id}
    else:
        if tamper in ("version", "fingerprint", "policy", "invalidated"):
            key = {"version": VERSION_KEY, "fingerprint": "fingerprint", "policy": "policy_fingerprint",
                   "invalidated": "invalidated"}[tamper]
            meta[key] = True if tamper == "invalidated" else "changed"
        elif tamper == "content":
            content["notice_text_hash"] = "0" * 64
        raw_content, raw_meta = json.dumps(content), json.dumps(meta)
        if tamper == "duplicate_content":
            raw_content = raw_content[:-1] + ', "root_id":1}'
        elif tamper == "duplicate_meta":
            raw_meta = raw_meta[:-1] + ', "fingerprint": "duplicate"}'
        with db.db:
            db.db.execute("UPDATE artifacts SET content=?,meta=? WHERE artifact_id=?",
                          (raw_content, raw_meta, ids["notice"]))
    with pytest.raises(gate.DeferredSend, match="summary_unverified"):
        gate.semantic_gate(db.db, event, payload, _cfg("enforce"))
    with pytest.raises(gate.FreezeSend, match="summary_unverified"):
        gate.semantic_gate(db.db, event, payload, _cfg("enforce"), in_progress=True)


def test_another_same_target_notice_receipt_does_not_hide_the_bound_receipt(world):
    db, event, payload, _, _ = world
    db.artifact_add("semantic_notice_receipt", "{}", project_id=1, message_id=1, meta={})
    gate.semantic_gate(db.db, event, payload, _cfg("enforce"))


@pytest.mark.parametrize("raw", ["not-json", "null", "[]", '"text"', '{}',
                                 '{"message_ids":"1"}', '{"message_ids":[true]}',
                                 '{"message_ids":[1,1]}', '{"message_ids":[1],"message_ids":[1]}'])
def test_unreadable_source_event_is_deferred_without_suppression(world, raw):
    db, event, payload, _, _ = world
    with db.db:
        db.db.execute("UPDATE notify_outbox SET payload=? WHERE event_id=?", (raw, payload["src_event_id"]))
    with pytest.raises(gate.DeferredSend, match="src_event_unverified"):
        gate.semantic_gate(db.db, event, payload, _cfg("enforce"))
    with pytest.raises(gate.FreezeSend, match="src_event_unverified"):
        gate.semantic_gate(db.db, event, payload, _cfg("enforce"), in_progress=True)


def test_missing_and_suppressed_source_remain_stale(world):
    db, event, payload, _, _ = world
    with pytest.raises(gate.StaleSend, match="src_event_ineligible"):
        gate.semantic_gate(db.db, event, {**payload, "src_event_id": 999999}, _cfg("enforce"))
    with db.db:
        db.db.execute("UPDATE notify_outbox SET state='suppressed',payload='not-json' WHERE event_id=?",
                      (payload["src_event_id"],))
    with pytest.raises(gate.StaleSend, match="src_event_ineligible"):
        gate.semantic_gate(db.db, event, payload, _cfg("enforce"))


def test_definite_source_and_policy_change_remain_stale(world):
    db, event, payload, _, _ = world
    with pytest.raises(gate.StaleSend, match="stale_generation"):
        gate.semantic_gate(db.db, event, {**payload, "fingerprint": "outdated"}, _cfg("enforce"))
    with pytest.raises(gate.StaleSend, match="policy_changed"):
        gate.semantic_gate(db.db, event, {**payload, "policy_fingerprint": "outdated"}, _cfg("enforce"))
