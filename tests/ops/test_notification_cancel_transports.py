"""Cancellation on Discord and LINE WORKS spools, with per-transport stop proof (synthetic only)."""
# ruff: noqa: F811  (tests take the imported `case` fixture as a parameter)
from __future__ import annotations

import json
import os

import pytest

import notification_cancel as cancel
from test_notification_cancel import (  # noqa: F401  (case is a fixture)
    FAKE_PID, _assert_cancelled, _request, _snapshot, case)


def _move_to(case, transport):
    """Re-home every target card and notice of the synthetic queue onto one transport."""
    db = case.led.db
    db.execute("UPDATE notification_cards SET transport=?", (transport,))
    db.execute("UPDATE notification_renders SET transport=? WHERE intent_event_id=?",
               (transport, case.events[-1]))
    db.commit()
    spool = case.data / f"{transport}_render"
    spool.mkdir(exist_ok=True)
    for delivery, _aid, _spec in case.notices:
        (case.data / "slack_render" / f"{delivery}.json").rename(spool / f"{delivery}.json")
    (case.data / "worker_status" / "slack-synthetic.json").unlink()
    return spool


def _archived(case, request, spool):
    archive = case.data / "notification_cancellations" / request["command_id"]
    for delivery, _aid, spec in case.notices:
        assert not (spool / f"{delivery}.json").exists()
        assert (archive / f"{delivery}.json").read_bytes() == spec
    assert all((case.data / "slack_render" / name).read_bytes() == spec
               for name, spec in case.old_specs.items())


def test_slack_plan_and_receipt_shape_is_unchanged(case):
    request = _request(case)
    assert all(set(e) == {"event_id", "kind", "state_sha256", "notice_specs"}
               for e in request["plan"]["events"])
    receipt = cancel.apply(case.root, request)
    assert set(receipt) == {"kind", "outcome", "command_id", "actor", "reason", "processed_at",
                            "event_ids", "backup", "plan", "notice_specs", "remote_operations"}
    _archived(case, request, case.data / "slack_render")


def test_discord_cancel_uses_its_spool_and_worker_witness(case):
    spool = _move_to(case, "discord")
    (case.data / "worker_status" / "discord-synthetic.json").write_text(json.dumps({
        "version": cancel.VERSION, "reason": "worker_started", "pid": FAKE_PID}))
    request = _request(case)
    receipt = cancel.apply(case.root, request)
    _assert_cancelled(case)
    assert receipt["remote_operations"] == 0
    _archived(case, request, spool)
    assert cancel.apply(case.root, request) == receipt


@pytest.mark.parametrize("transport", ["discord", "lineworks"])
def test_another_transports_proof_never_stands_in(case, transport):
    _move_to(case, transport)
    # A stopped Slack worker says nothing about this transport's sender.
    (case.data / "worker_status" / "slack-synthetic.json").write_text(json.dumps({
        "version": cancel.VERSION, "reason": "worker_started", "pid": FAKE_PID}))
    request = _request(case)
    before = _snapshot(case.led.db)
    with pytest.raises(ValueError, match="worker_stop_unverified"):
        cancel.apply(case.root, request)
    assert _snapshot(case.led.db) == before
    assert not (case.data / "notification_cancellations").exists()


def test_mixed_plan_needs_every_transports_proof(case):
    case.led.db.execute("UPDATE notification_cards SET transport='discord' WHERE card_id=1")
    case.led.db.commit()
    request = _request(case)
    before = _snapshot(case.led.db)
    with pytest.raises(ValueError, match="worker_stop_unverified"):
        cancel.apply(case.root, request)
    assert _snapshot(case.led.db) == before


def test_unknown_transport_stays_unsupported(case):
    case.led.db.execute("UPDATE notification_cards SET transport='synthetic-other' WHERE card_id=1")
    case.led.db.commit()
    with pytest.raises(ValueError, match="unsupported_transport"):
        cancel.build_plan(case.led.db, case.events)


def _adapter_lock(case):
    state = case.data / "lineworks_state"
    state.mkdir(exist_ok=True)
    path = state / "send-synthetic.lock"
    path.touch()
    return path


def test_lineworks_cancel_holds_the_adapter_scope_lock_throughout(case, monkeypatch):
    spool = _move_to(case, "lineworks")
    lock = _adapter_lock(case)
    withdraw = cancel._withdraw
    seen = []

    def during(data, receipt):
        # An adapter starting now must find its scope lock taken.
        seen.append(cancel.acquire_run_lock(str(lock)))
        withdraw(data, receipt)
    monkeypatch.setattr(cancel, "_withdraw", during)
    request = _request(case)
    receipt = cancel.apply(case.root, request)
    assert seen == [None]
    _assert_cancelled(case)
    assert receipt["remote_operations"] == 0
    _archived(case, request, spool)
    assert lock.exists()
    fd = cancel.acquire_run_lock(str(lock))  # released when apply returned
    assert fd is not None
    os.close(fd)


def test_running_lineworks_adapter_refuses(case):
    _move_to(case, "lineworks")
    adapter = cancel.acquire_run_lock(str(_adapter_lock(case)))
    request = _request(case)
    before = _snapshot(case.led.db)
    try:
        with pytest.raises(ValueError, match="worker_not_stopped"):
            cancel.apply(case.root, request)
    finally:
        os.close(adapter)
    assert _snapshot(case.led.db) == before
    assert not (case.data / "notification_cancellations").exists()
