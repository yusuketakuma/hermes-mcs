"""Discord reply cards: a later reply gets its own card inside the root card's
companion thread (a separate channel id). Synthetic ledger only."""
import json

import notify_cards
from notify_testkit import CFG, NOW, _dispatch, _intent, _msg, _seed_thread, led

__all__ = ["led"]

REPLY = {**CFG, "notify": {**CFG["notify"], "reply_cards": True}}


def _cards(led):
    return [dict(r) for r in led.db.execute("SELECT * FROM notification_cards ORDER BY card_id")]


def test_discord_reply_card_waits_for_and_then_uses_the_root_thread(led, monkeypatch):
    clock = [NOW]
    monkeypatch.setattr(notify_cards.time, "time", lambda: clock[0])
    _seed_thread(led, mids=(100,))
    assert _dispatch(led, _intent(led), REPLY, now=clock[0])["dispatched"]
    (root,) = _cards(led)
    assert root["layout"] == 4

    clock[0] += 60
    _msg(led, 202, parent=100, body="合成の返信", ts=int(clock[0]))
    _dispatch(led, _intent(led, payload={"message_ids": [202]}), REPLY, now=clock[0])
    root, reply = _cards(led)
    assert reply["card_key"].endswith("|reply:202") and reply["thread_id"] is None
    assert led.db.execute("SELECT COUNT(*) FROM notification_renders WHERE card_id=?",
                          (reply["card_id"],)).fetchone()[0] == 0

    # the root card is delivered with its companion thread (its own channel id)
    with led.db:
        led.db.execute("UPDATE notification_cards SET delivery_state='delivered',message_id='m-root',"
                       "thread_id='th-root',thread_state='created' WHERE card_id=?", (root["card_id"],))
        specs = []
        notify_cards.issue_render(led.db, reply["card_id"], REPLY, clock[0] + 1, specs)
    (spec,) = specs
    assert spec["parts"]["source_thread"] is True
    assert spec["delivery"]["thread_id"] == "th-root"
    assert json.loads(_cards(led)[1]["anchor_key"]) == {"root": 100, "source": 202}


def test_lineworks_keeps_collecting_replies():
    lw = {**REPLY, "notify": {**REPLY["notify"], "interactive": "lineworks"}}
    assert notify_cards.reply_cards_on(lw) is False
