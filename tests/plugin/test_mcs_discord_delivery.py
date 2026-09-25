"""Synthetic Discord thread backfill without a network or MCS data."""

import asyncio
from types import SimpleNamespace

from hermes_plugin.mcs_delivery.registry import Registry
from hermes_plugin.mcs_discord.delivery import DeliveryWorker


def test_backfill_preserves_repeated_body_chunks(tmp_path):
    chunk = "x" * 1900
    posted = [chunk]

    class Thread:
        id = 123

        def history(self, *, limit):
            async def messages():
                for content in reversed(posted):
                    yield SimpleNamespace(content=content)
            return messages()

        async def send(self, content):
            posted.append(content)

    worker = DeliveryWorker(
        bot=None, settings={}, root=str(tmp_path), reg=Registry(str(tmp_path)),
        worker_id="synthetic",
        log=lambda *_args, **_kwargs: None)
    spec = {"parts": {"thread_body": chunk + chunk}}

    async def scenario():
        await worker._thread_body(spec, Thread(), dedupe=True)
        assert posted == [chunk, chunk]
        await worker._thread_body(spec, Thread(), dedupe=True)
        assert posted == [chunk, chunk]

    asyncio.run(scenario())
