"""Pending card-owned intents record why they wait, and health can break
the pending population down by that reason without ever raising.
Synthetic ledger and stubbed delivery only."""
from __future__ import annotations

import json

import pytest

import notify_cards
from notify_testkit import CFG, NOW, _dispatch, _intent, _patient, _seed_thread, _signal_row, led

__all__ = ["led"]


def _row(led, event_id):
    return led.db.execute("SELECT * FROM notify_outbox WHERE event_id=?",
                          (event_id,)).fetchone()


def _wait(led, event_id):
    return json.loads(_row(led, event_id)["progress"])["wait"]


def _population_count(led, now):
    return led.db.execute(
        "SELECT COUNT(*) FROM notify_outbox "
        f"WHERE {notify_cards.OUTBOX_PENDING_WHERE}", (now,)).fetchone()[0]


def test_dispatch_records_issued_then_unsettled_with_the_row_update(led):
    _seed_thread(led)
    ev = _intent(led)
    _dispatch(led, ev)
    wait = _wait(led, ev["event_id"])
    assert wait["reason"] == "intent_cards_issued"
    assert wait["at"] == pytest.approx(_row(led, ev["event_id"])["updated_at"])
    later = NOW + notify_cards.RESEAT_S + 5
    _dispatch(led, ev, now=later)
    assert _wait(led, ev["event_id"])["reason"] == "intent_cards_unsettled"
    breakdown = notify_cards.pending_breakdown(led.db, later)
    assert breakdown["by_reason"]["intent_cards_unsettled"]["count"] == 1


def test_sealed_signal_with_signals_off_is_a_setting_hold(led):
    _patient(led, 1)
    _signal_row(led, "sig-r", pid=1)
    ev = _intent(led, kind="signal", pid=1,
                 payload={"signal_keys": ["sig-r"], "project_id": 1,
                          "type": "med_followup"})
    _dispatch(led, ev)
    cfg = {"notify": CFG["notify"], "signals": {"notify": False}}
    _dispatch(led, ev, cfg=cfg, now=NOW + notify_cards.RESEAT_S + 5)
    assert _wait(led, ev["event_id"])["reason"] == "signals_off_sealed"


@pytest.mark.parametrize("before, kept", [
    ('{"thread_hold": "source_thread_not_ready"}', {"thread_hold": "source_thread_not_ready"}),
    ("not json", {"invalid_progress": "not json"}),
    ("[1]", {"invalid_progress": "[1]"}),
])
def test_reseat_keeps_existing_progress(led, before, kept):
    eid = led.outbox_add("new_messages", 1, {"message_ids": [1]})
    led.db.execute("UPDATE notify_outbox SET progress=? WHERE event_id=?", (before, eid))
    notify_cards._reseat(led.db, eid, NOW, "intent_cards_unsettled")
    progress = json.loads(_row(led, eid)["progress"])
    assert {k: progress[k] for k in kept} == kept
    assert progress["wait"] == {"reason": "intent_cards_unsettled", "at": NOW}


def _pending(led, kind="new_messages", payload=None, *, progress=None,
             updated_at=None, created_at=NOW, next_try=NOW):
    eid = led.outbox_add(kind, 1, payload or {"message_ids": [1]})
    led.db.execute(
        "UPDATE notify_outbox SET progress=?,updated_at=?,created_at=?,next_try=? "
        "WHERE event_id=?",
        (progress, updated_at if updated_at is not None else created_at,
         created_at, next_try, eid))
    return eid


def test_breakdown_reasons_add_up_to_the_population(led):
    now = NOW + 60
    _pending(led)                                        # no record
    fresh = _pending(led)
    notify_cards._reseat(led.db, fresh, NOW, "alert_render_live")
    stale = _pending(led)
    notify_cards._reseat(led.db, stale, NOW, "alert_render_live")
    led.db.execute("UPDATE notify_outbox SET updated_at=? WHERE event_id=?",
                   (NOW + 30, stale))                    # a later writer
    _pending(led, progress="{bad json")
    _pending(led, progress=json.dumps({"hold_reason": "delivery_held"}))
    # a scheduled signal digest waits by design and is not in the population
    _pending(led, kind="signal", payload={"digest": True}, next_try=now + 3600)
    breakdown = notify_cards.pending_breakdown(led.db, now)
    reasons = {k: v["count"] for k, v in breakdown["by_reason"].items()}
    assert reasons == {"unrecorded": 1, "alert_render_live": 1, "stale": 1,
                       "unknown": 1, "hold_reason:delivery_held": 1}
    assert breakdown["total"] == sum(reasons.values()) == _population_count(led, now)
    # unknown and the delivery hold need an operator; the rest are waits
    assert breakdown["attention"] == 2


def test_unknown_card_delivery_outranks_the_recorded_wait(led):
    _seed_thread(led)
    ev = _intent(led)
    _dispatch(led, ev)
    led.db.execute(
        "UPDATE notification_cards SET delivery_state='delivery_unknown' "
        "WHERE card_id IN (SELECT card_id FROM notification_intent_cards WHERE event_id=?)",
        (ev["event_id"],))
    breakdown = notify_cards.pending_breakdown(led.db, NOW + 1)
    assert {k: v["count"] for k, v in breakdown["by_reason"].items()} == {
        "intent_card_delivery_unknown": 1}
    assert breakdown["attention"] == 1


def test_a_long_wait_needs_attention_even_by_design(led):
    eid = _pending(led, created_at=NOW - notify_cards.PENDING_ATTENTION_S)
    notify_cards._reseat(led.db, eid, NOW, "notice_render_issued")
    breakdown = notify_cards.pending_breakdown(led.db, NOW)
    assert breakdown["by_reason"]["notice_render_issued"]["count"] == 1
    assert breakdown["attention"] == 1
    assert breakdown["oldest_age_s"] == pytest.approx(notify_cards.PENDING_ATTENTION_S)


def test_empty_population(led):
    assert notify_cards.pending_breakdown(led.db, NOW) == {
        "total": 0, "oldest_age_s": None, "by_reason": {}, "attention": 0}


@pytest.mark.parametrize("progress, updated_at", [
    (json.dumps({"wait": {"reason": ["intent_cards_issued"], "at": NOW}}), NOW),
    (json.dumps({"wait": {"reason": "intent_cards_issued", "at": float("nan")}}), NOW),
    (json.dumps({"wait": {"reason": "intent_cards_issued", "at": 10 ** 400}}), NOW),
    (json.dumps({"wait": {"reason": "intent_cards_issued", "at": True}}), NOW),
    (json.dumps({"wait": {"reason": "not_a_code", "at": NOW}}), NOW),
    (json.dumps({"wait": "x"}), NOW),
])
def test_malformed_wait_records_are_unknown(led, progress, updated_at):
    _pending(led, progress=progress, updated_at=updated_at)
    breakdown = notify_cards.pending_breakdown(led.db, NOW + 1)
    assert {k: v["count"] for k, v in breakdown["by_reason"].items()} == {"unknown": 1}


def test_non_numeric_row_timestamps_do_not_raise(led):
    eid = _pending(led, progress=json.dumps({"wait": {"reason": "intent_cards_issued", "at": NOW}}))
    led.db.execute("UPDATE notify_outbox SET updated_at='x', created_at=NULL WHERE event_id=?", (eid,))
    breakdown = notify_cards.pending_breakdown(led.db, NOW + 1)
    assert breakdown["total"] == 1 and breakdown["by_reason"] == {
        "unknown": {"count": 1, "oldest_age_s": None}}
