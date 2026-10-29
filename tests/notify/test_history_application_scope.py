"""A synthetic Bot change cannot borrow another application's history receipts."""
import json

import pytest

import initial_import_state
import notify_history
from notify_testkit import NOW, _card, _dispatch, led, pinned_clock
from notify_lineworks_testkit import LINEWORKS, SCOPE
from test_patient_history_cards import seed, ready

__all__ = ["led", "pinned_clock"]
pytestmark = pytest.mark.usefixtures("pinned_clock")


@pytest.mark.parametrize("phase", ["analyzing", "complete"])
def test_new_application_starts_its_own_history(led, monkeypatch, phase):
    event = seed(led)
    ready(monkeypatch, False)
    _dispatch(led, event, LINEWORKS)
    card = _card(led)
    led.db.execute("UPDATE notification_cards SET delivery_state='delivered',ui_state=? WHERE card_id=?",
                   (json.dumps({"history_latest": {"phase": phase}}), card["card_id"]))
    payload = json.loads(event["payload"])
    current = notify_history.targets(led.db, 1, payload, "lineworks", scope=SCOPE)
    assert (current[0]["anchor"]["phase"] if current else None) == ("complete" if phase == "complete" else None)
    changed = notify_history.targets(led.db, 1, payload, "lineworks",
                                    scope={**SCOPE, "application_id": "2000002"})
    assert changed[0]["anchor"]["phase"] == "start"


def test_old_application_completion_does_not_suppress_new_application(led, monkeypatch):
    event = seed(led)
    ready(monkeypatch, False)
    _dispatch(led, event, LINEWORKS)
    card = dict(_card(led))
    anchor = json.loads(card["anchor_key"])
    led.db.execute("UPDATE notification_cards SET anchor_key=? WHERE card_id=?",
                   (json.dumps({**anchor, "phase": "complete"}), card["card_id"]))
    led.db.execute("UPDATE notify_outbox SET state='delivered'")
    card.update(application_id="2000002", delivery_state="delivered",
                ui_state=json.dumps({"history_latest": {"phase": "complete"}}))
    notify_history.emit_completion_tx(led, card, NOW + 301)
    assert led.db.execute("SELECT COUNT(*) FROM notify_outbox WHERE state='pending'").fetchone()[0] == 1
    # The same intent remains idempotent until the new Bot dispatches it.
    notify_history.emit_completion_tx(led, card, NOW + 302)
    assert led.db.execute("SELECT COUNT(*) FROM notify_outbox WHERE state='pending'").fetchone()[0] == 1
    assert initial_import_state.load(led.db, 1)["cohort_id"] == anchor["cohort_id"]
