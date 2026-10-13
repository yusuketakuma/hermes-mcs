"""Unreadable runtime selection cannot start or stop either service family."""
import json
import os

import pytest

import mcs_update as updater
from ops_testkit import _load, bounded_python


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(updater, "DATA", str(data))
    for name in ("_run", "_bootstrap_agent", "_agent_pid", "_remove_marker",
                 "_standalone_drainer_problems"):
        monkeypatch.setattr(updater, name, lambda *a, **k: pytest.fail("runtime mutation or probe"))
    return tmp_path / "config.json"


@pytest.mark.parametrize("raw", ["{broken", "[]", "null", '{"runtime_mode":"other"}',
                                 '{"runtime_mode":null}', '{"runtime_mode":[]}'])
@pytest.mark.parametrize("bounce", [False, True])
def test_invalid_config_refuses_restart_before_service_calls(runtime, raw, bounce):
    runtime.write_text(raw)
    assert updater.restart_agents(bounce=bounce) == ["runtime_mode_unverifiable"]


@pytest.mark.parametrize("kind", ["symlink", "dangling", "directory", "fifo"])
def test_nonregular_config_refuses_restart(runtime, tmp_path, kind):
    if kind in ("symlink", "dangling"):
        target = tmp_path / "other.json"
        if kind == "symlink":
            target.write_text('{"runtime_mode":"hermes"}')
        runtime.symlink_to(target)
    elif kind == "directory":
        runtime.mkdir()
    else:
        os.mkfifo(runtime)
        bounded_python(tmp_path, """
import mcs_update as updater
updater.DATA = sys.argv[1]
def forbidden(*args, **kwargs):
    raise AssertionError('unknown runtime reached service calls')
updater._run = updater._bootstrap_agent = updater._agent_pid = forbidden
updater._remove_marker = updater._standalone_drainer_problems = forbidden
assert updater.restart_agents() == ['runtime_mode_unverifiable']
""", updater.DATA)
        return
    assert updater.restart_agents() == ["runtime_mode_unverifiable"]


def test_config_read_error_is_not_hermes(runtime, monkeypatch):
    def unreadable(path):
        raise PermissionError("synthetic denied")
    monkeypatch.setattr(updater, "_read_control_json", unreadable)
    assert updater.restart_agents() == ["runtime_mode_unverifiable"]


@pytest.mark.parametrize("cfg,expected", [(None, "hermes"), ({}, "hermes"),
                                         ({"runtime_mode": "hermes"}, "hermes"),
                                         ({"runtime_mode": "standalone"}, "standalone")])
def test_valid_and_absent_config_preserve_runtime_contract(
        runtime, monkeypatch, cfg, expected):
    if cfg is not None:
        runtime.write_text(json.dumps(cfg))
    recovery = _load()
    monkeypatch.setattr(recovery, "DATA", updater.DATA)
    assert updater._restart_runtime_mode() == recovery._runtime_mode() == expected
    removed = []
    monkeypatch.setattr(updater, "_remove_marker", lambda: removed.append(True))
    monkeypatch.setattr(updater, "RESIDENT_LABELS", ())
    monkeypatch.setattr(updater, "LEGACY_LABELS", ())
    monkeypatch.setattr(updater, "WATCHER_LABELS", ())
    if expected == "standalone":
        monkeypatch.setattr(updater, "_standalone_drainer_problems", lambda: [])
    assert updater.restart_agents() == []
    assert removed == [True]
