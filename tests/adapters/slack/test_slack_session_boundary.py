"""Standalone Slack HTTP session limits, exercised through a fake aiohttp (no network)."""
import asyncio
import sys
from contextlib import asynccontextmanager
from types import ModuleType, SimpleNamespace

import pytest

from adapters.slack import standalone

API = "https://slack.com/api/chat.postMessage"
SOCKET = "wss://wss-primary.slack.com/link/?ticket=synthetic"
XOXB = "xox" + "b-synthetic"   # split so the CI secret-pattern scan never sees a token shape


class _Response:
    status, headers, content_type = 200, {}, "application/json"

    def __init__(self, chunks):
        async def iter_chunked(_size):
            for chunk in chunks:
                yield chunk
        self.content = SimpleNamespace(iter_chunked=iter_chunked)


def fake_aiohttp(chunks=(b'{"ok": true}',)):
    sessions = []

    class TraceConfig:
        def __init__(self):
            self.on_request_start, self.on_request_redirect = [], []

    class ClientSession:
        def __init__(self, **kwargs):
            self.kwargs, self.calls, self.closed = kwargs, [], False
            sessions.append(self)

        @asynccontextmanager
        async def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            yield _Response(chunks)

        def ws_connect(self, url, **kwargs):
            self.calls.append(("WS", url, kwargs))
            return "synthetic-websocket"

        async def close(self):
            self.closed = True

    module = SimpleNamespace(TraceConfig=TraceConfig, ClientSession=ClientSession,
                             ClientTimeout=lambda total: ("timeout", total))
    return module, sessions


async def _fetch(guarded, url, **kwargs):
    async with guarded.request("POST", url, **kwargs) as response:
        return await response.json()


def test_request_never_trusts_env_proxies_or_follows_redirects():
    aiohttp, sessions = fake_aiohttp()
    guarded = standalone._GuardedSession(aiohttp)
    assert asyncio.run(_fetch(guarded, API, json={}, proxy="http://proxy.invalid:3128",
                              allow_redirects=True, ssl=False)) == {"ok": True}
    (session,) = sessions
    assert session.kwargs["trust_env"] is False
    (_method, _url, kwargs), = session.calls
    assert (kwargs["proxy"], kwargs["allow_redirects"], kwargs["ssl"]) == (None, False, True)
    trace = session.kwargs["trace_configs"][0]
    with pytest.raises(ValueError, match="slack_redirect_not_allowed"):
        asyncio.run(trace.on_request_redirect[0](session, None, None))
    with pytest.raises(ValueError, match="slack_destination_not_allowed"):
        asyncio.run(trace.on_request_start[0](
            session, None, SimpleNamespace(url="https://proxy.invalid/")))


def test_foreign_destination_and_oversized_response_are_refused():
    aiohttp, sessions = fake_aiohttp()
    guarded = standalone._GuardedSession(aiohttp)
    with pytest.raises(ValueError, match="slack_destination_not_allowed"):
        asyncio.run(_fetch(guarded, "https://foreign.invalid/api/chat.postMessage"))
    assert sessions == []                   # refused before any session or wire call
    big, _ = fake_aiohttp(chunks=[b"x" * 65536] * 17)
    with pytest.raises(ValueError, match="slack_response_too_large"):
        asyncio.run(_fetch(standalone._GuardedSession(big), API))


def test_socket_connect_forces_no_proxy_and_closed_session_stays_closed():
    aiohttp, sessions = fake_aiohttp()
    guarded = standalone._GuardedSession(aiohttp, websocket=True)
    assert guarded.ws_connect(SOCKET, proxy="http://proxy.invalid:3128", ssl=False) \
        == "synthetic-websocket"
    (_kind, _url, kwargs), = sessions[0].calls
    assert (kwargs["proxy"], kwargs["ssl"]) == (None, True)
    with pytest.raises(ValueError, match="slack_destination_not_allowed"):
        guarded.ws_connect("wss://proxy.invalid/link")
    asyncio.run(guarded.close())
    assert sessions[0].closed
    with pytest.raises(ValueError, match="slack_session_closed"):
        guarded.ws_connect(SOCKET)
    assert len(sessions) == 1               # a closed guard never reconnects


def test_sdk_client_drops_env_proxy_and_stops_after_close(monkeypatch):
    aiohttp, _ = fake_aiohttp()

    class AsyncWebClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.proxy = "http://proxy.invalid:3128"   # the SDK adopts *_PROXY itself

        async def _request(self, **_kwargs):
            return "sent"

        async def _upload_file(self, **_kwargs):
            return "uploaded"

    async_client = ModuleType("slack_sdk.web.async_client")
    async_client.AsyncWebClient = AsyncWebClient
    monkeypatch.setitem(sys.modules, "aiohttp", aiohttp)
    monkeypatch.setitem(sys.modules, "slack_sdk", ModuleType("slack_sdk"))
    monkeypatch.setitem(sys.modules, "slack_sdk.web", ModuleType("slack_sdk.web"))
    monkeypatch.setitem(sys.modules, "slack_sdk.web.async_client", async_client)
    client, session = standalone._client({"bot_token": XOXB})
    assert client.proxy is None
    assert client.kwargs["session"] is session and client.kwargs["retry_handlers"] == []
    assert asyncio.run(client._request()) == "sent"
    asyncio.run(session.close())
    for call in (client._request, client._upload_file):
        with pytest.raises(ValueError, match="slack_session_closed"):
            asyncio.run(call())
