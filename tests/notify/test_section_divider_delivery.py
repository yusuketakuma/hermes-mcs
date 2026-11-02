"""Synthetic transport send paths show the sealed thread divider."""
import asyncio
import json

import notify_render
import present_slack
from discord_delivery_testkit import FakeThread, _claim as discord_claim, _mkworker
from discord_delivery_testkit import synthetic_discord_sdk  # noqa: F401
from lineworks_testkit import _worker as lineworks_worker, world as lineworks_world
from notify_lineworks_testkit import LINEWORKS
from notify_testkit import CFG, _dispatch, _intent, _latest_render, _seed_thread, led
from slack_testkit import SLACK, _granted_card, _mkworld

__all__ = ["led"]


def _make_spec(led, cfg):
    _seed_thread(led, mids=(100,))
    body = "合成本文" * 400 + "\n" + notify_render.SECTION_RULE + "\n末尾"
    led.db.execute("UPDATE messages SET body_text=? WHERE message_id=100", (body,))
    assert _dispatch(led, _intent(led, payload={"message_ids": [100]}), cfg)["dispatched"]
    spec = json.loads(_latest_render(led)["spec_json"])
    header = next(p for p in spec["parts"]["manifest"] if p.get("name") == "m:100:header#1")
    text = spec["parts"]["thread_body_parts"][int(header["part_id"].split(":")[1]) - 1]
    return spec, header, text, body


def test_slack_delivers_native_separator_with_plain_text_fallback(led, monkeypatch):
    monkeypatch.setattr(present_slack, "FACE_ORIGINAL_MAX", 20)
    spec, _part, header, body = _make_spec(led, SLACK)
    w = _mkworld(led)
    asyncio.run(_granted_card(w.worker, led, w.root))
    post = next(p for p in w.client.thread_posts if p.get("text") == header)
    assert post["blocks"][-1] == {"type": "divider"}
    assert "".join(p["text"] for p in w.client.thread_posts if p.get("text") != header) == body
    assert all(p.get("thread_ts") for p in w.client.thread_posts)


def test_discord_sends_sealed_header_in_thread(led, tmp_path):
    spec, part, header, _body = _make_spec(led, CFG)
    w, _, _ = _mkworker(tmp_path)
    thread = FakeThread(7001)
    result = asyncio.run(w._perform_part(discord_claim(spec), part,
                         {"thread": thread, "thread_id": str(thread.id), "history": None, "consumed": set()}))
    assert result["result"] == "delivered"
    assert thread.sent[0].content == header and header.endswith("\n" + notify_render.SECTION_RULE)


def test_lineworks_sends_header_and_original_as_separate_posts(led, tmp_path):
    spec, header_part, header, _body = _make_spec(led, LINEWORKS)
    w = lineworks_world(tmp_path)
    worker = lineworks_worker(w)
    claim = {"spec": spec}
    context = {"card_message_id": "lw:" + "c" * 32}
    original = next(p for p in spec["parts"]["manifest"] if p.get("name") == "m:100#1")
    async def send():
        assert (await worker._perform_part(claim, header_part, context))["result"] == "delivered"
        assert (await worker._perform_part(claim, original, context))["result"] == "delivered"
    asyncio.run(send())
    messages = [payload["text"] for op, payload, _ in w.client.calls if op == "message"]
    assert len(messages) == 2 and messages[0] == header
    assert messages[0].endswith("\n" + notify_render.SECTION_RULE)
    assert messages[1] == spec["parts"]["thread_body_parts"][int(original["part_id"].split(":")[1]) - 1]
