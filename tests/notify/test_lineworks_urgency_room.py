"""LINE WORKS urgency re-check notices are room posts headed with the patient (synthetic)."""
import json

import mcs_setup
import notify_cards
import notify_render
import notify_transport
from adapters.common import envelopes
from adapters.lineworks import cards as lineworks_cards
from notify_testkit import NOW
from notify_urgent_testkit import _alert, _events, led, world

__all__ = ["led", "world"]

LW_SCOPE = {"profile": "mcs", "application_id": "123", "team_id": "456", "channel_id": "789"}
POLICY = {"mode": "on", "room_cooldown_min": 60}
BASE = {"mcs_login_id": "synthetic", "notify_target": "local"}


def _lineworks(cfg):
    return {**cfg, "notify": {"interactive": "lineworks", "route_epoch": 1, "lineworks": LW_SCOPE}}


def _mode_on_errors(notify):
    errors, _ = mcs_setup.validate_config({**BASE, "urgency_escalation": POLICY, "notify": notify})
    return [e for e in errors if e.startswith('urgency_escalation.mode "on"')]


def test_mode_on_accepts_lineworks_without_card_thread():
    lw = {"allowed_user_ids": ["operator"], **LW_SCOPE}
    for card_thread in ({}, {"card_thread": False}, {"card_thread": True}):
        assert _mode_on_errors({"interactive": "lineworks", **card_thread, "lineworks": lw}) == []


def test_mode_on_still_needs_card_threads_on_slack_and_discord():
    slack = {"interactive": "slack", "slack": {"profile": "default", "application_id": "A1234567890",
                                               "team_id": "T1234567890", "channel_id": "C1234567890"}}
    assert _mode_on_errors({**slack, "card_thread": True}) == []
    assert _mode_on_errors({**slack, "card_thread": False})
    assert _mode_on_errors({"interactive": "off"})


def test_lineworks_notice_is_a_room_post_headed_with_the_patient(world):
    store, cfg, event, _ = _alert(world)
    lw = _lineworks(cfg)
    assert notify_cards.dispatch_intent(store, dict(event), lw)["dispatched"]
    assert "thread_hold" not in (_events(store)[0]["progress"] or "")
    [render] = notify_cards._notice_renders(store.db, event["event_id"])
    spec = json.loads(render["spec_json"])
    lineworks_cards.validate(spec)   # also runs the shared display checks
    assert "thread_notice" not in spec["parts"]
    assert spec["delivery"]["transport"] == "lineworks"
    assert "thread_id" not in spec["delivery"] and "message_id" not in spec["delivery"]
    head, alarm = spec["parts"]["containers"][:2]
    assert head["type"] == "heading"
    assert head["text"].startswith(notify_render.patient_heading(store.db, 1) + " · ")
    assert alarm == {"type": "text", "rule": True, "text": "🚨 緊急度高・再確認候補"}
    assert spec["parts"]["preview_text"].startswith("🚨 緊急度高 ")
    # a re-entry while the render is live never seals a second post
    assert not notify_cards.dispatch_intent(store, dict(event), lw)["dispatched"]
    assert len(notify_cards._notice_renders(store.db, event["event_id"])) == 1


def test_lineworks_room_notice_is_granted_by_the_runner(world):
    # needs notify_transport._begin_check to accept a room notice without thread_notice
    store, cfg, event, _ = _alert(world)
    lw = _lineworks(cfg)
    assert notify_cards.dispatch_intent(store, dict(event), lw)["dispatched"]
    [render] = notify_cards._notice_renders(store.db, event["event_id"])
    claim = {"spec": json.loads(render["spec_json"]), "attempt_id": "a" * 16,
             "worker_id": "b" * 16, "payload_hash": render["payload_hash"]}
    got = notify_transport.apply_transport_begin(store, envelopes.transport_begin(claim), lw, now=NOW)
    assert got["granted"], got


def test_discord_notice_stays_in_the_source_thread_with_the_alert_heading(world):
    store, cfg, event, _ = _alert(world)
    assert notify_cards.dispatch_intent(store, dict(event), cfg)["dispatched"]
    spec = json.loads(notify_cards._notice_renders(store.db, event["event_id"])[0]["spec_json"])
    assert spec["parts"]["thread_notice"] is True
    assert spec["delivery"]["thread_id"] == "synthetic-thread"
    assert spec["parts"]["containers"][0] == {"type": "heading", "text": "🚨 緊急度高・再確認候補"}


def test_discord_without_a_thread_still_waits_and_never_posts_to_the_channel(world):
    store, cfg, event, _ = _alert(world, ready=False)
    assert notify_cards.dispatch_intent(store, dict(event), cfg)["parked"] == "source_thread_not_ready"
    assert notify_cards._notice_renders(store.db, event["event_id"]) == []
