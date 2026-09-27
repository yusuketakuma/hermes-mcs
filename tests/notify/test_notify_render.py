"""Signal-card evidence changes must invalidate previously rendered actions."""

import json

import pytest

import notify_cards
import notify_render
from test_notify_cards import (
    CFG, NOW, ORIGIN, _begin, _dispatch, _intent, _latest_render,
    _msg, _notif, _patient, _receipt, _signal_row, _token_for, led,
)

__all__ = ["led"]  # shared isolated-ledger fixture


@pytest.mark.parametrize(
    ("message_ids", "expected"),
    [([100, 101], "後の投稿"), ([100, "invalid"], "退院時の投稿")],
)
def test_signal_display_and_body_select_same_evidence(
        led, message_ids, expected):
    _patient(led)
    _msg(led, 100, body="最初の投稿")
    _msg(led, 101, body="後の投稿")
    _msg(led, 102, body="退院時の投稿")
    sig = {"project_id": 1, "type": "med_followup", "note": "合成候補",
           "evidence": {
        "message_ids": message_ids, "discharge_message_id": 102,
        "message_id": 100}}

    blocks = notify_render._signal_display(led.db, sig)
    body = notify_render._signal_body(led.db, sig)

    quotes = [block["text"] for block in blocks if block["type"] == "quote"]
    assert len(quotes) == 1 and expected in quotes[0]
    assert expected in body
    assert all(other not in body for other in
               ("最初の投稿", "後の投稿", "退院時の投稿") if other != expected)


def test_deleted_signal_evidence_differs_between_card_and_body(led):
    _patient(led)
    _msg(led, 100, body="消された本文")
    led.db.execute(
        "UPDATE messages SET body_state='deleted' WHERE message_id=100")
    sig = {"project_id": 1, "type": "med_followup", "note": "合成候補",
           "evidence": {"message_id": 100}}

    blocks = notify_render._signal_display(led.db, sig)
    body = notify_render._signal_body(led.db, sig)

    assert all(block["type"] != "quote" for block in blocks)
    assert "（削除済み）" in body and "消された本文" not in body


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
