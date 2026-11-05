"""Reply routing stays with its proven parent; synthetic ledgers and receipts only."""
import json

import pytest

import notify_cards
import notify_card_renders
import notify_scope
import notify_transport
from notify_testkit import CFG, NOW, _dispatch, _intent, _latest_render, _seed_thread, _uuid, led
from slack_testkit import SLACK

__all__ = ["led"]


@pytest.fixture(params=["slack", "discord"])
def cfg(request):
    base = SLACK if request.param == "slack" else CFG
    return {**base, "notify": {**base["notify"], "reply_cards": True}}


def _cards(led):
    return [dict(row) for row in led.db.execute(
        "SELECT * FROM notification_cards ORDER BY card_id")]


def _echo(render):
    return {**notify_scope.stored_scope(render),
            **{key: render[key] for key in (
                "delivery_id", "render_rev", "payload_hash", "route_epoch", "correlation")}}


def _begin(led, render, cfg, n):
    return notify_transport.apply_transport_begin(led, {
        **_echo(render), "version": 2 if render["transport"] == "slack" else 1,
        "op": "transport_begin", "command_id": _uuid(n),
        "attempt_id": f"{n:016x}", "worker_id": "ab" * 8}, cfg, now=NOW + 1)


def _receipt(led, render, cfg, n, result="delivered", message_id="reply-post"):
    return notify_transport.apply_transport_receipt(led, {
        **_echo(render), "command_id": _uuid(n + 1000),
        "attempt_id": f"{n:016x}", "result": result,
        "message_id": message_id}, cfg, now=NOW + 2)


def _deliver(led, render, cfg, n, *, message_id, thread_id):
    assert _begin(led, render, cfg, n)["granted"]
    assert _receipt(led, render, cfg, n, message_id=message_id)["applied"]
    parts = led.db.execute(
        "SELECT * FROM notification_render_parts WHERE delivery_id=? "
        "AND kind!='card' ORDER BY idx", (render["delivery_id"],)).fetchall()
    for part in parts:
        remote = thread_id if part["kind"] == "thread" else f"part-{n}-{part['idx']}"
        assert notify_transport.apply_part_receipt(led, {
            **_echo(render), "part_id": part["part_id"], "result": "delivered",
            "remote_id": remote}, cfg, now=NOW + 3)["applied"]


def _scene(led, cfg):
    _seed_thread(led)
    event = _intent(led)
    assert _dispatch(led, event, cfg)["dispatched"]
    root, reply = _cards(led)
    return event, root, reply


def _deliver_root(led, root, cfg):
    thread = "root-post" if root["transport"] == "slack" else "root-thread"
    _deliver(led, _latest_render(led, root["card_id"]), cfg, 100,
             message_id="root-post", thread_id=thread)
    return thread


def test_reply_never_falls_back_to_channel_after_six_hours(led, cfg):
    event, _, reply = _scene(led, cfg)
    late = NOW + notify_cards.SIGNAL_SOURCE_HOLD_MAX_S + 3600
    assert not _dispatch(led, event, cfg, now=late)["dispatched"]
    assert _latest_render(led, reply["card_id"]) is None
    current = notify_scope.card_row(led.db, reply["card_id"])
    assert current["message_id"] is None and current["thread_id"] is None
    outbox = led.db.execute("SELECT * FROM notify_outbox WHERE event_id=?",
                            (event["event_id"],)).fetchone()
    assert outbox["state"] == "pending"
    assert json.loads(outbox["progress"])["thread_hold"] == "source_thread_not_ready"


def test_sealed_event_reentry_resumes_reply_after_root_receipts_without_sweep(led, cfg):
    event, root, reply = _scene(led, cfg)
    thread = _deliver_root(led, root, cfg)
    assert _latest_render(led, reply["card_id"]) is None
    assert _dispatch(led, event, cfg, now=NOW + 4)["dispatched"]
    render = _latest_render(led, reply["card_id"])
    spec = json.loads(render["spec_json"])
    assert spec["parts"]["source_thread"] is True
    assert spec["parts"]["post_source_id"] == 101
    assert spec["delivery"]["thread_id"] == thread
    assert "message_id" not in spec["delivery"]
    _dispatch(led, event, cfg, now=NOW + 5)
    assert _latest_render(led, reply["card_id"])["delivery_id"] == render["delivery_id"]
    assert led.db.execute("SELECT state FROM notify_outbox WHERE event_id=?",
                          (event["event_id"],)).fetchone()[0] == "pending"
    _deliver(led, render, cfg, 200, message_id="reply-post", thread_id=thread)
    assert led.db.execute("SELECT state FROM notify_outbox WHERE event_id=?",
                          (event["event_id"],)).fetchone()[0] == "accepted"


@pytest.mark.parametrize("op", ["create", "update"])
@pytest.mark.parametrize("fault", ["missing", "scope", "recreated"])
def test_parent_drift_denies_sealed_reply_and_preserves_existing_remote_identity(led, cfg, op, fault):
    event, root, reply = _scene(led, cfg)
    thread = _deliver_root(led, root, cfg)
    _dispatch(led, event, cfg, now=NOW + 4)
    render = _latest_render(led, reply["card_id"])
    if op == "update":
        _deliver(led, render, cfg, 200, message_id="reply-post", thread_id=thread)
        with led.db:
            notify_cards.issue_render(led.db, reply["card_id"], cfg, NOW + 5, [], force=True)
        render = _latest_render(led, reply["card_id"])
        assert render["op"] == "update"
    before = notify_scope.card_row(led.db, reply["card_id"])
    with led.db:
        if fault == "missing":
            led.db.execute("UPDATE notification_cards SET thread_state='deleted' WHERE card_id=?",
                           (root["card_id"],))
        elif fault == "scope":
            led.db.execute("UPDATE notification_cards SET channel_id='foreign-channel' WHERE card_id=?",
                           (root["card_id"],))
        else:
            led.db.execute("UPDATE notification_cards SET message_id='new-root',thread_id='new-root' "
                           "WHERE card_id=?", (root["card_id"],))
    begun = _begin(led, render, cfg, 300)
    assert not begun["granted"] and begun["error"] == "denied_reply_thread_changed"
    after = notify_scope.card_row(led.db, reply["card_id"])
    assert (after["message_id"], after["thread_id"]) == (before["message_id"], before["thread_id"])
    assert _latest_render(led, reply["card_id"])["state"] == "cancelled"
    specs = []
    with led.db:
        notify_cards.issue_render(led.db, reply["card_id"], cfg, NOW + 6, specs, force=True)
    assert specs == []


@pytest.mark.parametrize("state", ["granted", "unknown"])
def test_parent_drift_never_releases_unsettled_reply_attempt(led, cfg, state):
    event, root, reply = _scene(led, cfg)
    _deliver_root(led, root, cfg)
    _dispatch(led, event, cfg, now=NOW + 4)
    render = _latest_render(led, reply["card_id"])
    assert _begin(led, render, cfg, 200)["granted"]
    if state == "unknown":
        assert _receipt(led, render, cfg, 200, result="unknown", message_id=None)["applied"]
    before = dict(led.db.execute(
        "SELECT * FROM notification_delivery_attempts WHERE attempt_id=?", (f"{200:016x}",)).fetchone())
    with led.db:
        led.db.execute("UPDATE notification_cards SET message_id='new-root',thread_id='new-root' "
                       "WHERE card_id=?", (root["card_id"],))
        assert notify_cards.issue_render(led.db, reply["card_id"], cfg, NOW + 5, [], force=True) is None
    _dispatch(led, event, cfg, now=NOW + 6)
    after = dict(led.db.execute(
        "SELECT * FROM notification_delivery_attempts WHERE attempt_id=?", (f"{200:016x}",)).fetchone())
    assert after == before
    assert _latest_render(led, reply["card_id"])["state"] == ("sending" if state == "granted" else "unknown")


def test_old_queued_channel_fallback_is_resealed_before_grant(led, cfg, monkeypatch):
    # Reproduce the previous version's already sealed fallback, without sending it.
    with monkeypatch.context() as previous:
        previous.setattr(notify_card_renders, "_bind_reply_thread", lambda db, card, now: (card, False))
        event, root, reply = _scene(led, cfg)
    old = _latest_render(led, reply["card_id"])
    assert "thread_id" not in json.loads(old["spec_json"])["delivery"]
    # The previous worker queued this envelope before the current hold gate.
    with led.db:
        led.db.execute("UPDATE notification_renders SET state='queued' WHERE delivery_id=?",
                       (old["delivery_id"],))
    thread = _deliver_root(led, root, cfg)
    result = _begin(led, old, cfg, 300)
    assert result["error"] == "denied_reply_thread_changed"
    new = _latest_render(led, reply["card_id"])
    assert new["delivery_id"] != old["delivery_id"]
    assert led.db.execute("SELECT state FROM notification_renders WHERE delivery_id=?",
                          (old["delivery_id"],)).fetchone()[0] == "cancelled"
    spec = json.loads(new["spec_json"])
    assert spec["parts"]["source_thread"] is True
    assert spec["delivery"]["thread_id"] == thread
    assert _begin(led, new, cfg, 301)["granted"]
    assert led.db.execute("SELECT state FROM notify_outbox WHERE event_id=?",
                          (event["event_id"],)).fetchone()[0] == "pending"


def test_parent_drift_preserves_unknown_parts_of_delivered_reply(led, cfg):
    event, root, reply = _scene(led, cfg)
    _deliver_root(led, root, cfg)
    _dispatch(led, event, cfg, now=NOW + 4)
    render = _latest_render(led, reply["card_id"])
    assert _begin(led, render, cfg, 200)["granted"]
    assert _receipt(led, render, cfg, 200)["applied"]
    assert notify_transport.apply_part_receipt(led, {
        **_echo(render), "part_id": "thread", "result": "unknown",
        "error_code": "synthetic_timeout"}, cfg, now=NOW + 5)["applied"]
    before = [dict(row) for row in led.db.execute(
        "SELECT * FROM notification_render_parts WHERE delivery_id=? ORDER BY idx",
        (render["delivery_id"],))]
    with led.db:
        led.db.execute("UPDATE notification_cards SET message_id='new-root',thread_id='new-root' "
                       "WHERE card_id=?", (root["card_id"],))
        assert notify_cards.issue_render(led.db, reply["card_id"], cfg, NOW + 6, [], force=True) is None
    assert [dict(row) for row in led.db.execute(
        "SELECT * FROM notification_render_parts WHERE delivery_id=? ORDER BY idx",
        (render["delivery_id"],))] == before
    assert any(part["state"] == "unknown" for part in before)
    assert notify_scope.card_row(led.db, reply["card_id"])["message_id"] == "reply-post"


@pytest.mark.parametrize("reply_cards", [False, True])
def test_single_post_layout_omits_other_post_stamp_picker_but_keeps_direct_stamps(led, cfg, monkeypatch, reply_cards):
    monkeypatch.setattr(notify_card_renders, "_stamp_enabled", lambda cfg: True)
    cfg = {**cfg, "notify": {**cfg["notify"], "reply_cards": reply_cards}}
    _seed_thread(led, mids=(100,))
    _dispatch(led, _intent(led, payload={"message_ids": [100]}), cfg)
    spec = json.loads(_latest_render(led)["spec_json"])
    ids = [button["id"] for row in spec["parts"]["action_rows"] for button in row]
    single = reply_cards or cfg["notify"]["interactive"] == "slack"
    assert ("stamp" in ids) is not single
    assert "stamp_press" in ids
    assert spec["parts"].get("post_source_id") == (100 if single else None)
    assert ids.count("stamp_press") == 5
