"""Regression: a Slack 429 waits out Retry-After and resends once — the
rejection proves nothing was written, so it never duplicates a post."""
import asyncio
from types import SimpleNamespace

from adapters.slack import delivery
from slack_card_testkit import _spec
from slack_testkit import FakeClient


class RateLimited(Exception):
    def __init__(self, retry_after="2"):
        super().__init__("ratelimited")
        self.response = SimpleNamespace(
            status_code=429, data={"ok": False, "error": "ratelimited"},
            headers={"Retry-After": retry_after})


def _run(fails, monkeypatch, retry_after="2"):
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(delivery.asyncio, "sleep", sleep)
    client = FakeClient()
    left = [fails]
    post = client.chat_postMessage

    async def flaky(**kwargs):
        if left[0]:
            left[0] -= 1
            client.calls.append(("create", kwargs))
            raise RateLimited(retry_after)
        return await post(**kwargs)

    client.chat_postMessage = flaky
    adapter = delivery.SlackCardAdapter(
        SimpleNamespace(client=client), team_id="T_SYNTHETIC",
        application_id="A_SYNTHETIC", channel_id="C_SYNTHETIC",
        profile="cco", allowed_user_ids={"U_OPERATOR"})

    async def scenario():
        assert await adapter.bind()
        return await adapter.perform(_spec())

    out = asyncio.run(scenario())
    return out, [c for c in client.calls if c[0] == "create"], waits


def test_single_429_is_waited_out_and_delivered(monkeypatch):
    out, creates, waits = _run(1, monkeypatch)
    assert out["result"] == "delivered" and len(creates) == 2
    assert waits == [2.0]


def test_repeated_429_stays_not_sent_after_one_retry(monkeypatch):
    out, creates, waits = _run(2, monkeypatch, retry_after="9999")
    assert out == {"result": "not_sent", "error_code": "ratelimited"}
    assert len(creates) == 2 and waits == [delivery.RATE_WAIT_MAX_S]
