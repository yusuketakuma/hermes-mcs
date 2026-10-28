"""Synthetic Hermes CLI only: no real Hermes, Slack or Discord."""
import getpass
import subprocess

import pytest

import hermes_credential_setup as setup

BOT, APP = "xoxb-synthetic-bot-token", "xapp-synthetic-app-token"


class FakeHermes:
    """A profile .env plus the public `config env-path` / `config set --stdin` commands."""
    def __init__(self, env, stored=None, *, set_ok=True, path_ok=True, lines=None):
        self.env, self.set_ok, self.path_ok, self.calls = env, set_ok, path_ok, []
        if lines is not None or stored:
            env.write_text("".join(lines or [f"{k}={v}\n" for k, v in stored.items()]))
            env.chmod(0o600)

    @property
    def stored(self):
        if not self.env.exists():
            return {}
        return dict(line.split("=", 1) for line in self.env.read_text().splitlines() if "=" in line)

    def __call__(self, exe, profile, *argv, input_text=None):
        self.calls.append((profile, argv, input_text))
        if argv == ("config", "env-path"):
            if not self.path_ok:
                return subprocess.CompletedProcess(argv, 1, "", "profile not found")
            return subprocess.CompletedProcess(argv, 0, f"{self.env}\n", "")
        if argv[:2] == ("config", "set") and argv[3:] == ("--stdin",) and self.set_ok:
            stored = {**self.stored, argv[2]: input_text}
            self.env.write_text("".join(f"{k}={v}\n" for k, v in stored.items()))
            self.env.chmod(0o600)
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


def test_changes_only_the_chosen_token_over_stdin_and_never_prints_it(term, capsys, tmp_path):
    hermes = FakeHermes(tmp_path / ".env", {"SLACK_BOT_TOKEN": "xoxb-old-registered-token", "SLACK_APP_TOKEN": "xapp-old-app"})
    term(["1", "2", "1"], ["bad value", BOT])          # retry an invalid value, keep the app token
    assert setup.run("hermes", "work", "slack", cli=hermes) == 0
    assert hermes.stored == {"SLACK_BOT_TOKEN": BOT, "SLACK_APP_TOKEN": "xapp-old-app"}
    writes = [call for call in hermes.calls if call[1][:2] == ("config", "set")]
    assert writes == [("work", ("config", "set", "SLACK_BOT_TOKEN", "--stdin"), BOT)]
    assert all(BOT not in arg for _, argv, _ in hermes.calls for arg in argv)
    out = capsys.readouterr().out
    assert BOT not in out and "old" not in out and "再起動" in out


@pytest.mark.parametrize("answers", [["2", "2"], ["1", "2", "2"], ["0"]])
def test_keeping_declining_or_aborting_writes_nothing(term, answers, tmp_path):
    hermes = FakeHermes(tmp_path / ".env", {"SLACK_BOT_TOKEN": "xoxb-old-registered-token"})
    term(answers, [BOT])
    assert setup.run("hermes", "", "slack", cli=hermes) == 0
    assert hermes.stored == {"SLACK_BOT_TOKEN": "xoxb-old-registered-token"}
    assert not [call for call in hermes.calls if call[1][:2] == ("config", "set")]


@pytest.mark.parametrize("reference", ["${VAULT_SLACK_APP}", "op://vault/slack/app", "\"op://vault/slack/app\"",
                                       "__BITWARDEN_MANAGED__", "xapp-${VAULT_PART}/suffix",
                                       "${VAULT_SLACK_APP:-xapp-fallback}"])
def test_environment_and_externally_managed_tokens_are_left_alone(term, monkeypatch, capsys, tmp_path, reference):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-from-environment")
    hermes = FakeHermes(tmp_path / ".env", {"SLACK_APP_TOKEN": reference})
    term([], [])
    assert setup.run("hermes", "", "slack", cli=hermes) == 0
    assert hermes.stored == {"SLACK_APP_TOKEN": reference}
    out = capsys.readouterr().out
    assert "環境変数" in out and "外部の秘密情報管理" in out and "xoxb-from-environment" not in out


def test_an_old_hermes_without_stdin_fails_instead_of_using_argv(term, capsys, tmp_path):
    hermes = FakeHermes(tmp_path / ".env", set_ok=False)
    term(["1", "1"], ["discord-synthetic-token-value"])
    assert setup.run("hermes", "", "discord", cli=hermes) == 1
    assert hermes.stored == {}
    assert "保存を確認できません" in capsys.readouterr().out


def test_unavailable_hermes_and_interrupts(term, monkeypatch, capsys, tmp_path):
    assert setup.run("hermes", "", "discord", cli=lambda *a, **k: None) == 1
    hermes = FakeHermes(tmp_path / ".env")

    def interrupt(prompt=""):
        raise KeyboardInterrupt
    term(["1"], [])
    monkeypatch.setattr(getpass, "getpass", interrupt)
    assert setup.run("hermes", "", "discord", cli=hermes) == 130
    assert "保存していません" in capsys.readouterr().out and hermes.stored == {}


@pytest.mark.parametrize("profile, transport", [("-p", "slack"), ("a b", "slack"), ("", "line")])
def test_rejects_option_like_profiles_and_unknown_transports(profile, transport):
    with pytest.raises(ValueError):
        setup.run("hermes", profile, transport, cli=lambda *a, **k: None)


def test_outside_a_terminal_nothing_is_asked(monkeypatch):
    monkeypatch.setattr(setup, "is_terminal", lambda: False)
    assert setup.run("hermes", "", "slack", cli=lambda *a, **k: None) == 2


@pytest.mark.parametrize("hermes_kw", [
    {"path_ok": False},                                                       # profile cannot be resolved
    {"lines": ["SLACK_BOT_TOKEN=xoxb-first\n", "export SLACK_BOT_TOKEN=xoxb-second\n"]},   # ambiguous
    {"lines": [b"\xff".decode("latin-1") + "SLACK_BOT_TOKEN=x\n"], "binary": True},           # unreadable
    {"lines": ["OTHER=1\n"], "mode": 0o640},                                    # others could read it
])
def test_an_unverifiable_profile_env_is_never_treated_as_unregistered(term, tmp_path, hermes_kw):
    env = tmp_path / ".env"
    binary, mode = hermes_kw.pop("binary", False), hermes_kw.pop("mode", None)
    hermes = FakeHermes(env, **hermes_kw)
    if binary:
        env.write_bytes(b"\xffSLACK_BOT_TOKEN=x\n")
    if mode is not None:
        env.chmod(mode)
    before = env.read_bytes() if env.exists() else None
    term([], [])
    assert setup.run("hermes", "", "slack", cli=hermes) == 1
    assert (env.read_bytes() if env.exists() else None) == before
    assert not [call for call in hermes.calls if call[1][:2] == ("config", "set")]


def test_a_missing_profile_env_is_unregistered_and_the_saved_line_is_verified(term, tmp_path):
    hermes = FakeHermes(tmp_path / ".env")
    term(["1", "1"], ["discord-synthetic-token-value"])
    assert setup.run("hermes", "", "discord", cli=hermes) == 0
    assert hermes.stored == {"DISCORD_BOT_TOKEN": "discord-synthetic-token-value"}
