"""Independent recovery rejects unsafe local state without reading special files."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from ops_testkit import _load


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    module = _load()
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(module.mcs_recovery_core, "DATA", str(tmp_path / "data"))
    for name, filename in (("STATE_PATH", "update_state.json"),
                           ("REPORT_PATH", "recovery_report.json")):
        monkeypatch.setattr(module.mcs_recovery_core, name, str(tmp_path / "data" / filename))
    return module


def test_absent_config_keeps_hermes_default(recovery):
    assert recovery.mcs_recovery_core._runtime_config() == {}
    assert recovery.mcs_recovery_core._runtime_mode() == "hermes"


@pytest.mark.parametrize("present", [False, True])
def test_symlinked_config_never_selects_a_runtime_or_sender(recovery, tmp_path, monkeypatch, present):
    target = tmp_path / "other.json"
    if present:
        target.write_text('{"runtime_mode":"standalone"}')
    (tmp_path / "config.json").symlink_to(target)
    assert recovery.mcs_recovery_core._runtime_config() == {"runtime_mode": "invalid"}
    monkeypatch.setattr(recovery.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("invalid config started a sender"))
    recovery.mcs_recovery_services._notify("synthetic")
    monkeypatch.setattr(recovery.mcs_recovery_services, "_launchctl",
                        lambda *a, **k: pytest.fail("invalid config restarted Hermes drainers"))
    assert recovery.mcs_recovery_services._restart_drainers() == ["runtime_mode_unverifiable"]


@pytest.mark.parametrize("method,relative,expected", [
    ("_runtime_config", "config.json", {"runtime_mode": "invalid"}),
    ("_standalone_status", "data/standalone-status.json", None),
    ("_load_state", "data/update_state.json", {"_corrupt": True}),
    ("_last_report", "data/recovery_report.json", {}),
    ("_awaiting_consent", "data/restore_pending.json", {"unreadable": True}),
])
def test_fifo_state_is_rejected_without_waiting_for_a_writer(
        recovery, tmp_path, method, relative, expected):
    path = tmp_path / relative
    os.mkfifo(path, 0o600)
    ready = tmp_path / "reader-started"
    # The pre-fix implementation blocks in open(); subprocess.run kills and
    # reaps only this fully synthetic child when the bounded check fails.
    program = """
import importlib.util, json, pathlib, sys
spec = importlib.util.spec_from_file_location('synthetic_recovery', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.mcs_recovery_core.DATA = sys.argv[2]
module.mcs_recovery_core.STATE_PATH = str(pathlib.Path(module.mcs_recovery_core.DATA, 'update_state.json'))
module.mcs_recovery_core.REPORT_PATH = str(pathlib.Path(module.mcs_recovery_core.DATA, 'recovery_report.json'))
pathlib.Path(sys.argv[4]).write_text('ready')
print(json.dumps(getattr(module, sys.argv[3])()))
"""
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-c", program, recovery.__file__, recovery.mcs_recovery_core.DATA,
             method, str(ready)], capture_output=True, text=True, timeout=3)
    except subprocess.TimeoutExpired:
        assert ready.is_file(), "synthetic child never reached the state reader"
        pytest.fail("recovery reader blocked on a FIFO")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == expected


@pytest.mark.parametrize("contents", ["[]", "{broken", "x" * 262145])
def test_bad_config_remains_invalid(recovery, tmp_path, contents):
    (tmp_path / "config.json").write_text(contents)
    assert recovery.mcs_recovery_core._runtime_config() == {"runtime_mode": "invalid"}


def test_private_regular_status_keeps_liveness_and_mode_contract(recovery, tmp_path, monkeypatch):
    status = {"pid": 4242, "updated_at": time.time(), "children": {}, "generation": "a" * 32}
    path = tmp_path / "data/standalone-status.json"
    path.write_text(json.dumps(status))
    path.chmod(0o600)
    calls = []
    monkeypatch.setattr(recovery.os, "kill", lambda pid, sig: calls.append((pid, sig)))
    assert recovery.mcs_recovery_services._standalone_status() == status
    assert calls == [(4242, 0)]
    path.chmod(0o644)
    assert recovery.mcs_recovery_services._standalone_status() is None
    assert calls == [(4242, 0)]


def test_config_read_is_bounded_even_if_file_grows_after_stat(recovery, tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text('{"runtime_mode":"standalone","padding":"' + "x" * 262144 + '"}')
    original = recovery.os.fstat

    def before_growth(fd):
        metadata = original(fd)
        return SimpleNamespace(st_mode=metadata.st_mode, st_size=1)

    monkeypatch.setattr(recovery.os, "fstat", before_growth)
    assert recovery.mcs_recovery_core._runtime_config() == {"runtime_mode": "invalid"}


@pytest.mark.parametrize("method,filename,expected", [
    ("_load_state", "update_state.json", {"_corrupt": True}),
    ("_last_report", "recovery_report.json", {}),
    ("_awaiting_consent", "restore_pending.json", {"unreadable": True}),
])
@pytest.mark.parametrize("present", [False, True])
def test_symlinked_journals_never_authorize_missing_or_restored_state(
        recovery, tmp_path, method, filename, expected, present):
    target = tmp_path / "other.json"
    if present:
        target.write_text('{"v":1,"phase":"restored","result":"synthetic"}')
    (tmp_path / "data" / filename).symlink_to(target)
    assert getattr(recovery, method)() == expected


def test_unbounded_journal_size_policy_and_missing_classification_are_preserved(recovery, tmp_path):
    assert recovery.mcs_recovery_core._load_state() == {"v": 1, "stages": [], "applied": [], "applying": None}
    assert recovery.mcs_recovery_core._awaiting_consent() is None
    state = {"v": 1, "stages": [], "padding": "x" * 300000}
    Path(recovery.mcs_recovery_core.STATE_PATH).write_text(json.dumps(state))
    assert recovery.mcs_recovery_core._load_state() == state


def test_unreadable_restore_marker_is_refused_before_database_access(recovery, tmp_path, monkeypatch):
    (tmp_path / "data/restore_pending.json").symlink_to(tmp_path / "missing")
    monkeypatch.setattr(recovery.mcs_recovery_database, "_db_version",
                        lambda *a: pytest.fail("unreadable marker reached database"))
    assert recovery.mcs_recovery_database._restore_db("synthetic-unused") == "restore_marker_unreadable"


def test_frozen_restart_keeps_independent_local_reader(recovery, tmp_path):
    namespace = {"__name__": "synthetic_frozen"}
    exec(compile(recovery.mcs_recovery_gateway._GATEWAY_RESTART_PROGRAM.rsplit("sys.exit(", 1)[0],
                 "<frozen>", "exec"), namespace)
    path = tmp_path / "frozen.json"
    path.write_text('{"phase":"restored"}')
    assert namespace["_read_local_json"](path) == {"phase": "restored"}
    link = tmp_path / "frozen-link.json"
    link.symlink_to(path)
    with pytest.raises(OSError):
        namespace["_read_local_json"](link)
