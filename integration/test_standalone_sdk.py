"""mcs_standalone against the pinned real SDKs (deployment/requirements-
standalone.txt), offline: only each SDK's aiohttp session is replaced by
a recording fake, so serialization, login, retries and error mapping are
the SDKs' own. Skips when the SDKs are not installed."""
import asyncio
import json
import re
from types import SimpleNamespace

import pytest

discord = pytest.importorskip("discord")
pytest.importorskip("slack_bolt")
from multidict import CIMultiDict  # noqa: E402  (aiohttp dependency)

from mcs_standalone import discord_runtime, slack_runtime  # noqa: E402
from mcs_standalone.host import NotSent  # noqa: E402

# split so the CI secret-pattern scan never sees a token shape
XOXB = "xox" + "b-synthetic"
APP, GUILD, CHANNEL = "2000000000000000002", "3000000000000000003", "1000000000000000001"
USER = {"id": APP, "username": "mcs", "discriminator": "0", "avatar": None, "bot": True}


class Response:
    def __init__(self, status, body):
        self.status, self.reason = status, "synthetic"
        self._body = json.dumps(body)
        self.headers = CIMultiDict({"Content-Type": "application/json"})
        self.content_type = "application/json"
        self.content = SimpleNamespace(iter_chunked=self.iter_chunked)

    async def iter_chunked(self, size):
        body = self._body.encode()
        for start in range(0, len(body), size):
            yield body[start:start + size]

    async def text(self, encoding=None):
        return self._body

    async def json(self, **_):
        return json.loads(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class Session:
    """Answers by (method, url regex); records every request."""

    trust_env = False

    def __init__(self, routes, log):
        self.routes, self.log, self.closed = routes, log, False

    def request(self, method, url, **kwargs):
        self.log.append((method, str(url), kwargs))
        for (verb, pattern), answer in self.routes.items():
            if verb == method and re.search(pattern, str(url)):
                if isinstance(answer, BaseException):
                    raise answer
                return Response(*answer)
        return Response(404, {"message": "unrouted", "code": 0})

    async def close(self):
        self.closed = True


def message(content):
    return {"id": "9000000000000000009", "channel_id": CHANNEL, "content": content,
            "author": USER, "attachments": [], "embeds": [], "mentions": [],
            "mention_roles": [], "pinned": False, "mention_everyone": False,
            "tts": False, "timestamp": "2026-10-01T00:00:00+00:00",
            "edited_timestamp": None, "type": 0, "flags": 0}


def discord_routes(post=(200, message("合成通知"))):
    return {
        ("GET", r"/users/@me$"): (200, USER),
        ("GET", r"/oauth2/applications/@me$"): (200, {
            "id": APP, "name": "mcs", "description": "", "icon": None,
            "bot_public": False, "bot_require_code_grant": False,
            "owner": USER, "verify_key": "synthetic"}),
        ("GET", rf"/channels/{CHANNEL}$"): (200, {
            "id": CHANNEL, "type": 0, "guild_id": GUILD, "name": "mcs",
            "position": 0, "permission_overwrites": []}),
        ("POST", rf"/channels/{CHANNEL}/messages$"): post,
        ("GET", rf"/applications/{APP}/commands$"): (200, []),
        ("POST", rf"/applications/{APP}/commands$"): (200, {
            "id": "1", "application_id": APP, "name": "mcs", "description": "x",
            "type": 1, "version": "1"}),
    }


@pytest.fixture
def discord_wire(monkeypatch):
    log, routes = [], {}
    monkeypatch.setattr(discord.http.aiohttp, "ClientSession",
                        lambda *a, **kw: Session(routes, log))
    return routes, log


def test_discord_send_posts_once_without_mentions(discord_wire):
    routes, log = discord_wire
    routes.update(discord_routes())
    message_id = asyncio.run(discord_runtime.send(
        "synthetic-token", CHANNEL, "合成通知 @everyone", [(b"synthetic", "a.txt")]))
    assert message_id == "9000000000000000009"
    posts = [entry for entry in log if entry[0] == "POST"]
    assert len(posts) == 1
    form = posts[0][2]["data"]          # multipart: payload_json + the file
    fields = {f[0]["name"]: f[2] for f in form._fields}
    payload = json.loads(fields["payload_json"])
    assert payload["content"] == "合成通知 @everyone"
    assert payload["allowed_mentions"] == {"parse": []}
    assert payload["attachments"][0]["filename"] == "a.txt"


def test_discord_send_outcomes(discord_wire):
    routes, _ = discord_wire
    routes.update(discord_routes(post=(403, {"message": "Missing Access", "code": 50001})))
    with pytest.raises(NotSent):        # definitive reject: nothing posted
        asyncio.run(discord_runtime.send("t", CHANNEL, "x", []))
    routes.update(discord_routes(post=(413, {"message": "Request entity too large", "code": 40005})))
    from mcs_standalone.host import Refused
    with pytest.raises(Refused):        # over the upload limit: retry text-only
        asyncio.run(discord_runtime.send("t", CHANNEL, "x", [(b"big", "a.bin")]))
    routes.update(discord_routes(post=ConnectionResetError()))
    with pytest.raises(Exception) as unknown:   # lost mid-post: never a NotSent
        asyncio.run(discord_runtime.send("t", CHANNEL, "x", []))
    assert not isinstance(unknown.value, NotSent)
    routes[("GET", r"/users/@me$")] = (401, {"message": "401: Unauthorized", "code": 0})
    with pytest.raises(NotSent):        # login failed before any post
        asyncio.run(discord_runtime.send("t", CHANNEL, "x", []))


def test_discord_run_registers_only_mcs_and_releases_the_worker(discord_wire, tmp_path, monkeypatch):
    routes, log = discord_wire
    routes.update(discord_routes())
    connected = asyncio.Event()

    async def connect(self, *, reconnect=True):
        connected.set()
        await asyncio.sleep(3600)
    monkeypatch.setattr(discord.Client, "connect", connect)
    settings = {"data_root": str(tmp_path / "data"), "profile": "default",
                "application_id": APP, "channel_id": CHANNEL, "guild_id": GUILD,
                "snapshot": str(tmp_path / "data/snapshots/ledger-snapshot.db"),
                "inbox": str(tmp_path / "data/cmd"),
                "allowed_user_ids": frozenset({"4"}), "allowed_chat_ids": frozenset({CHANNEL}),
                "allowed_role_ids": frozenset(), "project_ids": frozenset({101})}

    import notify_cards
    notify_cards.ensure_dirs(settings["data_root"])     # the runner provisions these

    async def scenario():
        stop = asyncio.Event()
        runner = asyncio.create_task(discord_runtime.run(settings, {"DISCORD_BOT_TOKEN": "synthetic"}, stop))
        await asyncio.wait_for(connected.wait(), 10)
        await asyncio.sleep(0.2)
        stop.set()
        await asyncio.wait_for(runner, 10)
    asyncio.run(scenario())
    upserts = [entry for entry in log if entry[0] == "POST" and "/commands" in entry[1]]
    assert len(upserts) == 1            # single upsert, never a bulk overwrite (PUT)
    command = json.loads(upserts[0][2]["data"])
    assert command["name"] == "mcs"
    assert [(o["name"], o["type"], o["required"]) for o in command["options"]] == [("args", 3, True)]
    assert not any(entry[0] == "PUT" for entry in log)
    # unchanged /mcs on the next start: no second create (daily quota)
    routes[("GET", rf"/applications/{APP}/commands$")] = (200, [{**command, "id": "1"}])
    connected = asyncio.Event()          # a fresh loop needs a fresh event
    asyncio.run(scenario())
    assert len([e for e in log if e[0] == "POST" and "/commands" in e[1]]) == 1
    # the worker released its scope lock on shutdown: a successor gets it
    from adapters.common import registry
    from adapters.discord.delivery import DeliveryWorker
    successor = DeliveryWorker(bot=None, settings=settings, root=settings["data_root"],
                               reg=registry.Registry(str(tmp_path / "data/discord_state"),
                                                     scope=settings),
                               worker_id="successor", log=lambda *a, **k: None)
    assert successor.acquire_scope_lock()
    successor.release_scope_lock()


@pytest.fixture
def slack_wire(monkeypatch):
    import slack_sdk.web.async_internal_utils as internal
    log, routes = [], {}
    monkeypatch.setattr(internal.aiohttp, "ClientSession", lambda *a, **kw: Session(routes, log))
    return routes, log


def test_slack_send_text_then_files_once(slack_wire):
    routes, log = slack_wire
    routes.update({
        ("POST", r"chat\.postMessage$"): (200, {"ok": True, "channel": "C0SYNTH", "ts": "1.000001"}),
        ("POST", r"files\.getUploadURLExternal$"): (200, {
            "ok": True, "upload_url": "https://files.slack.com/upload/v1/x", "file_id": "F1"}),
        ("POST", r"files\.slack\.com/upload"): (200, {"ok": True}),
        ("POST", r"files\.completeUploadExternal$"): (200, {"ok": True, "files": [{"id": "F1"}]}),
    })
    ts = asyncio.run(slack_runtime.send(XOXB, "C0SYNTH", "合成通知",
                                        [(b"synthetic", "a.txt")]))
    assert ts == "1.000001"
    calls = [url.rsplit("/", 1)[-1] for _, url, _ in log]
    assert calls[0] == "chat.postMessage" and calls.count("chat.postMessage") == 1
    assert "files.completeUploadExternal" in calls


def test_slack_send_reject_is_not_sent_and_network_loss_is_unknown(slack_wire):
    routes, _ = slack_wire
    routes[("POST", r"chat\.postMessage$")] = (200, {"ok": False, "error": "channel_not_found"})
    with pytest.raises(NotSent):
        asyncio.run(slack_runtime.send(XOXB, "C0SYNTH", "x", []))
    routes[("POST", r"chat\.postMessage$")] = ConnectionResetError()
    with pytest.raises(Exception) as unknown:
        asyncio.run(slack_runtime.send(XOXB, "C0SYNTH", "x", []))
    assert not isinstance(unknown.value, NotSent)


def test_slack_app_registers_the_card_handlers_without_oauth(slack_wire):
    from adapters.slack.actions import _ACTION, _CONFIRM
    app = slack_runtime._app(slack_runtime._client(XOXB))
    assert app.oauth_flow is None and app.client.retry_handlers == []
    assert _ACTION and _CONFIRM


def test_slack_run_binds_workspace_registers_handlers_and_stops(slack_wire, tmp_path, monkeypatch):
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    import notify_cards
    routes, log = slack_wire
    routes[("POST", r"auth\.test$")] = (200, {"ok": True, "team_id": "T0SYNTH",
                                             "bot_id": "B0SYNTH", "user_id": "U0BOT"})
    sockets = []

    async def connect(self):
        sockets.append(self)

    async def close(self):
        sockets.append("closed")
    monkeypatch.setattr(AsyncSocketModeHandler, "connect_async", connect)
    monkeypatch.setattr(AsyncSocketModeHandler, "close_async", close)
    settings = {"transport": "slack", "data_root": str(tmp_path / "data"), "profile": "default",
                "team_id": "T0SYNTH", "application_id": "A0SYNTH", "channel_id": "C0SYNTH",
                "snapshot": str(tmp_path / "data/snapshots/ledger-snapshot.db"),
                "allowed_user_ids": frozenset({"U0SYNTH"}), "project_ids": frozenset({101})}
    notify_cards.ensure_dirs(settings["data_root"])
    tokens = {"SLACK_BOT_TOKEN": XOXB, "SLACK_APP_TOKEN": "xapp-synthetic"}

    async def scenario():
        stop = asyncio.Event()
        runner = asyncio.create_task(slack_runtime.run(settings, tokens, stop))
        for _ in range(100):
            if sockets:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.3)
        stop.set()
        await asyncio.wait_for(runner, 10)
        return sockets[0].app
    app = asyncio.run(scenario())
    assert sockets[-1] == "closed"
    assert any("auth.test" in url for _, url, _ in log)
    assert len(app._async_listeners) >= 5        # actions, link, menu, confirm, modal

    with pytest.raises(RuntimeError, match="slack_workspace_mismatch"):
        asyncio.run(slack_runtime.run({**settings, "team_id": "T0OTHER"}, tokens, asyncio.Event()))


def test_discord_attachment_reject_and_oversize(discord_wire):
    from mcs_standalone.host import Refused
    routes, log = discord_wire
    routes.update(discord_routes(post=(403, {"message": "Missing Permissions", "code": 50013})))
    with pytest.raises(Refused):        # files refused -> notify_flush retries text-only
        asyncio.run(discord_runtime.send("t", CHANNEL, "x", [(b"a", "a.txt")]))
    routes.update(discord_routes())
    log.clear()
    big = b"x" * (discord_runtime._UPLOAD_FLOOR + 1)
    asyncio.run(discord_runtime.send("t", CHANNEL, "本文", [(b"ok", "a.txt"), (big, "big.pdf")]))
    fields = {f[0]["name"]: f[2] for f in [e for e in log if e[0] == "POST"][0][2]["data"]._fields}
    payload = json.loads(fields["payload_json"])
    assert [a["filename"] for a in payload["attachments"]] == ["a.txt"]
    assert "big.pdf" in payload["content"] and payload["content"].startswith("本文")


def test_discord_forum_target_creates_a_post(discord_wire):
    routes, log = discord_wire
    routes.update(discord_routes())
    routes[("GET", rf"/channels/{CHANNEL}$")] = (200, {
        "id": CHANNEL, "type": 15, "guild_id": GUILD, "name": "forum", "position": 0,
        "permission_overwrites": [], "available_tags": []})
    thread = {"id": "8000000000000000008", "type": 11, "guild_id": GUILD, "parent_id": CHANNEL,
              "name": "合成", "owner_id": APP, "message_count": 1, "member_count": 1,
              "rate_limit_per_user": 0, "last_message_id": None, "thread_metadata": {
                  "archived": False, "auto_archive_duration": 1440,
                  "archive_timestamp": "2026-10-01T00:00:00+00:00", "locked": False},
              "message": message("合成通知")}
    routes[("POST", rf"/channels/{CHANNEL}/threads$")] = (201, thread)
    assert asyncio.run(discord_runtime.send("t", CHANNEL, "合成通知", [])) == "9000000000000000009"
    assert [e[0] for e in log if "/threads" in e[1]] == ["POST"]


def test_slack_text_is_escaped_and_upload_failure_keeps_the_text(slack_wire):
    routes, log = slack_wire
    routes.update({
        ("POST", r"chat\.postMessage$"): (200, {"ok": True, "channel": "C0SYNTH", "ts": "1.000001"}),
        ("POST", r"files\.getUploadURLExternal$"): (200, {"ok": False, "error": "missing_scope"}),
    })
    ts = asyncio.run(slack_runtime.send(XOXB, "C0SYNTH", "**患者** <!channel> <@U1> BP<90 & >60",
                                        [(b"x", "a.txt")]))
    assert ts == "1.000001"             # delivered: never held or re-sent
    body = [kw for _, url, kw in log if url.endswith("chat.postMessage")][0]
    text = (body.get("data") or body.get("json") or {}).get("text") if isinstance(
        body.get("data") or body.get("json"), dict) else str(body)
    assert "*患者*" in text and "<!channel>" not in text and "<@U1>" not in text
    assert "&lt;!channel&gt;" in text and "&amp;" in text


def test_slack_app_acks_unhandled_events(slack_wire):
    app = slack_runtime._app(slack_runtime._client(XOXB))
    # exactly one listener before the Supervisor registers its own: the
    # catch-all event ack (registered without any network call)
    assert len(app._async_listeners) == 1


def test_discord_unsupported_channel_and_forum_files(discord_wire):
    from mcs_standalone.host import Refused
    routes, log = discord_wire
    routes.update(discord_routes())
    routes[("GET", rf"/channels/{CHANNEL}$")] = (200, {
        "id": CHANNEL, "type": 4, "guild_id": GUILD, "name": "category", "position": 0,
        "permission_overwrites": []})
    with pytest.raises(Refused):
        asyncio.run(discord_runtime.send("t", CHANNEL, "x", []))
    assert not [e for e in log if e[0] == "POST"]


def test_slack_catch_all_acks_an_unhandled_event(slack_wire):
    from slack_bolt.request.async_request import AsyncBoltRequest
    routes, _ = slack_wire
    routes[("POST", r"auth\.test$")] = (200, {"ok": True, "team_id": "T0SYNTH",
                                             "bot_id": "B0SYNTH", "user_id": "U0BOT"})
    app = slack_runtime._app(slack_runtime._client(XOXB))
    body = {"type": "event_callback", "team_id": "T0SYNTH", "api_app_id": "A0SYNTH",
            "event": {"type": "app_mention", "user": "U0SYNTH", "text": "hi",
                      "ts": "1.0", "channel": "C0SYNTH"}}
    response = asyncio.run(app.async_dispatch(AsyncBoltRequest(body=body, mode="socket_mode")))
    assert response.status == 200
