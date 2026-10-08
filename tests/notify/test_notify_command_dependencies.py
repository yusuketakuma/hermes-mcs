"""Synthetic pre-HTTP recovery receipt must wait for a queued predecessor outside the scan window."""
import asyncio
import json
import os
import uuid
from pathlib import Path

import pytest

import notify_cards
import notify_cmds

from adapters.common import envelopes, registry, worker
from notify_testkit import (
    CFG, NOW, ORIGIN, SCOPE, _begin, _delivered_card, _dispatch, _intent,
    _latest_render, _seed_thread, _uuid, led,
)

__all__ = ["led"]


@pytest.fixture(autouse=True)
def pin_clock(monkeypatch):
    monkeypatch.setattr(notify_cards.time, "time", lambda: NOW)


def _queued_render(led):
    _seed_thread(led)
    assert _dispatch(led, _intent(led))["dispatched"]
    return _latest_render(led)


def _begin_req(render, n=9000):
    return {"version": 1, "op": "transport_begin", "command_id": _uuid(n),
            "attempt_id": f"{n:016x}", "worker_id": "dd" * 8,
            "delivery_id": render["delivery_id"], "render_rev": render["render_rev"],
            "payload_hash": render["payload_hash"], "route_epoch": 1, **SCOPE}


def _receipt_req(render, n, *, attempt=9000, result="not_sent"):
    req = {"version": 1, "op": "transport_receipt", "command_id": _uuid(n),
           "attempt_id": f"{attempt:016x}", "delivery_id": render["delivery_id"],
           "render_rev": render["render_rev"], "payload_hash": render["payload_hash"],
           "route_epoch": 1, "correlation": render["correlation"], **SCOPE,
           "result": result}
    if result == "delivered":
        req["message_id"] = "synthetic-remote"
    else:
        req["error_code"] = "synthetic-recovery"
    return req


def _put(queue, name, req):
    assert notify_cmds.validate_int(req) is None
    notify_cards.publish_file(str(queue), name, json.dumps(req).encode())


def test_recovery_receipt_does_not_orphan_a_begin_outside_the_512_name_window(
        led, tmp_path, monkeypatch):
    monkeypatch.setattr(notify_cards.time, "time", lambda: NOW)
    root = str(tmp_path / "data")
    notify_cards.ensure_dirs(root)
    notify_cards.publish_flags(CFG, root)
    _seed_thread(led)
    assert _dispatch(led, _intent(led))["dispatched"]
    state = str(Path(root) / "discord_state")
    oldreg = registry.Registry(state, scope=SCOPE)
    old = worker.DeliveryWorker(bot=None, settings=SCOPE, root=root, reg=oldreg,
                                worker_id=registry.new_worker_id(), log=lambda *a, **k: None)
    begin_id = uuid.UUID("ffffffff-ffff-4fff-8fff-fffffffffff0")
    receipt_id = uuid.UUID(_uuid(1))

    async def scenario():
        assert old.acquire_scope_lock()
        try:
            # Real claim/begin publication; this abstract worker cannot issue wire calls.
            monkeypatch.setattr(envelopes.uuid, "uuid4", lambda: begin_id)
            await old.tick()
            claim = next(iter(oldreg.claims().values()))
            assert claim["phase"] == "begin_sent"
            oldreg.save()
        finally:
            old.release_scope_lock()
        freshreg = registry.Registry(state, scope=SCOPE)
        fresh = worker.DeliveryWorker(bot=None, settings=SCOPE, root=root, reg=freshreg,
                                      worker_id=registry.new_worker_id(), log=lambda *a, **k: None)
        assert fresh.acquire_scope_lock()
        try:
            monkeypatch.setattr(envelopes.uuid, "uuid4", lambda: receipt_id)
            stats = await fresh.reconcile()
            assert stats["not_sent"] == 1 and stats["unknown"] == 0
            queue = Path(root) / "cmd_int"
            recovery = json.loads((queue / (str(receipt_id) + ".json")).read_text())
            assert recovery["attempt_id"] == claim["attempt_id"]
            assert recovery["result"] == "not_sent"
            for i in range(2, 513):
                # Existing intake/quarantine must process invalid traffic too.
                envelopes.publish_command(str(queue), {
                    "version": 1, "op": "bogus", "command_id": _uuid(i)})
            assert len(list(queue.glob("*.json"))) == 513
            for _ in range(20):
                result = {"errors": []}
                notify_cmds.drain_int_commands(led, result, CFG, root)
                assert result["errors"] == []
                if not list(queue.glob("*.json")):
                    break
            row = led.db.execute("SELECT state FROM notification_delivery_attempts WHERE attempt_id=?",
                                 (claim["attempt_id"],)).fetchone()
            assert row is not None and row["state"] == "not_sent", dict(row) if row else None
        finally:
            fresh.release_scope_lock()

    asyncio.run(scenario())


def test_512_uncertain_receipts_do_not_starve_a_later_healthy_begin(
        led, tmp_path, monkeypatch):
    render = _queued_render(led)
    root = tmp_path / "data"
    queue = root / "cmd_int"
    for n in range(1, 513):
        _put(queue, f"a{n:04}.json", _receipt_req(render, n, attempt=10000 + n))
    _put(queue, "z.json", _begin_req(render))
    reads = []
    original = notify_cmds.mcs_requests.read_command

    def read(path):
        if str(path).endswith(".json"):
            reads.append(path)
        return original(path)

    monkeypatch.setattr(notify_cmds.mcs_requests, "read_command", read)
    first = {"errors": []}
    assert notify_cmds.drain_int_commands(led, first, CFG, str(root)) == 0
    assert len(reads) == 512 and first["errors"] == []
    # The next independent drain restores its position from disk.
    reads.clear()
    second = {"errors": []}
    assert notify_cmds.drain_int_commands(led, second, CFG, str(root)) == 1
    assert len(reads) == 512 and second["errors"] == []
    row = led.db.execute("SELECT state FROM notification_delivery_attempts WHERE attempt_id=?",
                         (f"{9000:016x}",)).fetchone()
    assert row["state"] == "granted"
    assert len(list(queue.glob("*.json"))) == 512
    assert len(list((root / "cmd_results").glob("*.json"))) == 1


def test_waiting_receipts_do_not_spend_the_32_ready_command_budget(led, tmp_path):
    _delivered_card(led)
    render = _latest_render(led)
    root = tmp_path / "data"
    queue = root / "cmd_int"
    for n in range(1, 41):
        _put(queue, f"a{n:04}.json", _receipt_req(render, n, attempt=10000 + n))
    for n in range(1, 34):
        req = {"version": 1, "op": "refresh", "command_id": _uuid(20000 + n),
               "actor": "synthetic-operator", "origin": dict(ORIGIN, message_id="m-9")}
        _put(queue, f"z{n:04}.json", req)
    result = {"errors": []}
    assert notify_cmds.drain_int_commands(led, result, CFG, str(root)) == 32
    assert result["commands"] == 32 and result["errors"] == []
    for n in range(1, 33):
        receipt = json.loads((root / "cmd_results" / (_uuid(20000 + n) + ".json")).read_text())
        assert receipt["outcome"] == "applied"
    assert len(list(queue.glob("a*.json"))) == 40
    assert len(list(queue.glob("z*.json"))) == 1


def test_result_publication_failure_keeps_dispatch_bounded_and_retry_idempotent(
        led, tmp_path, monkeypatch):
    _delivered_card(led)
    root = tmp_path / "data"
    queue = root / "cmd_int"
    for n in range(1, 34):
        _put(queue, f"z{n:04}.json", {
            "version": 1, "op": "refresh", "command_id": _uuid(20000 + n),
            "actor": "synthetic-operator", "origin": dict(ORIGIN, message_id="m-9")})
    dispatch = notify_cmds.dispatch
    calls = []

    def record_dispatch(*args, **kwargs):
        calls.append(args[1]["command_id"])
        return dispatch(*args, **kwargs)

    publish = notify_cards.publish_file

    def fail_result(directory, name, raw):
        if Path(directory).name == "cmd_results":
            raise OSError("synthetic-result-write")
        return publish(directory, name, raw)

    monkeypatch.setattr(notify_cmds, "dispatch", record_dispatch)
    monkeypatch.setattr(notify_cards, "publish_file", fail_result)
    result = {"errors": []}
    assert notify_cmds.drain_int_commands(led, result, CFG, str(root)) == 0
    assert len(calls) == 32
    assert len(result["errors"]) == 32
    assert all(error.startswith("result_publish_failed:") for error in result["errors"])
    assert result.get("commands", 0) == 0
    assert len(list(queue.glob("*.json"))) == 33
    assert {r[0] for r in led.db.execute(
        "SELECT command_id FROM command_receipts WHERE json_extract(receipt_json,'$.kind')='refresh'"
    )} == {_uuid(20000 + n) for n in range(1, 33)}
    render_count = led.db.execute("SELECT count(*) FROM notification_renders").fetchone()[0]
    monkeypatch.setattr(notify_cards, "publish_file", publish)
    calls.clear()
    assert notify_cmds.drain_int_commands(led, {}, CFG, str(root)) == 32
    assert len(calls) == 32
    assert led.db.execute("SELECT count(*) FROM notification_renders").fetchone()[0] == render_count
    assert len(list(queue.glob("*.json"))) == 1
    assert notify_cmds.drain_int_commands(led, {}, CFG, str(root)) == 1
    assert not list(queue.glob("*.json"))
    assert {r[0] for r in led.db.execute(
        "SELECT command_id FROM command_receipts WHERE json_extract(receipt_json,'$.kind')='refresh'"
    )} == {_uuid(20000 + n) for n in range(1, 34)}


@pytest.mark.parametrize("cursor", [
    "{bad", "null", '{"after":true}', '{"after":"../z.json"}',
    '{"after":"z.json","extra":1}', '{"after":"z.json"}',
    '{"after":"0.json"}', '[' * 1100 + ']' * 1100,
    '{"after":"' + 'a' * 17000 + '.json"}',
])
def test_corrupt_or_boundary_cursor_cannot_skip_normal_validation(
        led, tmp_path, cursor):
    render = _queued_render(led)
    root = tmp_path / "data"
    queue = root / "cmd_int"
    (queue / notify_cmds._DRAIN_CURSOR).write_text(cursor)
    _put(queue, "a.json", _begin_req(render))
    assert notify_cmds.drain_int_commands(led, {}, CFG, str(root)) == 1
    assert led.db.execute("SELECT state FROM notification_delivery_attempts WHERE attempt_id=?",
                          (f"{9000:016x}",)).fetchone()["state"] == "granted"


def test_cursor_symlink_is_not_followed_or_modified(led, tmp_path):
    render = _queued_render(led)
    root = tmp_path / "data"
    queue = root / "cmd_int"
    target = tmp_path / "synthetic-cursor-target"
    target.write_text('{"after":"z.json"}')
    (queue / notify_cmds._DRAIN_CURSOR).symlink_to(target)
    _put(queue, "a.json", _begin_req(render))
    assert notify_cmds.drain_int_commands(led, {}, CFG, str(root)) == 1
    assert target.read_text() == '{"after":"z.json"}'
    assert not (queue / notify_cmds._DRAIN_CURSOR).is_symlink()


def test_cursor_publication_failure_is_reported_without_changing_receipts(
        led, tmp_path, monkeypatch):
    render = _queued_render(led)
    root = tmp_path / "data"
    queue = root / "cmd_int"
    _put(queue, "a.json", _begin_req(render))
    publish = notify_cards.publish_file

    def fail_cursor(directory, name, raw):
        if name == notify_cmds._DRAIN_CURSOR:
            raise OSError("synthetic-cursor-write")
        return publish(directory, name, raw)

    monkeypatch.setattr(notify_cards, "publish_file", fail_cursor)
    result = {"errors": []}
    assert notify_cmds.drain_int_commands(led, result, CFG, str(root)) == 1
    assert result["errors"] == ["cmd_int_cursor_publish_failed:OSError"]
    receipt = json.loads((root / "cmd_results" / (_uuid(9000) + ".json")).read_text())
    assert receipt["granted"]
    assert notify_cmds.drain_int_commands(led, {}, CFG, str(root)) == 0


@pytest.mark.parametrize("result", ["delivered", "not_sent", "unknown"])
def test_ancient_orphan_receipt_retains_its_factual_witness(led, tmp_path, result):
    render = _queued_render(led)
    root = tmp_path / "data"
    queue = root / "cmd_int"
    req = _receipt_req(render, 1, result=result)
    _put(queue, "a.json", req)
    os.utime(queue / "a.json", (NOW - 86400 * 100, NOW - 86400 * 100))
    for _ in range(3):
        assert notify_cmds.drain_int_commands(led, {}, CFG, str(root)) == 0
    assert json.loads((queue / "a.json").read_text()) == req
    assert not list((root / "cmd_results").glob("*.json"))
    assert not led.db.execute("SELECT 1 FROM notification_delivery_attempts").fetchall()


def test_invalid_receipt_is_quarantined_and_existing_attempt_echo_gate_remains(
        led, tmp_path):
    render = _queued_render(led)
    root = tmp_path / "data"
    queue = root / "cmd_int"
    invalid = _receipt_req(render, 1)
    invalid["payload_hash"] = "invalid"
    (queue / "a.json").write_text(json.dumps(invalid))
    assert notify_cmds.drain_int_commands(led, {}, CFG, str(root)) == 1
    assert (queue / "a.json.invalid").exists()
    assert _begin(led, render, n=9000)["granted"]
    mismatch = _receipt_req(render, 2)
    mismatch["payload_hash"] = "f" * 64
    _put(queue, "b.json", mismatch)
    assert notify_cmds.drain_int_commands(led, {}, CFG, str(root)) == 1
    receipt = json.loads((root / "cmd_results" / (_uuid(2) + ".json")).read_text())
    assert not receipt["applied"] and receipt["error"] == "payload_hash_mismatch"
    assert led.db.execute("SELECT state FROM notification_delivery_attempts WHERE attempt_id=?",
                          (f"{9000:016x}",)).fetchone()["state"] == "granted"
