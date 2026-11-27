"""Delivery through the real runner and Slack worker during an unfinished first import."""
import asyncio
import json
from datetime import datetime, timezone

import pytest

import initial_import_state
import notify_flush
from ingest_testkit import _message, _unread_patient
from notify_testkit import NOW
from slack_testkit import SLACK, _granted_card, _mkworld, isolated_slack_ledger as led

__all__ = ["led"]


@pytest.mark.parametrize("parent", [None, 1])
def test_initial_history_does_not_delay_the_new_slack_card(led, monkeypatch, parent):
    monkeypatch.setattr(notify_flush.time, "time", lambda: NOW)
    monkeypatch.setattr(notify_flush, "_config", lambda: SLACK)
    monkeypatch.setattr(notify_flush.mcs_util, "hermes_exe", lambda cfg: "/synthetic/missing")
    led.upsert_patient_info(_unread_patient(1), is_archived=False, notify_new=True,
                            observation_ts=int(NOW) - 60)
    old_stamp = datetime.fromtimestamp(NOW - 3600, timezone.utc).isoformat()
    live_stamp = datetime.fromtimestamp(NOW - 1, timezone.utc).isoformat()
    led.save_messages([_message(mid=i, body=f"OLD_SYNTHETIC_{i}", unread=True, posted_at=old_stamp)
                       for i in range(1, 257)], project_id=1, notify={"source": "discovery"}, notify_all_new=True)
    assert not led.db.execute("SELECT 1 FROM notify_outbox").fetchone()
    with led.db:
        for _ in range(20):
            led.outbox_add_tx("patient_history", 1, {"source": "initial_history"}, route="interactive")
    message = _message(mid=257, body="LIVE_SYNTHETIC", unread=True, posted_at=live_stamp, parent_id=parent)
    if parent is None:
        led.save_messages([message], project_id=1, notify={"source": "unread"})
    else:
        led.save_thread_replies([message], 1, notify={"source": "unread"})
    assert led.job_pending("history", 1)
    assert initial_import_state.load(led.db, 1)["capture_phase"] == "capturing"
    # One legacy cleanup uses the remaining time in this flush.
    clock = [100]
    dispatch = notify_flush._dispatch_interactive

    def cleanup(ledger, ev, cfg, res):
        outcome = dispatch(ledger, ev, cfg, res)
        if ev["kind"] == "patient_history":
            clock[0] = 102
        return outcome

    with monkeypatch.context() as patch:
        patch.setattr(notify_flush.time, "monotonic", lambda: clock[0])
        patch.setattr(notify_flush, "_dispatch_interactive", cleanup)
        assert notify_flush.flush(led, deadline=101)["dispatched"] == 1
    world = _mkworld(led)
    asyncio.run(_granted_card(world.worker, led, world.root))
    creates = [kwargs for op, kwargs in world.client.calls if op == "create"]
    assert len(creates) == 1
    wire = json.dumps(creates, ensure_ascii=False)
    assert "LIVE_SYNTHETIC" in wire and "OLD_SYNTHETIC_" not in wire
    assert led.db.execute("SELECT state FROM notify_outbox WHERE kind='new_messages'").fetchone()[0] == "accepted"
    assert led.db.execute("SELECT count(*) FROM notify_outbox WHERE kind='patient_history' AND state='pending'").fetchone()[0] == 19
    assert led.job_pending("history", 1)
    assert initial_import_state.load(led.db, 1)["capture_phase"] == "capturing"
