"""Synthetic: stopping during a proven-unsent wait must not start another wire call."""
import asyncio
import time
from types import SimpleNamespace

import pytest
from adapters.lineworks import delivery
from adapters.lineworks.client import ClientError


@pytest.mark.parametrize("refusal", ["429", "sender_busy"])
@pytest.mark.parametrize("kind", ["body", "attachment", "unavailable"])
@pytest.mark.parametrize("boundary", ["stop", "route", "withdrawn", "authorized"])
def test_stop_during_unsent_wait_never_starts_the_next_wire_call(
        monkeypatch, tmp_path, refusal, kind, boundary):
    worker = object.__new__(delivery.DeliveryWorker)
    worker._stopping = False
    worker._root = "synthetic-unused-root"
    calls = []
    route = {"open": True}
    worker._dirs = {"render": str(tmp_path)}
    spec_path = tmp_path / "synthetic-delivery.json"
    spec_path.write_text("{}")

    def wire(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == 1:
            error = ClientError("rate_limited" if refusal == "429" else "sender_busy",
                                429 if refusal == "429" else None)
            if refusal == "429":
                error.until = time.time() + 1
            raise error
        return "synthetic-file-id"

    worker._sender = SimpleNamespace(route_current=lambda spec: True,
                                     send=wire, attachment=wire)

    async def allowed(spec=None):
        return not worker._stopping and route["open"]

    worker._send_allowed = allowed

    async def stop_instead_of_waiting(_seconds):
        if boundary == "stop":
            worker.stop()
        elif boundary == "route":
            route["open"] = False
        elif boundary == "withdrawn":
            spec_path.unlink()

    monkeypatch.setattr(delivery.asyncio, "sleep", stop_instead_of_waiting)
    monkeypatch.setattr(delivery.paths, "read_verified_attachment", lambda *args: b"synthetic")
    spec = {"op": "create", "delivery_id": "synthetic-delivery", "delivery": {"route_epoch": 1},
            "parts": {"thread_body_parts": ["synthetic-body"], "manifest": []}}
    if kind == "body":
        part = {"kind": "body_part", "part_id": "body:0001", "name": "m:1#1"}
    else:
        part = {"kind": "attachment_part", "part_id": "attach:0001", "name": "synthetic.bin"}
        if kind == "unavailable":
            part.update(unavailable=True, caption="synthetic unavailable")
    out = asyncio.run(worker._perform_part({"spec": spec}, part, {}))
    assert worker._stopping is (boundary == "stop")
    assert len(calls) == (2 if boundary == "authorized" else 1)
    assert out["result"] == ("delivered" if boundary == "authorized" else "not_sent")
    if boundary != "authorized":
        # Preserve the actual, provably unsent response rather than an unknown
        # classification or a synthetic successful delivery.
        assert out["error_code"] == ("rate_limited" if refusal == "429" else "sender_busy")


def test_primary_card_busy_wait_also_borrows_the_worker_stop_gate(monkeypatch, tmp_path):
    worker = object.__new__(delivery.DeliveryWorker)
    worker._stopping = False
    worker._dirs = {"render": str(tmp_path)}
    calls = []
    settings = {key: "synthetic" for key in delivery.SCOPE_KEYS}
    settings["route_epoch"] = 1
    sender = delivery.Sender(None, settings, "synthetic-unused-root")
    worker._sender = sender
    worker._ours = lambda scope: True
    sender.route_current = lambda spec: True

    def rejected(content):
        calls.append(content)
        raise ClientError("sender_busy")

    sender.send = rejected

    async def allowed(spec=None):
        return not worker._stopping

    worker._send_allowed = allowed

    async def stop(_seconds):
        worker.stop()

    monkeypatch.setattr(delivery.asyncio, "sleep", stop)
    monkeypatch.setattr(delivery.cards, "render", lambda spec: {"type": "text", "text": "synthetic"})
    spec = {"op": "create", "delivery_id": "synthetic-card",
            "delivery": settings, "parts": {"manifest": []}}
    out = asyncio.run(worker._perform({"spec": spec}))
    assert calls == [{"type": "text", "text": "synthetic"}]
    assert out == {"result": "not_sent", "error_code": "sender_busy"}
