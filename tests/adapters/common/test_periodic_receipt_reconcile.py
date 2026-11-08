"""Periodic receipt recovery never treats the current sender's grants as orphaned."""
import asyncio
import json
from pathlib import Path

import pytest

from adapters.common import envelopes, journal, worker as common_worker
from adapters.lineworks import delivery
from lineworks_testkit import overflow_spec, world


def _worker(w):
    return delivery.DeliveryWorker(
        sender=w.sender, settings=w.settings, root=str(w.data), reg=w.reg,
        worker_id="synthetic-current", log=lambda *a, **kw: None)


@pytest.mark.parametrize("phase", ["begin_sent", "granted", "started", "result"])
@pytest.mark.parametrize("missing_witness", [False, True])
def test_periodic_reconcile_preserves_current_claim(tmp_path, phase, missing_witness):
    w = world(tmp_path)
    worker = _worker(w)
    spec = overflow_spec()
    path = Path(w.dirs["render"], spec["delivery_id"] + ".json")
    path.write_bytes(envelopes.canonical(spec))

    async def scenario():
        assert worker.acquire_scope_lock()
        try:
            await worker._claim_spec(str(path), spec)
            claim = w.reg.claimed(spec["delivery_id"])
            claim["phase"] = phase
            w.reg.claim(spec["delivery_id"], claim)
            if missing_witness:
                Path(worker._journal_path()).unlink()
            before = set(Path(w.dirs["cmd_int"]).iterdir())
            assert await worker.reconcile(periodic=True) == {
                "receipt_republished": 0, "not_sent": 0, "unknown": 0}
            assert w.reg.claimed(spec["delivery_id"])["phase"] == phase
            assert not w.reg.is_dead(spec["delivery_id"])
            assert set(Path(w.dirs["cmd_int"]).iterdir()) == before
            assert not w.client.calls
        finally:
            worker.release_scope_lock()

    asyncio.run(scenario())


@pytest.mark.parametrize("result", [None, "delivered"])
def test_periodic_reconcile_recovers_old_receipt_once_without_sending(tmp_path, result):
    w = world(tmp_path)
    worker = _worker(w)
    spec = overflow_spec()
    claim = {"attempt_id": "a" * 32, "worker_id": "synthetic-old", "spec": spec,
             "payload_hash": envelopes.payload_hash(spec), "phase": "started",
             "spec_path": str(Path(w.dirs["render"], spec["delivery_id"] + ".json"))}
    w.reg.claim(spec["delivery_id"], claim)
    env = envelopes.transport_receipt(claim, "unknown")
    journal.append(w.dirs["state"], "synthetic-old", {
        "phase": "started", "attempt_id": claim["attempt_id"],
        "delivery_id": spec["delivery_id"], "receipt_envelope": env})
    if result is not None:
        journal.append(w.dirs["state"], "synthetic-old", {
            "phase": "result", "attempt_id": claim["attempt_id"],
            "delivery_id": spec["delivery_id"], "result": result,
            "message_id": "lw:" + "c" * 32})

    async def scenario():
        assert worker.acquire_scope_lock()
        try:
            stats = await worker.reconcile(periodic=True)
            assert stats["receipt_republished" if result else "unknown"] == 1
            assert w.reg.claims() == {}
            assert w.reg.is_dead(spec["delivery_id"])
            assert len(list(Path(w.dirs["cmd_int"]).glob("*.json"))) == 1
            assert not any((await worker.reconcile(periodic=True)).values())
            assert len(list(Path(w.dirs["cmd_int"]).glob("*.json"))) == 1
            assert not w.client.calls
        finally:
            worker.release_scope_lock()

    asyncio.run(scenario())


def test_periodic_reconcile_needs_scope_lock_and_backs_off_failure(tmp_path, monkeypatch):
    w = world(tmp_path)
    worker = _worker(w)
    now = [1000.0]
    monkeypatch.setattr(common_worker.time, "monotonic", lambda: now[0])
    worker._next_reconcile = 0
    calls = []

    async def fail(*, periodic):
        calls.append(periodic)
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(worker, "reconcile", fail)

    async def scenario():
        await worker._periodic_reconcile()
        assert not calls
        assert worker.acquire_scope_lock()
        try:
            with pytest.raises(OSError):
                await worker._periodic_reconcile()
            await worker._periodic_reconcile()
            assert calls == [True]
            now[0] += common_worker.RECONCILE_EVERY_S
            with pytest.raises(OSError):
                await worker._periodic_reconcile()
            assert calls == [True, True]
        finally:
            worker.release_scope_lock()

    asyncio.run(scenario())


def test_receipt_from_current_reconciler_does_not_hide_old_attempt(tmp_path):
    w = world(tmp_path)
    worker = _worker(w)
    spec = overflow_spec()
    claim = {"attempt_id": "b" * 32, "worker_id": "synthetic-old", "spec": spec,
             "payload_hash": envelopes.payload_hash(spec), "phase": "started",
             "spec_path": str(Path(w.dirs["render"], spec["delivery_id"] + ".json"))}
    w.reg.claim(spec["delivery_id"], claim)
    journal.append(w.dirs["state"], "synthetic-old", {
        "phase": "result", "attempt_id": claim["attempt_id"],
        "delivery_id": spec["delivery_id"], "result": "delivered",
        "receipt_envelope": envelopes.transport_receipt(claim, "delivered")})
    worker._journal("receipt", attempt_id=claim["attempt_id"],
                    delivery_id=spec["delivery_id"], result="delivered", reconcile=True)
    async def scenario():
        assert worker.acquire_scope_lock()
        try:
            assert not any((await worker.reconcile(periodic=True)).values())
            assert w.reg.claims() == {}
            assert not list(Path(w.dirs["cmd_int"]).glob("*.json"))
            assert not w.client.calls
        finally:
            worker.release_scope_lock()
    asyncio.run(scenario())


def test_duplicate_phase_never_turns_a_started_send_into_not_sent(tmp_path):
    w = world(tmp_path)
    worker = _worker(w)
    spec = overflow_spec()
    claim = {"attempt_id": "d" * 32, "worker_id": "synthetic-old", "spec": spec,
             "payload_hash": envelopes.payload_hash(spec), "phase": "started",
             "spec_path": str(Path(w.dirs["render"], spec["delivery_id"] + ".json"))}
    w.reg.claim(spec["delivery_id"], claim)
    row = {"phase": "started", "attempt_id": claim["attempt_id"],
           "delivery_id": spec["delivery_id"],
           "receipt_envelope": envelopes.transport_receipt(claim, "unknown")}
    raw = json.dumps(row)[:-1] + ', "phase": "begin"}\n'
    path = Path(w.dirs["state"], "journal-synthetic-old.jsonl")
    path.write_text(raw)

    async def scenario():
        assert worker.acquire_scope_lock()
        try:
            stats = await worker.reconcile(periodic=True)
            assert stats["not_sent"] == 0 and stats["unknown"] == 1
            files = list(Path(w.dirs["cmd_int"]).glob("*.json"))
            assert len(files) == 1
            receipt = json.loads(files[0].read_bytes())
            assert receipt["result"] == "unknown"
            assert receipt["error_code"] == "journal_incomplete"
            assert path.read_text() == raw
            assert not w.client.calls
        finally:
            worker.release_scope_lock()

    asyncio.run(scenario())
