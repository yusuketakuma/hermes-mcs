"""Late synthetic replies reach their original Slack thread and channel once."""
import asyncio

import notify_cards
from notify_testkit import NOW, _card, _dispatch, _intent, _msg, _seed_thread
from slack_testkit import SLACK, _granted_card, _mkworld
from test_mcs_slack import isolated_slack_ledger as led

__all__ = ["led"]


def test_delayed_reply_is_new_in_original_thread_and_broadcast_once(led, monkeypatch):
    clock = [NOW]
    monkeypatch.setattr(notify_cards.time, "time", lambda: clock[0])
    _seed_thread(led)
    assert _dispatch(led, _intent(led), SLACK, now=clock[0])["dispatched"]
    world = _mkworld(led)
    asyncio.run(_granted_card(world.worker, led, world.root))
    original = _card(led)
    assert original["delivery_state"] == "delivered"
    original_ts = original["message_id"]
    before = len(world.client.thread_posts)

    clock[0] += 3 * 86400
    _msg(led, 202, parent=100, body="合成の遅れて届いた返信", ts=int(clock[0]))
    intent = _intent(led, payload={"message_ids": [202]})
    assert _dispatch(led, intent, SLACK, now=clock[0])["dispatched"]
    notify_cards.publish_flags(SLACK, str(world.root))
    asyncio.run(_granted_card(world.worker, led, world.root))

    posts = world.client.thread_posts[before:]
    assert len(posts) == 1
    assert posts[0]["thread_ts"] == original_ts
    assert posts[0]["reply_broadcast"] is True
    assert "合成の遅れて届いた返信" in posts[0]["text"]
    assert _card(led)["message_id"] == original_ts
    assert led.db.execute("SELECT COUNT(*) FROM notification_cards").fetchone()[0] == 1
    assert led.db.execute("SELECT state FROM notify_outbox WHERE event_id=?",
                          (intent["event_id"],)).fetchone()[0] == "accepted"

    _dispatch(led, intent, SLACK, now=clock[0])
    asyncio.run(_granted_card(world.worker, led, world.root))
    assert len(world.client.thread_posts) == before + 1
