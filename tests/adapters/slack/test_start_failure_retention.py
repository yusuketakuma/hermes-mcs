"""A refused supervisor start must not retain stopped Slack connections by scope."""
import asyncio

from adapters.common.registry import scope_key
from adapters.slack import tasks
from mcs_standalone.host import Host
from slack_testkit import FakeClient
from test_supervisor_startup_retry import _App


def test_repeated_refused_starts_release_global_supervisor_ownership(tmp_path):
    for name in ("slack_render", "flags", "cmd_int", "cmd_results"):
        (tmp_path / name).mkdir()
    keys = []

    async def scenario():
        host = Host({})
        await host.close()
        for index in range(32):
            settings = {"transport": "slack", "data_root": str(tmp_path),
                        "team_id": "T_SYNTHETIC", "application_id": "A_SYNTHETIC",
                        "channel_id": "C_SYNTHETIC", "profile": f"refused-{index}",
                        "allowed_user_ids": frozenset({"U_OPERATOR"}),
                        "project_ids": frozenset({1})}
            keys.append(scope_key(settings))
            supervisor = tasks.Supervisor(ctx=host, app=_App(FakeClient()), adapter=None,
                                          settings=settings, log=lambda *_a, **_kw: None)
            assert supervisor.start() is False
            assert supervisor._task is None and not host.tasks
        assert all(key not in tasks._LIVE for key in keys)

    try:
        asyncio.run(scenario())
    finally:
        for key in keys:
            tasks._LIVE.pop(key, None)
