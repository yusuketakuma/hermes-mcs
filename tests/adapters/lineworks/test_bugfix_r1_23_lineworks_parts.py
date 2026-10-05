"""Regression: LINE WORKS thread parts survive a brief api.lock hold and never repeat a caption."""
import asyncio
import threading

from adapters.lineworks import delivery
from test_lineworks_adapter import _worker, overflow_spec, world

CTX = {"card_message_id": "lw:" + "c" * 32}


def test_body_part_waits_out_a_briefly_held_api_lock(tmp_path, monkeypatch):
    w = world(tmp_path)
    spec = overflow_spec()
    part = spec["parts"]["manifest"][1]
    held, release = threading.Event(), threading.Event()

    def hold():
        with delivery.api_lock(str(w.data)):
            held.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    assert held.wait(5)

    async def first_retry_releases(_seconds):
        release.set()
        await asyncio.to_thread(holder.join, 5)

    monkeypatch.setattr(delivery.asyncio, "sleep", first_retry_releases)
    out = asyncio.run(_worker(w)._perform_part({"spec": spec}, part, CTX))
    assert out["result"] == "delivered"
    assert len(w.client.calls) == 1


def test_unavailable_caption_with_prior_id_is_not_reposted(tmp_path):
    w = world(tmp_path)
    gone = {"part_id": "attachment:1", "kind": "attachment_part", "name": "合成.jpg",
            "unavailable": True, "caption": "📎 合成.jpg — 取得失敗",
            "prior_remote_id": "lw:" + "f" * 32}
    assert asyncio.run(_worker(w)._perform_part({"spec": overflow_spec()}, gone, CTX)) == {
        "result": "delivered", "remote_id": "lw:" + "f" * 32}
    assert w.client.calls == []
