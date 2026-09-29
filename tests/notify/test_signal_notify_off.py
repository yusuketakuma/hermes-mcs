"""Disabled review notifications never reach the interactive card transport."""

import pytest

import ledger
import notify_flush
from notify_testkit import (
    CFG, NOW, _begin, _dispatch, _intent, _latest_render, _msg, _patient,
    _signal_row,
)


ON = {**CFG, "signals": {"notify": True}}
OFF = {**CFG, "signals": {"notify": False}}


@pytest.fixture
def led(tmp_path):
    path = tmp_path / "data"
    path.mkdir()
    db = ledger.Ledger(str(path / "ledger.db"))
    yield db
    db.close()


def _signal(led):
    _patient(led)
    _msg(led, 100)
    _signal_row(led, "synthetic-key", mids=[100])
    return _intent(
        led, kind="signal", payload={
            "signal_key": "synthetic-key", "project_id": 1,
        })


def test_disabled_signal_suppresses_unsealed_intent(led, tmp_path):
    event = _signal(led)

    outcome = _dispatch(led, event, OFF)

    assert outcome.get("suppressed") is True
    assert led.db.execute(
        "SELECT state FROM notify_outbox WHERE event_id=?",
        (event["event_id"],)
    ).fetchone()["state"] == "suppressed"
    assert led.db.execute(
        "SELECT COUNT(*) FROM notification_intent_batches"
    ).fetchone()[0] == 0
    assert not list((tmp_path / "data" / "discord_render").glob("*.json"))


def test_disabled_signal_refuses_grant_but_allows_chat(led):
    event = _signal(led)
    assert _dispatch(led, event, ON, now=NOW)["dispatched"]
    render = _latest_render(led)

    denied = _begin(led, render, cfg=OFF)
    assert not denied["granted"]
    assert denied["error"] == "denied_signal_notify_off"

    chat = _intent(led, payload={"message_ids": [100]})
    assert _dispatch(led, chat, OFF, now=NOW)["dispatched"]
    chat_render = led.db.execute(
        "SELECT * FROM notification_renders WHERE card_id=("
        "SELECT card_id FROM notification_cards WHERE kind='thread')"
    ).fetchone()
    assert _begin(led, chat_render, n=2, cfg=OFF)["granted"]


def test_flush_suppresses_review_without_muting_chat(led, tmp_path,
                                                    monkeypatch):
    review = _signal(led)
    chat = _intent(led, payload={"message_ids": [100]})
    monkeypatch.setattr(notify_flush, "_config", lambda: OFF)
    monkeypatch.setattr(
        notify_flush, "_hermes_exe",
        lambda cfg: str(tmp_path / "missing-hermes"))

    outcome = notify_flush.flush(led)

    assert outcome["suppressed"] == 1
    assert outcome["dispatched"] == 1
    assert led.db.execute(
        "SELECT state FROM notify_outbox WHERE event_id=?",
        (review["event_id"],)
    ).fetchone()["state"] == "suppressed"
    assert led.db.execute(
        "SELECT state FROM notify_outbox WHERE event_id=?",
        (chat["event_id"],)
    ).fetchone()["state"] == "pending"
    assert len(list((tmp_path / "data" / "discord_render").glob("*.json"))) == 1
