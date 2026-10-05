"""Sealed Slack intents survive the kill switch; a queued Slack notice is
re-aimed after a profile change. Synthetic temp ledger only."""
from __future__ import annotations

import notify_cards
import notify_digest
from notify_testkit import NOW, _dispatch, _intent, _patient, _seed_thread, led
from slack_testkit import SLACK

__all__ = ["led"]


def test_sealed_slack_intent_reseats_with_kill_switch_off(led):
    _seed_thread(led)
    ev = _intent(led)
    assert _dispatch(led, ev, SLACK)["dispatched"]
    off = {**SLACK, "notify": {**SLACK["notify"], "interactive": "off"}}
    out = _dispatch(led, ev, off)
    assert "error" not in out and out.get("resealed")


def test_profile_change_reissues_queued_slack_notice(led):
    cfg = {**SLACK, "daily_digest": {"enabled": True, "hour_jst": 0}}
    _patient(led, 1, name="患者A")
    led.db.commit()
    assert notify_digest.maybe_enqueue(led, cfg, now=NOW) == 1
    ev = led.db.execute("SELECT * FROM notify_outbox WHERE kind="
                        "'daily_digest'").fetchone()
    notify_cards.dispatch_intent(led, dict(ev), cfg, now=NOW)
    moved = {**cfg, "notify": {**cfg["notify"], "slack": {
        **cfg["notify"]["slack"], "profile": "other"}}}
    notify_cards.dispatch_intent(led, dict(ev), moved, now=NOW)
    first, second = notify_cards._notice_renders(led.db, ev["event_id"])
    assert (first["state"], second["state"]) == ("cancelled", "queued")
    assert second["profile"] == "other"
