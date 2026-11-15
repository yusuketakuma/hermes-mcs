"""Card intent acceptance is first-time only, and a busy local sender lock
never spends the MAX_RESEND budget. Synthetic temp ledger only."""
from __future__ import annotations

import notify_card_state as cs
from common_testkit import ledger_fixture

led = ledger_fixture()


def _card(db, card_id=1):
    db.execute(
        "INSERT INTO notification_cards(card_id,card_key,kind,project_id,"
        "anchor_key,created_at,updated_at) VALUES(?,?,'thread',1,'a',0,0)",
        (card_id, f"k{card_id}"))


def test_reaccept_keeps_first_accept_time(led, monkeypatch):
    db = led.db
    _card(db)
    db.execute("INSERT INTO notify_outbox(event_id,kind,project_id,state,"
               "created_at,updated_at) VALUES(1,'new_messages',1,'pending',0,0)")
    db.execute("INSERT INTO notification_intent_cards(event_id,card_id,"
               "coverage,state) VALUES(1,1,'full','delivered')")
    calls = []
    import attachment_followups
    monkeypatch.setattr(attachment_followups,
                        "enqueue_ready_attachment_followups_tx",
                        lambda _db, ev, now: calls.append(ev) or 0)
    assert cs.complete_intent(db, 1, 100.0) == "accepted"
    assert cs.complete_intent(db, 1, 5000.0) == "accepted"
    row = db.execute("SELECT state,updated_at FROM notify_outbox "
                     "WHERE event_id=1").fetchone()
    assert (row["state"], row["updated_at"]) == ("accepted", 100.0)
    # followups stay re-examined on every settle (enqueue dedups)
    assert calls == [1, 1]


def test_sender_busy_not_sents_do_not_exhaust_resend(led):
    db = led.db
    _card(db)
    for i in range(1, cs.MAX_RESEND + 2):
        db.execute(
            "INSERT INTO notification_renders(delivery_id,card_id,op,"
            "render_rev,route_epoch,payload_hash,correlation,state,"
            "created_at,updated_at) VALUES(?,1,'update',?,1,'h',?,"
            "'not_sent',0,0)", (f"d{i}", i, f"c{i}"))
        db.execute(
            "INSERT INTO notification_delivery_attempts(attempt_id,"
            "delivery_id,begin_command_id,state,error_code,created_at) "
            "VALUES(?,?,?,'not_sent','sender_busy',0)",
            (f"a{i}", f"d{i}", f"b{i}"))
    assert not cs.resend_exhausted(db, 1)
    db.execute("UPDATE notification_delivery_attempts SET error_code=NULL")
    assert cs.resend_exhausted(db, 1)
