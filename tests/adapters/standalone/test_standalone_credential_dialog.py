"""Credential choices use synthetic roots and never contact a transport."""
import json
import warnings
from types import SimpleNamespace

import pytest

import mcs_setup
from mcs_standalone import __main__ as cli
from mcs_standalone import config
from standalone_testkit import write_root


@pytest.mark.parametrize("transport", ["discord", "slack"])
def test_new_credentials_are_entered_hidden_and_published(tmp_path, monkeypatch, transport):
    write_root(tmp_path, env="")
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    values = iter(["synthetic-discord-token"] if transport == "discord" else
                  ["xox" + "b-synthetic-bot", "xapp-synthetic-app"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: next(values))
    assert cli.initialize(tmp_path, transport) == 0
    stored = json.loads((tmp_path / "data" / f"{transport}-credentials.json").read_text())
    assert stored["bot_token"].endswith("synthetic-discord-token" if transport == "discord" else "synthetic-bot")
    if transport == "slack":
        assert stored["app_token"] == "xapp-synthetic-app"


@pytest.mark.parametrize("source", ["json", "env"])
@pytest.mark.parametrize("answer", ["2", "0"])
def test_existing_credentials_are_not_read_out_or_changed_when_held_or_cancelled(
        tmp_path, monkeypatch, capsys, source, answer):
    write_root(tmp_path, env="DISCORD_BOT_TOKEN=synthetic-existing-token\n")
    path = tmp_path / "data" / "discord-credentials.json"
    if source == "json":
        path.parent.mkdir()
        path.write_text('{"bot_token":"synthetic-json-token"}')
        path.chmod(0o600)
    env_before = (tmp_path / ".env").read_bytes()
    json_before = path.read_bytes() if path.exists() else None
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda _: answer)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: pytest.fail("must not ask for a new secret"))
    assert cli.initialize(tmp_path, "discord") == 1
    output = capsys.readouterr().out
    assert "synthetic" not in output
    assert ("保留中" in output) if answer == "2" else ("中止" in output)
    assert (tmp_path / ".env").read_bytes() == env_before
    assert (path.read_bytes() if path.exists() else None) == json_before


def test_invalid_choice_reprompts_and_default_keeps_json(tmp_path, monkeypatch, capsys):
    write_root(tmp_path, env="")
    path = tmp_path / "data" / "discord-credentials.json"
    path.parent.mkdir()
    path.write_text('{"bot_token":"synthetic-json-token"}')
    path.chmod(0o600)
    original = path.read_bytes()
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    answers = iter(["bad", ""])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    assert cli.initialize(tmp_path, "discord") == 0
    assert "いずれかを入力" in capsys.readouterr().out
    assert path.read_bytes() == original


@pytest.mark.parametrize("error", [EOFError, KeyboardInterrupt])
def test_interrupted_existing_choice_cancels_without_writing(tmp_path, monkeypatch, error):
    write_root(tmp_path, env="DISCORD_BOT_TOKEN=synthetic-existing-token\n")
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    def interrupt(_):
        raise error
    monkeypatch.setattr("builtins.input", interrupt)
    assert cli.initialize(tmp_path, "discord") == 1
    assert not (tmp_path / "data" / "discord-credentials.json").exists()


def test_setup_does_not_continue_other_connectors_after_cancel(monkeypatch):
    calls = []
    monkeypatch.setattr(mcs_setup, "transport_enabled", lambda cfg, name: name in {"slack", "discord"})
    monkeypatch.setattr(mcs_setup.subprocess, "run", lambda argv, **kwargs:
                        calls.append(argv) or SimpleNamespace(returncode=1))
    assert not mcs_setup._init_standalone_connectors({}, SimpleNamespace(yes=False), True)
    assert len(calls) == 1


@pytest.mark.parametrize("yes", [False, True])
def test_noninteractive_existing_json_is_kept_without_question(tmp_path, monkeypatch, yes):
    write_root(tmp_path, env="")
    path = tmp_path / "data" / "discord-credentials.json"
    path.parent.mkdir()
    path.write_text('{"bot_token":"synthetic-json-token"}')
    path.chmod(0o600)
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("must not prompt"))
    assert cli.initialize(tmp_path, "discord", yes=yes) == 0
    assert path.read_text() == '{"bot_token":"synthetic-json-token"}'


@pytest.mark.parametrize("transport,env", [
    ("slack", "SLACK_BOT_TOKEN=xox" + "b-synthetic-bot\n"),
    ("slack", "SLACK_APP_TOKEN=xapp-synthetic-app\n"),
    ("discord", "DISCORD_BOT_TOKEN=bad\n"),
    ("discord", "DISCORD_BOT_TOKEN=\n"),
])
def test_incomplete_or_invalid_env_is_not_treated_as_new(
        tmp_path, monkeypatch, transport, env):
    write_root(tmp_path, env=env)
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: pytest.fail("must not ask for a secret"))
    with pytest.raises(config.ConfigError, match="^credential_existing_state_unverifiable$"):
        cli.initialize(tmp_path, transport)
    assert (tmp_path / ".env").read_text() == env
    assert not (tmp_path / "data" / f"{transport}-credentials.json").exists()


def test_unreadable_env_does_not_trigger_new_entry(tmp_path, monkeypatch):
    write_root(tmp_path, env="")
    (tmp_path / ".env").chmod(0o644)
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: pytest.fail("must not ask for a secret"))
    with pytest.raises(config.ConfigError, match="^credential_existing_state_unverifiable$"):
        cli.initialize(tmp_path, "discord")
    assert not (tmp_path / "data" / "discord-credentials.json").exists()


def test_env_modified_during_input_cancels_publication(tmp_path, monkeypatch):
    write_root(tmp_path, env="")
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    def answer(_):
        (tmp_path / ".env").write_text("DISCORD_BOT_TOKEN=another-synthetic-token\n")
        return "synthetic-new-discord-token"
    monkeypatch.setattr(cli.getpass, "getpass", answer)
    with pytest.raises(config.ConfigError, match="^credential_existing_state_changed$"):
        cli.initialize(tmp_path, "discord")
    data = tmp_path / "data"
    assert not (data / "discord-credentials.json").exists()
    assert list(data.iterdir()) == []


def test_setup_does_not_complete_an_incomplete_env_implicitly(tmp_path, monkeypatch):
    env = "SLACK_BOT_TOKEN=xox" + "b-synthetic-bot\n"
    write_root(tmp_path, env=env)
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    monkeypatch.setattr(mcs_setup, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(mcs_setup.getpass, "getpass", lambda _: pytest.fail("must not ask for a secret"))
    assert not mcs_setup._apply_standalone_credentials(
        SimpleNamespace(yes=False), {"slack"}, [("SLACK_APP_TOKEN", "app")])
    assert (tmp_path / ".env").read_text() == env


def test_setup_does_not_save_partially_entered_credentials(tmp_path, monkeypatch):
    write_root(tmp_path, env="")
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    monkeypatch.setattr(mcs_setup, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(mcs_setup, "env_value", lambda *a, **kw: None)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    answers = iter(["xox" + "b-synthetic-bot", ""])
    monkeypatch.setattr(mcs_setup.getpass, "getpass", lambda _: next(answers))
    assert not mcs_setup._apply_standalone_credentials(
        SimpleNamespace(yes=False), {"slack"},
        [("SLACK_BOT_TOKEN", "bot"), ("SLACK_APP_TOKEN", "app")])
    assert (tmp_path / ".env").read_text() == ""


def test_setup_detects_env_change_during_entry(tmp_path, monkeypatch):
    write_root(tmp_path, env="")
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    monkeypatch.setattr(mcs_setup, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(mcs_setup, "env_value", lambda *a, **kw: None)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    def answer(_):
        (tmp_path / ".env").write_text("DISCORD_BOT_TOKEN=other-synthetic-token\n")
        return "synthetic-discord-token"
    monkeypatch.setattr(mcs_setup.getpass, "getpass", answer)
    assert not mcs_setup._apply_standalone_credentials(
        SimpleNamespace(yes=False), {"discord"}, [("DISCORD_BOT_TOKEN", "bot")])
    assert (tmp_path / ".env").read_text() == "DISCORD_BOT_TOKEN=other-synthetic-token\n"


def test_init_fsync_failure_reports_unknown_even_if_file_was_published(tmp_path, monkeypatch, capsys):
    from adapters.common import paths
    write_root(tmp_path, env="DISCORD_BOT_TOKEN=synthetic-discord-token\n")
    def fail_fsync(_):
        raise OSError("synthetic")
    monkeypatch.setattr(paths, "fsync_dir", fail_fsync)
    assert cli.main(["init", "--root", str(tmp_path), "--transport", "discord", "--yes"]) == 1
    captured = capsys.readouterr()
    assert "保存結果を確認できません" in captured.out
    assert "synthetic" not in captured.out + captured.err
    assert (tmp_path / "data" / "discord-credentials.json").exists()


def test_setup_env_write_failure_reports_unknown(tmp_path, monkeypatch, capsys):
    write_root(tmp_path, env="")
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    monkeypatch.setattr(mcs_setup, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(mcs_setup, "env_value", lambda *a, **kw: None)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.setattr(mcs_setup.getpass, "getpass", lambda _: "synthetic-discord-token")
    def fail_write(*_):
        raise OSError("synthetic")
    monkeypatch.setattr(mcs_setup.lifecycle_io, "write_env", fail_write)
    assert not mcs_setup._apply_standalone_credentials(
        SimpleNamespace(yes=False), {"discord"}, [("DISCORD_BOT_TOKEN", "bot")])
    captured = capsys.readouterr()
    assert "保存結果を確認できません" in captured.out
    assert "synthetic" not in captured.out + captured.err


def test_setup_does_not_save_invalid_entered_token(tmp_path, monkeypatch):
    write_root(tmp_path, env="")
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    monkeypatch.setattr(mcs_setup, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(mcs_setup, "env_value", lambda *a, **kw: None)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.setattr(mcs_setup.getpass, "getpass", lambda _: "short")
    assert not mcs_setup._apply_standalone_credentials(
        SimpleNamespace(yes=False), {"discord"}, [("DISCORD_BOT_TOKEN", "bot")])
    assert (tmp_path / ".env").read_text() == ""


def test_native_init_refuses_echo_fallback_before_input(tmp_path, monkeypatch):
    write_root(tmp_path, env="")
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    def echo_fallback(_):
        warnings.warn("synthetic echo fallback", cli.getpass.GetPassWarning)
        pytest.fail("fallback must not read input")
    monkeypatch.setattr(cli.getpass, "getpass", echo_fallback)
    with pytest.raises(config.ConfigError, match="^credential_hidden_input_unavailable$"):
        cli.initialize(tmp_path, "discord")
    assert not (tmp_path / "data" / "discord-credentials.json").exists()


def test_setup_refuses_echo_fallback_before_input(tmp_path, monkeypatch):
    write_root(tmp_path, env="")
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    monkeypatch.setattr(mcs_setup, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(mcs_setup, "env_value", lambda *a, **kw: None)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    def echo_fallback(_):
        warnings.warn("synthetic echo fallback", cli.getpass.GetPassWarning)
        pytest.fail("fallback must not read input")
    monkeypatch.setattr(mcs_setup.getpass, "getpass", echo_fallback)
    assert not mcs_setup._apply_standalone_credentials(
        SimpleNamespace(yes=False), {"discord"}, [("DISCORD_BOT_TOKEN", "bot")])
    assert (tmp_path / ".env").read_text() == ""


def test_setup_uses_existing_json_without_reading_irrelevant_env(tmp_path, monkeypatch):
    write_root(tmp_path, env="")
    (tmp_path / ".env").chmod(0o644)
    path = tmp_path / "data" / "discord-credentials.json"
    path.parent.mkdir()
    path.write_text('{"bot_token":"synthetic-discord-token"}')
    path.chmod(0o600)
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    monkeypatch.setattr(mcs_setup, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(mcs_setup.getpass, "getpass", lambda _: pytest.fail("must not prompt"))
    assert mcs_setup._apply_standalone_credentials(
        SimpleNamespace(yes=False), {"discord"}, [("DISCORD_BOT_TOKEN", "bot")])
    assert path.read_text() == '{"bot_token":"synthetic-discord-token"}'
