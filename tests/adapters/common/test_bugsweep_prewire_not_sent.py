"""A send the adapter refused itself before any HTTP request is not_sent, never unknown.

Synthetic clients only: no Slack, Discord, network or real data.
"""
import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

import notify_cards
from adapters.common import journal, worker
from adapters.discord import standalone as discord_standalone
from adapters.slack import delivery
from adapters.slack import standalone as slack_standalone
from discord_testkit import _fake_discord
from notify_testkit import NOW, _dispatch, _intent, _seed_thread
from slack_testkit import ROOT_TS, SLACK, FakeClient, _attach, _granted_card, _mkworld
from slack_testkit import isolated_slack_ledger as led

__all__ = ["led"]


class _WireClient(FakeClient):
    """Routes writes through api_call as the SDK does and records each HTTP request."""

    def __init__(self):
        super().__init__(uploads=False)
        self.http = []          # shared with single_attempt() copies
        self.uploading = []

    async def api_call(self, method, **kwargs):
        self.http.append(method)
        native = {"chat.postMessage": FakeClient.chat_postMessage,
                  "chat.update": FakeClient.chat_update}.get(method)
        return await native(self, **kwargs) if native else {"ok": True}

    async def chat_postMessage(self, **kwargs):
        return await self.api_call("chat.postMessage", **kwargs)

    async def chat_update(self, **kwargs):
        return await self.api_call("chat.update", **kwargs)

    async def files_upload_v2(self, **kwargs):
        self.uploading.append(True)
        await self.api_call("files.getUploadURLExternal")
        await self.api_call("files.completeUploadExternal")
        return await FakeClient._files_upload_v2(self, **kwargs)


def _world(store, tmp_path, monkeypatch, gate, *, attachment=False):
    monkeypatch.setattr(notify_cards.time, "time", lambda: NOW)
    _seed_thread(store, mids=(100,))
    if attachment:
        _attach(store, tmp_path, name="synthetic-0.pdf", blob=b"synthetic-0")
    assert _dispatch(store, _intent(store), SLACK)["dispatched"]
    world = _mkworld(store)
    world.wire = _WireClient()
    world.sender._client = world.wire
    world.worker._retry_gate = lambda _spec: gate(world.wire)
    return world


def _results(world, kind=None):
    return [row for rows in journal.scan(world.dirs["state"]).values() for row in rows
            if row.get("phase") == "result" and row.get("kind") == kind]


def test_slack_card_refused_by_authority_before_http_is_not_sent(led, tmp_path, monkeypatch):
    def gate(_wire):
        async def denied():
            return False
        return denied

    world = _world(led, tmp_path, monkeypatch, gate)
    asyncio.run(_granted_card(world.worker, led, world.root))
    assert world.wire.http == []
    rows = _results(world)
    assert rows and all(row["result"] == "not_sent"
                        and row["error_code"] == "denied_current_authority" for row in rows)


def test_slack_upload_refused_before_its_first_request_is_not_sent(led, tmp_path, monkeypatch):
    def gate(wire):
        async def allowed():
            return not wire.uploading
        return allowed

    world = _world(led, tmp_path, monkeypatch, gate, attachment=True)
    asyncio.run(_granted_card(world.worker, led, world.root))
    assert not any(method.startswith("files.") for method in world.wire.http)
    rows = _results(world, "attachment_part")
    assert [row["result"] for row in rows] == ["not_sent"]
    assert rows[0]["error_code"] == "denied_current_authority"


def test_slack_upload_refused_after_it_began_stays_unknown(led, tmp_path, monkeypatch):
    def gate(wire):
        async def allowed():
            return "files.getUploadURLExternal" not in wire.http
        return allowed

    world = _world(led, tmp_path, monkeypatch, gate, attachment=True)
    asyncio.run(_granted_card(world.worker, led, world.root))
    assert "files.getUploadURLExternal" in world.wire.http
    assert "files.completeUploadExternal" not in world.wire.http
    assert [row["result"] for row in _results(world, "attachment_part")] == ["unknown"]
    assert world.wire.upload_calls == []


def test_slack_token_pin_failure_keeps_delivered_card_and_message_id(led, tmp_path, monkeypatch):
    def gate(_wire):
        async def allowed():
            return True
        return allowed

    world = _world(led, tmp_path, monkeypatch, gate)
    events, failed = [], []
    world.worker._log = lambda event, **_fields: events.append(event)
    real = world.reg.put_tokens

    def put_tokens(tokens, *, durable=False):
        if durable and not failed:
            failed.append(True)
            raise OSError("synthetic disk fault")
        return real(tokens, durable=durable)

    world.reg.put_tokens = put_tokens
    asyncio.run(_granted_card(world.worker, led, world.root))
    assert failed and "token_pin_failed" in events
    rows = _results(world)
    assert rows and rows[0]["result"] == "delivered" and rows[0]["message_id"] == ROOT_TS


def test_slack_standalone_settings_change_is_a_pre_wire_refusal():
    def changed():
        raise ValueError("slack_configuration_changed_restart_required")

    with pytest.raises(worker.SendNotAdmitted) as caught:
        slack_standalone._GuardedSession(None, verify_current=changed)._verify()
    # The connector's reconnect classification still reads the original code.
    assert str(caught.value) == "slack_configuration_changed_restart_required"
    assert delivery._failed(caught.value, "create", None) == {
        "result": "not_sent", "error_code": "denied_current_authority"}

    def unreadable():
        raise OSError("/synthetic/private/path")

    with pytest.raises(worker.SendNotAdmitted) as caught:
        slack_standalone._GuardedSession(None, verify_current=unreadable)._verify()
    assert "synthetic" not in str(caught.value)


def _discord_bot(allowed, sent, *, tries=1):
    def request(method, url, *args, **kwargs):
        sent.append(method)
        allowed[0] = False          # the scope changes right after this try

    session = SimpleNamespace(trust_env=False, request=request)

    class HTTP:
        def __init__(self):
            self._HTTPClient__session = session

        async def request(self, route):
            for _ in range(tries):  # the SDK's in-call retry loop
                session.request("POST", route.url)
            return "posted"

    bot = SimpleNamespace(http=HTTP())
    setattr(bot, discord_standalone._CURRENT, lambda: allowed[0])
    discord_standalone._harden(bot)
    return bot


def test_discord_scope_refusal_before_the_first_request_is_not_sent():
    route = SimpleNamespace(url="https://discord.com/api/v10/channels/42/messages")
    sent = []
    bot = _discord_bot([False], sent)
    with pytest.raises(worker.SendNotAdmitted) as caught:
        asyncio.run(bot.http.request(route))
    assert sent == [] and worker.is_definitive_reject(caught.value)
    # A refusal after a try that may have committed stays ambiguous.
    sent = []
    bot = _discord_bot([True], sent, tries=2)
    with pytest.raises(ValueError, match="discord_scope_changed") as caught:
        asyncio.run(bot.http.request(route))
    assert sent == ["POST"] and not worker.is_definitive_reject(caught.value)


def test_discord_standalone_send_refused_before_http_is_not_sent(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "discord", _fake_discord())
    (tmp_path / "config.json").write_text(json.dumps({
        "runtime_mode": "standalone", "notify_target": "discord:42",
        "notify": {"interactive": "discord", "route_epoch": 1,
                   "discord": {"profile": "mcs", "application_id": "1", "guild_id": "7",
                               "channel_id": "42", "allowed_user_ids": ["1001"],
                               "project_ids": [1]}}}))
    (tmp_path / "data").mkdir()
    credential = tmp_path / "data/discord-credentials.json"
    credential.write_text(json.dumps({"bot_token": "synthetic-discord-token"}))
    credential.chmod(0o600)

    class Channel:
        async def send(self, *args, **kwargs):
            raise worker.SendNotAdmitted(code="discord_scope_changed")

    class Bot:
        async def login(self, token):
            pass

        async def fetch_channel(self, channel_id):
            return Channel()

        async def close(self):
            pass

    async def single_post(bot, factory, **kwargs):
        return await factory()

    monkeypatch.setattr(discord_standalone, "_bot", lambda *a, **kw: Bot())
    monkeypatch.setattr(discord_standalone, "_verify_identity", lambda *a, **kw: None)
    monkeypatch.setattr(discord_standalone, "_verify_channel", lambda *a, **kw: None)
    monkeypatch.setattr(discord_standalone.delivery, "single_post", single_post)
    result = asyncio.run(discord_standalone.send(tmp_path, "discord:42", {"text": "架空の通知"}))
    assert result == {"result": "not_sent", "error_code": "denied_current_authority"}
