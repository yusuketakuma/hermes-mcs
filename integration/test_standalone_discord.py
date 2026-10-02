"""Run the independent connector with real discord.py and a completely local HTTP stub."""
import asyncio
import hashlib
import json
import logging

import pytest

discord = pytest.importorskip("discord")
aiohttp = pytest.importorskip("aiohttp")

from adapters.discord import standalone as runtime  # noqa: E402
from mcs_standalone.config import connector_settings  # noqa: E402
from mcs_standalone.host import Host  # noqa: E402


def configuration(root):
    (root / "config.json").write_text(json.dumps({"runtime_mode": "standalone", "notify": {
        "interactive": "discord", "discord": {"profile": "mcs", "guild_id": "7",
        "application_id": "1", "channel_id": "42", "allowed_user_ids": ["1001"],
        "project_ids": [1]}}}))
    (root / "data").mkdir(exist_ok=True)
    credential = root / "data/discord-credentials.json"
    credential.write_text(json.dumps({"bot_token": "private-synthetic-token"}))
    credential.chmod(0o600)


BOT = {"id": "1", "username": "MCS", "discriminator": "0000", "avatar": None, "bot": True}
HUMAN = {"id": "1001", "username": "田中さん", "discriminator": "0000", "avatar": None, "bot": False}


def message(ident, content):
    return {"id": str(ident), "channel_id": "42", "author": BOT, "content": content,
            "timestamp": "2026-10-01T00:00:00+00:00", "edited_timestamp": None,
            "tts": False, "mention_everyone": False, "mentions": [], "mention_roles": [],
            "attachments": [], "embeds": [], "pinned": False, "type": 0, "flags": 0}


class Response:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status
        self.reason = "synthetic"
        self.headers = {"content-type": "application/json"}

    async def text(self, **kwargs):
        return json.dumps(self.body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class Session:
    def __init__(self, calls, *, post_status=200, **kwargs):
        self.calls = calls
        self.post_status = post_status
        self.trust_env = False
        self.closed = False
        self.connector = kwargs.get("connector")

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/users/@me"):
            return Response(BOT)
        if url.endswith("/oauth2/applications/@me"):
            return Response({"id": "1", "name": "MCS", "description": "fictional", "icon": None,
                             "bot_public": False, "bot_require_code_grant": False,
                             "owner": HUMAN, "verify_key": "0" * 64})
        if url.endswith("/channels/42"):
            return Response({"id": "42", "type": 0, "name": "架空のチャンネル", "guild_id": "7",
                             "position": 0, "permission_overwrites": [], "parent_id": None})
        if url.endswith("/channels/42/messages"):
            if self.post_status != 200:
                return Response({"message": "synthetic failure", "code": 0}, self.post_status)
            payload = kwargs.get("data")
            content = json.loads(payload).get("content", "") if isinstance(payload, str) else ""
            return Response(message(100 + len(self.calls), content))
        raise AssertionError("unexpected SDK endpoint")

    async def close(self):
        self.closed = True
        if self.connector is not None:
            await self.connector.close()


@pytest.mark.parametrize("post_status,result", [(200, "delivered"), (403, "not_sent"), (500, "unknown")])
def test_real_sdk_text_attempt_has_fixed_boundary_and_no_unsafe_retry(tmp_path, monkeypatch, caplog,
                                                                    post_status, result):
    configuration(tmp_path)
    calls, sessions = [], []

    def factory(**kwargs):
        session = Session(calls, post_status=post_status, **kwargs)
        sessions.append(session)
        return session

    monkeypatch.setattr(aiohttp, "ClientSession", factory)
    failures = []
    original_error = runtime.worker.err_code
    monkeypatch.setattr(runtime.worker, "err_code", lambda exc: failures.append(exc) or original_error(exc))
    sleep = asyncio.sleep

    async def no_backoff(_delay):
        await sleep(0)
    monkeypatch.setattr(discord.http.asyncio, "sleep", no_backoff)
    caplog.set_level(logging.DEBUG)
    out = asyncio.run(runtime.send(tmp_path, "discord:42", {"text": "private-fictional-body"}))
    if out["result"] != result and failures:
        raise failures[0]
    assert out["result"] == result, out
    assert len([call for call in calls if call[0] == "POST"]) == 1
    assert calls[0][1].endswith("/users/@me")
    assert all(kwargs["allow_redirects"] is False and kwargs["proxy"] is None
               and kwargs["proxy_auth"] is None for _, _, kwargs in calls)
    assert sessions and all(session.closed for session in sessions)
    assert "private-fictional-body" not in caplog.text and "private-synthetic-token" not in caplog.text


def test_real_sdk_sealed_attachment_and_full_text_are_transmitted_once(tmp_path, monkeypatch):
    configuration(tmp_path)
    files = tmp_path / "data/attachments"
    files.mkdir()
    source = files / "説明.txt"
    source.write_bytes(b"fictional-attachment")
    pin = {"name": "説明.txt", "path": str(source), "bytes": source.stat().st_size,
           "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    calls = []
    monkeypatch.setattr(aiohttp, "ClientSession", lambda **kwargs: Session(calls, **kwargs))
    out = asyncio.run(runtime.send(tmp_path, "discord:42", {"text": "x" * 4000, "files": [pin]}))
    assert out["result"] == "delivered"
    posts = [kwargs for method, _, kwargs in calls if method == "POST"]
    text_chunks = [json.loads(p["data"])["content"] for p in posts if isinstance(p["data"], str)]
    assert "".join(text_chunks) == "x" * 4000
    file_posts = [p for p in posts if not isinstance(p["data"], str)]
    assert len(file_posts) == 1
    field_names = [field[0]["name"] for field in file_posts[0]["data"]._fields]
    assert "files[0]" in field_names


def test_real_sdk_native_buttons_dispatch_after_rebuild_and_slash_schema_preserves_text(tmp_path):
    configuration(tmp_path)

    async def exercise():
        settings = connector_settings(tmp_path, "discord")
        for _generation in range(2):
            bot = runtime._bot(settings, gateway=True)
            await bot._async_setup_hook()
            bot._connection.user = discord.ClientUser(state=bot._connection, data=BOT)
            host = Host(settings)
            scope = runtime._Scope(tmp_path, settings, asyncio.Event())
            commands = runtime._Commands(host, bot, settings, scope)
            command = discord.app_commands.Command(
                name="mcs", description="MCS", callback=commands.slash)
            bot.tree.add_command(command)
            schema = command.to_dict(bot.tree)
            assert schema["name"] == "mcs"
            assert schema["options"][0]["name"] == "text" and schema["options"][0]["type"] == 3
            assert bot.intents.message_content and bot.intents.messages and bot.intents.guilds
            assert not bot.intents.members and not bot.intents.presences
            received, event = [], asyncio.Event()

            async def listener(native):
                received.append(native)
                event.set()
            bot.add_listener(listener, "on_interaction")
            # No add_view() registration exists after rebuild. Discord's
            # native event still reaches the adapter's custom-ID listener.
            bot._connection.parse_interaction_create({
                "id": "10", "application_id": "1", "type": 3, "token": "fictional-interaction-token",
                "version": 1, "guild_id": "7", "channel_id": "42",
                "channel": {"id": "42", "type": 0, "guild_id": "7", "name": "care",
                            "position": 0, "permission_overwrites": []},
                "member": {"user": HUMAN, "roles": [], "joined_at": "2026-10-01T00:00:00+00:00",
                           "deaf": False, "mute": False, "permissions": "0", "flags": 0},
                "locale": "ja", "guild_locale": "ja", "entitlements": [],
                "attachment_size_limit": 10 * 1024 * 1024,
                "message": message(100, "card"),
                "data": {"component_type": 2, "custom_id": "mcs:a:" + "a" * 32}})
            await asyncio.wait_for(event.wait(), 1)
            assert received[0].data["custom_id"].startswith("mcs:a:")
            context = runtime._native_context(received[0], settings, slash=True)
            assert context["user_id"] == "1001" and context["chat_id"] == "42"
            await scope.close()
            await host.close()
            await bot.close()
    asyncio.run(exercise())
