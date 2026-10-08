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
    if fault and fault != "archived":    # an auto-archived thread reopens on send
        assert outcome["result"] == "not_sent" and thread.sent == []
    else:
        assert outcome["result"] == "delivered" and len(thread.sent) == 1


@pytest.mark.parametrize("fault", [None, "archived", "locked", "thread_gone", "message_gone", "wrong_parent", "wrong_guild", "forbidden"])
def test_signal_revoke_targets_sealed_reply_even_when_source_thread_unavailable(tmp_path, monkeypatch, fault):
    from test_mcs_discord_delivery import FakeHTTP, FakeMessage
    bot = FakeBot()
    worker, _, _ = _mkworker(tmp_path, bot=bot)
    thread = FakeThread(123456)
    thread.parent_id = 999 if fault == "wrong_parent" else 42
    thread.guild = SimpleNamespace(id=999 if fault == "wrong_guild" else 7)
    thread.archived, thread.locked = fault == "archived", fault == "locked"
    message = FakeMessage(345678, thread)
    thread.sent.append(message)
    if fault == "message_gone":
        thread.fetch_fail = FakeHTTP(404)
    elif fault == "forbidden":
        thread.fetch_fail = FakeHTTP(403)
    if fault != "thread_gone":
        bot.channels[thread.id] = thread
    monkeypatch.setattr(cards, "message_payload", lambda *_args, **_kw: pytest.fail("revoke must not render or resend old content"))
    spec = _spec([])
    spec.update(op="revoke", kind="signal")
    spec["parts"]["source_thread"] = True
    spec["delivery"].update(thread_id=str(thread.id), message_id=str(message.id))
    outcome = asyncio.run(_outcome_of(worker._perform({"spec": spec})))
    if fault in ("wrong_parent", "wrong_guild", "forbidden"):
        assert outcome["result"] == "not_sent" and not message.deleted
    else:
        assert outcome["result"] == "delivered"
        assert message.deleted is (fault not in ("thread_gone", "message_gone"))
    assert thread.sent == [message]
