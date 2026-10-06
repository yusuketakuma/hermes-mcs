"""Legacy runtime aliases must close every owned resource despite cleanup errors."""
import asyncio
import sys
from types import SimpleNamespace

import pytest

from adapters.discord import runtime_compat as discord
from adapters.slack import runtime_compat as slack
from mcs_standalone import runtime


@pytest.mark.parametrize("failure", ["socket", "host"])
def test_slack_compat_closes_all_resources(monkeypatch, failure):
    closed = []

    async def close(name):
        closed.append(name)
        if name == failure:
            raise RuntimeError("synthetic_cleanup_failure")

    async def identity():
        return {"team_id": "synthetic"}

    session = SimpleNamespace(_aiohttp=None, close=lambda: close("session"))
    client = SimpleNamespace(auth_test=identity, session=session)
    socket = SimpleNamespace(client=SimpleNamespace(proxy=None, aiohttp_client_session=SimpleNamespace(
        close=lambda: close("old_session"))), connect_async=lambda: asyncio.sleep(0),
        close_async=lambda: close("socket"))
    monkeypatch.setattr(slack, "_client", lambda _token: client)
    monkeypatch.setattr(slack, "_app", lambda _client: SimpleNamespace())
    monkeypatch.setattr(slack, "Host", lambda _settings: SimpleNamespace(close=lambda: close("host")))
    monkeypatch.setattr(slack.standalone, "_GuardedSession", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setitem(sys.modules, "slack_bolt.adapter.socket_mode.async_handler",
                        SimpleNamespace(AsyncSocketModeHandler=lambda *_args, **_kwargs: socket))

    def supervisor(**_kwargs):
        return SimpleNamespace(start=lambda: True, _task=asyncio.create_task(asyncio.sleep(0)))

    monkeypatch.setattr(slack, "Supervisor", supervisor)

    async def run():
        with pytest.raises(RuntimeError):
            await slack.run({"team_id": "synthetic"},
                            {"SLACK_BOT_TOKEN": "synthetic", "SLACK_APP_TOKEN": "synthetic"},
                            asyncio.Event())
        await asyncio.sleep(0)
        assert not (asyncio.all_tasks() - {asyncio.current_task()})

    asyncio.run(run())
    assert closed == ["old_session", "socket", "host", "session"]


def test_discord_compat_closes_bot_after_host_failure(monkeypatch):
    closed = []

    async def host_close():
        closed.append("host")
        raise RuntimeError("synthetic_cleanup_failure")

    async def bot_close():
        closed.append("bot")

    async def commands(*_args):
        return [{"name": "synthetic", "description": "synthetic"}]

    bot = SimpleNamespace(application_id=1, tree=SimpleNamespace(add_command=lambda *_args: None),
                          login=lambda *_args: asyncio.sleep(0), connect=lambda **_kwargs: asyncio.sleep(0),
                          close=bot_close, http=SimpleNamespace(get_global_commands=commands))
    monkeypatch.setattr(discord, "_client", lambda **_kwargs: bot)
    monkeypatch.setattr(discord, "Host", lambda _settings: SimpleNamespace(close=host_close))
    monkeypatch.setattr(discord, "_mcs_command", lambda *_args: SimpleNamespace(
        to_dict=lambda *_args: {"name": "synthetic", "description": "synthetic"}))
    monkeypatch.setattr(discord, "Supervisor", lambda **_kwargs: SimpleNamespace(
        start=lambda: True, _task=asyncio.create_task(asyncio.sleep(0))))

    async def run():
        with pytest.raises(RuntimeError, match="synthetic_cleanup_failure"):
            await discord.run({"application_id": "1"}, {"DISCORD_BOT_TOKEN": "synthetic"},
                              asyncio.Event())
        await asyncio.sleep(0)
        assert not (asyncio.all_tasks() - {asyncio.current_task()})

    asyncio.run(run())
    assert closed == ["host", "bot"]


def test_unrepresentable_restart_timestamp_is_ignored(monkeypatch, tmp_path):
    instance = object.__new__(runtime.Runtime)
    instance.data = tmp_path
    instance.generation = "a" * 32
    monkeypatch.setattr(runtime.config, "_private_bytes", lambda *_args: (
        '{"generation":"' + "a" * 32 + '","request_id":"' + "b" * 32
        + '","requested_at":' + str(10 ** 400) + '}').encode())
    assert instance._restart_requested(1) is False
