"""An urgent alert whose send is already granted/unknown must never be closed
as suppressed by the thread-hold expiry (unknown != never sent). Synthetic."""
import notify_cards
import notify_scope
import notify_transport
from notify_testkit import _begin, _uuid
from notify_urgent_testkit import _alert, _events, led, world

__all__ = ["led", "world"]


def test_in_flight_alert_is_not_suppressed_when_thread_binding_lapses(world):
    store, cfg, event, _ = _alert(world)
    assert notify_cards.dispatch_intent(store, dict(event), cfg)["dispatched"]
    render = notify_cards._notice_renders(store.db, event["event_id"])[0]
    assert _begin(store, render, n=1100, cfg=cfg)["granted"]       # the send is on the wire
    with store.db:                                                 # the card's thread is lost meanwhile
        store.db.execute("UPDATE notification_cards SET thread_state='deleted'")
    late = event["created_at"] + notify_cards.SIGNAL_SOURCE_HOLD_MAX_S + 1
    outcome = notify_cards.dispatch_intent(store, dict(event), cfg, now=late)
    assert "suppressed" not in outcome, outcome
    # the in-flight send then lands: the outbox must record the delivery
    req = {"version": 1, "op": "transport_receipt", "command_id": _uuid(1101),
           "attempt_id": f"{1100:016x}", "delivery_id": render["delivery_id"],
           "render_rev": render["render_rev"], "payload_hash": render["payload_hash"],
           "route_epoch": 1, "correlation": render["correlation"],
           **notify_scope.delivery_scope(cfg), "result": "delivered", "message_id": "alert-reply"}
    assert notify_transport.apply_transport_receipt(store, req, cfg)["applied"]
    assert _events(store)[0]["state"] == "accepted"
