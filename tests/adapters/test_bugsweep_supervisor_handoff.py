"""Synthetic regressions for the card-supervisor handoff: a superseded
supervisor in start-up backoff must not leave the scope without a
sender, a closed Discord client must not start sends, a supervisor that
ends unserved must not keep a 'started' witness, and Hermes Slack setup
must use the binding workspace's own client."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from adapters.common import worker as worker_mod
from adapters.discord import delivery as discord_delivery
from adapters.discord import tasks as discord_tasks
from adapters.slack import tasks as slack_tasks
from discord_testkit import SETTINGS as DISCORD_SETTINGS
from discord_testkit import discord_world as discord_world  # noqa: F401  (fixture ``world``)
from hermes_plugin import card_workers
from mcs_discord_testkit import SignalBot
from slack_testkit import FakeClient
from test_supervisor_startup_retry import _App

SLACK_SETTINGS = {"transport": "slack", "team_id": "T_SYNTHETIC",
                  "application_id": "A_SYNTHETIC", "channel_id": "C_SYNTHETIC",
                  "profile": "cco", "allowed_user_ids": frozenset({"U_OPERATOR"}),
                  "project_ids": frozenset({1})}


def _slack_root(path):
    for name in ("slack_render", "flags", "cmd_int", "cmd_results"):
        (path / name).mkdir(parents=True)
    (path / "flags" / "notify.json").write_text(
        json.dumps({"interactive": True, "transport": "slack"}))
    return path


def _ctx(**extra):
    return SimpleNamespace(
        spawn_task=lambda coro, name=None: asyncio.ensure_future(coro),
        on_unload=lambda cb: None, **extra)


@pytest.fixture
def slow_backoff(monkeypatch):
    # A predecessor's first backoff (5 s) dwarfs the successor's window
    # (0.3 s); a stop is noticed only every 1 s, so the successor must
    # also wait past its window while that predecessor settles.
    monkeypatch.setattr(worker_mod, "POLL_S", 5.0)
    monkeypatch.setattr(worker_mod, "STARTUP_STOP_CHECK_S", 1.0)
    monkeypatch.setattr(slack_tasks, "LOCK_WAIT_S", 0.3)
    monkeypatch.setattr(discord_tasks, "LOCK_WAIT_S", 0.3)
    monkeypatch.setattr(slack_tasks, "_LIVE", {})
    monkeypatch.setattr(discord_tasks, "_LIVE", {})


def test_slack_successor_takes_over_from_a_predecessor_in_startup_backoff(tmp_path, slow_backoff):
    root = _slack_root(tmp_path / "data")
    settings = {**SLACK_SETTINGS, "data_root": str(root)}
    down = FakeClient()

    async def unreachable():
        raise OSError("synthetic network down")

    down.auth_test = unreachable
    logs = []

    async def run():
        old = slack_tasks.Supervisor(ctx=_ctx(), app=_App(down), adapter=None,
                                     settings=dict(settings),
                                     log=lambda e, **f: logs.append(("old", e)))
        assert old.start()
        while ("old", "startup_retry") not in logs:
            await asyncio.sleep(0.01)
        new = slack_tasks.Supervisor(ctx=_ctx(), app=_App(FakeClient()), adapter=None,
                                     settings=dict(settings),
                                     log=lambda e, **f: logs.append(("new", e)))
        assert new.start()
        try:
            await asyncio.wait_for(old._task, 3)
            while not new._actions._active:
                assert not new._task.done(), logs
                await asyncio.sleep(0.01)
            assert slack_tasks._LIVE[slack_tasks.registry.scope_key(settings)] is new
            assert not new.abandoned and not old.abandoned
        finally:
            new.unload()
            await asyncio.wait_for(new._task, 5)

    asyncio.run(run())
    assert ("new", "scope_lock_handoff_wait") in logs
    assert ("new", "scope_lock_unavailable") not in logs


def test_discord_successor_takes_over_from_a_closed_predecessor_in_backoff(world, slow_backoff, monkeypatch):
    world.seed()
    world.dispatch()
    reconcile = discord_delivery.DeliveryWorker.reconcile
    failing = set()

    async def flaky(self):
        if id(self) in failing:
            raise OSError("synthetic disk full")
        return await reconcile(self)

    monkeypatch.setattr(discord_delivery.DeliveryWorker, "reconcile", flaky)
    settings = {**DISCORD_SETTINGS, "data_root": str(world.data), "profile": "mcs"}

    def bot():
        b = SignalBot()
        b.closed = False
        b.is_closed = lambda: b.closed
        return b

    async def run():
        old_bot, new_bot = bot(), bot()
        old = discord_tasks.Supervisor(ctx=_ctx(), bot=old_bot, settings=settings,
                                       log=lambda e, **f: world.logs.append(("old", e)))
        failing.add(id(old._worker))
        assert old.start()
        while ("old", "startup_retry") not in world.logs:
            await asyncio.sleep(0.01)
        old_bot.closed = True                       # fatal rebuild closes the old client
        new = discord_tasks.Supervisor(ctx=_ctx(), bot=new_bot, settings=settings,
                                       log=lambda e, **f: world.logs.append(("new", e)))
        assert new.start()
        try:
            await asyncio.wait_for(old._task, 3)
            await asyncio.wait_for(new_bot.wired.wait(), 3)
            assert not new.abandoned
        finally:
            new.unload()
            await asyncio.wait_for(new._task, 5)

    asyncio.run(run())
    assert ("new", "scope_lock_handoff_wait") in world.logs
    assert ("new", "scope_lock_unavailable") not in world.logs


def test_a_closed_discord_bot_starts_no_claim(world):
    world.seed()
    world.dispatch()
    settings = {**DISCORD_SETTINGS, "data_root": str(world.data), "profile": "mcs"}
    closed = SignalBot()
    closed.is_closed = lambda: True
    sup = discord_tasks.Supervisor(ctx=_ctx(), bot=closed, settings=settings,
                                   log=lambda e, **f: world.logs.append((e, f)))
    specs = list((world.data / "discord_render").glob("*.json"))
    assert specs

    async def run(supervisor):
        assert supervisor._worker.acquire_scope_lock()
        try:
            await supervisor._worker.tick()
        finally:
            supervisor._worker.release_scope_lock()

    asyncio.run(run(sup))
    assert all(sup._reg.claimed(p.stem) is None for p in specs)
    assert not any(c.sent for c in closed.channels.values())
    # the same tick on an open client does claim, so the closed bot is what stopped it
    opened = discord_tasks.Supervisor(ctx=_ctx(), bot=SignalBot(), settings=settings,
                                      log=lambda e, **f: None)
    asyncio.run(run(opened))
    assert any(opened._reg.claimed(p.stem) is not None for p in specs)


def test_a_supervisor_that_never_gets_the_lock_leaves_a_failed_witness(tmp_path, monkeypatch):
    monkeypatch.setattr(slack_tasks, "LOCK_WAIT_S", 0.3)
    monkeypatch.setattr(slack_tasks, "_LIVE", {})
    root = _slack_root(tmp_path / "data")
    witnesses = []
    monkeypatch.setattr(card_workers, "_worker_witness",
                        lambda settings, transport, reason: witnesses.append((transport, reason)))
    config = {"slack_adapter_enabled": True, "slack_team_id": "T_SYNTHETIC",
              "slack_application_id": "A_SYNTHETIC", "slack_channel_id": "C_SYNTHETIC",
              "slack_allowed_user_ids": ["U_OPERATOR"], "slack_profile": "cco",
              "project_ids": [1], "data_root": str(root)}
    ctx = _ctx(profile_name="cco", get_config=lambda key, default=None: config.get(key, default))
    # a foreign owner (not an in-process predecessor) holds the scope lock
    holder = slack_tasks.Supervisor(ctx=_ctx(), app=_App(FakeClient()), adapter=None,
                                    settings={**SLACK_SETTINGS, "data_root": str(root)},
                                    log=lambda e, **f: None)

    async def run():
        assert holder._worker.acquire_scope_lock()
        try:
            sup = card_workers.make_slack_factory(ctx)(_App(FakeClient()), None)
            await asyncio.wait_for(sup._task, 3)
            await asyncio.sleep(0)               # let the done callback run
            assert sup.abandoned
        finally:
            holder._worker.release_scope_lock()

    asyncio.run(run())
    assert witnesses == [("slack", "worker_started"), ("slack", "worker_start_failed")]


def _setup_world(tmp_path, monkeypatch, team_clients):
    import slack_channel_setup
    from adapters.slack import provision
    (tmp_path / "data").mkdir()
    binding = {"team_id": "T_SECOND", "application_id": "A_SYNTHETIC", "profile": "cco"}
    monkeypatch.setattr(slack_channel_setup, "read", lambda root: {
        "state": "pending", "plan": {"binding": binding, "runtime_mode": "hermes"}})
    consumed = []

    async def consume(root, client, got):
        consumed.append((client, got))

    monkeypatch.setattr(provision, "consume", consume)
    config = {"data_root": str(tmp_path / "data"), "slack_team_id": "T_SECOND",
              "slack_application_id": "A_SYNTHETIC", "slack_profile": "cco"}
    ctx = _ctx(get_config=lambda key, default=None: config.get(key, default))
    first = SimpleNamespace(name="first-workspace")

    def get_client(chat_id, team_id=None):       # Hermes' lookup, primary fallback
        return team_clients.get(team_id, first)

    adapter = SimpleNamespace(_owner_profile="cco", _team_clients=team_clients,
                              _get_client=get_client)
    return ctx, SimpleNamespace(client=first), adapter, consumed, binding


def test_hermes_slack_setup_uses_the_bound_workspace_client(tmp_path, monkeypatch):
    second = SimpleNamespace(name="second-workspace")
    ctx, native, adapter, consumed, binding = _setup_world(
        tmp_path, monkeypatch, {"T_FIRST": SimpleNamespace(), "T_SECOND": second})

    async def run():
        task = card_workers._start_slack_setup(ctx, native, adapter)
        await asyncio.wait_for(task, 3)

    asyncio.run(run())
    assert consumed == [(second, binding)]


def test_hermes_slack_setup_without_the_bound_client_keeps_the_intent(tmp_path, monkeypatch):
    ctx, native, adapter, consumed, _ = _setup_world(
        tmp_path, monkeypatch, {"T_FIRST": SimpleNamespace()})

    async def run():
        with pytest.raises(ValueError, match="slack_setup_client_unverified"):
            card_workers._start_slack_setup(ctx, native, adapter)

    asyncio.run(run())
    assert consumed == []
