"""Real Slack SDK/Bolt serialization and Socket Mode dispatch without a network."""
import asyncio
from contextlib import suppress
import json
from pathlib import Path
import socket
import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("slack_bolt")
aiohttp = pytest.importorskip("aiohttp")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests/adapters/slack"))

from adapters.common import paths, registry  # noqa: E402
from adapters.common.spec import token_map  # noqa: E402
from adapters.slack import standalone  # noqa: E402
from adapters.slack.actions import Actions  # noqa: E402
from adapters.slack.delivery import SlackCardAdapter  # noqa: E402
from adapters.slack.paths import ensure_dirs  # noqa: E402
from slack_card_testkit import _spec  # noqa: E402
from slack_sdk.socket_mode.request import SocketModeRequest  # noqa: E402


class Wire:
    """The native HTTP/session edge; every body is newly invented."""
    closed = False

    def __init__(self):
        self.calls = []
        self.ephemerals = []
        self.views = []
        self.responses = []
        self.fail = None

    def request(self, method, url, **kwargs):
        assert kwargs["proxy"] is None and kwargs["allow_redirects"] is False
        assert kwargs["ssl"] is True
        assert standalone._url_allowed(url)
        op = str(url).rsplit("/", 1)[-1]
        params = kwargs.get("json") or kwargs.get("data") or kwargs.get("params") or {}
        self.calls.append((op, params))
        if self.fail == op:
            raise TimeoutError("fictional disconnected response")
        if "/upload/v1/" in str(url):
            payload, content_type = b"OK - 6", "text/plain"
        else:
            content_type = "application/json"
            data = {"ok": True}
            if op == "auth.test":
                data.update(team_id="T_SYNTHETIC", bot_id="B_SYNTHETIC", user_id="U_BOT")
            elif op == "bots.info":
                data["bot"] = {"id": "B_SYNTHETIC", "app_id": "A_SYNTHETIC", "deleted": False}
            elif op == "users.info":
                data["user"] = {"profile": {"display_name": "田中さん"}}
            elif op == "apps.connections.open":
                data["url"] = "wss://wss-primary.slack.com/?ticket=fictional"
            elif op == "chat.postMessage":
                data.update(channel="C_SYNTHETIC", ts="1790000000.000001")
            elif op == "chat.postEphemeral":
                self.ephemerals.append(params)
            elif op == "views.open":
                self.views.append(params)
                data["view"] = {"id": "V_SYNTHETIC"}
            elif op == "files.getUploadURLExternal":
                data.update(file_id="F_SYNTHETIC", upload_url="https://files.slack.com/upload/v1/fictional")
            elif op == "files.completeUploadExternal":
                data["files"] = [{"id": "F_SYNTHETIC"}]
            payload = json.dumps(data).encode()

        async def chunks(_size):
            yield payload

        return Reply(SimpleNamespace(status=200, headers={}, content_type=content_type,
                                     content=SimpleNamespace(iter_chunked=chunks)))

    async def close(self):
        self.closed = True


class Reply:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *_args):
        pass


class WebSocket:
    closed = False

    def __init__(self):
        self.acks = []

    async def send_str(self, body):
        self.acks.append(json.loads(body))

    async def close(self):
        self.closed = True

    async def ping(self, _message):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(3600)
        raise StopAsyncIteration


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *_a: pytest.fail("no real sockets"))


def test_real_sdk_upload_no_retries_and_closed_session_never_falls_back(monkeypatch):
    async def scenario():
        client, session = standalone._client({"bot_token": "xox" + "b-fictional"})
        wire = Wire()
        monkeypatch.setattr(session, "_live", lambda: wire)
        try:
            assert client.retry_handlers == [] and client.proxy is None
            assert await standalone._identity(client, {"team_id": "T_SYNTHETIC",
                                                       "application_id": "A_SYNTHETIC"})
            result = await client.files_upload_v2(channel="C_SYNTHETIC", thread_ts="1790000000.000001",
                                                file=b"sample", filename="sample.txt", title="sample.txt")
            assert result["ok"]
            assert [op for op, _ in wire.calls] == [
                "auth.test", "bots.info", "files.getUploadURLExternal",
                "fictional", "files.completeUploadExternal"]
            assert wire.calls[-1][1]["thread_ts"] == "1790000000.000001"
            wire.fail = "chat.postMessage"
            with pytest.raises(TimeoutError):
                await client.chat_postMessage(channel="C_SYNTHETIC", text="架空通知")
            assert sum(op == "chat.postMessage" for op, _ in wire.calls) == 1
        finally:
            await session.close()
        with pytest.raises(ValueError, match="session_closed"):
            await client.auth_test()
    asyncio.run(scenario())


def test_real_socket_mode_card_modal_preview_and_actor_confirm(tmp_path, monkeypatch):
    """Native SDK payloads retain the current card/human-command contracts."""
    async def scenario():
        credentials = {"bot_token": "xox" + "b-fictional", "app_token": "xapp-fictional"}
        client, session = standalone._client(credentials)
        wire = Wire()
        monkeypatch.setattr(session, "_live", lambda: wire)
        inflight = set()
        app, handler = await standalone._socket(client, credentials, inflight)
        ws = WebSocket()
        handler.client.current_session = ws
        spec = _spec()
        spec["parts"]["action_rows"][0][0].update(id="request", label="依頼")
        spec["parts"]["context"] = {"project_id": 123, "source_message_id": 456,
                                       "source_hash": "a" * 64}
        settings = {**spec["delivery"], "data_root": str(tmp_path / "data"),
                    "allowed_user_ids": frozenset({"U_OPERATOR", "U_OTHER"}),
                    "project_ids": frozenset({123})}
        for name in ("slack_render", "cmd_int", "cmd_results", "flags"):
            (tmp_path / "data" / name).mkdir(parents=True)
        dirs = ensure_dirs(settings["data_root"])
        reg = registry.Registry(dirs["state"], scope=settings)
        sender = SlackCardAdapter(app, team_id=settings["team_id"],
                                  application_id=settings["application_id"],
                                  channel_id=settings["channel_id"], profile=settings["profile"],
                                  allowed_user_ids=settings["allowed_user_ids"])
        actions = Actions(app, settings, dirs, reg, sender, lambda *_a, **_k: None)
        actions.register()
        monkeypatch.setattr("adapters.slack.actions.MODAL_OPEN_WAIT_S", 0)
        tasks = []

        async def dispatch(payload):
            request = SocketModeRequest(type="interactive", envelope_id="fictional-envelope",
                                        payload=payload, accepts_response_payload=True)
            await handler.client.socket_mode_request_listeners[0](handler.client, request)
            # Bolt returns after ack; its native listener finishes separately.
            for _ in range(100):
                if not inflight:
                    return
                await asyncio.sleep(0.01)
            pytest.fail("native Bolt listener did not finish")

        try:
            assert await sender.bind()
            outcome = await sender.perform(spec)
            assert outcome["result"] == "delivered"
            reg.put_tokens({token: {**context, "team_id": settings["team_id"],
                                    "channel_id": settings["channel_id"],
                                    "message_id": outcome["message_id"]}
                            for token, context in token_map(spec).items()})
            token = "b" * 32
            body = {"type": "block_actions", "team": {"id": "T_SYNTHETIC"},
                    "api_app_id": "A_SYNTHETIC", "channel": {"id": "C_SYNTHETIC"},
                    "user": {"id": "U_OPERATOR"}, "message": {"ts": outcome["message_id"]},
                    "trigger_id": "fictional-trigger",
                    "actions": [{"type": "button", "action_id": "mcs:a:" + token,
                                 "value": token}]}
            await dispatch(body)
            assert wire.views and ws.acks
            modal = wire.views[-1]["view"]
            if isinstance(modal, str):
                modal = json.loads(modal)
            env = json.loads(next(Path(dirs["cmd_int"]).glob("*.json")).read_text())
            paths.atomic_write(str(Path(dirs["cmd_results"]) / (env["request_id"] + ".json")),
                               json.dumps({"request_id": env["request_id"], "outcome": "applied",
                                           "modal": True, "params": {"project_id": 123}}).encode())
            view = {"type": "modal", "id": "V_SYNTHETIC", "callback_id": "mcs:modal",
                    "private_metadata": modal["private_metadata"],
                    "state": {"values": {name: {name: {"type": "plain_text_input", "value": value}}
                                           for name, value in {"task": "架空の確認依頼", "assignee": "",
                                                               "due_date": ""}.items()}}}
            await dispatch({"type": "view_submission", "team": body["team"],
                            "api_app_id": body["api_app_id"], "user": body["user"], "view": view})
            assert wire.ephemerals
            preview = wire.ephemerals[-1]
            assert preview["user"] == "U_OPERATOR"
            blocks = preview["blocks"]
            if isinstance(blocks, str):
                blocks = json.loads(blocks)
            confirm = blocks[1]["elements"][0]
            await dispatch({**body, "user": {"id": "U_OTHER"}, "actions": [confirm]})
            assert len(list(Path(dirs["cmd_int"]).glob("*.json"))) == 1
            await dispatch({**body, "actions": [confirm]})
            commands = [json.loads(path.read_text()) for path in Path(dirs["cmd_int"]).glob("*.json")]
            human = next(command for command in commands if command.get("cmd") == "request.create")
            assert human["human_confirmed"] is True and human["actor"] == "slack:T_SYNTHETIC:U_OPERATOR"
            assert human["title"] == "架空の確認依頼" and human["source_hash"] == "a" * 64
            await dispatch({**body, "actions": [confirm]})
            assert len(list(Path(dirs["cmd_int"]).glob("*.json"))) == 2
            assert all(ack["envelope_id"] == "fictional-envelope" for ack in ws.acks)
        finally:
            actions.unload()
            for task in tuple(inflight):
                task.cancel()
                tasks.append(task)
            await asyncio.gather(*tasks, return_exceptions=True)
            await handler.close_async()
            with suppress(asyncio.CancelledError):
                await handler.client.message_processor
            await session.close()
    asyncio.run(scenario())


def test_native_sessions_keep_guards_after_recreation_and_ws_redirect_is_rejected():
    async def scenario():
        session = standalone._GuardedSession(aiohttp, websocket=True)
        try:
            first = session._live()
            await first.close()
            second = session._live()
            assert second is not first and not second.trust_env
            params = SimpleNamespace(url="https://wss-primary.slack.com/?ticket=fictional")
            for trace in second.trace_configs:
                await trace.on_request_start.send(second, None, params)
                with pytest.raises(ValueError, match="redirect_not_allowed"):
                    await trace.on_request_redirect.send(second, None, params)
                with pytest.raises(ValueError, match="destination_not_allowed"):
                    await trace.on_request_start.send(second, None,
                                                     SimpleNamespace(url="https://evil.invalid/"))
        finally:
            await session.close()
    asyncio.run(scenario())


def test_real_supervisor_and_socket_start_only_after_scope_lock(tmp_path, monkeypatch):
    from mcs_standalone import config
    from adapters.slack.tasks import _LIVE

    async def scenario():
        spec = _spec()
        settings = {**spec["delivery"], "data_root": str(tmp_path / "data"),
                    "allowed_user_ids": frozenset({"U_OPERATOR"}),
                    "project_ids": frozenset({123})}
        credentials = {"bot_token": "xox" + "b-fictional", "app_token": "xapp-fictional"}
        for name in ("slack_render", "cmd_int", "cmd_results", "flags"):
            (tmp_path / "data" / name).mkdir(parents=True)
        (tmp_path / "data/flags/notify.json").write_text(json.dumps({
            "interactive": True, "transport": "slack"}))
        monkeypatch.setattr(config, "connector_settings", lambda *_a, **_k: dict(settings))
        monkeypatch.setattr(config, "load_credentials", lambda *_a: dict(credentials))
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
        wire = Wire()
        original_client, original_socket = standalone._client, standalone._socket
        clients, handlers, ws_calls = [], [], []
        stopping = asyncio.Event()

        def client_factory(values, verify_current):
            client, session = original_client(values, verify_current)
            monkeypatch.setattr(session, "_live", lambda: wire)
            clients.append(client)
            return client, session

        async def socket_factory(client, values, inflight):
            app, handler = await original_socket(client, values, inflight)

            async def connect(url, **kwargs):
                supervisor = _LIVE[registry.scope_key(settings)]
                assert supervisor._worker._lock_fd is not None
                assert supervisor._actions._active and len(app._async_listeners) == 5
                assert kwargs["proxy"] is None and kwargs["max_msg_size"] == 1024 * 1024
                assert kwargs["ssl"] is True
                ws_calls.append(url)
                stopping.set()
                return WebSocket()

            monkeypatch.setattr(handler.client.aiohttp_client_session, "_live", lambda: SimpleNamespace(
                closed=False, ws_connect=connect, close=wire.close))
            handlers.append(handler)
            return app, handler

        monkeypatch.setattr(standalone, "_client", client_factory)
        monkeypatch.setattr(standalone, "_socket", socket_factory)
        await standalone.run(tmp_path, stopping)
        assert len(ws_calls) == 1 and handlers[0].client.closed
        assert clients[0].session.closed and clients[0].proxy is None
        assert handlers[0].client.proxy is None
        assert registry.scope_key(settings) not in _LIVE
        assert any(op == "apps.connections.open" for op, _ in wire.calls)
    asyncio.run(scenario())


def test_socket_mode_rejects_ambient_oauth_and_redacts_sdk_debug(monkeypatch, caplog):
    async def scenario():
        credentials = {"bot_token": "xox" + "b-fictional", "app_token": "xapp-fictional"}
        client, session = standalone._client(credentials)
        wire = Wire()
        monkeypatch.setattr(session, "_live", lambda: wire)
        caplog.set_level(10)
        try:
            await client.chat_postMessage(channel="C_SYNTHETIC", text="fictional-private-body")
            assert "fictional-private-body" not in caplog.text
            monkeypatch.setenv("SLACK_CLIENT_ID", "fictional-client")
            monkeypatch.setenv("SLACK_CLIENT_SECRET", "fictional-secret")
            with pytest.raises(ValueError, match="ambient_oauth_not_allowed"):
                await standalone._socket(client, credentials, set())
            assert "fictional-secret" not in caplog.text
        finally:
            await session.close()
    asyncio.run(scenario())
