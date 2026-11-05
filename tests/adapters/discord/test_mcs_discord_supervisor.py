"""Synthetic mcs discord supervisor contracts with isolated shared fixtures."""
from __future__ import annotations
import asyncio
from contextlib import suppress
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from adapters.common import worker as worker_mod
from adapters.discord import delivery, tasks
from discord_testkit import SETTINGS, FakeBot
from discord_testkit import discord_world as discord_world
from mcs_discord_testkit import SignalBot as SignalBot, _pin_wall_clock as _pin_wall_clock


def test_signal_bot_wiring_event_tracks_listeners():
    """The wait signal the supervisor tests rely on: set on wiring, cleared
    only when the last listener is removed (so a re-wire is observable)."""
    async def run():
        bot = SignalBot()
        assert not bot.wired.is_set()
        bot.add_listener(print, "on_a")
        bot.add_listener(len, "on_b")
        assert bot.wired.is_set()
        bot.remove_listener(print, "on_a")
        assert bot.wired.is_set()
        bot.remove_listener(len, "on_b")
        assert not bot.wired.is_set()
    asyncio.run(run())


def test_supervisor_registers_and_stops(world):
    """Listener lands once per Bot, unload removes it, and the
    supervisor drives the loop through spawn_task."""
    world.seed()
    world.dispatch()
    spawned = []

    class FakeCtx:
        def spawn_task(self, coro, *, name=None):
            t = asyncio.ensure_future(coro)
            spawned.append(t)
            return t

        def on_unload(self, cb):
            self._unload = cb

    async def run():
        bot = SignalBot()
        sup = tasks.Supervisor(
            ctx=FakeCtx(), bot=bot,
            settings={**SETTINGS, "data_root": str(world.data)},
            log=lambda e, **f: world.logs.append((e, f)))
        assert sup.start() is True
        assert spawned and not spawned[0].done()
        await asyncio.wait_for(bot.wired.wait(), 5)
        assert len(bot.listeners) == 1
        sup.unload()
        spawned[0].cancel()
        with suppress(asyncio.CancelledError):
            await spawned[0]
        assert not bot.listeners

    asyncio.run(run())


def test_supervisor_retries_a_transient_reconcile_failure(world, monkeypatch):
    """Regression: an OSError (or registry_corrupt) during the start-up
    reconcile made _run exit silently — delivery stopped until restart."""
    world.seed()
    world.dispatch()
    real_sleep = asyncio.sleep

    async def fast_sleep(_seconds):
        await real_sleep(0)

    monkeypatch.setattr(worker_mod.asyncio, "sleep", fast_sleep)
    reconcile = delivery.DeliveryWorker.reconcile
    fails = [1]

    async def flaky(self):
        if fails[0]:
            fails[0] -= 1
            raise OSError("synthetic disk blip")
        return await reconcile(self)

    monkeypatch.setattr(delivery.DeliveryWorker, "reconcile", flaky)
    ctx = SimpleNamespace(spawn_task=lambda coro, name=None: asyncio.ensure_future(coro),
                          on_unload=lambda cb: None)

    async def run():
        bot = SignalBot()
        sup = tasks.Supervisor(
            ctx=ctx, bot=bot,
            settings={**SETTINGS, "data_root": str(world.data)},
            log=lambda e, **f: world.logs.append((e, f)))
        assert sup.start() is True
        await asyncio.wait_for(bot.wired.wait(), 5)
        sup.unload()
        await asyncio.wait_for(sup._task, 5)

    asyncio.run(run())
    assert ("startup_retry", {"error": "OSError"}) in world.logs


# ---------- D3: supervisor scope isolation (RC17) ------------------------------

def test_supervisor_profile_scopes_do_not_mix(world, monkeypatch):
    """A->B->A: two profiles on the same data root hold separate scope
    locks; unload removes only its own listener; a duplicate worker on
    an owned scope reports visible-stopped instead of racing sends."""
    monkeypatch.setattr(tasks, "LOCK_WAIT_S", 1.0)
    world.seed()
    world.dispatch()
    spawned = []

    class FakeCtx:
        def spawn_task(self, coro, *, name=None):
            t = asyncio.ensure_future(coro)
            spawned.append(t)
            return t

        def on_unload(self, cb):
            self._unload = cb

    def _sup(bot, profile):
        return tasks.Supervisor(
            ctx=FakeCtx(), bot=bot,
            settings={**SETTINGS, "data_root": str(world.data),
                      "profile": profile},
            log=lambda e, **f: world.logs.append((e, f)))

    async def run():
        bot_a, bot_b = SignalBot(), SignalBot()
        sup_a = _sup(bot_a, "mcs")
        sup_b = _sup(bot_b, "other")
        assert sup_a.start() and sup_b.start()
        await asyncio.wait_for(
            asyncio.gather(bot_a.wired.wait(), bot_b.wired.wait()), 5)
        locks = list((world.data / "discord_state")
                     .glob("send-*.lock"))
        assert len(locks) == 2        # different profiles, both live

        # a second worker on A's owned scope gets a visible stop
        bot_dup = FakeBot()
        sup_dup = _sup(bot_dup, "mcs")
        assert sup_dup.start()
        dup_task = spawned[-1]
        await asyncio.wait_for(dup_task, 5)
        assert not bot_dup.listeners
        assert any(e == "scope_lock_unavailable"
                   for e, _ in world.logs)

        # unload A -> its listener goes; B is untouched
        sup_a.unload()
        assert not bot_a.listeners and len(bot_b.listeners) == 1
        spawned[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await spawned[0]
        # A returns -> exactly one listener on the fresh binding
        sup_a2 = _sup(bot_a, "mcs")
        assert sup_a2.start()
        assert not bot_a.wired.is_set()
        await asyncio.wait_for(bot_a.wired.wait(), 5)
        assert len(bot_a.listeners) == 1

        sup_b.unload()
        sup_a2.unload()
        for t in spawned:
            t.cancel()
        for t in spawned:
            with suppress(asyncio.CancelledError):
                await t

    asyncio.run(run())


def test_supervisor_yields_scope_when_bot_closes(world):
    """Fatal adapter rebuild: the predecessor's bot is closed before the
    replacement connects; the old supervisor must release the scope lock
    so the successor — wired on the new bot — can take over intake."""
    world.seed()
    world.dispatch()
    spawned = []

    class FakeCtx:
        def spawn_task(self, coro, *, name=None):
            t = asyncio.ensure_future(coro)
            spawned.append(t)
            return t

        def on_unload(self, cb):
            self._unload = cb

    def _sup(bot):
        return tasks.Supervisor(
            ctx=FakeCtx(), bot=bot,
            settings={**SETTINGS, "data_root": str(world.data),
                      "profile": "mcs"},
            log=lambda e, **f: world.logs.append((e, f)))

    async def run():
        closed = {"a": False}
        bot_a = SignalBot()
        bot_a.is_closed = lambda: closed["a"]
        sup_a = _sup(bot_a)
        assert sup_a.start()
        await asyncio.wait_for(bot_a.wired.wait(), 5)

        # fatal adapter error path: host closes the old client, then a
        # rebuilt adapter wires a successor on the new bot
        closed["a"] = True
        bot_b = SignalBot()
        sup_b = _sup(bot_b)
        assert sup_b.start()

        # predecessor notices the closed client and releases the lock;
        # the successor waits out the handoff and takes over intake
        await asyncio.wait_for(spawned[0], 10)
        await asyncio.wait_for(bot_b.wired.wait(), 10)
        assert len(bot_b.listeners) == 1
        assert not bot_a.listeners
        assert any(e == "bot_closed" for e, _ in world.logs)

        sup_b.unload()
        for t in spawned:
            t.cancel()
        for t in spawned:
            with suppress(asyncio.CancelledError):
                await t

    asyncio.run(run())


def test_rebuilds_do_not_keep_old_supervisors_alive_in_the_host(world, monkeypatch):
    """Each fatal rebuild starts a new supervisor on the same host; the
    host's unload list must not pin every superseded one, an old host's
    close leaves a newer host alone, and a closed, dropped host goes."""
    import gc
    import weakref
    from mcs_standalone.host import Host
    monkeypatch.setattr(tasks, "_LIVE", {})
    monkeypatch.setattr(tasks, "_HOOKED", weakref.WeakKeyDictionary())
    world.seed()
    world.dispatch()
    settings = {**SETTINGS, "data_root": str(world.data), "profile": "mcs"}

    def _bot():
        bot = SignalBot()
        bot.closed = False
        bot.is_closed = lambda: bot.closed
        return bot

    async def run():
        host = Host({})
        gone, current = [], None
        for _ in range(4):
            bot = _bot()
            if current is not None:
                current._bot.closed = True             # fatal rebuild closes the old client
            previous, current = current, tasks.Supervisor(
                ctx=host, bot=bot, settings=settings,
                log=lambda e, **f: world.logs.append((e, f)))
            assert current.start()
            if previous is not None:
                await asyncio.wait_for(previous._task, 10)
                gone.append(weakref.ref(previous))
            await asyncio.wait_for(bot.wired.wait(), 10)
            del previous, bot
        gc.collect()
        assert [ref() for ref in gone] == [None] * 3
        assert len(host._unload) == 1
        other = Host({})                               # a newer host for the same scope
        newest = current
        del current
        later = tasks.Supervisor(ctx=other, bot=_bot(), settings=settings,
                                 log=lambda e, **f: None)
        newest._bot.closed = True
        assert later.start()
        await asyncio.wait_for(newest._task, 10)
        await host.close()                             # the old host
        assert not later._stopping
        await other.close()
        assert later._stopping
        return weakref.ref(host)

    host_ref = asyncio.run(run())
    gc.collect()
    assert host_ref() is None


def test_closing_the_host_before_the_task_runs_releases_the_supervisor(world, monkeypatch):
    """start() then an immediate close cancels a task that never ran, so its
    finally cannot clean up; the host's own unload must drop the live entry."""
    import gc
    import weakref
    from mcs_standalone.host import Host
    monkeypatch.setattr(tasks, "_LIVE", {})
    monkeypatch.setattr(tasks, "_HOOKED", weakref.WeakKeyDictionary())
    settings = {**SETTINGS, "data_root": str(world.data), "profile": "mcs"}

    async def run():
        host = Host({})
        sup = tasks.Supervisor(ctx=host, bot=SignalBot(), settings=settings,
                               log=lambda e, **f: None)
        assert sup.start()
        await host.close()                     # no yield: the task never started
        assert sup._stopping and not tasks._LIVE
        # an old host's close leaves a newer host's supervisor alone
        old, other = Host({}), Host({})
        first = tasks.Supervisor(ctx=old, bot=SignalBot(), settings=settings,
                                 log=lambda e, **f: None)
        newer = tasks.Supervisor(ctx=other, bot=SignalBot(), settings=settings,
                                 log=lambda e, **f: None)
        assert first.start() and newer.start()
        await old.close()
        assert tasks._LIVE[tasks.registry.scope_key(settings)] is newer and not newer._stopping
        await other.close()
        assert newer._stopping and not tasks._LIVE
        return weakref.ref(sup), weakref.ref(host)

    refs = asyncio.run(run())
    gc.collect()
    assert [r() for r in refs] == [None, None]
