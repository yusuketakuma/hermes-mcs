"""Legacy Discord aliases enforce the existing SDK session boundary offline."""
import asyncio
import json
from types import SimpleNamespace

import pytest

discord = pytest.importorskip("discord")

from mcs_standalone import discord_runtime as legacy  # noqa: E402


class Response:
    status, reason = 200, "synthetic"
    headers = {"content-type": "application/json"}

    async def text(self, **kwargs):
        return json.dumps({"id": "4242", "username": "MCS", "discriminator": "0000",
                           "avatar": None, "bot": True})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class Session:
    trust_env, closed = False, False

    def __init__(self, calls):
        self.calls = calls

    def request(self, method, url, **kwargs):
        self.calls.append((method, str(url), kwargs))
        return Response()

    async def close(self):
        self.closed = True


@pytest.mark.parametrize("gateway", [False, True])
@pytest.mark.parametrize("boundary", ["proxy_redirect", "foreign_url"])
def test_legacy_client_guards_every_authenticated_sdk_request(monkeypatch, gateway, boundary):
    async def scenario():
        calls = []
        bot = legacy._client(gateway=gateway)
        await bot._async_setup_hook()
        bot.http.token = "fictional-token"
        bot.http._global_over = asyncio.Event()
        bot.http._global_over.set()
        bot.http._HTTPClient__session = Session(calls)
        bot.http.proxy, bot.http.proxy_auth = "http://proxy.invalid:3128", "fictional"
        route = discord.http.Route("GET", "/users/@me")
        if boundary == "foreign_url":
            route.url = "https://foreign.invalid/api/"
        try:
            if boundary == "foreign_url":
                with pytest.raises(ValueError, match="endpoint_forbidden"):
                    await bot.http.request(route)
                assert not calls
            else:
                await bot.http.request(route)
                assert len(calls) == 1
                kwargs = calls[0][2]
                assert kwargs.get("allow_redirects") is False
                assert kwargs.get("proxy") is kwargs.get("proxy_auth") is None
        finally:
            await bot.close()
    asyncio.run(scenario())


def test_legacy_private_command_followup_never_retries_unknown_post(monkeypatch):
    async def scenario():
        calls = []
        bot = legacy._client(gateway=False)
        await bot._async_setup_hook()

        class Failure(Response):
            status = 500

            async def text(self, **kwargs):
                return '{"message":"synthetic uncertainty","code":0}'

        class FailedSession(Session):
            def request(self, method, url, **kwargs):
                calls.append((method, str(url), kwargs))
                return Failure()

        bot.http._HTTPClient__session = FailedSession(calls)
        followup = discord.Webhook.partial(4242, "fictional", client=bot)
        followup.type = discord.WebhookType.application

        async def defer(**kwargs):
            assert kwargs == {"ephemeral": True, "thinking": True}

        interaction = SimpleNamespace(client=bot, user=SimpleNamespace(id=1001, bot=False),
                                      guild_id=7, channel_id=42, followup=followup,
                                      response=SimpleNamespace(defer=defer))
        monkeypatch.setattr(legacy, "_make_handler", lambda host: lambda *args: "合成個別回答")
        sleep = asyncio.sleep

        async def no_backoff(delay):
            await sleep(0)

        monkeypatch.setattr(discord.webhook.async_.asyncio, "sleep", no_backoff)
        command = legacy._mcs_command({"profile": "mcs"}, object())
        try:
            with pytest.raises((discord.HTTPException, legacy.cards.DiscordRetrySuppressed)) as unknown:
                await command.callback(interaction, '{}')
            assert len(calls) == 1
            assert all(method == "POST" for method, *_ in calls)
            assert isinstance(unknown.value, legacy.cards.DiscordRetrySuppressed)
        finally:
            await bot.close()
    asyncio.run(scenario())
