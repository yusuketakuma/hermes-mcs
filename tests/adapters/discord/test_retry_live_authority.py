"""Synthetic SDK cooldowns recheck live authority without losing sent parts."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from adapters.common import worker as worker_module
from adapters.discord import delivery
from discord_delivery_testkit import _claim, _sent_parts, _spec, _state
from test_mcs_discord_single_post import (
    WireBot, WireThread, _attachment_spec, _mk, synthetic_discord_sdk,
)

__all__ = ["synthetic_discord_sdk"]


def _boundary(worker, root, spec, boundary):
    flags = root / "flags" / "notify.json"
    if boundary in ("off", "epoch"):
        value = json.loads(flags.read_text())
        value["interactive" if boundary == "off" else "route_epoch"] = (
            False if boundary == "off" else 2)
        flags.write_text(json.dumps(value))
    elif boundary == "restore":
        (root / "restore_pending.json").write_text('{"synthetic":true}')
    elif boundary == "stop":
        worker.stop()
    elif boundary == "withdrawn":
        (root / "discord_render" / (spec["delivery_id"] + ".json")).unlink()


@pytest.mark.parametrize("kind", ["card", "thread", "body", "attachment"])
@pytest.mark.parametrize("boundary", ["off", "epoch", "restore", "stop", "withdrawn", "authorized"])
def test_sdk_429_retry_rechecks_worker_authority(tmp_path, kind, boundary):
    bot = WireBot(faults=[429])
    worker, _ = _mk(tmp_path, bot)
    spec = _attachment_spec(tmp_path) if kind == "attachment" else _spec(["合成の本文"])
    (tmp_path / "discord_render" / (spec["delivery_id"] + ".json")).write_text(json.dumps(spec))
    thread = WireThread(bot, 200)
    bot.threads[200] = thread
    ctx = {"thread": thread, "thread_id": "200", "card_message_id": "9001"}

    async def backoff():
        _boundary(worker, tmp_path, spec, boundary)

    bot.http.rate_limit_backoff = backoff
    claim = _claim(spec)
    if kind == "card":
        pending = worker._perform(claim)
    else:
        expected_kind = {"thread": "thread", "body": "body_part", "attachment": "attachment_part"}[kind]
        part = next(item for item in spec["parts"]["manifest"] if item["kind"] == expected_kind)
        pending = worker._perform_part(claim, part, ctx)
    result = asyncio.run(worker_module._outcome_of(pending))
    allowed = boundary == "authorized"
    assert len(bot.wire.posts()) == (2 if allowed else 1)
    assert bot.wire.committed == int(allowed)
    assert result["result"] == ("delivered" if allowed else "not_sent")
    if not allowed:
        assert result["error_code"] == "http_429"


@pytest.mark.parametrize("answer", [False, None, 1, "true", "error"])
def test_unknown_or_failed_authority_never_reaches_first_post(answer):
    bot = WireBot()

    async def authority(**_kwargs):
        if answer == "error":
            raise ValueError("synthetic unavailable authority")
        return answer

    result = asyncio.run(worker_module._outcome_of(delivery.single_post(
        bot, lambda: bot.http.request("POST", "/synthetic"), retry_allowed=authority)))
    assert result == {"result": "not_sent", "error_code": "denied_current_authority"}
    assert bot.wire.calls == []
    assert delivery._POSTS.get() is None and delivery._AUTHORITY.get() is None


def test_authority_callback_does_not_leak_to_a_concurrent_send():
    bot = WireBot()

    async def denied(**_kwargs):
        return False

    async def scenario():
        return await asyncio.gather(
            worker_module._outcome_of(delivery.single_post(
                bot, lambda: bot.http.request("POST", "/denied"), retry_allowed=denied)),
            delivery.single_post(bot, lambda: bot.http.request("POST", "/other")))

    denied_result, other = asyncio.run(scenario())
    assert denied_result == {"result": "not_sent", "error_code": "denied_current_authority"}
    assert other["id"] == 5001 and bot.wire.posts() == [("POST", "/other")]


@pytest.mark.parametrize("answer", [1, "error"])
def test_failed_retry_authority_preserves_the_observed_429(answer):
    bot = WireBot(faults=[429])

    async def authority(*, retry):
        if not retry:
            return True
        if answer == "error":
            raise ValueError("synthetic unavailable authority")
        return answer

    result = asyncio.run(worker_module._outcome_of(delivery.single_post(
        bot, lambda: bot.http.request("POST", "/synthetic"), retry_allowed=authority)))
    assert result == {"result": "not_sent", "error_code": "http_429"}
    assert len(bot.wire.posts()) == 1 and bot.wire.committed == 0


def test_cancelled_authority_restores_call_context():
    bot = WireBot()

    async def authority(**_kwargs):
        raise asyncio.CancelledError

    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            await delivery.single_post(
                bot, lambda: bot.http.request("POST", "/synthetic"), retry_allowed=authority)
        assert delivery._POSTS.get() is None and delivery._AUTHORITY.get() is None

    asyncio.run(scenario())
    assert bot.wire.calls == []


@pytest.mark.parametrize("boundary", ["off", "epoch", "restore", "stop", "withdrawn"])
def test_refused_announcement_keeps_delivered_body_receipt_on_resume(tmp_path, boundary):
    bot = WireBot()
    worker, _ = _mk(tmp_path, bot)
    spec = _spec(["合成の返信"], op="update", thread_id="200")
    body = next(item for item in spec["parts"]["manifest"] if item["kind"] == "body_part")
    body["name"] = "m:202#1"
    body["index"] = 1
    spec["delivery"]["message_id"] = "9001"
    spec["parts"]["manifest"] = [spec["parts"]["manifest"][0], body]
    thread = WireThread(bot, 200)
    bot.threads[200] = thread

    async def post_body(content=None, **_kwargs):
        sent = await bot.http.request("POST", "/channels/200/messages")
        _boundary(worker, tmp_path, spec, boundary)
        return SimpleNamespace(id=sent["id"], jump_url=f"https://discord.com/channels/7/200/{sent['id']}")

    async def announce(*_args, **_kwargs):
        return await bot.http.request("POST", "/channels/42/messages")

    thread.send = post_body
    bot.channel.send = announce
    claim = _claim(spec)
    asyncio.run(worker._deliver_parts(claim, "9001"))
    assert bot.wire.posts() == [("POST", "/channels/200/messages")]
    saved = _sent_parts(_state(tmp_path))[body["part_id"]]
    assert saved["result"] == "delivered" and saved["remote_id"] == "5001"
    assert saved["receipt_envelope"]["result"] == "delivered"
    assert saved["receipt_envelope"]["remote_id"] == "5001"
    # A new worker resumes from the durable result, never from the failed
    # best-effort announcement. Restoring authority cannot resend the body.
    marker = tmp_path / "restore_pending.json"
    if marker.exists():
        marker.unlink()
    restarted, _ = _mk(tmp_path, bot)
    asyncio.run(restarted._deliver_parts(claim, "9001"))
    assert bot.wire.posts() == [("POST", "/channels/200/messages")]
    assert _sent_parts(_state(tmp_path))[body["part_id"]] == saved
