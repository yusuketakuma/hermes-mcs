"""Regression: a shared 429 cooldown is waited out for thread parts, never journaled as not_sent."""
import asyncio
import json
import os
import time

from adapters.lineworks import delivery
from adapters.lineworks.client import ClientError
from test_lineworks_adapter import _worker, overflow_spec, world

CTX = {"card_message_id": "lw:" + "c" * 32}


def test_body_part_waits_out_the_shared_cooldown(tmp_path):
    w = world(tmp_path)
    spec = overflow_spec()
    with open(os.path.join(w.dirs["state"], "rate-limit.json"), "w") as stream:
        json.dump({"until": time.time() + 0.2}, stream)
    out = asyncio.run(_worker(w)._perform_part({"spec": spec}, spec["parts"]["manifest"][1], CTX))
    assert out["result"] == "delivered"
    assert len(w.client.calls) == 1


def test_body_part_retries_a_wire_429_after_the_cooldown(tmp_path, monkeypatch):
    w = world(tmp_path)
    spec = overflow_spec()
    now = [100.0]
    monkeypatch.setattr(delivery.time, "time", lambda: now[0])

    async def skip(seconds):
        now[0] += seconds
        w.client.error = None

    monkeypatch.setattr(delivery.asyncio, "sleep", skip)
    w.client.error = ClientError("rate_limited", 429)
    out = asyncio.run(_worker(w)._perform_part({"spec": spec}, spec["parts"]["manifest"][1], CTX))
    assert out["result"] == "delivered"
    assert len(w.client.calls) == 2


def test_card_keeps_local_cooldown_as_not_sent(tmp_path):
    w = world(tmp_path)
    with open(os.path.join(w.dirs["state"], "rate-limit.json"), "w") as stream:
        json.dump({"until": time.time() + 30}, stream)
    out = asyncio.run(w.sender.perform(overflow_spec()))
    assert out == {"result": "not_sent", "error_code": "rate_limited"} and w.client.calls == []
