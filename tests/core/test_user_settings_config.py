"""The three former environment-only settings live in config.json (synthetic only)."""
import json
from types import SimpleNamespace

import pytest

import config_edit
import cloud_policy
import extract_llm
import llm_availability
import local_llm
import mcs_config
import mcs_util
import provider_settings
import semantic_jev
import semantic_policy
from mcs_config import validate_config


@pytest.fixture
def conf(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"mcs_login_id": "synthetic-login", "notify_target": "synthetic-target",
                               "notify": {"synthetic": True}}))
    monkeypatch.setattr(mcs_util, "CONF_PATH", str(path))
    for module in (mcs_util, extract_llm, llm_availability):
        monkeypatch.setattr(module, "load_config", lambda *a, **k: mcs_util.json.loads(path.read_text()), raising=False)
    for name in ("MCS_EXTRACT_PREFILTER", "MCS_DISK_GUARD_MB", "MCS_LLM_ADMISSION"):
        monkeypatch.delenv(name, raising=False)
    return path


def _set(**kw):
    return config_edit.settings_command(SimpleNamespace(prefilter=kw.get("prefilter"),
        disk_guard_mb=kw.get("guard"), admission=kw.get("admission")))


def test_defaults_without_config_or_environment(conf):
    assert extract_llm._prefilter_enabled() is True
    assert llm_availability.disk_floor_mb() == 512.0
    assert local_llm.admission_enabled() is False


def test_config_values_win_over_the_legacy_environment(conf, monkeypatch):
    monkeypatch.setenv("MCS_EXTRACT_PREFILTER", "on")
    monkeypatch.setenv("MCS_DISK_GUARD_MB", "100")
    monkeypatch.setenv("MCS_LLM_ADMISSION", "0")
    assert _set(prefilter="off", guard="2048", admission="on") == 0
    saved = json.loads(conf.read_text())
    assert saved["extract"] == {"prefilter": False}
    assert saved["local_llm"] == {"disk_guard_mb": 2048, "admission": True}
    assert saved["notify"] == {"synthetic": True} and (conf.stat().st_mode & 0o777) == 0o600
    assert extract_llm._prefilter_enabled() is False
    assert llm_availability.disk_floor_mb() == 2048.0
    assert local_llm.admission_enabled() is True
    assert local_llm._admission_db_path().endswith("llm_admission.db")


def test_the_legacy_environment_still_applies_when_config_is_silent(conf, monkeypatch):
    monkeypatch.setenv("MCS_EXTRACT_PREFILTER", "off")
    monkeypatch.setenv("MCS_DISK_GUARD_MB", "0")
    assert extract_llm._prefilter_enabled() is False and llm_availability.disk_floor_mb() == 0.0


@pytest.mark.parametrize("choice", [None, True, False])
def test_saved_choices_and_environment_fallback_survive_default_changes(conf, tmp_path, monkeypatch, choice):
    cfg = json.loads(conf.read_text())
    cfg["optional_features"] = {"future_feature": {"synthetic": [False, None, 2]}}
    cfg["future_extension"] = {"keep": ["synthetic", False]}
    if choice is not None:
        cfg["optional_features"]["csdc"] = choice
    conf.write_text(json.dumps(cfg))
    broker = str(tmp_path / "synthetic-broker.db")
    monkeypatch.setenv("MCS_EXTRACT_PREFILTER", "off")
    monkeypatch.setenv("MCS_DISK_GUARD_MB", "1234")
    monkeypatch.setenv("MCS_LLM_ADMISSION", broker)

    # An unrelated validated edit must not pin absent release defaults or
    # erase unknown settings and the existing environment fallback.
    saved, errors = config_edit.apply({"extract.patient_state_context": False})
    assert not errors
    assert saved["optional_features"] == cfg["optional_features"]
    assert saved["future_extension"] == cfg["future_extension"]
    assert "prefilter" not in saved["extract"] and "local_llm" not in saved
    assert extract_llm._prefilter_enabled() is False
    assert llm_availability.disk_floor_mb() == 1234.0
    assert local_llm.admission_enabled() is True
    assert local_llm._admission_db_path() == broker

    stored = conf.read_bytes()
    for release_default in (False, True):
        monkeypatch.setitem(mcs_config.OPTIONAL_FEATURE_DEFAULTS, "csdc", release_default)
        loaded = json.loads(conf.read_text())
        assert mcs_config.feature_enabled(loaded, "csdc") is (
            release_default if choice is None else choice)
        assert conf.read_bytes() == stored


@pytest.mark.parametrize("kw", [{"prefilter": "maybe"}, {"guard": "-1"}, {"guard": "nan"}, {"admission": "1"}])
def test_invalid_values_are_refused_without_writing(conf, kw):
    before = conf.read_text()
    assert _set(**kw) == 1 and conf.read_text() == before


def test_an_unreadable_config_is_never_overwritten(conf):
    conf.write_text("not json")
    assert _set(prefilter="off") == 1 and conf.read_text() == "not json"


@pytest.mark.parametrize("cfg,field", [({"extract": {"prefilter": "on"}}, "extract.prefilter"),
                                       ({"local_llm": {"disk_guard_mb": -5}}, "local_llm.disk_guard_mb"),
                                       ({"local_llm": {"admission": 1}}, "local_llm.admission")])
def test_the_validator_rejects_wrong_types(cfg, field):
    errors, _ = validate_config(cfg)
    assert any(e.startswith(field) for e in errors)


@pytest.mark.parametrize("value", [10**400, float("nan"), float("inf"), -float("inf"), True])
def test_nonfinite_and_huge_disk_values_are_rejected_and_runtime_falls_back(conf, monkeypatch, value):
    cfg = {"local_llm": {"disk_guard_mb": value}}
    assert any(e.startswith("local_llm.disk_guard_mb") for e in validate_config(cfg)[0])
    monkeypatch.setattr(llm_availability, "load_config", lambda: cfg)
    monkeypatch.setenv("MCS_DISK_GUARD_MB", "321")
    assert llm_availability.disk_floor_mb() == 321.0


@pytest.mark.parametrize("section", [None, 0, 5, [], [["admission", True]], "synthetic"])
def test_bad_section_shape_is_not_coerced_or_written(conf, section):
    cfg = json.loads(conf.read_text())
    cfg["local_llm"] = section
    conf.write_text(json.dumps(cfg))
    before = conf.read_bytes()
    assert _set(admission="on") == 1
    assert conf.read_bytes() == before


def test_unrelated_invalid_config_blocks_settings_save_without_leaking_values(conf, capsys):
    cfg = json.loads(conf.read_text())
    cfg["notify_target"] = []
    cfg["extension"] = {"secret": "SYNTHETIC_PRIVATE_MARKER"}
    conf.write_text(json.dumps(cfg))
    before = conf.read_bytes()
    assert _set(prefilter="off") == 1
    assert conf.read_bytes() == before
    assert "SYNTHETIC_PRIVATE_MARKER" not in capsys.readouterr().out


def test_unknown_config_keys_are_preserved(conf, capsys):
    cfg = json.loads(conf.read_text())
    cfg["future_extension"] = {"secret": "SYNTHETIC_PRIVATE_MARKER", "flags": [False, None, 2]}
    cfg["local_llm"] = {"future_option": {"keep": True}}
    conf.write_text(json.dumps(cfg))
    assert _set(prefilter="off", guard="0") == 0
    saved = json.loads(conf.read_text())
    assert saved["future_extension"] == cfg["future_extension"]
    assert saved["local_llm"]["future_option"] == {"keep": True}
    assert "SYNTHETIC_PRIVATE_MARKER" not in capsys.readouterr().out


@pytest.mark.parametrize("semantic", [
    {}, {"mode": "off", "project_ids": [], "daily_request_budget": 0, "until": None},
    {"mode": "assist", "project_ids": None, "daily_request_budget": 0,
     "model": semantic_jev.JEV_MODEL},
])
def test_provider_split_preserves_user_settings_and_jev_defaults(semantic):
    raw = {"cloud_llm": {"provider": "openai", "mode": "off", "project_ids": [],
                         "provider_budgets": {"openai": {"daily_cost_limit_usd": 0,
                                                          "monthly_cost_limit_usd": 0}}},
           "semantic": semantic, "extract": {"prefilter": False},
           "local_llm": {"disk_guard_mb": 0, "admission": False}, "future_setting": {"synthetic": None}}
    owners = {model: name for name, model in cloud_policy.PROVIDERS.items()}
    common, providers = provider_settings.split_legacy(raw, owners, jev_model=semantic_jev.JEV_MODEL)
    rebuilt = provider_settings.compose(common, providers, selected_provider="openai",
                                        model_owners=owners, jev_model=semantic_jev.JEV_MODEL)
    assert rebuilt == raw
    assert semantic_policy.semantic_config(rebuilt, key_present=True) == semantic_policy.semantic_config(raw, key_present=True)
    assert cloud_policy.cloud_config(rebuilt, key_present=True) == cloud_policy.cloud_config(raw, key_present=True)


def test_provider_failure_keeps_local_user_settings_for_existing_consumers(tmp_path, monkeypatch):
    import hashlib

    tmp_path.chmod(0o700)
    common = {
        "cloud_llm": {"mode": "off", "project_ids": []},
        "semantic": {"mode": "off"},
        "extract": {"prefilter": False},
        "local_llm": {"disk_guard_mb": 2048, "admission": False},
        "notify_target": "synthetic-target", "notify": {"synthetic": True},
        "future_setting": {"synthetic": None},
    }

    def write_json(path, value):
        raw = (json.dumps(value, sort_keys=True) + "\n").encode()
        path.write_bytes(raw)
        path.chmod(0o600)
        return hashlib.sha256(raw).hexdigest()

    config = tmp_path / "config.json"
    write_json(config, common)
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    directory = data / "provider_settings"
    directory.mkdir(mode=0o700)
    hashes = {name: write_json(directory / (name + ".json"), {}) for name in provider_settings.PROVIDERS}
    write_json(data / ".provider-settings-authority.json", {
        "version": 2, "state": "committed", "files": hashes,
        "common_policy_sha256": provider_settings.common_policy_digest(common),
    })
    target = directory / "openai.json"
    original = target.read_bytes()
    target.write_bytes(b"{synthetic-invalid")
    real_load_config = mcs_util.load_config
    def load_local():
        return real_load_config(str(config))

    monkeypatch.setattr(extract_llm, "load_config", load_local)
    monkeypatch.setattr(llm_availability, "load_config", load_local)
    monkeypatch.setattr(mcs_util, "load_config", load_local)
    monkeypatch.setenv("MCS_EXTRACT_PREFILTER", "on")
    monkeypatch.setenv("MCS_DISK_GUARD_MB", "100")
    monkeypatch.setenv("MCS_LLM_ADMISSION", "1")
    assert mcs_util.load_config_checked(str(config)) is None
    assert extract_llm._prefilter_enabled() is False
    assert llm_availability.disk_floor_mb() == 2048.0
    assert local_llm.admission_enabled() is False
    loaded = load_local()
    assert loaded["notify_target"] == common["notify_target"]
    assert loaded["notify"] == common["notify"]
    assert loaded["future_setting"] == common["future_setting"]
    target.write_bytes(original)
    target.chmod(0o600)
    assert mcs_util.load_config_checked(str(config)) is not None
    assert extract_llm._prefilter_enabled() is False
    assert llm_availability.disk_floor_mb() == 2048.0
