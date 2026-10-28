"""Synthetic Hermes CLI only: no real Hermes, Slack or Discord."""
import getpass
import subprocess

import pytest

import hermes_credential_setup as setup

BOT, APP = "xoxb-synthetic-bot-token", "xapp-synthetic-app-token"


class FakeHermes:
    def __init__(self, stored=None, *, set_ok=True):
        self.stored, self.set_ok, self.calls = dict(stored or {}), set_ok, []

    def __call__(self, exe, profile, *argv, input_text=None):
        self.calls.append((profile, argv, input_text))
        if argv[:2] == ("config", "get"):
            value = self.stored.get(argv[2])
            if value is None:
                return subprocess.CompletedProcess(argv, 1, "", "Config key not set")
            shown = value if value.startswith("${") else value[:4] + "..." + value[-4:]
            return subprocess.CompletedProcess(argv, 0, shown + "\n", "")
        if argv[:2] == ("config", "set") and argv[3:] == ("--stdin",) and self.set_ok:
            self.stored[argv[2]] = input_text
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 2, "", "unsupported")


@pytest.fixture
def term(monkeypatch):
    monkeypatch.setattr(setup, "is_terminal", lambda: True)
    for key in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "DISCORD_BOT_TOKEN"):
        monkeypatch.delenv(key, raising=False)

    def script(answers, secrets):
        answers, secrets = iter(answers), iter(secrets)
        monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
        monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(secrets))
    return script


def test_changes_only_the_chosen_token_over_stdin_and_never_prints_it(term, capsys):
    hermes = FakeHermes({"SLACK_BOT_TOKEN": "xoxb-old-registered-token", "SLACK_APP_TOKEN": "xapp-old-app"})
    term(["1", "2", "1"], ["bad value", BOT])          # retry an invalid value, keep the app token
    assert setup.run("hermes", "work", "slack", cli=hermes) == 0
    assert hermes.stored == {"SLACK_BOT_TOKEN": BOT, "SLACK_APP_TOKEN": "xapp-old-app"}
    writes = [call for call in hermes.calls if call[1][:2] == ("config", "set")]
    assert writes == [("work", ("config", "set", "SLACK_BOT_TOKEN", "--stdin"), BOT)]
    assert all(BOT not in arg for _, argv, _ in hermes.calls for arg in argv)
    out = capsys.readouterr().out
    assert BOT not in out and "old" not in out and "再起動" in out


@pytest.mark.parametrize("answers", [["2", "2"], ["1", "2", "2"], ["0"]])
def test_keeping_declining_or_aborting_writes_nothing(term, answers):
    hermes = FakeHermes({"SLACK_BOT_TOKEN": "xoxb-old-registered-token"})
    term(answers, [BOT])
    assert setup.run("hermes", "", "slack", cli=hermes) == 0
    assert hermes.stored == {"SLACK_BOT_TOKEN": "xoxb-old-registered-token"}
    assert not [call for call in hermes.calls if call[1][:2] == ("config", "set")]


def test_environment_and_externally_managed_tokens_are_left_alone(term, monkeypatch, capsys):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-from-environment")
    hermes = FakeHermes({"SLACK_APP_TOKEN": "${VAULT_SLACK_APP}"})
    term([], [])
    assert setup.run("hermes", "", "slack", cli=hermes) == 0
    assert hermes.stored == {"SLACK_APP_TOKEN": "${VAULT_SLACK_APP}"}
    out = capsys.readouterr().out
    assert "環境変数" in out and "外部の秘密情報管理" in out and "xoxb-from-environment" not in out


def test_an_old_hermes_without_stdin_fails_instead_of_using_argv(term, capsys):
    hermes = FakeHermes(set_ok=False)
    term(["1", "1"], ["discord-synthetic-token-value"])
    assert setup.run("hermes", "", "discord", cli=hermes) == 1
    assert hermes.stored == {}
    assert "保存を確認できません" in capsys.readouterr().out


def test_unavailable_hermes_and_interrupts(term, monkeypatch, capsys):
    assert setup.run("hermes", "", "discord", cli=lambda *a, **k: None) == 1
    hermes = FakeHermes()

    def interrupt(prompt=""):
        raise KeyboardInterrupt
    term(["1"], [])
    monkeypatch.setattr(getpass, "getpass", interrupt)
    assert setup.run("hermes", "", "discord", cli=hermes) == 130
    assert "保存していません" in capsys.readouterr().out and hermes.stored == {}


@pytest.mark.parametrize("profile, transport", [("-p", "slack"), ("a b", "slack"), ("", "line")])
def test_rejects_option_like_profiles_and_unknown_transports(profile, transport):
    with pytest.raises(ValueError):
        setup.run("hermes", profile, transport, cli=FakeHermes())


def test_outside_a_terminal_nothing_is_asked(monkeypatch):
    monkeypatch.setattr(setup, "is_terminal", lambda: False)
    assert setup.run("hermes", "", "slack", cli=FakeHermes()) == 2
