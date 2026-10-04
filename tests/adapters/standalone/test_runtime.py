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
    assert set(host.children) == {"extract-0", "extract-2", "mcs_check", "mcs_deep", "mcs_health", "update"}
    assert all(kw["env"]["MCS_ROOT"] == str(tmp_path) for _, kw, _ in launched)
    assert host.argv["extract-0"][-2:] == ["0", "--semantic"]
    assert host.argv["extract-2"][-2:] == ["2", "--semantic"]
    host.tick(now + 0.5)
    assert len(launched) == 6  # one instance per job and scheduled minute

    marker = data / UPDATE_MARKER_NAME
    marker.write_text("synthetic")
    host.tick(now + 1)
    assert all(not host.children[job]["process"].terminated
               for job in ("mcs_check", "mcs_deep", "mcs_health", "update"))
    host.children["mcs_check"]["process"].code = 0
    host.children["mcs_deep"]["process"].code = 0
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


def test_interactive_command_intake_retries_sooner_only_after_success(tmp_path, monkeypatch):
    data = tmp_path / "data"
    for name in runtime.COMMANDS:
        (data / name).mkdir(parents=True)
    host = runtime.Runtime(tmp_path, {"runtime_mode": "standalone"})
    for job, code in (("cmd_int", 0), ("cmd", 0), ("extract-0", 0)):
        host.children[job] = {"process": SimpleNamespace(pid=1, poll=lambda c=code: c),
                              "kind": "core", "stopping_at": None}
    host.tick(100, draining=True)
    assert host.retry_at == {"cmd_int": 110, "cmd": 130, "extract-0": 130}
    host.children["cmd_int"] = {"process": SimpleNamespace(pid=1, poll=lambda: 1),
                                "kind": "core", "stopping_at": None}
    host.tick(200, draining=True)
    assert host.retry_at["cmd_int"] == 230

    started = []
    monkeypatch.setattr(runtime.subprocess, "Popen",
                        lambda argv, **kw: started.append(argv) or SimpleNamespace(pid=2, poll=lambda: None))
    host.retry_at = {"cmd_int": 110}
    (data / "cmd_int" / "synthetic.json").write_text("{}")
    host.start("cmd_int", "core", 109)
    assert not started
    host.start("cmd_int", "core", 110)
    assert len(started) == 1


def test_new_command_file_after_success_starts_without_backoff(tmp_path, monkeypatch):
    data = tmp_path / "data"
    for name in runtime.COMMANDS:
        (data / name).mkdir(parents=True)
    inbox = data / "cmd_int"
    codes = []

    def spawn(argv, **kw):
        codes.append(None)
        index = len(codes) - 1
        return SimpleNamespace(pid=10 + index, poll=lambda: codes[index])

    monkeypatch.setattr(runtime.subprocess, "Popen", spawn)
    host = runtime.Runtime(tmp_path, {"runtime_mode": "standalone"})
    monkeypatch.setattr(host, "schedules", [])
    host.argv = {job: argv for job, argv in host.argv.items() if job in runtime.COMMANDS}
    monkeypatch.setattr(runtime, "BACKGROUND", {})

    (inbox / "a.json").write_text("{}")
    host.tick(100)
    assert host.children["cmd_int"]["pending"] == {"a.json"}
    codes[0] = 0
    (inbox / "a.json").unlink()
    host.tick(101)
    assert "cmd_int" not in host.children
    (inbox / "b.json").write_text("{}")
    host.tick(103)  # a new click 2 s later starts at once
    assert "cmd_int" in host.children and len(codes) == 2

    # A consent hold exits 0 but leaves the same files: keep the backoff.
    codes[1] = 0
    host.tick(104)
    host.tick(105)
    assert "cmd_int" not in host.children and len(codes) == 2

    # After a failure (e.g. lock_held rc 3) even a new file waits for the backoff.
    host.tick(115)
    codes[2] = 3
    host.tick(116)
    (inbox / "c.json").write_text("{}")
    host.tick(117)
    assert "cmd_int" not in host.children and len(codes) == 3
    host.tick(146)
    assert "cmd_int" in host.children and len(codes) == 4

    # `cmd` contacts MCS: a new file after success still waits for its backoff.
    cmd = data / "cmd"
    (cmd / "r.json").write_text("{}")
    host.tick(150)
    assert "cmd" in host.children and len(codes) == 5
    codes[4] = 0
    (cmd / "r.json").unlink()
    host.tick(151)
    (cmd / "s.json").write_text("{}")
    host.tick(153)
    assert "cmd" not in host.children and len(codes) == 5


def test_scheduled_slot_runs_even_if_previous_run_ended_just_before(tmp_path, monkeypatch):
    codes = []

    def spawn(argv, **kw):
        codes.append(None)
        index = len(codes) - 1
        return SimpleNamespace(pid=10 + index, poll=lambda: codes[index])

    monkeypatch.setattr(runtime.subprocess, "Popen", spawn)
    monkeypatch.setattr(runtime, "BACKGROUND", {})
    (tmp_path / "data").mkdir()
    host = runtime.Runtime(tmp_path, {"runtime_mode": "standalone"})
    slot = 6000 * 60  # a whole minute
    minute = runtime.time.localtime(slot).tm_min
    host.schedules = [("mcs_check", [{"Minute": minute}])]
    host.argv["mcs_check"] = ["synthetic"]
    host.children["mcs_check"] = {"process": SimpleNamespace(pid=1, poll=lambda: 0),
                                  "kind": "core", "stopping_at": None}
    host.tick(slot - 10)  # the previous run ends 10 s before the slot
    assert host.retry_at["mcs_check"] == slot + 20
    host.tick(slot)
    assert "mcs_check" in host.children and len(codes) == 1
