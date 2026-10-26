"""Human-approved cancellation against synthetic queues, with no transport I/O."""
from __future__ import annotations

from contextlib import closing
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import uuid

import pytest

import notification_cancel as cancel
import notify_transport
from common_testkit import make_ledger
from notify_testkit import NOW, _dispatch, _intent, _latest_render, _seed_thread
from slack_testkit import SLACK, SCOPE


TABLES = (
    "notify_outbox", "notification_intent_cards", "notification_cards",
    "notification_renders", "notification_render_parts", "notification_delivery_attempts", "command_receipts",
    "messages", "artifacts",
)
PAYLOAD_CANARY = "SYNTHETIC-PRIVATE-PAYLOAD"
FAKE_PID = 1_073_741_823


def _snapshot(db):
    return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in TABLES}


def _event(led, kind, pid, payload):
    eid = led.outbox_add(kind, pid, payload)
    led.db.execute("UPDATE notify_outbox SET route='interactive',next_try=?,progress='{}' "
                   "WHERE event_id=?", (NOW, eid))
    led.db.commit()
    return eid


def _attempt(led, delivery, state):
    aid = uuid.uuid4().hex[:16]
    led.db.execute("INSERT INTO notification_delivery_attempts "
                   "(attempt_id,delivery_id,begin_command_id,state,worker_id,created_at,finished_at) "
                   "VALUES(?,?,?,?,?,?,?)", (aid, delivery, str(uuid.uuid4()), state,
                                              "b" * 16, NOW, NOW))
    return aid


def _notice_render(led, event_id, rev=1):
    delivery = str(uuid.uuid4())
    spec = json.dumps({"delivery_id": delivery, "synthetic_private": PAYLOAD_CANARY})
    led.db.execute("INSERT INTO notification_renders "
                   "(delivery_id,card_id,op,render_rev,route_epoch,profile,application_id,team_id,"
                   "channel_id,transport,spec_json,payload_hash,correlation,state,intent_event_id,"
                   "created_at,updated_at) VALUES(?,NULL,'notice',?,1,?,?,?,?, 'slack',?,?,?,'not_sent',?,?,?)",
                   (delivery, rev, SCOPE["profile"], SCOPE["application_id"], SCOPE["team_id"],
                    SCOPE["channel_id"], spec, hashlib.sha256(spec.encode()).hexdigest(),
                    uuid.uuid4().hex, event_id, NOW, NOW))
    return delivery, _attempt(led, delivery, "not_sent"), spec.encode()


def _request(case):
    return {"command_id": str(uuid.uuid4()), "actor": "synthetic-operator",
            "reason": "合成の取消受入確認", "human_confirmed": True,
            "plan": cancel.build_plan(case.led.db, case.events)}


@pytest.fixture
def case(tmp_path, monkeypatch, request):
    # Companion-body receipts exist on legacy cards or the current long-body fallback.
    part_mode = getattr(request, "param", None)
    if part_mode == "legacy":
        import present_slack
        monkeypatch.setattr(present_slack, "BODY_ON_FACE", False)
    root = tmp_path / "synthetic-install"
    root.mkdir()
    led = make_ledger(root, data_dir=True)
    data = root / "data"
    witnesses = data / "worker_status"
    witnesses.mkdir()
    (witnesses / "slack-synthetic.json").write_text(json.dumps({
        "version": cancel.VERSION, "reason": "worker_started", "pid": FAKE_PID}))

    def stopped(pid, signal):
        assert (pid, signal) == (FAKE_PID, 0)
        raise ProcessLookupError("synthetic stopped worker")

    monkeypatch.setattr(cancel.os, "kill", stopped)
    events, old_renders = [], []
    for pid, mid in ((1, 100), (2, 200)):
        _seed_thread(led, pid=pid, root=mid, mids=(mid,))
        if part_mode == "overflow":
            led.db.execute("UPDATE messages SET body_text=?,content_hash=? WHERE message_id=?",
                           ("合成" * 16000, "b" * 64, mid))
            led.db.commit()
        old_event = _intent(led, pid=pid, payload={"message_ids": [mid]})
        assert _dispatch(led, old_event, SLACK)["dispatched"]
        render = dict(_latest_render(led, pid))
        led.db.execute("UPDATE notification_renders SET state='unknown' WHERE delivery_id=?",
                       (render["delivery_id"],))
        aid = _attempt(led, render["delivery_id"], "unknown")
        old_renders.append((render, aid))
        event = _event(led, "new_messages", pid,
                       {"message_ids": [mid], "synthetic_private": PAYLOAD_CANARY})
        led.db.execute("INSERT INTO notification_intent_cards(event_id,card_id,coverage) "
                       "VALUES(?,?,?)", (event, pid, json.dumps([mid])))
        events.append(event)
    digest = _event(led, "daily_digest", None, {"date": "2026-10-18"})
    led.db.execute("UPDATE notify_outbox SET state='failed',next_try=NULL,progress=? WHERE event_id=?",
                   (json.dumps({"hold_reason": "resend_exhausted"}), digest))
    notices = [_notice_render(led, digest, rev) for rev in range(1, 4)]
    renders_dir = data / "slack_render"
    renders_dir.mkdir(exist_ok=True)
    for delivery, _aid, spec in notices:
        (renders_dir / f"{delivery}.json").write_bytes(spec)
    unrelated = _event(led, "ops_alert", None, {"synthetic_private": "UNRELATED"})
    led.db.commit()
    value = SimpleNamespace(root=root, data=data, led=led, events=[*events, digest],
                            old_renders=old_renders, notices=notices, unrelated=unrelated,
                            old_specs={f.name: f.read_bytes() for f in renders_dir.glob("*.json")
                                       if f.stem not in {n[0] for n in notices}})
    try:
        yield value
    finally:
        led.close()


def _assert_cancelled(case):
    rows = case.led.db.execute("SELECT state,next_try FROM notify_outbox WHERE event_id IN (?,?,?) "
                               "ORDER BY event_id", case.events).fetchall()
    assert [tuple(row) for row in rows] == [("suppressed", None)] * 3
    assert [tuple(row) for row in case.led.db.execute(
        "SELECT state,delivery_id,required_render_rev FROM notification_intent_cards "
        "WHERE event_id IN (?,?) ORDER BY event_id", case.events[:2])] == [
            ("suppressed", None, 0), ("suppressed", None, 0)]


def test_plan_is_readonly_metadata_and_hashes_without_payloads_or_destinations(case):
    before = _snapshot(case.led.db)
    plan = cancel.build_plan(case.led.db, list(reversed(case.events)))
    assert _snapshot(case.led.db) == before
    assert plan["version"] == 1
    assert [event["event_id"] for event in plan["events"]] == sorted(case.events)
    for event in plan["events"]:
        assert set(event) == {"event_id", "kind", "state_sha256", "notice_specs"}
        assert len(event["state_sha256"]) == 64
        int(event["state_sha256"], 16)
    encoded = json.dumps(plan)
    assert all(value not in encoded for value in (PAYLOAD_CANARY, *SCOPE.values()))


def test_cancel_three_intents_preserves_old_deliveries_and_archives_unsent_specs(case):
    request = _request(case)
    before = _snapshot(case.led.db)
    receipt = cancel.apply(case.root, request)
    _assert_cancelled(case)
    assert receipt["event_ids"] == case.events
    assert receipt["remote_operations"] == 0
    after = _snapshot(case.led.db)
    for table in ("notification_cards", "notification_renders", "notification_render_parts", "notification_delivery_attempts",
                  "messages", "artifacts"):
        assert after[table] == before[table]
    assert case.led.db.execute("SELECT state FROM notify_outbox WHERE event_id=?",
                               (case.unrelated,)).fetchone()[0] == "pending"
    untouched = [row for row in before["notify_outbox"] if row[0] not in case.events]
    assert [row for row in after["notify_outbox"] if row[0] not in case.events] == untouched
    assert [row for row in after["notification_intent_cards"] if row[0] not in case.events] == [
        row for row in before["notification_intent_cards"] if row[0] not in case.events]
    archive = case.data / "notification_cancellations" / request["command_id"]
    for delivery, _aid, spec in case.notices:
        assert not (case.data / "slack_render" / f"{delivery}.json").exists()
        assert (archive / f"{delivery}.json").read_bytes() == spec
    assert all((case.data / "slack_render" / name).read_bytes() == spec
               for name, spec in case.old_specs.items())
    assert json.loads((archive / "receipt.json").read_text()) == receipt
    with closing(sqlite3.connect(receipt["backup"])) as backup:
        assert backup.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert _snapshot(backup) == before
    assert Path(receipt["backup"]).stat().st_mode & 0o777 == 0o600
    assert case.led.db.execute("SELECT COUNT(*) FROM command_receipts").fetchone()[0] == 1


def test_same_command_is_idempotent_but_changed_payload_conflicts(case):
    request = _request(case)
    first = cancel.apply(case.root, request)
    before = _snapshot(case.led.db)
    assert cancel.apply(case.root, copy.deepcopy(request)) == first
    assert _snapshot(case.led.db) == before
    with pytest.raises(ValueError, match="command_id_conflict"):
        cancel.apply(case.root, dict(request, reason="別の合成理由"))
    assert _snapshot(case.led.db) == before
    assert len(list((case.data / "notification_cancellations").glob("*/before-*.db"))) == 1


@pytest.mark.parametrize("field,value", [
    ("human_confirmed", False), ("human_confirmed", 1), ("reason", "  "),
    ("actor", ""), ("command_id", "not-a-uuid"),
])
def test_requires_explicit_human_identity_reason_and_command(case, field, value):
    request = _request(case)
    request[field] = value
    before = _snapshot(case.led.db)
    with pytest.raises(ValueError, match="human_confirmation_reason_and_identity_required"):
        cancel.apply(case.root, request)
    assert _snapshot(case.led.db) == before


@pytest.mark.parametrize("marker", ["restore_pending.json", "update_in_progress.marker"])
def test_lifecycle_markers_preserve_the_queue(case, marker):
    request = _request(case)
    (case.data / marker).write_text("{}")
    before = _snapshot(case.led.db)
    with pytest.raises(ValueError, match="lifecycle_operation_pending"):
        cancel.apply(case.root, request)
    assert _snapshot(case.led.db) == before


@pytest.mark.parametrize("marker", ["restore_pending.json", "update_in_progress.marker"])
def test_lifecycle_marker_created_during_lock_acquisition_preserves_queue(case, monkeypatch, marker):
    request = _request(case)
    acquire = cancel.acquire_run_lock
    def lifecycle_race(path):
        fd = acquire(path)
        if Path(path).name == "run.lock":
            (case.data / marker).write_text("{}")
        return fd
    monkeypatch.setattr(cancel, "acquire_run_lock", lifecycle_race)
    before = _snapshot(case.led.db)
    with pytest.raises(ValueError, match="lifecycle_operation_pending"):
        cancel.apply(case.root, request)
    assert _snapshot(case.led.db) == before
    assert not (case.data / "notification_cancellations").exists()
    monkeypatch.setattr(cancel, "acquire_run_lock", acquire)
    (case.data / marker).unlink()
    cancel.apply(case.root, request)  # both locks were released on rejection
    _assert_cancelled(case)


@pytest.mark.parametrize("lock_name", ["update.lock", "run.lock"])
def test_existing_lock_refuses_cancellation_without_modifying_queue(case, lock_name):
    request = _request(case)
    fd = cancel.acquire_run_lock(str(case.data / lock_name))
    assert fd is not None
    before = _snapshot(case.led.db)
    try:
        with pytest.raises(ValueError, match="operation_lock_held"):
            cancel.apply(case.root, request)
    finally:
        os.close(fd)
    assert _snapshot(case.led.db) == before
    receipt = cancel.apply(case.root, request)  # rejection released any earlier acquired lock
    assert receipt["event_ids"] == case.events


@pytest.mark.parametrize("worker", ["live", "unknown", "missing", "old_version", "bad_reason"])
def test_worker_stop_must_be_proven_before_queue_changes(case, monkeypatch, worker):
    request = _request(case)
    path = case.data / "worker_status" / "slack-synthetic.json"
    if worker == "missing":
        path.unlink()
    elif worker in ("old_version", "bad_reason"):
        record = json.loads(path.read_text())
        record["version" if worker == "old_version" else "reason"] = "invalid-synthetic"
        path.write_text(json.dumps(record))
    else:
        def probe(pid, signal):
            assert (pid, signal) == (FAKE_PID, 0)
            if worker == "unknown":
                raise PermissionError("synthetic inaccessible process")
        monkeypatch.setattr(cancel.os, "kill", probe)
    before = _snapshot(case.led.db)
    with pytest.raises((ValueError, PermissionError)):
        cancel.apply(case.root, request)
    assert _snapshot(case.led.db) == before


def test_every_worker_witness_must_be_stopped(case, monkeypatch):
    request = _request(case)
    path = case.data / "worker_status" / "slack-second.json"
    path.write_text(json.dumps({"version": cancel.VERSION, "reason": "worker_started", "pid": FAKE_PID - 1}))
    def probe(pid, signal):
        assert signal == 0 and pid in (FAKE_PID, FAKE_PID - 1)
        if pid == FAKE_PID:
            raise ProcessLookupError("synthetic stopped worker")
    monkeypatch.setattr(cancel.os, "kill", probe)
    before = _snapshot(case.led.db)
    with pytest.raises(ValueError, match="worker_not_stopped"):
        cancel.apply(case.root, request)
    assert _snapshot(case.led.db) == before


def test_worker_restarting_before_commit_rolls_back_the_entire_cancellation(case, monkeypatch):
    request = _request(case)
    calls = []
    stopped = cancel.os.kill
    def restarting(pid, signal):
        assert (pid, signal) == (FAKE_PID, 0)
        calls.append(pid)
        if len(calls) == 1:
            raise ProcessLookupError("synthetic stopped worker")
    monkeypatch.setattr(cancel.os, "kill", restarting)
    before = _snapshot(case.led.db)
    with pytest.raises(ValueError, match="worker_not_stopped"):
        cancel.apply(case.root, request)
    assert len(calls) == 2
    assert _snapshot(case.led.db) == before
    earlier = set((case.data / "notification_cancellations").rglob("before-*.db"))
    assert len(earlier) == 1
    monkeypatch.setattr(cancel.os, "kill", stopped)
    receipt = cancel.apply(case.root, request)
    assert Path(receipt["backup"]) not in earlier
    assert all(p.exists() for p in earlier)
    _assert_cancelled(case)


def test_unreleased_restore_hold_refuses_cancellation(case):
    request = _request(case)
    case.led.db.execute("INSERT INTO notification_restore_holds(reason,held_at) VALUES('synthetic',?)", (NOW,))
    case.led.db.commit()
    before = _snapshot(case.led.db)
    with pytest.raises(ValueError, match="restore_delivery_held"):
        cancel.apply(case.root, request)
    assert _snapshot(case.led.db) == before


@pytest.mark.parametrize("boundary", [
    "bound_ic", "required_revision", "own_issued", "own_unknown", "other_transport",
    "text_progress", "unknown_notice", "granted_notice", "no_attempt_proof", "card_notice",
])
def test_issued_or_uncertain_delivery_is_never_cancellable(case, boundary):
    db = case.led.db
    event = case.events[0]
    if boundary == "bound_ic":
        db.execute("UPDATE notification_intent_cards SET delivery_id=? WHERE event_id=?",
                   (case.old_renders[0][0]["delivery_id"], event))
    elif boundary == "required_revision":
        db.execute("UPDATE notification_intent_cards SET required_render_rev=1 WHERE event_id=?", (event,))
    elif boundary in ("own_issued", "own_unknown"):
        delivery, _aid, _spec = _notice_render(case.led, event)
        db.execute("UPDATE notification_renders SET state=? WHERE delivery_id=?",
                   ("queued" if boundary == "own_issued" else "unknown", delivery))
    elif boundary == "other_transport":
        db.execute("UPDATE notification_cards SET transport='synthetic-other' WHERE card_id=1")
    elif boundary == "text_progress":
        db.execute("UPDATE notify_outbox SET progress=? WHERE event_id=?",
                   (json.dumps({"slack": {"sent": True}}), event))
    else:
        delivery, aid, _spec = case.notices[0]
        if boundary == "unknown_notice":
            db.execute("UPDATE notification_renders SET state='unknown' WHERE delivery_id=?", (delivery,))
        elif boundary == "granted_notice":
            db.execute("UPDATE notification_delivery_attempts SET state='granted' WHERE attempt_id=?", (aid,))
        elif boundary == "no_attempt_proof":
            db.execute("DELETE FROM notification_delivery_attempts WHERE delivery_id=?", (delivery,))
        elif boundary == "card_notice":
            db.execute("UPDATE notification_renders SET card_id=1,render_rev=99 WHERE delivery_id=?", (delivery,))
    db.commit()
    before = _snapshot(db)
    with pytest.raises(ValueError):
        cancel.build_plan(db, case.events)
    assert _snapshot(db) == before


def test_stale_plan_cannot_cancel_even_other_eligible_events(case):
    request = _request(case)
    case.led.db.execute("UPDATE notify_outbox SET next_try=next_try+1 WHERE event_id=?", (case.events[0],))
    case.led.db.commit()
    before = _snapshot(case.led.db)
    with pytest.raises(ValueError, match="stale_plan"):
        cancel.apply(case.root, request)
    assert _snapshot(case.led.db) == before
    assert not (case.data / "notification_cancellations").exists()


def test_cleanup_failure_resumes_same_receipt_without_reopening_notifications(case, monkeypatch):
    request = _request(case)
    original = cancel._withdraw
    def interrupted(data, receipt):
        # Model a crash after the first spec was archived, before remaining cleanup.
        delivery = receipt["notice_specs"][0]
        archive = data / "notification_cancellations" / receipt["command_id"]
        (data / "slack_render" / f"{delivery}.json").rename(archive / f"{delivery}.json")
        raise OSError("synthetic cleanup interruption")
    monkeypatch.setattr(cancel, "_withdraw", interrupted)
    with pytest.raises(OSError, match="synthetic cleanup interruption"):
        cancel.apply(case.root, request)
    _assert_cancelled(case)
    before = _snapshot(case.led.db)
    monkeypatch.setattr(cancel, "_withdraw", original)
    receipt = cancel.apply(case.root, request)
    assert _snapshot(case.led.db) == before
    assert receipt["command_id"] == request["command_id"]
    archive = case.data / "notification_cancellations" / request["command_id"]
    assert json.loads((archive / "receipt.json").read_text()) == receipt
    assert all((archive / f"{delivery}.json").read_bytes() == spec
               for delivery, _aid, spec in case.notices)
    assert all(not (case.data / "slack_render" / f"{delivery}.json").exists()
               for delivery, _aid, _spec in case.notices)
    assert all((case.data / "slack_render" / name).read_bytes() == spec
               for name, spec in case.old_specs.items())


def _receipt(render, attempt, result):
    return {"version": 2, "op": "transport_receipt", "command_id": str(uuid.uuid4()),
            "attempt_id": attempt, "delivery_id": render["delivery_id"],
            "render_rev": render["render_rev"], "payload_hash": render["payload_hash"],
            "route_epoch": render["route_epoch"], "correlation": render["correlation"],
            **SCOPE, "result": result, "message_id": "1790000000.000123",
            "error_code": "synthetic_not_sent"}


def test_late_receipts_cannot_revive_the_three_cancelled_events(case):
    cancel.apply(case.root, _request(case))
    for render, aid in case.old_renders:
        out = notify_transport.apply_transport_receipt(
            case.led, _receipt(render, aid, "delivered"), SLACK, now=NOW + 1)
        assert out["applied"]  # a real old-render fact, not only an unknown-attempt rejection
        _assert_cancelled(case)
    for delivery, aid, _spec in case.notices:
        render = case.led.db.execute("SELECT * FROM notification_renders WHERE delivery_id=?", (delivery,)).fetchone()
        for result in ("not_sent", "delivered"):
            out = notify_transport.apply_transport_receipt(
                case.led, _receipt(render, aid, result), SLACK, now=NOW + 2)
            assert out["applied"] is (result == "not_sent")
            if result == "delivered":
                assert out["error"] == "attempt_conflict"
            _assert_cancelled(case)


@pytest.mark.parametrize("case", ["legacy", "overflow"], indirect=True)
def test_delivered_primary_with_unknown_body_part_survives_cancel_and_late_part_receipt(case):
    db = case.led.db
    render, aid = case.old_renders[0]
    body = db.execute("SELECT * FROM notification_render_parts WHERE delivery_id=? "
                      "AND kind='body_part' ORDER BY idx LIMIT 1", (render["delivery_id"],)).fetchone()
    assert body is not None
    # The old primary was delivered; only a companion body remains uncertain.
    db.execute("UPDATE notification_renders SET state='delivered',parts_state='unknown' WHERE delivery_id=?",
               (render["delivery_id"],))
    db.execute("UPDATE notification_delivery_attempts SET state='delivered',message_id=? WHERE attempt_id=?",
               ("1790000000.000101", aid))
    db.execute("UPDATE notification_cards SET delivery_state='delivered',message_id=?,applied_render_rev=?,"
               "thread_state='created',thread_id=? WHERE card_id=?",
               ("1790000000.000101", render["render_rev"], "1790000000.000102", render["card_id"]))
    db.execute("UPDATE notification_render_parts SET state='delivered',remote_id=? WHERE delivery_id=?",
               ("1790000000.000103", render["delivery_id"]))
    db.execute("UPDATE notification_render_parts SET state='unknown',remote_id=NULL WHERE delivery_id=? AND part_id=?",
               (render["delivery_id"], body["part_id"]))
    db.commit()
    before = _snapshot(db)
    cancel.apply(case.root, _request(case))
    _assert_cancelled(case)
    after = _snapshot(db)
    for table in ("notification_cards", "notification_renders", "notification_render_parts",
                  "notification_delivery_attempts"):
        assert after[table] == before[table]
    req = _receipt(render, f"p:{render['delivery_id'].replace('-', '')}:{body['part_id']}", "delivered")
    req.update(op="part_receipt", part_id=body["part_id"], kind=body["kind"], remote_id="1790000000.000104")
    del req["message_id"], req["error_code"]
    outcome = notify_transport.apply_part_receipt(case.led, req, SLACK, now=NOW + 3)
    assert outcome["applied"] is True
    assert db.execute("SELECT state,remote_id FROM notification_render_parts WHERE delivery_id=? AND part_id=?",
                      (render["delivery_id"], body["part_id"])).fetchone()[:] == ("delivered", "1790000000.000104")
    _assert_cancelled(case)


@pytest.mark.parametrize("function,locked,snapshot_bad,writer_bad", [
    ("apply", True, False, False),
    ("apply", False, False, True),
    ("plan", False, True, False),
    ("main", True, True, False),
])
def test_operator_sqlite_gate_exception_is_scoped_and_requires_the_run_lock(
        tmp_path, monkeypatch, function, locked, snapshot_bad, writer_bad):
    spec = importlib.util.spec_from_file_location(
        "cancel_test_gates", Path(__file__).resolve().parents[2] / "ci/gates.py")
    gates = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gates)
    mcs = tmp_path / "synthetic-mcs"
    path = mcs / "ops/notification_cancel.py"
    path.parent.mkdir(parents=True)
    body = "    acquire_run_lock('synthetic.lock')\n" if locked else ""
    path.write_text(f"import sqlite3\ndef {function}():\n" + body
                    + "    sqlite3.connect('file:synthetic?mode=rw', uri=True)\n")
    monkeypatch.setattr(gates, "MCS", mcs)
    monkeypatch.setattr(gates, "_py_files", lambda *args: [path])
    assert bool(gates.gate_snapshot_readonly()) is snapshot_bad
    assert bool(gates.gate_writer_lock()) is writer_bad
