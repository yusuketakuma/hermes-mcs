"""Auto-mode gateway restart requests bind PID generation, request ID and lock FD."""
import json
import os
import plistlib
import subprocess
from types import SimpleNamespace

import pytest

from ops_testkit import _load

REQUEST_ID = "0123456789abcdef" * 2


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    rec = _load()
    home = tmp_path / 'home'
    agents = home / 'Library/LaunchAgents'
    agents.mkdir(parents=True)
    data = home / '.mcs/data'
    data.mkdir(parents=True)
    command = home / '.local/bin/hermes'
    command.parent.mkdir(parents=True)
    command.write_text('#!/bin/sh\nexit 0\n')
    command.chmod(0o700)
    plist = agents / 'ai.hermes.gateway.plist'
    plist.write_bytes(plistlib.dumps({
        'Label': 'ai.hermes.gateway',
        'WorkingDirectory': str(home / '.hermes'),
        'EnvironmentVariables': {'PATH': str(command.parent)},
        'ProgramArguments': ['/usr/bin/osascript', '-l', 'JavaScript',
                             'Application("Terminal").doScript("hermes gateway run")']}))
    calls = []
    running = {'pid': 101}

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[-4:] == ['-p', 'default', 'gateway', 'restart']:
            assert kwargs['stdin'] == subprocess.DEVNULL
            assert kwargs['stdout'] is kwargs['stderr'] and kwargs['stdout'].fileno() >= 0
            assert kwargs['env']['HOME'] == str(home)
            assert kwargs['env']['HERMES_HOME'] == str(plistlib.loads(plist.read_bytes()).get(
                'EnvironmentVariables', {}).get('HERMES_HOME', home / '.hermes'))
            running['pid'] = 202
            return subprocess.CompletedProcess(argv, 0)
        assert argv[0] == 'launchctl'
        if argv[-1].startswith(running.get('domain', 'user') + '/'):
            return subprocess.CompletedProcess(argv, 0,
                                               f'path = {plist}\npid = {running["pid"]}\n')
        return subprocess.CompletedProcess(argv, 113, '')

    monkeypatch.setattr(rec.subprocess, 'run', run)
    return rec, data, agents, plist, calls, running


def _lock_fd(data):
    path = data / "update.lock"
    path.touch(exist_ok=True)
    return os.open(str(path), os.O_RDWR)


def _spawned(monkeypatch, rec):
    spawned = []
    monkeypatch.setattr(rec.subprocess, "Popen",
                        lambda argv, **kw: spawned.append((argv, kw)))
    return spawned


def test_manual_request_keeps_argv_and_receipt_shape(gateway, monkeypatch):
    rec, data, agents, _, _, _ = gateway
    spawned = _spawned(monkeypatch, rec)
    assert rec.request_gateway_restart(str(data), str(agents), 501)
    argv, kw = spawned[0]
    assert argv[4:] == [str(data), str(agents), "501"]
    assert kw["pass_fds"] == ()
    report = json.loads((data / "gateway_restart.json").read_text())
    assert report["status"] == "requested"
    assert "request_id" not in report


def test_auto_request_extends_argv_and_binds_request(gateway, monkeypatch):
    rec, data, agents, _, _, _ = gateway
    fd = _lock_fd(data)
    spawned = _spawned(monkeypatch, rec)
    try:
        assert rec.request_gateway_restart(
            str(data), str(agents), 501,
            expected_pid=101, request_id=REQUEST_ID, update_lock_fd=fd)
    finally:
        os.close(fd)
    argv, kw = spawned[0]
    assert argv[4:] == [str(data), str(agents), "501", "101", REQUEST_ID, str(fd)]
    assert kw["pass_fds"] == (fd,)
    report = json.loads((data / "gateway_restart.json").read_text())
    assert report["status"] == "requested"
    assert report["request_id"] == REQUEST_ID


@pytest.mark.parametrize("kwargs", [
    {"expected_pid": 101},
    {"request_id": REQUEST_ID},
    {"update_lock_fd": 3},
    {"expected_pid": True, "request_id": REQUEST_ID, "update_lock_fd": 3},
    {"expected_pid": 101, "request_id": "A" * 32, "update_lock_fd": 3},
    {"expected_pid": 101, "request_id": "g" * 32, "update_lock_fd": 3},
    {"expected_pid": 101, "request_id": REQUEST_ID[:31], "update_lock_fd": 3},
    {"expected_pid": 0, "request_id": REQUEST_ID, "update_lock_fd": 3},
    {"expected_pid": 101, "request_id": REQUEST_ID, "update_lock_fd": -1},
])
def test_auto_request_rejects_invalid_args_without_receipt(gateway, monkeypatch, kwargs):
    rec, data, agents, _, _, _ = gateway
    spawned = _spawned(monkeypatch, rec)
    assert not rec.request_gateway_restart(str(data), str(agents), 501, **kwargs)
    assert spawned == []
    assert not (data / "gateway_restart.json").exists()


def test_auto_request_rejects_lock_fd_not_bound_to_update_lock(gateway, monkeypatch, tmp_path):
    rec, data, agents, _, _, _ = gateway
    _lock_fd(data)
    other = tmp_path / "other.lock"
    other.write_text("x")
    fd = os.open(str(other), os.O_RDWR)
    spawned = _spawned(monkeypatch, rec)
    try:
        assert not rec.request_gateway_restart(
            str(data), str(agents), 501,
            expected_pid=101, request_id=REQUEST_ID, update_lock_fd=fd)
    finally:
        os.close(fd)
    assert spawned == []
    assert not (data / "gateway_restart.json").exists()


def test_auto_request_rejects_nonregular_lock_fd(gateway, monkeypatch):
    rec, data, agents, _, _, _ = gateway
    spawned = _spawned(monkeypatch, rec)
    dir_fd = os.open(str(data), os.O_RDONLY)
    try:
        assert not rec.request_gateway_restart(
            str(data), str(agents), 501,
            expected_pid=101, request_id=REQUEST_ID, update_lock_fd=dir_fd)
    finally:
        os.close(dir_fd)
    assert spawned == []
    assert not (data / "gateway_restart.json").exists()


def test_auto_request_rejects_fd_when_update_lock_absent(gateway, monkeypatch, tmp_path):
    rec, data, agents, _, _, _ = gateway
    other = tmp_path / "other.lock"
    other.write_text("x")
    fd = os.open(str(other), os.O_RDWR)
    spawned = _spawned(monkeypatch, rec)
    try:
        assert not rec.request_gateway_restart(
            str(data), str(agents), 501,
            expected_pid=101, request_id=REQUEST_ID, update_lock_fd=fd)
    finally:
        os.close(fd)
    assert spawned == []
    assert not (data / "gateway_restart.json").exists()


def test_auto_run_target_changed_never_issues_restart(gateway):
    rec, data, agents, _, calls, running = gateway
    running["pid"] = 303
    fd = _lock_fd(data)
    try:
        assert rec._gateway_restart_run(
            str(data), str(agents), 501, "101", REQUEST_ID, str(fd)) == 1
    finally:
        os.close(fd)
    report = json.loads((data / "gateway_restart.json").read_text())
    assert report["status"] == "failed"
    assert report["error"] == "gateway_restart_target_changed"
    assert report["expected_pid"] == 101
    assert report["observed_pid"] == 303
    assert report["request_id"] == REQUEST_ID
    assert not any(c[-4:] == ["-p", "default", "gateway", "restart"] for c in calls)


def test_auto_run_zero_observed_pid_is_target_changed_not_success(gateway):
    rec, data, agents, _, calls, running = gateway
    running["pid"] = 0
    fd = _lock_fd(data)
    try:
        assert rec._gateway_restart_run(
            str(data), str(agents), 501, "101", REQUEST_ID, str(fd)) == 1
    finally:
        os.close(fd)
    report = json.loads((data / "gateway_restart.json").read_text())
    assert report["status"] == "failed"
    assert report["error"] == "gateway_restart_target_changed"
    assert report["observed_pid"] == 0
    assert not any(c[-4:] == ["-p", "default", "gateway", "restart"] for c in calls)


def test_auto_run_verifies_replacement_with_request_id(gateway):
    rec, data, agents, _, calls, running = gateway
    fd = _lock_fd(data)
    try:
        assert rec._gateway_restart_run(
            str(data), str(agents), 501, "101", REQUEST_ID, str(fd)) == 0
    finally:
        os.close(fd)
    report = json.loads((data / "gateway_restart.json").read_text())
    assert report["status"] == "supervisor_restart_verified"
    assert report["pid"] == 202 and report["previous_pid"] == 101
    assert report["request_id"] == REQUEST_ID
    assert any(c[-4:] == ["-p", "default", "gateway", "restart"] for c in calls)


def test_auto_run_rechecks_pid_before_each_retry(gateway, monkeypatch):
    rec, data, agents, plist, calls, running = gateway
    launches = []
    refusal = ("✗ Refusing to restart the gateway: 2 live session(s) would be interrupted.\n"
               "  • alpha\n  • beta\n"
               "  Let them finish, or interrupt them deliberately:\n"
               "    hermes gateway restart --yes   # interrupts 2 live session(s)\n").encode()

    def run(argv, **kwargs):
        if argv[-4:] == ["-p", "default", "gateway", "restart"]:
            launches.append(argv)
            kwargs["stdout"].write(refusal)
            kwargs["stdout"].flush()
            return subprocess.CompletedProcess(argv, 1)
        assert argv[0] == "launchctl"
        pid = 303 if launches else running["pid"]
        return subprocess.CompletedProcess(argv, 0, f"path = {plist}\npid = {pid}\n")

    monkeypatch.setattr(rec.subprocess, "run", run)
    clock = [0.0]
    monkeypatch.setattr(rec.mcs_recovery_gateway, "time", SimpleNamespace(
        time=lambda: 0.0, monotonic=lambda: clock[0],
        sleep=lambda n: clock.__setitem__(0, clock[0] + n)))
    fd = _lock_fd(data)
    try:
        assert rec._gateway_restart_run(
            str(data), str(agents), 501, "101", REQUEST_ID, str(fd)) == 1
    finally:
        os.close(fd)
    assert len(launches) == 1
    report = json.loads((data / "gateway_restart.json").read_text())
    assert report["status"] == "failed"
    assert report["error"] == "gateway_restart_target_changed"
    assert report["observed_pid"] == 303
    assert report["request_id"] == REQUEST_ID


def test_auto_run_rejects_malformed_args(gateway):
    rec, data, agents, _, calls, running = gateway
    fd = _lock_fd(data)
    try:
        assert rec._gateway_restart_run(
            str(data), str(agents), 501, "abc", REQUEST_ID, str(fd)) == 1
    finally:
        os.close(fd)
    report = json.loads((data / "gateway_restart.json").read_text())
    assert report["status"] == "failed"
    assert report["error"] == "gateway_restart_auto_args_invalid"
    assert report["request_id"] == REQUEST_ID
    assert not any(c[-4:] == ["-p", "default", "gateway", "restart"] for c in calls)


def test_auto_run_rejects_foreign_lock_fd(gateway, tmp_path):
    rec, data, agents, _, calls, running = gateway
    _lock_fd(data)
    other = tmp_path / "other.lock"
    other.write_text("x")
    fd = os.open(str(other), os.O_RDWR)
    try:
        assert rec._gateway_restart_run(
            str(data), str(agents), 501, "101", REQUEST_ID, str(fd)) == 1
    finally:
        os.close(fd)
    report = json.loads((data / "gateway_restart.json").read_text())
    assert report["status"] == "failed"
    assert report["error"] == "gateway_restart_lock_fd_mismatch"
    assert report["request_id"] == REQUEST_ID
    assert not any(c[-4:] == ["-p", "default", "gateway", "restart"] for c in calls)


def test_manual_run_receipt_has_no_request_id(gateway):
    rec, data, agents, _, _, _ = gateway
    assert rec._gateway_restart_run(str(data), str(agents), 501) == 0
    report = json.loads((data / "gateway_restart.json").read_text())
    assert report["status"] == "supervisor_restart_verified"
    assert "request_id" not in report


def test_auto_spawn_failure_keeps_request_id(gateway, monkeypatch):
    rec, data, agents, _, _, _ = gateway
    fd = _lock_fd(data)

    def boom(argv, **kw):
        raise OSError("synthetic")

    monkeypatch.setattr(rec.subprocess, "Popen", boom)
    try:
        assert not rec.request_gateway_restart(
            str(data), str(agents), 501,
            expected_pid=101, request_id=REQUEST_ID, update_lock_fd=fd)
    finally:
        os.close(fd)
    report = json.loads((data / "gateway_restart.json").read_text())
    assert report["status"] == "failed"
    assert report["error"] == "gateway_restart_spawn_failed"
    assert report["request_id"] == REQUEST_ID


def test_frozen_program_carries_auto_contract(gateway):
    rec, *_ = gateway
    program = rec.mcs_recovery_gateway._GATEWAY_RESTART_PROGRAM
    assert "gateway_restart_target_changed" in program
    assert "gateway_restart_lock_fd_mismatch" in program
    assert "request_id" in program
    assert "update_lock_fd" in program
