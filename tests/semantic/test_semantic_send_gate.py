"""Semantic outbox gates bind source, target, policy, and publication stage."""
import semantic_render
import semantic_send_gate
import semantic_policy
import semantic_store
import json

import pytest

import notify_flush
from semantic_testkit import (_cfg, _delivery_db, _delivery_patient, _message,
                              _semantic_event)


def test_normal_gate_requires_source_target_revision_and_pass_summary(
        tmp_path, monkeypatch):
    db = _delivery_db(tmp_path, [_message()])
    monkeypatch.setattr(notify_flush, "_config", lambda: _cfg("enforce"))
    event = _semantic_event(db)
    payload = json.loads(event["payload"])

    semantic_send_gate.semantic_gate(db.db, event, payload, notify_flush._config())

    missing_target = dict(payload)
    missing_target.pop("target_message_id")
    with pytest.raises(ValueError, match="payload_invalid"):
        semantic_send_gate.semantic_gate(db.db, event, missing_target, notify_flush._config())

    wrong_revision = dict(payload, target_revision="old-revision")
    with pytest.raises(semantic_send_gate.StaleSend, match="stale_generation"):
        semantic_send_gate.semantic_gate(db.db, event, wrong_revision, notify_flush._config())

    other_source = db.outbox_add("new_messages", 2, {"message_ids": [1]})
    wrong_project = dict(payload, src_event_id=other_source)
    with pytest.raises(semantic_send_gate.StaleSend, match="src_event_ineligible"):
        semantic_send_gate.semantic_gate(db.db, event, wrong_project, notify_flush._config())

    summary = db.db.execute(
        "SELECT artifact_id,meta FROM artifacts "
        "WHERE kind='semantic_summary' AND message_id=1 "
        "ORDER BY artifact_id DESC LIMIT 1").fetchone()
    meta = json.loads(summary["meta"])
    meta["publication_mode"] = "shadow"
    db.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?",
                  (json.dumps(meta), summary["artifact_id"]))
    db.db.commit()
    with pytest.raises(semantic_send_gate.DeferredSend, match="summary_unverified"):
        semantic_send_gate.semantic_gate(db.db, event, payload, notify_flush._config())
    db.close()


def _raw_notice_with_claims(db, claims):
    from mcs_util import payload_hash

    _semantic_event(db)
    row = db.db.execute("SELECT artifact_id,content FROM artifacts "
                        "WHERE kind='semantic_summary'").fetchone()
    summary = json.loads(row["content"])
    facts = [f"合成必須事実{i} [fact_{i}; ev_{i}]" for i in range(41)]
    summary.update(claims=claims, mandatory_facts=facts)
    with db.db:
        db.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?",
                      (json.dumps(summary, ensure_ascii=False), row["artifact_id"]))
        audit = db.db.execute("SELECT artifact_id,meta FROM artifacts WHERE kind='semantic_audit' "
                              "ORDER BY artifact_id DESC LIMIT 1").fetchone()
        meta = json.loads(audit["meta"])
        binding_facts = json.loads(db.artifacts("semantic_facts", message_id=1)[-1]["content"])["facts"]
        meta["summary_hash"] = payload_hash(summary)
        meta["claim_input_binding"] = payload_hash({
            "project_id": 1, "message_id": 1, "fingerprint": meta["fingerprint"],
            "policy": meta["policy_fingerprint"], "revision": meta["target_revision"],
            "claims": summary["claims"], "facts": binding_facts})
        db.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?", (json.dumps(meta), audit["artifact_id"]))
    return facts, db.db.execute("SELECT * FROM notify_outbox WHERE kind='new_messages'").fetchone()


def test_raw_notice_keeps_all_verified_mandatory_facts(tmp_path, monkeypatch):
    db = _delivery_db(tmp_path, [_message()])
    try:
        monkeypatch.setattr(notify_flush, "_config", lambda: _cfg("enforce"))
        claims = [{"text": "合成の補助要約"}]
        facts, raw = _raw_notice_with_claims(db, claims)
        text = notify_flush._format_event(db, raw)[0]
        assert all("・" + fact in text for fact in facts)
        assert all(claim["text"] in text for claim in claims)
    finally:
        db.close()


def test_raw_notice_hides_forged_pass_with_empty_claims(tmp_path, monkeypatch):
    db = _delivery_db(tmp_path, [_message()])
    try:
        cfg = _cfg("enforce")
        monkeypatch.setattr(notify_flush, "_config", lambda: cfg)
        facts, raw = _raw_notice_with_claims(db, [])
        assert semantic_send_gate.semantic_render_state(db.db, raw, cfg) == ()
        text = notify_flush._format_event(db, raw)[0]
        assert "要約（自動検査済）" not in text
        assert all(fact not in text for fact in facts)
    finally:
        db.close()


def _produced_degraded(db, cfg, target_ids):
    import semantic_notice

    source_id = db.outbox_add("new_messages", 1, {"message_ids": target_ids})
    with db.db:
        db.db.execute("UPDATE notify_outbox SET created_at=0 WHERE event_id=?", (source_id,))
    assert semantic_notice.emit_degraded(db, semantic_policy.semantic_config(cfg)[0]) == 1
    event = db.db.execute("SELECT * FROM notify_outbox WHERE kind='semantic_notice' "
                          "ORDER BY event_id DESC LIMIT 1").fetchone()
    payload = json.loads(event["payload"])
    assert payload["src_event_id"] == source_id
    return event, payload, source_id


def test_chunker_preserves_embedded_footer_text_and_uses_terminal_source_link():
    body = "■経過\n・合成事実一\n▶ MCSで確認\nhttps://untrusted.invalid/body\n・合成必須事実二"
    text = "【合成】患者A\n取得：全件\n要約：PASS\n\n" + body + \
        "\n\n▶ MCSで確認\nhttps://example.invalid/p/1\n"
    parts, bodies = semantic_render.semantic_chunk_parts(text, 150)
    assert "".join(bodies) == body
    assert all(part.endswith("▶ MCSで確認\nhttps://example.invalid/p/1") for part in parts)


def test_chunker_rejects_unexpected_text_after_source_footer():
    text = "【合成】患者A\n取得：全件\n要約：PASS\n\n■経過\n・合成事実\n" \
        "▶ MCSで確認\nhttps://example.invalid/p/1\n合成の未配送末尾"
    with pytest.raises(ValueError, match="semantic_provenance_missing"):
        semantic_render.semantic_chunk_parts(text, 200)


def test_degraded_gate_requires_frozen_generation_and_unattempted_source(
        tmp_path, monkeypatch):
    db = _delivery_db(tmp_path, [_message()])
    monkeypatch.setattr(notify_flush, "_config", lambda: _cfg("enforce"))
    event, payload, source_id = _produced_degraded(db, _cfg("enforce"), [1])

    semantic_send_gate.semantic_gate(db.db, event, payload, notify_flush._config())

    missing_targets = dict(payload)
    missing_targets.pop("target_message_ids")
    with pytest.raises(semantic_send_gate.DeferredSend, match="degraded_unverified"):
        semantic_send_gate.semantic_gate(db.db, event, missing_targets, notify_flush._config())

    legacy = dict(payload)
    legacy.pop("degraded_receipt_id")
    with pytest.raises(semantic_send_gate.DeferredSend, match="degraded_unverified"):
        semantic_send_gate.semantic_gate(db.db, event, legacy, notify_flush._config())

    changed_policy = _cfg("enforce", calibration_version="synthetic-changed")
    with pytest.raises(semantic_send_gate.StaleSend, match="policy_changed"):
        semantic_send_gate.semantic_gate(db.db, event, payload, changed_policy)
    with pytest.raises(semantic_send_gate.FreezeSend, match="policy_changed"):
        semantic_send_gate.semantic_gate(db.db, event, payload, changed_policy, in_progress=True)

    db.db.execute(
        "UPDATE notify_outbox SET state='failed',attempts=1,progress=? "
        "WHERE event_id=?", (json.dumps({}), source_id))
    db.db.commit()
    with pytest.raises(semantic_send_gate.DeferredSend, match="src_event_unverified"):
        semantic_send_gate.semantic_gate(db.db, event, payload, notify_flush._config())

    db.db.execute(
        "UPDATE notify_outbox SET state='pending',attempts=0,progress=? "
        "WHERE event_id=?", (json.dumps({}), source_id))
    db.db.commit()
    db.save_patient(_delivery_patient([_message(body="訂正された本文", unread=False)]),
                    notify=None)
    with pytest.raises(semantic_send_gate.StaleSend, match="stale_generation"):
        semantic_send_gate.semantic_gate(db.db, event, payload, notify_flush._config())
    db.close()


def test_raw_notice_ignores_shadow_publication(tmp_path, monkeypatch):
    db = _delivery_db(tmp_path, [_message()])
    monkeypatch.setattr(notify_flush, "_config", lambda: _cfg("enforce"))
    event = db.db.execute(
        "SELECT * FROM notify_outbox WHERE kind='new_messages'"
    ).fetchone()
    bundle = semantic_store.thread_bundle(db.db, 1, 1)
    policy = semantic_policy.policy_fingerprint(
        semantic_policy.semantic_config(_cfg("enforce"))[0])
    content = {"claims": [{"text": "shadow claim"}], "limitations": []}
    meta = {"fingerprint": bundle["source_fingerprint"],
            "policy_fingerprint": policy, "audit_status": "PASS",
            "publication_mode": "shadow",
            "target_revision": bundle["members"][0]["revision"]}
    db.artifact_add("semantic_summary", json.dumps(content), project_id=1,
                    message_id=1, model="Qwen3.5-9B", meta=meta)
    assert semantic_send_gate.semantic_render_state(db.db, event, notify_flush._config()) == ()
    assert "要約（自動検査済）" not in notify_flush._format_event(db, event)[0]

    meta["publication_mode"] = "enforce"
    db.artifact_add("semantic_summary", json.dumps(content), project_id=1,
                    message_id=1, model="Qwen3.5-9B", meta=meta)
    assert semantic_send_gate.semantic_render_state(db.db, event, notify_flush._config()) == ()
    assert "要約（自動検査済）" not in notify_flush._format_event(db, event)[0]
    from semantic_testkit import _summary
    _summary(db)
    assert semantic_send_gate.semantic_render_state(db.db, event, notify_flush._config())
    assert "要約（自動検査済）" in notify_flush._format_event(db, event)[0]
    db.close()


def test_semantic_chunks_output_and_part_bodies_are_pinned():
    """Byte-pinned send output (captured before the chunk-parts split);
    the bodies are exactly the per-part slices that rebuild the body."""
    text = "\n".join(["【合成】患者A", "取得：全件", "要約：PASS", "",
                      "■経過", "・合成事実一", "・合成事実二", "",
                      "▶ MCSで確認", "https://example.invalid/p/1"])
    head = "【合成】患者A\n取得：全件\n要約：PASS\n"
    foot = "\n▶ MCSで確認\nhttps://example.invalid/p/1"
    expected = [head + "part 1/2\n■経過\n・合成事実一\n・合成事実" + foot,
                head + "part 2/2\n二" + foot]
    assert semantic_render.semantic_chunks(text, 85) == expected
    parts, bodies = semantic_render.semantic_chunk_parts(text, 85)
    assert parts == expected
    assert bodies == ["■経過\n・合成事実一\n・合成事実", "二"]
    assert "".join(bodies) == "■経過\n・合成事実一\n・合成事実二"


def test_stale_summary_rejected_by_gate_render_state_and_block(
        tmp_path, monkeypatch):
    """One shared predicate: a stale-flagged summary is unpublishable for
    the send gate, the render snapshot, and the rendered body alike."""
    db = _delivery_db(tmp_path, [_message()])
    monkeypatch.setattr(notify_flush, "_config", lambda: _cfg("enforce"))
    event = _semantic_event(db)
    payload = json.loads(event["payload"])
    raw = db.db.execute(
        "SELECT * FROM notify_outbox WHERE kind='new_messages'").fetchone()
    semantic_send_gate.semantic_gate(db.db, event, payload, notify_flush._config())
    assert semantic_send_gate.semantic_render_state(db.db, raw, notify_flush._config())
    assert "要約（自動検査済）" in notify_flush._format_event(db, raw)[0]

    summary = db.db.execute(
        "SELECT artifact_id,meta FROM artifacts "
        "WHERE kind='semantic_summary' AND message_id=1 "
        "ORDER BY artifact_id DESC LIMIT 1").fetchone()
    meta = dict(json.loads(summary["meta"]), stale=True)
    db.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?",
                  (json.dumps(meta), summary["artifact_id"]))
    db.db.commit()
    with pytest.raises(semantic_send_gate.DeferredSend, match="summary_unverified"):
        semantic_send_gate.semantic_gate(db.db, event, payload, notify_flush._config())
    assert semantic_send_gate.semantic_render_state(db.db, raw, notify_flush._config()) == ()
    assert "要約（自動検査済）" not in notify_flush._format_event(db, raw)[0]

    # Unpublishable meta short-circuits before the whole-thread bundle.
    calls = []
    real = semantic_store.thread_bundle
    monkeypatch.setattr(semantic_store, 'thread_bundle',
                        lambda *a, **k: calls.append(a) or real(*a, **k))
    assert semantic_send_gate.semantic_render_state(db.db, raw, notify_flush._config()) == ()
    assert "要約（自動検査済）" not in notify_flush._format_event(db, raw)[0]
    assert calls == []
    db.close()


@pytest.mark.parametrize("column", ["content", "meta"])
def test_deep_summary_is_unpublishable_without_changing_saved_data(
        tmp_path, monkeypatch, column, recursion_limited_json):
    db = _delivery_db(tmp_path, [_message()])
    try:
        cfg = _cfg("enforce")
        monkeypatch.setattr(notify_flush, "_config", lambda: cfg)
        event = _semantic_event(db)
        payload = json.loads(event["payload"])
        raw = db.db.execute(
            "SELECT * FROM notify_outbox WHERE kind='new_messages'").fetchone()
        deep = "[" * 10_000 + "0" + "]" * 10_000
        with pytest.raises(RecursionError):
            json.loads(deep)
        with db.db:
            db.db.execute(f"UPDATE artifacts SET {column}=? WHERE kind='semantic_summary'", (deep,))
        before = list(db.db.iterdump())
        assert semantic_send_gate.semantic_render_state(db.db, raw, notify_flush._config()) == ()
        with pytest.raises(semantic_send_gate.DeferredSend, match="summary_unverified"):
            semantic_send_gate.semantic_gate(db.db, event, payload, notify_flush._config())
        assert "要約（自動検査済）" not in notify_flush._format_event(db, raw)[0]
        assert list(db.db.iterdump()) == before
    finally:
        db.close()


@pytest.mark.parametrize("in_progress", [False, True])
def test_deep_source_payload_is_deferred_or_frozen(
        tmp_path, monkeypatch, in_progress, recursion_limited_json):
    db = _delivery_db(tmp_path, [_message()])
    try:
        monkeypatch.setattr(notify_flush, "_config", lambda: _cfg("enforce"))
        event = _semantic_event(db)
        payload = json.loads(event["payload"])
        deep = "[" * 10_000 + "0" + "]" * 10_000
        with pytest.raises(RecursionError):
            json.loads(deep)
        with db.db:
            db.db.execute("UPDATE notify_outbox SET payload=? WHERE event_id=?", (deep, payload["src_event_id"]))
        before = list(db.db.iterdump())
        error = semantic_send_gate.FreezeSend if in_progress else semantic_send_gate.DeferredSend
        with pytest.raises(error, match="src_event_unverified"):
            semantic_send_gate.semantic_gate(db.db, event, payload, notify_flush._config(), in_progress=in_progress)
        assert list(db.db.iterdump()) == before
    finally:
        db.close()


@pytest.mark.parametrize("in_progress", [False, True])
def test_deep_source_progress_preserves_degraded_send_hold(
        tmp_path, monkeypatch, in_progress, recursion_limited_json):
    db = _delivery_db(tmp_path, [_message()])
    try:
        monkeypatch.setattr(notify_flush, "_config", lambda: _cfg("enforce"))
        event, payload, _ = _produced_degraded(db, _cfg("enforce"), [1])
        deep = "[" * 10_000 + "0" + "]" * 10_000
        with pytest.raises(RecursionError):
            json.loads(deep)
        with db.db:
            db.db.execute("UPDATE notify_outbox SET state='pending',attempts=0,progress=? WHERE event_id=?", (deep, payload["src_event_id"]))
        before = list(db.db.iterdump())
        error = semantic_send_gate.FreezeSend if in_progress else semantic_send_gate.DeferredSend
        with pytest.raises(error, match="src_event_unverified"):
            semantic_send_gate.semantic_gate(db.db, event, payload, notify_flush._config(), in_progress=in_progress)
        assert list(db.db.iterdump()) == before
        with db.db:
            db.db.execute("DELETE FROM notify_outbox WHERE event_id=?", (payload["src_event_id"],))
        error = semantic_send_gate.FreezeSend if in_progress else semantic_send_gate.StaleSend
        with pytest.raises(error, match="src_event_ineligible"):
            semantic_send_gate.semantic_gate(db.db, event, payload, notify_flush._config(), in_progress=in_progress)
    finally:
        db.close()


@pytest.mark.parametrize("in_progress", [False, True])
def test_initial_history_never_inherits_live_origin_notice(tmp_path, in_progress):
    db = _delivery_db(tmp_path, [_message()])
    try:
        event = _semantic_event(db)
        payload = json.loads(event["payload"])
        db.db.execute("INSERT INTO message_metadata(message_id,source,content,checked_at) "
                      "VALUES(1,'capture',?,0) ON CONFLICT(message_id,source) DO UPDATE SET content=excluded.content",
                      (json.dumps({"_initial_import": {"role": "history", "cohort_id": "a" * 32}}),))
        db.db.commit()
        error = semantic_send_gate.FreezeSend if in_progress else semantic_send_gate.StaleSend
        with pytest.raises(error, match="initial_history"):
            semantic_send_gate.semantic_gate(db.db, event, payload, _cfg("enforce"), in_progress=in_progress)
        assert dict(db.db.execute("SELECT * FROM notify_outbox WHERE event_id=?",
                                  (event["event_id"],)).fetchone()) == dict(event)
    finally:
        db.close()


def test_degraded_mixed_thread_blocks_history_but_allows_live_reply(tmp_path):
    db = _delivery_db(tmp_path, [_message(), _message(mid=2, parent=1)])
    try:
        cfg = _cfg("enforce")
        event, payload, _ = _produced_degraded(db, cfg, [1, 2])
        assert payload["target_message_ids"] == [1, 2]
        db.db.execute("INSERT INTO message_metadata(message_id,source,content,checked_at) "
                      "VALUES(1,'capture',?,0) ON CONFLICT(message_id,source) DO UPDATE SET content=excluded.content",
                      (json.dumps({"_initial_import": {"role": "history", "cohort_id": "a" * 32}}),))
        db.db.commit()
        with pytest.raises(semantic_send_gate.StaleSend, match="initial_history"):
            semantic_send_gate.semantic_gate(db.db, event, payload, cfg)
        with pytest.raises(semantic_send_gate.DeferredSend, match="degraded_unverified"):
            semantic_send_gate.semantic_gate(db.db, event, dict(payload, target_message_ids=[2]), cfg)
        live_path = tmp_path / "live"
        live_path.mkdir()
        live_db = _delivery_db(live_path, [_message(), _message(mid=2, parent=1)])
        try:
            with live_db.db:
                live_db.db.execute("INSERT INTO message_metadata(message_id,source,content,checked_at) "
                                   "VALUES(1,'capture',?,0) ON CONFLICT(message_id,source) "
                                   "DO UPDATE SET content=excluded.content",
                                   (json.dumps({"_initial_import": {"role": "history", "cohort_id": "a" * 32}}),))
            live_event, live_payload, _ = _produced_degraded(live_db, cfg, [1, 2])
            assert live_payload["target_message_ids"] == [2]
            semantic_send_gate.semantic_gate(live_db.db, live_event, live_payload, cfg)
        finally:
            live_db.close()
    finally:
        db.close()


def test_degraded_producer_keeps_live_reply_from_mixed_history_origin(tmp_path, monkeypatch):
    import semantic_notice
    db = _delivery_db(tmp_path, [_message(), _message(mid=2, parent=1)])
    try:
        cfg = _cfg("enforce")
        scfg = semantic_policy.semantic_config(cfg)[0]
        # Existing bootstrap intents would otherwise produce another notice.
        db.db.execute("UPDATE notify_outbox SET state='suppressed'")
        source = db.outbox_add("new_messages", 1, {"message_ids": [1,2]})
        db.db.execute("UPDATE notify_outbox SET created_at=0 WHERE event_id=?", (source,))
        db.db.execute("INSERT INTO message_metadata(message_id,source,content,checked_at) VALUES(1,'capture',?,0) "
                      "ON CONFLICT(message_id,source) DO UPDATE SET content=excluded.content",
                      (json.dumps({"_initial_import": {"role":"history", "cohort_id":"a"*32}}),))
        db.db.commit()
        monkeypatch.setattr(semantic_notice.semantic_transport, "llm_model", lambda: None)
        assert semantic_notice.emit_degraded(db, scfg) == 1
        event = db.db.execute("SELECT * FROM notify_outbox WHERE kind='semantic_notice'").fetchone()
        payload = json.loads(event["payload"])
        assert payload["target_message_ids"] == [2]
        assert payload["src_event_id"] == source
        semantic_send_gate.semantic_gate(db.db, event, payload, cfg)
        assert json.loads(db.db.execute("SELECT payload FROM notify_outbox WHERE event_id=?", (source,)).fetchone()[0])["message_ids"] == [1,2]
    finally:
        db.close()
