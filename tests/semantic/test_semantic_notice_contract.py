"""Audited claims and timestamps survive the final notification render."""
import json

import pytest

import semantic_llm
import semantic_notice
import semantic_policy
import semantic_send_gate
from semantic_testkit import _cfg
import semantic_render
from semantic_testkit import _seeded


def test_notice_preserves_all_sections_and_uses_instant_order(tmp_path):
    db = _seeded(tmp_path)
    try:
        db.db.execute("UPDATE messages SET posted_at=? WHERE message_id=1",
                      ("2026-09-20T00:00:00+09:00",))
        db.db.execute("UPDATE messages SET posted_at=? WHERE message_id=2",
                      ("2026-09-19T20:00:00+00:00",))
        db.db.commit()
        claims = [{"section": section, "text": f"文{section}",
                   "claim_kind": "inference"}
                  for section in semantic_llm.CLAIM_SECTIONS]
        limitations = [f"制約{i}" for i in range(8)]
        text = semantic_render.render_notice(db.db, 1, 1,
                {"claims": claims, "limitations": limitations}, "PASS",
                targets=[1, 2], quality="full")
        assert "2026/09/20 05:00 JST" in text
        assert all(f"【提案】{c['text']}" in text for c in claims)
        assert not any(item in text for item in limitations)
        assert "要約対象外の情報があります。MCS原文を確認してください。" in text
    finally:
        db.close()


@pytest.mark.parametrize("mode,summary_mode,status,notification_free,enqueue", [
    ("enforce", "enforce", "PASS", False, True),
    ("shadow", "enforce", "PASS", False, False),
    ("enforce", "shadow", "PASS", False, False),
    ("enforce", "enforce", "PENDING", False, False),
    ("enforce", "enforce", "PASS", True, False),
])
def test_plan_and_per_target_intent_keep_selection_and_dedupe(
        tmp_path, mode, summary_mode, status, notification_free, enqueue):
    ledger = _seeded(tmp_path)
    try:
        summary = {"claims": [], "mandatory_facts": ["合成の原文確認事項"],
                   "_internal": "not rendered"}
        clean_summary = {key: value for key, value in summary.items() if not key.startswith("_")}
        summary_id = ledger.artifact_add("semantic_summary", json.dumps(clean_summary),
                                         project_id=1, message_id=1, meta={})
        origin = {"source": "synthetic-origin", "event_id": 991}
        args = (ledger, 1, 1, 1, "synthetic-fingerprint", "synthetic-policy",
                summary, status, {"revision": "selected-revision"}, [1, 2])
        kwargs = dict(mode=mode, summary_mode=summary_mode,
                      notification_free=notification_free, origin=origin,
                      content_quality="partial")
        with ledger.db:
            semantic_notice.record_notice_plan_tx(*args, **kwargs)
            semantic_notice.record_notice_plan_tx(*args, **kwargs)
        plans = ledger.artifacts(semantic_policy.KIND_PLAN, message_id=1)
        assert len(plans) == 1
        plan = json.loads(plans[0]["content"])
        meta = json.loads(plans[0]["meta"])
        assert plan["root_id"] == 1 and plan["target_message_id"] == 1
        assert "合成の原文確認事項" in plan["text"]
        assert "取得：一部未取得" in plan["text"]
        assert "not rendered" not in plan["text"]
        assert meta["origin"] == origin
        assert meta["fingerprint"] == "synthetic-fingerprint"
        assert meta["policy_fingerprint"] == "synthetic-policy"
        assert meta["publication_mode"] == summary_mode and meta["mode"] == mode
        assert meta["audit_status"] == status
        # The replay keeps the first plan's enqueued fact and emits at most one intent.
        assert bool(meta.get("enqueued")) is enqueue
        rows = ledger.db.execute("SELECT payload FROM notify_outbox WHERE kind='semantic_notice'").fetchall()
        assert len(rows) == int(enqueue)
        if enqueue:
            payload = json.loads(rows[0]["payload"])
            assert payload["target_message_id"] == 1
            assert payload["target_revision"] == "selected-revision"
            assert payload["policy_fingerprint"] == "synthetic-policy"
            assert payload["fingerprint"] == "synthetic-fingerprint"
            assert payload["text"] == plan["text"]
            assert payload["summary_artifact_id"] == summary_id
            from mcs_util import payload_hash
            assert payload["summary_hash"] == payload_hash(clean_summary)
            assert payload["notice_text_hash"] == payload_hash(plan["text"])
            receipt = ledger.db.execute("SELECT content FROM artifacts WHERE artifact_id=?",
                                        (payload["notice_receipt_id"],)).fetchone()
            assert json.loads(receipt["content"])["notice_text_hash"] == payload["notice_text_hash"]
            assert type(payload["src_event_id"]) is int
    finally:
        ledger.close()


def test_plan_failure_rolls_back_intent_and_callers_pending_result(tmp_path, monkeypatch):
    ledger = _seeded(tmp_path)
    original = ledger.artifact_add_tx
    try:
        def fail_plan(kind, *args, **kwargs):
            if kind == semantic_policy.KIND_PLAN:
                raise ValueError("synthetic-plan-write-failure")
            return original(kind, *args, **kwargs)

        monkeypatch.setattr(ledger, "artifact_add_tx", fail_plan)
        with pytest.raises(ValueError, match="synthetic-plan-write-failure"):
            with ledger.db:
                ledger.artifact_add_tx("synthetic_result", "{}", project_id=1, message_id=1)
                semantic_notice.record_notice_plan_tx(
                    ledger, 1, 1, 1, "synthetic-fingerprint", "synthetic-policy",
                    {"claims": []}, "PASS", {"revision": "selected-revision"}, [1],
                    mode="enforce", summary_mode="enforce", notification_free=False,
                    origin=None, content_quality="full")
        assert not ledger.db.in_transaction
        assert ledger.db.execute("SELECT count(*) FROM artifacts WHERE kind IN ('synthetic_result',?)",
                                 (semantic_policy.KIND_PLAN,)).fetchone()[0] == 0
        assert ledger.db.execute("SELECT count(*) FROM notify_outbox WHERE kind='semantic_notice'").fetchone()[0] == 0
        assert ledger.db.execute("SELECT count(*) FROM notify_outbox WHERE kind='new_messages'").fetchone()[0] > 0
        assert ledger.db.execute("SELECT 1").fetchone()[0] == 1
    finally:
        ledger.close()


def test_notice_does_not_bind_a_different_latest_summary(tmp_path):
    ledger = _seeded(tmp_path)
    try:
        ledger.artifact_add("semantic_summary", json.dumps({"claims": [{"text": "different"}]}),
                            project_id=1, message_id=1, meta={})
        with ledger.db:
            semantic_notice.record_notice_plan_tx(
                ledger, 1, 1, 1, "synthetic-fingerprint", "synthetic-policy",
                {"claims": []}, "PASS", {"revision": "selected-revision"}, [1],
                mode="enforce", summary_mode="enforce", notification_free=False,
                origin=None, content_quality="full")
        assert not ledger.db.execute("SELECT 1 FROM notify_outbox WHERE kind='semantic_notice'").fetchone()
    finally:
        ledger.close()


def test_notice_readers_use_pending_connection_without_commit_or_close(tmp_path):
    ledger = _seeded(tmp_path)
    try:
        ledger.db.execute("BEGIN IMMEDIATE")
        ledger.db.execute("UPDATE patients SET patient_name='合成の未commit名' WHERE project_id=1")
        ledger.artifact_add_tx(semantic_policy.KIND_PLAN, '{}', message_id=1,
                               meta={"fingerprint": "pending-fingerprint", "audit_status": "PASS"})
        changes = ledger.db.total_changes
        text = semantic_render.render_notice(ledger.db, 1, 1, {"claims": []}, "PASS")
        assert "合成の未commit名" in text
        assert semantic_notice.plan_exists(ledger.db, 1, "pending-fingerprint", "PASS")
        assert semantic_notice.origin_event_id(ledger.db, 1, [1]) is not None
        assert not semantic_notice.outbox_has_delivery(ledger.db, "absent-key")
        scfg, errors = semantic_policy.semantic_config(_cfg("enforce"))
        assert not errors
        assert semantic_send_gate.enforce_block_reason(ledger.db, scfg, 1) is None
        assert ledger.db.total_changes == changes
        assert ledger.db.in_transaction
        ledger.db.rollback()
        assert not semantic_notice.plan_exists(ledger.db, 1, "pending-fingerprint", "PASS")
        assert "合成の未commit名" not in semantic_render.render_degraded(ledger.db, 1)
        assert ledger.db.execute("SELECT 1").fetchone()[0] == 1
    finally:
        ledger.close()
