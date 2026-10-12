"""A queued ⏰ task reminder is rechecked right before it is sent.

Adopted from the Codex proof (TASK-REMINDER-STALE-SEND): synthetic temp
ledger, a normal request.update receipt, only ``_send`` stubbed."""
from __future__ import annotations

from datetime import datetime

import pytest

import mcs_requests
import notify_cards
import notify_flush
from mcs_queries import JST
from notify_testkit import CFG, _add_request, _delivered_card, _uuid, led

__all__ = ["led"]


def _at(day, hour=9, minute=0):
    return datetime.fromisoformat(
        f"{day}T{hour:02d}:{minute:02d}:00").replace(tzinfo=JST).timestamp()


def _queued(led):
    _delivered_card(led)
    assert notify_cards.task_reminders(led, CFG, now=_at("2026-09-20")) == 0
    rid = _add_request(led, title="SYNTHETIC reminder", due="2026-10-01")
    assert notify_cards.task_reminders(led, CFG, now=_at("2026-10-01")) == 1
    ev = led.db.execute(
        "SELECT * FROM notify_outbox WHERE kind='task_reminder'").fetchone()
    return rid, ev


def _flush_one(led, monkeypatch, ev, cfg, send_at):
    monkeypatch.setattr(notify_cards.time, "time", lambda: send_at)
    monkeypatch.setattr(notify_flush, "_config", lambda: cfg)
    captures = []
    monkeypatch.setattr(notify_flush, "_send",
                        lambda argv, content, *a, **kw: captures.append(content))
    res = {"sent": 0, "failed": 0, "suppressed": 0, "parked": 0}
    try:
        notify_flush._send_text(led, ev, cfg, ["synthetic-sender"],
                                "synthetic-target", res, None)
    except Exception as exc:          # the flush loop's own accounting
        notify_flush._fail_event(led, ev, cfg, res, exc)
    state = led.db.execute("SELECT state FROM notify_outbox WHERE event_id=?",
                           (ev["event_id"],)).fetchone()[0]
    return captures, res, state


@pytest.mark.parametrize("change", ["done", "cancelled", "future_due",
                                    "interactive_off"])
def test_reminder_no_longer_true_is_not_sent(led, monkeypatch, change):
    rid, ev = _queued(led)
    cfg = CFG
    if change == "interactive_off":
        cfg = {**CFG, "notify": {**CFG["notify"], "interactive": "off"}}
    else:
        patch = ({"due_date": "2026-12-01"} if change == "future_due"
                 else {"status": change})
        receipt = mcs_requests.apply_command(led, {
            "version": 1, "cmd": "request.update", "command_id": _uuid(912),
            "actor": "synthetic-reviewer", "human_confirmed": True,
            "project_id": 1, "request_id": rid, "expected_revision": 1,
            "expected_source_hash": f"{100:064x}", "patch": patch,
            "reason": "synthetic lifecycle change before delayed send"})
        assert receipt["outcome"] == "applied"
    captures, res, state = _flush_one(led, monkeypatch, ev, cfg,
                                      _at("2026-10-01", 10))
    assert captures == [] and res["suppressed"] == 1
    assert state == "suppressed"


def test_unchanged_reminder_is_sent_once(led, monkeypatch):
    _, ev = _queued(led)
    captures, res, state = _flush_one(led, monkeypatch, ev, CFG,
                                      _at("2026-10-01", 10))
    assert res["sent"] == 1 and len(captures) == 1
    assert "本日 2026-10-01" in captures[0] and state == "accepted"
    # the once-only marker still dedupes the enqueue side
    assert notify_cards.task_reminders(led, CFG, now=_at("2026-10-01", 11)) == 0


def test_revoked_card_drops_queued_reminder(led, monkeypatch):
    _, ev = _queued(led)
    led.db.execute("UPDATE notification_cards SET delivery_state='revoked'")
    led.db.commit()
    captures, res, _ = _flush_one(led, monkeypatch, ev, CFG,
                                  _at("2026-10-01", 10))
    assert captures == [] and res["suppressed"] == 1


def test_hour_boundary_waits_then_due_day_text_expires(led, monkeypatch):
    """Queued at 20:59, flushed at 21:00 — parked, not sent at night; the
    next morning the 本日 text is no longer true and the overdue stage
    takes over."""
    _delivered_card(led)
    assert notify_cards.task_reminders(led, CFG, now=_at("2026-09-20")) == 0
    _add_request(led, title="SYNTHETIC late", due="2026-10-01")
    assert notify_cards.task_reminders(
        led, CFG, now=_at("2026-10-01", 20, 59)) == 1
    ev = led.db.execute(
        "SELECT * FROM notify_outbox WHERE kind='task_reminder'").fetchone()
    captures, res, state = _flush_one(led, monkeypatch, ev, CFG,
                                      _at("2026-10-01", 21))
    assert captures == [] and res["parked"] == 1 and state == "pending"
    captures, res, state = _flush_one(led, monkeypatch, ev, CFG,
                                      _at("2026-10-02", 9))
    assert captures == [] and state == "suppressed"
    assert notify_cards.task_reminders(led, CFG, now=_at("2026-10-02")) == 1


def test_inflight_reminder_keeps_unknown_outcome(led, monkeypatch):
    """A send already begun stays an honest unknown even if the task
    closed meanwhile — never rewritten as suppressed."""
    _, ev = _queued(led)
    led.db.execute("UPDATE requests SET status='done'")
    led.db.execute("UPDATE notify_outbox SET progress=? WHERE event_id=?",
                   ('{"sending": 0}', ev["event_id"]))
    led.db.commit()
    ev = led.db.execute("SELECT * FROM notify_outbox WHERE event_id=?",
                        (ev["event_id"],)).fetchone()
    captures, res, state = _flush_one(led, monkeypatch, ev, CFG,
                                      _at("2026-10-01", 10))
    assert captures == [] and res.get("uncertain") == 1
    assert state != "suppressed"
