"""Synthetic startup recovery must not infer non-delivery from missing evidence."""
import asyncio
import json
from pathlib import Path

import ledger
import notify_cards
import notify_cmds
import pytest

from adapters.common import envelopes, journal, registry, worker as common_worker
from adapters.lineworks import delivery
from notify_testkit import NOW, _dispatch, _intent, _seed_thread
from test_lineworks_adapter import CONFIG, overflow_spec, world


@pytest.mark.parametrize("phase", ["begin_sent", "granted"])
@pytest.mark.parametrize("damage", ["torn", "deep", "bad_phase", "missing"])
def test_restart_missing_send_witness_stays_unknown(tmp_path, monkeypatch, phase, damage):
    monkeypatch.setattr(notify_cards.time, "time", lambda: NOW)
    data = tmp_path / "data"
    data.mkdir()
    led = ledger.Ledger(str(data / "ledger.db"))
    try:
        _seed_thread(led)
        assert _dispatch(led, _intent(led), CONFIG)["dispatched"]
        w = world(tmp_path)
        old = delivery.DeliveryWorker(sender=w.sender, settings=w.settings, root=str(data),
                                      reg=w.reg, worker_id=registry.new_worker_id(),
                                      log=lambda *a, **kw: None)

        async def crash_and_restart():
            assert old.acquire_scope_lock()
            try:
                await old.tick()
                notify_cmds.drain_int_commands(led, {"errors": []}, CONFIG, str(data))
                claim = next(iter(w.reg.claims().values()))
                claim["phase"] = phase
                w.reg.claim(claim["spec"]["delivery_id"], claim)
                w.reg.save()
                # A real synthetic send commits; its started witness is then lost.
                assert (await old._perform(claim))["result"] == "delivered"
                witness = Path(old._journal_path())
                if damage == "missing":
                    witness.unlink()
                else:
                    row = {"phase": "started", "attempt_id": claim["attempt_id"],
                           "delivery_id": claim["spec"]["delivery_id"]}
                    if damage == "torn":
                        tail = json.dumps(row).encode()[:-1]
                    elif damage == "deep":
                        tail = b"[" * 20000 + b"]" * 20000 + b"\n"
                    else:
                        tail = json.dumps({**row, "phase": "start-corrupt"}).encode() + b"\n"
                    with witness.open("ab") as handle:
                        handle.write(tail)
            finally:
                old.release_scope_lock()
            resumed_reg = registry.Registry(w.dirs["state"], scope=w.settings)
            resumed_reg.reload()
            resumed = delivery.DeliveryWorker(sender=w.sender, settings=w.settings, root=str(data),
                                              reg=resumed_reg, worker_id=registry.new_worker_id(),
                                              log=lambda *a, **kw: None)
            assert resumed.acquire_scope_lock()
            try:
                stats = await resumed.reconcile()
                assert stats["unknown"] == 1 and stats["not_sent"] == 0
                receipts = [json.loads(p.read_text())
                            for p in Path(w.dirs["cmd_int"]).glob("*.json")]
                assert len(receipts) == 1 and receipts[0]["result"] == "unknown"
                assert receipts[0]["attempt_id"] == claim["attempt_id"]
                result = {"errors": []}
                notify_cmds.drain_int_commands(led, result, CONFIG, str(data))
                assert result["errors"] == []
                assert led.db.execute("SELECT delivery_state FROM notification_cards").fetchone()[0] == "delivery_unknown"
                assert resumed_reg.claims() == {}
                assert resumed_reg.is_dead(claim["spec"]["delivery_id"])
                await resumed.tick()
                await resumed.reconcile()
                await resumed.tick()
                assert len(w.client.calls) == 1
            finally:
                resumed.release_scope_lock()

        asyncio.run(crash_and_restart())
    finally:
        led.close()


def test_journal_scan_retains_good_rows_around_deep_damage(tmp_path):
    journal.append(str(tmp_path), "synthetic", {"phase": "begin", "attempt_id": "a"})
    with Path(journal._path(str(tmp_path), "synthetic")).open("ab") as handle:
        handle.write(b"[" * 20000 + b"]" * 20000 + b"\n")
    journal.append(str(tmp_path), "synthetic", {"phase": "started", "attempt_id": "b"})
    assert set(journal.scan(str(tmp_path))) == {"a", "b"}


def test_restart_corrupt_part_witness_never_reposts_the_part(tmp_path):
    w = world(tmp_path)
    spec = overflow_spec()
    mid = "lw:" + "c" * 32
    spec["delivery"]["thread_id"] = mid
    Path(w.dirs["render"], spec["delivery_id"] + ".json").write_bytes(envelopes.canonical(spec))
    claim = {"attempt_id": "a" * 32, "worker_id": "synthetic-old", "spec": spec,
             "payload_hash": envelopes.payload_hash(spec), "phase": "settled"}
    worker = delivery.DeliveryWorker(sender=w.sender, settings=w.settings, root=str(w.data),
                                     reg=w.reg, worker_id=registry.new_worker_id(),
                                     log=lambda *a, **kw: None)

    async def scenario():
        assert (await w.sender.perform(spec))["result"] == "delivered"
        part = spec["parts"]["manifest"][1]
        assert (await worker._perform_part(claim, part, {"card_message_id": mid}))["result"] == "delivered"
        for phase in ("result", "receipt"):
            journal.append(w.dirs["state"], "synthetic-old", {
                "phase": phase, "attempt_id": claim["attempt_id"],
                "delivery_id": spec["delivery_id"], "result": "delivered", "message_id": mid,
                "receipt_envelope": envelopes.transport_receipt(claim, "delivered", message_id=mid)})
        with Path(journal._path(w.dirs["state"], "synthetic-old")).open("ab") as handle:
            handle.write(b'{"phase":"started","attempt_id":"torn-part')
        w.reg.mark_dead(spec["delivery_id"])
        assert worker.acquire_scope_lock()
        try:
            await worker.reconcile()
            assert common_worker._card_message_id(worker._jview.refresh(), spec["delivery_id"]) == mid
            await worker.tick()
            await worker.tick()
            assert len(w.client.calls) == 2
            assert not w.reg.parts_done(spec["delivery_id"])
        finally:
            worker.release_scope_lock()

    asyncio.run(scenario())


def test_clean_pre_http_receipt_remains_claimable_across_two_restarts(tmp_path):
    w = world(tmp_path)
    spec = overflow_spec()
    Path(w.dirs["render"], spec["delivery_id"] + ".json").write_bytes(envelopes.canonical(spec))

    async def scenario():
        old = delivery.DeliveryWorker(sender=w.sender, settings=w.settings, root=str(w.data),
                                      reg=w.reg, worker_id=registry.new_worker_id(),
                                      log=lambda *a, **kw: None)
        await old.tick()
        assert w.reg.claimed(spec["delivery_id"])["phase"] == "begin_sent"
        for index in range(2):
            reg = registry.Registry(w.dirs["state"], scope=w.settings)
            resumed = delivery.DeliveryWorker(sender=w.sender, settings=w.settings, root=str(w.data),
                                              reg=reg, worker_id=registry.new_worker_id(),
                                              log=lambda *a, **kw: None)
            stats = await resumed.reconcile()
            assert stats["not_sent"] == (1 if index == 0 else 0)
            assert not reg.is_dead(spec["delivery_id"])
            assert reg.claims() == {}
        assert w.client.calls == []

    asyncio.run(scenario())
