"""A delayed Discord view followup needs the role NOW, not only at click time (synthetic)."""
import asyncio
import sys
from types import SimpleNamespace

import pytest
from adapters.discord import actions as actions_mod
from discord_testkit import CFG, SETTINGS, FakeInteraction, _deliver
from discord_testkit import discord_world  # noqa: F401  (registers fixture ``world``)


class Guild:
    def __init__(self, roles, cached):
        self.member = SimpleNamespace(id=3003, roles=[SimpleNamespace(id=r) for r in roles])
        # without the members intent the cache never sees a role removal
        self.cached = cached and SimpleNamespace(id=3003, roles=[SimpleNamespace(id=555)])
        self.fetches = 0

    def get_member(self, uid):
        return self.cached if uid == 3003 else None

    async def fetch_member(self, uid):
        self.fetches += 1
        if uid != 3003:
            raise LookupError("synthetic unknown member")
        return self.member


@pytest.mark.parametrize(("current", "cached", "guild_known", "delivered"), [
    (["555"], True, True, True),        # still holds the role
    (["555"], False, True, True),       # still holds it, uncached member
    ([], True, True, False),            # removed while waiting; stale cache ignored
    ([], False, True, False),           # removed, uncached member
    (["555"], True, False, False),      # guild unknown: fail closed
])
def test_delayed_followup_checks_current_role(world, monkeypatch, current, cached,
                                              guild_known, delivered):
    world.seed()
    world.dispatch(cfg={"notify": {**CFG["notify"], "card_thread": False}})
    worker, reg, bot = world.mkworker()
    asyncio.run(_deliver(world, worker))
    _, spec = world.spec()
    act = world.mkactions(reg, bot)
    act._settings = {**SETTINGS, "allowed_role_ids": {"555"}}
    logs = []
    act._log = lambda event, **kw: logs.append((event, kw))
    ix = FakeInteraction(f"mcs:a:{world.token(spec, 'body')}", user_id=3003,
                         message_id=bot.channels[42].sent[0].id)
    ix.user = SimpleNamespace(id=3003, roles=[SimpleNamespace(id=555)])
    monkeypatch.setattr(actions_mod, "RESULT_WAIT_S", 0)
    asyncio.run(act.on_interaction(ix))
    assert len(reg.followups()) == 1
    guild = Guild(current, cached)
    bot.get_guild = lambda gid: guild if guild_known and gid == 7 else None
    webhook = sys.modules["discord"].Webhook
    webhook.sent = []
    asyncio.run(act.sweep_followups())       # result not ready: no lookup yet
    assert guild.fetches == 0 and len(reg.followups()) == 1
    world.drain()
    asyncio.run(act.sweep_followups())
    assert len(webhook.sent) == (1 if delivered else 0)
    if delivered:
        assert webhook.sent[0]["ephemeral"] and "本文" in webhook.sent[0]["content"]
    assert not reg.followups()          # delivered or dropped, never retried
    assert guild.fetches == (1 if guild_known else 0)
    if not delivered:
        assert ("followup_dropped", {"reason": "role_check"}) in logs


def test_unauthorized_followup_drop_is_logged(world, monkeypatch):
    """A followup the sweep can no longer authorize (here: card settings
    without the required guild_id) is dropped with a log, never silently."""
    world.seed()
    world.dispatch(cfg={"notify": {**CFG["notify"], "card_thread": False}})
    worker, reg, bot = world.mkworker()
    asyncio.run(_deliver(world, worker))
    _, spec = world.spec()
    act = world.mkactions(reg, bot)
    ix = FakeInteraction(f"mcs:a:{world.token(spec, 'body')}",
                         message_id=bot.channels[42].sent[0].id)
    monkeypatch.setattr(actions_mod, "RESULT_WAIT_S", 0)
    asyncio.run(act.on_interaction(ix))
    assert len(reg.followups()) == 1
    act._settings = {k: v for k, v in SETTINGS.items() if k != "guild_id"}
    logs = []
    act._log = lambda event, **kw: logs.append((event, kw))
    asyncio.run(act.sweep_followups())
    assert not reg.followups()
    assert ("followup_dropped", {"reason": "unauthorized"}) in logs
