"""Cardless urgent notices replace only provably unsent renders after route changes."""
import json

import pytest

import notify_cards
import notify_transport
from notify_testkit import _uuid
from test_alert_thread_hotfix import _alert
from test_notify_urgent import led, world

__all__ = ["led", "world"]


def _begin(store, render, cfg, serial):
    identity = {"version": 1, **notify_cards.delivery_scope(cfg),
                "delivery_id": render["delivery_id"], "render_rev": render["render_rev"],
                "payload_hash": render["payload_hash"], "route_epoch": render["route_epoch"],
                "correlation": render["correlation"]}
    outcome = notify_transport.apply_transport_begin(store, {
        **identity, "op": "transport_begin", "command_id": _uuid(serial),
        "attempt_id": f"{serial:016x}", "worker_id": "bb" * 8}, cfg)
    return outcome, identity


@pytest.mark.parametrize("attempt", ["none", "granted", "unknown"])
def test_urgent_route_epoch_rebinds_unsent_and_keeps_uncertain_attempt(world, attempt):
    store, cfg, event, _ = _alert(world)
    assert notify_cards.dispatch_intent(store, dict(event), cfg)["dispatched"]
    render = notify_cards._notice_renders(store.db, event["event_id"])[0]
    if attempt != "none":
        granted, identity = _begin(store, render, cfg, 7100)
        assert granted["granted"]
        if attempt == "unknown":
            assert notify_transport.apply_transport_receipt(store, {
                **identity, "op": "transport_receipt", "command_id": _uuid(7101),
                "attempt_id": f"{7100:016x}", "result": "unknown"}, cfg)["applied"]
    changed = {**cfg, "notify": {**cfg["notify"], "route_epoch": 2}}
    outcome = notify_cards.dispatch_intent(store, dict(event), changed)
    renders = notify_cards._notice_renders(store.db, event["event_id"])
    if attempt != "none":
        assert not outcome["dispatched"] and len(renders) == 1
        assert renders[0]["state"] == ("sending" if attempt == "granted" else "unknown")
        return
    assert outcome["dispatched"] and len(renders) == 2
    assert renders[0]["state"] == "cancelled"
    replacement = renders[1]
    assert replacement["state"] == "queued" and replacement["route_epoch"] == 2
    spec = json.loads(replacement["spec_json"])
    assert spec["parts"]["thread_notice"] is True
    assert spec["delivery"]["thread_id"] == "synthetic-thread"
    granted, identity = _begin(store, replacement, changed, 7200)
    assert granted["granted"]
    assert notify_transport.apply_transport_receipt(store, {
        **identity, "op": "transport_receipt", "command_id": _uuid(7201),
        "attempt_id": f"{7200:016x}", "result": "delivered", "message_id": "synthetic-alert-reply"}, changed)["applied"]
    assert notify_cards.dispatch_intent(store, dict(event), changed)["skipped"]
    assert len(notify_cards._notice_renders(store.db, event["event_id"])) == 2
