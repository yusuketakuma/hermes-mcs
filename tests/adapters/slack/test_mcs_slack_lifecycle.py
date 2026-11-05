"""Synthetic mcs slack lifecycle contracts with isolated shared fixtures."""
import asyncio
import json
from types import SimpleNamespace
import pytest
from hermes_plugin.card_workers import make_slack_factory
from adapters.common import registry
from slack_testkit import FakeClient
from slack_testkit import isolated_slack_ledger as isolated_slack_ledger
from mcs_slack_testkit import _pin_wall_clock as _pin_wall_clock


@pytest.mark.parametrize("launcher", ["default", "cco"])
@pytest.mark.parametrize("foreign_owner", [None, "foreign"])
def test_rebuilt_slack_app_takes_over_from_live_predecessor(
        tmp_path, monkeypatch, caplog, launcher, foreign_owner):
    """Hermes rebuilds its AsyncApp on an in-process reconnect and re-runs
    the factory on the new app while the old supervisor still holds the
    scope lock; the successor must take over and wire its handlers."""
    from adapters.slack import tasks as slack_tasks
    monkeypatch.setattr(slack_tasks, "POLL_S", 0.01)
    monkeypatch.setattr(slack_tasks, "_LIVE", {}, raising=False)
    for name in ("slack_render", "flags", "cmd_int", "cmd_results"):
        (tmp_path / name).mkdir()
    (tmp_path / "flags" / "notify.json").write_text(
        json.dumps({"interactive": True, "transport": "slack"}))
    settings = {"transport": "slack", "data_root": str(tmp_path),
                "team_id": "T_SYNTHETIC", "application_id": "A_SYNTHETIC",
                "channel_id": "C_SYNTHETIC", "profile": "cco",
                "allowed_user_ids": {"U_OPERATOR"}, "project_ids": {1}}
    caplog.set_level("INFO", logger="hermes.plugin")

    class App:
        def __init__(self):
            self.client = FakeClient()
            self.handlers = []
            self.wired = asyncio.Event()

        def _wire(self, key):
            def deco(fn):
                self.handlers.append(key)
                self.wired.set()
            return deco

        def action(self, pattern):
            return self._wire(pattern)

        def view(self, name):
            return self._wire(name)

    config = {
        "slack_adapter_enabled": True, "slack_team_id": settings["team_id"],
        "slack_application_id": settings["application_id"],
        "slack_channel_id": settings["channel_id"], "slack_profile": "cco",
        "slack_allowed_user_ids": ["U_OPERATOR"], "project_ids": [1],
        "data_root": str(tmp_path),
    }

    class Ctx:
        profile_name = launcher

        def get_config(self, key, default=None):
            return config.get(key, default)

        def spawn_task(self, coro, name=None):
            return asyncio.ensure_future(coro)

        def on_unload(self, fn):
            pass

    factory = make_slack_factory(Ctx())

    async def scenario():
        supervisors = []
        key = registry.scope_key(settings)

        def connect(app, owner):
            native = SimpleNamespace(
                _owner_profile=owner,
                gateway_runner=SimpleNamespace(_primary_profile_name="default"),
                _get_client=lambda channel, team_id=None: app.client)
            supervisor = factory(app, native)
            if supervisor is not None:
                supervisors.append(supervisor)
            return supervisor

        old_app, new_app, foreign_app = App(), App(), App()
        try:
            assert connect(foreign_app, foreign_owner) is None
            assert key not in slack_tasks._LIVE
            old = connect(old_app, "cco")
            assert old is not None
            await asyncio.wait_for(old_app.wired.wait(), 5)
            assert slack_tasks._LIVE[key] is old

            assert connect(foreign_app, foreign_owner) is None
            assert slack_tasks._LIVE[key] is old
            assert not old._stopping and old._actions._active

            new = connect(new_app, "cco")
            assert new is not None
            await asyncio.wait_for(new_app.wired.wait(), 5)
            assert old._task.done()
            assert not old._actions._active and new._actions._active
            assert slack_tasks._LIVE[key] is new

            assert connect(foreign_app, foreign_owner) is None
            assert slack_tasks._LIVE[key] is new
            assert not new._stopping and new._actions._active
            assert foreign_app.handlers == foreign_app.client.calls == []
            assert not any("scope_lock_unavailable" in record.getMessage()
                           for record in caplog.records)
        finally:
            for supervisor in supervisors:
                supervisor.unload()
            await asyncio.wait_for(asyncio.gather(
                *(supervisor._task for supervisor in supervisors)), 5)
        assert key not in slack_tasks._LIVE

    asyncio.run(scenario())


def test_reconnects_do_not_keep_old_supervisors_alive_in_the_host(tmp_path, monkeypatch):
    """Each in-process reconnect starts a new supervisor on the same host;
    the host's unload list must not pin every superseded one for the
    process lifetime, and closing it still stops the newest."""
    import gc
    import weakref
    from adapters.slack import tasks as slack_tasks
    from mcs_standalone.host import Host
    monkeypatch.setattr(slack_tasks, "POLL_S", 0.01)
    monkeypatch.setattr(slack_tasks, "_LIVE", {}, raising=False)
    monkeypatch.setattr(slack_tasks, "_HOOKED", __import__("weakref").WeakKeyDictionary(), raising=False)
    for name in ("slack_render", "flags", "cmd_int", "cmd_results"):
        (tmp_path / name).mkdir()
    (tmp_path / "flags" / "notify.json").write_text(
        json.dumps({"interactive": True, "transport": "slack"}))
    config = {
        "slack_adapter_enabled": True, "slack_team_id": "T_SYNTHETIC",
        "slack_application_id": "A_SYNTHETIC", "slack_channel_id": "C_SYNTHETIC",
        "slack_profile": "cco", "slack_allowed_user_ids": ["U_OPERATOR"],
        "project_ids": [1], "data_root": str(tmp_path)}

    class App:
        def __init__(self):
            self.client = FakeClient()
            self.wired = asyncio.Event()

        def _wire(self, key):
            def deco(fn):
                self.wired.set()
            return deco

        def action(self, pattern):
            return self._wire(pattern)

        def view(self, name):
            return self._wire(name)

    class Ctx(Host):
        profile_name = "cco"

    async def scenario():
        host = Ctx(config)
        factory = make_slack_factory(host)
        gone = []
        current = None
        for _ in range(5):
            app = App()
            native = SimpleNamespace(
                _owner_profile="cco",
                gateway_runner=SimpleNamespace(_primary_profile_name="default"),
                _get_client=lambda channel, team_id=None, app=app: app.client)
            previous, current = current, factory(app, native)
            assert current is not None
            await asyncio.wait_for(app.wired.wait(), 5)
            if previous is not None:
                await asyncio.wait_for(previous._task, 5)
                gone.append(weakref.ref(previous))
            del previous, app, native
        gc.collect()
        assert [ref() for ref in gone] == [None] * 4      # superseded ones are released
        assert len(host._unload) == 1
        newest = current
        del current
        await host.close()
        assert newest._stopping and newest._task.done()

    asyncio.run(scenario())


def test_unload_hooks_follow_each_host_and_scope_without_pinning_hosts(monkeypatch):
    """One hook per host and scope: switching hosts A->B->A adds none twice,
    closing an old host leaves the newer host's supervisor running, and a
    closed host that is dropped is released."""
    import gc
    import weakref
    from adapters.slack import tasks as slack_tasks
    from mcs_standalone.host import Host
    monkeypatch.setattr(slack_tasks, "_LIVE", {})
    monkeypatch.setattr(slack_tasks, "_HOOKED", weakref.WeakKeyDictionary())

    class Live:
        def __init__(self, ctx):
            self._ctx, self.unloaded = ctx, 0

        def unload(self):
            self.unloaded += 1

    async def scenario():
        a, b = Host({}), Host({})
        for ctx in (a, b, a):
            slack_tasks._hook_unload(ctx, "scope-1")
        slack_tasks._hook_unload(a, "scope-2")
        assert len(a._unload) == 2 and len(b._unload) == 1   # no duplicate on A
        newest = slack_tasks._LIVE["scope-1"] = Live(b)
        await a.close()                                       # the old host
        assert newest.unloaded == 0
        await b.close()
        assert newest.unloaded == 1
        gone = weakref.ref(a)
        return gone

    gone = asyncio.run(scenario())
    gc.collect()
    assert gone() is None and not slack_tasks._LIVE and not len(slack_tasks._HOOKED)


def test_closing_the_host_before_the_task_runs_releases_the_supervisor(tmp_path, monkeypatch):
    """start() then an immediate close cancels a task that never ran, so its
    finally cannot clean up; the host's own unload must drop the live entry."""
    import gc
    import weakref
    from adapters.slack import tasks as slack_tasks
    from mcs_standalone.host import Host
    monkeypatch.setattr(slack_tasks, "_LIVE", {})
    monkeypatch.setattr(slack_tasks, "_HOOKED", weakref.WeakKeyDictionary())
    for name in ("slack_render", "flags", "cmd_int", "cmd_results"):
        (tmp_path / name).mkdir()
    settings = {"transport": "slack", "data_root": str(tmp_path),
                "team_id": "T_SYNTHETIC", "application_id": "A_SYNTHETIC",
                "channel_id": "C_SYNTHETIC", "profile": "cco",
                "allowed_user_ids": {"U_OPERATOR"}, "project_ids": {1}}
    app = SimpleNamespace(client=FakeClient(), action=lambda p: (lambda fn: fn),
                          view=lambda n: (lambda fn: fn))

    def sup(ctx):
        return slack_tasks.Supervisor(ctx=ctx, app=app, adapter=None, settings=settings,
                                      log=lambda *a, **k: None)

    async def run():
        host = Host({})
        first = sup(host)
        assert first.start()
        await host.close()                     # no yield: the task never started
        assert first._stopping and not slack_tasks._LIVE
        old, other = Host({}), Host({})
        assert sup(old).start()
        newer = sup(other)
        assert newer.start()                   # supersedes the old host's supervisor
        await old.close()
        assert slack_tasks._LIVE[registry.scope_key(settings)] is newer and not newer._stopping
        await other.close()
        assert newer._stopping and not slack_tasks._LIVE
        return weakref.ref(first), weakref.ref(host)

    refs = asyncio.run(run())
    gc.collect()
    assert [r() for r in refs] == [None, None]
