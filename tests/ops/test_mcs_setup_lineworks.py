"""LINE WORKS setup accepts explicit scopes and rejects incomplete authority."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

import local_llm
import mcs_setup


def config():
    return {
        "mcs_login_id": "synthetic", "notify_target": "lineworks:room-synthetic",
        "notify": {"interactive": "lineworks", "lineworks": {
            "profile": "default", "application_id": "2000001", "team_id": "40029600",
            "channel_id": "room-synthetic", "allowed_user_ids": ["user-synthetic"],
            "project_ids": [101], "project_ids_auto": False,
        }},
    }


def test_accept_lineworks_and_existing_slack():
    assert mcs_setup.validate_config(config()) == ([], [])
    cfg = config()
    cfg["notify"] = {"interactive": "slack", "slack": {
        "profile": "default", "application_id": "A1", "team_id": "T1", "channel_id": "C1"}}
    assert mcs_setup.validate_config(cfg) == ([], [])
    assert mcs_setup._apply_plugin_integration(config(), SimpleNamespace()) is True


@pytest.mark.parametrize("key,value", [
    ("application_id", "../1"), ("team_id", "0"), ("application_id", "9" * 30),
    ("profile", "bad profile"), ("channel_id", "room/path"), ("channel_id", "room\n"),
    ("allowed_user_ids", []), ("allowed_user_ids", ["user/path"]),
    ("allowed_user_ids", [True]), ("project_ids", []), ("project_ids", [True]),
    ("project_ids", [-1]), ("project_ids", ["101"]), ("project_ids_auto", "true"),
])
def test_invalid_scope_fails_closed(key, value):
    cfg = config()
    cfg["notify"]["lineworks"][key] = value
    errors, _ = mcs_setup.validate_config(cfg)
    assert any(f"notify.lineworks.{key}:" in e for e in errors)


def test_missing_scope_and_explicit_auto_scope():
    cfg = config()
    del cfg["notify"]["lineworks"]
    assert any("notify.lineworks: required" in e for e in mcs_setup.validate_config(cfg)[0])
    cfg = config()
    cfg["notify"]["lineworks"].update(project_ids=[], project_ids_auto=True)
    assert mcs_setup.validate_config(cfg)[0] == []
    # The runtime adapter additionally requires a published scope snapshot.


def test_wizard_collects_authority_and_keeps_thread_settings_hidden():
    cfg = config()
    fields = {key: (kind, gate) for _, rows in mcs_setup.WIZARD
              for key, kind, _, _, gate in rows}
    assert "lineworks" in fields["notify.interactive"][0]
    assert fields["notify.lineworks.allowed_user_ids"][0] == "reqcsv"
    assert fields["notify.lineworks.project_ids"][1](cfg)
    assert not fields["notify.card_thread"][1](cfg)
    assert mcs_setup._parse_answer("reqcsv", ",,")[0] is False
    assert mcs_setup._parse_answer("reqcsv", "u1,u2") == (True, ["u1", "u2"])


@pytest.mark.parametrize("result", [0, 1, 124])
def test_local_diagnostic_has_no_hermes_gateway_or_secret_output(monkeypatch, tmp_path, result):
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path / ".mcs"))
    monkeypatch.setattr(mcs_setup.sys, "platform", "linux")
    monkeypatch.setattr(mcs_setup.shutil, "which", lambda *a: None)
    monkeypatch.setattr(mcs_setup, "env_value", lambda *a, **kw: None)
    monkeypatch.setattr(local_llm, "bounded_request", lambda *a: (503, {}, b""))
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        return SimpleNamespace(returncode=result, stdout="synthetic-secret", stderr="synthetic-secret")

    monkeypatch.setattr(mcs_setup, "_run", run)
    errors, warnings = mcs_setup.check_environment(deepcopy(config()))
    assert len(calls) == 1
    assert calls[0][0][-3:] == ["check", "--root", mcs_setup.HOME]
    assert calls[0][0][1] == str(Path(mcs_setup.REPO_ROOT) / "lineworks_adapter" / "__main__.py")
    assert calls[0][1]["timeout"] == 20
    assert not any("hermes CLI" in e or "synthetic-secret" in e for e in errors + warnings)
    assert any("adapter local check failed" in e for e in errors) is bool(result)


def test_lineworks_never_installs_hermes_gateway():
    assert mcs_setup._sync_gateway(config(), "nonexistent", lambda _: pytest.fail("gateway mutation"), False) == 0


def test_off_lineworks_text_target_still_uses_the_independent_local_diagnostic(monkeypatch, tmp_path):
    import json
    from adapters.lineworks import __main__ as adapter_cli

    cfg = config()
    cfg["notify"]["interactive"] = "off"
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    monkeypatch.setattr(mcs_setup.sys, "platform", "linux")
    monkeypatch.setattr(mcs_setup.shutil, "which", lambda *args: None)
    monkeypatch.setattr(mcs_setup, "env_value", lambda *args, **kwargs: None)
    monkeypatch.setattr(local_llm, "bounded_request", lambda *args: (503, {}, b""))
    monkeypatch.setattr(adapter_cli, "load_credentials", lambda *_: (
        SimpleNamespace(credentials=SimpleNamespace(private_key_path="synthetic-key-path")), "synthetic"))
    monkeypatch.setattr(adapter_cli, "_sign", lambda *args: b"synthetic")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=adapter_cli.check(argv[-1]), stdout="", stderr="")

    monkeypatch.setattr(mcs_setup, "_run", run)
    assert mcs_setup.check_environment(cfg)[0] == []
    assert calls[0][-3:] == ["check", "--root", str(tmp_path)]


def test_init_provisions_runner_directories_and_flags_before_adapter_launch(monkeypatch, tmp_path):
    import json
    from test_mcs_setup import _init_env

    _init_env(monkeypatch, tmp_path, config())
    monkeypatch.setattr(mcs_setup.sys, "argv", ["mcs_setup", "init", "--yes"])
    assert mcs_setup.main() == 0
    for name in ("lineworks_render", "cmd_int", "cmd_results", "flags"):
        assert (tmp_path / "data" / name).is_dir()
    flags = json.loads((tmp_path / "data" / "flags" / "notify.json").read_text())
    assert flags["transport"] == "lineworks" and flags["interactive"] is True


def test_cli_uses_runtime_home_instead_of_source_checkout(monkeypatch, tmp_path):
    from adapters.lineworks import __main__ as adapter_cli

    runtime = str(tmp_path / ".mcs")
    monkeypatch.setattr(adapter_cli, "HOME", runtime)
    checked = []
    monkeypatch.setattr(adapter_cli, "check", lambda root: checked.append(root) or 0)
    assert adapter_cli.main(["check"]) == 0
    assert checked == [runtime]
