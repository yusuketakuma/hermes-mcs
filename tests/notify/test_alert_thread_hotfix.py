"""Thread-only alerts reuse trusted receipts; no native service or patient data."""
import json

import pytest

import notify_cards
import notify_flush
import notify_transport
import notify_urgent
from adapters.common import spec as display_spec
from notify_testkit import _begin, _uuid
from test_notify_urgent import _base, _events, _fact, led, world

__all__ = ["led", "world"]


def _alert(world, *, ready=True):
    store, cfg, _, _ = world
    base = _base(store, interactive=True)
    with store.db:
        store.db.execute("UPDATE notification_cards SET thread_id='synthetic-thread',thread_state=?",
                         ("created" if ready else "none",))
    _fact(store)
    assert notify_urgent.maybe_enqueue(store, cfg)["queued"] == 1
    return store, cfg, _events(store)[0], base


def test_alert_seals_only_delivered_original_thread_and_receipt_is_idempotent(world):
    store, cfg, event, _ = _alert(world)
    assert notify_cards.dispatch_intent(store, dict(event), cfg)["dispatched"]
    render = notify_cards._notice_renders(store.db, event["event_id"])[0]
    spec = json.loads(render["spec_json"])
    display_spec.validate(spec)
    assert spec["parts"]["thread_notice"] is True and spec["parts"]["action_rows"] == []
    assert spec["delivery"]["thread_id"] == "synthetic-thread"
    assert spec["delivery"]["message_id"] == "m-9"
    assert _begin(store, render, n=1100, cfg=cfg)["granted"]
    req = {"version": 1, "op": "transport_receipt", "command_id": _uuid(1101),
           "attempt_id": f"{1100:016x}", "delivery_id": render["delivery_id"],
           "render_rev": render["render_rev"], "payload_hash": render["payload_hash"],
           "route_epoch": 1, "correlation": render["correlation"],
           **notify_cards.delivery_scope(cfg), "result": "delivered", "message_id": "alert-reply"}
    assert notify_transport.apply_transport_receipt(store, req, cfg)["applied"]
    assert notify_transport.apply_transport_receipt(store, req, cfg)
    assert _events(store)[0]["state"] == "accepted"
    assert notify_cards.dispatch_intent(store, dict(event), cfg)["skipped"]
    assert len(notify_cards._notice_renders(store.db, event["event_id"])) == 1


@pytest.mark.parametrize("fault", ["thread_not_ready", "deleted", "scope", "coverage"])
def test_unverified_thread_is_parked_and_never_sent_to_channel(world, fault):
    store, cfg, event, _ = _alert(world, ready=fault != "thread_not_ready")
    with store.db:
        if fault == "deleted":
            store.db.execute("UPDATE notification_cards SET thread_state='deleted'")
        elif fault == "scope":
            store.db.execute("UPDATE notification_cards SET channel_id='other-channel'")
        elif fault == "coverage":
            store.db.execute("UPDATE notification_intent_cards SET coverage='[]'")
    result = notify_flush.flush(store)
    assert result["suppressed" if fault == "coverage" else "parked"] == 1 and not world[3]
    assert notify_cards._notice_renders(store.db, event["event_id"]) == []
    if fault != "coverage":
        assert json.loads(_events(store)[0]["progress"])["thread_hold"] == "source_thread_not_ready"


def test_thread_receipt_later_resumes_same_alert_without_new_top_level_post(world):
    store, cfg, event, _ = _alert(world, ready=False)
    assert notify_cards.dispatch_intent(store, dict(event), cfg)["parked"] == "source_thread_not_ready"
    with store.db:
        store.db.execute("UPDATE notification_cards SET thread_state='created'")
    assert notify_cards.dispatch_intent(store, dict(event), cfg)["dispatched"]
    assert len(notify_cards._notice_renders(store.db, event["event_id"])) == 1


@pytest.mark.parametrize("fault", ["source", "thread"])
def test_runner_grant_rechecks_source_and_thread_binding(world, fault):
    store, cfg, event, _ = _alert(world)
    assert notify_cards.dispatch_intent(store, dict(event), cfg)["dispatched"]
    render = notify_cards._notice_renders(store.db, event["event_id"])[0]
    with store.db:
        if fault == "source":
            store.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=100")
        else:
            store.db.execute("UPDATE notification_cards SET thread_id='another-thread'")
    result = _begin(store, render, n=1100, cfg=cfg)
    assert not result["granted"]
    assert result["error"] == ("denied_urgent_source_changed" if fault == "source" else "denied_urgent_thread_changed")


def test_old_text_route_never_begun_migrates_to_thread_without_channel_post(world):
    store, cfg, event, _ = _alert(world)
    with store.db:
        store.db.execute("UPDATE notify_outbox SET route='text' WHERE event_id=?", (event["event_id"],))
    assert notify_flush.flush(store).get("dispatched") == 1 and not world[3]
    assert _events(store)[0]["route"] == "interactive"


def test_unknown_legacy_channel_send_is_held_and_never_duplicates_as_thread_post(world):
    store, cfg, event, _ = _alert(world)
    with store.db:
        store.db.execute("UPDATE notify_outbox SET route='text',progress=? WHERE event_id=?",
                         (json.dumps({"sending": 0}), event["event_id"]))
    assert notify_flush.flush(store)["failed"] == 1 and not world[3]
    assert notify_cards._notice_renders(store.db, event["event_id"]) == []
    assert _events(store)[0]["next_try"] is None


def test_lineworks_has_explicit_hold_instead_of_pseudo_thread_or_channel_fallback(world):
    store, cfg, event, _ = _alert(world)
    lw = {**cfg, "notify": {"interactive": "lineworks", "route_epoch": 1,
          "lineworks": {"profile": "mcs", "application_id": "123", "team_id": "456", "channel_id": "789"}}}
    outcome = notify_cards.dispatch_intent(store, dict(event), lw)
    assert outcome == {"held": "lineworks_thread_unsupported"}
    held = _events(store)[0]
    assert held["state"] == "failed" and held["next_try"] is None
    assert "lineworks_thread_unsupported" in held["progress"]
    assert not world[3]
