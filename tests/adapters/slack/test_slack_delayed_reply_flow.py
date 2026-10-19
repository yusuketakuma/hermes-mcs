"""Late synthetic replies reach their original Slack thread and channel once."""
import asyncio
import json
import pytest

import notify_cards
import notify_spool
from notify_testkit import NOW, _card, _dispatch, _intent, _latest_render, _msg, _seed_thread
from slack_testkit import SLACK, _granted_card, _mkworld
from slack_testkit import isolated_slack_ledger as led

__all__ = ["led"]


@pytest.mark.parametrize("legacy", [False, pytest.param(True, marks=pytest.mark.thread_bodies)])
def test_delayed_reply_is_new_in_original_thread_and_broadcast_once(led, monkeypatch, legacy):
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
    notify_spool.publish_flags(SLACK, str(world.root))
    asyncio.run(_granted_card(world.worker, led, world.root))

    # The new layout keeps the original on the card, with one short broadcast
    # notice. Legacy cards keep their header/body split and broadcast once too.
    posts = world.client.thread_posts[before:]
    expected = 2 if legacy else 1
    assert len(posts) == expected
    assert all(p["thread_ts"] == original_ts for p in posts)
    assert [p.get("reply_broadcast") for p in posts] == ([True, None] if legacy else [True])
    if legacy:
        assert "合成の遅れて届いた返信" in posts[1]["text"]
    else:
        assert "新しい返信をカードに追加しました。" in posts[0]["text"]
        assert "合成の遅れて届いた返信" not in posts[0]["text"]
        current = json.loads(_latest_render(led)["spec_json"])
        assert "合成の遅れて届いた返信" in [p["original"] for p in current["parts"]["slack"]["face_posts"]]
    assert _card(led)["message_id"] == original_ts
    assert led.db.execute("SELECT COUNT(*) FROM notification_cards").fetchone()[0] == 1
    assert led.db.execute("SELECT state FROM notify_outbox WHERE event_id=?",
                          (intent["event_id"],)).fetchone()[0] == "accepted"

    _dispatch(led, intent, SLACK, now=clock[0])
    asyncio.run(_granted_card(world.worker, led, world.root))
    assert len(world.client.thread_posts) == before + expected

    # Re-rendering accepted content is an edit, never a fresh notification.
    issued = []
    with led.db:
        led.db.execute("UPDATE messages SET body_text='合成の編集後返信',content_hash=? WHERE message_id=202", ("e" * 64,))
        notify_cards.issue_render(led.db, 1, SLACK, clock[0] + 1, issued, force=True)
    notify_spool.publish_specs(led.db, notify_spool.notify_dirs(str(world.root)), issued, clock[0] + 1)
    asyncio.run(_granted_card(world.worker, led, world.root))
    assert len(world.client.thread_posts) == before + expected
    assert sum(bool(p.get("reply_broadcast")) for p in world.client.thread_posts[before:]) == 1


def test_new_reply_broadcast_excludes_old_face_posts_moving_to_thread(led, monkeypatch):
    import present_slack
    clock = [NOW]
    monkeypatch.setattr(notify_cards.time, "time", lambda: clock[0])
    monkeypatch.setattr(present_slack, "FACE_ORIGINAL_MAX", 20)
    _seed_thread(led)
    _dispatch(led, _intent(led), SLACK, now=clock[0])
    world = _mkworld(led)
    asyncio.run(_granted_card(world.worker, led, world.root))
    assert not world.client.thread_posts

    clock[0] += 3 * 86400
    with led.db:
        led.db.execute("UPDATE messages SET body_text=?,content_hash=? WHERE message_id=100",
                       ("GROWN_OLD_ORIGINAL" * 5, "c" * 64))
    _msg(led, 202, parent=100, body="LATE_NEW_REPLY", ts=int(clock[0]))
    _dispatch(led, _intent(led, payload={"message_ids": [202]}), SLACK, now=clock[0])
    spec = json.loads(_latest_render(led)["spec_json"])
    notify_spool.publish_flags(SLACK, str(world.root))
    asyncio.run(_granted_card(world.worker, led, world.root))
    bodies = [p for p in spec["parts"]["manifest"] if p["kind"] == "body_part"]
    text_by_name = {p["name"]: text for p, text in zip(bodies, spec["parts"]["thread_body_parts"], strict=True)}
    assert len(world.client.thread_posts) == 6
    broadcasts = [p for p in world.client.thread_posts if p.get("reply_broadcast")]
    assert [p["text"] for p in broadcasts] == [text_by_name["m:202:header#1"]]
    asyncio.run(_granted_card(world.worker, led, world.root))
    assert len(world.client.thread_posts) == 6
