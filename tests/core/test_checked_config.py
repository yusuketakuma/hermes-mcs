"""Checked local config preserves the existing strict reader's safety contract."""
import hashlib
import json
import os

import pytest

import cloud_policy
import mcs_util
import openai_client
import provider_settings
import semantic_jev
import semantic_policy


def _private_json(path, value):
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    return hashlib.sha256(raw).hexdigest()


def _split_root(tmp_path, monkeypatch, common, providers, keys=()):
    tmp_path.chmod(0o700)
    monkeypatch.setattr(mcs_util, "HOME", str(tmp_path))
    monkeypatch.setattr(cloud_policy, "provider_key", lambda name, root=None: "synthetic" if name in keys else None)
    monkeypatch.setattr(semantic_jev, "api_key", lambda root=None: "synthetic")
    config = tmp_path / "config.json"
    _private_json(config, common)
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    directory = data / "provider_settings"
    directory.mkdir(mode=0o700)
    hashes = {name: _private_json(directory / (name + ".json"), providers.get(name, {}))
              for name in provider_settings.PROVIDERS}
    _private_json(data / ".provider-settings-authority.json", {
        "version": 2, "state": "committed", "files": hashes,
        "common_policy_sha256": provider_settings.common_policy_digest(common)})
    return config, data, directory


def _assert_unavailable_never_enables_external_routes(path):
    try:
        config = mcs_util.load_config(str(path))
    except (ValueError, RuntimeError, OSError):
        return
    assert isinstance(config, dict) and config != {}
    semantic, _ = semantic_policy.runtime_config(config, root=str(path.parent))
    cloud, _ = cloud_policy.resolve_settings(config, root=str(path.parent))
    assert semantic["mode"] == "off" and cloud["mode"] == "off"


def test_checked_config_distinguishes_empty_object_from_missing(tmp_path):
    path = tmp_path / "config.json"
    assert mcs_util.load_config_checked(str(path)) is None
    assert mcs_util.load_config(str(path)) == {}
    path.write_text("{}", encoding="utf-8")
    assert mcs_util.load_config_checked(str(path)) == {}
    assert mcs_util.load_config(str(path)) == {}
    path.write_text('{"mode":"synthetic", "nested":{"enabled":true}}', encoding="utf-8")
    expected = {"mode": "synthetic", "nested": {"enabled": True}}
    assert mcs_util.load_config_checked(str(path)) == expected
    assert mcs_util.load_config(str(path)) == expected


@pytest.mark.parametrize("remnant", [".provider-settings-backup-synthetic-config.bin",
                                     ".provider-settings-stage-synthetic"])
def test_provider_remnant_without_authority_never_enables_paid_routes(tmp_path, monkeypatch, remnant):
    tmp_path.chmod(0o700)
    config = tmp_path / "config.json"
    _private_json(config, {"cloud_llm": {"mode": "on", "project_ids": [1]}, "semantic": {"mode": "assist"}})
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    (data / remnant).symlink_to(data / "synthetic-missing")
    monkeypatch.setattr(cloud_policy, "provider_key", lambda *_args: pytest.fail("unverified settings read a key"))
    assert mcs_util.load_config_checked(str(config)) is None
    with pytest.raises(mcs_util.ProviderSettingsUnreadable):
        mcs_util.load_config(str(config))
    assert os.path.lexists(data / remnant)


def test_valid_split_with_retained_migration_remnants_stays_readable(tmp_path, monkeypatch):
    common = {"cloud_llm": {"mode": "off", "project_ids": []}, "semantic": {"mode": "off"}}
    config, data, _ = _split_root(tmp_path, monkeypatch, common, {})
    (data / ".provider-settings-backup-synthetic-config.bin").write_text("synthetic witness")
    (data / ".provider-settings-stage-synthetic").mkdir()
    assert mcs_util.load_config_checked(str(config)) == common


def test_provider_remnant_scan_failure_never_falls_back_to_legacy(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    config = tmp_path / "config.json"
    _private_json(config, {"cloud_llm": {"mode": "on", "project_ids": [1]}})
    (tmp_path / "data").mkdir(mode=0o700)
    def failed(_fd):
        raise OSError("synthetic private canary")
    monkeypatch.setattr(mcs_util, "_provider_artifact_present", failed)
    assert mcs_util.load_config_checked(str(config)) is None
    with pytest.raises(mcs_util.ProviderSettingsUnreadable):
        mcs_util.load_config(str(config))


@pytest.mark.parametrize("raw", [
    b'{"x":1,"x":2}', b'{"nested":{"x":1,"x":2}}', b'{"x":NaN}',
    b'{"x":Infinity}', b'{"x":-Infinity}', b'\xff', b'{',
    b'[]', b'null', b'true', b'123', b'"synthetic"',
])
def test_checked_config_rejects_invalid_or_non_object_without_details(tmp_path, capsys, raw):
    path = tmp_path / "config.json"
    path.write_bytes(raw)
    assert mcs_util.load_config_checked(str(path)) is None
    assert mcs_util.load_config(str(path)) == {}
    captured = capsys.readouterr()
    assert not captured.out and not captured.err


def test_checked_config_rejects_permissions_without_exposing_error(monkeypatch, capsys):
    def denied(_path, _limit):
        raise PermissionError("synthetic-secret error details")

    monkeypatch.setattr(mcs_util, "_read_local", denied)
    assert mcs_util.load_config_checked("synthetic-path") is None
    assert mcs_util.load_config("synthetic-path") == {}
    captured = capsys.readouterr()
    assert not captured.out and not captured.err


def test_checked_config_keeps_bounded_regular_file_rules(tmp_path, monkeypatch):
    monkeypatch.setattr(mcs_util, "CONFIG_MAX_BYTES", 8)
    path = tmp_path / "config.json"
    path.write_bytes(b'{"x": 1}')
    assert mcs_util.load_config_checked(str(path)) == {"x": 1}
    path.write_bytes(b'{"x": 12}')
    assert mcs_util.load_config_checked(str(path)) is None
    assert mcs_util.load_config(str(path)) == {}
    path.write_bytes(b"{}")
    link = tmp_path / "link.json"
    link.symlink_to(path)
    assert mcs_util.load_config_checked(str(link)) is None
    assert mcs_util.load_config(str(link)) == {}


def test_canonical_legacy_settings_keep_original_raw_values(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    monkeypatch.setattr(mcs_util, "HOME", str(tmp_path))
    config = tmp_path / "config.json"
    raw = {"semantic": {"mode": "off", "until": None}, "cloud_llm": {"project_ids": [], "mode": "off"},
           "future_setting": {"synthetic": 0}}
    _private_json(config, raw)
    assert mcs_util.load_config_checked(str(config)) == raw
    assert mcs_util.load_config(str(config)) == raw


def test_unrelated_json_is_not_overlaid_or_blocked_by_split_pending(tmp_path, monkeypatch):
    common = {"cloud_llm": {"mode": "off", "project_ids": []}, "semantic": {"mode": "off"}}
    _, data, _ = _split_root(tmp_path, monkeypatch, common, {})
    _private_json(data / ".provider-settings-pending.json", {"synthetic": True})
    other = data / "synthetic-health.json"
    raw = {"semantic": {"state": "synthetic"}, "count": 0}
    _private_json(other, raw)
    assert mcs_util.load_config_checked(str(other)) == raw
    assert mcs_util.load_config(str(other)) == raw


@pytest.mark.parametrize("damage", ["pending", "missing", "mismatch", "corrupt"])
def test_split_failure_never_becomes_empty_auto_config(tmp_path, monkeypatch, damage):
    common = {"cloud_llm": {"mode": "off", "project_ids": []}, "semantic": {"mode": "off"}}
    config, data, directory = _split_root(tmp_path, monkeypatch, common, {})
    target = directory / "openai.json"
    if damage == "pending":
        _private_json(data / ".provider-settings-pending.json", {"synthetic": True})
    elif damage == "missing":
        target.unlink()
    elif damage == "mismatch":
        _private_json(target, {"model": openai_client.MODEL})
    else:
        target.write_text("{")
    assert mcs_util.load_config_checked(str(config)) is None
    _assert_unavailable_never_enables_external_routes(config)


def test_disappearing_pending_does_not_turn_split_failure_into_empty_config(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    monkeypatch.setattr(mcs_util, "HOME", str(tmp_path))
    monkeypatch.setattr(cloud_policy, "provider_key", lambda name, root=None: "synthetic")
    monkeypatch.setattr(semantic_jev, "api_key", lambda root=None: "synthetic")
    config = tmp_path / "config.json"
    _private_json(config, {"semantic": {"mode": "off"}, "cloud_llm": {"mode": "off", "project_ids": []}})
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    pending = data / ".provider-settings-pending.json"
    _private_json(pending, {"synthetic": True})
    calls = []
    def interrupted(*args, **kwargs):
        calls.append(True)
        pending.unlink()
        raise ValueError("synthetic pending")
    monkeypatch.setattr(provider_settings, "read_with_verified_common", interrupted)
    _assert_unavailable_never_enables_external_routes(config)
    assert calls == [True]


def test_unreadable_provider_state_is_not_treated_as_legacy_absence(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    monkeypatch.setattr(mcs_util, "HOME", str(tmp_path))
    monkeypatch.setattr(cloud_policy, "provider_key", lambda name, root=None: "synthetic")
    monkeypatch.setattr(semantic_jev, "api_key", lambda root=None: "synthetic")
    config = tmp_path / "config.json"
    _private_json(config, {})
    (tmp_path / "data").mkdir(mode=0o700)
    original_stat, original_lstat = os.stat, os.lstat
    names = {".provider-settings-authority.json", ".provider-settings-pending.json", "provider_settings"}
    def deny_stat(path, *args, **kwargs):
        if os.path.basename(os.fspath(path)) in names:
            raise PermissionError("synthetic denied")
        return original_stat(path, *args, **kwargs)
    def deny_lstat(path, *args, **kwargs):
        if os.path.basename(os.fspath(path)) in names:
            raise PermissionError("synthetic denied")
        return original_lstat(path, *args, **kwargs)
    monkeypatch.setattr(os, "stat", deny_stat)
    monkeypatch.setattr(os, "lstat", deny_lstat)
    assert mcs_util.load_config_checked(str(config)) is None
    _assert_unavailable_never_enables_external_routes(config)


@pytest.mark.parametrize("keys,selected", [
    (("openai",), "openai"), (("anthropic",), "anthropic"),
    (("anthropic", "openai"), "anthropic"), ((), "anthropic"),
])
def test_split_auto_provider_model_is_accepted_after_selection(tmp_path, monkeypatch, keys, selected):
    common = {"cloud_llm": {"mode": "on", "project_ids": [1], "provider": "auto",
                            "provider_order": ["anthropic", "openai"]},
              "semantic": {"mode": "off"}}
    providers = {name: {"model": model} for name, model in cloud_policy.PROVIDERS.items()}
    config, _, _ = _split_root(tmp_path, monkeypatch, common, providers, keys)
    loaded = mcs_util.load_config_checked(str(config))
    assert loaded is not None
    effective, errors = cloud_policy.resolve_settings(loaded, str(tmp_path))
    assert not cloud_policy.blocking_errors(errors)
    assert effective["provider"] == selected and effective["model"] == cloud_policy.PROVIDERS[selected]


def test_split_missing_cost_cap_does_not_erase_other_settings(tmp_path, monkeypatch):
    common = {"cloud_llm": {"daily_cost_limit_usd": None, "monthly_cost_limit_usd": None},
              "semantic": {"mode": "off", "project_ids": []}, "runtime_mode": "standalone"}
    config, _, _ = _split_root(tmp_path, monkeypatch, common, {}, ("openai",))
    loaded = mcs_util.load_config_checked(str(config))
    assert loaded is not None and loaded["runtime_mode"] == "standalone"
    effective, errors = cloud_policy.resolve_settings(loaded, str(tmp_path))
    assert effective["mode"] == "off" and cloud_policy.COST_LIMIT_REQUIRED in errors
    assert not cloud_policy.blocking_errors(errors)


@pytest.mark.parametrize("provider", provider_settings.PROVIDERS)
@pytest.mark.parametrize("damage", ["missing", "malformed"])
def test_provider_failure_preserves_common_disables_paid_routes_and_recovers(
        tmp_path, monkeypatch, provider, damage):
    common = {
        "runtime_mode": "standalone", "notify_target": "synthetic-target",
        "notify": {"synthetic": True}, "extract": {"prefilter": False},
        "collection": {"synthetic": 3},
        "cloud_llm": {"mode": "on", "provider": "openai", "project_ids": [1],
                      "daily_cost_limit_usd": 1, "monthly_cost_limit_usd": 10},
        "semantic": {"mode": "assist", "project_ids": [1]},
    }
    providers = {"jev": {"daily_request_budget": 7}}
    config, _, directory = _split_root(tmp_path, monkeypatch, common, providers, ("openai",))
    before = mcs_util.load_config_checked(str(config))
    assert before is not None and before["semantic"]["daily_request_budget"] == 7
    target = directory / (provider + ".json")
    original = target.read_bytes()
    if damage == "missing":
        target.unlink()
    else:
        target.write_bytes(b"{synthetic-invalid")
    damaged = target.read_bytes() if target.exists() else None
    assert mcs_util.load_config_checked(str(config)) is None
    loaded = mcs_util.load_config(str(config))
    expected = dict(common)
    expected["cloud_llm"] = {**common["cloud_llm"], "mode": "off"}
    expected["semantic"] = {**common["semantic"], "daily_request_budget": 0}
    assert loaded == expected
    cloud, _ = cloud_policy.resolve_settings(loaded, root=str(tmp_path))
    semantic, _ = semantic_policy.runtime_config(loaded, root=str(tmp_path))
    assert cloud["mode"] == "off" and semantic["daily_request_budget"] == 0
    assert (target.read_bytes() if target.exists() else None) == damaged
    target.write_bytes(original)
    target.chmod(0o600)
    assert mcs_util.load_config_checked(str(config)) == before
    assert mcs_util.load_config(str(config)) == before


@pytest.mark.parametrize("damage", ["common", "authority", "pending"])
def test_common_or_authority_failure_is_not_a_provider_only_fallback(tmp_path, monkeypatch, damage):
    common = {"runtime_mode": "standalone", "notify_target": "synthetic-target",
              "cloud_llm": {"mode": "off", "project_ids": []}, "semantic": {"mode": "off"}}
    config, data, directory = _split_root(tmp_path, monkeypatch, common, {})
    provider = directory / "openai.json"
    provider_original = provider.read_bytes()
    provider.write_bytes(b"{synthetic-invalid")
    path = {"common": config, "authority": data / ".provider-settings-authority.json",
            "pending": data / ".provider-settings-pending.json"}[damage]
    original = path.read_bytes() if path.exists() else None
    if damage == "common":
        _private_json(path, {**common, "runtime_mode": "synthetic-changed"})
    else:
        _private_json(path, {})
    assert mcs_util.load_config_checked(str(config)) is None
    with pytest.raises(mcs_util.ProviderSettingsUnreadable):
        mcs_util.load_config(str(config))
    if original is None:
        path.unlink()
    else:
        path.write_bytes(original)
        path.chmod(0o600)
    assert mcs_util.load_config(str(config))["notify_target"] == "synthetic-target"
    provider.write_bytes(provider_original)
    provider.chmod(0o600)
    assert mcs_util.load_config_checked(str(config))["runtime_mode"] == "standalone"
