"""Synthetic configuration FD, concurrency and compare-before-publish boundaries."""
import json
import os
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

import cloud_cli
import config_edit
import extract_llm
import llm_availability
import mcs_config
import mcs_util
import safe_io


@pytest.fixture
def conf(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"mcs_login_id": "synthetic-login", "notify_target": "synthetic-target",
        "extension": {"secret": "SYNTHETIC_PRIVATE_MARKER"}}))
    monkeypatch.setattr(mcs_util, "CONF_PATH", str(path))
    for module in (mcs_util, extract_llm, llm_availability):
        monkeypatch.setattr(module, "load_config", lambda *a, **k: json.loads(path.read_text()))
    return path


def test_fifo_is_rejected_before_any_blocking_text_read(conf, monkeypatch):
    conf.unlink()
    os.mkfifo(conf)
    def forbidden(*args, **kwargs):
        raise AssertionError("unbounded text read")
    monkeypatch.setattr(Path, "read_text", forbidden)
    real_open = os.open
    def checked(name, flags, *a, **k):
        assert flags & os.O_NONBLOCK and flags & os.O_NOFOLLOW
        return real_open(name, flags, *a, **k)
    monkeypatch.setattr(safe_io.os, "open", checked)
    with pytest.raises(ValueError):
        config_edit.read_strict()


def test_symlink_is_rejected_without_rewriting_target(conf):
    target = conf.with_name("synthetic-target.json")
    raw = conf.read_bytes()
    conf.rename(target)
    conf.symlink_to(target)
    with pytest.raises(OSError):
        config_edit.apply({"extract.prefilter": False})
    assert target.read_bytes() == raw and conf.is_symlink()


def test_same_content_replacement_after_fd_read_is_rejected(conf, monkeypatch):
    reader = safe_io.read_regular_nofollow
    def swapped(*a, **k):
        result = reader(*a, **k)
        replacement = conf.with_name("replacement")
        replacement.write_bytes(result[0])
        replacement.replace(conf)
        return result
    monkeypatch.setattr(safe_io, "read_regular_nofollow", swapped)
    with pytest.raises(config_edit.ConfigConflict):
        config_edit.read_strict()


def test_explicit_config_path_has_an_independent_snapshot_and_lock(conf, tmp_path):
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other = other_dir / "config.json"
    other.write_bytes(b'{"extension": true}')
    raw, base = config_edit._snapshot(other)
    assert raw == other.read_bytes()
    with config_edit._edit_lock(other):
        with pytest.raises(config_edit.ConfigConflict):
            with config_edit._edit_lock(other):
                pass
        with config_edit._edit_lock():
            assert config_edit._snapshot(other)[1] == base
    other.write_bytes(b'{"extension": false}')
    assert config_edit._snapshot(other)[1] != base
    assert json.loads(conf.read_bytes())["extension"] == {"secret": "SYNTHETIC_PRIVATE_MARKER"}


@pytest.mark.parametrize("kind", ["oversize", "growth"])
def test_size_limit_is_checked_before_and_after_read(conf, monkeypatch, kind):
    monkeypatch.setattr(mcs_util, "CONFIG_MAX_BYTES", 8)
    if kind == "oversize":
        conf.write_bytes(b" " * 9)
    else:
        conf.write_bytes(b"{}")
        fstat = safe_io.os.fstat
        def grew(fd):
            info = fstat(fd)
            conf.write_bytes(b"{}" + b" " * 9)
            return info
        monkeypatch.setattr(safe_io.os, "fstat", grew)
    with pytest.raises(ValueError):
        config_edit.read_strict()


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nonstandard_nonfinite_json_is_never_rewritten(conf, constant):
    raw = ('{"extension": {"number": ' + constant + '}}').encode()
    conf.write_bytes(raw)
    with pytest.raises(ValueError):
        config_edit.apply({"extract.prefilter": False})
    assert conf.read_bytes() == raw


@pytest.mark.parametrize("replacement", [False, True])
def test_external_base_change_during_validation_is_not_overwritten(conf, monkeypatch, replacement):
    validate = mcs_config.validate_config
    external = {**json.loads(conf.read_text()), "extension": {"external": True}}
    raw = json.dumps(external).encode()
    def edited(cfg):
        if replacement:
            other = conf.with_name("replacement")
            other.write_bytes(raw)
            other.replace(conf)
        else:
            conf.write_bytes(raw)
        return validate(cfg)
    monkeypatch.setattr(mcs_config, "validate_config", edited)
    with pytest.raises(config_edit.ConfigConflict):
        config_edit.apply({"extract.prefilter": False})
    assert conf.read_bytes() == raw
    assert not list(conf.parent.glob(".svc.*"))


def test_oversize_serialized_update_is_not_published(conf, monkeypatch):
    before = conf.read_bytes()
    monkeypatch.setattr(mcs_util, "CONFIG_MAX_BYTES", len(before) + 1)
    with pytest.raises(ValueError):
        config_edit.apply({"extract.prefilter": False})
    assert conf.read_bytes() == before


def test_concurrent_settings_and_budget_conflict_then_retry_preserves_both(conf, monkeypatch, capsys):
    read_started, release = Event(), Event()
    snapshot = config_edit._snapshot
    first = []
    result = []
    errors = []
    def pause_first():
        value = snapshot()
        if not first:
            first.append(True)
            read_started.set()
            assert release.wait(3), "test did not release the paused edit"
        return value
    monkeypatch.setattr(config_edit, "_snapshot", pause_first)
    def settings():
        try:
            result.append(config_edit.settings_command(SimpleNamespace(prefilter="off", admission=None,
                                                                       disk_guard_mb=None)))
        except BaseException as error:
            errors.append(error)
    worker = Thread(target=settings)
    worker.start()
    try:
        assert read_started.wait(3)
        assert cloud_cli.budget_command(SimpleNamespace(daily_usd="1", monthly_usd=None)) == 1
    finally:
        release.set()
        worker.join(4)
    assert not worker.is_alive() and not errors and result == [0]
    assert cloud_cli.budget_command(SimpleNamespace(daily_usd="1", monthly_usd=None)) == 0
    cfg = json.loads(conf.read_text())
    assert cfg["extract"]["prefilter"] is False
    assert cfg["cloud_llm"]["daily_cost_limit_usd"] == 1
    assert cfg["extension"] == {"secret": "SYNTHETIC_PRIVATE_MARKER"}
    assert "SYNTHETIC_PRIVATE_MARKER" not in capsys.readouterr().out
    assert conf.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("kind", ["symlink", "fifo", "nonempty", "loose"])
def test_invalid_edit_lock_is_not_used_or_removed(conf, kind):
    path = Path(str(conf) + ".edit.lock")
    if kind == "symlink":
        path.symlink_to(conf)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    else:
        path.write_bytes(b"synthetic" if kind == "nonempty" else b"")
        path.chmod(0o644 if kind == "loose" else 0o600)
    before = conf.read_bytes()
    with pytest.raises((OSError, ValueError)):
        config_edit.apply({"extract.prefilter": False})
    assert conf.read_bytes() == before and path.exists()


@pytest.mark.parametrize("command", ["settings", "budget"])
@pytest.mark.parametrize("raw", ['{"mcs_login_id": "a", "mcs_login_id": "b"}', '{"local_llm": {"disk_guard_mb": NaN}}'])
def test_unreadable_config_is_not_reported_as_missing(conf, capsys, command, raw):
    def run():
        if command == "settings":
            return config_edit.settings_command(SimpleNamespace(prefilter="off", admission=None, disk_guard_mb=None))
        return cloud_cli.budget_command(SimpleNamespace(daily_usd="1", monthly_usd=None))
    conf.write_text(raw)
    before = conf.read_bytes()
    assert run() == 1
    out = capsys.readouterr().out
    assert "読み取れない" in out and "check" in out and "init" not in out
    assert conf.read_bytes() == before
    conf.unlink()
    assert run() == 1
    assert "init" in capsys.readouterr().out
