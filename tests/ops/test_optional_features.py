"""Optional setup guidance: a release default that only unset choices follow, an
explicit choice that survives a default change, and no CSDC registration, consent
or sending implied by the choice (synthetic config only)."""
import ast
import json
from pathlib import Path

import pytest

import mcs_config
import mcs_setup
import setup_wizard


@pytest.mark.parametrize("value,problem", [
    ({}, False), ({"csdc": True}, False), ({"csdc": False}, False),
    ({"later_feature": 1}, False),                     # rollback-safe: warning only
    ([], True), ("on", True), ({"csdc": 1}, True), ({"csdc": None}, True), ({"csdc": "true"}, True),
])
def test_optional_features_validation(value, problem):
    errors, warnings = mcs_config.validate_config({"optional_features": value})
    assert any(e.startswith("optional_features") for e in errors) is problem
    if value == {"later_feature": 1}:
        assert any("optional_features.later_feature" in w for w in warnings)


@pytest.mark.parametrize("cfg,expected", [
    ({}, False), ({"optional_features": {}}, False),
    ({"optional_features": {"csdc": True}}, True), ({"optional_features": {"csdc": False}}, False),
    ({"optional_features": {"csdc": "yes"}}, False), ({"optional_features": []}, False),
])
def test_unset_follows_the_release_default_and_explicit_wins(cfg, expected, monkeypatch):
    assert mcs_config.feature_enabled(cfg, "csdc") is expected
    monkeypatch.setitem(mcs_config.OPTIONAL_FEATURE_DEFAULTS, "csdc", True)    # a later release
    explicit = cfg.get("optional_features", {}).get("csdc") if isinstance(cfg.get("optional_features"), dict) else None
    assert mcs_config.feature_enabled(cfg, "csdc") is (explicit if type(explicit) is bool else True)


def _choose(monkeypatch, cfg, answer):
    monkeypatch.setattr("builtins.input", lambda *_a: answer)
    setup_wizard.wizard_optional_features(cfg, interactive=True)
    return cfg


@pytest.mark.parametrize("answer,stored", [("2", {"csdc": True}), ("show", {"csdc": True}),
                                           ("3", {"csdc": False}), ("omit", {"csdc": False})])
def test_explicit_choices_are_stored_and_other_settings_kept(monkeypatch, answer, stored):
    cfg = {"mcs_login_id": "synthetic", "sdc": {"state_root": "/synthetic"}, "unknown_key": 1}
    _choose(monkeypatch, cfg, answer)
    assert cfg == {"mcs_login_id": "synthetic", "sdc": {"state_root": "/synthetic"}, "unknown_key": 1,
                   "optional_features": stored}


def test_following_the_default_removes_only_the_explicit_key(monkeypatch):
    cfg = {"optional_features": {"csdc": True, "later_feature": False}, "sdc": {"enabled": True}}
    _choose(monkeypatch, cfg, "1")
    assert cfg == {"optional_features": {"later_feature": False}, "sdc": {"enabled": True}}
    cfg = {"optional_features": {"csdc": False}}
    _choose(monkeypatch, cfg, "follow")
    assert cfg == {}


def test_enter_keeps_the_current_choice(monkeypatch):
    for current in ({}, {"optional_features": {"csdc": False}}, {"optional_features": {"csdc": True}}):
        cfg = json.loads(json.dumps(current))
        _choose(monkeypatch, cfg, "")
        assert cfg == current


def test_without_a_terminal_nothing_is_asked_or_written(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda *_a: pytest.fail("input question"))
    cfg = {"optional_features": {"csdc": True}}
    setup_wizard.wizard_optional_features(cfg, interactive=False)
    assert cfg == {"optional_features": {"csdc": True}}
    out = capsys.readouterr().out
    assert "現在の選択: 案内を表示する" in out and "mcs_setup.py features" in out


def test_the_csdc_guidance_runs_only_when_enabled_after_interactive_success():
    tree = ast.parse(Path(mcs_setup.__file__).read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "cmd_init")
    guards = [n for n in ast.walk(fn) if isinstance(n, ast.If)
              and ast.unparse(n.test) == "feature_enabled(cfg, 'csdc')"
              and any(isinstance(c, ast.Call) and ast.unparse(c.func) == "setup_wizard.offer_sdc_enrollment"
                      for c in ast.walk(n))]
    assert len(guards) == 1
    calls = [c for c in ast.walk(fn) if isinstance(c, ast.Call)
             and ast.unparse(c.func) == "setup_wizard.offer_sdc_enrollment"]
    assert len(calls) == 1                         # no unguarded path to the CSDC guidance


def test_doctor_reports_the_choice_not_a_registration_state():
    assert mcs_setup._doctor_optional_features({}) == {
        "csdc": {"choice": "default", "guidance_shown": False, "release_default": False}}
    assert mcs_setup._doctor_optional_features({"optional_features": {"csdc": True}})["csdc"] == {
        "choice": "explicit", "guidance_shown": True, "release_default": False}


def test_features_command_saves_only_after_confirmation(monkeypatch, tmp_path, capsys):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"mcs_login_id": "synthetic", "notify_target": "local", "keep": [1]}))
    monkeypatch.setattr(mcs_setup, "CONF_PATH", str(path))
    monkeypatch.setattr(mcs_setup, "_config_problem", lambda: None)
    monkeypatch.setattr(mcs_setup.mcs_util, "load_config", lambda: json.loads(path.read_text()))
    monkeypatch.setattr(mcs_setup.sys.stdin, "isatty", lambda: True)
    replies = iter(["2", "2"])                       # show, then decline saving
    monkeypatch.setattr("builtins.input", lambda *_a: next(replies))
    assert mcs_setup.main(["features"]) == 130
    assert "optional_features" not in json.loads(path.read_text())
    replies = iter(["2", "1"])                       # show, then save
    monkeypatch.setattr("builtins.input", lambda *_a: next(replies))
    assert mcs_setup.main(["features"]) == 0
    assert json.loads(path.read_text()) == {"mcs_login_id": "synthetic", "notify_target": "local",
                                            "keep": [1], "optional_features": {"csdc": True}}
    assert path.stat().st_mode & 0o777 == 0o600


def test_features_command_refuses_without_a_terminal(monkeypatch, tmp_path):
    monkeypatch.setattr(mcs_setup, "_config_problem", lambda: None)
    monkeypatch.setattr(mcs_setup.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda *_a: pytest.fail("input question"))
    assert mcs_setup.main(["features"]) == 1
