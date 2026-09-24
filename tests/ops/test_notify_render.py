"""Signal-card evidence changes must invalidate previously rendered actions."""

import json

import pytest

import notify_cards
from test_notify_cards import (
    CFG, NOW, ORIGIN, _begin, _dispatch, _intent, _latest_render,
    _msg, _notif, _patient, _receipt, _signal_row, _token_for, led,
)

__all__ = ["led"]  # shared isolated-ledger fixture


@pytest.mark.parametrize("change", ["edit", "delete"])
def test_signal_evidence_change_invalidates_old_action(led, change):
    _patient(led)
    _msg(led, 100)
    _signal_row(led, "synthetic-signal", mids=[100])
    _dispatch(led, _intent(led, "signal", payload={
        "signal_keys": ["synthetic-signal"], "project_id": 1,
        "type": "med_followup"}))
    render = _latest_render(led)
    spec = json.loads(render["spec_json"])
    attempt = _begin(led, render)
    _receipt(led, render, attempt["attempt_id"], message_id="mid-1")
    if change == "edit":
        led.db.execute("UPDATE messages SET content_hash='edited',body_text='更新済み' WHERE message_id=100")
    else:
        led.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=100")
    led.db.commit()
    # The signal artifact has not been re-evaluated yet; its evidence
    # nevertheless changed and must invalidate the button immediately.
    request = _notif(_token_for(spec, "assign"))
    request["origin"] = ORIGIN
    result = notify_cards.apply_notification(led, request, CFG, now=NOW + 1)
    assert result["outcome"] == "rejected" and result["error"] == "stale_source"
