"""Only files actually covered by accepted cards reach the attachment outbox."""
import json

import pytest

import ledger
import notify_cards
from test_notify_cards import (
    NOW, _dispatch, _intent, _msg, _seed_thread,
)


@pytest.fixture
def led(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    instance = ledger.Ledger(str(data / "ledger.db"))
    yield instance
    instance.close()


@pytest.mark.parametrize("download_after", [False, True])
def test_card_attachment_scope_uses_frozen_delivered_coverage(
        led, tmp_path, monkeypatch, download_after):
    _seed_thread(led, mids=(100,))
    _msg(led, 200)
    _msg(led, 300)
    event = _intent(led, payload={"message_ids": [100, 200]})
    _dispatch(led, event)
    with led.db:
        led.db.execute(
            "UPDATE notification_intent_cards SET state=CASE "
            "WHEN card_id=(SELECT card_id FROM notification_cards "
            "WHERE root_message_id=100) THEN 'delivered' ELSE 'suppressed' END")
        # The accepted intent was sealed before this mutable row changed.
        led.db.execute("UPDATE notify_outbox SET payload=? WHERE event_id=?",
                       (json.dumps({"message_ids": [300]}), event["event_id"]))
        for mid in (100, 200, 300):
            led.db.execute(
                "INSERT INTO attachments(message_id,file_id,state,local_path) "
                "VALUES(?,?,?,?)", (mid, f"file-{mid}",
                                   "pending" if download_after else "downloaded",
                                   str(tmp_path / f"synthetic-{mid}.txt")))
        assert notify_cards._complete_intent(led.db, event["event_id"], NOW) == "accepted"
    if download_after:
        monkeypatch.setattr(ledger.time, "time", lambda: NOW + 1)
        for row in led.db.execute("SELECT * FROM attachments").fetchall():
            led.attachment_saved(row["attachment_id"], row["local_path"], 1, "synthetic")
    rows = led.db.execute(
        "SELECT payload FROM notify_outbox WHERE kind='attachment_followup'").fetchall()
    assert [json.loads(row["payload"])["message_id"] for row in rows] == [100]
