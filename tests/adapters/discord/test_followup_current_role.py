"""A delayed Discord view followup needs the role NOW, not only at click time (synthetic)."""
import asyncio
import sys
from types import SimpleNamespace

import pytest
from discord_testkit import SETTINGS
from test_mcs_discord import CFG, FakeInteraction, _deliver, actions_mod, world  # noqa: F401


class Guild:
    def __init__(self, roles, cached):
        self.member = SimpleNamespace(id=3003, roles=[SimpleNamespace(id=r) for r in roles])
        self.cached, self.fetches = cached, 0

    def get_member(self, uid):
        return self.member if self.cached and uid == 3003 else None

    async def fetch_member(self, uid):
        self.fetches += 1
        if uid != 3003:
            raise LookupError("synthetic unknown member")
        return self.member


@pytest.mark.parametrize(("current", "cached", "guild_known", "delivered"), [
    (["555"], True, True, True),        # still holds the role (cache)
    (["555"], False, True, True),       # still holds it (one member fetch)
    ([], True, True, False),            # role removed while waiting
    ([], False, True, False),           # removed, uncached member
    (["555"], True, False, False),      # guild unknown: fail closed
])
def test_delayed_followup_checks_current_role(world, monkeypatch, current, cached,  # noqa: F811
                                              guild_known, delivered):
    world.seed()
    world.dispatch(cfg={"notify": {**CFG["notify"], "card_thread": False}})
    worker, reg, bot = world.mkworker()
    asyncio.run(_deliver(world, worker))
    _, spec = world.spec()
    act = world.mkactions(reg, bot)
    act._settings = {**SETTINGS, "allowed_role_ids": {"555"}}
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
    assert guild.fetches == (0 if cached or not guild_known else 1)
