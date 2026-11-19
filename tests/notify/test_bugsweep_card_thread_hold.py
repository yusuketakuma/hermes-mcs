"""Thread holds keep outbox quarantine; a never-sent reply follows its re-created root.

Synthetic temp ledgers and receipts only."""
import json

import notify_cards
import notify_scope
import notify_transport
from notify_testkit import NOW, _latest_render, _uuid, led
from test_reply_card_binding_hotfix import _begin, _deliver, _deliver_root, _echo, _receipt, _scene, cfg

__all__ = ["led", "cfg"]


def _outbox(led, event_id):
    return led.db.execute("SELECT * FROM notify_outbox WHERE event_id=?", (event_id,)).fetchone()


def _recreate_root(led, root):
    # the root's old message/thread is gone and a new create bound a new thread
    with led.db:
        led.db.execute("UPDATE notification_cards SET message_id='new-root',thread_id='new-root' "
                       "WHERE card_id=?", (root["card_id"],))


def test_thread_hold_never_releases_a_quarantined_event(led, cfg):
    event, _, reply = _scene(led, cfg)   # root unproven: the reply waits for its thread
    assert json.loads(_outbox(led, event["event_id"])["progress"])["thread_hold"]
    led.outbox_hold(event["event_id"], "restore_unlinked")
    notify_cards.sweep(led, cfg, now=NOW + 10)
    with led.db:
        notify_cards.issue_render(led.db, reply["card_id"], cfg, NOW + 11, [], force=True)
    row = _outbox(led, event["event_id"])
    assert row["state"] == "failed" and row["next_try"] is None
    assert json.loads(row["progress"])["hold_reason"] == "restore_unlinked"
    assert led.outbox_due(limit=50) == []


def test_never_sent_reply_follows_recreated_root_thread(led, cfg):
    event, root, reply = _scene(led, cfg)
    old_thread = _deliver_root(led, root, cfg)
    notify_cards.dispatch_intent(led, dict(event), cfg, now=NOW + 4)
    render = _latest_render(led, reply["card_id"])
    assert json.loads(render["spec_json"])["delivery"]["thread_id"] == old_thread
    assert _begin(led, render, cfg, 200)["granted"]
    # the old thread is gone: the create provably posted nothing but keeps its binding
    assert notify_transport.apply_transport_receipt(led, {
        **_echo(render), "command_id": _uuid(1200), "attempt_id": f"{200:016x}",
        "result": "not_sent", "error_code": "http_404"}, cfg, now=NOW + 3)["applied"]
    assert notify_scope.card_row(led.db, reply["card_id"])["thread_id"] == old_thread
    _recreate_root(led, root)
    specs = []
    with led.db:
        notify_cards.issue_render(led.db, reply["card_id"], cfg, NOW + 6, specs, force=True)
    card = notify_scope.card_row(led.db, reply["card_id"])
    assert (card["thread_id"], card["thread_state"], card["message_id"]) == ("new-root", "created", None)
    new = _latest_render(led, reply["card_id"])
    assert new["delivery_id"] != render["delivery_id"]
    assert json.loads(new["spec_json"])["delivery"]["thread_id"] == "new-root"
    _deliver(led, new, cfg, 300, message_id="reply-post", thread_id="new-root")
    assert _outbox(led, event["event_id"])["state"] == "accepted"


def test_reply_with_send_evidence_keeps_its_old_thread(led, cfg):
    event, root, reply = _scene(led, cfg)
    old_thread = _deliver_root(led, root, cfg)
    notify_cards.dispatch_intent(led, dict(event), cfg, now=NOW + 4)
    render = _latest_render(led, reply["card_id"])
    assert _begin(led, render, cfg, 200)["granted"]
    assert _receipt(led, render, cfg, 200, result="unknown", message_id=None)["applied"]
    _recreate_root(led, root)
    specs = []
    with led.db:
        assert notify_cards.issue_render(led.db, reply["card_id"], cfg, NOW + 6, specs, force=True) is None
    assert specs == []
    card = notify_scope.card_row(led.db, reply["card_id"])
    assert (card["thread_id"], card["message_id"]) == (old_thread, None)
    assert _latest_render(led, reply["card_id"])["delivery_id"] == render["delivery_id"]
