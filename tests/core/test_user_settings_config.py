"""The three former environment-only settings live in config.json (synthetic only)."""
import json
from types import SimpleNamespace

import pytest

import config_edit
import extract_llm
import llm_availability
import local_llm
import mcs_util
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
