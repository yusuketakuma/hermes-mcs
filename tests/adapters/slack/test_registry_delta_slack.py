"""Slack card bursts keep posted pins durable without a registry rewrite per card."""
import asyncio

from hermes_plugin.mcs_delivery import registry
from hermes_plugin.mcs_slack.delivery import DeliveryWorker
from slack_card_testkit import _spec


def test_slack_burst_rewrites_registry_once_and_pins_survive_crash(tmp_path, monkeypatch):
    spec = _spec()
    settings = spec["delivery"]
    reg = registry.Registry(str(tmp_path), scope=settings)
    writes = []
    real = registry.paths.atomic_write

    def counted(path, raw, **kw):
        if path == reg._path:
            writes.append(path)
        return real(path, raw, **kw)

    monkeypatch.setattr(registry.paths, "atomic_write", counted)

    class Sender:
        async def perform(self, received):
            return {"result": "delivered", "message_id": "1790000000.000001"}

    worker = DeliveryWorker(sender=Sender(), settings=settings, root=str(tmp_path),
                            reg=reg, worker_id="synthetic-worker", log=lambda *_a, **_k: None)

    async def burst():
        with reg.batch():
            for _ in range(5):
                await worker._perform({"spec": spec})
            assert writes == []
            crashed = registry.Registry(str(tmp_path), scope=settings)
            assert crashed.token("b" * 32)["message_id"] == "1790000000.000001"
    asyncio.run(burst())
    assert len(writes) == 1
