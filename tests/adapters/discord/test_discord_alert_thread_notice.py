"""Native Discord alerts remain in the receipt-bound source thread."""
import asyncio
from types import SimpleNamespace

import pytest

from adapters.discord import cards
from adapters.common.worker import _outcome_of
from test_mcs_discord_delivery import _mkworker, FakeBot, FakeThread, synthetic_discord_sdk
from discord_delivery_testkit import _spec

__all__ = ["synthetic_discord_sdk"]


@pytest.mark.parametrize("kind", ["notice", "signal"])
@pytest.mark.parametrize("fault", [None, "parent", "guild", "archived", "locked", "gone"])
def test_native_alert_only_posts_in_bound_source_thread(tmp_path, monkeypatch, fault, kind):
    monkeypatch.setattr(cards, "message_payload", lambda spec: {"content": "完全合成のアラート"})
    bot = FakeBot()
    worker, _, _ = _mkworker(tmp_path, bot=bot)
    before = len(bot.channels[42].sent)
    thread = FakeThread(123456)
    thread.parent_id = 999 if fault == "parent" else 42
    thread.guild = SimpleNamespace(id=999 if fault == "guild" else 7)
    thread.archived, thread.locked = fault == "archived", fault == "locked"
    if fault != "gone":
        bot.channels[thread.id] = thread
    spec = _spec([])
    spec["op"] = "notice" if kind == "notice" else "create"
    spec["kind"] = "signal"
    spec["parts"]["thread_notice" if kind == "notice" else "source_thread"] = True
    spec["delivery"]["thread_id"] = str(thread.id)
    if kind == "notice":
        spec["delivery"]["message_id"] = str(thread.id)
    outcome = asyncio.run(_outcome_of(worker._perform({"spec": spec})))
    assert len(bot.channels[42].sent) == before
    if fault:
        assert outcome["result"] == "not_sent" and thread.sent == []
    else:
        assert outcome["result"] == "delivered" and len(thread.sent) == 1
