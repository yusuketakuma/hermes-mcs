"""A late reply posted into a Discord thread also gets one channel notice, like
Slack's reply_broadcast; creation, later chunks and failures stay thread-only.
Synthetic objects only."""
import asyncio
from types import SimpleNamespace

from adapters.common.announce import new_reply_announced
from adapters.discord import delivery


class _Channel:
    def __init__(self, cid, fail=False):
        self.id, self.sent, self.fail = cid, [], fail

    async def send(self, text, **kwargs):
        if self.fail:
            raise RuntimeError("synthetic")
        self.sent.append(text)
        return SimpleNamespace(id=900 + len(self.sent),
                               jump_url=f"https://discord.com/channels/1/{self.id}/{900 + len(self.sent)}")


def _worker(monkeypatch, channel):
    worker = object.__new__(delivery.DeliveryWorker)
    worker._bot, worker._logged = object(), []
    worker._log = lambda event, **kw: worker._logged.append(event)

    async def lookup(_cid):
        return channel
    worker._channel = lookup

    async def direct(_bot, factory, **_kwargs):
        return await factory()
    monkeypatch.setattr(delivery, "single_post", direct)
    monkeypatch.setattr(delivery.cards, "no_pings", lambda: None)
    return worker


def _spec(op="update", intents=(7,)):
    return {"op": op, "kind": "thread",
            "delivery": {"channel_id": "100", "thread_id": "200", "intent_event_ids": list(intents)},
            "parts": {"manifest": [{"name": "m:202#1"}]}}


def _post(worker, spec, part, thread):
    return asyncio.run(worker._text_part(spec, thread, part, "合成の見出し行\n合成の本文", {}))


def test_new_reply_on_existing_card_also_notifies_the_channel(monkeypatch):
    channel, thread = _Channel("100"), _Channel("200")
    worker = _worker(monkeypatch, channel)
    monkeypatch.setattr(worker, "_remote_match", lambda *a, **k: _none(), raising=False)
    monkeypatch.setattr(worker, "_rewrite_prior", lambda *a, **k: _none(), raising=False)
    out = _post(worker, _spec(), {"part_id": "body:0002", "kind": "body_part", "name": "m:202#1"}, thread)
    assert out["result"] == "delivered" and thread.sent == ["合成の見出し行\n合成の本文"]
    assert len(channel.sent) == 1 and channel.sent[0].startswith("↳ スレッドに新しい返信: 合成の見出し行\n")
    assert channel.sent[0].endswith("/200/901")


def test_creation_later_chunks_and_failed_notice_stay_thread_only(monkeypatch):
    channel, thread = _Channel("100", fail=True), _Channel("200")
    worker = _worker(monkeypatch, channel)
    monkeypatch.setattr(worker, "_remote_match", lambda *a, **k: _none(), raising=False)
    monkeypatch.setattr(worker, "_rewrite_prior", lambda *a, **k: _none(), raising=False)
    assert not new_reply_announced(_spec(op="create"), {"kind": "body_part", "name": "m:202#1"})
    assert not new_reply_announced(_spec(), {"kind": "body_part", "name": "m:202#2"})
    assert not new_reply_announced(_spec(intents=()), {"kind": "body_part", "name": "m:202#1"})
    assert not new_reply_announced(_spec(), {"kind": "body_part", "name": "m:202#1", "broadcast": False})
    out = _post(worker, _spec(), {"part_id": "body:0002", "kind": "body_part", "name": "m:202#1"}, thread)
    # the thread post is delivered; a failed notice is logged, never retried or turned into a failure
    assert out["result"] == "delivered" and worker._logged == ["reply_announce_failed"]


async def _none():
    return None
