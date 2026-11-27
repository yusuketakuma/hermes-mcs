"""A collecting tick gives Slack a send grant before bulk history work."""
import asyncio
import json
import sys
import time
from contextlib import closing
from types import SimpleNamespace

import pytest

import collection_stages
import job_ops
import mcs_contract
import notify_flush
import run_check
from ingest_testkit import _message, _unread_patient
from ledger import Ledger
from run_check_stage_testkit import _point_run_check_at
from slack_testkit import SLACK, _granted_card, _mkworld


@pytest.mark.parametrize("source", ["unread", "self", "previous_tick"])
def test_arrival_reaches_slack_before_history_and_tail_still_delivers(tmp_path, monkeypatch, capsys, source):
    data = _point_run_check_at(tmp_path, monkeypatch)
    cfg = {**SLACK, "self_posts": source == "self", "deep_history": False}
    monkeypatch.setattr(run_check, "_config", lambda: cfg)
    monkeypatch.setattr(notify_flush, "_config", lambda: cfg)
    monkeypatch.setattr(notify_flush.mcs_util, "hermes_exe", lambda cfg: "/synthetic/missing")
    monkeypatch.setattr(sys, "argv", ["run_check"])
    patient = _unread_patient(1)
    arrival = _message(10, body="SYNTHETIC_ARRIVAL", unread=source != "self")
    with closing(Ledger(run_check.DB)) as store:
        store.upsert_patient_info(patient)
        store.save_messages([_message(1, posted_at="2020-01-01T00:00:00+00:00")], project_id=1)
        store.outbox_add("patient_history", 1, {"source": "initial_history"})
        if source == "previous_tick":
            store.save_messages([arrival], project_id=1, notify={"source": "unread"})

    events, sleeps = [], []
    def list_unread():
        events.append("list")
        return mcs_contract.UnreadSnapshot(timestamp=123, patients=[patient] if source == "unread" else [])

    adapter = SimpleNamespace(
        set_deadline=lambda deadline: None, list_unread=list_unread, list_projects=lambda: [patient],
        fetch_unread_messages=lambda *_: mcs_contract.MessageBatch([arrival], reached=True),
        fetch_unread_replies=lambda *_: mcs_contract.ReplyBatch([], []),
        fetch_latest=lambda pid: {"message_id": 10, "is_self_only": True},
        fetch_history=lambda *a, **k: mcs_contract.MessageBatch([arrival], reached=True),
        self_profile=lambda: {"sender_id": 1, "name": "synthetic", "stations": []})
    monkeypatch.setattr(run_check, "MCSAdapter", lambda **kwargs: adapter)
    monkeypatch.setattr(collection_stages, "outage_since", lambda: None)
    monkeypatch.setattr(run_check, "_semantic_enabled", lambda *args: False)

    world = None
    flush = notify_flush.flush
    def observe_flush(store, **kwargs):
        nonlocal world
        if kwargs["arrivals_after"] is not None:
            assert 0 < kwargs["deadline"] - time.monotonic() <= run_check.ARRIVAL_NOTIFY_BUDGET_S
        out = flush(store, **kwargs)
        if out.get("dispatched"):
            if world is None:
                assert (data / "flags" / "notify.json").is_file()
                world = _mkworld(store)
                assert asyncio.run(world.sender.bind())
            if source != "unread" or kwargs["arrivals_after"] is None:
                asyncio.run(world.worker.tick())
            events.append("publish")
        return out

    def pass_worker_poll(seconds):
        sleeps.append(seconds)
        asyncio.run(world.worker.tick())  # sends a real transport_begin command

    def backfill(adapter, store, *args, **kwargs):
        events.append("history")
        asyncio.run(world.worker.tick())  # requires a grant from the collecting tick
        creates = [kw for op, kw in world.client.calls if op == "create"]
        assert len(creates) == 1 and "SYNTHETIC_ARRIVAL" in json.dumps(creates)
        assert store.db.execute("SELECT state FROM notify_outbox WHERE kind='patient_history'").fetchone()[0] == "pending"

    def derive(store, *args, **kwargs):
        store.save_thread_replies([_message(20, body="SYNTHETIC_LATER", parent_id=10, unread=True)],
                                  1, notify={"source": "history"})

    monkeypatch.setattr(run_check.time, "sleep", pass_worker_poll)
    monkeypatch.setattr(notify_flush, "flush", observe_flush)
    monkeypatch.setattr(collection_stages, "stage_backfill", backfill)
    monkeypatch.setattr(run_check, "stage_derive", derive)
    monkeypatch.setattr(collection_stages, "stage_karte_summary", lambda *a, **k: None)
    monkeypatch.setattr(run_check, "_housekeeping", lambda *a: None)
    monkeypatch.setattr(run_check, "_write_health", lambda *a, **k: None)
    monkeypatch.setattr(run_check, "_finish_run", lambda *a, **k: "ok")
    drain = job_ops.drain_commands
    monkeypatch.setattr(job_ops, "drain_commands", lambda store, result: drain(store, result, str(data / "cmd")))

    code = run_check.main()
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result
    first = ["publish", "list"] if source == "previous_tick" else ["list", "publish"]
    assert events == first + ["history", "publish"]
    assert result["notify"]["dispatched"] == 2 and result["notify"]["suppressed"] == 1
    assert sleeps == ([3] if source == "unread" else [])
    with closing(Ledger(run_check.DB)) as store:
        asyncio.run(_granted_card(world.worker, store, world.root))
        states = [r[0] for r in store.db.execute("SELECT state FROM notify_outbox WHERE kind='new_messages'")]
        assert states == ["accepted", "accepted"]
        assert len(world.client.thread_posts) == 1
        assert "SYNTHETIC_LATER" in json.dumps(world.client.thread_posts)

@pytest.mark.parametrize("case", ["jobs_only", "no_notify", "history_only", "backoff", "unknown", "expired", "restore"])
def test_early_delivery_gates_do_not_flush_or_wait(tmp_path, monkeypatch, case):
    import notify_spool

    data = _point_run_check_at(tmp_path, monkeypatch)
    clock = time.monotonic()
    with closing(Ledger(run_check.DB)) as store:
        kind = "patient_history" if case == "history_only" else "new_messages"
        eid = store.outbox_add(kind, 1, {"message_ids": [1]})
        if case == "backoff":
            store.outbox_defer(eid, 3600)
        if case == "unknown":
            store.outbox_hold(eid, "send_outcome_unknown")
        if case == "restore":
            notify_spool.mark_restored(str(data), phase="restored")
        monkeypatch.setattr(collection_stages, "outage_since", lambda: None)
        monkeypatch.setattr(collection_stages, "stage_unread", lambda *a, **kw: None)
        monkeypatch.setattr(notify_flush, "flush", lambda *a, **kw: pytest.fail("unexpected early flush"))
        monkeypatch.setattr(run_check.time, "sleep", lambda *a: pytest.fail("unexpected worker wait"))
        args = SimpleNamespace(jobs_only=case == "jobs_only", no_notify=case == "no_notify",
                               mark_read=False, no_backfill=True)
        result = {"errors": []}
        before = tuple(store.db.execute("SELECT state,next_try,progress FROM notify_outbox").fetchone())
        run_check._stage_fetch(None, store, args, {}, result, clock + (0 if case == "expired" else 30), 1, False, None)
        assert not result["errors"]
        assert tuple(store.db.execute("SELECT state,next_try,progress FROM notify_outbox").fetchone()) == before


@pytest.mark.parametrize("fail_at", ["select", "restore_marker", "open_transaction"])
def test_early_delivery_error_preserves_collection_and_transaction_gate(tmp_path, monkeypatch, fail_at):
    import sqlite3
    import notify_spool

    _point_run_check_at(tmp_path, monkeypatch)
    collected = []
    with closing(Ledger(run_check.DB)) as store:
        store.outbox_add("new_messages", 1, {"message_ids": [1]})
        def marker(root):
            if fail_at == "open_transaction":
                store.db.execute("UPDATE notify_outbox SET attempts=17")
            raise OSError("synthetic fault")

        if fail_at == "select":
            store.db.set_authorizer(lambda op, table, *rest:
                                    sqlite3.SQLITE_DENY if op == sqlite3.SQLITE_READ and table == "notify_outbox"
                                    else sqlite3.SQLITE_OK)
        else:
            monkeypatch.setattr(notify_spool, "restore_pending", marker)
        monkeypatch.setattr(collection_stages, "outage_since", lambda: None)
        monkeypatch.setattr(collection_stages, "stage_unread", lambda *a, **kw: collected.append(1))
        monkeypatch.setattr(notify_flush, "flush", lambda *a, **kw: pytest.fail("unguarded flush"))
        args = SimpleNamespace(jobs_only=False, no_notify=False, mark_read=False, no_backfill=True)
        result = {"errors": []}
        if fail_at == "open_transaction":
            with pytest.raises(run_check.TickTransactionOpen):
                run_check._stage_fetch(None, store, args, {}, result, time.monotonic() + 30, 1, False, None)
            assert not collected and store.db.in_transaction
        else:
            run_check._stage_fetch(None, store, args, {}, result, time.monotonic() + 30, 1, False, None)
            assert collected == [1] and not store.db.in_transaction
        assert result["errors"][0].startswith("notify: ")

@pytest.mark.parametrize("carried", [True, False], ids=["all_carried", "sender_returns_after_fetch"])
def test_unavailable_text_window_cannot_hide_a_slack_arrival(tmp_path, monkeypatch, carried):
    _point_run_check_at(tmp_path, monkeypatch)
    sender = ["/synthetic/missing"]
    monkeypatch.setattr(notify_flush.mcs_util, "hermes_exe", lambda cfg: sender[0])
    monkeypatch.setattr(notify_flush, "_config", lambda: SLACK)
    monkeypatch.setattr(run_check.time, "sleep", lambda _: None)
    with closing(Ledger(run_check.DB)) as store:
        store.ensure_patient(1)
        with store.db:
            for mid in range(1, 11):
                store.outbox_add_tx("new_messages", 1, {"message_ids": [mid]}, route="text")
        # An unverified in-flight record must retain its original receipt and eligibility.
        store.outbox_progress(1, 0, [], "synthetic-fingerprint", sending=0)
        def old_rows():
            return list(map(tuple, store.db.execute(
                "SELECT event_id,state,next_try,attempts,progress FROM notify_outbox WHERE route='text' ORDER BY event_id")))

        initial = old_rows()
        result = {"errors": []}
        deadline = time.monotonic() + 100
        boundary = 0
        if not carried:
            boundary = run_check._deliver_arrivals(store, SLACK, result, deadline)
            assert boundary == 10 and old_rows() == initial
            sender[0] = "/usr/bin/true"  # would select the entire old window without the event boundary
        store.save_messages([_message(11, body="SYNTHETIC_NATIVE", unread=True)],
                            project_id=1, notify={"source": "unread"})
        run_check._deliver_arrivals(store, SLACK, result, deadline, after_event=boundary)
        world = _mkworld(store)
        asyncio.run(_granted_card(world.worker, store, world.root))
        posts = [kw for op, kw in world.client.calls if op == "create"]
        assert len(posts) == 1 and "SYNTHETIC_NATIVE" in json.dumps(posts)
        assert old_rows() == initial
        assert store.db.execute("SELECT state FROM notify_outbox WHERE route='interactive'").fetchone()[0] == "accepted"
        assert [r["event_id"] for r in store.outbox_due(10)] == list(range(1, 11))
