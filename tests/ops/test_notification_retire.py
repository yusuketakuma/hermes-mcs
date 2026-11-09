"""Human-approved retirement of stale run_failed operational alerts.

A retire withdraws the notification without claiming it was unsent,
delivered, or recovered: payload/progress/attempt evidence stays
byte-identical and delivery_outcome remains unknown."""
from __future__ import annotations

from contextlib import closing
import copy
import json
import os
import sqlite3
from types import SimpleNamespace
import uuid

import pytest

import notification_cancel as cancel
from common_testkit import make_ledger
from notify_testkit import NOW


TABLES = (
    "notify_outbox", "notification_intent_cards", "notification_cards",
    "notification_renders", "notification_render_parts", "notification_delivery_attempts", "command_receipts",
)


def _snapshot(db):
    return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in TABLES}


def _alert(led, *, progress=None, **overrides):
    eid = led.outbox_add("run_failed", None, {"code": "synthetic_crash"})
    held = {"sending": {"chunk": 0}, "fingerprint": "synthetic-fp",
            "hold_reason": "send_outcome_unknown"}
    led.db.execute(
        "UPDATE notify_outbox SET state='failed',next_try=NULL,route='text',progress=? "
        "WHERE event_id=?", (json.dumps(held if progress is None else progress), eid))
    for column, value in overrides.items():
        led.db.execute(f"UPDATE notify_outbox SET {column}=? WHERE event_id=?",
                       (value, eid))
    led.db.commit()
    return eid


def _request(case, events):
    return {"command_id": str(uuid.uuid4()), "actor": "synthetic-operator",
            "reason": "合成の退役受入確認", "human_confirmed": True,
            "plan": cancel.build_plan(case.led.db, events,
                                      action="retire_operational_alert")}


@pytest.fixture
def case(tmp_path, monkeypatch):
    root = tmp_path / "synthetic-install"
    root.mkdir()
    led = make_ledger(root, data_dir=True)
    data = root / "data"
    eligible = [_alert(led), _alert(led, progress={"hold_reason": "restore_text_unverified"})]
    unrelated = _alert(led)
    value = SimpleNamespace(root=root, data=data, led=led,
                            events=eligible, unrelated=unrelated)
    try:
        yield value
    finally:
        led.close()


def test_retire_suppresses_eligible_alerts_and_preserves_evidence_bytes(case):
    request = _request(case, case.events)
    before = _snapshot(case.led.db)
    receipt = cancel.apply(case.root, request)
    preserved = ("payload", "progress", "attempts", "accepted_ref")
    rows = [dict(r) for r in case.led.db.execute(
        "SELECT event_id,state,next_try," + ",".join(preserved) + " FROM notify_outbox "
        "WHERE event_id IN (?,?) ORDER BY event_id", case.events)]
    assert [(row["state"], row["next_try"]) for row in rows] == [("suppressed", None)] * 2
    columns = [d[0] for d in case.led.db.execute("SELECT * FROM notify_outbox LIMIT 0").description]
    prior = {dict(zip(columns, old))["event_id"]: dict(zip(columns, old))
             for old in before["notify_outbox"]}
    for row in rows:
        for column in preserved:
            assert row[column] == prior[row["event_id"]][column]
    assert receipt["kind"] == "ops.notification_retire_operational_alert"
    assert receipt["action"] == "retire_operational_alert"
    assert receipt["delivery_outcome"] == "unknown"
    assert receipt["residual"] == "orphan_delivery_unproven"
    assert receipt["remote_operations"] == 0
    assert receipt["event_ids"] == case.events
    assert receipt["notice_specs"] == []
    archive = case.data / "notification_cancellations" / request["command_id"]
    assert json.loads((archive / "receipt.json").read_text()) == receipt
    with closing(sqlite3.connect(receipt["backup"])) as backup:
        assert _snapshot(backup) == before
    assert case.led.db.execute("SELECT COUNT(*) FROM command_receipts").fetchone()[0] == 1
    assert [tuple(r) for r in case.led.db.execute(
        "SELECT state,next_try FROM notify_outbox WHERE event_id=?", (case.unrelated,))] \
        == [("failed", None)]


def test_same_command_is_idempotent_but_changed_payload_conflicts(case):
    request = _request(case, case.events)
    first = cancel.apply(case.root, request)
    before = _snapshot(case.led.db)
    assert cancel.apply(case.root, copy.deepcopy(request)) == first
    assert _snapshot(case.led.db) == before
    with pytest.raises(ValueError, match="command_id_conflict"):
        cancel.apply(case.root, dict(request, reason="別の合成理由"))
    assert _snapshot(case.led.db) == before
    assert len(list((case.data / "notification_cancellations").glob("*/before-*.db"))) == 1


@pytest.mark.parametrize("damage", [
    "project_id", "interactive", "pending", "accepted", "next_try", "other_kind",
    "progress_broken", "progress_list", "other_hold_reason", "no_hold_reason",
    "intent_batch", "intent_card", "render",
])
def test_retire_rejects_ineligible_events(case, damage):
    db = case.led.db
    event = case.events[0]
    if damage == "project_id":
        db.execute("UPDATE notify_outbox SET project_id=1 WHERE event_id=?", (event,))
    elif damage == "interactive":
        db.execute("UPDATE notify_outbox SET route='interactive' WHERE event_id=?", (event,))
    elif damage == "pending":
        db.execute("UPDATE notify_outbox SET state='pending' WHERE event_id=?", (event,))
    elif damage == "accepted":
        db.execute("UPDATE notify_outbox SET state='accepted' WHERE event_id=?", (event,))
    elif damage == "next_try":
        db.execute("UPDATE notify_outbox SET next_try=? WHERE event_id=?", (NOW + 60, event))
    elif damage == "other_kind":
        db.execute("UPDATE notify_outbox SET kind='session_expired' WHERE event_id=?", (event,))
    elif damage == "progress_broken":
        db.execute("UPDATE notify_outbox SET progress='{' WHERE event_id=?", (event,))
    elif damage == "progress_list":
        db.execute("UPDATE notify_outbox SET progress='[]' WHERE event_id=?", (event,))
    elif damage == "other_hold_reason":
        db.execute("UPDATE notify_outbox SET progress=? WHERE event_id=?",
                   (json.dumps({"hold_reason": "resend_exhausted"}), event))
    elif damage == "no_hold_reason":
        db.execute("UPDATE notify_outbox SET progress='{}' WHERE event_id=?", (event,))
    elif damage == "intent_batch":
        db.execute("INSERT INTO notification_intent_batches "
                   "(event_id,frozen_payload,payload_hash,route_epoch,transport,sealed_at) "
                   "VALUES(?,?,?,?,?,?)", (event, "{}", "h", 1, "slack", NOW))
    elif damage == "intent_card":
        db.execute("INSERT INTO notification_cards"
                   "(card_key,kind,anchor_key,created_at,updated_at) "
                   "VALUES('synthetic','thread','a',?,?)", (NOW, NOW))
        db.execute("INSERT INTO notification_intent_cards(event_id,card_id,coverage) "
                   "VALUES(?,?,?)", (event, 1, "[]"))
    elif damage == "render":
        db.execute("INSERT INTO notification_renders "
                   "(delivery_id,card_id,op,render_rev,route_epoch,transport,"
                   "payload_hash,correlation,state,intent_event_id,created_at,updated_at) "
                   "VALUES(?,NULL,'notice',1,1,'slack','h','c','not_sent',?,?,?)",
                   (str(uuid.uuid4()), event, NOW, NOW))
    db.commit()
    before = _snapshot(db)
    with pytest.raises(ValueError):
        cancel.build_plan(db, case.events, action="retire_operational_alert")
    assert _snapshot(db) == before


def test_retire_plan_rejects_cancel_targets_and_vice_versa(case):
    db = case.led.db
    plain = case.led.outbox_add("new_messages", 1, {"message_ids": [1]})
    db.execute("UPDATE notify_outbox SET route='interactive',next_try=?,progress='{}' "
               "WHERE event_id=?", (NOW, plain))
    db.commit()
    # A retire plan over a non-run_failed event is refused...
    with pytest.raises(ValueError):
        cancel.build_plan(db, case.events + [plain], action="retire_operational_alert")
    # ...and the cancel path still refuses run_failed (route is text).
    with pytest.raises(ValueError):
        cancel.build_plan(db, [plain] + case.events)


def test_retire_stale_plan_locks_markers_and_restore_holds(case):
    request = _request(case, case.events)
    db = case.led.db
    db.execute("UPDATE notify_outbox SET updated_at=updated_at+1 WHERE event_id=?",
               (case.events[0],))
    db.commit()
    before = _snapshot(db)
    with pytest.raises(ValueError, match="stale_plan"):
        cancel.apply(case.root, request)
    assert _snapshot(db) == before
    assert not (case.data / "notification_cancellations").exists()
    db.execute("UPDATE notify_outbox SET updated_at=updated_at-1 WHERE event_id=?",
               (case.events[0],))
    db.commit()

    for lock_name in ("update.lock", "run.lock"):
        fd = cancel.acquire_run_lock(str(case.data / lock_name))
        assert fd is not None
        try:
            with pytest.raises(ValueError, match="operation_lock_held"):
                cancel.apply(case.root, _request(case, case.events))
        finally:
            os.close(fd)

    for marker in ("restore_pending.json", "update_in_progress.marker"):
        (case.data / marker).write_text("{}")
        with pytest.raises(ValueError, match="lifecycle_operation_pending"):
            cancel.apply(case.root, _request(case, case.events))
        (case.data / marker).unlink()

    db.execute("INSERT INTO notification_restore_holds(reason,held_at) VALUES('synthetic',?)",
               (NOW,))
    db.commit()
    with pytest.raises(ValueError, match="restore_delivery_held"):
        cancel.apply(case.root, _request(case, case.events))
    db.execute("DELETE FROM notification_restore_holds")
    db.commit()
    receipt = cancel.apply(case.root, _request(case, case.events))
    assert receipt["event_ids"] == case.events


def test_retire_plan_is_readonly_and_hash_bound(case):
    before = _snapshot(case.led.db)
    plan = cancel.build_plan(case.led.db, case.events, action="retire_operational_alert")
    assert _snapshot(case.led.db) == before
    assert plan["version"] == 1 and plan["action"] == "retire_operational_alert"
    for event in plan["events"]:
        assert event["action"] == "retire_operational_alert"
        assert event["delivery_outcome"] == "unknown"
        assert event["notice_specs"] == []
        assert len(event["state_sha256"]) == 64
    db = case.led.db
    db.execute("UPDATE notify_outbox SET attempts=attempts+1 WHERE event_id=?",
               (case.events[0],))
    db.commit()
    with pytest.raises(ValueError, match="stale_plan"):
        cancel.apply(case.root, {"command_id": str(uuid.uuid4()),
                                 "actor": "synthetic-operator",
                                 "reason": "合成", "human_confirmed": True,
                                 "plan": plan})
    assert not (case.data / "notification_cancellations").exists()
