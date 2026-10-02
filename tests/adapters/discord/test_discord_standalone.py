"""Exercise independent Discord admission, sealed sends and complete shutdown offline."""
import asyncio
import hashlib
import json
import sys
from types import SimpleNamespace

import pytest

from adapters.discord import standalone as runtime
from discord_delivery_testkit import FakeHTTP, FakeHTTPClient
from discord_testkit import _fake_discord
from mcs_standalone.config import connector_settings
from mcs_standalone.host import Host


def root_config(root):
    cfg = {"runtime_mode": "standalone", "notify_target": "discord:42",
           "notify": {"interactive": "discord", "route_epoch": 1,
                      "discord": {"profile": "mcs", "application_id": "1", "guild_id": "7",
                                  "channel_id": "42", "allowed_user_ids": ["1001"],
                                  "project_ids": [1]}}}
    (root / "config.json").write_text(json.dumps(cfg))
    (root / "data").mkdir(exist_ok=True)
    credential = root / "data/discord-credentials.json"
    credential.write_text(json.dumps({"bot_token": "synthetic-discord-token"}))
    credential.chmod(0o600)
    return cfg


class Response:
    def __init__(self):
        self.done = False
        self.responses = []

    async def defer(self, **kwargs):
        self.done = True
        self.responses.append(kwargs)

    async def send_message(self, content, **kwargs):
        self.done = True
        self.responses.append({"content": content, **kwargs})

    def is_done(self):
        return self.done


class Followup:
    def __init__(self):
        self.sent = []

    async def send(self, content, **kwargs):
        self.sent.append({"content": content, **kwargs})


def native(**overrides):
    return SimpleNamespace(**{
        "application_id": 1, "guild_id": 7, "channel_id": 42, "id": 10,
        "channel": SimpleNamespace(id=42, parent_id=None),
        "user": SimpleNamespace(id=1001, bot=False), "response": Response(),
        "followup": Followup(), **overrides})


class Channel:
    def __init__(self, outcomes=()):
        self.id = 42
        self.guild = SimpleNamespace(id=7)
        self.sent = []
        self.outcomes = iter(outcomes)

    async def send(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})
        result = next(self.outcomes, None)
        if isinstance(result, Exception):
            raise result
        return result or SimpleNamespace(id=100 + len(self.sent))


class Bot:
    def __init__(self, channel=None):
        self.user = SimpleNamespace(id=1)
        self.http = FakeHTTPClient()
        self.channel = channel or Channel()
        self.closed = False
        self.login_calls = 0
        self.connect_calls = 0
        self.listeners = {}
        self.commands = []
        self.tree = SimpleNamespace(add_command=lambda command: self.commands.append(command))

    async def login(self, _token):
        self.login_calls += 1

    async def fetch_channel(self, channel_id):
        assert channel_id == 42
        return self.channel

    def add_listener(self, listener, name):
        self.listeners[name] = listener

    async def connect(self, **kwargs):
        assert kwargs == {"reconnect": True}
        self.connect_calls += 1
        await asyncio.Event().wait()

    async def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def stub_discord(monkeypatch):
    sdk = _fake_discord()
    sdk.File = lambda source, filename: SimpleNamespace(fp=source, filename=filename)
    monkeypatch.setitem(sys.modules, "discord", sdk)


@pytest.mark.parametrize("url,allowed", [
    ("https://discord.com/api/v10/users/@me", True),
    ("wss://gateway.discord.gg/?v=10", True),
    ("wss://gateway-us-east1-a.discord.gg/?v=10", True),
    ("https://discord.com/api/v10/webhooks/1/private-token", True),
    ("https://discord.com.evil.invalid/api/", False),
    ("http://discord.com/api/", False),
    ("https://user:password@discord.com/api/", False),
    ("https://discord.com:8443/api/", False),
    ("https://discord.com/login", False),
    ("wss://evil.invalid/", False),
    ("wss://gateway.evil.discord.gg/", False),
])
def test_official_endpoint_boundary(url, allowed):
    assert runtime._official_url(url) is allowed


def test_session_boundary_prevents_redirect_proxy_and_keeps_single_post_guard():
    calls = []
    session = SimpleNamespace(trust_env=False, request=lambda *a, **kw: calls.append((a, kw)))
    runtime._session_boundary(session)
    session.request("GET", "https://discord.com/api/v10/users/@me",
                    allow_redirects=True, proxy="http://foreign.invalid", proxy_auth="secret")
    assert calls[0][1] == {"allow_redirects": False, "proxy": None, "proxy_auth": None}
    original = session.request
    session.request = runtime.cards._guarded(session.request)
    guard = session.request
    runtime._session_boundary(session)
    assert session.request is guard and session.request is not original
    with pytest.raises(ValueError, match="discord_endpoint_forbidden"):
        session.request("POST", "https://foreign.invalid/api/")
    with pytest.raises(ValueError, match="discord_proxy_forbidden"):
        runtime._session_boundary(SimpleNamespace(trust_env=True))


def test_session_boundary_stops_existing_post_after_scope_revocation(tmp_path):
    cfg = root_config(tmp_path)

    async def exercise():
        settings = connector_settings(tmp_path, "discord")
        scope = runtime._Scope(tmp_path, settings, asyncio.Event())
        sent = []
        session = SimpleNamespace(trust_env=False, request=lambda *a, **kw: sent.append((a, kw)))
        runtime._session_boundary(session, scope.current)
        session.request("POST", "https://discord.com/api/v10/channels/42/messages")
        assert len(sent) == 1
        cfg["notify"]["discord"]["allowed_user_ids"] = ["9999"]
        (tmp_path / "config.json").write_text(json.dumps(cfg))
        # Followups and the worker already hold this session. Check every
        # call, rather than relying only on the background monitor interval.
        with pytest.raises(ValueError, match="discord_scope_changed"):
            session.request("POST", "https://discord.com/api/v10/webhooks/1/token")
        assert len(sent) == 1
        await asyncio.wait_for(scope.stopping.wait(), 1)
    asyncio.run(exercise())


@pytest.mark.parametrize("change", ["user", "bot", "application", "channel", "guild"])
def test_native_context_is_scope_bound(tmp_path, change):
    root_config(tmp_path)
    settings = connector_settings(tmp_path, "discord")
    ix = native()
    assert runtime._native_context(ix, settings, slash=True)["user_id"] == "1001"
    if change == "user":
        ix.user.id = 9999
    elif change == "bot":
        ix.user.bot = True
    elif change == "application":
        ix.application_id = 2
    elif change == "channel":
        ix.channel_id = 43
    else:
        ix.guild_id = 8
    assert runtime._native_context(ix, settings, slash=True) is None


def test_gateway_rejects_an_http_interaction_application(tmp_path):
    root_config(tmp_path)
    settings = connector_settings(tmp_path, "discord")
    bot = Bot()
    bot.application = SimpleNamespace(id=1, interactions_endpoint_url="https://foreign.invalid/")
    with pytest.raises(ValueError, match="discord_http_interactions_configured"):
        runtime._verify_identity(bot, settings, gateway=True)
    bot.application.id = 2
    with pytest.raises(ValueError, match="discord_bot_identity_mismatch"):
        runtime._verify_identity(bot, settings)


@pytest.mark.parametrize("change", ["guild", "channel"])
def test_channel_metadata_must_match_configured_scope(tmp_path, change):
    root_config(tmp_path)
    settings = connector_settings(tmp_path, "discord")
    channel = Channel()
    if change == "guild":
        channel.guild.id = 8
    else:
        channel.id = 43
    with pytest.raises(ValueError, match="discord_channel_scope_mismatch"):
        runtime._verify_channel(channel, settings)


def test_slash_preview_confirmation_and_receipt_use_existing_core(tmp_path):
    import job_ops
    from ledger import Ledger, publish_snapshot
    from mcs_adapter import Message

    root_config(tmp_path)
    data = tmp_path / "data"
    (data / "cmd").mkdir()
    ledger = Ledger(str(data / "source.db"))
    ledger.ensure_patient(1)
    ledger.save_messages([Message(1, 1, None, 1, "田中さん", "user", "", "",
                                 "2026-10-01T00:00:00+09:00", "<p>架空の依頼です。</p>",
                                 "full", False, 0)])
    publish_snapshot(str(data / "source.db"), str(data / "snapshots"))

    async def exercise():
        settings = connector_settings(tmp_path, "discord")
        host = Host(settings)
        scope = runtime._Scope(tmp_path, settings, asyncio.Event())
        commands = runtime._Commands(host, Bot(), settings, scope)

        async def invoke(payload, ix=None):
            ix = ix or native()
            await commands.slash(ix, json.dumps(payload))
            assert all(item["ephemeral"] for item in ix.followup.sent)
            assert ix.response.responses[0] == {"ephemeral": True, "thinking": True}
            return json.loads("\n".join(item["content"] for item in ix.followup.sent))

        preview = await invoke({"op": "request", "phase": "preview", "action": "create",
                                "project_id": 1, "source_message_id": 1,
                                "title": "架空の依頼", "reason": "原文を本人が確認した"})
        assert preview["ok"] and not list((data / "cmd").iterdir()), preview
        confirm = {key: preview[key] for key in ("payload", "payload_hash", "origin")}
        confirm.update(op="request", phase="confirm")
        assert not (await invoke(confirm, native(user=SimpleNamespace(id=9999, bot=False))))["ok"]
        assert not list((data / "cmd").iterdir())
        result = await invoke(confirm)
        assert result["ok"] and result["phase"] == "confirm"
        assert ledger.db.execute("SELECT count(*) FROM requests").fetchone()[0] == 0
        job_ops.drain_commands(ledger, {"errors": []}, str(data / "cmd"))
        receipt = json.loads(ledger.db.execute("SELECT receipt_json FROM command_receipts").fetchone()[0])
        assert receipt["actor"] == "discord:1001" and receipt["reason"] == "原文を本人が確認した"
        assert (await invoke(confirm))["ok"]
        job_ops.drain_commands(ledger, {"errors": []}, str(data / "cmd"))
        assert ledger.db.execute("SELECT count(*) FROM requests").fetchone()[0] == 1
        await scope.close()
        await host.close()

    try:
        asyncio.run(exercise())
    finally:
        ledger.close()


def test_slash_outputs_are_private_and_never_truncated(tmp_path):
    root_config(tmp_path)

    async def exercise():
        settings = connector_settings(tmp_path, "discord")
        scope = runtime._Scope(tmp_path, settings, asyncio.Event())
        commands = runtime._Commands(Host(settings), Bot(), settings, scope)
        answer = "a" * 20000
        commands.handler = lambda *_: answer
        ix = native()
        await commands.slash(ix, "{}")
        assert "".join(item["content"] for item in ix.followup.sent) == answer
        assert len(ix.followup.sent) > 4
        assert all(item["ephemeral"] and len(item["content"]) <= 2000 for item in ix.followup.sent)
    asyncio.run(exercise())


def test_native_channel_input_replies_only_to_human_dm(tmp_path):
    root_config(tmp_path)

    async def exercise():
        settings = connector_settings(tmp_path, "discord")
        scope = runtime._Scope(tmp_path, settings, asyncio.Event())
        bot = Bot()
        commands = runtime._Commands(Host(settings), bot, settings, scope)
        dm = Channel()

        async def create_dm():
            return dm

        calls = []
        answer = "a" * 10000
        commands.handler = lambda raw, context: calls.append((raw, context)) or answer
        author = SimpleNamespace(id=1001, bot=False, create_dm=create_dm)
        message = SimpleNamespace(author=author, id=101, guild=SimpleNamespace(id=7),
                                  channel=bot.channel, content='/mcs {"op":"status"}',
                                  webhook_id=None, message_snapshots=[])
        await commands.message(message)
        assert calls[0][0] == '{"op":"status"}'
        assert calls[0][1]["native_input"] and calls[0][1]["message_id"] == "101"
        assert "".join(item["content"] for item in dm.sent) == answer
        assert not bot.channel.sent
        sent_count = len(dm.sent)
        for attr, unsafe in (("webhook_id", 2), ("message_snapshots", [object()])):
            original = getattr(message, attr)
            setattr(message, attr, unsafe)
            await commands.message(message)
            setattr(message, attr, original)
        author.bot = True
        await commands.message(message)
        assert len(calls) == 1 and len(dm.sent) == sent_count and not bot.channel.sent
    asyncio.run(exercise())


def test_scope_change_stops_admission_and_cancels_in_flight_listener(tmp_path):
    cfg = root_config(tmp_path)

    async def exercise():
        settings = connector_settings(tmp_path, "discord")
        stopping = asyncio.Event()
        scope = runtime._Scope(tmp_path, settings, stopping)
        admitted = asyncio.Event()
        cancelled = asyncio.Event()

        async def listener():
            admitted.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        task = asyncio.create_task(scope.invoke(listener))
        await admitted.wait()
        cfg["notify"]["discord"]["allowed_user_ids"] = ["9999"]
        (tmp_path / "config.json").write_text(json.dumps(cfg))
        # This path also runs inside _make_handler's thread.
        assert not await asyncio.to_thread(scope.current)
        await asyncio.wait_for(stopping.wait(), 1)
        assert not await scope.invoke(listener)
        await scope.close()
        assert cancelled.is_set() and task.cancelled() and not scope.active
    asyncio.run(exercise())


def test_replayed_native_input_is_not_admitted_before_gateway_resumed(tmp_path):
    root_config(tmp_path)

    async def exercise():
        settings = connector_settings(tmp_path, "discord")
        scope = runtime._Scope(tmp_path, settings, asyncio.Event())
        calls = []

        async def callback():
            calls.append("native input")

        scope.admit_native = False
        assert not await scope.invoke(callback) and not calls
        scope.admit_native = True
        assert await scope.invoke(callback) and calls == ["native input"]
    asyncio.run(exercise())


@pytest.mark.parametrize("outcome,expected", [(None, "delivered"), (FakeHTTP(403), "not_sent"),
                                            (TimeoutError("secret"), "unknown")])
def test_send_classifies_one_attempt_without_retry(tmp_path, monkeypatch, outcome, expected):
    root_config(tmp_path)
    bot = Bot(Channel([outcome]))
    monkeypatch.setattr(runtime, "_bot", lambda *a, **kw: bot)
    result = asyncio.run(runtime.send(tmp_path, "discord:42", {"text": "架空の通知"}))
    assert result["result"] == expected
    assert bot.closed and bot.login_calls == 1 and len(bot.channel.sent) == 1
    assert "secret" not in json.dumps(result)


def test_system_notification_uses_its_configured_channel_in_the_same_guild(tmp_path, monkeypatch):
    cfg = root_config(tmp_path)
    cfg["notify_system_target"] = "discord:43"
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    bot = Bot()
    bot.channel.id = 43
    async def fetch(channel_id):
        assert channel_id == 43
        return bot.channel
    bot.fetch_channel = fetch
    monkeypatch.setattr(runtime, "_bot", lambda *args, **kwargs: bot)
    result = asyncio.run(runtime.send(tmp_path, "discord:43", {"text": "合成の運用通知"}))
    assert result["result"] == "delivered" and len(bot.channel.sent) == 1
    bot.channel.guild.id = 8
    result = asyncio.run(runtime.send(tmp_path, "discord:43", {"text": "合成の運用通知"}))
    assert result["result"] == "not_sent" and len(bot.channel.sent) == 1


def test_send_validates_all_files_before_network_and_preserves_full_text(tmp_path, monkeypatch):
    root_config(tmp_path)
    attachments = tmp_path / "data/attachments"
    attachments.mkdir()
    source = attachments / "file.bin"
    source.write_bytes(b"fictional")
    pin = {"path": str(source), "bytes": 9,
           "sha256": hashlib.sha256(b"fictional").hexdigest(), "name": "説明.txt"}
    bot = Bot()
    monkeypatch.setattr(runtime, "_bot", lambda *a, **kw: bot)
    with pytest.raises(ValueError, match="discord_payload_invalid"):
        asyncio.run(runtime.send(tmp_path, "discord:42", {"text": "x", "files": [{**pin, "sha256": "0" * 64}]}))
    assert bot.login_calls == 0 and not bot.channel.sent
    payload = "x" * 12000
    result = asyncio.run(runtime.send(tmp_path, "discord:42", {"text": payload, "files": [pin]}))
    assert result["result"] == "delivered"
    assert "".join(item["content"] or "" for item in bot.channel.sent) == payload
    assert all(len(item["content"] or "") <= 2000 for item in bot.channel.sent)
    with pytest.raises(ValueError, match="discord_destination_not_configured"):
        asyncio.run(runtime.send(tmp_path, "discord:999", {"text": "x"}))


def test_partial_send_failure_is_unknown_and_never_restarts_at_first_chunk(tmp_path, monkeypatch):
    root_config(tmp_path)
    bot = Bot(Channel([None, FakeHTTP(403)]))
    monkeypatch.setattr(runtime, "_bot", lambda *a, **kw: bot)
    result = asyncio.run(runtime.send(tmp_path, "discord:42", {"text": "x" * 4000}))
    assert result["result"] == "unknown" and len(bot.channel.sent) == 2 and bot.closed


def test_run_owns_one_supervisor_and_recovers_every_task_on_stop(tmp_path, monkeypatch):
    root_config(tmp_path)
    bot = Bot()
    registered = []

    async def upsert(application, payload):
        registered.append((application, payload))
    bot.http.upsert_global_command = upsert

    class Command:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def to_dict(self, tree):
            return {"name": self.kwargs["name"]}

    supervisors = []
    class Supervisor:
        def __init__(self, **kwargs):
            self.host = kwargs["ctx"]
            self._actions = SimpleNamespace(on_interaction=self.action)
            supervisors.append(self)

        async def action(self, native):
            pass

        def start(self):
            self._task = self.host.spawn_task(asyncio.Event().wait())
            return True

    monkeypatch.setattr(runtime, "Supervisor", Supervisor)
    monkeypatch.setattr(runtime, "_bot", lambda *a, **kw: bot)
    monkeypatch.setattr(runtime, "_sdk", lambda: (SimpleNamespace(app_commands=SimpleNamespace(Command=Command)), None))

    async def exercise():
        stopping = asyncio.Event()
        task = asyncio.create_task(runtime.run(tmp_path, stopping))
        while bot.connect_calls == 0:
            await asyncio.sleep(0)
        stopping.set()
        await asyncio.wait_for(task, 1)
        assert len(supervisors) == 1 and bot.connect_calls == 1
        assert not supervisors[0].host.tasks and supervisors[0]._task.cancelled()
        assert bot.closed and registered == [(1, {"name": "mcs"})]
    asyncio.run(exercise())


def test_run_already_stopped_does_not_load_credentials_or_connect(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Already stopped runner must not construct a client")

    monkeypatch.setattr(runtime, "_bot", forbidden)

    async def exercise():
        stopping = asyncio.Event()
        stopping.set()
        # There is deliberately no configuration or credential file.
        await runtime.run(tmp_path, stopping)
    asyncio.run(exercise())
