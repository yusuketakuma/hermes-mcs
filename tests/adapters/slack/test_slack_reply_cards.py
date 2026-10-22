"""Slack reply cards (layout 4, notify.slack_reply_cards): each later reply gets
its own card inside the root card's thread; existing cards keep collecting.
Synthetic ledger and fake Slack client only."""
import asyncio
import json

import pytest

import notify_cards
import notify_spool
from notify_testkit import NOW, _dispatch, _intent, _msg, _seed_thread
from slack_testkit import SLACK, _granted_card, _mkworld
from slack_testkit import isolated_slack_ledger as led

__all__ = ["led"]

REPLY = {**SLACK, "notify": {**SLACK["notify"], "slack_reply_cards": True}}


def _cards(led):
    return [dict(r) for r in led.db.execute(
        "SELECT * FROM notification_cards ORDER BY card_id")]


def _spec(led, card_id):
    row = led.db.execute("SELECT spec_json FROM notification_renders WHERE card_id=? "
                         "ORDER BY render_rev DESC LIMIT 1", (card_id,)).fetchone()
    return json.loads(row[0])


def _deliver(world, led):
    asyncio.run(_granted_card(world.worker, led, world.root))


def test_late_reply_gets_its_own_card_in_the_root_thread(led, monkeypatch):
    clock = [NOW]
    monkeypatch.setattr(notify_cards.time, "time", lambda: clock[0])
    _seed_thread(led, mids=(100,))
    assert _dispatch(led, _intent(led), REPLY, now=clock[0])["dispatched"]
    world = _mkworld(led)
    _deliver(world, led)
    (root,) = _cards(led)
    assert root["layout"] == 4 and root["delivery_state"] == "delivered"
    assert root["thread_id"] == root["message_id"]
    before = len(world.client.thread_posts)

    clock[0] += 3600
    _msg(led, 202, parent=100, body="合成の後から届いた返信", ts=int(clock[0]))
    intent = _intent(led, payload={"message_ids": [202]})
    assert _dispatch(led, intent, REPLY, now=clock[0])["dispatched"]
    notify_spool.publish_flags(REPLY, str(world.root))
    _deliver(world, led)

    root, reply = _cards(led)
    assert reply["card_key"].endswith("|reply:202")
    assert json.loads(reply["anchor_key"]) == {"root": 100, "source": 202}
    assert reply["layout"] == 4 and reply["delivery_state"] == "delivered"
    # posted inside the root card's thread, never re-shown in the channel
    assert reply["thread_id"] == root["message_id"] != reply["message_id"]
    posts = world.client.thread_posts[before:]
    card_posts = [p for p in posts if p.get("blocks")]
    assert len(card_posts) == 1 and card_posts[0]["thread_ts"] == root["message_id"]
    assert not any(p.get("reply_broadcast") for p in posts)
    assert not any("新しい返信をカードに追加しました。" in (p.get("text") or "") for p in posts)

    spec = _spec(led, reply["card_id"])
    assert spec["parts"]["source_thread"] is True
    assert [p["original"] for p in spec["parts"]["slack"]["face_posts"]] == ["合成の後から届いた返信"]
    root_spec = _spec(led, root["card_id"])
    texts = [c.get("text") or "" for c in root_spec["parts"]["containers"]]
    assert "↳ 返信 1件はスレッド内" in texts
    assert "合成の後から届いた返信" not in json.dumps(root_spec, ensure_ascii=False)
    # the stamp row on each card aims at the post that card shows
    root_content = notify_cards.card_content(led.db, root, cfg=REPLY)
    reply_content = notify_cards.card_content(led.db, reply, cfg=REPLY)
    assert root_content["shown"] == [100] and reply_content["shown"] == [202]
    assert notify_cards._stamp_target(led.db, reply, reply_content)[0] == 202
    assert notify_cards._stamp_target(led.db, root, root_content)[0] == 100


def test_reply_card_waits_until_the_root_thread_exists(led):
    _seed_thread(led, mids=(100, 101))
    assert _dispatch(led, _intent(led), REPLY, now=NOW)["dispatched"]
    root, reply = _cards(led)
    assert reply["card_key"].endswith("|reply:101")
    # no render for the reply until its root card has a delivered thread
    assert led.db.execute("SELECT COUNT(*) FROM notification_renders WHERE card_id=?",
                          (reply["card_id"],)).fetchone()[0] == 0
    progress = led.db.execute("SELECT progress FROM notify_outbox").fetchone()[0]
    assert json.loads(progress)["thread_hold"] == "source_thread_not_ready"


def test_an_existing_layout_three_root_keeps_collecting_replies(led, monkeypatch):
    clock = [NOW]
    monkeypatch.setattr(notify_cards.time, "time", lambda: clock[0])
    _seed_thread(led, mids=(100,))
    assert _dispatch(led, _intent(led), SLACK, now=clock[0])["dispatched"]
    (root,) = _cards(led)
    assert root["layout"] == 3

    clock[0] += 3600
    _msg(led, 202, parent=100, body="合成の返信", ts=int(clock[0]))
    _dispatch(led, _intent(led, payload={"message_ids": [202]}), REPLY, now=clock[0])
    (only,) = _cards(led)
    assert only["card_id"] == root["card_id"] and only["layout"] == 3


@pytest.mark.parametrize("value", ["true", 1, None])
def test_reply_card_switch_must_be_boolean(value):
    import mcs_config
    assert any("slack_reply_cards" in e for e in mcs_config.validate_notify({"slack_reply_cards": value}))
