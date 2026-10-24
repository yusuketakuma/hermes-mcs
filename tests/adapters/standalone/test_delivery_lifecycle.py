"""Synthetic delivery ownership survives reconnects and cancelled shutdown waiters."""
import asyncio
import fcntl
import threading
from types import SimpleNamespace

import pytest

from adapters.common import worker as common_worker
from adapters.discord import tasks as discord_tasks
from adapters.slack import tasks as slack_tasks
from mcs_standalone.host import DeliveryAdmission, Host


async def _until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(wait(), 5)


def _provision(root):
    for name in ("flags", "cmd_int", "cmd_results", "discord_render", "slack_render"):
        (root / name).mkdir()


def test_delivery_owner_requires_current_ready_identity_and_explicit_reopen():
    owner = DeliveryAdmission()
    first = owner.begin_connector()
    assert owner.snapshot() == {"open": True, "state": "starting", "ready": False,
                                "active_passes": 0}
    assert not owner.begin_pass(first)
    owner.connector_ready(first)
    assert owner.begin_pass(first)
    owner.close()
    assert not owner.begin_pass(first) and not owner.send_allowed(first)
    with pytest.raises(RuntimeError, match="not_drained"):
        owner.end_connector(first)
    owner.connector_closing(first)
    owner.connector_ready(first)
    assert owner.snapshot()["state"] == "closing"
    owner.end_pass(first)
    owner.end_connector(first)
    second = owner.begin_connector()
    owner.connector_ready(first)
    assert not owner.snapshot()["ready"]
    owner.connector_ready(second)
    assert not owner.begin_pass(second)  # Reconnect cannot reopen a reservation.
    owner.open()
    assert not owner.begin_pass(first) and owner.begin_pass(second)
    owner.connector_starting(second)
    assert not owner.snapshot()["ready"] and owner.snapshot()["active_passes"] == 1
    owner.end_pass(second)
    owner.connector_closing(first)
    with pytest.raises(RuntimeError, match="not_drained"):
        owner.end_connector(first)
    assert owner.snapshot()["state"] == "starting"
    owner.end_connector(second)


@pytest.mark.parametrize("platform", ["slack", "discord"])
@pytest.mark.parametrize("phase", ["startup", "tick", "followup"])
def test_supervisor_keeps_scope_until_actual_thread_ends(
        tmp_path, monkeypatch, platform, phase):
    _provision(tmp_path)
    entered, release, ended = (threading.Event() for _ in range(3))
    settings = {"transport": platform, "data_root": str(tmp_path),
                "profile": "synthetic", "application_id": "1", "channel_id": "2",
                "team_id": "T_SYNTHETIC", "guild_id": "3"}
    calls = []

    def blocked():
        entered.set()
        try:
            assert release.wait(5), "synthetic delivery was not released"
        finally:
            ended.set()

    class Worker(common_worker.DeliveryWorker):
        def __init__(self, **kwargs):
            kwargs.pop("sender", None)
            kwargs["bot"] = None
            super().__init__(**kwargs)

        async def reload_reconcile(self):
            if phase == "startup":
                await asyncio.to_thread(blocked)

        async def tick(self):
            calls.append("tick")
            if phase == "tick":
                await asyncio.to_thread(blocked)

    class Actions:
        def __init__(self, **kwargs):
            self._active = False
            self.on_interaction = lambda *_: None

        def register(self):
            self._active = True

        def unload(self):
            self._active = False

        async def sweep_followups(self):
            calls.append("followup")
            if phase == "followup":
                await asyncio.to_thread(blocked)

    async def bound():
        return True

    module = slack_tasks if platform == "slack" else discord_tasks
    monkeypatch.setattr(module, "POLL_S", 0.001)
    monkeypatch.setattr(module, "_LIVE", {})
    if platform == "slack":
        monkeypatch.setattr(module, "DeliveryWorker", Worker)
        monkeypatch.setattr(module, "SlackCardAdapter", lambda *a, **kw: SimpleNamespace(bind=bound))
        monkeypatch.setattr(module, "Actions", Actions)
    else:
        monkeypatch.setattr(module.delivery, "DeliveryWorker", Worker)
        monkeypatch.setattr(module.actions, "Actions", Actions)

    async def scenario():
        owner = DeliveryAdmission()
        identity = owner.begin_connector()
        host = Host(settings, delivery_admission=owner, delivery_identity=identity)
        bot = SimpleNamespace(is_closed=lambda: False, add_listener=lambda *a: None,
                              remove_listener=lambda *a: None)
        kwargs = {"app": None, "adapter": None} if platform == "slack" else {"bot": bot}
        supervisor = module.Supervisor(ctx=host, settings=settings,
                                       log=lambda *a, **kw: None, **kwargs)
        assert supervisor.start()
        closing = None
        try:
            if phase != "startup":
                await _until(lambda: supervisor._ready)
                assert not calls and not owner.snapshot()["ready"]
                owner.connector_ready(identity)
            await _until(entered.is_set)
            assert owner.snapshot()["active_passes"] == (phase != "startup")
            closing = asyncio.create_task(host.close())
            await _until(lambda: supervisor._stopping)
            for _ in range(3):
                supervisor._task.cancel()
                closing.cancel()
                await asyncio.sleep(0)
                assert not closing.done() and not supervisor._task.done()
                assert not ended.is_set() and supervisor._worker._lock_fd is not None
            assert owner.snapshot()["state"] == "closing"
            lock_path, = tmp_path.rglob("send-*.lock")
            with lock_path.open("ab") as contender:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(closing, 5)
            assert ended.is_set() and supervisor._worker._lock_fd is None
            assert owner.snapshot()["active_passes"] == 0 and not host.tasks
            owner.end_connector(identity)
        finally:
            release.set()
            supervisor.unload()
            if closing is None:
                closing = asyncio.create_task(host.close())
            await asyncio.gather(closing, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["delivered", "unknown"])
def test_closed_gate_still_publishes_primary_result_without_another_send(
        tmp_path, monkeypatch, outcome):
    from adapters.common import envelopes, journal, registry
    from adapters.common import paths

    _provision(tmp_path)
    settings = {"transport": "discord", "profile": "synthetic", "application_id": "1",
                "channel_id": "2", "guild_id": "3"}
    dirs = paths.ensure_dirs(str(tmp_path))
    reg = registry.Registry(dirs["state"], scope=settings)
    worker = common_worker.DeliveryWorker(bot=None, settings=settings, root=str(tmp_path),
                                         reg=reg, worker_id="synthetic-worker", log=lambda *a, **kw: None)
    monkeypatch.setattr(paths, "read_flags", lambda *_: {})
    monkeypatch.setattr(paths, "route_open", lambda *a, **kw: True)
    receipts, sends, parts = [], [], []
    monkeypatch.setattr(envelopes, "transport_receipt", lambda claim, result, **kw: {"result": result, **kw})
    monkeypatch.setattr(envelopes, "publish_command", lambda directory, envelope: receipts.append(envelope))

    async def scenario():
        owner = DeliveryAdmission()
        identity = owner.begin_connector()
        owner.connector_ready(identity)
        worker.send_admitted = lambda: owner.send_allowed(identity)
        entered, release = asyncio.Event(), asyncio.Event()

        async def perform(claim):
            sends.append(claim["attempt_id"])
            entered.set()
            await release.wait()
            return {"result": outcome, **({"message_id": "123"} if outcome == "delivered" else {})}

        async def deliver_parts(*args):
            parts.append(True)

        worker._perform = perform
        worker._deliver_parts = deliver_parts
        claim = {"phase": "granted", "attempt_id": "synthetic-attempt",
                 "spec_path": str(tmp_path / "synthetic.json"),
                 "spec": {"op": "create", "delivery_id": "synthetic-delivery",
                          "delivery": {"correlation": "synthetic", "route_epoch": 1}}}
        reg.claim(claim["spec"]["delivery_id"], claim)
        task = asyncio.create_task(common_worker.await_settled(worker._step_claim(claim)))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            owner.close()
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            assert receipts[0]["result"] == outcome and not parts
            rows = journal.scan_checked(dirs["state"])[0]["synthetic-attempt"]
            assert [row["phase"] for row in rows] == ["started", "result", "receipt"]
            assert rows[-1]["result"] == outcome and sends == ["synthetic-attempt"]
            assert not await worker._send_allowed(claim["spec"])
            await worker._step_claim(claim)
            assert sends == ["synthetic-attempt"]
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_helper_task_creation_failure_closes_unused_coroutine(monkeypatch):
    async def operation():
        pytest.fail("unowned operation must never start")

    async def scenario():
        coroutine = operation()

        def fail(_coroutine):
            raise RuntimeError("synthetic scheduling failure")

        monkeypatch.setattr(common_worker.asyncio, "create_task", fail)
        with pytest.raises(RuntimeError, match="synthetic scheduling failure"):
            await common_worker.await_settled(coroutine)
        assert coroutine.cr_frame is None

    asyncio.run(scenario())


@pytest.mark.parametrize("blocked_at", ["transport", "receipt"])
def test_part_and_receipt_settle_after_pause_without_starting_next_part(
        tmp_path, monkeypatch, blocked_at):
    from adapters.common import envelopes, journal, paths, registry

    _provision(tmp_path)
    dirs = paths.ensure_dirs(str(tmp_path))
    reg = registry.Registry(dirs["state"], scope={})
    worker = common_worker.DeliveryWorker(bot=None, settings={}, root=str(tmp_path),
                                         reg=reg, worker_id="synthetic-part", log=lambda *a, **kw: None)
    monkeypatch.setattr(paths, "read_flags", lambda *_: {})
    monkeypatch.setattr(paths, "route_open", lambda *a, **kw: True)
    monkeypatch.setattr(envelopes, "part_receipt", lambda claim, part, result, **kw:
                        {"result": result, "part_id": part["part_id"], **kw})
    receipts, sends = [], []
    entered, release, ended = (threading.Event() for _ in range(3))
    manifest = [{"part_id": "body:0001", "kind": "body_part"},
                {"part_id": "body:0002", "kind": "body_part"}]
    spec = {"op": "create", "delivery_id": "synthetic-delivery", "parts": {},
            "delivery": {"route_epoch": 1}}
    (tmp_path / "discord_render" / "synthetic-delivery.json").write_text("{}")

    def publish(_directory, envelope):
        if blocked_at == "receipt":
            entered.set()
            try:
                assert release.wait(5), "synthetic receipt was not released"
            finally:
                ended.set()
        receipts.append(envelope)

    monkeypatch.setattr(envelopes, "publish_command", publish)

    async def scenario():
        owner = DeliveryAdmission()
        identity = owner.begin_connector()
        owner.connector_ready(identity)
        worker.send_admitted = lambda: owner.send_allowed(identity)

        async def perform(claim, part, ctx):
            sends.append(part["part_id"])
            if blocked_at == "transport":
                entered.set()
                await _until(release.is_set)
                ended.set()
            return {"result": "delivered", "remote_id": "123"}

        worker._perform_part = perform
        claim = {"spec": spec}
        task = asyncio.create_task(common_worker.await_settled(worker._drive_parts(
            claim, manifest, {"thread_id": "456"}, {})))
        try:
            await _until(entered.is_set)
            owner.close()
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done() and not ended.is_set()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            records, clean = journal.scan_checked(dirs["state"])
            aid = envelopes.part_attempt_id(spec["delivery_id"], "body:0001")
            assert clean and [row["phase"] for row in records[aid]] == ["started", "result", "receipt"]
            assert receipts[0]["result"] == "delivered" and sends == ["body:0001"]
            assert not reg.parts_done(spec["delivery_id"])
            await worker._drive_parts(claim, manifest, {"thread_id": "456"}, records)
            assert len(receipts) == 1 and sends == ["body:0001"]
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("platform", ["slack", "discord"])
@pytest.mark.parametrize("startup_fails", [False, True])
def test_connector_requires_sdk_and_supervisor_readiness_and_drains_cleanup(
        tmp_path, monkeypatch, platform, startup_fails):
    from adapters.discord import standalone as discord
    from adapters.slack import standalone as slack
    from mcs_standalone import config

    module = slack if platform == "slack" else discord
    monkeypatch.setattr(config, "connector_settings", lambda *a: {"channel_id": "2", "application_id": "1"})
    monkeypatch.setattr(config, "load_credentials", lambda *a: {"bot_token": "synthetic"})

    async def scenario():
        owner = DeliveryAdmission()
        identity = owner.begin_connector()
        stopping = asyncio.Event()
        sdk_entered, sdk_release = asyncio.Event(), asyncio.Event()
        cleanup_entered, cleanup_release = asyncio.Event(), asyncio.Event()
        supervisors, listeners = [], {}
        socket_connected = False

        async def noop(*args, **kwargs):
            return None

        class Supervisor:
            def __init__(self, **kwargs):
                self.host = kwargs["ctx"]
                self._ready = False
                self._actions = SimpleNamespace(_active=True, on_interaction=noop,
                                                unload=lambda: None)
                self._worker = SimpleNamespace(stop=lambda: None)
                supervisors.append(self)

            def start(self):
                self._task = self.host.spawn_task(asyncio.Event().wait())
                return True

        monkeypatch.setattr(module, "Supervisor", Supervisor)

        async def close():
            cleanup_entered.set()
            await cleanup_release.wait()

        async def connect(*args, **kwargs):
            nonlocal socket_connected
            sdk_entered.set()
            await sdk_release.wait()
            if startup_fails:
                raise ValueError("synthetic_startup_failure")
            socket_connected = True
            if platform == "discord":
                await listeners["on_ready"]()
                await asyncio.Event().wait()

        async def is_connected():
            return socket_connected

        if platform == "slack":
            client = SimpleNamespace(closed=False, is_connected=is_connected)
            monkeypatch.setattr(module, "_client", lambda *a: (client, SimpleNamespace(close=close)))
            monkeypatch.setattr(module, "_identity", noop)

            async def socket(*args):
                return None, SimpleNamespace(client=client, connect_async=connect, close_async=noop)

            monkeypatch.setattr(module, "_socket", socket)
        else:
            bot = SimpleNamespace(
                tree=SimpleNamespace(add_command=lambda command: None), login=noop,
                fetch_channel=noop, connect=connect, close=close,
                add_listener=lambda callback, name: listeners.__setitem__(name, callback),
                http=SimpleNamespace(upsert_global_command=noop))
            monkeypatch.setattr(module, "_bot", lambda *a, **kw: bot)
            monkeypatch.setattr(module, "_verify_identity", lambda *a, **kw: None)
            monkeypatch.setattr(module, "_verify_channel", lambda *a: None)
            monkeypatch.setattr(module, "_sdk", lambda: (SimpleNamespace(app_commands=SimpleNamespace(
                Command=lambda **kw: SimpleNamespace(to_dict=lambda tree: {}))), None))
            monkeypatch.setattr(module, "_Commands", lambda *a: SimpleNamespace(slash=noop, message=noop, error=noop))
            monkeypatch.setattr(module, "_Scope", lambda *a, **kw: SimpleNamespace(
                current=lambda: True, monitor=lambda: asyncio.Event().wait(), close=noop))

        running = asyncio.create_task(module.run(
            tmp_path, stopping, delivery_admission=owner, delivery_identity=identity))
        try:
            await asyncio.wait_for(sdk_entered.wait(), 5)
            assert not owner.snapshot()["ready"]
            if platform == "discord":
                await listeners["on_connect"]()
                assert not owner.snapshot()["ready"]
            sdk_release.set()
            if startup_fails:
                await asyncio.wait_for(cleanup_entered.wait(), 5)
                assert owner.snapshot()["state"] == "closing"
            else:
                await _until(lambda: socket_connected)
                assert not owner.snapshot()["ready"]  # SDK success alone is insufficient.
                supervisors[0]._ready = True
                await _until(lambda: owner.snapshot()["ready"])
                owner.close()
                if platform == "discord":
                    await listeners["on_disconnect"]()
                else:
                    socket_connected = False
                await _until(lambda: owner.snapshot()["state"] == "starting")
                if platform == "discord":
                    await listeners["on_resumed"]()
                else:
                    socket_connected = True
                await _until(lambda: owner.snapshot()["ready"])
                assert not owner.begin_pass(identity)  # SDK reconnect cannot reopen the gate.
                stopping.set()
                await asyncio.wait_for(cleanup_entered.wait(), 5)
                assert owner.snapshot()["state"] == "closing"
                for _ in range(3):
                    running.cancel()
                    await asyncio.sleep(0)
                    assert not running.done()
            cleanup_release.set()
            error = ValueError if startup_fails else asyncio.CancelledError
            with pytest.raises(error):
                await asyncio.wait_for(running, 5)
            assert not supervisors[0].host.tasks
            owner.end_connector(identity)
            assert owner.snapshot()["state"] == "off"
        finally:
            stopping.set()
            sdk_release.set()
            cleanup_release.set()
            await asyncio.gather(running, return_exceptions=True)

    asyncio.run(scenario())
