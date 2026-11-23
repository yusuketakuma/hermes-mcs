"""Committed dispatch outcomes, archived in-flight sends and restore holds
on retrying intents keep their delivery truth (synthetic ledger)."""
import json

import notify_cards as cards
import notify_flush
import notify_reconcile
import notify_scope
import notify_spool as spool
import notify_transport
from notify_testkit import CFG, _begin, _msg, _patient, _uuid, led
from notify_urgent_testkit import _alert, world
import notify_reconcile_testkit as rk
import ledger as ledger_mod

__all__ = ["led", "world"]


def test_publication_failure_after_commit_keeps_committed_suppression(led, monkeypatch):
    events = []
    for i in range(3):
        _patient(led, i + 1)
        _msg(led, 100 + i, pid=i + 1)
        events.append(led.outbox_add("new_messages", i + 1, {"message_ids": [100 + i]}))
    original = cards._dispatch_intent_tx

    def dispatch(ledger, event, cfg, now, specs):
        if event["event_id"] == events[0]:
            ledger.db.execute("UPDATE notify_outbox SET state='suppressed',next_try=NULL "
                              "WHERE event_id=?", (events[0],))
            return {"suppressed": True}
        return original(ledger, event, cfg, now, specs)

    def fail(*args):
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(cards, "_dispatch_intent_tx", dispatch)
    monkeypatch.setattr(spool, "publish_file", fail)
    monkeypatch.setattr(notify_flush, "_config", lambda: CFG)
    monkeypatch.setattr(notify_flush.mcs_util, "hermes_exe", lambda cfg: "/synthetic/missing-hermes")
    result = notify_flush.flush(led)
    assert result["publish_failed"] == 3 and result["failed"] == 0
    rows = {r["event_id"]: r for r in led.db.execute(
        "SELECT event_id,state,attempts FROM notify_outbox").fetchall()}
    assert tuple(rows[events[0]][1:]) == ("suppressed", 0)
    assert all(tuple(rows[e][1:]) == ("pending", 0) for e in events[1:])
    assert led.db.execute("SELECT COUNT(*) FROM notification_renders WHERE spec_published=0").fetchone()[0] == 2


def test_single_dispatch_publication_failure_after_commit_is_not_an_attempt(world, monkeypatch):
    store, cfg, event, _ = _alert(world)
    with store.db:
        store.db.execute("UPDATE notify_outbox SET attempts=4,next_try=0 WHERE event_id=?", (event["event_id"],))

    def fail(*args):
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(spool, "publish_file", fail)
    result = notify_flush.flush(store)
    assert result["publish_failed"] == 1 and result["failed"] == 0
    row = store.db.execute("SELECT state,attempts,progress FROM notify_outbox WHERE event_id=?",
                           (event["event_id"],)).fetchone()
    assert (row["state"], row["attempts"]) != ("failed", 5)
    assert "internal_failure" not in (row["progress"] or "")
    assert store.db.execute("SELECT COUNT(*) FROM notification_renders WHERE spec_published=0").fetchone()[0] >= 1


def test_single_dispatch_failure_before_commit_still_counts(world, monkeypatch):
    store, cfg, event, _ = _alert(world)

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic pre-commit failure")

    monkeypatch.setattr(cards, "dispatch_intent", fail)
    result = notify_flush.flush(store)
    assert result["failed"] == 1 and result.get("publish_failed", 0) == 0
    row = store.db.execute("SELECT state,attempts FROM notify_outbox WHERE event_id=?",
                           (event["event_id"],)).fetchone()
    assert row["state"] == "failed"


def test_archived_urgent_notice_with_sending_render_is_held_not_suppressed(world):
    store, cfg, event, _ = _alert(world)
    assert cards.dispatch_intent(store, dict(event), cfg)["dispatched"]
    render = cards._notice_renders(store.db, event["event_id"])[0]
    assert _begin(store, render, n=1200, cfg=cfg)["granted"]
    with store.db:
        store.db.execute("UPDATE notify_outbox SET next_try=0 WHERE event_id=?", (event["event_id"],))
        store.db.execute("UPDATE patients SET is_archived=1")
    result = notify_flush.flush(store)
    assert result["uncertain"] == 1 and result["suppressed"] == 0
    row = store.db.execute("SELECT state,next_try,progress FROM notify_outbox WHERE event_id=?",
                           (event["event_id"],)).fetchone()
    assert (row["state"], row["next_try"]) == ("failed", None)
    assert json.loads(row["progress"])["hold_reason"] == "send_outcome_unknown"
    req = {"version": 1, "op": "transport_receipt", "command_id": _uuid(1201),
           "attempt_id": f"{1200:016x}", "delivery_id": render["delivery_id"],
           "render_rev": render["render_rev"], "payload_hash": render["payload_hash"],
           "route_epoch": 1, "correlation": render["correlation"],
           **notify_scope.delivery_scope(cfg), "result": "delivered", "message_id": "alert-reply"}
    assert notify_transport.apply_transport_receipt(store, req, cfg)["applied"]
    assert store.db.execute("SELECT state FROM notify_outbox WHERE event_id=?",
                            (event["event_id"],)).fetchone()[0] == "accepted"


def test_restore_hold_covers_failed_intent_with_retry_timer(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    spool.ensure_dirs(str(data))
    store = ledger_mod.Ledger(str(data / "ledger.db"))
    try:
        _patient(store)
        for mid in (100, 101):
            _msg(store, mid)
        eid = store.outbox_add("new_messages", 1, {"message_ids": [100, 101]})
        ev = dict(store.db.execute("SELECT * FROM notify_outbox WHERE event_id=?", (eid,)).fetchone())
        assert cards.dispatch_intent(store, ev, rk.CFG, now=rk.NOW)["dispatched"]
        render = store.db.execute("SELECT delivery_id FROM notification_renders").fetchone()
        dirs = spool.notify_dirs(str(data))
        with store.db:   # a dispatch error left it retrying an hour later
            store.db.execute("UPDATE notify_outbox SET state='failed',next_try=? WHERE event_id=?",
                             (rk.NOW + 3600, eid))
        notify_reconcile._apply_hold(store.db, None, render["delivery_id"], "attempt_lost:delivered",
                                     dirs, rk.NOW)
        row = store.db.execute("SELECT state,next_try,progress FROM notify_outbox WHERE event_id=?",
                               (eid,)).fetchone()
        assert (row["state"], row["next_try"]) == ("failed", None)
        assert json.loads(row["progress"])["hold_reason"] == "restore_hold"
    finally:
        store.close()
