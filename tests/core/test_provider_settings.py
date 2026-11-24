"""Synthetic provider configuration boundaries and read-only snapshots."""
import fcntl
import hashlib
import json
import os
from types import SimpleNamespace

import pytest

import cloud_policy
import mcs_config
import mcs_util
import provider_settings as settings
import safe_io


OWNERS = {"synthetic-anthropic": "anthropic", "synthetic-openai": "openai"}


def private_json(path, value):
    raw = (json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    return hashlib.sha256(raw).hexdigest()


def tree(tmp_path, *, split=False):
    tmp_path.chmod(0o700)
    common = {"cloud_llm": {"mode": "off", "project_ids": []}, "semantic": {"mode": "off"}}
    private_json(tmp_path / "config.json", common)
    hashes = {}
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    directory = data / "provider_settings"
    if split:
        directory.mkdir(mode=0o700)
        for name in settings.PROVIDERS:
            hashes[name] = private_json(directory / (name + ".json"), {})
        private_json(data / ".provider-settings-authority.json",
                     {"version": 2, "state": "committed", "files": hashes,
                      "common_policy_sha256": settings.common_policy_digest(common)})
    return common, data, directory


@pytest.mark.parametrize("relative", [False, True])
@pytest.mark.parametrize("split", [False, True])
def test_private_root_link_reads_and_commits_same_target(tmp_path, monkeypatch, relative, split):
    tmp_path.chmod(0o700)
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    tree(target, split=split)
    link = tmp_path / "root-link"
    link.symlink_to(target.name if relative else target, target_is_directory=True)
    before = settings.read(link, model_owners=OWNERS)
    assert before["mode"] == ("split" if split else "legacy_unverified")
    assert before["snapshot"]["root_link"]["text"] == os.readlink(link)
    assert settings.probe(link)["config_present"] is True
    after = _commit(link, monkeypatch, before)
    assert after["mode"] == "split"
    assert after["snapshot"]["root_link"] == before["snapshot"]["root_link"]
    assert not (target / "data" / ".provider-settings-pending.json").exists()


def test_openai_profiles_roundtrip_in_private_settings_only():
    owners = {'gpt-6-luna': 'openai', 'claude-haiku-5-5': 'anthropic'}
    raw = {'cloud_llm': {'provider': 'openai', 'mode': 'off', 'profiles': {
        'backlog': 'luna-medium', 'patient_context': 'sol-medium', 'quality_repair': 'sol-high'}}}
    common, providers = settings.split_legacy(raw, owners)
    assert 'profiles' not in common['cloud_llm']
    assert providers['openai']['profiles'] == raw['cloud_llm']['profiles']
    assert settings.compose(common, providers, selected_provider='openai', model_owners=owners) == raw
    anthropic = settings.compose(common, providers, selected_provider='anthropic', model_owners=owners)
    assert 'profiles' not in anthropic['cloud_llm']


@pytest.mark.parametrize('profiles', [None, [], {'backlog': 'unknown'}, {'unknown': 'luna-medium'},
                                      {'backlog': None}, {'backlog': True}])
def test_openai_profiles_refuse_invalid_values(profiles):
    with pytest.raises(ValueError, match='provider_profiles_invalid'):
        settings.split_legacy({'cloud_llm': {'profiles': profiles}}, OWNERS)


def test_root_link_replacement_with_same_target_rejects_stale_snapshot(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    tree(target, split=True)
    link = tmp_path / "root-link"
    link.symlink_to(target.name, target_is_directory=True)
    before = settings.read(link, model_owners=OWNERS)
    replacement = tmp_path / "replacement-link"
    replacement.symlink_to(target.name, target_is_directory=True)
    os.replace(replacement, link)
    with pytest.raises(ValueError, match="provider_snapshot_changed"):
        _commit(link, monkeypatch, before)
    assert not (target / "data" / ".provider-settings-pending.json").exists()


def test_root_link_swap_during_read_is_rejected(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    tree(target, split=True)
    link = tmp_path / "root-link"
    link.symlink_to(target.name, target_is_directory=True)
    read_file = settings._read_file
    swapped = []
    def swap(parent, name, **kwargs):
        value = read_file(parent, name, **kwargs)
        if name == "config.json" and not swapped:
            replacement = tmp_path / "replacement-link"
            replacement.symlink_to(target.name, target_is_directory=True)
            os.replace(replacement, link)
            swapped.append(True)
        return value
    monkeypatch.setattr(settings, "_read_file", swap)
    with pytest.raises(ValueError, match="provider_snapshot_changed"):
        settings.read(link, model_owners=OWNERS)
    assert swapped


def test_root_link_swap_during_commit_keeps_pending_and_never_writes_new_target(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    first, second = tmp_path / "first", tmp_path / "second"
    for root in (first, second):
        root.mkdir(mode=0o700)
        tree(root, split=True)
    link = tmp_path / "root-link"
    link.symlink_to(first.name, target_is_directory=True)
    before = settings.read(link, model_owners=OWNERS)
    second_config = (second / "config.json").read_bytes()
    second_authority = (second / "data" / ".provider-settings-authority.json").read_bytes()
    replace = settings._replace
    swapped = []
    def switch(*args, **kwargs):
        result = replace(*args, **kwargs)
        if not swapped:
            replacement = tmp_path / "replacement-link"
            replacement.symlink_to(second.name, target_is_directory=True)
            os.replace(replacement, link)
            swapped.append(True)
        return result
    monkeypatch.setattr(settings, "_replace", switch)
    with pytest.raises(ValueError, match="provider_snapshot_changed"):
        _commit(link, monkeypatch, before)
    assert swapped and (first / "data" / ".provider-settings-pending.json").exists()
    assert (second / "config.json").read_bytes() == second_config
    assert (second / "data" / ".provider-settings-authority.json").read_bytes() == second_authority
    assert not (second / "data" / ".provider-settings-pending.json").exists()


def test_root_link_swap_during_editor_lock_does_not_create_lock_in_new_target(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    first, second = tmp_path / "first", tmp_path / "second"
    for root in (first, second):
        root.mkdir(mode=0o700)
        tree(root, split=True)
    link = tmp_path / "root-link"
    link.symlink_to(first.name, target_is_directory=True)
    before = settings.read(link, model_owners=OWNERS)
    real_open = os.open
    swapped = []
    def switch(path, flags, *args, **kwargs):
        if path == "config.json.edit.lock" and kwargs.get("dir_fd") is not None and not swapped:
            replacement = tmp_path / "replacement-link"
            replacement.symlink_to(second.name, target_is_directory=True)
            os.replace(replacement, link)
            swapped.append(True)
        return real_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(settings.os, "open", switch)
    with pytest.raises(ValueError, match="provider_snapshot_changed"):
        _commit(link, monkeypatch, before)
    assert swapped
    assert not (second / "config.json.edit.lock").exists()
    assert not (first / "data" / ".provider-settings-pending.json").exists()
    assert not (second / "data" / ".provider-settings-pending.json").exists()


def _churn_during_authority_read(settings_module, data, name):
    read_file = settings_module._read_file
    churned = []

    def churn(parent, item, **kwargs):
        result = read_file(parent, item, **kwargs)
        if item == ".provider-settings-authority.json" and not churned:
            churned.append(True)
            (data / name).write_bytes(b"synthetic")
        return result

    return churn, churned


def test_runtime_churn_in_data_dir_does_not_invalidate_read_or_probe(tmp_path, monkeypatch):
    """Runtime files churn data/'s directory times constantly; only the
    provider namespace may invalidate a snapshot."""
    tmp_path.chmod(0o700)
    common, data, _ = tree(tmp_path, split=True)
    churn, churned = _churn_during_authority_read(settings, data, "synthetic-runtime-journal")
    monkeypatch.setattr(settings, "_read_file", churn)
    state = settings.read(tmp_path, model_owners=OWNERS)
    assert state["mode"] == "split" and state["common"] == common
    assert churned
    churn, churned = _churn_during_authority_read(settings, data, "synthetic-runtime-spool")
    monkeypatch.setattr(settings, "_read_file", churn)
    assert settings.probe(tmp_path)["state"] == "requires_checked_read"
    assert churned


def test_provider_namespace_change_during_read_still_fails_closed(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    tree(tmp_path, split=True)
    data = tmp_path / "data"
    churn, churned = _churn_during_authority_read(settings, data, ".provider-settings-backup-synthetic")
    monkeypatch.setattr(settings, "_read_file", churn)
    with pytest.raises(ValueError, match="provider_settings_changed"):
        settings.read(tmp_path, model_owners=OWNERS)
    assert churned


def test_provider_namespace_change_during_probe_still_fails_closed(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    tree(tmp_path, split=True)
    data = tmp_path / "data"
    churn, churned = _churn_during_authority_read(settings, data, ".provider-settings-stage-synthetic")
    monkeypatch.setattr(settings, "_read_file", churn)
    with pytest.raises(ValueError, match="provider_settings_changed"):
        settings.probe(tmp_path)
    assert churned


def test_root_link_rejects_dangling_and_untrusted_target(tmp_path):
    tmp_path.chmod(0o700)
    target = tmp_path / "target"
    link = tmp_path / "root-link"
    link.symlink_to(target.name, target_is_directory=True)
    with pytest.raises(ValueError):
        settings.probe(link)
    target.mkdir(mode=0o777)
    target.chmod(0o777)
    with pytest.raises(ValueError, match="provider_directory_invalid"):
        settings.probe(link)


@pytest.mark.parametrize("budgets", [None, {}, {"openai": {"daily_cost_limit_usd": 0, "monthly_cost_limit_usd": 0}}])
def test_explicit_values_round_trip_without_defaults(budgets):
    cloud = {"mode": "off", "project_ids": [], "model": "synthetic-openai"}
    if budgets is not None:
        cloud["provider_budgets"] = budgets
    raw = {"cloud_llm": cloud, "semantic": {"mode": "off", "daily_request_budget": 0,
                                               "until": None, "attempt_timeout_seconds": 20.0}}
    common, providers = settings.split_legacy(raw, OWNERS)
    assert settings.compose(common, providers, selected_provider="openai", model_owners=OWNERS) == raw
    assert raw["semantic"]["until"] is None
    assert "daily_request_budget" not in common["semantic"]


def test_split_missing_values_do_not_become_defaults():
    raw = {"semantic": {"mode": "off"}, "cloud_llm": {"project_ids": None}}
    common, providers = settings.split_legacy(raw, OWNERS)
    assert settings.compose(common, providers, selected_provider="anthropic", model_owners=OWNERS) == raw
    assert all(not doc for doc in providers.values())


def _fresh_root(tmp_path, common):
    tmp_path.chmod(0o700)
    (tmp_path / "data").mkdir(mode=0o700)
    private_json(tmp_path / "config.json", common)


@pytest.mark.parametrize("common", [
    {}, {"runtime_mode": "hermes"}, {"runtime_mode": "standalone", "install_args": []},
    {"recovery_python": "/synthetic-recovery/python", "install_args": []},
    {"unknown": "synthetic"},
    {"runtime_mode": "hermes", "future_synthetic": {"nested": [None, False, 0, "例"]}},
])
def test_fresh_bootstrap_keeps_common_exact_and_provider_files_empty(tmp_path, common):
    import config_edit

    _fresh_root(tmp_path, common)
    with config_edit._edit_lock(tmp_path / "config.json") as fd:
        before = settings.read(tmp_path, model_owners=OWNERS)
        result = settings.bootstrap_locked(tmp_path, before["snapshot"], common, model_owners=OWNERS, lock_fd=fd)
        assert os.fstat(fd).st_ino == (tmp_path / "config.json.edit.lock").stat().st_ino
    assert result["mode"] == "split" and result["common"] == common
    assert result["configuration_state"] == "bootstrap"
    assert result["providers"] == {name: {} for name in settings.PROVIDERS}
    assert json.loads((tmp_path / "config.json").read_text()) == common
    assert not (tmp_path / "data" / ".provider-settings-pending.json").exists()


def test_bootstrap_runtime_edit_preserves_unknown_values_and_state(tmp_path):
    import config_edit

    common = {"runtime_mode": "hermes", "install_args": [], "future_synthetic": {"value": [0, None]}}
    _fresh_root(tmp_path, common)
    with config_edit._edit_lock(tmp_path / "config.json") as fd:
        legacy = settings.read(tmp_path, model_owners=OWNERS)
        initial = settings.bootstrap_locked(tmp_path, legacy["snapshot"], common, model_owners=OWNERS, lock_fd=fd)
        first = settings.bootstrap_runtime_locked(tmp_path, initial["snapshot"], {"runtime_mode": "standalone"},
                                                  model_owners=OWNERS, lock_fd=fd)
        second = settings.bootstrap_runtime_locked(tmp_path, first["snapshot"],
                                                   {"recovery_python": "/synthetic/python"},
                                                   model_owners=OWNERS, lock_fd=fd)
        assert os.fstat(fd).st_ino == (tmp_path / "config.json.edit.lock").stat().st_ino
    assert second["common"] == {**common, "runtime_mode": "standalone", "recovery_python": "/synthetic/python"}
    assert second["configuration_state"] == "bootstrap"
    assert second["providers"] == {name: {} for name in settings.PROVIDERS}
    authority = json.loads((tmp_path / "data" / ".provider-settings-authority.json").read_text())
    assert authority["configuration_state"] == "bootstrap"
    assert "configuration_state" not in second["common"]
    assert not (tmp_path / "data" / ".provider-settings-pending.json").exists()
    records = settings.change_records(tmp_path, 5)["records"]
    assert [(record["origin"], record["changed"]) for record in records] == [
        ("install_runtime", []), ("install_runtime", []), ("install_bootstrap", [])]


def test_bootstrap_runtime_edit_accepts_snapshot_before_new_editor_lock(tmp_path):
    import config_edit

    _fresh_root(tmp_path, {})
    with config_edit._edit_lock(tmp_path / "config.json") as fd:
        legacy = settings.read(tmp_path, model_owners=OWNERS)
        settings.bootstrap_locked(tmp_path, legacy["snapshot"], {}, model_owners=OWNERS, lock_fd=fd)
    (tmp_path / "config.json.edit.lock").unlink()
    before = settings.read(tmp_path, model_owners=OWNERS)
    with config_edit._edit_lock(tmp_path / "config.json") as fd:
        result = settings.bootstrap_runtime_locked(tmp_path, before["snapshot"], {"runtime_mode": "hermes"},
                                                    model_owners=OWNERS, lock_fd=fd)
    assert result["common"] == {"runtime_mode": "hermes"}


def test_bootstrap_runtime_edit_preserves_nonmissing_validation_errors(tmp_path, monkeypatch):
    import config_edit

    _fresh_root(tmp_path, {})
    with config_edit._edit_lock(tmp_path / "config.json") as fd:
        legacy = settings.read(tmp_path, model_owners=OWNERS)
        initial = settings.bootstrap_locked(tmp_path, legacy["snapshot"], {}, model_owners=OWNERS, lock_fd=fd)
        authority = (tmp_path / "data" / ".provider-settings-authority.json").read_bytes()
        original_validate = mcs_config.validate_config
        def invalid(cfg):
            errors, warnings = original_validate(cfg)
            return errors + ["synthetic-validation-error"], warnings
        monkeypatch.setattr(mcs_config, "validate_config", invalid)
        with pytest.raises(ValueError, match="provider_config_invalid"):
            settings.bootstrap_runtime_locked(tmp_path, initial["snapshot"], {"runtime_mode": "hermes"},
                                              model_owners=OWNERS, lock_fd=fd)
        assert (tmp_path / "data" / ".provider-settings-authority.json").read_bytes() == authority
        assert not (tmp_path / "data" / ".provider-settings-pending.json").exists()


@pytest.mark.parametrize("name", [".provider-bootstrap-fresh.json", ".provider-bootstrap-complete.json"])
@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_bootstrap_runtime_edit_rejects_unfinished_completion_marker(tmp_path, name, kind):
    import config_edit

    _fresh_root(tmp_path, {})
    with config_edit._edit_lock(tmp_path / "config.json") as fd:
        legacy = settings.read(tmp_path, model_owners=OWNERS)
        settings.bootstrap_locked(tmp_path, legacy["snapshot"], {}, model_owners=OWNERS, lock_fd=fd)
        marker = tmp_path / "data" / name
        if kind == "file":
            private_json(marker, {"synthetic": True})
        else:
            marker.symlink_to("missing-synthetic")
        initial = settings.read(tmp_path, model_owners=OWNERS)
        with pytest.raises(ValueError):
            settings.bootstrap_runtime_locked(tmp_path, initial["snapshot"], {"runtime_mode": "hermes"},
                                              model_owners=OWNERS, lock_fd=fd)
        assert os.path.lexists(marker)
        assert not (tmp_path / "data" / ".provider-settings-pending.json").exists()


@pytest.mark.parametrize("changes", [
    {"runtime_mode": "invalid"}, {"recovery_python": None}, {"recovery_python": "relative"},
    {"install_args": []}, {"future_synthetic": "changed"}, {"semantic": {}},
])
def test_bootstrap_runtime_edit_rejects_invalid_or_other_changes(tmp_path, changes):
    import config_edit

    _fresh_root(tmp_path, {})
    with config_edit._edit_lock(tmp_path / "config.json") as fd:
        legacy = settings.read(tmp_path, model_owners=OWNERS)
        initial = settings.bootstrap_locked(tmp_path, legacy["snapshot"], {}, model_owners=OWNERS, lock_fd=fd)
        authority = (tmp_path / "data" / ".provider-settings-authority.json").read_bytes()
        with pytest.raises(ValueError):
            settings.bootstrap_runtime_locked(tmp_path, initial["snapshot"], changes, model_owners=OWNERS, lock_fd=fd)
        assert (tmp_path / "data" / ".provider-settings-authority.json").read_bytes() == authority
        assert not (tmp_path / "data" / ".provider-settings-pending.json").exists()


@pytest.mark.parametrize("condition", ["ordinary", "known_common", "nonempty_provider", "stale", "pending"])
def test_bootstrap_runtime_edit_requires_unconfigured_authority_and_snapshot(tmp_path, condition):
    import config_edit

    if condition in ("ordinary", "known_common", "nonempty_provider"):
        tree(tmp_path, split=True)
        authority_path = tmp_path / "data" / ".provider-settings-authority.json"
        authority = json.loads(authority_path.read_text())
        if condition != "ordinary":
            authority["configuration_state"] = "bootstrap"
        if condition == "nonempty_provider":
            common = {}
            private_json(tmp_path / "config.json", common)
            authority["common_policy_sha256"] = settings.common_policy_digest(common)
            authority["files"]["anthropic"] = private_json(
                tmp_path / "data" / "provider_settings" / "anthropic.json", {"model": "synthetic-anthropic"})
        private_json(authority_path, authority)
    else:
        _fresh_root(tmp_path, {})
    with config_edit._edit_lock(tmp_path / "config.json") as fd:
        if condition in ("stale", "pending"):
            legacy = settings.read(tmp_path, model_owners=OWNERS)
            initial = settings.bootstrap_locked(tmp_path, legacy["snapshot"], {}, model_owners=OWNERS, lock_fd=fd)
        else:
            initial = settings.read(tmp_path, model_owners=OWNERS)
        if condition == "stale":
            settings.bootstrap_runtime_locked(tmp_path, initial["snapshot"], {"runtime_mode": "hermes"},
                                              model_owners=OWNERS, lock_fd=fd)
        elif condition == "pending":
            private_json(tmp_path / "data" / ".provider-settings-pending.json", {"synthetic": True})
        with pytest.raises(ValueError):
            settings.bootstrap_runtime_locked(tmp_path, initial["snapshot"], {"runtime_mode": "standalone"},
                                              model_owners=OWNERS, lock_fd=fd)
        assert (tmp_path / "data" / ".provider-settings-pending.json").exists() is (condition == "pending")


@pytest.mark.parametrize("value", [None, False, 0, "unknown", {}, []])
def test_authority_rejects_unknown_configuration_states(tmp_path, value):
    tree(tmp_path, split=True)
    path = tmp_path / "data" / ".provider-settings-authority.json"
    authority = json.loads(path.read_text())
    authority["configuration_state"] = value
    private_json(path, authority)
    with pytest.raises(ValueError, match="provider_authority_invalid"):
        settings.read(tmp_path, model_owners=OWNERS)


def test_normal_full_configuration_commit_removes_bootstrap_state(tmp_path):
    import config_edit

    _fresh_root(tmp_path, {})
    with config_edit._edit_lock(tmp_path / "config.json") as fd:
        legacy = settings.read(tmp_path, model_owners=OWNERS)
        initial = settings.bootstrap_locked(tmp_path, legacy["snapshot"], {}, model_owners=OWNERS, lock_fd=fd)
        configured = {"mcs_login_id": "synthetic", "notify_target": "local"}
        result = settings.commit_locked(tmp_path, initial["snapshot"], configured, initial["providers"],
                                        selected_provider="anthropic", model_owners=OWNERS, lock_fd=fd)
        assert result["common"] == configured and "configuration_state" not in result
        authority = json.loads((tmp_path / "data" / ".provider-settings-authority.json").read_text())
        assert "configuration_state" not in authority
        with pytest.raises(ValueError, match="provider_bootstrap_unverified"):
            settings.bootstrap_runtime_locked(tmp_path, result["snapshot"], {"runtime_mode": "hermes"},
                                              model_owners=OWNERS, lock_fd=fd)


@pytest.mark.parametrize("common", [
    {"runtime_mode": "invalid"}, {"runtime_mode": None}, {"install_args": "invalid"},
    {"recovery_python": "relative/python"}, {"mcs_login_id": "synthetic"},
    {"cloud_llm": {"mode": "on"}}, {"semantic": {"mode": "off"}},
    {"notify": {}}, {"notify_target": "synthetic"}, {"drug_map": {}}, {"sdc": {}},
])
def test_bootstrap_rejects_invalid_or_nonfresh_common_without_publication(tmp_path, common):
    import config_edit

    _fresh_root(tmp_path, common)
    original = (tmp_path / "config.json").read_bytes()
    with config_edit._edit_lock(tmp_path / "config.json") as fd:
        before = settings.read(tmp_path, model_owners=OWNERS)
        with pytest.raises(ValueError):
            settings.bootstrap_locked(tmp_path, before["snapshot"], common, model_owners=OWNERS, lock_fd=fd)
    assert (tmp_path / "config.json").read_bytes() == original
    assert not (tmp_path / "data" / ".provider-settings-pending.json").exists()
    assert not (tmp_path / "data" / "provider_settings").exists()


def test_fresh_bootstrap_binds_unknown_values_to_authority(tmp_path):
    import config_edit

    common = {"future_synthetic": {"items": [None, False, 0, "例"]}}
    _fresh_root(tmp_path, common)
    with config_edit._edit_lock(tmp_path / "config.json") as fd:
        before = settings.read(tmp_path, model_owners=OWNERS)
        result = settings.bootstrap_locked(tmp_path, before["snapshot"], common, model_owners=OWNERS, lock_fd=fd)
    assert result["common"] == common
    authority = json.loads((tmp_path / "data" / ".provider-settings-authority.json").read_text())
    assert authority["common_policy_sha256"] == settings.common_policy_digest(common)
    private_json(tmp_path / "config.json", {"future_synthetic": {"items": [None, False, 1, "例"]}})
    with pytest.raises(ValueError, match="provider_authority_invalid"):
        settings.read(tmp_path, model_owners=OWNERS)


def test_bootstrap_rejects_nonmissing_validation_error(tmp_path, monkeypatch):
    import config_edit

    _fresh_root(tmp_path, {})
    original_validate = mcs_config.validate_config
    def invalid(cfg):
        errors, warnings = original_validate(cfg)
        return errors + ["synthetic-validation-error"], warnings
    monkeypatch.setattr(mcs_config, "validate_config", invalid)
    with config_edit._edit_lock(tmp_path / "config.json") as fd:
        before = settings.read(tmp_path, model_owners=OWNERS)
        with pytest.raises(ValueError, match="provider_config_invalid"):
            settings.bootstrap_locked(tmp_path, before["snapshot"], {}, model_owners=OWNERS, lock_fd=fd)
    assert not (tmp_path / "data" / ".provider-settings-pending.json").exists()


def test_regular_commit_still_rejects_missing_required_fields(tmp_path):
    import config_edit

    _fresh_root(tmp_path, {})
    with config_edit._edit_lock(tmp_path / "config.json") as fd:
        before = settings.read(tmp_path, model_owners=OWNERS)
        with pytest.raises(ValueError, match="provider_config_invalid"):
            settings.commit_locked(tmp_path, before["snapshot"], {}, {name: {} for name in settings.PROVIDERS},
                                   selected_provider="anthropic", model_owners=OWNERS, lock_fd=fd)
    assert not (tmp_path / "data" / ".provider-settings-pending.json").exists()


@pytest.mark.parametrize("damage", ["missing_config", "common_changed", "snapshot_changed", "pending", "split"])
def test_bootstrap_requires_matching_fresh_legacy_snapshot(tmp_path, damage):
    import config_edit

    if damage == "split":
        tree(tmp_path, split=True)
    else:
        _fresh_root(tmp_path, {})
    with config_edit._edit_lock(tmp_path / "config.json") as fd:
        before = settings.read(tmp_path, model_owners=OWNERS)
        common = {} if damage == "split" else before["common"]
        if damage == "missing_config":
            (tmp_path / "config.json").unlink()
        elif damage == "common_changed":
            common = {"runtime_mode": "hermes"}
        elif damage == "snapshot_changed":
            private_json(tmp_path / "config.json", {"runtime_mode": "hermes"})
        elif damage == "pending":
            private_json(tmp_path / "data" / ".provider-settings-pending.json", {})
        with pytest.raises(ValueError):
            settings.bootstrap_locked(tmp_path, before["snapshot"], common, model_owners=OWNERS, lock_fd=fd)
    if damage == "pending":
        assert (tmp_path / "data" / ".provider-settings-pending.json").exists()


@pytest.mark.parametrize(("provider", "order", "selected", "kept"), [
    ("auto", ["anthropic", "openai"], "openai", True),
    ("auto", ["anthropic", "openai"], "anthropic", True),
    ("auto", ["openai", "anthropic"], "anthropic", True),
    ("auto", ["openai", "anthropic"], "openai", True),
    ("openai", ["anthropic", "openai"], "openai", True),
])
def test_execution_view_preserves_explicit_selected_model(provider, order, selected, kept):
    common = {"cloud_llm": {"provider": provider, "provider_order": order,
                            "project_ids": None, "mode": "off"}}
    providers = {"anthropic": {"model": "synthetic-anthropic"},
                 "openai": {"model": "synthetic-openai"}, "jev": {}}
    before = json.dumps([common, providers], sort_keys=True)
    raw = settings.compose(common, providers, selected_provider=selected, model_owners=OWNERS)
    view = settings.execution_view(common, providers, selected_provider=selected, model_owners=OWNERS)
    assert raw["cloud_llm"]["model"] == "synthetic-" + selected
    assert ("model" in view["cloud_llm"]) is kept
    assert view["cloud_llm"]["provider"] == provider
    assert view["cloud_llm"]["provider_order"] == order
    assert json.dumps([common, providers], sort_keys=True) == before


def test_execution_view_accepts_known_multiple_models_and_preserves_invalid_order():
    common = {"cloud_llm": {"provider": "auto"}}
    providers = {"anthropic": {}, "openai": {"model": "synthetic-openai"}, "jev": {}}
    owners = {**OWNERS, "synthetic-openai-other": "openai"}
    view = settings.execution_view(common, providers, selected_provider="openai", model_owners=owners)
    assert view["cloud_llm"]["model"] == "synthetic-openai"
    common["cloud_llm"]["provider_order"] = ["anthropic", "anthropic"]
    view = settings.execution_view(common, providers, selected_provider="openai", model_owners=OWNERS)
    assert view["cloud_llm"]["provider_order"] == ["anthropic", "anthropic"]
    assert view["cloud_llm"]["model"] == "synthetic-openai"


def test_auto_execution_view_preserves_all_budgets_and_jev_values():
    common = {"cloud_llm": {"mode": "off", "provider": "auto",
                            "provider_order": ["anthropic", "openai"],
                            "daily_cost_limit_usd": None, "project_ids": []},
              "semantic": {"mode": "off"}, "extract": {"prefilter": False}}
    providers = {
        "anthropic": {"model": "synthetic-anthropic", "budget": {
            "daily_cost_limit_usd": 0, "monthly_cost_limit_usd": 0}},
        "openai": {"model": "synthetic-openai", "budget": {
            "daily_cost_limit_usd": 1, "monthly_cost_limit_usd": 2}},
        "jev": {"model": "synthetic-jev", "daily_request_budget": 0, "until": None,
                "attempt_timeout_seconds": 10, "max_attempts_per_try": 1},
    }
    before = json.dumps([common, providers], sort_keys=True)
    raw = settings.compose(common, providers, selected_provider="openai", model_owners=OWNERS,
                           jev_model="synthetic-jev")
    raw_before = json.dumps(raw, sort_keys=True)
    view = settings.execution_view(common, providers, selected_provider="openai", model_owners=OWNERS,
                                   jev_model="synthetic-jev")
    assert raw["cloud_llm"]["model"] == "synthetic-openai"
    assert view["cloud_llm"]["model"] == providers["openai"]["model"]
    assert view["cloud_llm"]["provider_budgets"] == {
        "anthropic": providers["anthropic"]["budget"], "openai": providers["openai"]["budget"]}
    assert view["cloud_llm"]["daily_cost_limit_usd"] is None
    assert "monthly_cost_limit_usd" not in view["cloud_llm"]
    assert view["semantic"] == {"mode": "off", **providers["jev"]}
    assert view["extract"] == {"prefilter": False}
    expected = json.loads(raw_before)
    assert view == expected
    assert json.dumps(raw, sort_keys=True) == raw_before
    assert json.dumps([common, providers], sort_keys=True) == before


def test_auto_selected_second_provider_uses_its_pinned_model(monkeypatch):
    owners = {model: name for name, model in cloud_policy.PROVIDERS.items()}
    common = {"cloud_llm": {"provider": "auto", "mode": "off", "project_ids": []}}
    providers = {name: {"model": model} for name, model in cloud_policy.PROVIDERS.items()}
    providers["jev"] = {}
    monkeypatch.setattr(cloud_policy, "provider_key",
                        lambda name, root: "synthetic-key" if name == "openai" else None)
    selected, errors = cloud_policy.resolve_settings(common, "synthetic-root")
    assert not errors and selected["provider"] == "openai"
    view = settings.execution_view(common, providers, selected_provider=selected["provider"], model_owners=owners)
    resolved, errors = cloud_policy.resolve_settings(view, "synthetic-root")
    assert not errors and resolved["provider"] == "openai"
    assert resolved["model"] == cloud_policy.PROVIDERS["openai"]
    assert view["cloud_llm"]["model"] == cloud_policy.PROVIDERS["openai"]


def test_unknown_model_or_secret_provider_key_is_rejected():
    with pytest.raises(ValueError):
        settings.split_legacy({"cloud_llm": {"model": "synthetic-unknown"}}, OWNERS)
    providers = {name: {} for name in settings.PROVIDERS}
    providers["openai"] = {"secret": "synthetic-canary"}
    with pytest.raises(ValueError):
        settings.compose({}, providers, selected_provider="openai", model_owners=OWNERS)


def test_explicit_jev_model_requires_trusted_registry():
    raw = {"semantic": {"model": "synthetic-jev", "until": None}}
    with pytest.raises(ValueError, match="provider_model_invalid"):
        settings.split_legacy(raw, OWNERS)
    common, providers = settings.split_legacy(raw, OWNERS, jev_model="synthetic-jev")
    assert settings.compose(common, providers, selected_provider="openai", model_owners=OWNERS,
                            jev_model="synthetic-jev") == raw


def test_read_without_authority_does_not_claim_legacy(tmp_path):
    common, _, _ = tree(tmp_path)
    result = settings.read(tmp_path)
    assert result["mode"] == "legacy_unverified" and result["common"] == common
    assert result["snapshot"]["authority"] is None


def test_empty_provider_directory_without_authority_never_becomes_legacy(tmp_path):
    _, _, directory = tree(tmp_path)
    directory.mkdir(mode=0o700)
    assert settings.probe(tmp_path)["state"] == "requires_checked_read"
    with pytest.raises(ValueError, match="provider_authority_missing"):
        settings.read(tmp_path, model_owners=OWNERS)
    with pytest.raises(ValueError, match="provider_authority_missing"):
        settings.read_with_verified_common(tmp_path, model_owners=OWNERS)
    assert directory.is_dir() and not list(directory.iterdir())


@pytest.mark.parametrize("key", sorted(settings._JEV_KEYS))
@pytest.mark.parametrize("private_damaged", [False, True])
def test_split_common_never_contains_jev_keys_even_if_private_missing_key(tmp_path, key, private_damaged):
    common, data, directory = tree(tmp_path, split=True)
    common["semantic"][key] = None
    private_json(tmp_path / "config.json", common)
    authority_path = data / ".provider-settings-authority.json"
    authority = json.loads(authority_path.read_text())
    authority["common_policy_sha256"] = settings.common_policy_digest(common)
    private_json(authority_path, authority)
    if private_damaged:
        private_json(directory / "anthropic.json", {"model": "synthetic-unverified"})
    else:
        with pytest.raises(ValueError, match="provider_config_ambiguous"):
            settings.read(tmp_path, model_owners=OWNERS)
    with pytest.raises(ValueError, match="provider_config_ambiguous"):
        settings.read_with_verified_common(tmp_path, model_owners=OWNERS)
    providers = {name: {} for name in settings.PROVIDERS}
    with pytest.raises(ValueError, match="provider_config_ambiguous"):
        settings.compose(common, providers, selected_provider="anthropic", model_owners=OWNERS)


@pytest.mark.parametrize("name,kind", [
    (".provider-settings-backup-synthetic-config.bin", "file"),
    (".provider-settings-stage-synthetic", "directory"),
    (".provider-settings-stage-synthetic", "symlink"),
])
def test_retained_remnant_without_authority_is_never_legacy(tmp_path, name, kind):
    _, data, _ = tree(tmp_path)
    remnant = data / name
    if kind == "directory":
        remnant.mkdir()
    elif kind == "symlink":
        remnant.symlink_to(data / "synthetic-missing")
    else:
        remnant.write_text("synthetic witness")
    assert settings.probe(tmp_path)["state"] == "requires_checked_read"
    with pytest.raises(ValueError, match="provider_authority_missing"):
        settings.read(tmp_path, model_owners=OWNERS)
    with pytest.raises(ValueError, match="provider_authority_missing"):
        settings.read_with_verified_common(tmp_path, model_owners=OWNERS)
    assert os.path.lexists(remnant)


def test_retained_remnants_with_valid_authority_do_not_block_split(tmp_path):
    common, data, _ = tree(tmp_path, split=True)
    (data / ".provider-settings-backup-synthetic-config.bin").write_text("synthetic witness")
    (data / ".provider-settings-stage-synthetic").mkdir()
    assert settings.probe(tmp_path)["state"] == "requires_checked_read"
    assert settings.read(tmp_path, model_owners=OWNERS)["common"] == common


def test_probe_never_calls_split_without_checked_read(tmp_path):
    tree(tmp_path, split=True)
    assert settings.probe(tmp_path) == {"state": "requires_checked_read", "config_present": True}


@pytest.mark.parametrize("damage", ["missing", "invalid", "symlink"])
def test_pending_precedes_unavailable_common_config(tmp_path, damage):
    _, data, _ = tree(tmp_path)
    private_json(data / ".provider-settings-pending.json", {"state": "pending"})
    path = tmp_path / "config.json"
    if damage == "missing":
        path.unlink()
    elif damage == "invalid":
        path.write_text("{")
    else:
        path.unlink()
        path.symlink_to(tmp_path / "synthetic-missing")
    with pytest.raises(ValueError, match="provider_settings_pending"):
        settings.read(tmp_path)


def test_missing_common_without_pending_still_fails_closed(tmp_path):
    tmp_path.chmod(0o700)
    with pytest.raises(ValueError, match="provider_file_missing"):
        settings.read(tmp_path)
    assert settings.probe(tmp_path)["state"] == "uninitialized_or_legacy_unverified"


def test_pending_appearing_during_missing_common_read_is_reported(tmp_path, monkeypatch):
    _, data, _ = tree(tmp_path)
    (tmp_path / "config.json").unlink()
    original = settings._read_file
    def move_during_read(parent, name, **kwargs):
        if name == "config.json":
            private_json(data / ".provider-settings-pending.json", {"state": "pending"})
        return original(parent, name, **kwargs)
    monkeypatch.setattr(settings, "_read_file", move_during_read)
    with pytest.raises(ValueError, match="provider_settings_pending"):
        settings.read(tmp_path)


@pytest.mark.parametrize("mode", [0o600, 0o644, 0o666])
def test_common_file_keeps_legacy_permission_handling(tmp_path, capsys, mode):
    common, _, _ = tree(tmp_path)
    (tmp_path / "config.json").chmod(mode)
    assert settings.read(tmp_path)["common"] == common
    warning = capsys.readouterr().err
    assert ("local_file_permissions_weak (config.json)" in warning) is bool(mode & 0o022)


def test_common_file_hardlink_keeps_legacy_read_support(tmp_path):
    common, _, _ = tree(tmp_path)
    os.link(tmp_path / "config.json", tmp_path / "synthetic-hardlink")
    assert settings.read(tmp_path)["common"] == common


def test_split_read_and_missing_directory_fail_closed(tmp_path):
    common, _, directory = tree(tmp_path, split=True)
    result = settings.read(tmp_path)
    assert result["mode"] == "split" and result["common"] == common
    assert all(result["snapshot"]["files"][name] for name in settings.PROVIDERS)
    directory.rename(directory.with_name("synthetic-moved"))
    with pytest.raises(ValueError, match="provider_authority_invalid"):
        settings.read(tmp_path)


@pytest.mark.parametrize("root_key", ["drug_map", "sdc", "install_args"])
def test_independent_root_save_reformat_and_rollback_keep_provider_caps(tmp_path, root_key):
    common, data, _ = tree(tmp_path, split=True)
    config_path = tmp_path / "config.json"
    authority_path = data / ".provider-settings-authority.json"
    original_raw = config_path.read_text()
    authority_raw = authority_path.read_bytes()
    updated = {**common, root_key: {"synthetic": 0}}
    safe_io.atomic_write(str(config_path), lambda stream: stream.write(json.dumps(updated, indent=4)), mode=0o600)
    assert settings.read(tmp_path)["mode"] == "split"
    assert authority_path.read_bytes() == authority_raw
    safe_io.atomic_write(str(config_path), lambda stream: stream.write(original_raw), mode=0o600)
    assert settings.read(tmp_path)["common"] == common
    assert authority_path.read_bytes() == authority_raw


@pytest.mark.parametrize("root_key", ["cloud_llm", "semantic", "notify", "notify_target", "stamps", "extract", "future_root"])
def test_critical_or_unknown_root_change_requires_authority_update(tmp_path, root_key):
    common, _, _ = tree(tmp_path, split=True)
    private_json(tmp_path / "config.json", {**common, root_key: {"synthetic": "changed"}})
    with pytest.raises(ValueError, match="provider_authority_invalid"):
        settings.read(tmp_path)


def test_common_policy_distinguishes_absent_null_zero_and_off():
    values = [{}, {"future_root": None}, {"future_root": 0}, {"future_root": "off"}]
    assert len({settings.common_policy_digest(value) for value in values}) == len(values)


def test_independent_root_change_still_rejects_stale_short_term_cas(tmp_path, monkeypatch):
    common, _, _ = tree(tmp_path, split=True)
    original = settings.read(tmp_path)
    private_json(tmp_path / "config.json", {**common, "drug_map": {"synthetic": 1}})
    assert settings.read(tmp_path)["mode"] == "split"
    with pytest.raises(ValueError, match="provider_snapshot_changed"):
        _commit(tmp_path, monkeypatch, original)


def test_unrecognized_old_authority_is_held_without_rebaseline(tmp_path):
    _, data, _ = tree(tmp_path, split=True)
    path = data / ".provider-settings-authority.json"
    authority = json.loads(path.read_text())
    authority["version"] = 1
    private_json(path, authority)
    with pytest.raises(ValueError, match="provider_authority_invalid"):
        settings.read(tmp_path)


def test_split_model_must_belong_to_trusted_provider_registry(tmp_path):
    _, data, directory = tree(tmp_path, split=True)
    authority_path = data / ".provider-settings-authority.json"
    authority = json.loads(authority_path.read_text())
    authority["files"]["openai"] = private_json(directory / "openai.json", {"model": "synthetic-openai"})
    private_json(authority_path, authority)
    with pytest.raises(ValueError, match="provider_model_invalid"):
        settings.read(tmp_path)
    assert settings.read(tmp_path, model_owners=OWNERS)["mode"] == "split"


def _commit(tmp_path, monkeypatch, original):
    monkeypatch.setattr(mcs_config, "validate_config", lambda _: ([], []))
    return settings.commit(tmp_path, original["snapshot"], original["common"], original["providers"],
                           selected_provider="openai", model_owners=OWNERS)


def test_commit_validates_execution_view_but_persists_explicit_model(tmp_path, monkeypatch):
    tree(tmp_path, split=True)
    original = settings.read(tmp_path, model_owners=OWNERS)
    original["common"]["cloud_llm"]["provider"] = "auto"
    original["providers"]["openai"]["model"] = "synthetic-openai"
    seen = []
    monkeypatch.setattr(mcs_config, "validate_config", lambda view: (seen.append(view) or [], []))
    result = settings.commit(tmp_path, original["snapshot"], original["common"], original["providers"],
                             selected_provider="openai", model_owners=OWNERS)
    assert len(seen) == 1 and seen[0]["cloud_llm"]["model"] == "synthetic-openai"
    assert result["providers"]["openai"]["model"] == "synthetic-openai"
    assert result["common"]["cloud_llm"]["provider"] == "auto"


def test_explicit_large_common_config_limit_does_not_expand_private_files(tmp_path, monkeypatch):
    common, _, directory = tree(tmp_path, split=True)
    common["synthetic_padding"] = "x" * settings.CONFIG_MAX_BYTES
    private_json(tmp_path / "config.json", common)
    authority_path = tmp_path / "data" / ".provider-settings-authority.json"
    authority = json.loads(authority_path.read_text())
    authority["common_policy_sha256"] = settings.common_policy_digest(common)
    private_json(authority_path, authority)
    with pytest.raises(ValueError):
        settings.read(tmp_path)
    original = settings.read(tmp_path, config_limit=settings.ALIAS_CONFIG_MAX_BYTES)
    monkeypatch.setattr(mcs_config, "validate_config", lambda _: ([], []))
    final = settings.commit(tmp_path, original["snapshot"], original["common"], original["providers"],
                            selected_provider="openai", model_owners=OWNERS,
                            config_limit=settings.ALIAS_CONFIG_MAX_BYTES)
    assert final["mode"] == "split" and final["common"] == common
    private_json(directory / "openai.json", {"synthetic_padding": "x" * settings.CONFIG_MAX_BYTES})
    with pytest.raises(ValueError):
        settings.read(tmp_path, config_limit=settings.ALIAS_CONFIG_MAX_BYTES)


def test_commit_retains_separate_backup_against_open_old_fd(tmp_path, monkeypatch):
    tree(tmp_path)
    original = settings.read(tmp_path)
    old_fd = os.open(tmp_path / "config.json", os.O_WRONLY)
    original_bytes = (tmp_path / "config.json").read_bytes()
    try:
        final = _commit(tmp_path, monkeypatch, original)
        backup, = (tmp_path / "data").glob(".provider-settings-backup-*-config.bin")
        assert backup.read_bytes() == original_bytes
        assert backup.stat().st_ino != os.fstat(old_fd).st_ino
        assert final["mode"] == "split"
        os.pwrite(old_fd, b"synthetic-old-write", 0)
        assert backup.read_bytes() == original_bytes
        assert settings.read(tmp_path)["mode"] == "split"
    finally:
        os.close(old_fd)


def test_repeated_commit_does_not_accumulate_witnesses(tmp_path, monkeypatch):
    tree(tmp_path)
    first = _commit(tmp_path, monkeypatch, settings.read(tmp_path))
    authority = tmp_path / "data" / ".provider-settings-authority.json"
    second = _commit(tmp_path, monkeypatch, first)
    current = json.loads(authority.read_text())
    assert second["mode"] == "split" and "witnesses" not in current


def test_borrowed_editor_lock_is_preserved_without_reacquiring(tmp_path, monkeypatch):
    import config_edit

    tree(tmp_path)
    lock_path = tmp_path / "config.json.edit.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        original = settings.read(tmp_path)
        monkeypatch.setattr(mcs_config, "validate_config", lambda _: ([], []))
        monkeypatch.setattr(config_edit, "_edit_lock", lambda *args: pytest.fail("lock already held"))
        final = settings.commit_locked(tmp_path, original["snapshot"], original["common"], original["providers"],
                                       selected_provider="openai", model_owners=OWNERS, lock_fd=lock_fd)
        assert final["mode"] == "split" and os.fstat(lock_fd).st_ino == lock_path.stat().st_ino
        other = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(other)
    finally:
        os.close(lock_fd)


def test_root_directory_replacement_rejects_stale_snapshot(tmp_path, monkeypatch):
    root = tmp_path / "synthetic-root"
    root.mkdir(mode=0o700)
    tree(root)
    original = settings.read(root)
    root.rename(tmp_path / "synthetic-old-root")
    root.mkdir(mode=0o700)
    tree(root)
    with pytest.raises(ValueError, match="provider_snapshot_changed"):
        _commit(root, monkeypatch, original)
    assert not (root / "data" / ".provider-settings-pending.json").exists()


class _Crash(BaseException):
    """Synthetic hard stop (power loss, SIGKILL) that no in-process cleanup observes."""


@pytest.mark.parametrize("stage", range(1, 6))
def test_interrupted_replacement_keeps_pending_and_blocks_reader(tmp_path, monkeypatch, stage):
    tree(tmp_path)
    original = settings.read(tmp_path)
    replace = settings._replace
    calls = []
    def interrupted(*args):
        result = replace(*args)
        calls.append(True)
        if len(calls) == stage:
            raise _Crash("synthetic interruption")
        return result
    monkeypatch.setattr(settings, "_replace", interrupted)
    with pytest.raises(_Crash, match="synthetic interruption"):
        _commit(tmp_path, monkeypatch, original)
    assert len(calls) == stage
    with pytest.raises(ValueError, match="provider_settings_pending"):
        settings.read(tmp_path)
    backup, = (tmp_path / "data").glob(".provider-settings-backup-*-config.bin")
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == original["snapshot"]["files"]["config"]["sha256"]


@pytest.mark.parametrize("phase", ["pending", "backup", "staged"])
def test_interrupted_prepublication_keeps_pending_and_source(tmp_path, monkeypatch, phase):
    tree(tmp_path)
    original = settings.read(tmp_path)
    source = (tmp_path / "config.json").read_bytes()
    write_new = settings._write_new
    def interrupted(parent, name, raw, **kwargs):
        result = write_new(parent, name, raw, **kwargs)
        if (phase == "pending" and name == ".provider-settings-pending.json"
                or phase == "backup" and name.startswith(".provider-settings-backup-")
                or phase == "staged" and name.startswith("new-")):
            raise _Crash("synthetic interruption")
        return result
    monkeypatch.setattr(settings, "_write_new", interrupted)
    with pytest.raises(_Crash, match="synthetic interruption"):
        _commit(tmp_path, monkeypatch, original)
    assert (tmp_path / "config.json").read_bytes() == source
    with pytest.raises(ValueError, match="provider_settings_pending"):
        settings.read(tmp_path)


def _interrupt_commit(monkeypatch, point, failure):
    kind, _, count = point.partition("-")
    if kind == "replace":
        replace, calls = settings._replace, []
        def interrupted(*args):
            result = replace(*args)
            calls.append(True)
            if len(calls) == int(count):
                raise failure("synthetic interruption")
            return result
        monkeypatch.setattr(settings, "_replace", interrupted)
    elif kind == "link":
        link, failed = os.link, []
        def interrupted(src, dst, **kwargs):
            if dst == "openai.json" and not failed:
                failed.append(True)
                raise failure("synthetic interruption")
            return link(src, dst, **kwargs)
        monkeypatch.setattr(settings.os, "link", interrupted)
    else:
        write_new = settings._write_new
        def interrupted(parent, name, raw, **kwargs):
            result = write_new(parent, name, raw, **kwargs)
            if (kind == "pending" and name == ".provider-settings-pending.json"
                    or kind == "backup" and name.startswith(".provider-settings-backup-")
                    or kind == "staged" and name.startswith("new-")
                    or kind == "record" and name == settings.CHANGE_RECORD):
                raise failure("synthetic interruption")
            return result
        monkeypatch.setattr(settings, "_write_new", interrupted)


def _tree_bytes(root):
    return {str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
            for path in [root / "config.json", *(root / "data").rglob("*")]}


@pytest.mark.parametrize("point", ["pending", "backup", "staged", "replace-1", "replace-3", "replace-5", "link",
                                   "record"])
@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt])
@pytest.mark.parametrize("split", [False, True])
def test_io_failure_during_commit_restores_previous_settings(tmp_path, monkeypatch, capsys, point, failure, split):
    tree(tmp_path, split=split)
    original = settings.read(tmp_path)
    before = _tree_bytes(tmp_path)
    original["providers"]["openai"] = {"model": "synthetic-openai"}
    _interrupt_commit(monkeypatch, point, failure)
    with pytest.raises(failure, match="synthetic interruption"):
        _commit(tmp_path, monkeypatch, original)
    assert _tree_bytes(tmp_path) == before
    assert "provider_settings_rollback_failed" not in capsys.readouterr().err
    after = settings.read(tmp_path)
    assert after["mode"] == ("split" if split else "legacy_unverified")
    assert after["providers"] == {name: {} for name in settings.PROVIDERS}
    assert ({key: value and value["sha256"] for key, value in after["snapshot"]["files"].items()}
            == {key: value and value["sha256"] for key, value in original["snapshot"]["files"].items()})


def test_unverified_rollback_keeps_pending_and_blocks_reader(tmp_path, monkeypatch, capsys):
    tree(tmp_path)
    original = settings.read(tmp_path)
    _interrupt_commit(monkeypatch, "replace-2", OSError)
    def refused(*args, **kwargs):
        raise OSError("synthetic rollback failure")
    monkeypatch.setattr(settings.os, "rmdir", refused)
    with pytest.raises(OSError, match="synthetic interruption"):
        _commit(tmp_path, monkeypatch, original)
    assert "provider_settings_rollback_failed: OSError" in capsys.readouterr().err
    assert (tmp_path / "data" / ".provider-settings-pending.json").exists()
    with pytest.raises(ValueError, match="provider_settings_pending"):
        settings.read(tmp_path)


def test_interrupt_after_receipt_removal_keeps_committed_settings(tmp_path, monkeypatch):
    tree(tmp_path, split=True)
    original = settings.read(tmp_path, model_owners=OWNERS)
    original["providers"]["openai"] = {"model": "synthetic-openai"}
    unlink, fired = os.unlink, []
    def interrupted(name, **kwargs):
        unlink(name, **kwargs)
        if name == ".provider-settings-pending.json" and not fired:
            fired.append(True)
            raise KeyboardInterrupt("synthetic interruption")
    monkeypatch.setattr(settings.os, "unlink", interrupted)
    with pytest.raises(KeyboardInterrupt, match="synthetic interruption"):
        _commit(tmp_path, monkeypatch, original)
    assert not (tmp_path / "data" / ".provider-settings-pending.json").exists()
    after = settings.read(tmp_path, model_owners=OWNERS)
    assert after["mode"] == "split" and after["providers"]["openai"] == {"model": "synthetic-openai"}


def test_failure_creating_receipt_leaves_settings_untouched(tmp_path, monkeypatch, capsys):
    tree(tmp_path, split=True)
    original = settings.read(tmp_path)
    before = _tree_bytes(tmp_path)
    original["providers"]["openai"] = {"model": "synthetic-openai"}
    create = settings.create_exclusive
    def refused(name, *args, **kwargs):
        if name == ".provider-settings-pending.json":
            raise OSError("synthetic interruption")
        return create(name, *args, **kwargs)
    monkeypatch.setattr(settings, "create_exclusive", refused)
    with pytest.raises(OSError, match="synthetic interruption"):
        _commit(tmp_path, monkeypatch, original)
    assert _tree_bytes(tmp_path) == before
    assert "provider_settings_rollback_failed" not in capsys.readouterr().err
    assert settings.read(tmp_path)["providers"]["openai"] == {}


@pytest.mark.parametrize("name", ["config", "anthropic", "openai", "jev", "authority"])
def test_stale_snapshot_never_rebaselines_or_writes(tmp_path, monkeypatch, name):
    tree(tmp_path, split=True)
    original = settings.read(tmp_path)
    target = (tmp_path / "config.json" if name == "config" else
              tmp_path / "data" / ".provider-settings-authority.json" if name == "authority" else
              tmp_path / "data" / "provider_settings" / (name + ".json"))
    private_json(target, {"synthetic": "changed"})
    before = target.read_bytes()
    with pytest.raises(ValueError):
        _commit(tmp_path, monkeypatch, original)
    assert target.read_bytes() == before
    assert not (tmp_path / "data" / ".provider-settings-pending.json").exists()


def test_late_old_writer_is_detected_before_next_publication(tmp_path, monkeypatch):
    tree(tmp_path, split=True)
    original = settings.read(tmp_path)
    replace = settings._replace
    calls = []
    def interference(*args):
        result = replace(*args)
        calls.append(True)
        if len(calls) == 1:
            other = tmp_path / "data" / "provider_settings" / "openai.json"
            private_json(other, {"model": "synthetic-openai"})
        return result
    monkeypatch.setattr(settings, "_replace", interference)
    with pytest.raises(ValueError, match="provider_snapshot_changed"):
        _commit(tmp_path, monkeypatch, original)
    with pytest.raises(ValueError, match="provider_settings_pending"):
        settings.read(tmp_path)


def test_old_path_replaced_between_check_and_rename_is_preserved(tmp_path, monkeypatch):
    tree(tmp_path)
    original = settings.read(tmp_path)
    rename = os.rename
    def interfere(src, dst, **kwargs):
        if src == "config.json":
            private_json(tmp_path / "synthetic-new.json", {"synthetic": "old-writer"})
            os.replace(tmp_path / "synthetic-new.json", tmp_path / "config.json")
        return rename(src, dst, **kwargs)
    monkeypatch.setattr(settings.os, "rename", interfere)
    with pytest.raises(ValueError, match="provider_snapshot_changed"):
        _commit(tmp_path, monkeypatch, original)
    stage, = (tmp_path / "data").glob(".provider-settings-stage-*")
    assert json.loads((stage / "config").read_text()) == {"synthetic": "old-writer"}
    with pytest.raises(ValueError, match="provider_settings_pending"):
        settings.read(tmp_path)


def test_old_path_created_after_rename_is_not_overwritten(tmp_path, monkeypatch):
    tree(tmp_path)
    original = settings.read(tmp_path)
    link = os.link
    def interfere(src, dst, **kwargs):
        if dst == "config.json":
            private_json(tmp_path / "config.json", {"synthetic": "old-writer"})
        return link(src, dst, **kwargs)
    monkeypatch.setattr(settings.os, "link", interfere)
    with pytest.raises(FileExistsError):
        _commit(tmp_path, monkeypatch, original)
    assert json.loads((tmp_path / "config.json").read_text()) == {"synthetic": "old-writer"}
    with pytest.raises(ValueError, match="provider_settings_pending"):
        settings.read(tmp_path)


def test_old_fd_write_before_commit_is_held_and_backup_preserved(tmp_path, monkeypatch):
    tree(tmp_path)
    original = settings.read(tmp_path)
    old_fd = os.open(tmp_path / "config.json", os.O_WRONLY)
    replace = settings._replace
    calls = []
    def interfere(*args):
        result = replace(*args)
        calls.append(True)
        if len(calls) == 1:
            os.pwrite(old_fd, b"synthetic-old-write", 0)
        return result
    monkeypatch.setattr(settings, "_replace", interfere)
    try:
        with pytest.raises(ValueError, match="provider_snapshot_changed"):
            _commit(tmp_path, monkeypatch, original)
        backup, = (tmp_path / "data").glob(".provider-settings-backup-*-config.bin")
        assert hashlib.sha256(backup.read_bytes()).hexdigest() == original["snapshot"]["files"]["config"]["sha256"]
        with pytest.raises(ValueError, match="provider_settings_pending"):
            settings.read(tmp_path)
    finally:
        os.close(old_fd)


def test_late_old_common_replacement_does_not_rebaseline_authority(tmp_path, monkeypatch):
    tree(tmp_path)
    original = settings.read(tmp_path)
    _commit(tmp_path, monkeypatch, original)
    private_json(tmp_path / "config.json", {"cloud_llm": {"mode": "on"}})
    with pytest.raises(ValueError, match="provider_authority_invalid"):
        settings.read(tmp_path)


@pytest.mark.parametrize("damage", ["pending", "changed", "symlink", "hardlink", "mode", "missing", "duplicate", "extra"])
def test_split_read_rejects_unproven_or_unsafe_state(tmp_path, damage):
    _, data, directory = tree(tmp_path, split=True)
    target = directory / "openai.json"
    if damage == "pending":
        private_json(data / ".provider-settings-pending.json", {"state": "pending"})
    elif damage == "changed":
        private_json(target, {"model": "synthetic-openai"})
    elif damage == "symlink":
        target.unlink()
        target.symlink_to(directory / "jev.json")
    elif damage == "hardlink":
        os.link(target, directory / "synthetic-link")
    elif damage == "mode":
        target.chmod(0o644)
    elif damage == "missing":
        target.unlink()
    elif damage == "duplicate":
        target.write_text('{"model":1,"model":2}')
    else:
        target.write_text('{"secret":"synthetic-canary"}')
    if damage in ("duplicate", "extra"):
        authority_path = data / ".provider-settings-authority.json"
        authority = json.loads(authority_path.read_text())
        authority["files"]["openai"] = hashlib.sha256(target.read_bytes()).hexdigest()
        private_json(authority_path, authority)
    with pytest.raises((ValueError, OSError)):
        settings.read(tmp_path)


def test_verified_common_keeps_strict_result_for_healthy_and_legacy(tmp_path):
    tree(tmp_path, split=True)
    assert settings.read_with_verified_common(tmp_path, model_owners=OWNERS) == settings.read(
        tmp_path, model_owners=OWNERS)
    legacy = tmp_path / 'legacy'
    legacy.mkdir(mode=0o700)
    tree(legacy)
    assert settings.read_with_verified_common(legacy, model_owners=OWNERS) == settings.read(
        legacy, model_owners=OWNERS)


def _replace_provider_and_hash(data, directory, name, value):
    digest = private_json(directory / (name + '.json'), value)
    authority_path = data / '.provider-settings-authority.json'
    authority = json.loads(authority_path.read_text())
    authority['files'][name] = digest
    private_json(authority_path, authority)


@pytest.mark.parametrize('failure', ['missing', 'hash', 'json', 'model', 'budget', 'permissions'])
def test_verified_common_returns_only_common_for_provider_failure(tmp_path, failure):
    common, data, directory = tree(tmp_path, split=True)
    path = directory / 'openai.json'
    if failure == 'missing':
        path.unlink()
    elif failure == 'hash':
        private_json(path, {'model': 'synthetic-openai'})
    elif failure == 'json':
        raw = b'{invalid synthetic json'
        path.write_bytes(raw)
        authority_path = data / '.provider-settings-authority.json'
        authority = json.loads(authority_path.read_text())
        authority['files']['openai'] = hashlib.sha256(raw).hexdigest()
        private_json(authority_path, authority)
    elif failure == 'model':
        _replace_provider_and_hash(data, directory, 'openai', {'model': 'synthetic-anthropic'})
    elif failure == 'budget':
        _replace_provider_and_hash(data, directory, 'openai', {'budget': {'daily_cost_limit_usd': -1}})
    else:
        path.chmod(0o644)
    with pytest.raises(ValueError):
        settings.read(tmp_path, model_owners=OWNERS)
    result = settings.read_with_verified_common(tmp_path, model_owners=OWNERS)
    assert result['mode'] == 'provider_unavailable'
    assert result['common'] == common
    assert result['providers'] is None
    assert result['snapshot']['authority'] is not None


def test_verified_common_does_not_misclassify_missing_model_registry(tmp_path):
    _, data, directory = tree(tmp_path, split=True)
    _replace_provider_and_hash(data, directory, 'openai', {'model': 'synthetic-openai'})
    with pytest.raises(ValueError, match='provider_validation_context_missing'):
        settings.read_with_verified_common(tmp_path)


@pytest.mark.parametrize('failure', ['common', 'authority', 'pending', 'directory', 'ambiguous'])
def test_verified_common_fails_closed_for_nonprovider_failure(tmp_path, failure):
    common, data, directory = tree(tmp_path, split=True)
    private_json(directory / 'openai.json', {'unexpected': 'synthetic'})
    if failure == 'common':
        common['cloud_llm']['mode'] = 'on'
        private_json(tmp_path / 'config.json', common)
    elif failure == 'authority':
        private_json(data / '.provider-settings-authority.json', {'version': 2})
    elif failure == 'pending':
        private_json(data / '.provider-settings-pending.json', {})
    elif failure == 'directory':
        directory.chmod(0o755)
    else:
        common['semantic']['daily_request_budget'] = 1
        private_json(tmp_path / 'config.json', common)
        path = data / '.provider-settings-authority.json'
        authority = json.loads(path.read_text())
        authority['common_policy_sha256'] = settings.common_policy_digest(common)
        private_json(path, authority)
    with pytest.raises((ValueError, OSError)):
        settings.read_with_verified_common(tmp_path, model_owners=OWNERS)


@pytest.mark.parametrize('target', ['common', 'authority', 'pending', 'provider'])
def test_verified_common_rejects_concurrent_change(tmp_path, monkeypatch, target):
    common, data, directory = tree(tmp_path, split=True)
    private_json(directory / 'openai.json', {'unexpected': 'synthetic'})
    original = settings._read_file
    changed = False

    def read_then_change(parent, name, **kwargs):
        nonlocal changed
        result = original(parent, name, **kwargs)
        if name == 'openai.json' and not changed:
            changed = True
            if target == 'common':
                common['drug_map'] = {'synthetic': True}
                private_json(tmp_path / 'config.json', common)
            elif target == 'authority':
                private_json(data / '.provider-settings-authority.json', {})
            elif target == 'pending':
                private_json(data / '.provider-settings-pending.json', {})
            else:
                private_json(directory / 'openai.json', {'other': 'synthetic'})
        return result

    monkeypatch.setattr(settings, '_read_file', read_then_change)
    with pytest.raises((ValueError, OSError)):
        settings.read_with_verified_common(tmp_path, model_owners=OWNERS)
    assert changed


def test_verified_common_tolerates_transient_pending_without_trace(tmp_path, monkeypatch):
    """A pending marker that appears and disappears inside the read leaves the
    verified file set consistent; only a trace it leaves may hold the read."""
    common, data, directory = tree(tmp_path, split=True)
    private_json(directory / 'openai.json', {'unexpected': 'synthetic'})
    original = settings._read_file
    changed = False

    def read_then_change(parent, name, **kwargs):
        nonlocal changed
        result = original(parent, name, **kwargs)
        if name == 'openai.json' and not changed:
            changed = True
            pending = data / '.provider-settings-pending.json'
            private_json(pending, {})
            pending.unlink()
        return result

    monkeypatch.setattr(settings, '_read_file', read_then_change)
    state = settings.read_with_verified_common(tmp_path, model_owners=OWNERS)
    assert state['mode'] == 'provider_unavailable' and changed


@pytest.mark.parametrize('allow_unavailable', [False, True])
def test_runtime_config_retries_after_unrelated_atomic_data_update(tmp_path, monkeypatch, allow_unavailable):
    common, data, _ = tree(tmp_path, split=True)
    original = settings._read_file
    changed = False

    def read_then_update(parent, name, **kwargs):
        nonlocal changed
        value = original(parent, name, **kwargs)
        if name == 'openai.json' and not changed:
            changed = True
            temp = data / 'synthetic-runtime.tmp'
            temp.write_text('synthetic')
            os.replace(temp, data / 'synthetic-runtime.json')
        return value

    monkeypatch.setattr(settings, '_read_file', read_then_update)
    monkeypatch.setattr(mcs_util, '_provider_config_active', lambda path: True)
    monkeypatch.setattr(cloud_policy, 'resolve_settings', lambda common, root: ({'provider': 'openai'}, []))
    assert mcs_util._load_config(str(tmp_path / 'config.json'),
                                 allow_provider_unavailable=allow_unavailable) == common
    assert changed


@pytest.mark.parametrize(('failure', 'expected_calls'), [
    ('provider_settings_changed', 3),
    ('provider_settings_pending', 3),
    ('provider_authority_invalid', 1),
])
@pytest.mark.parametrize('allow_unavailable', [False, True])
def test_runtime_config_retry_is_bounded_and_never_defaults_open(
        tmp_path, monkeypatch, failure, expected_calls, allow_unavailable):
    calls = []

    def fail_reader(*args, **kwargs):
        calls.append(True)
        raise ValueError(failure)

    monkeypatch.setattr(mcs_util, '_provider_config_active', lambda path: True)
    reader = 'read_with_verified_common' if allow_unavailable else 'read'
    monkeypatch.setattr(settings, reader, fail_reader)
    with pytest.raises(mcs_util.ProviderSettingsUnreadable):
        mcs_util._load_config(str(tmp_path / 'config.json'), allow_provider_unavailable=allow_unavailable)
    assert len(calls) == expected_calls


@pytest.mark.parametrize('scan_number', [1, 2])
@pytest.mark.parametrize('allow_unavailable', [False, True])
def test_legacy_config_retries_unrelated_atomic_data_update(
        tmp_path, monkeypatch, scan_number, allow_unavailable):
    common, data, _ = tree(tmp_path)
    original = mcs_util._provider_artifact_present
    scans = []

    def scan_then_update(fd):
        present = original(fd)
        scans.append(True)
        if len(scans) == scan_number:
            temp = data / 'synthetic-runtime.tmp'
            temp.write_text('synthetic')
            os.replace(temp, data / 'synthetic-runtime.json')
        return present

    monkeypatch.setattr(mcs_util, '_provider_artifact_present', scan_then_update)
    assert mcs_util._load_config(str(tmp_path / 'config.json'),
                                allow_provider_unavailable=allow_unavailable) == common
    assert len(scans) == 3


@pytest.mark.parametrize('allow_unavailable', [False, True])
def test_legacy_config_tolerates_repeated_unrelated_data_changes(tmp_path, monkeypatch, allow_unavailable):
    """Unrelated data/ entries move the directory's times without changing
    the provider decision; sustained churn must not make config unreadable."""
    common, data, _ = tree(tmp_path)
    scans = []

    def scan_then_update(fd):
        scans.append(True)
        temp = data / 'synthetic-runtime.tmp'
        temp.write_text('synthetic')
        os.replace(temp, data / 'synthetic-runtime.json')
        return False

    monkeypatch.setattr(mcs_util, '_provider_artifact_present', scan_then_update)
    assert mcs_util._load_config(str(tmp_path / 'config.json'),
                                allow_provider_unavailable=allow_unavailable) == common
    assert len(scans) >= 2


@pytest.mark.parametrize('scan_number', [1, 2])
@pytest.mark.parametrize('marker', ['.provider-settings-pending.json', '.provider-settings-authority.json',
                                  'provider_settings', '.provider-settings-backup-synthetic'])
@pytest.mark.parametrize('allow_unavailable', [False, True])
def test_legacy_config_never_ignores_provider_state_created_during_scan(
        tmp_path, monkeypatch, scan_number, marker, allow_unavailable):
    _, data, _ = tree(tmp_path)
    original = mcs_util._provider_artifact_present
    scans = []

    def scan_then_create(fd):
        present = original(fd)
        scans.append(True)
        if len(scans) == scan_number:
            if marker == 'provider_settings':
                (data / marker).mkdir(mode=0o755)
            else:
                private_json(data / marker, {})
        return present

    monkeypatch.setattr(mcs_util, '_provider_artifact_present', scan_then_create)
    with pytest.raises(mcs_util.ProviderSettingsUnreadable):
        mcs_util._load_config(str(tmp_path / 'config.json'), allow_provider_unavailable=allow_unavailable)
    assert len(scans) <= 3


@pytest.mark.parametrize('failure', ['provider_artifact_scan_limit', 'provider_directory_invalid'])
def test_legacy_config_does_not_retry_other_scan_failures(tmp_path, monkeypatch, failure):
    tree(tmp_path)
    scans = []

    def fail_scan(fd):
        scans.append(True)
        raise ValueError(failure)

    monkeypatch.setattr(mcs_util, '_provider_artifact_present', fail_scan)
    with pytest.raises(mcs_util.ProviderSettingsUnreadable) as error:
        mcs_util.load_config(str(tmp_path / 'config.json'))
    assert str(error.value.__cause__) == failure
    assert len(scans) == 1


@pytest.mark.parametrize('change', ['replacement', 'permissions', 'ownership'])
def test_legacy_config_never_rebaselines_directory_identity_change(tmp_path, monkeypatch, change):
    _, data, _ = tree(tmp_path)
    original = mcs_util._provider_artifact_present
    fstat = os.fstat
    inode = data.stat().st_ino
    scans = []

    def scan_then_change(fd):
        present = original(fd)
        scans.append(True)
        if change == 'replacement':
            data.rename(tmp_path / 'synthetic-old-data')
            data.mkdir(mode=0o700)
        elif change == 'permissions':
            data.chmod(0o777)
        return present

    def changed_owner(fd):
        info = fstat(fd)
        if change == 'ownership' and info.st_ino == inode and scans:
            return SimpleNamespace(**{name: getattr(info, name) for name in (
                'st_dev', 'st_ino', 'st_mode', 'st_mtime_ns', 'st_ctime_ns')}, st_uid=info.st_uid + 1)
        return info

    monkeypatch.setattr(mcs_util, '_provider_artifact_present', scan_then_change)
    monkeypatch.setattr(mcs_util.os, 'fstat', changed_owner)
    with pytest.raises(mcs_util.ProviderSettingsUnreadable):
        mcs_util.load_config(str(tmp_path / 'config.json'))
    assert len(scans) == 1


@pytest.mark.parametrize('change', ['replacement', 'permissions', 'ownership', 'missing'])
def test_legacy_config_preserves_directory_identity_between_retry_attempts(tmp_path, monkeypatch, change):
    _, data, _ = tree(tmp_path)
    scan, fstat, stat, close = mcs_util._provider_artifact_present, os.fstat, os.stat, os.close
    inode = data.stat().st_ino
    scans, changed = [], []

    def scan_then_update(fd):
        present = scan(fd)
        scans.append(True)
        if len(scans) == 1:
            temp = data / 'synthetic-runtime.tmp'
            temp.write_text('synthetic')
            os.replace(temp, data / 'synthetic-runtime.json')
        return present

    def close_then_change(fd):
        info = fstat(fd)
        close(fd)
        if info.st_ino != inode or changed:
            return
        changed.append(True)
        if change in ('replacement', 'missing'):
            data.rename(tmp_path / 'synthetic-old-data')
            if change == 'replacement':
                data.mkdir(mode=0o700)
        elif change == 'permissions':
            data.chmod(0o777)

    def owner(info):
        if change == 'ownership' and changed and info.st_ino == inode:
            values = {name: getattr(info, name) for name in dir(info) if name.startswith('st_')}
            values['st_uid'] += 1
            return SimpleNamespace(**values)
        return info

    monkeypatch.setattr(mcs_util, '_provider_artifact_present', scan_then_update)
    monkeypatch.setattr(mcs_util.os, 'close', close_then_change)
    monkeypatch.setattr(mcs_util.os, 'fstat', lambda fd: owner(fstat(fd)))
    monkeypatch.setattr(mcs_util.os, 'stat', lambda *args, **kwargs: owner(stat(*args, **kwargs)))
    with pytest.raises(mcs_util.ProviderSettingsUnreadable) as error:
        mcs_util.load_config(str(tmp_path / 'config.json'))
    assert str(error.value.__cause__) == 'provider_settings_changed'
    assert changed and len(scans) == 1


def test_provider_unavailable_view_is_marked_so_it_is_held_not_revoked():
    view = mcs_util._paid_routes_closed({'cloud_llm': {'mode': 'on', 'project_ids': [1]}})
    assert view['cloud_llm']['mode'] == 'off' and view['provider_unavailable'] is True
    # a synthesized off proves nothing about an existing unit's scope
    assert cloud_policy.scope_revoked(view, 'reextract', 1) is False
    explicit = {'cloud_llm': {'mode': 'off'}}
    assert cloud_policy.scope_revoked(explicit, 'reextract', 1) is True


def test_provider_unavailable_off_is_not_classified_as_the_owners_mode_off(monkeypatch):
    view = mcs_util._paid_routes_closed({'cloud_llm': {'mode': 'on', 'project_ids': [1]}})
    monkeypatch.setattr(cloud_policy, 'resolve_selection', lambda cfg, root=None: ({'mode': 'off'}, [], None))
    monkeypatch.setattr(cloud_policy, 'selected_key_problem', lambda root, cfg: None)
    assert cloud_policy.refusal_code(view, 'scope', model='m') == 'unknown'
    explicit = {'cloud_llm': {'mode': 'off'}}
    assert cloud_policy.refusal_code(explicit, 'scope', model='m') == 'mode_off'


def _committed_with_stale_pending(tmp_path):
    """A commit that published every file and the authority, then lost power before unlinking pending."""
    import config_edit

    _fresh_root(tmp_path, {})
    with config_edit._edit_lock(tmp_path / "config.json") as fd:
        legacy = settings.read(tmp_path, model_owners=OWNERS)
        initial = settings.bootstrap_locked(tmp_path, legacy["snapshot"], {}, model_owners=OWNERS, lock_fd=fd)
        settings.commit_locked(tmp_path, initial["snapshot"], {"mcs_login_id": "synthetic", "notify_target": "local"},
                               initial["providers"], selected_provider="anthropic", model_owners=OWNERS, lock_fd=fd)
    data = tmp_path / "data"

    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    after = {"config": digest(tmp_path / "config.json"),
             **{name: digest(data / "provider_settings" / (name + ".json")) for name in settings.PROVIDERS}}
    operation = "0123456789abcdef0123456789abcdef"
    private_json(data / ".provider-settings-pending.json",
                 {"version": 1, "operation": operation, "before": dict.fromkeys(after), "after": after,
                  "authority_before": None})
    assert settings.probe(tmp_path)["state"] == "pending"
    return operation, after


def test_committed_pending_accepts_only_a_fully_published_commit(tmp_path):
    """GA-provider-settings-writer-1: the exit exists only for the all-"after" state."""
    import config_edit

    operation, after = _committed_with_stale_pending(tmp_path)
    with config_edit._edit_lock(tmp_path / "config.json") as fd:
        assert settings.committed_pending(tmp_path, fd) == operation
    pending = tmp_path / "data" / ".provider-settings-pending.json"
    private_json(pending, {"version": 1, "operation": operation, "before": dict.fromkeys(after),
                           "after": {**after, "openai": "0" * 64}, "authority_before": None})
    with config_edit._edit_lock(tmp_path / "config.json") as fd:
        with pytest.raises(ValueError, match="provider_pending_not_committed"):
            settings.committed_pending(tmp_path, fd)


def test_human_confirmed_finalize_sets_the_pending_record_aside(tmp_path, monkeypatch):
    import uuid
    from contextlib import closing
    import mcs_requests
    from ledger import Ledger

    operation, _after = _committed_with_stale_pending(tmp_path)
    monkeypatch.setattr(cloud_policy, "PROVIDERS", {name: model for model, name in OWNERS.items()})
    req = {"version": 1, "cmd": "ops.provider_pending_finalize", "command_id": str(uuid.uuid4()),
           "actor": "synthetic-human", "human_confirmed": True, "project_id": None,
           "reason": "synthetic: compared hashes by hand", "operation": operation}
    assert mcs_requests.validate(req) is None
    with closing(Ledger(str(tmp_path / "data" / "ledger.db"))) as ledger:
        wrong = mcs_requests.apply_command(ledger, {**req, "command_id": str(uuid.uuid4()), "operation": "f" * 32})
        assert (wrong["outcome"], wrong["error"]) == ("rejected", "provider_pending_changed")
        receipt = mcs_requests.apply_command(ledger, req)
    assert receipt["outcome"] == "applied", receipt
    data = tmp_path / "data"
    assert not (data / ".provider-settings-pending.json").exists()
    assert (data / f".provider-settings-pending.resolved-{operation}.json").exists()
    assert settings.read(tmp_path, model_owners=OWNERS)["mode"] == "split"


def _recorded_commit(tmp_path, monkeypatch, original, **kwargs):
    monkeypatch.setattr(mcs_config, "validate_config", lambda _: ([], []))
    return settings.commit(tmp_path, original["snapshot"], original["common"], original["providers"],
                           selected_provider="openai", model_owners=OWNERS, **kwargs)


def _record_paths(tmp_path):
    return sorted((tmp_path / "data").glob(".provider-settings-stage-*/" + settings.CHANGE_RECORD))


def test_commit_records_origin_hashes_and_limit_changes_privately(tmp_path, monkeypatch):
    import stat
    from semantic_runtime import CLOUD_OPERATING_LIMITS

    tree(tmp_path, split=True)
    original = settings.read(tmp_path)
    write_new, pending = settings._write_new, []
    def capture(parent, name, raw, **kwargs):
        if name == ".provider-settings-pending.json":
            pending.append(json.loads(raw))
        return write_new(parent, name, raw, **kwargs)
    monkeypatch.setattr(settings, "_write_new", capture)
    original["common"]["cloud_llm"]["daily_cost_limit_usd"] = 0
    original["providers"]["openai"] = {"budget": {"daily_cost_limit_usd": 1, "monthly_cost_limit_usd": 2.5}}
    original["providers"]["jev"] = {"until": None}
    _recorded_commit(tmp_path, monkeypatch, original, origin="cloud_llm_budget")
    path, = _record_paths(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    record = json.loads(path.read_text())
    assert record["operation"] == pending[0]["operation"] == path.parent.name.rsplit("-", 1)[1]
    assert record["files"] == {"before": pending[0]["before"], "after": pending[0]["after"]}
    assert (record["version"], record["origin"], record["command_id"]) == (1, "cloud_llm_budget", None)
    before, after = record["limits"]["before"], record["limits"]["after"]
    assert set(after) == ({"cloud_llm." + key for key in CLOUD_OPERATING_LIMITS if key != "provider_budgets"}
                          | {f"cloud_llm.provider_budgets.{name}.{field}" for name in settings.CLOUD_PROVIDERS
                             for field in ("daily_cost_limit_usd", "monthly_cost_limit_usd")}
                          | {"semantic.daily_request_budget", "semantic.until"})
    assert record["changed"] == ["cloud_llm.daily_cost_limit_usd",
                                 "cloud_llm.provider_budgets.openai.daily_cost_limit_usd",
                                 "cloud_llm.provider_budgets.openai.monthly_cost_limit_usd", "semantic.until"]
    # absent falls back to a default; an explicit null is no limit: they stay distinct.
    assert before["semantic.until"] == "absent" and after["semantic.until"] is None
    assert (before["cloud_llm.daily_cost_limit_usd"], after["cloud_llm.daily_cost_limit_usd"]) == ("absent", 0)
    assert after["cloud_llm.provider_budgets.openai.monthly_cost_limit_usd"] == 2.5
    assert after["cloud_llm.provider_budgets.anthropic.daily_cost_limit_usd"] == "absent"
    authority = json.loads((tmp_path / "data" / ".provider-settings-authority.json").read_text())
    current = {"files": authority["files"], "common_policy_sha256": authority["common_policy_sha256"]}
    assert record["authority"]["after"] == current and record["authority"]["before"]["files"] == {
        name: pending[0]["before"][name] for name in settings.PROVIDERS}
    assert settings.change_records(tmp_path, 5) == {
        "records": [record], "current": current, "save_in_progress": False, "more": False,
        "unrecorded_saves": 0, "rejected_records": 0}


def test_a_save_without_limit_changes_is_recorded_and_reading_changes_nothing(tmp_path, monkeypatch):
    tree(tmp_path, split=True)
    original = settings.read(tmp_path)
    original["common"]["cloud_llm"]["mode"] = "on"
    _recorded_commit(tmp_path, monkeypatch, original, origin="settings")
    read_before, probe_before, files_before = settings.read(tmp_path), settings.probe(tmp_path), _tree_bytes(tmp_path)
    record, = settings.change_records(tmp_path, 1)["records"]
    assert record["origin"] == "settings" and record["changed"] == []
    assert record["limits"]["before"] == record["limits"]["after"]
    assert settings.read(tmp_path) == read_before and settings.probe(tmp_path) == probe_before
    assert probe_before["state"] == "requires_checked_read" and read_before["mode"] == "split"
    assert _tree_bytes(tmp_path) == files_before


@pytest.mark.parametrize(("origin", "command_id"), [
    ("synthetic", None), (None, None), (["settings"], None), ("jev_model", "synthetic"),
    ("jev_model", "123E4567-E89B-12D3-A456-426614174000")])
def test_invalid_origin_is_refused_before_any_write(tmp_path, monkeypatch, origin, command_id):
    tree(tmp_path, split=True)
    original = settings.read(tmp_path)
    before = _tree_bytes(tmp_path)
    original["common"]["cloud_llm"]["daily_cost_limit_usd"] = 0
    with pytest.raises(ValueError, match="provider_settings_origin_invalid"):
        _recorded_commit(tmp_path, monkeypatch, original, origin=origin, command_id=command_id)
    assert _tree_bytes(tmp_path) == before


def test_committed_pending_accepts_a_commit_stopped_after_its_record(tmp_path, monkeypatch):
    import config_edit

    tree(tmp_path, split=True)
    original = settings.read(tmp_path)
    original["providers"]["openai"] = {"model": "synthetic-openai"}
    unlink = os.unlink
    def crash(name, **kwargs):
        if name == ".provider-settings-pending.json":
            raise _Crash("synthetic interruption")
        return unlink(name, **kwargs)
    monkeypatch.setattr(settings.os, "unlink", crash)
    with pytest.raises(_Crash, match="synthetic interruption"):
        _recorded_commit(tmp_path, monkeypatch, original, origin="settings")
    monkeypatch.setattr(settings.os, "unlink", unlink)
    pending = json.loads((tmp_path / "data" / ".provider-settings-pending.json").read_text())
    record, = settings.change_records(tmp_path, 1)["records"]
    assert record["operation"] == pending["operation"] and record["files"]["after"] == pending["after"]
    with config_edit._edit_lock(tmp_path / "config.json") as fd:
        assert settings.committed_pending(tmp_path, fd) == pending["operation"]


def test_change_records_are_newest_first_and_refuse_links_oversized_and_altered(tmp_path, monkeypatch):
    tree(tmp_path, split=True)
    state = settings.read(tmp_path)
    for amount in (1, 2, 3):
        state["common"]["cloud_llm"]["daily_cost_limit_usd"] = amount
        state = _recorded_commit(tmp_path, monkeypatch, state, origin="cloud_llm_budget")
    listed = settings.change_records(tmp_path, 2)
    assert [record["limits"]["after"]["cloud_llm.daily_cost_limit_usd"] for record in listed["records"]] == [3, 2]
    assert listed["more"] is True and listed["records"][0]["authority"]["after"] == listed["current"]
    # Each record starts from the state the previous one saved, whatever the clocks say.
    assert listed["records"][0]["authority"]["before"] == listed["records"][1]["authority"]["after"]
    valid = _record_paths(tmp_path)[0]
    data = tmp_path / "data"
    def stage(letter):
        path = data / (".provider-settings-stage-" + letter * 32)
        path.mkdir(mode=0o700)
        return path
    stage("a")                                                        # a save from before records existed
    oversized = stage("b") / settings.CHANGE_RECORD
    oversized.write_bytes(b" " * (64 * 1024 + 1))
    oversized.chmod(0o600)
    (stage("c") / settings.CHANGE_RECORD).symlink_to(valid)
    (data / (".provider-settings-stage-" + "d" * 32)).symlink_to(valid.parent, target_is_directory=True)
    altered = json.loads(valid.read_text())
    altered.update(operation="e" * 32, note="synthetic free text")
    private_json(stage("e") / settings.CHANGE_RECORD, altered)
    huge = json.loads(valid.read_text())
    huge["operation"] = "f" * 32
    for side in ("before", "after"):        # an unchanged limit, so only the number is wrong
        huge["limits"][side]["cloud_llm.max_concurrency"] = 10 ** 400
    private_json(stage("f") / settings.CHANGE_RECORD, huge)
    listed = settings.change_records(tmp_path, settings.CHANGE_RECORDS_MAX)
    assert len(listed["records"]) == 3
    assert (listed["unrecorded_saves"], listed["rejected_records"]) == (1, 5)
    for limit in (0, settings.CHANGE_RECORDS_MAX + 1, True, "1"):
        with pytest.raises(ValueError, match="provider_record_limit_invalid"):
            settings.change_records(tmp_path, limit)


def test_record_build_failure_is_refused_before_any_write(tmp_path, monkeypatch):
    tree(tmp_path, split=True)
    original = settings.read(tmp_path)
    before = _tree_bytes(tmp_path)
    original["common"]["cloud_llm"]["daily_cost_limit_usd"] = 0
    def refused(*args):
        raise ValueError("provider_file_too_large")
    monkeypatch.setattr(settings, "_change_record", refused)
    with pytest.raises(ValueError, match="provider_file_too_large"):
        _recorded_commit(tmp_path, monkeypatch, original, origin="settings")
    assert _tree_bytes(tmp_path) == before


def test_reader_opens_only_the_newest_stages_and_reports_a_save_in_progress(tmp_path, monkeypatch):
    tree(tmp_path, split=True)
    state = settings.read(tmp_path)
    for amount in (1, 2):
        state["common"]["cloud_llm"]["daily_cost_limit_usd"] = amount
        state = _recorded_commit(tmp_path, monkeypatch, state, origin="cloud_llm_budget")
    newest, older = (path.parent for path in sorted(_record_paths(tmp_path), key=lambda p: p.parent.stat().st_mtime_ns,
                                                    reverse=True))
    # A clock step backwards reorders the stages; the hash chain still names the current save.
    os.utime(newest, ns=(1, 1))
    listed = settings.change_records(tmp_path, 2)
    assert [record["operation"] for record in listed["records"]] == [older.name[-32:], newest.name[-32:]]
    assert listed["records"][1]["authority"]["after"] == listed["current"]
    opened, directory = [], settings._directory
    monkeypatch.setattr(settings, "_directory", lambda parent, name, **kw: opened.append(name) or directory(
        parent, name, **kw))
    assert len(settings.change_records(tmp_path, 1)["records"]) == 1
    assert [name for name in opened if name.startswith(".provider-settings-stage-")] == [older.name]
    private_json(tmp_path / "data" / ".provider-settings-pending.json", {"synthetic": True})
    assert settings.change_records(tmp_path, 1)["save_in_progress"] is True


@pytest.mark.parametrize("value,expected", [
    ("2026-11-25t12:00:00+00:00", "2026-11-25t12:00:00+00:00"),   # a separator until_epoch accepts
    ("2026-11-25T12:00:00Z", "2026-11-25T12:00:00Z"),
    ("not a time at all", settings.INVALID),
    ("2026-13-45T99:00:00+00:00", settings.INVALID),
])
def test_record_keeps_every_time_until_epoch_accepts(value, expected):
    assert settings._limit_value(value) == expected
    assert settings._stored_limit_ok(expected)
