"""Authentic persisted grants survive temporary LINE WORKS delivery holds."""
import asyncio
import copy
import json

import pytest

import ledger
import notify_cards
import notify_cmds
import notify_spool
from adapters.common import registry
from adapters.lineworks import delivery
from lineworks_testkit import CONFIG, world
from notify_testkit import NOW, _dispatch, _intent, _latest_render, _seed_thread


@pytest.mark.parametrize("hold", ["restore", "off", "epoch"])
@pytest.mark.parametrize("restart", [False, True])
def test_authentic_grant_is_not_retired_when_live_route_is_held(tmp_path, monkeypatch, hold, restart):
    monkeypatch.setattr(notify_cards.time, "time", lambda: NOW)
    data = tmp_path / "data"
    data.mkdir()
    led = ledger.Ledger(str(data / "ledger.db"))
    try:
        _seed_thread(led)
        assert _dispatch(led, _intent(led), CONFIG)["dispatched"]
        state = world(tmp_path)
        spec = json.loads(_latest_render(led)["spec_json"])

        def new_worker(worker_id, reg):
            return delivery.DeliveryWorker(
                sender=state.sender, settings=state.settings, root=str(data), reg=reg,
                worker_id=worker_id, log=lambda *args, **kwargs: None)

        async def scenario():
            worker = new_worker(registry.new_worker_id(), state.reg)
            assert worker.acquire_scope_lock()
            try:
                await worker.tick()
                notify_cmds.drain_int_commands(led, {}, CONFIG, str(data))
                claim = state.reg.claimed(spec["delivery_id"])
                attempt_id = claim["attempt_id"]
                grant = worker._read_begin_result(claim)
                assert grant is not None and grant["granted"] is True
                assert worker._verify_grant(claim, grant)
                assert led.db.execute(
                    "SELECT state FROM notification_delivery_attempts WHERE attempt_id=?",
                    (attempt_id,)).fetchone()[0] == "granted"
                held_config = copy.deepcopy(CONFIG)
                if hold == "restore":
                    (data / "restore_pending.json").write_text('{"synthetic":true}')
                elif hold == "off":
                    held_config["notify"]["interactive"] = "off"
                else:
                    held_config["notify"]["route_epoch"] += 1
                notify_spool.publish_flags(held_config, str(data))
                await worker.tick()
                held = state.reg.claimed(spec["delivery_id"])
                assert held is not None and held["attempt_id"] == attempt_id
                assert held["phase"] == "granted" and not state.reg.is_dead(spec["delivery_id"])
                assert state.client.calls == []
                assert led.db.execute(
                    "SELECT state FROM notification_delivery_attempts WHERE attempt_id=?",
                    (attempt_id,)).fetchone()[0] == "granted"
                if restart:
                    worker.release_scope_lock()
                    worker = new_worker(registry.new_worker_id(), registry.Registry(
                        state.dirs["state"], scope=state.settings))
                    assert worker.acquire_scope_lock()
                    recovered = await worker.reload_reconcile()
                    assert recovered["not_sent"] == 1 and recovered["unknown"] == 0
                    assert state.client.calls == []
                marker = data / "restore_pending.json"
                if marker.exists():
                    marker.unlink()
                notify_spool.publish_flags(CONFIG, str(data))
                # Reconcile may settle the unsent predecessor and issue one
                # fresh render; it never treats a started/unknown as unsent.
                for _ in range(5):
                    notify_cmds.drain_int_commands(led, {}, CONFIG, str(data))
                    await worker.tick()
                notify_cmds.drain_int_commands(led, {}, CONFIG, str(data))
                assert led.db.execute(
                    "SELECT delivery_state FROM notification_cards").fetchone()[0] == "delivered"
                assert sum(content[1].get("type") == "flex" for content in state.client.calls) == 1
                calls = list(state.client.calls)
                await worker.tick()
                assert state.client.calls == calls
            finally:
                worker.release_scope_lock()

        asyncio.run(scenario())
    finally:
        led.close()
