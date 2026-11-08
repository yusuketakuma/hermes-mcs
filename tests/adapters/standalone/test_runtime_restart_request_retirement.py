"""Retire only verified old requests under the new host's startup ownership."""
import asyncio
import fcntl
import json
import os

import pytest

import safe_io
from mcs_standalone import runtime, service

NOW = 1_000_000.0


def _write(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def _old():
    return {"generation": "a" * 32, "request_id": "b" * 32, "requested_at": NOW - 100}


@pytest.fixture
def owner(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir(mode=0o700)
    monkeypatch.setattr(runtime.time, "time", lambda: NOW)
    host = runtime.Runtime(tmp_path, {"runtime_mode": "standalone"})
    host.generation = "c" * 32
    fd = safe_io.open_lock_file(host.data / "standalone.lock")
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    host._planning_host_fd = fd
    try:
        yield host
    finally:
        host._planning_host_fd = None
        os.close(fd)


def test_verified_old_request_is_retired_and_directory_synced_once(owner, monkeypatch):
    path = owner.data / service.RESTART_FILE
    _write(path, _old())
    syncs = []
    fsync = safe_io.fsync_dir

    def synced(directory):
        assert not path.exists()
        syncs.append(directory)
        fsync(directory)

    monkeypatch.setattr(safe_io, "fsync_dir", synced)
    assert owner._retire_previous_restart() is True
    assert syncs == [owner.data] and owner.status_written is None
    _write(path, _old())
    assert owner._retire_previous_restart() is False
    assert path.exists() and len(syncs) == 1


@pytest.mark.parametrize("damage", ["same_generation", "extra", "missing", "bool_time", "negative_time",
                                    "nonfinite", "bad_uuid", "duplicate", "symlink", "public"])
def test_unknown_or_current_generation_request_is_unchanged(owner, damage):
    path = owner.data / service.RESTART_FILE
    value = _old()
    if damage == "same_generation":
        value["generation"] = owner.generation
    elif damage == "extra":
        value["extra"] = "synthetic_secret"
    elif damage == "missing":
        value.pop("request_id")
    elif damage == "bool_time":
        value["requested_at"] = True
    elif damage == "negative_time":
        value["requested_at"] = -1
    elif damage == "nonfinite":
        value["requested_at"] = float("inf")
    elif damage == "bad_uuid":
        value["request_id"] = "invalid"
    if damage == "symlink":
        target = owner.data / "synthetic-request"
        _write(target, value)
        path.symlink_to(target)
    else:
        _write(path, value)
        if damage == "duplicate":
            path.write_text('{"generation":"' + owner.generation + '",' + json.dumps(value)[1:])
            assert json.loads(path.read_text())["generation"] != owner.generation
        if damage == "public":
            path.chmod(0o644)
    before = path.read_bytes()
    assert owner._retire_previous_restart() is False
    assert path.read_bytes() == before
    if damage == "symlink":
        assert path.is_symlink()


@pytest.mark.parametrize("guard", ["no_owner", "already_published"])
def test_retirement_does_not_run_outside_startup(owner, guard):
    path = owner.data / service.RESTART_FILE
    _write(path, _old())
    before = path.read_bytes()
    if guard == "no_owner":
        owner._planning_host_fd = None
    else:
        owner._publish_status(NOW, False)
        assert json.loads((owner.data / service.STATUS_FILE).read_text())["generation"] == owner.generation
    assert owner._retire_previous_restart() is False and path.read_bytes() == before


def test_real_serve_retirement_precedes_publication_and_keeps_later_manual_request(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir(mode=0o700)
    cfg = {"runtime_mode": "standalone"}
    monkeypatch.setattr(runtime.time, "time", lambda: NOW)
    host = runtime.Runtime(tmp_path, cfg)
    host.generation = "c" * 32
    path = host.data / service.RESTART_FILE
    _write(path, _old())
    monkeypatch.setattr(runtime, "Runtime", lambda *_: host)
    monkeypatch.setattr(runtime.config, "load", lambda *_: cfg)
    monkeypatch.setattr(runtime.signal, "signal", lambda *_: None)
    monkeypatch.setattr(service, "pid_exists", lambda pid: True)
    monkeypatch.setattr(host, "adopt_orphans", lambda: None)
    monkeypatch.setattr(host, "recover_inflight", lambda: None)
    monkeypatch.setattr(host, "_respond_restart_planning", lambda *_: None)
    calls, ticks, manual = [], [], []
    retire = host._retire_previous_restart

    def under_owner():
        assert host.status_written is None
        fd = safe_io.open_lock_file(host.data / "standalone.lock")
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        calls.append(True)
        return retire()

    def tick(**kwargs):
        ticks.append(True)
        if len(ticks) == 1:
            assert not path.exists() and calls == [True]
            host._publish_status(NOW, False)
            request_id = service.request_restart(tmp_path)
            manual.append(path.read_bytes())
            assert json.loads(manual[0])["request_id"] == request_id
            assert host._restart_requested(NOW) is True
            return False
        assert path.read_bytes() == manual[0] and calls == [True]
        host.restarting = True
        return True

    monkeypatch.setattr(host, "_retire_previous_restart", under_owner)
    monkeypatch.setattr(host, "tick", tick)
    sleep = asyncio.sleep

    async def yield_once(*_):
        await sleep(0)

    monkeypatch.setattr(runtime.asyncio, "sleep", yield_once)
    assert asyncio.run(runtime.serve(tmp_path, cfg)) == 0
    assert len(ticks) == 2 and path.read_bytes() == manual[0]
    assert host._planning_host_fd is None
