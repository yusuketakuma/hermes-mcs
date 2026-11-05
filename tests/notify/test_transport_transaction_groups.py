"""Bounded transport transactions commit before any observable result or file effect."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

import notify_cmds
import notify_spool
import notify_transport
from notify_testkit import (
    CFG, NOW, SCOPE, _begin, _dispatch, _intent, _latest_render,
    _seed_thread, _uuid, led, pinned_clock,
)

__all__ = ["led", "pinned_clock"]
pytestmark = pytest.mark.usefixtures("pinned_clock", "v1_thread_bodies")


def _requests(led, count, *, receipts=False):
    requests = []
    for n in range(1, count + 1):
        mid = 100 + n
        _seed_thread(led, pid=n, root=mid, mids=(mid,))
        assert _dispatch(led, _intent(led, pid=n, payload={"message_ids": [mid]}))["dispatched"]
        render = _latest_render(led, n)
        begin = {"version": 1, "op": "transport_begin", "command_id": _uuid(n),
                 "attempt_id": f"{n:016x}", "worker_id": "bb" * 8,
                 "delivery_id": render["delivery_id"], "render_rev": render["render_rev"],
                 "payload_hash": render["payload_hash"], "route_epoch": 1, **SCOPE}
        if receipts:
            assert _begin(led, render, n=n)["granted"]
            requests.append(_receipt_request(begin, render, n))
        else:
            requests.append(begin)
    return requests


def _receipt_request(begin, render, n):
    return {**{key: value for key, value in begin.items() if key != "worker_id"},
            "op": "transport_receipt", "command_id": _uuid(1000 + n),
            "correlation": render["correlation"], "result": "delivered",
            "message_id": f"remote-{n}"}


def _queue(led, requests):
    root = Path(notify_spool.data_root(led))
    notify_spool.ensure_dirs(str(root))
    for n, req in enumerate(requests):
        assert notify_cmds.validate_int(req) is None
        notify_spool.publish_file(str(root / "cmd_int"), f"{n:04}.json",
                                  json.dumps(req).encode())
    return root


def _answers(root):
    return [json.loads(path.read_text()) for path in sorted((root / "cmd_results").glob("*.json"))]


@pytest.mark.parametrize("receipts", [False, True])
def test_33_commands_use_two_commits_and_publish_only_committed_facts(led, monkeypatch, receipts):
    requests = _requests(led, 33, receipts=receipts)
    root = _queue(led, requests)
    trace = []
    led.db.set_trace_callback(trace.append)
    publish = notify_spool.publish_file
    observed = []
    def check(directory, name, raw):
        if Path(directory).name == "cmd_results":
            assert not led.db.in_transaction
            answer = json.loads(raw)
            row = led.db.execute("SELECT state FROM notification_delivery_attempts WHERE attempt_id=?",
                                 (answer["attempt_id"],)).fetchone()
            observed.append(row[0])
            assert row[0] == ("delivered" if receipts else "granted")
        return publish(directory, name, raw)
    monkeypatch.setattr(notify_spool, "publish_file", check)
    try:
        assert notify_cmds.drain_int_commands(led, {}, CFG, str(root), limit=100) == 33
    finally:
        led.db.set_trace_callback(None)
    assert sum(sql == "BEGIN IMMEDIATE" for sql in trace) == 2
    assert sum(sql == "COMMIT" for sql in trace) == 2
    assert len(observed) == len(_answers(root)) == 33
    assert not list((root / "cmd_int").glob("*.json"))


@pytest.mark.parametrize("error_class", [sqlite3.IntegrityError, sqlite3.DataError, RuntimeError])
def test_item_error_rolls_back_only_its_savepoint_and_file_effects(led, monkeypatch, error_class):
    requests = _requests(led, 3)
    root = _queue(led, requests)
    original = notify_transport.apply_transport_begin_tx
    effects = []
    def apply(ledger, req, cfg, now, specs, unlinks):
        out = original(ledger, req, cfg, now, specs, unlinks)
        if req["attempt_id"] == requests[1]["attempt_id"]:
            specs.append({"forbidden": "rolled back"})
            unlinks.append(("forbidden", "discord"))
            raise error_class("synthetic failure")
        return out
    def publish(_ledger, specs, unlinks, _now):
        assert not led.db.in_transaction
        effects.append((list(specs), list(unlinks)))
    monkeypatch.setattr(notify_transport, "apply_transport_begin_tx", apply)
    monkeypatch.setattr(notify_transport, "publish_transport_effects", publish)
    assert notify_cmds.drain_int_commands(led, {}, CFG, str(root)) == 3
    assert effects == [([], []), ([], []), ([], [])]
    assert {row[0] for row in led.db.execute("SELECT attempt_id FROM notification_delivery_attempts")} == {
        requests[0]["attempt_id"], requests[2]["attempt_id"]}
    answer = next(row for row in _answers(root) if row["command_id"] == requests[1]["command_id"])
    assert answer["outcome"] == "rejected"
    assert answer["error"] == ("crash:RuntimeError" if error_class is RuntimeError
                               else f"storage_rejected:{error_class.__name__}")
    assert (root / "cmd_int" / "0001.json.invalid").exists() is (error_class is not RuntimeError)


@pytest.mark.parametrize("failure", ["busy", "commit"])
def test_storage_or_commit_failure_keeps_the_whole_group_unpublished(led, monkeypatch, failure):
    requests = _requests(led, 3)
    root = _queue(led, requests)
    original = notify_transport.apply_transport_begin_tx
    effects = []
    monkeypatch.setattr(notify_transport, "publish_transport_effects", lambda *a: effects.append(a))
    if failure == "busy":
        def apply(*args):
            out = original(*args)
            if args[1]["attempt_id"] == requests[1]["attempt_id"]:
                raise sqlite3.OperationalError("synthetic busy")
            return out
        monkeypatch.setattr(notify_transport, "apply_transport_begin_tx", apply)
    else:
        led.db.set_authorizer(lambda action, arg, *_: sqlite3.SQLITE_DENY
                              if action == sqlite3.SQLITE_TRANSACTION and arg == "COMMIT"
                              else sqlite3.SQLITE_OK)
    try:
        with pytest.raises(sqlite3.Error):
            notify_cmds.drain_int_commands(led, {}, CFG, str(root))
    finally:
        led.db.set_authorizer(None)
    assert not led.db.in_transaction
    assert led.db.execute("SELECT COUNT(*) FROM notification_delivery_attempts").fetchone()[0] == 0
    assert not _answers(root) and effects == []
    assert len(list((root / "cmd_int").glob("*.json"))) == 3
    monkeypatch.setattr(notify_transport, "apply_transport_begin_tx", original)
    assert notify_cmds.drain_int_commands(led, {}, CFG, str(root)) == 3
    assert led.db.execute("SELECT COUNT(*) FROM notification_delivery_attempts").fetchone()[0] == 3


@pytest.mark.parametrize("failure", ["effect", "result", "consume"])
def test_post_commit_failure_keeps_facts_and_replay_never_creates_a_second_attempt(
        led, monkeypatch, failure):
    requests = _requests(led, 2)
    root = _queue(led, requests)
    effects = notify_transport.publish_transport_effects
    publish = notify_spool.publish_file
    unlink = notify_cmds.os.unlink
    failed = []
    def fail_effect(*args):
        if not failed:
            failed.append(True)
            raise OSError("synthetic effect")
        return effects(*args)
    def fail_result(directory, name, raw):
        if Path(directory).name == "cmd_results" and not failed:
            failed.append(True)
            raise OSError("synthetic result")
        return publish(directory, name, raw)
    def fail_consume(path, *args, **kw):
        if str(path).endswith("0000.json") and not failed:
            failed.append(True)
            raise OSError("synthetic consume")
        return unlink(path, *args, **kw)
    monkeypatch.setattr(notify_transport, "publish_transport_effects", fail_effect if failure == "effect" else effects)
    monkeypatch.setattr(notify_spool, "publish_file", fail_result if failure == "result" else publish)
    monkeypatch.setattr(notify_cmds.os, "unlink", fail_consume if failure == "consume" else unlink)
    result = {}
    assert notify_cmds.drain_int_commands(led, result, CFG, str(root)) == 1
    assert len(list((root / "cmd_int").glob("*.json"))) == 1
    assert led.db.execute("SELECT COUNT(*) FROM notification_delivery_attempts").fetchone()[0] == 2
    assert all(row["granted"] for row in _answers(root))
    assert result["errors"]
    monkeypatch.setattr(notify_transport, "publish_transport_effects", effects)
    monkeypatch.setattr(notify_spool, "publish_file", publish)
    monkeypatch.setattr(notify_cmds.os, "unlink", unlink)
    assert notify_cmds.drain_int_commands(led, {}, CFG, str(root)) == 1
    assert led.db.execute("SELECT COUNT(*) FROM notification_delivery_attempts").fetchone()[0] == 2
    assert len(_answers(root)) == 2 and all(row["granted"] for row in _answers(root))


def test_deadline_commits_finished_items_and_leaves_the_rest(led, monkeypatch):
    requests = _requests(led, 3)
    root = _queue(led, requests)
    clock = [0.0]
    monkeypatch.setattr(notify_cmds, "time", SimpleNamespace(monotonic=lambda: clock[0], time=lambda: NOW))
    original = notify_transport.apply_transport_begin_tx
    def apply(*args):
        out = original(*args)
        clock[0] = 2.0
        return out
    monkeypatch.setattr(notify_transport, "apply_transport_begin_tx", apply)
    result = {}
    assert notify_cmds.drain_int_commands(led, result, CFG, str(root), deadline=1.0) == 1
    assert result["errors"] == ["cmd_int_drain_deadline"]
    assert len(_answers(root)) == 1
    assert len(list((root / "cmd_int").glob("*.json"))) == 2


@pytest.mark.parametrize("barrier", ["part_receipt", "thread_receipt", "notification"])
def test_unary_operations_and_begin_to_receipt_are_commit_barriers(led, monkeypatch, barrier):
    requests = _requests(led, 2)
    root = Path(notify_spool.data_root(led))
    pending = [("first.json", requests[0]), ("barrier.json", {"version": 99, "op": barrier}),
               ("last.json", requests[1])]
    trace = []
    led.db.set_trace_callback(trace.append)
    try:
        outcomes = list(notify_cmds._command_outcomes(led, pending, CFG, str(root), {}, None, 32))
    finally:
        led.db.set_trace_callback(None)
    assert len(outcomes) == 3
    assert sum(sql == "COMMIT" for sql in trace) == 2
    assert outcomes[1][2] is not None


def test_queued_receipt_observes_its_predecessor_only_after_grant_commit(led, monkeypatch):
    begin = _requests(led, 1)[0]
    render = _latest_render(led)
    receipt = _receipt_request(begin, render, 1)
    root = _queue(led, [receipt, begin])
    observed = []
    original = notify_spool.publish_file
    def publish(directory, name, raw):
        if Path(directory).name == "cmd_results":
            answer = json.loads(raw)
            state = led.db.execute("SELECT state FROM notification_delivery_attempts").fetchone()[0]
            observed.append((answer.get("granted"), state))
        return original(directory, name, raw)
    monkeypatch.setattr(notify_spool, "publish_file", publish)
    assert notify_cmds.drain_int_commands(led, {}, CFG, str(root)) == 2
    assert observed == [(True, "granted"), (None, "delivered")]


def test_unknown_receipt_stays_queued_without_spending_the_ready_limit(led):
    begin = _requests(led, 1)[0]
    render = _latest_render(led)
    receipt = {**_receipt_request(begin, render, 1), "attempt_id": "f" * 16,
               "message_id": "unproven"}
    root = _queue(led, [receipt, begin])
    assert notify_cmds.drain_int_commands(led, {}, CFG, str(root)) == 1
    assert len(list((root / "cmd_int").glob("*.json"))) == 1
    assert len(_answers(root)) == 1


def test_restore_hold_still_refuses_every_group_grant(led):
    requests = _requests(led, 2)
    root = _queue(led, requests)
    notify_spool.mark_restored(str(root))
    assert notify_cmds.drain_int_commands(led, {}, CFG, str(root)) == 2
    assert led.db.execute("SELECT COUNT(*) FROM notification_delivery_attempts").fetchone()[0] == 0
    assert all(row["error"] == "denied_restore_pending" for row in _answers(root))


def test_stale_source_replan_and_old_spec_cleanup_happen_only_after_commit(led, monkeypatch):
    requests = _requests(led, 1)
    root = _queue(led, requests)
    with led.db:
        led.db.execute("UPDATE messages SET content_hash=? WHERE message_id=101", ("e" * 64,))
    published, removed = [], []
    publish = notify_spool.publish_file
    unlink = notify_transport._unlink_spec
    def publish_check(directory, name, raw):
        if Path(directory).name == "discord_render":
            assert not led.db.in_transaction
            published.append(name)
        return publish(directory, name, raw)
    def remove_check(*args):
        assert not led.db.in_transaction
        removed.append(args[1])
        return unlink(*args)
    monkeypatch.setattr(notify_spool, "publish_file", publish_check)
    monkeypatch.setattr(notify_transport, "_unlink_spec", remove_check)
    assert notify_cmds.drain_int_commands(led, {}, CFG, str(root)) == 1
    assert published and removed == [requests[0]["delivery_id"]]
    assert _answers(root)[0]["granted"] is False


@pytest.mark.parametrize("owner", ["begin", "receipt", "drain"])
def test_existing_caller_transaction_is_not_committed_or_rolled_back(led, owner):
    req = _requests(led, 1, receipts=owner == "receipt")[0]
    root = _queue(led, [req])
    before = led.db.execute("SELECT updated_at FROM notification_cards").fetchone()[0]
    led.db.execute("BEGIN IMMEDIATE")
    led.db.execute("UPDATE notification_cards SET updated_at=42")
    try:
        with pytest.raises(sqlite3.ProgrammingError, match="transport_transaction_already_active"):
            if owner == "begin":
                notify_transport.apply_transport_begin(led, req, CFG, now=NOW)
            elif owner == "receipt":
                notify_transport.apply_transport_receipt(led, req, CFG, now=NOW)
            else:
                notify_cmds.drain_int_commands(led, {}, CFG, str(root))
        assert led.db.in_transaction
        assert led.db.execute("SELECT updated_at FROM notification_cards").fetchone()[0] == 42
        assert not _answers(root)
        assert len(list((root / "cmd_int").glob("*.json"))) == 1
    finally:
        led.db.rollback()
    assert led.db.execute("SELECT updated_at FROM notification_cards").fetchone()[0] == before


def test_begin_lock_failure_keeps_commands_and_results_unpublished(led):
    req = _requests(led, 1)[0]
    root = _queue(led, [req])
    database = led.db.execute("PRAGMA database_list").fetchone()["file"]
    timeout = led.db.execute("PRAGMA busy_timeout").fetchone()[0]
    other = sqlite3.connect(database)
    other.execute("BEGIN IMMEDIATE")
    led.db.execute("PRAGMA busy_timeout=1")
    try:
        with pytest.raises(sqlite3.OperationalError):
            notify_cmds.drain_int_commands(led, {}, CFG, str(root))
        assert not led.db.in_transaction
        assert not _answers(root)
        assert len(list((root / "cmd_int").glob("*.json"))) == 1
    finally:
        other.rollback()
        other.close()
        led.db.execute(f"PRAGMA busy_timeout={timeout}")


def test_unary_restore_denial_never_starts_a_write_transaction(led):
    req = _requests(led, 1)[0]
    root = _queue(led, [req])
    notify_spool.mark_restored(str(root))
    trace = []
    led.db.set_trace_callback(trace.append)
    try:
        answer = notify_transport.apply_transport_begin(led, req, CFG, now=NOW)
    finally:
        led.db.set_trace_callback(None)
    assert answer["error"] == "denied_restore_pending"
    assert not any(sql.startswith("BEGIN") for sql in trace)


def test_unknown_delivery_file_cleanup_does_not_escape_a_rolled_back_group(led, monkeypatch):
    requests = _requests(led, 2)
    requests[0] = {**requests[0], "delivery_id": _uuid(999)}
    root = _queue(led, requests)
    orphan = root / "discord_render" / (requests[0]["delivery_id"] + ".json")
    orphan.write_text("synthetic orphan")
    original = notify_transport.apply_transport_begin_tx
    def apply(*args):
        answer = original(*args)
        if args[1]["attempt_id"] == requests[1]["attempt_id"]:
            raise sqlite3.OperationalError("synthetic busy")
        return answer
    monkeypatch.setattr(notify_transport, "apply_transport_begin_tx", apply)
    with pytest.raises(sqlite3.OperationalError):
        notify_cmds.drain_int_commands(led, {}, CFG, str(root))
    assert orphan.exists() and orphan.read_text() == "synthetic orphan"
    assert not _answers(root)
    assert len(list((root / "cmd_int").glob("*.json"))) == 2
    assert led.db.execute("SELECT COUNT(*) FROM notification_delivery_attempts").fetchone()[0] == 0


def test_restore_hold_appearing_after_unary_preflight_is_rechecked_inside_tx(led, monkeypatch):
    req = _requests(led, 1)[0]
    observations = []
    def pending(_root):
        observations.append(led.db.in_transaction)
        return None if len(observations) == 1 else {"phase": "restored"}
    monkeypatch.setattr(notify_spool, "restore_pending", pending)
    answer = notify_transport.apply_transport_begin(led, req, CFG, now=NOW)
    assert observations == [False, True]
    assert answer["error"] == "denied_restore_pending"
    assert led.db.execute("SELECT COUNT(*) FROM notification_delivery_attempts").fetchone()[0] == 0
