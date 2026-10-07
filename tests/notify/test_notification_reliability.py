"""Synthetic text-delivery budget, crash, and restore boundary regression tests."""
import json
import sys
from types import SimpleNamespace

import pytest

import notify_cards
import notify_flush
import notify_reconcile
from ledger import Ledger


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
    monkeypatch.setattr(notify_flush, "_hermes_exe", lambda cfg: sys.executable)
    monkeypatch.setattr(notify_flush, "_target", lambda *args: "synthetic")
    monkeypatch.setattr(notify_flush, "_send_argv", lambda *args: ["hermes", "send"])
    monkeypatch.setattr(
        notify_flush, "_format_event",
        lambda led, ev: (
            "取得：合成\n要約：合成\n\nsynthetic\n▶ MCSで確認\nhttps://example.invalid/"
            if ev["kind"] == "semantic_notice" else "synthetic", []))
    monkeypatch.setattr(notify_flush, "_semantic_render_state", lambda *args: ())
    monkeypatch.setattr(notify_flush, "_semantic_gate", lambda *args, **kwargs: None)

    def child(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(notify_flush.subprocess, "run", child)
    yield SimpleNamespace(led=led, path=path, data=data, clock=clock, calls=calls)
    led.close()


def _row(led, eid):
    return led.db.execute(
        "SELECT * FROM notify_outbox WHERE event_id=?", (eid,)).fetchone()


@pytest.mark.parametrize("remaining", [-1, 0, 29.999, 30, 181])
def test_budget_boundary_preserves_attempts_and_inflight(text_world, remaining):
    w = text_world
    eid = w.led.outbox_add("run_failed", None, {})
    result = notify_flush.flush(w.led, deadline=100 + remaining)
    row = _row(w.led, eid)
    if remaining < notify_flush.SEND_MIN_BUDGET_S:
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
    with pytest.raises(notify_flush._SendBudget):
        notify_flush._send(argv, "synthetic", deadline=129)
    with pytest.raises(notify_flush._SendBudget):
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
        lambda *args: ("x" * (notify_flush._MAX_LEN + 1),
                      [("synthetic.txt", str(file))] if fallback else []))

    def child(argv, **kwargs):
        w.calls.append(kwargs)
        w.clock[0] = 101
        return SimpleNamespace(returncode=2 if fallback else 0, stdout="", stderr="")

    monkeypatch.setattr(notify_flush.subprocess, "run", child)
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
    monkeypatch.setattr(notify_flush.subprocess, "run", child)
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
             "future_content", *sorted(notify_flush._ALERT_KINDS)]
    ids = {kind: w.led.outbox_add_tx(kind, None, {}, route="text") for kind in kinds}
    w.led.db.commit()
    if phase == "corrupt":
        (w.data / notify_cards.RESTORE_MARKER).write_text("not-json")
    else:
        notify_cards.mark_restored(str(w.data), phase=phase)
    result = notify_flush.flush(w.led, limit=20)
    assert len(w.calls) == len(notify_flush._ALERT_KINDS)
    assert result["restore_pending"] == 5
    for kind, eid in ids.items():
        row = _row(w.led, eid)
        assert row["state"] == ("accepted" if kind in notify_flush._ALERT_KINDS else "pending")
        if kind not in notify_flush._ALERT_KINDS:
            assert row["progress"] is None and row["attempts"] == 0
    notify_cards.clear_restore_pending(str(w.data))
    # gated content was deferred out of the due window; it resumes after
    monkeypatch.setattr(notify_flush.time, "time",
                        lambda: 1_790_000_000.0 + notify_flush.RERENDER_RETRY_S)
    # Patient signal stays private-thread pending; the four other synthetic
    # content kinds retain their existing text route.
    assert notify_flush.flush(w.led, limit=20)["sent"] == 4


def test_reconcile_retains_unverifiable_text_after_marker_clears(text_world):
    w = text_world
    eid = w.led.outbox_add_tx("new_messages", None, {"message_ids": [1]}, route="text")
    w.led.db.commit()
    notify_cards.mark_restored(str(w.data))
    result = notify_reconcile.reconcile_after_restore(w.led, {})
    row = _row(w.led, eid)
    assert row["state"] == "failed" and row["next_try"] is None
    assert json.loads(row["progress"])["hold_reason"] == "restore_text_unverified"
    assert result["text_held"] == [
        {"event_id": eid, "kind": "new_messages", "reason": "restore_text_unverified"}]
    assert result["events_held"] == 1
    assert notify_cards.restore_pending(str(w.data)) is None
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
    notify_cards.mark_restored(str(w.data), now=1000.0)
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
    notify_cards.mark_restored(str(w.data), phase="awaiting_consent")
    result = notify_reconcile.reconcile_after_restore(w.led, {})
    assert result["skipped"] == "awaiting_consent"
    assert _row(w.led, eid)["state"] == "pending"
    assert notify_cards.restore_pending(str(w.data)) is not None


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
    notify_cards.mark_restored(str(w.data))

    def fail(*args):
        raise OSError("synthetic receipt failure")

    monkeypatch.setattr(notify_cards, "publish_file", fail)
    with pytest.raises(OSError, match="synthetic receipt failure"):
        notify_reconcile.reconcile_after_restore(w.led, {})
    assert notify_cards.restore_pending(str(w.data)) is not None
    assert _row(w.led, eid)["next_try"] is None
    assert not w.calls


def test_restore_marker_defers_content_so_later_alert_is_selected(text_world):
    w = text_world
    content = [w.led.outbox_add_tx("new_messages", None, {}, route="text") for _ in range(12)]
    alert = w.led.outbox_add_tx("run_failed", None, {}, route="text")
    w.led.db.commit()
    notify_cards.mark_restored(str(w.data))
    notify_flush.flush(w.led)
    assert _row(w.led, content[0])["attempts"] == 0
    notify_flush.flush(w.led)
    assert _row(w.led, alert)["state"] == "accepted"
    assert all(_row(w.led, eid)["state"] == "pending" for eid in content)


@pytest.mark.parametrize("kind", ["signal", "new_messages", "attachment_followup"])
def test_unknown_send_outcome_is_held_before_format_for_every_kind(text_world, monkeypatch, kind):
    w = text_world
    eid = w.led.outbox_add_tx(kind, None, {}, route="text")
    w.led.db.commit()
    w.led.outbox_progress(eid, 0, [], "fp", 1)

    def stale(*args):
        raise notify_flush._StaleSend("signal_not_open")

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
