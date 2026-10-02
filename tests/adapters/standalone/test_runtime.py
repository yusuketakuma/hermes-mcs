"""Native job ownership with synthetic queues and stubbed child processes."""
from datetime import datetime, timezone
import json
from types import SimpleNamespace

from mcs_standalone import runtime, service
from mcs_util import UPDATE_MARKER_NAME


def test_jobs_quiesce_and_restart_after_the_updater_finishes(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    for name in runtime.COMMANDS:
        (data / name).mkdir()
    launched = []

    class Child:
        def __init__(self, argv, **kw):
            self.pid = 1000 + len(launched)
            self.code = None
            self.terminated = False
            launched.append((argv, kw, self))

        def poll(self):
            return self.code

        def terminate(self):
            self.terminated = True
            self.code = 0

        def kill(self):
            self.code = -9

    monkeypatch.setattr(runtime.subprocess, "Popen", Child)
    host = runtime.Runtime(tmp_path, {"runtime_mode": "standalone"})
    now = datetime(2026, 10, 2, 5, 10, tzinfo=timezone.utc).timestamp()
    assert len(host.schedules) == 6
    host.tick(now)
    assert set(host.children) == {"extract-0", "extract-2", "mcs_check", "mcs_health", "update"}
    assert all(kw["env"]["MCS_ROOT"] == str(tmp_path) for _, kw, _ in launched)
    assert host.argv["extract-0"][-2:] == ["0", "--semantic"]
    assert host.argv["extract-2"][-2:] == ["2", "--semantic"]
    host.tick(now + 0.5)
    assert len(launched) == 5  # one instance per job and scheduled minute

    marker = data / UPDATE_MARKER_NAME
    marker.write_text("synthetic")
    host.tick(now + 1)
    assert all(not host.children[job]["process"].terminated
               for job in ("mcs_check", "mcs_health", "update"))
    host.children["mcs_check"]["process"].code = 0
    host.children["mcs_health"]["process"].code = 0
    host.tick(now + 2)
    snapshot = json.loads((data / service.STATUS_FILE).read_text())
    assert snapshot["update_in_progress"] is True and set(snapshot["children"]) == {"update"}
    assert (data / service.STATUS_FILE).stat().st_mode & 0o077 == 0

    marker.unlink()
    for name in runtime.COMMANDS:
        (data / name / "synthetic.json").write_text("{}")
    host.tick(now + 3)
    assert set(runtime.BACKGROUND) | set(runtime.COMMANDS) <= set(host.children)
    request = data / service.RESTART_FILE
    request.write_text(json.dumps({"generation": "wrong-generation", "request_id": "a" * 32,
                                   "requested_at": now + 4}))
    request.chmod(0o600)
    assert not host.tick(now + 4) and not host.restarting
    request.write_text(json.dumps({"generation": host.generation, "request_id": "a" * 32,
                                   "requested_at": float("nan")}))
    assert not host.tick(now + 5) and not host.restarting
    request.write_text(json.dumps({"generation": host.generation, "request_id": "a" * 32,
                                   "requested_at": now + 6}))
    assert not host.tick(now + 6) and host.restarting
    assert not host.children["update"]["process"].terminated
    for row in host.children.values():
        row["process"].code = 0
    assert host.tick(now + 7) and host.children == {}
    # Pending command files cannot create another child during a restart.
    assert all(isinstance(child, Child) for _, _, child in launched)


def test_failed_child_start_requests_a_safe_restart(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    host = runtime.Runtime(tmp_path, {"runtime_mode": "standalone"})
    running = SimpleNamespace(pid=1001, poll=lambda: None, terminated=False)
    host.children["update"] = {"process": running, "kind": "core", "stopping_at": None}
    def unavailable(*args, **kwargs):
        raise OSError("synthetic unavailable executable")
    monkeypatch.setattr(runtime.subprocess, "Popen", unavailable)
    host.start("extract-0", "background", 1)
    assert host.restarting and host.children["update"]["process"] is running


def test_log_and_status_failures_still_drain_children_and_close_connector(tmp_path, monkeypatch):
    import asyncio
    import pytest
    # Keep scheduled core jobs out: these stub children finish only on terminate.
    now = datetime(2026, 10, 2, 5, 11, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(runtime.time, 'time', lambda: now)
    cfg = {"runtime_mode": "standalone"}
    original_runtime = runtime.Runtime
    hosts, children, closed = [], [], []
    def host(*args):
        value = original_runtime(*args)
        hosts.append(value)
        return value
    class Child:
        pid = 1001
        code = None
        def __init__(self, *args, **kwargs):
            children.append(self)
        def poll(self):
            return self.code
        def terminate(self):
            self.code = 0
        def kill(self):
            self.code = -9
    real_open = runtime.os.open
    def open_log(path, *args, **kwargs):
        if str(path).endswith('extract-2.log'):
            raise PermissionError('synthetic log failure')
        return real_open(path, *args, **kwargs)
    def broken_status(*args, **kwargs):
        raise OSError('synthetic status failure')
    async def connector(root, stopping):
        await stopping.wait()
        closed.append(True)
    sleep = asyncio.sleep
    async def fast_sleep(*args):
        await sleep(0)
    monkeypatch.setattr(runtime, 'Runtime', host)
    monkeypatch.setattr(runtime.subprocess, 'Popen', Child)
    monkeypatch.setattr(runtime.os, 'open', open_log)
    monkeypatch.setattr(runtime, 'atomic_write', broken_status)
    monkeypatch.setattr(runtime.config, 'load', lambda root: cfg)
    monkeypatch.setattr(runtime.asyncio, 'sleep', fast_sleep)
    with pytest.raises(OSError, match='synthetic status failure'):
        asyncio.run(runtime.serve(tmp_path, cfg, connector))
    assert len(children) == 1 and children[0].code == 0
    assert not hosts[0].children and closed == [True]
