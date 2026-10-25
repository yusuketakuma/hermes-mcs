"""Standalone Slack lifecycle and trust boundaries with synthetic clients only."""
import asyncio
import hashlib
import json
import os
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest

import mcs_standalone.config as cfgmod
from adapters.slack import standalone
from adapters.slack import standalone as st
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


def test_every_web_api_method_the_adapter_calls_is_allowed():
    # the thread-root prefetch prefers conversations_history; an unlisted
    # method turns every thread notice into thread_prefetch_failed
    import re
    from pathlib import Path
    root = Path(standalone.__file__).parent
    called = set()
    for name in ("delivery.py", "actions.py"):
        source = (root / name).read_text(encoding="utf-8")
        called |= set(re.findall(
            r"\b((?:chat|conversations|files|views|users|bots|auth)_[A-Za-z]+)\b", source))
    assert "conversations_history" in called
    for method in called:
        dotted = method.replace("_", ".", 1)
        assert standalone._url_allowed(f"https://slack.com/api/{dotted}"), method


@pytest.fixture
def config(monkeypatch, tmp_path):
    settings = {**SCOPE, "allowed_user_ids": frozenset({"U_SYNTHETIC"}),
                "project_ids": frozenset({1}), "data_root": str(tmp_path / "data")}
    credentials = {"bot_token": "xox" + "b-synthetic", "app_token": "xapp-synthetic"}
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


def test_system_notification_uses_its_explicit_channel(tmp_path, monkeypatch):
    cfg = {"runtime_mode": "standalone", "notify_target": "slack:C0PRIMARY9",
           "notify_system_target": "slack:C0SYSTEM99",
           "notify": {"interactive": "slack", "slack": {
               **SCOPE, "channel_id": "C0PRIMARY9", "allowed_user_ids": ["U_SYNTHETIC"],
               "project_ids": [1]}}}
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    data = tmp_path / "data"
    data.mkdir()
    credentials = data / "slack-credentials.json"
    credentials.write_text(json.dumps({"bot_token": "xox" + "b-synthetic",
                                       "app_token": "xapp-synthetic"}))
    credentials.chmod(0o600)
    client = Client()
    wire_client(monkeypatch, client)
    result = asyncio.run(standalone.send(tmp_path, cfg["notify_system_target"],
                                        {"text": "合成の運用通知"}))
    assert result["result"] == "delivered"
    post = next(kwargs for op, kwargs in client.calls if op == "create")
    assert post["channel"] == "C0SYSTEM99"


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
        def __init__(self, _settings, *, native_admission=None, delivery_admission=None, delivery_identity=None):
            self.native_admission = native_admission
            self.delivery_admission = delivery_admission
            self.delivery_identity = delivery_identity

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


@pytest.mark.parametrize("cancel_shutdown", [False, True])
def test_shutdown_retains_supervisor_lock_until_native_thread_finishes(
        config, tmp_path, monkeypatch, cancel_shutdown):
    from adapters.common.commands import run_native
    from adapters.slack import tasks as slack_tasks

    for name in ("slack_render", "cmd_int", "cmd_results", "flags"):
        (tmp_path / "data" / name).mkdir(parents=True)
    (tmp_path / "data" / "flags" / "notify.json").write_text(json.dumps({
        "interactive": True, "transport": "slack"}))
    client = Client()
    session = wire_client(monkeypatch, client)
    started, release, ended = (threading.Event() for _ in range(3))
    supervisors, listeners = [], []
    real_supervisor = standalone.Supervisor

    def supervisor(**kwargs):
        instance = real_supervisor(**kwargs)
        supervisors.append(instance)
        return instance

    def native_work():
        started.set()
        try:
            assert release.wait(5), "synthetic native work was not released"
        finally:
            ended.set()

    monkeypatch.setattr(standalone, "Supervisor", supervisor)
    monkeypatch.setattr(slack_tasks, "POLL_S", 0.01)

    async def scenario():
        stopping, socket_closed = asyncio.Event(), asyncio.Event()

        async def socket(_client, _credentials, inflight):
            app = SimpleNamespace(client=client, action=lambda _p: lambda fn: fn,
                                  view=lambda _n: lambda fn: fn)

            async def connect():
                assert supervisors[0]._actions._native_command_current()
                task = asyncio.create_task(run_native(native_work))
                listeners.append(task)
                inflight.add(task)
                task.add_done_callback(inflight.discard)
                deadline = asyncio.get_running_loop().time() + 5
                while not started.is_set():
                    assert asyncio.get_running_loop().time() < deadline
                    await asyncio.sleep(0.005)
                stopping.set()

            async def close():
                socket_closed.set()

            return app, SimpleNamespace(connect_async=connect, close_async=close,
                                        client=SimpleNamespace(closed=False))

        monkeypatch.setattr(standalone, "_socket", socket)
        running = asyncio.create_task(standalone.run(tmp_path, stopping))
        try:
            await asyncio.wait_for(socket_closed.wait(), timeout=5)
            # Give the real supervisor several ticks: early unload would now
            # finish its finally and release the delivery scope lock.
            await asyncio.sleep(0.05)
            owner, = supervisors
            assert not owner._actions._active and owner._worker._stopping
            assert owner._actions._native_current() is False
            assert not running.done() and not owner._task.done()
            assert owner._worker._lock_fd is not None
            assert not session.closed and not ended.is_set()
            if cancel_shutdown:
                for _ in range(3):
                    running.cancel()
                    await asyncio.sleep(0)
                    assert not running.done() and not owner._task.done()
                    assert not ended.is_set() and owner._worker._lock_fd is not None
            release.set()
            if cancel_shutdown:
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(running, timeout=5)
            else:
                await asyncio.wait_for(running, timeout=5)
            assert ended.is_set() and session.closed
            assert owner._task.done() and owner._worker._lock_fd is None
            assert all(task.done() and task.cancelled() for task in listeners)
        finally:
            release.set()
            stopping.set()
            await asyncio.gather(running, return_exceptions=True)

    asyncio.run(scenario())


# ---------- control-sequence escaping and config change before post --------

def test_send_escapes_slack_control_sequences(config, tmp_path, monkeypatch):
    client = Client()
    wire_client(monkeypatch, client)
    payload = {"text": "<!channel> a&b <@U1>"}
    outcome = asyncio.run(standalone.send(tmp_path, "slack:C_SYNTHETIC", payload))
    assert outcome["result"] == "delivered"
    post = next(kwargs for op, kwargs in client.calls if op == "create")
    assert post["text"] == "&lt;!channel&gt; a&amp;b &lt;@U1&gt;"


def test_config_change_before_post_is_not_sent(monkeypatch, tmp_path):
    calls = {"n": 0}

    def settings(root, t, require_interactive=False, target=None):
        calls["n"] += 1
        return {"channel_id": "C1", "team_id": "T1", "application_id": "A1",
                "v": calls["n"] == 1}

    monkeypatch.setattr(cfgmod, "connector_settings", settings)
    monkeypatch.setattr(cfgmod, "load_credentials", lambda r, t: {"bot_token": "x"})
    posted = []

    def fake_client(creds, verify):
        async def post(**kw):
            verify()  # Client._request -> session._verify() precedes HTTP
            posted.append(kw)

        async def close():
            pass
        return SimpleNamespace(chat_postMessage=post), SimpleNamespace(close=close)

    async def identity(client, s):
        return None

    monkeypatch.setattr(st, "_client", fake_client)
    monkeypatch.setattr(st, "_identity", identity)
    result = asyncio.run(st.send(str(tmp_path), "slack:C1", {"text": "hi"}))
    assert posted == []
    assert result == {"result": "not_sent",
                      "error_code": "slack_configuration_changed_restart_required"}


def test_attachments_follow_the_resolved_data_root_with_unchanged_codes(tmp_path):
    real = tmp_path / "real-data"
    (real / "attachments").mkdir(parents=True)
    os.symlink(real, tmp_path / "data")
    blob = b"fictional attachment"
    for path in (real / "attachments" / "a", real / "sibling", tmp_path / "other"):
        path.write_bytes(blob)

    def pin(path, **over):
        return {"path": str(path), "name": "a.txt", "bytes": len(blob),
                "sha256": hashlib.sha256(blob).hexdigest(), **over}
    for inside in (tmp_path / "data" / "attachments" / "a", real / "attachments" / "a"):
        assert st._files(tmp_path, {"text": "x", "files": [pin(inside)]}) == [("a.txt", blob)]
    for outside in (real / "sibling", tmp_path / "other", tmp_path / "data" / "sibling"):
        with pytest.raises(ValueError, match="^slack_attachment_invalid$"):   # never "mismatch"
            st._files(tmp_path, {"text": "x", "files": [pin(outside)]})
    with pytest.raises(ValueError, match="^slack_attachment_mismatch$"):
        st._files(tmp_path, {"text": "x", "files": [pin(real / "attachments" / "a", bytes=1)]})


def test_a_file_swapped_outside_after_the_check_is_never_read(tmp_path, monkeypatch):
    (tmp_path / "data" / "attachments").mkdir(parents=True)
    inside, outside = tmp_path / "data" / "attachments" / "a", tmp_path / "secret-outside"
    blob = b"fictional attachment"
    inside.write_bytes(blob)
    outside.write_bytes(blob)                               # same bytes, so only the root can refuse
    real = st.read_verified_attachment

    def swap_then_read(path, part, root=None):
        os.unlink(path)
        os.symlink(outside, path)                           # replaced between the check and the read
        return real(path, part, root=root)
    monkeypatch.setattr(st, "read_verified_attachment", swap_then_read)
    pin = {"path": str(inside), "name": "a.txt", "bytes": len(blob),
           "sha256": hashlib.sha256(blob).hexdigest()}
    with pytest.raises(ValueError, match="^slack_attachment_mismatch$"):
        st._files(tmp_path, {"text": "x", "files": [pin]})
