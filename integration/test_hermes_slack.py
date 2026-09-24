"""Hermes plugin discovery -> native Slack card, synthetic transport only.

Run with the Hermes test runner; no Slack connection or MCS data is used.
"""

import asyncio
import json
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

MCS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MCS_ROOT))
sys.path.insert(0, str(MCS_ROOT / "tests" / "plugin"))

from hermes_cli.plugins import PluginManager  # noqa: E402
from gateway.platforms.base import BasePlatformAdapter  # noqa: E402
from hermes_plugin.mcs_discord.cards import token_map  # noqa: E402
from test_mcs_slack_cards import _spec  # noqa: E402


def test_real_hermes_discovery_keeps_slack_inert_until_opt_in(tmp_path, monkeypatch):
    monkeypatch.setattr(socket.socket, "connect",
                        lambda *_: (_ for _ in ()).throw(
                            AssertionError("no network in integration test")))
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))
    plugin_name = "mcs-discord-commands"
    plugin_dir = home / "plugins" / plugin_name
    plugin_dir.parent.mkdir()
    plugin_dir.symlink_to(MCS_ROOT / "hermes_plugin", target_is_directory=True)
    base_settings = {
        "snapshot": str(tmp_path / "unused-snapshot.db"),
        "inbox": str(tmp_path / "unused-inbox"),
        "allowed_user_ids": ["discord-user"],
        "allowed_chat_ids": ["discord-channel"],
        "project_ids": [1],
    }

    def configure(settings):
        (home / "config.yaml").write_text(yaml.safe_dump({
            "plugins": {"enabled": [plugin_name], "entries": {
                plugin_name: {"settings": {**base_settings, **settings}},
            }},
        }))

    configure({})
    manager = PluginManager()
    manager.discover_and_load()
    factories = manager.get_platform_handler_factories("slack")
    assert any(name == plugin_name for _, name in factories)
    factory = next(fn for fn, name in factories if name == plugin_name)
    supervisors = []

    def observed_factory(native, adapter):
        supervisor = factory(native, adapter)
        if supervisor is not None:
            supervisors.append(supervisor)
        return supervisor

    manager._platform_handler_factories["slack"] = [
        (observed_factory if fn is factory else fn, name)
        for fn, name in factories
    ]
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager",
                        lambda: manager)
    host = SimpleNamespace(name="Slack", platform="slack",
                           _plugin_handler_native=None,
                           _plugin_handlers_wired=None)

    class NativeClient:
        retry_handlers = []

        def __init__(self):
            self.calls = []
            self.ready = asyncio.Event()

        async def auth_test(self):
            self.calls.append("auth_test")
            self.ready.set()
            return {"ok": True, "team_id": "T_SYNTHETIC"}

        async def chat_postMessage(self, **kwargs):
            self.calls.append(kwargs)
            return {"ok": True, "channel": kwargs["channel"],
                    "ts": "1790000000.000001"}

    def native_app():
        client = NativeClient()
        app = SimpleNamespace(client=client)
        registered = asyncio.Event()
        handlers = []

        def action(matcher):
            def attach(handler):
                handlers.append((matcher, handler))
                registered.set()
                return handler
            return attach

        app.action = action
        app.view = lambda matcher: lambda handler: handler
        return app, client, registered, handlers

    app, client, _, handlers = native_app()
    BasePlatformAdapter._wire_plugin_handlers(host, app)
    assert not supervisors
    assert not handlers
    assert not client.calls

    settings = {
        "slack_adapter_enabled": True,
        "slack_team_id": "T_SYNTHETIC",
        "slack_application_id": "A_SYNTHETIC",
        "slack_channel_id": "C_SYNTHETIC",
        "slack_profile": "cco",
        "slack_allowed_user_ids": ["U_OPERATOR"],
    }
    data_root = tmp_path / "mcs-data"
    for name in ("slack_render", "flags", "cmd_int", "cmd_results"):
        (data_root / name).mkdir(parents=True)
    (data_root / "flags" / "notify.json").write_text(json.dumps({
        "interactive": True, "transport": "slack",
    }))
    settings["data_root"] = str(data_root)
    configure(settings)
    # A same-app rewire cannot revive a factory that ran while disabled.
    BasePlatformAdapter._wire_plugin_handlers(host, app)
    assert not supervisors
    assert not handlers

    # Gateway restart creates a new native app, re-registers the factory
    # and leaves no stale Bolt listeners to consume its callbacks.
    fresh_app, fresh_client, registered, fresh_handlers = native_app()

    async def one_synthetic_card():
        BasePlatformAdapter._wire_plugin_handlers(host, fresh_app)
        assert len(supervisors) == 1
        supervisor = supervisors[0]
        try:
            await asyncio.wait_for(registered.wait(), timeout=2)
            spec = _spec()
            spec["parts"]["context"]["project_id"] = 1
            outcome = await supervisor._sender.perform(spec)
            supervisor._reg.put_tokens({
                token: {**context, "team_id": "T_SYNTHETIC",
                        "message_id": outcome["message_id"]}
                for token, context in token_map(spec).items()
            })
            token = spec["parts"]["action_rows"][0][0]["token"]
            body = {
                "team": {"id": "T_SYNTHETIC"},
                "api_app_id": "A_SYNTHETIC",
                "channel": {"id": "C_SYNTHETIC"},
                "user": {"id": "U_OPERATOR"},
                "message": {"ts": outcome["message_id"]},
            }

            async def ack():
                pass

            listener = next(callback for matcher, callback in fresh_handlers
                            if matcher.match("mcs:a:" + token))
            await listener(ack, body, {
                "action_id": "mcs:a:" + token, "value": token,
            })
            commands = list((data_root / "cmd_int").glob("*.json"))
            assert len(commands) == 1
            command = json.loads(commands[0].read_text())
            assert command["op"] == "notification"
            assert command["actor"] == "slack:T_SYNTHETIC:U_OPERATOR"
            return outcome
        finally:
            supervisor.unload()
            supervisor._task.cancel()
            try:
                await supervisor._task
            except asyncio.CancelledError:
                pass

    assert asyncio.run(one_synthetic_card()) == {
        "result": "delivered", "message_id": "1790000000.000001",
    }
    assert len(fresh_client.calls) == 2
    assert len(fresh_handlers) == 2


def test_connected_sdk_client_posts_card_without_retrying(tmp_path, monkeypatch):
    """The actual Hermes SDK client must work without mutating shared retries."""
    from hermes_plugin.mcs_slack.delivery import SlackCardAdapter
    from slack_sdk.web import async_base_client
    from slack_sdk.web.async_client import AsyncWebClient

    monkeypatch.setattr(socket.socket, "connect",
                        lambda *_: (_ for _ in ()).throw(
                            AssertionError("no network in integration test")))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    seen = []

    async def fake_http(**request):
        method = request["api_url"].rsplit("/", 1)[-1]
        seen.append((method, request["retry_handlers"], request["req_args"]))
        data = ({"ok": True, "team_id": "T_SYNTHETIC"}
                if method == "auth.test" else
                {"ok": True, "channel": "C_SYNTHETIC",
                 "ts": "1790000000.000001"})
        return {"data": data, "headers": {}, "status_code": 200}

    monkeypatch.setattr(async_base_client, "_request_with_session", fake_http)
    client = AsyncWebClient(token="xoxb-synthetic")
    original_handlers = client.retry_handlers
    assert original_handlers
    adapter = SlackCardAdapter(
        SimpleNamespace(client=client),
        team_id="T_SYNTHETIC", application_id="A_SYNTHETIC",
        channel_id="C_SYNTHETIC", profile="cco",
        allowed_user_ids={"U_OPERATOR"},
    )

    async def send():
        assert await adapter.bind()
        return await adapter.perform(_spec())

    assert asyncio.run(send()) == {
        "result": "delivered", "message_id": "1790000000.000001",
    }
    assert [method for method, _, _ in seen] == ["auth.test", "chat.postMessage"]
    assert seen[1][1] == []
    assert seen[1][2]["json"]["blocks"][0]["type"] == "header"
    assert client.retry_handlers is original_handlers
