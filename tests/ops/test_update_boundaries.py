"""Discovery/launch preserve transaction, environment and descriptor boundaries."""
import builtins
import subprocess

import pytest

import mcs_operations
import mcs_util
import update_discovery
import update_launch


def test_discovery_uses_copied_environment_and_bounded_owned_session(monkeypatch):
    source = {"GIT_TERMINAL_PROMPT": "1", "SYNTHETIC": "keep"}
    seen = []

    def run(argv, timeout, environment, stdin=None):
        seen.append((argv, timeout, environment))
        return subprocess.CompletedProcess(argv, 0, "a" * 40 + "\trefs/tags/v1.2.3\n", "")
    monkeypatch.setattr("owned_process.run_owned", run)
    assert update_discovery.remote_tag_sha(
        "v1.2.3", checkout="/synthetic/checkout", environment=source) == "a" * 40
    argv, timeout, env = seen[0]
    assert argv == ["git", "-C", "/synthetic/checkout", "ls-remote", "origin",
                    "refs/tags/v1.2.3", "refs/tags/v1.2.3^{}"]
    assert timeout == 15 and env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["SYNTHETIC"] == "keep" and source["GIT_TERMINAL_PROMPT"] == "1"


@pytest.mark.parametrize("detached", [False, True])
def test_launch_environment_has_one_identity_policy_and_keeps_caller_mapping(detached):
    source = dict.fromkeys(update_launch.STRIPPED_ENV, "synthetic")
    source.update({"XPC_SERVICE_NAME": "synthetic-job", "MCS_JOB_PID": "1",
                   "MCS_UPDATE_RUN_LOCK_FD": "7", "SYNTHETIC": "keep"})
    result = update_launch.launch_environment(source, detached=detached)
    assert not any(key in result for key in update_launch.STRIPPED_ENV)
    assert ("MCS_JOB_PID" in result) is (not detached)
    assert ("XPC_SERVICE_NAME" in result) is (not detached)
    assert result["MCS_UPDATE_RUN_LOCK_FD"] == "7"
    assert result["SYNTHETIC"] == "keep" and source["HERMES_JOB_ID"] == "synthetic"


def test_readiness_preserves_wrapper_fallback_and_fresh_mode_observation(tmp_path, monkeypatch):
    wrapper = tmp_path / "mcs_update.sh"
    wrapper.write_text("#!/bin/sh\n")
    calls = []

    def config():
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("synthetic unreadable selection")
        return {"update": {"mode": "notify"}}
    monkeypatch.setattr(mcs_util, "load_config", config)
    assert update_launch.inspect_launch(root=tmp_path, hermes_wrapper=str(wrapper)) == (
        True, {"mode": "notify"})
    assert calls == [1, 1]


@pytest.mark.parametrize("failed", [False, True])
def test_detached_launch_passes_no_approval_and_closes_log(tmp_path, monkeypatch, failed):
    monkeypatch.setattr(mcs_util, "load_config", lambda: {"runtime_mode": "standalone"})
    captured = {}

    def spawn(argv, **kwargs):
        captured.update(argv=argv, **kwargs)
        assert not kwargs["stdout"].closed
        if failed:
            raise OSError("synthetic spawn failure")
    monkeypatch.setattr(update_launch.subprocess, "Popen", spawn)
    source = {"MCS_JOB_PID": "1", "XPC_SERVICE_NAME": "synthetic", "SYNTHETIC": "keep"}
    if failed:
        with pytest.raises(OSError, match="synthetic spawn failure"):
            update_launch.spawn_detached(root=tmp_path, data_dir=tmp_path / "data",
                                         hermes_wrapper="/synthetic/fallback", environment=source)
    else:
        update_launch.spawn_detached(root=tmp_path, data_dir=tmp_path / "data",
                                     hermes_wrapper="/synthetic/fallback", environment=source)
    assert captured["argv"] == [str(tmp_path / "scripts/mcs_update.sh")]
    assert captured["start_new_session"] is True and captured["close_fds"] is True
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["stderr"] is captured["stdout"] and captured["stdout"].closed
    assert captured["env"] == {"SYNTHETIC": "keep"}
    assert source["MCS_JOB_PID"] == "1"


def test_update_operations_never_import_full_updater_for_pins_or_readiness(tmp_path, monkeypatch):
    wrapper = tmp_path / "mcs_update.sh"
    wrapper.touch()
    monkeypatch.setattr(update_launch, "HERMES_WRAPPER", str(wrapper))
    monkeypatch.setattr(mcs_util, "load_config", lambda: {"update": {"mode": "notify"}})
    monkeypatch.setattr(update_discovery, "remote_tag_sha", lambda tag, **kw: "a" * 40)
    monkeypatch.setattr(update_discovery, "current_version", lambda **kw: ("v1.0.0", "b" * 40))
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        assert name != "mcs_update", "the updater CLI must not be loaded at receipt intake"
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded)
    req = mcs_operations.prepare_update_pins({"cmd": "ops.update_apply", "tag": "v1.2.3"})
    error, extra = mcs_operations._apply_update_op_tx(None, req, None)
    assert error is None and extra["scheduled"] is True
    assert extra["target_sha"] == "a" * 40 and extra["base_sha"] == "b" * 40


def test_drain_launches_after_committed_receipt_and_passes_no_command(tmp_path, monkeypatch):
    import sqlite3
    import uuid

    import job_ops
    from ledger import Ledger
    import mcs_requests

    data = tmp_path / "data"
    data.mkdir()
    path = data / "ledger.db"
    db = Ledger(str(path))
    command_id = str(uuid.uuid4())
    wrapper = tmp_path / "mcs_update.sh"
    wrapper.touch()
    monkeypatch.setattr(update_launch, "HERMES_WRAPPER", str(wrapper))
    monkeypatch.setattr(mcs_util, "load_config", lambda: {"update": {"mode": "notify"}})
    monkeypatch.setattr(update_discovery, "remote_tag_sha", lambda tag, **kw: "a" * 40)
    monkeypatch.setattr(update_discovery, "current_version", lambda **kw: ("v1.0.0", "b" * 40))
    cmd = data / "cmd"
    cmd.mkdir()
    mcs_requests.enqueue({"version": 1, "cmd": "ops.update_apply", "command_id": command_id,
                          "actor": "synthetic", "human_confirmed": True, "project_id": None,
                          "reason": "synthetic approval", "tag": "v1.2.3"}, str(cmd))
    seen = []

    def spawn(**paths):
        assert not db.db.in_transaction
        connection = sqlite3.connect(path)
        try:
            receipt = connection.execute(
                "SELECT outcome FROM command_receipts WHERE command_id=?", (command_id,)).fetchone()
        finally:
            connection.close()
        assert receipt == ("applied",)
        assert set(paths) == {"root", "data_dir", "hermes_wrapper", "environment"}
        seen.append(True)
    monkeypatch.setattr(update_launch, "spawn_detached", spawn)
    try:
        result = {"errors": []}
        job_ops.drain_commands(db, result, str(cmd))
        assert result["errors"] == [] and seen == [True]
    finally:
        db.close()


def test_detached_launch_log_is_private_and_never_follows_a_link(tmp_path, monkeypatch):
    import os
    monkeypatch.setattr(mcs_util, "load_config", lambda: {"runtime_mode": "standalone"})
    monkeypatch.setattr(update_launch.subprocess, "Popen", lambda argv, **kw: None)
    data = tmp_path / "data"
    kwargs = dict(root=tmp_path, data_dir=data, hermes_wrapper="/synthetic/fallback",
                  environment={})
    update_launch.spawn_detached(**kwargs)
    assert (os.stat(data / "update.log").st_mode & 0o777) == 0o600
    (data / "update.log").unlink()
    (data / "update.log").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(OSError):
        update_launch.spawn_detached(**kwargs)
    assert not (tmp_path / "elsewhere").exists()
