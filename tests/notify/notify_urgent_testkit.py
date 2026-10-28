"""Shared synthetic urgent-escalation world for the notify tests — a
temp ledger with stubbed notify_flush send/config/clock, an initial
observation driver, urgency facts and the queued urgent notices.  Not a
test module."""
from __future__ import annotations

import json

import pytest

import notify_flush
import notify_sender
import notify_urgent
from inference_version import CURRENT_VERSION, VERSION_KEY
from self_identity import record_station_staff
from notify_testkit import (
    CFG, NOW, _begin, _dispatch, _extract, _latest_render, _msg, _part_receipt,
    _patient, _receipt, _settle_bodies, led,
)
from semantic_projection import PROJECTION_VERSION

__all__ = ["led", "world"]


@pytest.fixture
def world(led, monkeypatch):
    clock = [NOW]
    monkeypatch.setattr(notify_flush.time, "time", lambda: clock[0])
    monkeypatch.setattr(notify_flush.time, "monotonic", lambda: clock[0])
    cfg = {**CFG, "notify_target": "discord:1", "urgency_escalation": {
        "mode": "on", "source": "llm", "room_cooldown_min": 1}}
    monkeypatch.setattr(notify_flush, "_config", lambda: cfg)
    calls = []
    monkeypatch.setattr(notify_sender, 'send',
                        lambda *args, **kwargs: calls.append((args, kwargs)))
    _patient(led)
    _msg(led, 100, ts=int(NOW - 7200), body="本人が急変し至急確認が必要です。PRIVATE-BODY-CANARY")
    with led.db:
        led.db.execute("UPDATE messages SET sender_id=8 WHERE message_id=100")
        record_station_staff(led.db, [{"staff_id": 7, "is_self": True}])
    return led, cfg, clock, calls


def _base(store, *, mid=100, at=NOW - 1800, state="accepted", interactive=True):
    with store.db:
        event = store.outbox_add_tx("new_messages", 1, {"message_ids": [mid]},
                                   route="interactive" if interactive else "text")
    row = store.db.execute("SELECT * FROM notify_outbox WHERE event_id=?", (event,)).fetchone()
    with store.db:
        notify_urgent.capture_initial(
            store, {"urgency_escalation": {"mode": "shadow", "room_cooldown_min": 1}}, row, now=at)
    if interactive:
        assert _dispatch(store, row, now=at)["dispatched"]
        cid = store.db.execute("SELECT card_id FROM notification_intent_cards WHERE event_id=?", (event,)).fetchone()[0]
        render = _latest_render(store, cid)
        serial = 90000 + event * 3
        assert _begin(store, render, n=serial)["granted"]
        assert _receipt(store, render, f"{serial:016x}", message_id="m-9", n=serial + 1)["applied"]
        _settle_bodies(store, render)
        assert _part_receipt(store, render, "thread", remote_id="synthetic-thread-" + str(cid), n=serial + 2)["applied"]
    with store.db:
        store.db.execute("UPDATE notify_outbox SET state=?,created_at=?,updated_at=? "
                         "WHERE event_id=?", (state, at - 1, at, event))
    return event


def _fact(store, *, mid=100, urgency="high", at=NOW - 600, kind="extract_llm", **meta):
    body = store.db.execute("SELECT body_text FROM messages WHERE message_id=?", (mid,)).fetchone()[0]
    content = {"urgency": urgency}
    if urgency == "high" and body:
        content["urgency_evidence"] = [body.split("。", 1)[0]]
    if kind in ("canonical_projection", "semantic_facts_v4"):
        content_hash = store.db.execute("SELECT content_hash FROM messages WHERE message_id=?",
                                        (mid,)).fetchone()[0]
        store.artifact_add(kind, json.dumps(content), project_id=1, message_id=mid,
                           meta={VERSION_KEY: CURRENT_VERSION, "hash": content_hash, "engine_version": 4,
                                 "projection_version": PROJECTION_VERSION, **meta})
    else:
        _extract(store, mid, content, kind=kind, **meta)
    aid = store.db.execute("SELECT max(artifact_id) FROM artifacts").fetchone()[0]
    with store.db:
        store.db.execute("UPDATE artifacts SET created_at=? WHERE artifact_id=?", (at, aid))
    return aid


def _events(store):
    return store.db.execute("SELECT * FROM notify_outbox WHERE kind='urgent_notice' "
                            "ORDER BY event_id").fetchall()


def _alert(world, *, ready=True):
    """A queued urgent notice over a delivered card whose thread is
    ready (or not) — (store, cfg, event row, base event id)."""
    store, cfg, _, _ = world
    base = _base(store, interactive=True)
    with store.db:
        store.db.execute("UPDATE notification_cards SET thread_id='synthetic-thread',thread_state=?",
                         ("created" if ready else "none",))
    _fact(store)
    assert notify_urgent.maybe_enqueue(store, cfg)["queued"] == 1
    return store, cfg, _events(store)[0], base
