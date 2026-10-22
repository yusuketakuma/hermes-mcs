"""Native command shutdown retains ownership until synchronous work ends.

Run only via the repository's isolated runner. The assertion requires that a
native confirmation cannot publish after scope + Host cleanup has completed.
No SDK, credential, production DB or network is needed. Real preview/confirm,
source checks, queue lock, atomic publication and request apply remain active.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager, nullcontext
import json
from pathlib import Path
import socket
import threading
from types import SimpleNamespace

import pytest


REPO = Path(__file__).resolve().parents[3]


async def _wait_event(event, timeout=5):
    deadline = asyncio.get_running_loop().time() + timeout
    while not event.is_set():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("synthetic synchronization event did not arrive")
        await asyncio.sleep(0.005)


@pytest.mark.parametrize("window", ["queue", "pre_link", "post_link"])
def test_native_confirm_never_publishes_after_scope_and_host_close(tmp_path, monkeypatch, window):
    def offline(*_args, **_kwargs):
        raise AssertionError("external network is forbidden in this synthetic probe")

    monkeypatch.setattr(socket.socket, "connect", offline)
    monkeypatch.setattr(socket.socket, "connect_ex", offline)
    monkeypatch.setattr(socket, "create_connection", offline)
    monkeypatch.setenv("MCS_ROOT", str(tmp_path))
    monkeypatch.syspath_prepend(str(REPO))
    monkeypatch.syspath_prepend(str(REPO / "mcs"))
    import _mcs_path  # noqa: F401
    import mcs_requests
    from ledger import Ledger, publish_snapshot
    from mcs_contract import Message
    from adapters.discord import standalone as runtime
    from mcs_standalone.config import connector_settings
    from mcs_standalone.host import Host

    assert Path(runtime.__file__).resolve() == REPO / "adapters/discord/standalone.py"
    # No SDK is used; only the native reply envelope is stubbed.
    monkeypatch.setattr(runtime.cards, "no_pings", lambda: None)
    cfg = {"runtime_mode": "standalone", "notify_target": "discord:42",
           "notify": {"interactive": "discord", "route_epoch": 1,
                      "discord": {"profile": "synthetic", "application_id": "1",
                                  "guild_id": "7", "channel_id": "42",
                                  "allowed_user_ids": ["1001"], "project_ids": [1]}}}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(cfg))
    data = tmp_path / "data"
    inbox = data / "cmd"
    inbox.mkdir(parents=True)
    ledger = Ledger(str(data / "source.db"))
    ledger.ensure_patient(1)
    ledger.save_messages([Message(
        1, 1, None, 1, "架空職員", "user", "", "", "2026-10-01T00:00:00+09:00",
        "<p>完全合成の依頼です。</p>", "full", False, 0)])
    publish_snapshot(str(data / "source.db"), str(data / "snapshots"))

    async def exercise():
        settings = connector_settings(tmp_path, "discord")
        stopping = asyncio.Event()
        host = Host(settings)
        scope = runtime._Scope(tmp_path, settings, stopping)
        commands = runtime._Commands(host, SimpleNamespace(), settings, scope)
        host_closed, at_queue, thread_done, close_returned, release = (
            threading.Event() for _ in range(5))
        host.on_unload(host_closed.set)
        publications, handler_results = [], []
        listener = closer = other_closer = None

        class Response:
            async def defer(self, **_kwargs):
                pass

            async def send_message(self, *_args, **_kwargs):
                pass

        class Followup:
            async def send(self, *_args, **_kwargs):
                pass

        interaction = SimpleNamespace(
            application_id=1, guild_id=7, channel_id=42, id=10,
            channel=SimpleNamespace(id=42, parent_id=None),
            user=SimpleNamespace(id=1001, bot=False),
            response=Response(), followup=Followup())
        identity = runtime._native_context(interaction, settings, slash=True)
        assert identity is not None and scope.current()
        handler = commands.handler
        preview = json.loads(handler(json.dumps({
            "op": "request", "phase": "preview", "action": "create",
            "project_id": 1, "source_message_id": 1, "title": "完全合成の依頼",
            "reason": "完全合成の人承認"}), identity))
        assert preview["ok"] and not list(inbox.iterdir()), preview
        confirmation = {key: preview[key] for key in ("payload", "payload_hash", "origin")}
        confirmation.update(op="request", phase="confirm")
        raw = json.dumps(confirmation)
        real_queue_lock, real_link = mcs_requests.queue_lock, mcs_requests.os.link
        real_fsync = mcs_requests.os.fsync

        @contextmanager
        def observed_queue_lock(cmd_dir, *, wait=True):
            # Signal only after real confirm + enqueue validation reached the lock.
            if window == "queue":
                at_queue.set()
            with real_queue_lock(cmd_dir, wait=wait) as acquired:
                yield acquired

        def observed_link(src, dst, *args, **kwargs):
            after_close, after_return = host_closed.is_set(), close_returned.is_set()
            result = real_link(src, dst, *args, **kwargs)
            target = Path(dst)
            if target.parent == inbox and target.suffix == ".json":
                publications.append({"after_host_close": after_close,
                                     "after_close_return": after_return, "path": target})
            return result

        def pause_before_link(fd):
            real_fsync(fd)
            if not at_queue.is_set():
                at_queue.set()
                assert release.wait(5), "synthetic pre-link write was not released"

        def pause_after_link(_path):
            at_queue.set()
            assert release.wait(5), "synthetic post-link write was not released"
            raise OSError("synthetic directory fsync failure")

        def observed_handler(*args):
            try:
                result = handler(*args)
                handler_results.append(json.loads(result))
                return result
            finally:
                thread_done.set()

        monkeypatch.setattr(mcs_requests, "queue_lock", observed_queue_lock)
        monkeypatch.setattr(mcs_requests.os, "link", observed_link)
        if window == "pre_link":
            monkeypatch.setattr(mcs_requests.os, "fsync", pause_before_link)
        elif window == "post_link":
            monkeypatch.setattr(mcs_requests, "fsync_dir", pause_after_link)
        commands.handler = observed_handler

        async def close_owned_scope():
            try:
                await scope.close()
            finally:
                await host.close()
                close_returned.set()

        try:
            # A real contended queue flock creates the synchronous waiting window.
            with real_queue_lock(str(inbox)) if window == "queue" else nullcontext():
                listener = asyncio.create_task(commands.slash(interaction, raw))
                await _wait_event(at_queue)
                cfg["notify"]["discord"]["allowed_user_ids"] = ["9999"]
                config_path.write_text(json.dumps(cfg))
                assert not scope.current()
                if window == "pre_link":
                    # Current config must reject even without Host teardown.
                    release.set()
                    await _wait_event(thread_done)
                    await asyncio.wait_for(listener, timeout=5)
                    assert not host_closed.is_set() and not close_returned.is_set()
                closer = asyncio.create_task(close_owned_scope())
                if window == "post_link":
                    other_closer = asyncio.create_task(close_owned_scope())
                    for _ in range(3):
                        await asyncio.sleep(0)
                        listener.cancel()
                        closer.cancel()
                    await asyncio.sleep(0.01)
                    assert not thread_done.is_set()
                    assert not closer.done() and not other_closer.done()
                    assert not host_closed.is_set() and not close_returned.is_set()
                    release.set()
                    await asyncio.wait_for(asyncio.gather(
                        closer, other_closer, return_exceptions=True), timeout=5)
                else:
                    # A revoked queue wait finishes with the foreign flock held.
                    await asyncio.wait_for(asyncio.shield(closer), timeout=5)
                assert thread_done.is_set()
            # Release the real queue lock, then observe the real publication.
            await _wait_event(thread_done)
            await asyncio.wait_for(asyncio.gather(closer, return_exceptions=True), timeout=5)
            await asyncio.gather(listener, return_exceptions=True)
            assert host_closed.is_set() and close_returned.is_set() and scope.closed and not scope.active
            if window == "post_link":
                assert len(publications) == 1
                result, = handler_results
                assert result["ok"]
                assert result["payload_hash"] == preview["payload_hash"]
                assert result["receipt"] == {
                    "command_id": preview["payload"]["command_id"],
                    "payload_hash": mcs_requests.payload_hash(preview["payload"]),
                    "outcome": "unknown", "error": "queue_durability_unknown"}
                queued, = inbox.glob("*.json")
                payload = json.loads(queued.read_text())
                assert payload == preview["payload"]
                receipt = mcs_requests.apply_command(ledger, payload, cfg=cfg)
                assert receipt["outcome"] == "applied"
                assert mcs_requests.apply_command(ledger, payload, cfg=cfg) == receipt
                assert ledger.db.execute("SELECT count(*) FROM requests").fetchone()[0] == 1
            else:
                assert handler_results == [{"ok": False, "error": "enqueue_failed"}]
                assert publications == []
                assert not list(inbox.glob("*.json"))
            # Existing admission defense works for newly started handlers.
            rejected = json.loads(handler(raw, identity))
            assert rejected == {"ok": False, "error": "plugin_config_incomplete"}
            late = [item for item in publications if item["after_host_close"]]
            assert not any(item["after_close_return"] for item in publications)
            # Check the queue consumer too: no scope/config bypass is mocked.
            outcomes = [mcs_requests.apply_command(
                ledger, json.loads(item["path"].read_text()), cfg=cfg)["outcome"]
                for item in late]
            assert not late, {
                "late_publications": len(late), "post_revocation_apply_outcomes": outcomes,
                "handler_confirmed": [r.get("ok") for r in handler_results],
                "new_admission": rejected["error"]}
        finally:
            # Every assertion path releases the flock before waiting for executor work.
            release.set()
            if listener is not None:
                if not listener.done():
                    listener.cancel()
                await asyncio.gather(listener, return_exceptions=True)
                if at_queue.is_set():
                    await _wait_event(thread_done)
            if closer is not None:
                await asyncio.wait_for(asyncio.gather(closer, return_exceptions=True), timeout=5)
            if other_closer is not None:
                await asyncio.wait_for(asyncio.gather(other_closer, return_exceptions=True), timeout=5)
            await scope.close()
            await host.close()

    try:
        asyncio.run(exercise())
    finally:
        ledger.close()


@pytest.mark.parametrize("revocation", ["unload", "configuration", "route"])
def test_slack_pending_confirmation_rechecks_current_scope(tmp_path, monkeypatch, revocation):
    import mcs_requests
    from ledger import publish_snapshot
    from ops_testkit import _source
    from adapters.common import commands
    from adapters.slack.actions import Actions

    db = _source(tmp_path)
    data = tmp_path / "data"
    inbox = data / "cmd"
    inbox.mkdir(parents=True)
    flags = data / "flags" / "notify.json"
    flags.parent.mkdir()
    flags.write_text(json.dumps({"interactive": True, "transport": "slack", "route_epoch": 1}))
    snapshot = publish_snapshot(str(tmp_path / "source.db"), str(data / "snapshots"))
    settings = {"transport": "slack", "profile": "synthetic", "application_id": "A1",
                "team_id": "T1", "channel_id": "C1", "route_epoch": 1,
                "data_root": str(data), "allowed_user_ids": {"U1"}, "project_ids": {1},
                "snapshot": snapshot, "inbox": str(inbox)}
    actions = Actions(SimpleNamespace(), settings, {"flags": str(flags.parent)},
                      None, None, lambda *_a, **_k: None)
    actions._active = True
    config_current = True
    actions._native_current = lambda: config_current
    preview = json.loads(commands.answer(settings, json.dumps({
        "op": "request", "phase": "preview", "action": "create", "project_id": 1,
        "source_message_id": 1, "title": "完全合成の依頼", "reason": "完全合成の人承認"}),
        user="U1", channel="C1"))
    assert preview["ok"], preview
    confirmation = {key: preview[key] for key in ("payload", "payload_hash", "origin")}
    confirmation.update(op="request", phase="confirm")
    real_lock, real_answer = mcs_requests.queue_lock, commands.answer
    at_queue, thread_done = threading.Event(), threading.Event()
    results = []

    @contextmanager
    def observed_lock(path, *, wait=True):
        at_queue.set()
        with real_lock(path, wait=wait) as acquired:
            yield acquired

    def observed_answer(*args, **kwargs):
        try:
            result = real_answer(*args, **kwargs)
            results.append(json.loads(result))
            return result
        finally:
            thread_done.set()

    monkeypatch.setattr(mcs_requests, "queue_lock", observed_lock)
    monkeypatch.setattr(commands, "answer", observed_answer)

    async def ack():
        pass

    async def scenario():
        nonlocal config_current
        listener = None
        try:
            with real_lock(str(inbox)):
                listener = asyncio.create_task(actions._command(ack, {
                    "team_id": "T1", "api_app_id": "A1", "user_id": "U1",
                    "channel_id": "C1", "text": json.dumps(confirmation)}))
                await _wait_event(at_queue)
                if revocation == "unload":
                    actions.unload()
                elif revocation == "configuration":
                    config_current = False
                else:
                    flags.write_text(json.dumps({"interactive": False, "transport": "slack"}))
                listener.cancel()
                await asyncio.sleep(0)
                listener.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(listener), timeout=5)
                assert thread_done.is_set()
            assert results == [{"ok": False, "error": "enqueue_failed"}]
            assert not list(inbox.iterdir())
        finally:
            actions.unload()
            if listener is not None:
                listener.cancel()
                await asyncio.gather(listener, return_exceptions=True)

    try:
        asyncio.run(scenario())
    finally:
        db.close()
