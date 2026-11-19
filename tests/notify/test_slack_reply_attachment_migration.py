"""Synthetic parent upload receipts survive the per-reply card migration."""
import asyncio
import copy
import json

import pytest

import notify_cards
import notify_parts
import notify_spool
from notify_testkit import NOW, _dispatch, _intent, _msg, _seed_thread
from slack_testkit import SLACK, _attach, _granted_card, _mkworld
from slack_testkit import isolated_slack_ledger as led

__all__ = ["led"]


@pytest.mark.parametrize("fault", [None, "unknown", "missing_spec", "hash", "thread", "scope", "queued"])
def test_parent_attachment_is_reused_only_with_complete_same_thread_evidence(
        led, tmp_path, monkeypatch, fault):
    monkeypatch.setattr(notify_cards.time, "time", lambda: NOW)
    _seed_thread(led, mids=(100,))
    _msg(led, 201, parent=100, body="Synthetic historical reply", ts=int(NOW - 10))
    aid, _, _ = _attach(led, tmp_path, mid=201)
    # Model the old parent plan: it carried the reply's attachment before
    # the reply received its own card. All payloads and receipts are synthetic.
    planner = notify_parts._thread_plan_ids
    with monkeypatch.context() as legacy:
        from slack_testkit import legacy_attachment_parts
        legacy_attachment_parts(legacy)
        legacy.setattr(notify_parts, "_thread_plan_ids", lambda *a, **kw: [100, 201])
        assert _dispatch(led, _intent(led, payload={"message_ids": [100]}), SLACK)["dispatched"]
    assert notify_parts._thread_plan_ids is planner
    world = _mkworld(led)
    asyncio.run(_granted_card(world.worker, led, world.root))
    parent = led.db.execute("SELECT * FROM notification_cards ORDER BY card_id LIMIT 1").fetchone()
    old = led.db.execute("SELECT * FROM notification_renders WHERE card_id=? ORDER BY render_rev DESC LIMIT 1",
                         (parent["card_id"],)).fetchone()
    part = led.db.execute("SELECT * FROM notification_render_parts WHERE delivery_id=? AND attachment_id=?",
                          (old["delivery_id"], aid)).fetchone()
    assert part["state"] == "delivered" and part["remote_id"]
    uploads = len(world.client.upload_calls)
    with led.db:
        if fault == "unknown":
            led.db.execute("UPDATE notification_render_parts SET state='unknown' WHERE delivery_id=? AND attachment_id=?",
                           (old["delivery_id"], aid))
        elif fault == "missing_spec":
            led.db.execute("UPDATE notification_renders SET spec_json=NULL WHERE delivery_id=?", (old["delivery_id"],))
        elif fault == "hash":
            led.db.execute("UPDATE notification_render_parts SET payload_sha256=? WHERE delivery_id=? AND attachment_id=?",
                           ("0" * 64, old["delivery_id"], aid))
        elif fault == "thread":
            led.db.execute("UPDATE notification_render_parts SET remote_id='1790000000.999999' WHERE delivery_id=? AND kind='thread'",
                           (old["delivery_id"],))
        elif fault == "scope":
            led.db.execute("UPDATE notification_renders SET channel_id='C_OTHER' WHERE delivery_id=?", (old["delivery_id"],))
        elif fault == "queued":
            led.db.execute("UPDATE notification_renders SET state='queued' WHERE delivery_id=?", (old["delivery_id"],))
    _dispatch(led, _intent(led, payload={"message_ids": [201]}), SLACK, now=NOW + 1)
    reply = led.db.execute("SELECT * FROM notification_cards WHERE card_key LIKE '%|reply:201'").fetchone()
    current = led.db.execute("SELECT * FROM notification_renders WHERE card_id=? ORDER BY render_rev DESC LIMIT 1",
                             (reply["card_id"],)).fetchone()
    if fault:
        assert current["state"] == "held"
        assert current["parts_state"] == "held"
        assert len(world.client.upload_calls) == uploads
        return
    spec = json.loads(current["spec_json"])
    migrated = next(p for p in spec["parts"]["manifest"] if p.get("attachment_id") == aid)
    assert spec["op"] == "create" and migrated["prior_remote_id"] == part["remote_id"]
    assert "edit_only" not in migrated
    assert migrated["prior_owner_message_id"] == parent["message_id"]
    from adapters.slack.cards import validate
    validate(spec)
    for field, value in (("prior_owner_message_id", "1790000000.999999"),
                         ("prior_owner_message_id", True), ("edit_only", True),
                         ("unavailable", True)):
        invalid = copy.deepcopy(spec)
        candidate = next(p for p in invalid["parts"]["manifest"] if p.get("attachment_id") == aid)
        candidate[field] = value
        with pytest.raises(ValueError):
            validate(invalid)
    notify_spool.publish_flags(SLACK, str(world.root))
    asyncio.run(_granted_card(world.worker, led, world.root))
    assert len(world.client.upload_calls) == uploads
    receipt = led.db.execute("SELECT state,remote_id,error_code FROM notification_render_parts WHERE delivery_id=? AND attachment_id=?",
                             (current["delivery_id"], aid)).fetchone()
    assert receipt["state"] == "delivered" and receipt["remote_id"] == part["remote_id"], dict(receipt)
    assert led.db.execute("SELECT parts_state FROM notification_renders WHERE delivery_id=?",
                          (current["delivery_id"],)).fetchone()[0] == "complete", [
        dict(row) for row in led.db.execute(
            "SELECT kind,state,error_code FROM notification_render_parts WHERE delivery_id=?",
            (current["delivery_id"],))]
    assert led.db.execute("SELECT remote_id FROM notification_render_parts WHERE delivery_id=? AND attachment_id=?",
                          (old["delivery_id"], aid)).fetchone()[0] == part["remote_id"]
    emitted = []
    with led.db:
        notify_cards.issue_render(led.db, reply["card_id"], SLACK, NOW + 2, emitted, force=True)
    update, = emitted
    updated_part = next(p for p in update["parts"]["manifest"] if p.get("attachment_id") == aid)
    assert updated_part["prior_owner_message_id"] == parent["message_id"]
    assert updated_part["prior_remote_id"] == part["remote_id"]
    validate(update)
    notify_spool.publish_specs(led.db, notify_spool.notify_dirs(str(world.root)), emitted, NOW + 2)
    asyncio.run(_granted_card(world.worker, led, world.root))
    updated_receipt = led.db.execute("SELECT state,remote_id,error_code FROM notification_render_parts WHERE delivery_id=? AND attachment_id=?",
                                    (update["delivery_id"], aid)).fetchone()
    assert updated_receipt["state"] == "delivered", dict(updated_receipt)
    assert len(world.client.upload_calls) == uploads
    # Parent evidence must not hide conflicting or unsettled child evidence.
    import notify_render
    from mcs_util import canonical, payload_hash
    for child_state, child_remote in (("delivered", "F_OTHER"), ("unknown", part["remote_id"]),
                                      ("pending", part["remote_id"])):
        led.db.execute("SAVEPOINT child_receipt_conflict")
        try:
            led.db.execute("UPDATE notification_render_parts SET state=?,remote_id=? WHERE delivery_id=? AND attachment_id=?",
                           (child_state, child_remote, update["delivery_id"], aid))
            current_card = led.db.execute("SELECT * FROM notification_cards WHERE card_id=?", (reply["card_id"],)).fetchone()
            content = notify_render.card_content(led.db, current_card, cfg=SLACK)
            _, hold = notify_cards._build_spec(led.db, current_card, content, "update", update["render_rev"] + 1, SLACK, NOW + 3)
            assert hold == "attachment_upload_identity_unverified", (child_state, child_remote)
        finally:
            led.db.execute("ROLLBACK TO child_receipt_conflict")
            led.db.execute("RELEASE child_receipt_conflict")
    for fault in ("source", "scope"):
        led.db.execute("SAVEPOINT child_sealed_identity")
        try:
            damaged = copy.deepcopy(update)
            if fault == "source":
                damaged["card_key"] += "|foreign-source"
            else:
                damaged["delivery"]["channel_id"] = "C_OTHER"
            led.db.execute("UPDATE notification_renders SET spec_json=?,payload_hash=? WHERE delivery_id=?",
                           (canonical(damaged).decode(), payload_hash(damaged), update["delivery_id"]))
            current_card = led.db.execute("SELECT * FROM notification_cards WHERE card_id=?",
                                          (reply["card_id"],)).fetchone()
            content = notify_render.card_content(led.db, current_card, cfg=SLACK)
            _, hold = notify_cards._build_spec(led.db, current_card, content, "update",
                                               update["render_rev"] + 1, SLACK, NOW + 3)
            assert hold == "attachment_upload_identity_unverified", fault
        finally:
            led.db.execute("ROLLBACK TO child_sealed_identity")
            led.db.execute("RELEASE child_sealed_identity")
    led.db.execute("SAVEPOINT child_group_unknown")
    try:
        led.db.execute("UPDATE notification_render_parts SET state='unknown' "
                       "WHERE delivery_id=? AND kind='file_group'", (update["delivery_id"],))
        emitted = []
        assert notify_cards.issue_render(led.db, reply["card_id"], SLACK, NOW + 3,
                                         emitted, force=True) is None
        assert emitted == []
        assert len(world.client.upload_calls) == uploads
    finally:
        led.db.execute("ROLLBACK TO child_group_unknown")
        led.db.execute("RELEASE child_group_unknown")


def test_a_held_reply_update_replans_once_the_parents_thread_receipt_lands(led, tmp_path, monkeypatch):
    import notify_card_renders
    import notify_flush
    import notify_scope
    clock = [NOW]
    monkeypatch.setattr(notify_cards.time, "time", lambda: clock[0])
    _seed_thread(led, mids=(100,))
    _msg(led, 201, parent=100, body="Synthetic historical reply", ts=int(NOW - 10))
    aid, _, _ = _attach(led, tmp_path, mid=201)
    with monkeypatch.context() as legacy:      # the old parent plan carried the reply's attachment
        from slack_testkit import legacy_attachment_parts
        legacy_attachment_parts(legacy)
        legacy.setattr(notify_parts, "_thread_plan_ids", lambda *a, **kw: [100, 201])
        assert _dispatch(led, _intent(led, payload={"message_ids": [100]}), SLACK)["dispatched"]
    world = _mkworld(led)
    asyncio.run(_granted_card(world.worker, led, world.root))
    parent = led.db.execute("SELECT * FROM notification_cards ORDER BY card_id LIMIT 1").fetchone()
    old = led.db.execute("SELECT delivery_id FROM notification_renders WHERE card_id=? ORDER BY render_rev DESC LIMIT 1",
                         (parent["card_id"],)).fetchone()[0]
    # The reply card is created while its file is not ready, so the reply itself holds no receipt
    # for it (not even an uncertain one); only the parent can prove the file afterwards.
    with led.db:
        led.db.execute("UPDATE attachments SET state='pending' WHERE attachment_id=?", (aid,))
    _dispatch(led, _intent(led, payload={"message_ids": [201]}), SLACK, now=NOW + 1)
    notify_spool.publish_flags(SLACK, str(world.root))
    asyncio.run(_granted_card(world.worker, led, world.root))
    reply = led.db.execute("SELECT * FROM notification_cards WHERE card_key LIKE '%|reply:201'").fetchone()
    assert reply["delivery_state"] == "delivered"
    assert led.db.execute("SELECT 1 FROM notification_render_parts p JOIN notification_renders r "
                          "ON r.delivery_id=p.delivery_id WHERE r.card_id=? AND p.attachment_id=?",
                          (reply["card_id"], aid)).fetchone() is None
    uploads = len(world.client.upload_calls)
    with led.db:
        led.db.execute("UPDATE attachments SET state='downloaded' WHERE attachment_id=?", (aid,))
        # The parent's thread receipt has not landed yet.
        led.db.execute("UPDATE notification_render_parts SET state='pending' WHERE delivery_id=? AND kind='thread'",
                       (old,))
        before_rev = led.db.execute("SELECT MAX(render_rev) FROM notification_renders WHERE card_id=?",
                                    (reply["card_id"],)).fetchone()[0]
        # A held build is stored but not returned (issue_render returns None for a budget hold).
        assert notify_cards.issue_render(led.db, reply["card_id"], SLACK, NOW + 2, [], force=True) is None

    def held():
        return led.db.execute(
            "SELECT r.delivery_id FROM notification_renders r WHERE r.card_id=? AND r.state='held' AND EXISTS("
            "SELECT 1 FROM notification_render_parts p WHERE p.delivery_id=r.delivery_id "
            "AND p.error_code='attachment_upload_identity_unverified')", (reply["card_id"],)).fetchall()
    [hold] = held()
    assert led.db.execute("SELECT render_rev FROM notification_renders WHERE delivery_id=?",
                          (hold[0],)).fetchone()[0] == before_rev + 1
    bound = [tuple(row) for row in led.db.execute(
        "SELECT event_id,delivery_id FROM notification_intent_cards WHERE card_id=? ORDER BY event_id",
        (reply["card_id"],))]
    assert notify_card_renders._replan_blocked(led.db, notify_scope.card_row(led.db, reply["card_id"]), hold[0])
    clock[0] = NOW + 11 + notify_cards.RESEAT_S
    for _ in range(2):           # the parent's thread is still pending: nothing is replanned
        notify_cards.sweep(led, SLACK)
        assert [tuple(row) for row in held()] == [tuple(hold)]
        assert [tuple(row) for row in led.db.execute(
            "SELECT event_id,delivery_id FROM notification_intent_cards WHERE card_id=? ORDER BY event_id",
            (reply["card_id"],))] == bound
    with led.db:
        led.db.execute("UPDATE notification_render_parts SET state='delivered' WHERE delivery_id=? AND kind='thread'",
                       (old,))
    notify_cards.sweep(led, SLACK)
    assert not held()
    latest = led.db.execute("SELECT delivery_id,state,spec_json FROM notification_renders WHERE card_id=? "
                            "ORDER BY render_rev DESC LIMIT 1", (reply["card_id"],)).fetchone()
    assert latest["delivery_id"] != hold[0] and latest["state"] == "queued"
    migrated = next(p for p in json.loads(latest["spec_json"])["parts"]["manifest"] if p.get("attachment_id") == aid)
    assert migrated["prior_owner_message_id"] == parent["message_id"]
    notify_flush.flush(led)
    asyncio.run(_granted_card(world.worker, led, world.root))
    assert len(world.client.upload_calls) == uploads            # the parent's file is reused, not re-sent
    assert led.db.execute("SELECT state FROM notification_renders WHERE delivery_id=?",
                          (latest["delivery_id"],)).fetchone()[0] == "delivered"
