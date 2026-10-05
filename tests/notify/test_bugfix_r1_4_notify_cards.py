"""Regression tests for notify_cards fixes (revoked reuse, LINE WORKS
failed thread overflow, blank body chunks, JST digest thread name)."""
from __future__ import annotations

import time
from datetime import datetime

import pytest

import notify_cards
from adapters.lineworks import cards as lwcards
from mcs_queries import JST
from notify_testkit import CFG, NOW, _card, _dispatch, _intent, _msg, _seed_thread, led as led  # noqa: F401
from test_notify_lineworks import LINEWORKS, MID, _apply, _claim, _render, envelopes


def _revoke_thread_card(led):
    _seed_thread(led)
    _dispatch(led, _intent(led))
    led.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=1")
    led.db.commit()
    notify_cards.sweep(led, CFG)
    assert _card(led)["delivery_state"] == "revoked"
    _msg(led, 102, 1, parent=100)


def _event(led, event_id):
    ic = [dict(r) for r in led.db.execute(
        "SELECT card_id,delivery_id,state FROM notification_intent_cards "
        "WHERE event_id=?", (event_id,))]
    ob = led.db.execute("SELECT state FROM notify_outbox WHERE event_id=?",
                        (event_id,)).fetchone()["state"]
    return ic, ob


def test_reactivated_unit_gets_a_fresh_card(led, monkeypatch):
    monkeypatch.setattr(notify_cards.time, "time", lambda: NOW)
    _revoke_thread_card(led)
    led.db.execute("UPDATE patients SET is_archived=0 WHERE project_id=1")
    led.db.commit()
    ev = _intent(led, payload={"message_ids": [102]})
    assert _dispatch(led, ev)["dispatched"]
    ic, _ob = _event(led, ev["event_id"])
    assert ic[0]["card_id"] != 1 and ic[0]["delivery_id"] is not None
    assert _card(led, ic[0]["card_id"])["delivery_state"] != "revoked"
    assert _card(led)["delivery_state"] == "revoked"


def test_still_archived_unit_suppresses_new_intent(led, monkeypatch):
    monkeypatch.setattr(notify_cards.time, "time", lambda: NOW)
    _revoke_thread_card(led)
    ev = _intent(led, payload={"message_ids": [102]})
    _dispatch(led, ev)
    ic, ob = _event(led, ev["event_id"])
    assert ic == [{"card_id": 1, "delivery_id": None, "state": "suppressed"}]
    assert ob == "suppressed"


def test_lineworks_failed_thread_keeps_display_overflow(led, monkeypatch):
    monkeypatch.setattr(notify_cards.time, "time", lambda: NOW)
    original = notify_cards._card_content

    def long_display(*a, **k):
        c = original(*a, **k)
        c["containers"] = [{"type": "text", "text": "合成内容" * 400}]
        return c
    monkeypatch.setattr(notify_cards, "_card_content", long_display)
    claim = _claim(_render(led))
    lwcards.validate(claim["spec"])
    assert _apply(led, envelopes.transport_begin(claim))["granted"]
    assert _apply(led, envelopes.transport_receipt(
        claim, "delivered", message_id=MID))["applied"]
    thread = next(p for p in claim["spec"]["parts"]["manifest"]
                  if p["kind"] == "thread")
    assert _apply(led, envelopes.part_receipt(
        claim, thread, "not_sent", error_code="scope_mismatch"))["applied"]
    db = notify_cards._db(led)
    card = notify_cards._card_row(db, 1)
    assert card["thread_state"] == "failed"
    content = notify_cards._card_content(db, card)
    spec = notify_cards._build_spec(
        db, card, content, notify_cards._generation_drift(card, content),
        "update", 99, LINEWORKS, NOW)
    lwcards.validate(spec)


@pytest.mark.parametrize("text", [
    "a\n" + (" " * 1000 + "\n") * 3 + "b",
    "x" * 1000 + "\n" + "\n" * 3000 + "b",
    "a" * 1000 + "\n" + " " * 3900,
    "head\n" + "\n" * 4000 + "body",
    "x" * 1900 + " " * 1905,
    "　" * 2500 + "本文",
])
def test_body_chunks_have_no_blank_post(text):
    chunks = notify_cards._split_body_chunks(text)
    assert all(c.strip() for c in chunks)
    assert all(len(c) <= notify_cards.THREAD_PART_LIMIT for c in chunks)
    joined = "".join(chunks)
    assert joined.split() == text.split()      # only whitespace may drop


def test_body_chunks_mid_blank_run_stays_lossless():
    for text in ("a\n" + (" " * 1000 + "\n") * 3 + "b",
                 "x" * 1000 + "\n" + "\n" * 3000 + "b"):
        assert "".join(notify_cards._split_body_chunks(text)) == text


def test_digest_thread_name_uses_jst_day(monkeypatch):
    now = datetime(2026, 10, 5, 8, 0, tzinfo=JST).timestamp()
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        assert notify_cards._digest_thread_name(now).endswith("10-05")
    finally:
        monkeypatch.undo()
        time.tzset()
