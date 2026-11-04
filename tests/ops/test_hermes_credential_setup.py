"""Synthetic Hermes CLI only: no real Hermes, Slack or Discord."""
import getpass
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

import hermes_credential_setup as setup

BOT, APP = "xox" "b-synthetic-bot-token", "xapp-synthetic-app-token"


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
    hermes = FakeHermes(tmp_path / ".env", {"SLACK_BOT_TOKEN": "xox" "b-old-registered-token", "SLACK_APP_TOKEN": "xapp-old-app"})
    # values Hermes would store differently (spaces, non-ASCII, '#', quotes, backslash) are asked again
    term(["1", "2", "1"], ["bad value", "xox" "b-tökén", "xox" "b-a#b", 'xox' 'b-"q"', "xox" "b-a\\b", BOT])
    assert setup.run("hermes", "work", "slack", cli=hermes) == 0
    assert hermes.stored == {"SLACK_BOT_TOKEN": BOT, "SLACK_APP_TOKEN": "xapp-old-app"}
    writes = [call for call in hermes.calls if call[1][:2] == ("config", "set")]
    assert writes == [("work", ("config", "set", "SLACK_BOT_TOKEN", "--stdin"), BOT)]
    assert all(BOT not in arg for _, argv, _ in hermes.calls for arg in argv)
    out = capsys.readouterr().out
    assert BOT not in out and "xox" "b-old-registered-token" not in out and "xapp-old-app" not in out and "再起動" in out


@pytest.mark.parametrize("answers", [["2", "2"], ["1", "2", "2"], ["0"]])
def test_keeping_declining_or_aborting_writes_nothing(term, answers, tmp_path):
    hermes = FakeHermes(tmp_path / ".env", {"SLACK_BOT_TOKEN": "xox" "b-old-registered-token"})
    term(answers, [BOT])
    assert setup.run("hermes", "", "slack", cli=hermes) == 0
    assert hermes.stored == {"SLACK_BOT_TOKEN": "xox" "b-old-registered-token"}
    assert not [call for call in hermes.calls if call[1][:2] == ("config", "set")]


@pytest.mark.parametrize("reference", ["${VAULT_SLACK_APP}", "op://vault/slack/app", "\"op://vault/slack/app\"",
                                       "__BITWARDEN_MANAGED__", "xapp-${VAULT_PART}/suffix",
                                       "${VAULT_SLACK_APP:-xapp-fallback}"])
def test_environment_and_externally_managed_tokens_are_left_alone(term, monkeypatch, capsys, tmp_path, reference):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xox" "b-from-environment")
    hermes = FakeHermes(tmp_path / ".env", {"SLACK_APP_TOKEN": reference})
    term([], [])
    assert setup.run("hermes", "", "slack", cli=hermes) == 0
    assert hermes.stored == {"SLACK_APP_TOKEN": reference}
    out = capsys.readouterr().out
    assert "環境変数" in out and "外部の秘密情報管理" in out and "xox" "b-from-environment" not in out


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
    {"lines": ["SLACK_BOT_TOKEN=xox" "b-first\n", "export SLACK_BOT_TOKEN=xox" "b-second\n"]},   # ambiguous
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


def test_an_empty_profile_is_the_explicit_default_never_the_sticky_active_one(term, tmp_path, capsys):
    hermes = FakeHermes(tmp_path / ".env")
    term(["1", "1"], ["discord-synthetic-token-value"])
    assert setup.run("hermes", "", "discord", cli=hermes) == 0
    assert {profile for profile, _, _ in hermes.calls} == {"default"}
    assert str(tmp_path / ".env") in capsys.readouterr().out     # the real destination is shown first


@pytest.mark.parametrize("admission", [lambda: False, lambda: 1 / 0, lambda: "yes"])
def test_the_callers_admission_is_rechecked_right_before_each_write(term, tmp_path, admission):
    hermes = FakeHermes(tmp_path / ".env")
    term(["1", "1"], ["discord-synthetic-token-value"])
    assert setup.run("hermes", "", "discord", cli=hermes, admission=admission) == 1
    assert hermes.stored == {} and not [c for c in hermes.calls if c[1][:2] == ("config", "set")]


def test_a_destination_that_moved_during_input_is_not_written(term, tmp_path, monkeypatch):
    hermes = FakeHermes(tmp_path / ".env")
    moved = tmp_path / "other" / ".env"

    def enter(prompt=""):
        hermes.env = moved          # e.g. HERMES_HOME or the profile store changed meanwhile
        return "discord-synthetic-token-value"
    term(["1", "1"], [])
    monkeypatch.setattr(getpass, "getpass", enter)
    assert setup.run("hermes", "", "discord", cli=hermes) == 1
    assert not [c for c in hermes.calls if c[1][:2] == ("config", "set")]


def _tree(tmp_path, *, shebang="#!/bin/sh\n", body='echo "argv=$*" "pp=${PYTHONPATH-unset}" "dy=${DYLD_INSERT_LIBRARIES-unset}"\n'):
    """A synthetic install: the install.sh shim and its venv executable (no real Hermes)."""
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    real = venv / "hermes"
    real.write_text(shebang + body)
    real.chmod(0o700)
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "hermes"
    shim.write_text(f'#!/usr/bin/env bash\nunset PYTHONPATH\nunset PYTHONHOME\nexec {real} "$@"\n')
    shim.chmod(0o700)
    return shim, real


@pytest.fixture
def trusted_host_ancestors(tmp_path, monkeypatch):
    # Model trusted install ancestry outside the synthetic tree. Linux /tmp
    # is shared; fixture files/directories retain every real permission check.
    ancestors = set()

    def register_ancestors(path):
        ancestors.update((info.st_dev, info.st_ino) for parent in path.parents
                         for info in (parent.stat(),))

    register_ancestors(tmp_path)
    original = setup._safe_owner
    monkeypatch.setattr(setup, "_safe_owner", lambda info: original(info) or (
        (info.st_dev, info.st_ino) in ancestors and info.st_uid in (os.getuid(), 0)))
    return register_ancestors


@pytest.fixture
def sandbox_hermes_tree(trusted_host_ancestors):
    # Keep the actual executable inside the process guard's existing temporary root.
    with tempfile.TemporaryDirectory(prefix='mcs-synthetic-hermes-') as directory:
        root = Path(directory)
        trusted_host_ancestors(root)  # The root and all inner paths retain actual permission checks.
        yield root


@pytest.mark.usefixtures("trusted_host_ancestors")
def test_the_mcs_shim_runs_its_venv_command_directly_with_a_minimal_environment(sandbox_hermes_tree, monkeypatch):
    tmp_path = sandbox_hermes_tree
    shim, real = _tree(tmp_path)
    for key, value in (("PYTHONPATH", "/synthetic/inject"), ("DYLD_INSERT_LIBRARIES", "/synthetic/lib"),
                       ("BASH_ENV", "/synthetic/rc"), ("HERMES_HOME", str(tmp_path / "hh"))):
        monkeypatch.setenv(key, value)
    argv0, env, identity = setup.trusted_command(str(shim))
    assert argv0 == os.path.realpath(real) and len(identity) == 3       # shim, venv command, /bin/sh
    assert not {"PYTHONPATH", "DYLD_INSERT_LIBRARIES", "BASH_ENV"} & set(env)
    assert env["HERMES_HOME"] == str(tmp_path / "hh") and env["PATH"].startswith(os.path.dirname(argv0))
    cli, unchanged = setup.secret_cli(str(shim))
    out = cli("ignored", "default", "config", "env-path").stdout
    assert "argv=-p default config env-path" in out and "pp=unset" in out and "dy=unset" in out
    assert unchanged()
    real.write_text("#!/bin/sh\necho swapped\n")                         # the same user edits it meanwhile
    assert not unchanged()


@pytest.mark.usefixtures("trusted_host_ancestors")
@pytest.mark.parametrize("change, code", [
    ("group_writable_dir", "hermes_writable_by_others"),
    ("env_shebang", "hermes_interpreter_from_path"),
    ("relative_shebang", "hermes_interpreter_unknown"),
    ("missing", "hermes_missing"),
])
def test_an_unprovable_hermes_command_is_refused_before_any_input(tmp_path, change, code):
    shim, real = _tree(tmp_path, shebang={"env_shebang": "#!/usr/bin/env python3\n",
                                          "relative_shebang": "#!python3\n"}.get(change, "#!/bin/sh\n"))
    if change == "group_writable_dir":
        real.parent.chmod(0o770)
    target = str(tmp_path / "nowhere") if change == "missing" else str(shim)
    with pytest.raises(setup.UntrustedExecutable) as raised:
        setup.trusted_command(target)
    assert str(raised.value) == code


@pytest.mark.usefixtures("trusted_host_ancestors")
def test_run_stops_before_asking_when_the_command_is_untrusted(term, tmp_path, capsys):
    shim, real = _tree(tmp_path, shebang="#!/usr/bin/env python3\n")
    term([], [])                                       # any prompt would raise StopIteration
    assert setup.run(str(shim), "", "slack") == 1
    out = capsys.readouterr().out
    assert "hermes_bin" in out and "hermes_interpreter_from_path" in out


@pytest.mark.usefixtures("trusted_host_ancestors")
def test_a_small_native_binary_is_checked_without_being_decoded(tmp_path):
    native = tmp_path / "bin" / "hermes"
    native.parent.mkdir()
    native.write_bytes(b"\xcf\xfa\xed\xfe\x07\x00\x00\x01" + b"\xff" * 64)   # Mach-O-like, not UTF-8
    native.chmod(0o700)
    argv0, _, identity = setup.trusted_command(str(native))
    assert argv0 == os.path.realpath(native) and len(identity) == 1


@pytest.mark.usefixtures("trusted_host_ancestors")
def test_an_interpreter_reached_through_a_replaceable_directory_is_refused(tmp_path):
    # the kernel follows the shebang path as written: a safe target behind a directory
    # others can write is not safe
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "sh").symlink_to("/bin/sh")
    shared.chmod(0o777)
    shim, real = _tree(tmp_path, shebang=f"#!{shared / 'sh'}\n")
    with pytest.raises(setup.UntrustedExecutable) as raised:
        setup.trusted_command(str(shim))
    assert str(raised.value) == "interpreter_writable_by_others"
    shared.chmod(0o755)                     # the same link in a private directory is accepted
    assert len(setup.trusted_command(str(shim))[2]) == 3
