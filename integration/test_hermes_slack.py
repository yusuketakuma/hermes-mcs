"""Hermes plugin discovery -> native Slack card, synthetic transport only.

Run with the Hermes test runner; no Slack connection or MCS data is used.
"""

import asyncio
import json
import socket
from contextlib import suppress
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

MCS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MCS_ROOT))
sys.path.insert(0, str(MCS_ROOT / "tests" / "adapters" / "slack"))

from hermes_cli.plugins import PluginManager  # noqa: E402
from gateway.platforms.base import BasePlatformAdapter  # noqa: E402
from hermes_plugin.mcs_delivery.spec import token_map  # noqa: E402
from slack_card_testkit import _spec  # noqa: E402


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
    host._get_client = lambda channel_id, team_id=None: host._plugin_handler_native.client

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
        app.native_commands = {}
        def attach_command(name):
            def attach(handler):
                app.native_commands[name] = handler
                return handler
            return attach
        app.command = attach_command
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
            assert set(fresh_app.native_commands) == {"/mcs", "/mcs-summary"}
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
            with suppress(asyncio.CancelledError):
                await supervisor._task

    assert asyncio.run(one_synthetic_card()) == {
        "result": "delivered", "message_id": "1790000000.000001",
    }
    assert len(fresh_client.calls) == 2
    # The native app exposes all four protocol actions, exactly once.
    # Keeping the earlier disabled-app assertions protects opt-in semantics.
    from hermes_plugin.mcs_slack.cards import LINK_ACTION, MENU_ACTION

    assert len(fresh_handlers) == 4
    for action_id, expected_callback in (
        ("mcs:a:" + "0" * 32, "_action"),
        (LINK_ACTION, "_link"),
        (MENU_ACTION, "_action"),
        ("mcs:c:" + "0" * 16, "_confirm"),
        ("mcs:c:" + "0" * 16 + ":cancel", "_confirm"),
    ):
        matches = [callback for matcher, callback in fresh_handlers
                   if matcher.fullmatch(action_id)]
        assert len(matches) == 1
        assert matches[0].__name__ == expected_callback
    for invalid_id in ("unrelated", "mcs:a:invalid", "mcs:c:invalid"):
        assert not any(matcher.fullmatch(invalid_id)
                       for matcher, _ in fresh_handlers)


def test_connected_secondary_sdk_client_posts_card_without_retrying(tmp_path, monkeypatch):
    """Hermes's workspace lookup selects the secondary SDK client without retrying."""
    from hermes_plugin.mcs_slack.delivery import SlackCardAdapter
    from plugins.platforms.slack.adapter import SlackAdapter
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
    client = AsyncWebClient(token="bot-synthetic")
    original_handlers = client.retry_handlers
    assert original_handlers
    native_adapter = object.__new__(SlackAdapter)
    native_adapter._team_clients = {"T_SYNTHETIC": client}
    native_adapter._channel_team = {}
    native_adapter._app = SimpleNamespace(client=object())
    adapter = SlackCardAdapter(
        native_adapter._app, native_adapter=native_adapter,
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


def test_real_sdk_drives_thread_parts_and_upload(tmp_path, monkeypatch):
    """The durable part path over the REAL AsyncWebClient — every wire
    call goes through the stubbed HTTP layer with retry_handlers=[],
    body chunks post under thread_ts, and files.uploadV2 runs its
    three-step external upload into the same thread."""
    import hashlib

    from hermes_plugin.mcs_delivery import envelopes, journal, registry
    from hermes_plugin.mcs_slack import paths as slack_paths
    from hermes_plugin.mcs_slack.delivery import (
        DeliveryWorker, SlackCardAdapter)
    from plugins.platforms.slack.adapter import SlackAdapter
    from slack_sdk.web import async_base_client
    from slack_sdk.web.async_client import AsyncWebClient

    monkeypatch.setattr(socket.socket, "connect",
                        lambda *_: (_ for _ in ()).throw(
                            AssertionError("no network in integration test")))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    blob = b"synthetic-upload-bytes"
    root = tmp_path / "mcs-data"
    f = root / "attachments" / "syn.bin"
    f.parent.mkdir(parents=True)
    f.write_bytes(blob)
    replies = []
    seen = []
    ts_n = [1]

    async def fake_http(**request):
        url = request["api_url"]
        method = ("upload" if "files.slack.com" in url
                  else url.rsplit("/", 1)[-1])
        seen.append((method, request["retry_handlers"], request["req_args"]))
        ts_n[0] += 1
        if method == "auth.test":
            data = {"ok": True, "team_id": "T_SYNTHETIC",
                    "bot_id": "B_INT", "user_id": "U_BOT"}
        elif method == "upload":
            return {"status_code": 200, "body": b"", "headers": {}}
        elif method == "files.getUploadURLExternal":
            data = {"ok": True,
                    "upload_url": "https://files.slack.com/upload/v1/SYN",
                    "file_id": "F_INT_0001"}
        elif method == "files.completeUploadExternal":
            data = {"ok": True, "files": [{"id": "F_INT_0001"}]}
        elif method == "chat.postMessage":
            js = request["req_args"].get("json", {})
            ts = f"1790000000.{ts_n[0]:06d}"
            if js.get("thread_ts"):
                replies.append({"ts": ts, "text": js.get("text"),
                                "bot_id": "B_INT"})
            data = {"ok": True, "channel": js.get("channel"), "ts": ts}
        elif method == "conversations.replies":
            root = request["req_args"].get("params", {}).get("ts")
            data = {"ok": True, "messages": [
                {"ts": root, "text": "<card>", "bot_id": "B_INT"},
                *replies]}
        else:
            data = {"ok": True}
        return {"data": data, "headers": {}, "status_code": 200}

    monkeypatch.setattr(async_base_client, "_request_with_session", fake_http)
    client = AsyncWebClient(token="bot-synthetic")
    native_adapter = object.__new__(SlackAdapter)
    native_adapter._team_clients = {"T_SYNTHETIC": client}
    native_adapter._channel_team = {}
    native_adapter._app = SimpleNamespace(client=object())
    adapter = SlackCardAdapter(
        native_adapter._app, native_adapter=native_adapter,
        team_id="T_SYNTHETIC", application_id="A_SYNTHETIC",
        channel_id="C_SYNTHETIC", profile="cco",
        allowed_user_ids={"U_OPERATOR"})

    scope = {"transport": "slack", "profile": "cco",
             "application_id": "A_SYNTHETIC", "team_id": "T_SYNTHETIC",
             "channel_id": "C_SYNTHETIC"}
    for name in ("slack_render", "flags", "cmd_int", "cmd_results"):
        (root / name).mkdir(parents=True)
    dirs = slack_paths.ensure_dirs(str(root))
    (root / "flags" / "notify.json").write_text(json.dumps({
        "interactive": True, "transport": "slack",
    }))
    reg = registry.Registry(dirs["state"], scope=scope)
    worker = DeliveryWorker(
        sender=adapter, settings=scope, root=str(root), reg=reg,
        worker_id=registry.new_worker_id(), log=lambda *_a, **_k: None)

    spec = _spec()
    spec["delivery"].update(scope)
    parts = spec["parts"]
    parts["thread_body_parts"] = ["chunk-1 of the synthetic body",
                                  "chunk-2 of the synthetic body"]

    def _sha(t):
        return hashlib.sha256(t.encode("utf-8")).hexdigest()
    card_sha = hashlib.sha256(json.dumps(
        {"containers": parts["containers"], "footer": parts["footer"],
         "action_rows": parts["action_rows"]},
        sort_keys=True, separators=(",", ":"),
        ensure_ascii=False).encode("utf-8")).hexdigest()
    parts["manifest"] = [
        {"part_id": "card", "kind": "card", "index": 0,
         "sha256": card_sha},
        {"part_id": "thread", "kind": "thread", "index": 1,
         "name": "synthetic-thread",
         "sha256": _sha("synthetic-thread")},
        {"part_id": "body:0001", "kind": "body_part", "index": 2,
         "sha256": _sha(parts["thread_body_parts"][0]),
         "bytes": len(parts["thread_body_parts"][0].encode())},
        {"part_id": "body:0002", "kind": "body_part", "index": 3,
         "sha256": _sha(parts["thread_body_parts"][1]),
         "bytes": len(parts["thread_body_parts"][1].encode())},
        {"part_id": "attach:0001", "kind": "attachment_part", "index": 4,
         "attachment_id": 1, "name": "syn.bin", "path": str(f),
         "sha256": hashlib.sha256(blob).hexdigest(),
         "bytes": len(blob)},
    ]
    claim = {"spec": spec, "attempt_id": "a" * 16,
             "worker_id": worker._worker_id,
             "payload_hash": envelopes.payload_hash(spec),
             "spec_path": None, "phase": "settled"}

    async def drive():
        assert await adapter.bind()
        out = await adapter.perform(spec)
        await worker._deliver_parts(claim, out["message_id"])
        return out

    card_ts = asyncio.run(drive())
    assert card_ts["result"] == "delivered"
    root_ts = card_ts["message_id"]
    methods = [m for m, _, _ in seen]
    # card post + two thread replies + the upload trio — no hidden call
    assert methods.count("chat.postMessage") == 3
    assert methods.count("files.getUploadURLExternal") == 1
    assert methods.count("upload") == 1
    assert methods.count("files.completeUploadExternal") == 1
    # every send-side call ran with retries stripped — the shared
    # client's own list is untouched and still the SDK default
    assert all(handlers == [] for _, handlers, _ in seen[1:])
    assert client.retry_handlers
    posts = [r for m, _, r in seen if m == "chat.postMessage"]
    assert posts[0]["json"].get("thread_ts") is None   # the card itself
    assert all(p["json"]["thread_ts"] == root_ts for p in posts[1:])
    assert [p["json"]["text"] for p in posts[1:]] \
        == spec["parts"]["thread_body_parts"]
    complete = next(r for m, _, r in seen
                    if m == "files.completeUploadExternal")
    assert complete["params"]["channel_id"] == "C_SYNTHETIC"
    assert complete["params"]["thread_ts"] == root_ts
    assert "F_INT_0001" in complete["params"]["files"]
    # the exact verified bytes hit the external upload URL
    raw = next(r for m, _, r in seen if m == "upload")
    assert raw["data"] == blob
    # every part journaled its own delivered receipt with a remote id
    rows = [r for rs in journal.scan(dirs["state"]).values() for r in rs
            if r.get("phase") == "receipt" and r.get("part_id")]
    assert {r["part_id"] for r in rows} \
        == {"thread", "body:0001", "body:0002", "attach:0001"}
