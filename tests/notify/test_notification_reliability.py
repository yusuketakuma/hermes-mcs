"""Synthetic text-delivery budget, crash, and restore boundary regression tests."""
import semantic_send_gate
import json
import sys
from types import SimpleNamespace

import pytest

import notify_spool
import notify_flush
import notify_sender
import mcs_util
import notify_reconcile
from ledger import Ledger
from notify_testkit import _row


@pytest.fixture
def text_world(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    path = data / "ledger.db"
    led = Ledger(str(path))
    clock = [100.0]
    calls = []
    monkeypatch.setattr(notify_flush.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(notify_flush.time, "time", lambda: 1_790_000_000.0)
    monkeypatch.setattr(notify_flush, "_config", lambda: {})
    monkeypatch.setattr(mcs_util, 'hermes_exe', lambda cfg: sys.executable)
    monkeypatch.setattr(notify_sender, 'target', lambda *args: "synthetic")
    monkeypatch.setattr(notify_sender, 'send_argv', lambda *args, kind=None: ["hermes", "send"])
    monkeypatch.setattr(
        notify_flush, "_format_event",
        lambda led, ev: (
            "取得：合成\n要約：合成\n\nsynthetic\n▶ MCSで確認\nhttps://example.invalid/"
            if ev["kind"] == "semantic_notice" else "synthetic", []))
    monkeypatch.setattr(semantic_send_gate, 'semantic_render_state', lambda *args: ())
    monkeypatch.setattr(semantic_send_gate, 'semantic_gate', lambda *args, **kwargs: None)

    def child(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(notify_sender.subprocess, "run", child)
    yield SimpleNamespace(led=led, path=path, data=data, clock=clock, calls=calls)
    led.close()


@pytest.mark.parametrize("remaining", [-1, 0, 29.999, 30, 181])
def test_budget_boundary_preserves_attempts_and_inflight(text_world, remaining):
    w = text_world
    eid = w.led.outbox_add("run_failed", None, {})
    result = notify_flush.flush(w.led, deadline=100 + remaining)
    row = _row(w.led, eid)
    if remaining < notify_sender.SEND_MIN_BUDGET_S:
        assert not w.calls
        assert row["attempts"] == 0 and row["state"] == "pending"
        assert json.loads(row["progress"] or "{}").get("sending") is None
        assert result["skipped"] == 1
    else:
        assert len(w.calls) == 1 and row["state"] == "accepted"
        assert w.calls[0][1]["timeout"] == min(180, remaining)


@pytest.mark.parametrize("argv", [
    ["hermes", "send"],
    [sys.executable, "/synthetic/lineworks_adapter/__main__.py", "send"],
    [sys.executable, "/synthetic/mcs_standalone/__main__.py", "send"],
])
def test_direct_transport_and_reservation_refuse_low_budget(text_world, argv):
    w = text_world
    eid = w.led.outbox_add("run_failed", None, {})
    with pytest.raises(notify_sender.SendBudget):
        notify_sender.send(argv, "synthetic", deadline=129)
    with pytest.raises(notify_sender.SendBudget):
        notify_flush._send_marked(
            w.led, _row(w.led, eid), 0, [], "fp", argv, "synthetic", None, 129)
    assert not w.calls
    assert _row(w.led, eid)["progress"] is None


def test_budget_expiring_after_reservation_is_proven_not_sent(text_world, monkeypatch):
    w = text_world
    eid = w.led.outbox_add("run_failed", None, {})
    progress = w.led.outbox_progress

    def record(*args, **kwargs):
        progress(*args, **kwargs)
        if len(args) == 5:
            w.clock[0] = 101.0

    monkeypatch.setattr(w.led, "outbox_progress", record)
    result = notify_flush.flush(w.led, deadline=130)
    row = _row(w.led, eid)
    assert not w.calls and result["send_budget_insufficient"] == 1
    assert row["attempts"] == 0 and row["next_try"] is not None
    assert json.loads(row["progress"])["sending"] is None


@pytest.mark.parametrize("fallback", [False, True])
def test_each_chunk_and_file_fallback_rechecks_budget(text_world, monkeypatch, fallback):
    w = text_world
    eid = w.led.outbox_add("run_failed", None, {})
    file = w.data / "synthetic.txt"
    file.write_text("synthetic")
    monkeypatch.setattr(
        notify_flush, "_format_event",
        lambda *args: ("x" * (notify_flush.TEXT_CHUNK_WIDTH + 1),
                      [("synthetic.txt", str(file))] if fallback else []))

    def child(argv, **kwargs):
        w.calls.append(kwargs)
        w.clock[0] = 101
        return SimpleNamespace(returncode=2 if fallback else 0, stdout="", stderr="")

    monkeypatch.setattr(notify_sender.subprocess, "run", child)
    result = notify_flush.flush(w.led, deadline=130)
    row = _row(w.led, eid)
    progress = json.loads(row["progress"])
    assert len(w.calls) == 1 and result["send_budget_insufficient"] == 1
    assert row["attempts"] == 0 and row["state"] == "pending"
    assert progress["sending"] is None
    assert progress["next"] == (0 if fallback else 1)


@pytest.mark.parametrize("point", ["reserved", "transport", "ack", "accepted"])
def test_crash_windows_never_blind_resend_after_reopen(text_world, monkeypatch, point):
    class Crash(BaseException):
        pass

    w = text_world
    eid = w.led.outbox_add("run_failed", None, {})
    progress = w.led.outbox_progress
    mark = w.led.outbox_mark

    def record(*args, **kwargs):
        progress(*args, **kwargs)
        if ((point == "reserved" and len(args) == 5)
                or (point == "ack" and args[1] == 1 and len(args) == 4)):
            raise Crash

    def child(argv, **kwargs):
        w.calls.append(kwargs)
        if point == "transport":
            raise Crash
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def accepted(*args, **kwargs):
        mark(*args, **kwargs)
        if point == "accepted":
            raise Crash

    monkeypatch.setattr(w.led, "outbox_progress", record)
    monkeypatch.setattr(w.led, "outbox_mark", accepted)
    monkeypatch.setattr(notify_sender.subprocess, "run", child)
    with pytest.raises(Crash):
        notify_flush.flush(w.led)
    reopened = Ledger(str(w.path))
    try:
        notify_flush.flush(reopened)
        row = _row(reopened, eid)
        if point in ("reserved", "transport"):
            assert row["state"] == "failed" and row["next_try"] is None
            assert json.loads(row["progress"])["hold_reason"] == "send_outcome_unknown"
            assert json.loads(row["progress"])["sending"] == 1
        else:
            assert row["state"] == "accepted"
    finally:
        reopened.close()
    assert len(w.calls) == (0 if point == "reserved" else 1)


@pytest.mark.parametrize("phase", ["restored", "awaiting_consent", "corrupt"])
def test_restore_marker_holds_content_but_allows_existing_alerts(text_world, monkeypatch, phase):
    w = text_world
    kinds = ["new_messages", "signal", "attachment_followup", "semantic_notice",
             "future_content", *sorted(notify_sender.SYSTEM_ALERT_KINDS)]
    ids = {kind: w.led.outbox_add_tx(kind, None, {}, route="text") for kind in kinds}
    # The semantic marker also binds a real arrival even when this restore
    # test stubs the rest of the semantic formatting/gate pipeline.
    w.led.db.execute('UPDATE notify_outbox SET project_id=1,payload=? WHERE event_id=?',
                     (json.dumps({'message_ids': [1]}), ids['new_messages']))
    w.led.db.execute('UPDATE notify_outbox SET project_id=1,payload=? WHERE event_id=?',
                     (json.dumps({'root_id': 1, 'target_message_id': 1,
                                  'src_event_id': ids['new_messages']}), ids['semantic_notice']))
    w.led.db.commit()
    if phase == "corrupt":
        (w.data / notify_spool.RESTORE_MARKER).write_text("not-json")
    else:
        notify_spool.mark_restored(str(w.data), phase=phase)
    result = notify_flush.flush(w.led, limit=20)
    # run_failed is the only system alert that no longer bypasses the
    # restore gate — a pre-restore operational alert cannot prove it was
    # never delivered, so it defers like ordinary content.
    assert len(w.calls) == len(notify_sender.RESTORE_BYPASS_ALERT_KINDS)
    assert result["restore_pending"] == 6
    for kind, eid in ids.items():
        row = _row(w.led, eid)
        assert row["state"] == (
            "accepted" if kind in notify_sender.RESTORE_BYPASS_ALERT_KINDS else "pending")
        if kind not in notify_sender.RESTORE_BYPASS_ALERT_KINDS:
            assert row["progress"] is None and row["attempts"] == 0
    notify_spool.clear_restore_pending(str(w.data))
    # gated content was deferred out of the due window; it resumes after
    monkeypatch.setattr(notify_flush.time, "time",
                        lambda: 1_790_000_000.0 + notify_flush.RERENDER_RETRY_S)
    # Patient signal stays private-thread pending; the four other synthetic
    # content kinds retain their existing text route, and the deferred
    # run_failed alert also sends once the marker is gone.
    assert notify_flush.flush(w.led, limit=20)["sent"] == 5


def test_reconcile_retains_unverifiable_text_after_marker_clears(text_world):
    w = text_world
    eid = w.led.outbox_add_tx("new_messages", None, {"message_ids": [1]}, route="text")
    w.led.db.commit()
    notify_spool.mark_restored(str(w.data))
    result = notify_reconcile.reconcile_after_restore(w.led, {})
    row = _row(w.led, eid)
    assert row["state"] == "failed" and row["next_try"] is None
    assert json.loads(row["progress"])["hold_reason"] == "restore_text_unverified"
    assert result["text_held"] == [
        {"event_id": eid, "kind": "new_messages", "reason": "restore_text_unverified"}]
    assert result["events_held"] == 1
    assert notify_spool.restore_pending(str(w.data)) is None
    assert w.led.db.execute("SELECT COUNT(*) FROM notify_outbox").fetchone()[0] == 2
    again = notify_reconcile.reconcile_after_restore(w.led, {})
    assert again["text_held"] == result["text_held"]
    assert w.led.db.execute("SELECT COUNT(*) FROM notify_outbox").fetchone()[0] == 2
    # Only the safe operational notice sends; no rescue of restored content.
    assert notify_flush.flush(w.led)["sent"] == 1
    assert len(w.calls) == 1


def test_events_created_after_the_restore_point_wait_instead_of_quarantine(text_world):
    w = text_world
    before = w.led.outbox_add_tx("new_messages", None, {"message_ids": [1]}, route="text")
    w.led.db.commit()
    notify_spool.mark_restored(str(w.data), now=1000.0)
    w.led.db.execute("UPDATE notify_outbox SET created_at=999.0 WHERE event_id=?", (before,))
    after = w.led.outbox_add_tx("new_messages", None, {"message_ids": [2]}, route="text")
    w.led.db.execute("UPDATE notify_outbox SET created_at=1001.0 WHERE event_id=?", (after,))
    w.led.db.commit()
    result = notify_reconcile.reconcile_after_restore(w.led, {})
    assert [h["event_id"] for h in result["text_held"]] == [before]
    assert _row(w.led, after)["state"] == "pending"


def test_consent_reconcile_does_not_quarantine_or_clear_marker(text_world):
    w = text_world
    eid = w.led.outbox_add_tx("new_messages", None, {"message_ids": [1]}, route="text")
    w.led.db.commit()
    notify_spool.mark_restored(str(w.data), phase="awaiting_consent")
    result = notify_reconcile.reconcile_after_restore(w.led, {})
    assert result["skipped"] == "awaiting_consent"
    assert _row(w.led, eid)["state"] == "pending"
    assert notify_spool.restore_pending(str(w.data)) is not None


def test_corrupt_receipt_keeps_evidence_and_cannot_be_rescued(text_world):
    w = text_world
    eid = w.led.outbox_add_tx(
        "new_messages", None, {"message_ids": [1]}, route="text")
    w.led.db.execute("UPDATE notify_outbox SET progress='[]' WHERE event_id=?", (eid,))
    w.led.db.commit()
    assert notify_flush.flush(w.led)["failed"] == 1
    row = _row(w.led, eid)
    progress = json.loads(row["progress"])
    assert progress == {"invalid_progress": "[]", "hold_reason": "payload_or_progress_invalid"}
    assert not notify_flush._send_never_began(row)
    with pytest.raises(ValueError, match="progress_invalid"):
        notify_flush._progress(row["progress"], 1)
    assert w.led.db.execute("SELECT COUNT(*) FROM notify_outbox").fetchone()[0] == 1
    assert not w.calls


def test_reconcile_receipt_failure_keeps_restore_gate(text_world, monkeypatch):
    w = text_world
    eid = w.led.outbox_add_tx("new_messages", None, {}, route="text")
    w.led.db.commit()
    notify_spool.mark_restored(str(w.data))

    def fail(*args):
        raise OSError("synthetic receipt failure")

    monkeypatch.setattr(notify_spool, "publish_file", fail)
    with pytest.raises(OSError, match="synthetic receipt failure"):
        notify_reconcile.reconcile_after_restore(w.led, {})
    assert notify_spool.restore_pending(str(w.data)) is not None
    assert _row(w.led, eid)["next_try"] is None
    assert not w.calls


@pytest.mark.parametrize("kind,expected", [
    ("update_notice", "accepted"), ("session_recovered", "accepted"),
    ("run_failed", "pending"),
])
def test_restore_marker_defers_content_and_run_failed_but_other_alerts_send(
        text_world, monkeypatch, kind, expected):
    w = text_world
    content = [w.led.outbox_add_tx("new_messages", None, {}, route="text") for _ in range(12)]
    alert = w.led.outbox_add_tx(kind, None, {}, route="text")
    w.led.db.commit()
    notify_spool.mark_restored(str(w.data))
    notify_flush.flush(w.led)
    assert _row(w.led, content[0])["attempts"] == 0
    notify_flush.flush(w.led)
    assert _row(w.led, alert)["state"] == expected
    assert all(_row(w.led, eid)["state"] == "pending" for eid in content)
    if expected == "pending":
        # Deferred without an attempt; once the marker clears it can
        # still send — the gate only stops an automatic resend.
        assert _row(w.led, alert)["attempts"] == 0
        notify_spool.clear_restore_pending(str(w.data))
        monkeypatch.setattr(notify_flush.time, "time",
                            lambda: 1_790_000_000.0 + notify_flush.RERENDER_RETRY_S)
        notify_flush.flush(w.led, limit=20)
        assert _row(w.led, alert)["state"] == "accepted"


def test_reconcile_quarantines_prerestore_run_failed_and_it_becomes_retirable(text_world):
    import notification_cancel
    w = text_world
    old = w.led.outbox_add_tx("run_failed", None, {"code": "synthetic"}, route="text")
    w.led.db.execute("UPDATE notify_outbox SET created_at=999.0 WHERE event_id=?", (old,))
    w.led.db.commit()
    notify_spool.mark_restored(str(w.data), now=1000.0)
    fresh = w.led.outbox_add_tx("run_failed", None, {"code": "synthetic2"}, route="text")
    w.led.db.execute("UPDATE notify_outbox SET created_at=1001.0 WHERE event_id=?", (fresh,))
    w.led.db.commit()
    notify_reconcile.reconcile_after_restore(w.led, {})
    old_row = _row(w.led, old)
    assert old_row["state"] == "failed" and old_row["next_try"] is None
    assert json.loads(old_row["progress"])["hold_reason"] == "restore_text_unverified"
    # Quarantined pre-restore alerts are exactly the retire-eligible set.
    plan = notification_cancel.build_plan(w.led.db, [old],
                                          action="retire_operational_alert")
    assert [e["event_id"] for e in plan["events"]] == [old]
    # A run_failed created after the restore point was never delivered,
    # stays unquarantined and sends normally.
    assert _row(w.led, fresh)["state"] == "pending"
    notify_flush.flush(w.led)
    assert _row(w.led, fresh)["state"] == "accepted"
    # same-path contrast: the quarantined alert is never a flush candidate
    # (state='failed' with next_try NULL is outside the due window), so it
    # stays 'failed' — only the explicit retire path can suppress it.
    assert _row(w.led, old)["state"] == "failed"
    assert _row(w.led, old)["next_try"] is None


@pytest.mark.parametrize("kind", ["signal", "new_messages", "attachment_followup"])
def test_unknown_send_outcome_is_held_before_format_for_every_kind(text_world, monkeypatch, kind):
    w = text_world
    eid = w.led.outbox_add_tx(kind, None, {}, route="text")
    w.led.db.commit()
    w.led.outbox_progress(eid, 0, [], "fp", 1)

    def stale(*args):
        raise semantic_send_gate.StaleSend("signal_not_open")

    monkeypatch.setattr(notify_flush, "_format_event", stale)
    notify_flush.flush(w.led)
    row = _row(w.led, eid)
    assert row["state"] == "failed" and row["next_try"] is None
    assert json.loads(row["progress"])["hold_reason"] == "send_outcome_unknown"
    assert not w.calls


def test_archived_event_with_unknown_send_outcome_is_held_not_suppressed(text_world, monkeypatch):
    w = text_world
    eid = w.led.outbox_add_tx("signal", 1, {}, route="text")
    w.led.db.commit()
    w.led.outbox_progress(eid, 0, [], "fp", 1)
    monkeypatch.setattr(w.led, "is_archived", lambda pid: True)
    notify_flush.flush(w.led)
    row = _row(w.led, eid)
    assert row["state"] == "failed" and row["next_try"] is None
    assert json.loads(row["progress"])["hold_reason"] == "send_outcome_unknown"
    assert not w.calls
