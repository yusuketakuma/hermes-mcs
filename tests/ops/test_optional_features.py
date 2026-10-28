"""Optional setup guidance: a release default that only unset choices follow, an
explicit choice that survives a default change, and no CSDC registration, consent
or sending implied by the choice (synthetic config only)."""
import ast
import json
import os
import stat
from pathlib import Path

import pytest

import config_edit
import mcs_config
import mcs_setup
import mcs_util
import safe_io
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
    assert "現在の選択: 案内を表示する" in out and "mcs setup features" in out


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


def test_an_unimplemented_feature_name_is_never_enabled():
    cfg = {"optional_features": {"future_unimplemented": True}}
    assert mcs_config.feature_enabled(cfg, "future_unimplemented") is False
    assert mcs_config.feature_enabled({}, "future_unimplemented") is False
    assert mcs_config.feature_enabled(cfg, "csdc") is False            # known name keeps its default


def _features_env(monkeypatch, tmp_path, initial):
    path = tmp_path / "config.json"
    if initial is not None:
        path.write_text(json.dumps(initial))
        path.chmod(0o600)
    monkeypatch.setattr(mcs_setup, "CONF_PATH", str(path))
    monkeypatch.setattr(mcs_setup, "_config_problem", lambda: None)
    monkeypatch.setattr(mcs_setup.mcs_util, "load_config",
                        lambda: json.loads(path.read_text()) if path.exists() else {})
    monkeypatch.setattr(mcs_setup.sys.stdin, "isatty", lambda: True)
    return path


def _during_the_prompt(monkeypatch, change):
    """Answer 'show', let another writer act while the save confirmation waits, then confirm."""
    replies = iter(["2", "1"])

    def answer(prompt):
        reply = next(replies)
        if "保存しますか" in prompt:
            change()
        return reply
    monkeypatch.setattr("builtins.input", answer)


@pytest.mark.parametrize("change", ["other_key", "same_feature", "secret_like", "sdc_flag"])
def test_features_never_overwrites_a_config_changed_while_asking(monkeypatch, tmp_path, change):
    initial = {"mcs_login_id": "synthetic", "notify_target": "local", "sdc": {"enabled": True}}
    path = _features_env(monkeypatch, tmp_path, initial)
    other = {"other_key": {**initial, "trickle_pages": 9},
             "same_feature": {**initial, "optional_features": {"csdc": False}},
             "secret_like": {**initial, "mcs_login_id": "synthetic-rotated"},
             "sdc_flag": {**initial, "sdc": {"enabled": False}}}[change]
    _during_the_prompt(monkeypatch, lambda: path.write_text(json.dumps(other)))
    assert mcs_setup.main(["features"]) == 1
    assert json.loads(path.read_text()) == other                    # the other writer's state is kept


def test_features_detects_a_cooperating_edit_during_the_prompt(monkeypatch, tmp_path):
    initial = {"mcs_login_id": "synthetic", "notify_target": "local", "keep": [1]}
    path = _features_env(monkeypatch, tmp_path, initial)
    monkeypatch.setattr(mcs_util, "CONF_PATH", str(path))
    _during_the_prompt(monkeypatch, lambda: config_edit.apply({"extract.prefilter": False}))
    assert mcs_setup.main(["features"]) == 1
    assert json.loads(path.read_text()) == {**initial, "extract": {"prefilter": False}}


def test_features_refuses_an_active_shared_lock(monkeypatch, tmp_path):
    initial = {"mcs_login_id": "synthetic", "notify_target": "local"}
    path = _features_env(monkeypatch, tmp_path, initial)
    _during_the_prompt(monkeypatch, lambda: None)
    with config_edit._edit_lock(path):
        assert mcs_setup.main(["features"]) == 1
    assert json.loads(path.read_text()) == initial


@pytest.mark.parametrize("failure", ["oversize", "invalid_lock"])
def test_features_reports_no_save_before_publication(monkeypatch, tmp_path, capsys, failure):
    initial = {"mcs_login_id": "synthetic", "notify_target": "local"}
    path = _features_env(monkeypatch, tmp_path, initial)
    raw = path.read_bytes()
    if failure == "oversize":
        monkeypatch.setattr(mcs_util, "CONFIG_MAX_BYTES", len(raw))
    else:
        lock = Path(str(path) + ".edit.lock")
        lock.write_text("synthetic nonempty lock")
        lock.chmod(0o600)
    _during_the_prompt(monkeypatch, lambda: None)
    assert mcs_setup.main(["features"]) == 1
    assert path.read_bytes() == raw
    out = capsys.readouterr().out
    assert "保存していません" in out
    assert "保存結果を確認できません" not in out


@pytest.mark.parametrize("initially_present", [True, False])
@pytest.mark.parametrize("error", [OSError, FileNotFoundError])
def test_features_reports_unknown_after_publication_fsync_error(monkeypatch, tmp_path, capsys,
                                                               initially_present, error):
    initial = {"mcs_login_id": "synthetic", "notify_target": "local"} if initially_present else None
    path = _features_env(monkeypatch, tmp_path, initial)
    if not initially_present:
        # Exercise exclusive publication independently of required-key validation.
        monkeypatch.setattr(mcs_setup, "validate_config", lambda cfg: ([], []))
    _during_the_prompt(monkeypatch, lambda: None)
    def fail_after_publication(*args, **kwargs):
        raise error("synthetic directory sync error")
    monkeypatch.setattr(safe_io, "fsync_dir", fail_after_publication)
    assert mcs_setup.main(["features"]) == 1
    assert json.loads(path.read_text())["optional_features"] == {"csdc": True}
    out = capsys.readouterr().out
    assert "保存結果を確認できません" in out
    assert "表示される現在の選択を確認" in out
    assert "保存していません" not in out and "変更しません" not in out


@pytest.mark.parametrize("change", ["deleted", "replaced_same_bytes", "corrupted"])
def test_features_aborts_when_the_config_is_removed_replaced_or_broken(monkeypatch, tmp_path, change):
    initial = {"mcs_login_id": "synthetic", "notify_target": "local"}
    path = _features_env(monkeypatch, tmp_path, initial)
    raw = path.read_bytes()
    after = {}

    def act():
        if change == "deleted":
            path.unlink()
        elif change == "replaced_same_bytes":
            fresh = tmp_path / "fresh.json"
            fresh.write_bytes(raw)
            fresh.replace(path)
        else:
            path.write_text("{broken")
        after["state"] = path.read_bytes() if path.exists() else None
    _during_the_prompt(monkeypatch, act)
    assert mcs_setup.main(["features"]) == 1
    assert (path.read_bytes() if path.exists() else None) == after["state"]   # left as the other writer left it


def test_features_aborts_when_a_config_appears_while_asking(monkeypatch, tmp_path):
    path = _features_env(monkeypatch, tmp_path, None)
    # an absent config never validates; isolate the guard from the required-key check
    monkeypatch.setattr(mcs_setup, "validate_config", lambda cfg: ([], []))
    created = {"mcs_login_id": "synthetic-new"}
    _during_the_prompt(monkeypatch, lambda: path.write_text(json.dumps(created)))
    assert mcs_setup.main(["features"]) == 1
    assert json.loads(path.read_text()) == created


@pytest.mark.parametrize("change", ["fifo", "symlink_same_bytes", "oversize"])
def test_features_rechecks_without_following_blocking_or_reading_unbounded(monkeypatch, tmp_path, change):
    initial = {"mcs_login_id": "synthetic", "notify_target": "local"}
    path = _features_env(monkeypatch, tmp_path, initial)
    raw = path.read_bytes()

    def act():
        if change == "fifo":                     # a blocking open would never return
            path.unlink()
            os.mkfifo(path)
        elif change == "symlink_same_bytes":
            target = tmp_path / "elsewhere.json"
            target.write_bytes(raw)
            path.unlink()
            path.symlink_to(target)
        else:
            path.write_bytes(raw + b" " * (mcs_util.CONFIG_MAX_BYTES + 1))
    _during_the_prompt(monkeypatch, act)
    assert mcs_setup.main(["features"]) == 1
    if change == "fifo":
        assert stat.S_ISFIFO(os.lstat(path).st_mode)
    elif change == "symlink_same_bytes":
        assert path.is_symlink() and (tmp_path / "elsewhere.json").read_bytes() == raw
    else:
        assert path.stat().st_size > mcs_util.CONFIG_MAX_BYTES


def test_features_never_saves_through_a_symlinked_config(monkeypatch, tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"mcs_login_id": "synthetic", "notify_target": "local"}))
    path = _features_env(monkeypatch, tmp_path, None)
    path.symlink_to(target)
    before = target.read_bytes()
    _during_the_prompt(monkeypatch, lambda: None)
    assert mcs_setup.main(["features"]) == 1
    assert path.is_symlink() and target.read_bytes() == before
