"""Late synthetic replies remain in their original Slack thread."""
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
def test_delayed_reply_is_delivered_once_in_original_thread(led, monkeypatch, legacy):
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

    # Both layouts keep late replies inside the verified parent thread.
    posts = world.client.thread_posts[before:]
    expected = 3 if legacy else 1
    assert len(posts) == expected
    assert all(p["thread_ts"] == original_ts for p in posts)
    assert not any(p.get("reply_broadcast") for p in posts)
    root = _card(led)
    reply = led.db.execute("SELECT * FROM notification_cards WHERE card_key LIKE '%|reply:202'").fetchone()
    assert root["message_id"] == original_ts and reply["thread_id"] == original_ts
    assert reply["message_id"] != original_ts and reply["delivery_state"] == "delivered"
    current = json.loads(_latest_render(led, reply["card_id"])["spec_json"])
    root_spec = json.loads(_latest_render(led, root["card_id"])["spec_json"])
    assert "合成の遅れて届いた返信" not in json.dumps(root_spec["parts"]["containers"], ensure_ascii=False)
    body_parts = [p for p in current["parts"]["manifest"] if p["kind"] == "body_part"]
    chunks = current["parts"].get("thread_body_parts") or []
    by_name = {p["name"]: text for p, text in zip(body_parts, chunks, strict=True)}
    if legacy:
        assert sorted(by_name) == ["m:202#1", "m:202:header#1"]
        assert by_name["m:202#1"] == "合成の遅れて届いた返信"
        assert "合成の遅れて届いた返信" not in by_name["m:202:header#1"]
        assert sum(p.get("text") == "合成の遅れて届いた返信" for p in posts) == 1
        assert not current["parts"].get("slack", {}).get("face_posts")
    else:
        assert not body_parts
        assert [p["original"] for p in current["parts"]["slack"]["face_posts"]] == ["合成の遅れて届いた返信"]
    cards = led.db.execute("SELECT card_key FROM notification_cards ORDER BY card_id").fetchall()
    assert len(cards) == 3 and any(row["card_key"].endswith("|reply:101") for row in cards)
    assert any(row["card_key"].endswith("|reply:202") for row in cards)
    assert led.db.execute("SELECT state FROM notify_outbox WHERE event_id=?",
                          (intent["event_id"],)).fetchone()[0] == "accepted"

    _dispatch(led, intent, SLACK, now=clock[0])
    asyncio.run(_granted_card(world.worker, led, world.root))
    assert len(world.client.thread_posts) == before + expected

    # Re-rendering accepted content is an edit, never a fresh notification.
    issued = []
    with led.db:
        led.db.execute("UPDATE messages SET body_text='合成の編集後返信',content_hash=? WHERE message_id=202", ("e" * 64,))
        notify_cards.issue_render(led.db, reply["card_id"], SLACK, clock[0] + 1, issued, force=True)
    notify_spool.publish_specs(led.db, notify_spool.notify_dirs(str(world.root)), issued, clock[0] + 1)
    asyncio.run(_granted_card(world.worker, led, world.root))
    assert len(world.client.thread_posts) == before + expected
    assert not any(p.get("reply_broadcast") for p in world.client.thread_posts[before:])


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
    assert text_by_name["m:100#1"] == "GROWN_OLD_ORIGINAL" * 5
    assert all(not name.startswith("m:202") for name in text_by_name)
    assert sum(p.get("text") == text_by_name["m:100#1"] for p in world.client.thread_posts) == 1
    reply = led.db.execute("SELECT * FROM notification_cards WHERE card_key LIKE '%|reply:202'").fetchone()
    assert reply is not None and reply["thread_id"] == _card(led)["message_id"]
    assert reply["delivery_state"] == "delivered"
    reply_spec = json.loads(_latest_render(led, reply["card_id"])["spec_json"])
    assert [p["original"] for p in reply_spec["parts"]["slack"]["face_posts"]] == ["LATE_NEW_REPLY"]
    assert "LATE_NEW_REPLY" not in json.dumps(spec["parts"]["containers"], ensure_ascii=False)
    assert not any(p.get("reply_broadcast") for p in world.client.thread_posts)
    delivered = len(world.client.thread_posts)
    asyncio.run(_granted_card(world.worker, led, world.root))
    assert len(world.client.thread_posts) == delivered
