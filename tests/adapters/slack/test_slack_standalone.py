"""Standalone Slack lifecycle and trust boundaries with synthetic clients only."""
import asyncio
import hashlib
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from adapters.slack import standalone
from slack_testkit import FakeClient, SCOPE


@pytest.mark.parametrize("url,websocket,allowed", [
    ("https://slack.com/api/chat.postMessage", False, True),
    ("https://files.slack.com/upload/v1/synthetic", False, True),
    ("wss://wss-primary.slack.com/?ticket=synthetic", True, True),
    ("wss://wss-backup.slack.com/?ticket=synthetic", True, True),
    ("https://slack.com/api/chat.postMessage", True, False),
    ("https://slack.com/api/unknown", False, False),
    ("https://files.slack.com/other", False, False),
    ("https://slack.com.evil.invalid/api/chat.postMessage", False, False),
    ("https://user:secret@slack.com/api/chat.postMessage", False, False),
    ("https://slack.com:444/api/chat.postMessage", False, False),
    ("https://slack.com/api/chat.postMessage#fragment", False, False),
    ("http://slack.com/api/chat.postMessage", False, False),
    ("wss://wss-primary.slack.com.evil.invalid", True, False),
    ("https://localhost/upload/v1/synthetic", False, False),
])
def test_destinations_are_fixed_official_urls(url, websocket, allowed):
    assert standalone._url_allowed(url, websocket=websocket) is allowed


@pytest.fixture
def config(monkeypatch, tmp_path):
    settings = {**SCOPE, "allowed_user_ids": frozenset({"U_SYNTHETIC"}),
                "project_ids": frozenset({1}), "data_root": str(tmp_path / "data")}
    credentials = {"bot_token": "xoxb-synthetic", "app_token": "xapp-synthetic"}
    module = ModuleType("mcs_standalone.config")
    module.connector_settings = lambda *_a, **_k: dict(settings)
    module.load_credentials = lambda *_a: dict(credentials)
    monkeypatch.setitem(sys.modules, "mcs_standalone.config", module)
    return settings, credentials


class Client(FakeClient):
    async def bots_info(self, **kwargs):
        self.calls.append(("bots_info", kwargs))
        return {"ok": True, "bot": {"id": self.bot_id,
                                    "app_id": SCOPE["application_id"],
                                    "deleted": False}}


class Session:
    closed = False

    def _verify(self):
        pass

    async def close(self):
        self.closed = True


def wire_client(monkeypatch, client):
    session = Session()
    client.session = session
    monkeypatch.setattr(standalone, "_client", lambda *_a: (client, session))
    return session


def test_text_and_pinned_attachments_share_the_delivered_thread(config, tmp_path,
                                                               monkeypatch):
    path = tmp_path / "data" / "attachments" / "sample.txt"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"fictional attachment")
    payload = {"text": "架空の通知", "files": [{
        "path": str(path), "name": "sample.txt", "bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]}
    client = Client()
    session = wire_client(monkeypatch, client)
    outcome = asyncio.run(standalone.send(tmp_path, "slack:C_SYNTHETIC", payload))
    assert outcome == {"result": "delivered", "message_id": "1790000000.000001"}
    assert session.closed
    post = next(kwargs for op, kwargs in client.calls if op == "create")
    assert not post["mrkdwn"] and not post["link_names"]
    assert client.upload_calls[0]["thread_ts"] == outcome["message_id"]
    assert client.upload_calls[0]["file"] == b"fictional attachment"


def test_bad_target_or_attachment_never_constructs_an_sdk(config, tmp_path, monkeypatch):
    monkeypatch.setattr(standalone, "_client", lambda *_a: pytest.fail("no SDK"))
    with pytest.raises(ValueError, match="destination_not_configured"):
        asyncio.run(standalone.send(tmp_path, "slack:C_FOREIGN", {"text": "sample"}))
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"sample")
    pin = {"path": str(outside), "name": "sample.txt", "bytes": 6,
           "sha256": hashlib.sha256(b"sample").hexdigest()}
    with pytest.raises(ValueError, match="attachment_invalid"):
        asyncio.run(standalone.send(tmp_path, "slack:C_SYNTHETIC",
                                   {"text": "sample", "files": [pin]}))


def test_ambiguous_post_and_upload_are_not_retried(config, tmp_path, monkeypatch):
    client = Client()
    client.failure = TimeoutError("fictional")
    session = wire_client(monkeypatch, client)
    outcome = asyncio.run(standalone.send(tmp_path, "slack:C_SYNTHETIC", {"text": "sample"}))
    assert outcome["result"] == "unknown" and session.closed
    assert sum(op == "create" for op, _ in client.calls) == 1

    path = tmp_path / "data" / "attachments" / "sample.txt"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"sample")
    client = Client()
    client.upload_fail_at.add(0)
    wire_client(monkeypatch, client)
    outcome = asyncio.run(standalone.send(tmp_path, "slack:C_SYNTHETIC", {
        "text": "sample", "files": [{"path": str(path), "name": "sample.txt",
                                     "bytes": 6, "sha256": hashlib.sha256(b"sample").hexdigest()}]}))
    assert outcome["result"] == "unknown" and outcome["message_id"]
    assert sum(op == "create" for op, _ in client.calls) == 1
    assert sum(op == "files_upload_v2" for op, _ in client.calls) == 1


@pytest.mark.parametrize("foreign", ["team", "app"])
def test_identity_mismatch_never_posts(config, tmp_path, monkeypatch, foreign):
    client = Client(team="T_FOREIGN" if foreign == "team" else SCOPE["team_id"])
    if foreign == "app":
        async def bots_info(**_kwargs):
            return {"ok": True, "bot": {"id": client.bot_id,
                                        "app_id": "A_FOREIGN", "deleted": False}}
        client.bots_info = bots_info
    session = wire_client(monkeypatch, client)
    outcome = asyncio.run(standalone.send(tmp_path, "slack:C_SYNTHETIC", {"text": "sample"}))
    assert outcome["result"] == "not_sent" and session.closed
    assert not any(op == "create" for op, _ in client.calls)


@pytest.mark.parametrize("change,close_failure", [(False, False), (True, False), (False, True)])
def test_runtime_registers_before_receive_and_closes_all_tasks(
        config, tmp_path, monkeypatch, change, close_failure):
    settings, _ = config
    for name in ("slack_render", "cmd_int", "cmd_results", "flags"):
        (tmp_path / "data" / name).mkdir(parents=True)
    (tmp_path / "data" / "flags" / "notify.json").write_text(json.dumps({
        "interactive": True, "transport": "slack"}))
    client = Client()
    session = wire_client(monkeypatch, client)
    tasks = []
    hooks = []
    closed = []

    class Host:
        def __init__(self, _settings):
            pass

        def spawn_task(self, coroutine, **kwargs):
            task = asyncio.create_task(coroutine, **kwargs)
            tasks.append(task)
            return task

        def on_unload(self, callback):
            hooks.append(callback)

        async def close(self):
            for callback in hooks:
                callback()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            closed.append("host")

    module = ModuleType("mcs_standalone.host")
    module.Host = Host
    monkeypatch.setitem(sys.modules, "mcs_standalone.host", module)

    async def scenario():
        stopping = asyncio.Event()
        actions = []
        inflight_cancelled = asyncio.Event()

        async def pending_listener():
            try:
                await asyncio.sleep(3600)
            finally:
                inflight_cancelled.set()

        async def socket(_client, _credentials, inflight):
            app = SimpleNamespace(client=client,
                                  action=lambda pattern: lambda handler: actions.append(handler),
                                  view=lambda name: lambda handler: None)
            task = asyncio.create_task(pending_listener())
            inflight.add(task)

            async def connect():
                assert len(actions) == 4
                assert tasks and not tasks[0].done()
                if change:
                    settings["allowed_user_ids"] = frozenset({"U_CHANGED"})
                else:
                    stopping.set()

            async def close():
                closed.append("socket")
                if close_failure:
                    raise RuntimeError("fictional close failure")

            return app, SimpleNamespace(connect_async=connect, close_async=close,
                                        client=SimpleNamespace(closed=False))

        monkeypatch.setattr(standalone, "_socket", socket)
        if change or close_failure:
            with pytest.raises((ValueError, RuntimeError)):
                await standalone.run(tmp_path, stopping)
        else:
            await standalone.run(tmp_path, stopping)
        assert inflight_cancelled.is_set()

    asyncio.run(scenario())
    assert closed == ["socket", "host"] and session.closed
    assert all(task.done() for task in tasks)
