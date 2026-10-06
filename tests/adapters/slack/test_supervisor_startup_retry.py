"""Regression: a transient auth.test failure at gateway start used to stop
the Slack supervisor for good (until a gateway restart)."""
import asyncio
import json
from types import SimpleNamespace

from adapters.common import worker
from hermes_plugin.card_workers import make_slack_factory
from hermes_plugin.mcs_slack.tasks import Supervisor
from slack_testkit import FakeClient


class _App:
    def __init__(self, client):
        self.client = client

    def action(self, _pattern):
        return lambda fn: fn

    view = command = action


def test_bind_failure_is_retried_until_the_workspace_binds(tmp_path, monkeypatch):
    settings = {
        "slack_adapter_enabled": True, "slack_team_id": "T_SYNTHETIC",
        "slack_application_id": "A_SYNTHETIC", "slack_channel_id": "C_SYNTHETIC",
        "slack_allowed_user_ids": ["U_OPERATOR"], "slack_profile": "cco",
        "project_ids": [1], "data_root": str(tmp_path)}
    for name in ("slack_render", "flags", "cmd_int", "cmd_results"):
        (tmp_path / name).mkdir()
    (tmp_path / "flags" / "notify.json").write_text(
        json.dumps({"interactive": True, "transport": "slack"}))
    monkeypatch.setattr(Supervisor, "start", lambda self: None)
    real_sleep = asyncio.sleep
    waits = []

    async def fast_sleep(seconds):
        waits.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(worker.asyncio, "sleep", fast_sleep)
    client = FakeClient()
    auth = client.auth_test
    fails = [2]

    async def flaky_auth():
        if fails[0]:
            fails[0] -= 1
            raise OSError("synthetic network blip")
        return await auth()

    client.auth_test = flaky_auth
    logs = []
    ctx = SimpleNamespace(get_config=lambda key, default=None: settings.get(key, default))

    async def scenario():
        sup = make_slack_factory(ctx)(_App(client), None)
        sup._log = lambda event, **fields: logs.append(event)
        task = asyncio.ensure_future(sup._run())
        for _ in range(200):
            if sup._actions._active:
                break
            await real_sleep(0)
        assert sup._actions._active          # bound after the blips
        sup.unload()
        await asyncio.wait_for(task, 5)

    asyncio.run(scenario())
    assert logs.count("workspace_bind_failed") == 2
    assert waits[:2] == [worker.POLL_S, 2 * worker.POLL_S]
