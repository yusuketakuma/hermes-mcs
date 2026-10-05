"""Legacy Slack aliases retain the native SDK's fixed, isolated wire boundary."""
import asyncio
import json
from types import SimpleNamespace

import pytest

aiohttp = pytest.importorskip("aiohttp")
pytest.importorskip("slack_bolt")

from mcs_standalone import slack_runtime as legacy  # noqa: E402


class Response:
    status, content_type = 200, "application/json"
    headers = {}

    def __init__(self):
        self.raw = json.dumps({"ok": True, "team_id": "T_SYNTHETIC", "bot_id": "B_SYNTHETIC",
                               "user_id": "U_SYNTHETIC", "channel": "C_SYNTHETIC",
                               "ts": "1790000000.000001"}).encode()
        self.content = SimpleNamespace(iter_chunked=self.chunks)

    async def chunks(self, _size):
        yield self.raw

    async def json(self, **kwargs):
        return json.loads(self.raw)

    async def text(self, **kwargs):
        return self.raw.decode()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class Session:
    closed, trust_env = False, False

    def __init__(self, calls):
        self.calls = calls

    def request(self, method, url, **kwargs):
        self.calls.append((method, str(url), kwargs))
        return Response()

    def ws_connect(self, url, **kwargs):
        self.calls.append(("WS", str(url), kwargs))
        return object()

    async def close(self):
        self.closed = True


@pytest.fixture
def wire(monkeypatch):
    calls = []
    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **kw: Session(calls))
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    return calls


def test_legacy_send_never_uses_proxy_or_redirects(wire):
    assert asyncio.run(legacy.send("xox" + "b-fictional", "C_SYNTHETIC", "合成本文", []))
    assert len(wire) == 1
    assert wire[0][2].get("proxy") is None
    assert wire[0][2].get("allow_redirects") is False


def test_legacy_sdk_rejects_foreign_base_url_before_authenticated_request(wire):
    async def scenario():
        client = legacy._client("xox" + "b-fictional")
        client.base_url = "https://foreign.invalid/api/"
        try:
            with pytest.raises(ValueError, match="destination_not_allowed"):
                await client.auth_test()
            assert not wire
        finally:
            if client.session is not None:
                await client.session.close()
    asyncio.run(scenario())


def test_legacy_app_rejects_ambient_oauth(wire, monkeypatch):
    monkeypatch.setenv("SLACK_CLIENT_ID", "fictional-id")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "fictional-secret")
    client = legacy._client("xox" + "b-fictional")
    with pytest.raises(ValueError, match="ambient_oauth_not_allowed"):
        legacy._app(client)
    assert not wire


@pytest.mark.parametrize("boundary", ["proxy", "foreign_url"])
def test_legacy_socket_rejects_foreign_handshake_without_proxy(wire, tmp_path, monkeypatch, boundary):
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

    async def scenario():
        stopping = asyncio.Event()
        tasks = []

        class Supervisor:
            def __init__(self, **kwargs):
                pass

            def start(self):
                self._task = asyncio.create_task(asyncio.Event().wait())
                tasks.append(self._task)
                return True

        async def close_host():
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        async def connect(handler):
            stopping.set()
            if boundary == "proxy":
                assert handler.client.proxy is None
            else:
                with pytest.raises(ValueError, match="destination_not_allowed"):
                    handler.client.aiohttp_client_session.ws_connect("https://foreign.invalid/")

        async def close_handler(handler):
            await handler.client.aiohttp_client_session.close()

        monkeypatch.setattr(legacy, "Host", lambda settings: SimpleNamespace(close=close_host))
        monkeypatch.setattr(legacy, "Supervisor", Supervisor)
        monkeypatch.setattr(AsyncSocketModeHandler, "connect_async", connect)
        monkeypatch.setattr(AsyncSocketModeHandler, "close_async", close_handler)
        await legacy.run({"team_id": "T_SYNTHETIC", "data_root": str(tmp_path)},
                         {"SLACK_BOT_TOKEN": "xox" + "b-fictional",
                          "SLACK_APP_TOKEN": "xapp-fictional"}, stopping)
        assert not any(method == "WS" for method, *_ in wire)
    asyncio.run(scenario())
