"""Synthetic configuration FD, concurrency and compare-before-publish boundaries."""
import json
import hashlib
import os
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

import cloud_cli
import cloud_policy
import config_edit
import extract_llm
import llm_availability
import mcs_config
import mcs_util
import provider_settings
import safe_io
import semantic_jev


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


def test_nested_budget_edit_rejects_a_changed_base(conf):
    base = config_edit.read_strict()
    changed = {**base, "cloud_llm": {"provider": "anthropic"}}
    conf.write_text(json.dumps(changed))
    with pytest.raises(config_edit.ConfigConflict):
        config_edit.apply({"cloud_llm.provider_budgets": {}}, expected=base)
    assert json.loads(conf.read_text()) == changed


@pytest.fixture
def split_conf(conf, monkeypatch):
    common = json.loads(conf.read_text())
    common.update(cloud_llm={"mode": "off", "project_ids": [], "provider": "auto",
                             "provider_order": ["anthropic", "openai"]},
                  semantic={"mode": "off"})
    conf.write_text(json.dumps(common))
    directory = conf.parent / "data" / "provider_settings"
    directory.mkdir(parents=True, mode=0o700)
    providers = {"anthropic": {"model": cloud_policy.PROVIDERS["anthropic"], "budget": {
        "daily_cost_limit_usd": 0, "monthly_cost_limit_usd": 0}},
        "openai": {"model": cloud_policy.PROVIDERS["openai"], "budget": {
            "daily_cost_limit_usd": 2, "monthly_cost_limit_usd": 3}},
        "jev": {"model": semantic_jev.JEV_MODEL, "daily_request_budget": 0, "until": None}}
    hashes = {}
    for name, value in providers.items():
        path = directory / (name + ".json")
        raw = json.dumps(value).encode()
        path.write_bytes(raw)
        path.chmod(0o600)
        hashes[name] = hashlib.sha256(raw).hexdigest()
    authority = directory.parent / ".provider-settings-authority.json"
    authority.write_text(json.dumps({"version": 2, "state": "committed", "files": hashes,
        "common_policy_sha256": provider_settings.common_policy_digest(common)}))
    authority.chmod(0o600)
    key = conf.parent / ".env"
    key.write_text("SYNTHETIC_KEY=synthetic-secret-canary\n")
    key.chmod(0o600)
    monkeypatch.setattr(cloud_policy, "provider_key", lambda name, root=None: "synthetic" if name == "openai" else None)
    return conf, common, providers, directory, key


def _split_state(conf):
    owners = {model: name for name, model in cloud_policy.PROVIDERS.items()}
    return provider_settings.read(conf.parent, model_owners=owners, jev_model=semantic_jev.JEV_MODEL)


def test_split_auto_edit_preserves_other_provider_jev_and_key(split_conf):
    conf, common, providers, _, key = split_conf
    before_key = key.read_bytes()
    expected = config_edit.read_strict()
    assert expected["cloud_llm"]["model"] == providers["openai"]["model"]
    budgets = {name: dict(providers[name]["budget"]) for name in ("anthropic", "openai")}
    budgets["openai"]["daily_cost_limit_usd"] = 1
    saved, errors = config_edit.apply({"cloud_llm.provider_budgets": budgets}, expected=expected)
    assert not errors and saved["cloud_llm"]["provider_budgets"] == budgets
    state = _split_state(conf)
    assert state["common"] == common
    assert state["providers"]["anthropic"] == providers["anthropic"]
    assert state["providers"]["openai"]["model"] == providers["openai"]["model"]
    assert state["providers"]["openai"]["budget"] == budgets["openai"]
    assert state["providers"]["jev"] == providers["jev"] and key.read_bytes() == before_key


def test_split_provider_switch_does_not_clear_pinned_models(split_conf):
    conf, _, providers, _, _ = split_conf
    before = config_edit.read_strict()
    saved, errors = config_edit.apply({"cloud_llm.provider": "anthropic", "cloud_llm.model": None},
                                      expected=before)
    assert not errors and saved["cloud_llm"]["provider"] == "anthropic"
    state = _split_state(conf)
    assert state["providers"] == providers


def test_split_visible_expected_conflict_and_pending_refuse_changes(split_conf):
    conf, _, providers, directory, _ = split_conf
    before = config_edit.read_strict()
    changed = dict(before)
    changed["cloud_llm"] = {**before["cloud_llm"], "mode": "shadow"}
    with pytest.raises(config_edit.ConfigConflict):
        config_edit.apply({"extract.prefilter": False}, expected=changed)
    assert _split_state(conf)["providers"] == providers
    marker = directory.parent / ".provider-settings-pending.json"
    marker.write_text('{}')
    marker.chmod(0o600)
    with pytest.raises(ValueError, match="provider_settings_pending"):
        config_edit.apply({"extract.prefilter": False}, expected=before)
    assert marker.exists()


def test_split_cas_refuses_provider_change_after_read(split_conf, monkeypatch):
    conf, _, _, directory, _ = split_conf
    original = provider_settings.commit_locked
    changed = directory / "openai.json"
    raw = changed.read_bytes() + b" "
    def concurrent(*args, **kwargs):
        changed.write_bytes(raw)
        return original(*args, **kwargs)
    monkeypatch.setattr(provider_settings, "commit_locked", concurrent)
    before = conf.read_bytes()
    with pytest.raises(ValueError):
        config_edit.apply({"extract.prefilter": False})
    assert changed.read_bytes() == raw and conf.read_bytes() == before


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


def test_budget_snapshot_refuses_config_path_change(conf, monkeypatch):
    alternate = conf.parent / "alternate.json"
    alternate.write_text(json.dumps({"mcs_login_id": "synthetic-other", "notify_target": "local"}))
    before = conf.read_bytes(), alternate.read_bytes()
    original = config_edit._snapshot
    calls = []

    def switch_after_first_read():
        result = original()
        calls.append(True)
        if len(calls) == 1:
            monkeypatch.setattr(mcs_util, "CONF_PATH", str(alternate))
        return result

    monkeypatch.setattr(config_edit, "_snapshot", switch_after_first_read)
    with pytest.raises(config_edit.ConfigConflict, match="config_changed"):
        cloud_cli._settings_for_edit()
    assert len(calls) == 1
    assert (conf.read_bytes(), alternate.read_bytes()) == before


@pytest.mark.parametrize('split', [False, True])
def test_editor_callbacks_report_verified_own_save(request, split):
    fixture = request.getfixturevalue('split_conf' if split else 'conf')
    conf = fixture[0] if split else fixture
    before = config_edit._snapshot(conf)
    events = []
    def admission(path, raw, stamp):
        assert path == str(conf) and (raw, stamp) == before
        with pytest.raises(config_edit.ConfigConflict, match='config_busy'):
            with config_edit._edit_lock(conf):
                pass
        events.append('admitted')
    def on_saved(path, raw, stamp):
        assert path == str(conf) and (raw, stamp) == config_edit._snapshot(conf)
        assert json.loads(raw)['extract']['prefilter'] is False
        events.append('saved')
    result, errors = config_edit.apply({'extract.prefilter': False}, admission=admission, on_saved=on_saved)
    assert not errors and result['extract']['prefilter'] is False
    assert events == ['admitted', 'saved']


@pytest.mark.parametrize('split', [False, True])
def test_editor_admission_rejection_and_conflict_never_notify(request, split):
    fixture = request.getfixturevalue('split_conf' if split else 'conf')
    conf = fixture[0] if split else fixture
    before = conf.read_bytes()
    events = []
    def reject(*args):
        raise config_edit.ConfigConflict('config_changed')
    with pytest.raises(config_edit.ConfigConflict):
        config_edit.apply({'extract.prefilter': False}, admission=reject, on_saved=lambda *a: events.append(a))
    assert conf.read_bytes() == before and not events
    def change(*args):
        replacement = conf.with_name('synthetic-concurrent')
        replacement.write_bytes(before)
        replacement.replace(conf)
    with pytest.raises((config_edit.ConfigConflict, ValueError)):
        config_edit.apply({'extract.prefilter': False}, admission=change, on_saved=lambda *a: events.append(a))
    assert conf.read_bytes() == before and not events


def test_legacy_editor_does_not_report_other_same_bytes_inode(conf, monkeypatch):
    atomic = safe_io.atomic_write
    events = []
    def concurrent(path, writer, *args, **kwargs):
        atomic(path, writer, *args, **kwargs)
        other = conf.with_name('synthetic-concurrent')
        other.write_bytes(conf.read_bytes())
        other.replace(conf)
    monkeypatch.setattr(safe_io, 'atomic_write', concurrent)
    with pytest.raises(ValueError, match='config_save_unverified'):
        config_edit.apply({'extract.prefilter': False}, on_saved=lambda *a: events.append(a))
    assert not events


def test_split_editor_does_not_report_other_same_bytes_inode(split_conf, monkeypatch):
    conf, _, _, _, _ = split_conf
    commit = provider_settings.commit_locked
    events = []
    def concurrent(*args, **kwargs):
        result = commit(*args, **kwargs)
        other = conf.with_name('synthetic-concurrent')
        other.write_bytes(conf.read_bytes())
        other.replace(conf)
        return result
    monkeypatch.setattr(provider_settings, 'commit_locked', concurrent)
    with pytest.raises(ValueError, match='config_save_unverified'):
        config_edit.apply({'extract.prefilter': False}, on_saved=lambda *a: events.append(a))
    assert not events
