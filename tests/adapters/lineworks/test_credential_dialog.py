"""LINE WORKS の認証対話を合成データで検証する。"""
import json
from types import SimpleNamespace

import pytest

from adapters.lineworks import __main__ as cli, config
from lineworks_testkit import CONFIG, SECRET, world


def _terminal(monkeypatch, answers):
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    answers = iter(answers)
    monkeypatch.setattr("builtins.input", lambda _: next(answers))


def test_registration_requires_explicit_choice_and_uses_private_file(monkeypatch, tmp_path, capsys):
    world(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps(CONFIG))
    key = tmp_path / "synthetic.pem"
    key.write_text("synthetic-key")
    key.chmod(0o600)
    path = config.credentials_path(tmp_path)
    assert not path.exists()
    _terminal(monkeypatch, ["2", str(key)])
    values = iter(("synthetic-client", "synthetic-secret", "synthetic@example.invalid", SECRET))
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: next(values))
    monkeypatch.setattr(cli, "_sign", lambda *args: b"synthetic-signature")
    assert cli.main(["credentials", "--root", str(tmp_path)]) == 0
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())["bot_secret"] == SECRET
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err


@pytest.mark.parametrize("answer", ["", "0", "1"])
def test_default_or_cancel_never_starts_registration(monkeypatch, tmp_path, answer):
    _terminal(monkeypatch, [answer])
    monkeypatch.setattr(cli, "initialize", lambda _: pytest.fail("registration must not start"))
    assert cli.credential_dialog(tmp_path) == 0
    assert not config.credentials_path(tmp_path).exists()


@pytest.mark.parametrize("kind", ["regular", "dangling_link"])
def test_existing_credentials_change_is_blocked_without_secret_input(monkeypatch, tmp_path, capsys, kind):
    path = config.credentials_path(tmp_path)
    path.parent.mkdir(parents=True)
    if kind == "regular":
        path.write_text("synthetic-existing")
        before = path.read_bytes()
    else:
        path.symlink_to("missing-synthetic")
    _terminal(monkeypatch, ["2"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: pytest.fail("must not ask for secret"))
    monkeypatch.setattr(cli, "initialize", lambda _: pytest.fail("must not replace existing"))
    assert cli.credential_dialog(tmp_path) == 1
    assert "変更しません" in capsys.readouterr().out
    if kind == "regular":
        assert path.read_bytes() == before
    else:
        assert path.is_symlink()


def test_nonterminal_never_requests_input_or_writes(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("no prompt without terminal"))
    assert cli.main(["credentials", "--root", str(tmp_path)]) == 1
    assert not config.credentials_path(tmp_path).exists()


@pytest.mark.parametrize("command", ["init", "credentials"])
def test_no_echo_fallback_is_rejected_before_secret_input(monkeypatch, tmp_path, capsys, command):
    world(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps(CONFIG))
    _terminal(monkeypatch, ["2"] if command == "credentials" else [])

    monkeypatch.setattr(cli.getpass, "getpass", cli.getpass.fallback_getpass)
    monkeypatch.setattr(cli.getpass, "_raw_input", lambda *args, **kwargs:
                        pytest.fail("fallback would read a visible secret"))
    assert cli.main([command, "--root", str(tmp_path)]) == 1
    assert not config.credentials_path(tmp_path).exists()
    out = capsys.readouterr()
    assert "credential_input_not_hidden" in out.err
    assert "Warning: Password input may be echoed" not in out.out + out.err


@pytest.mark.parametrize("error", [EOFError, KeyboardInterrupt])
def test_cancelled_choice_never_starts_registration(monkeypatch, tmp_path, error):
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(error()))
    monkeypatch.setattr(cli, "initialize", lambda _: pytest.fail("registration must not start"))
    assert cli.credential_dialog(tmp_path) == 0
    assert not config.credentials_path(tmp_path).exists()


def test_competing_registration_is_preserved(monkeypatch, tmp_path):
    world(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps(CONFIG))
    path = config.credentials_path(tmp_path)
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))

    def choose(_prompt):
        path.write_text("synthetic-concurrent")
        return "2"

    monkeypatch.setattr("builtins.input", choose)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: pytest.fail("must preserve concurrent file"))
    assert cli.credential_dialog(tmp_path) == 0
    assert path.read_text() == "synthetic-concurrent"


@pytest.mark.parametrize("error", [KeyboardInterrupt, OSError])
def test_registration_failure_may_have_published(monkeypatch, tmp_path, capsys, error):
    _terminal(monkeypatch, ["2"])
    path = config.credentials_path(tmp_path)
    path.parent.mkdir(parents=True)

    def publish_then_fail(_root):
        path.write_text("synthetic-concurrent")
        raise error()

    monkeypatch.setattr(cli, "initialize", publish_then_fail)
    assert cli.credential_dialog(tmp_path) == 1
    assert path.read_text() == "synthetic-concurrent"
    assert "登録結果を確認できません" in capsys.readouterr().out


@pytest.mark.parametrize('case', ['oversized', 'scope_changed', 'parent_untrusted', 'post_write_changed', 'post_scope_changed'])
def test_registration_checks_scope_size_and_saved_result(monkeypatch, tmp_path, capsys, case):
    world(tmp_path)
    config_path = tmp_path / 'config.json'
    config_path.write_text(json.dumps(CONFIG))
    key = tmp_path / 'synthetic.pem'
    key.write_text('synthetic-key')
    key.chmod(0o600)
    path = config.credentials_path(tmp_path)
    if case == 'parent_untrusted':
        path.parent.chmod(0o777)
    _terminal(monkeypatch, ['2', str(key)])
    values = iter(['synthetic-client', 'synthetic-secret', 'synthetic-account', '\U0001f642' * 4096] if case == 'oversized' else
                  ['synthetic-client', 'synthetic-secret', 'synthetic-account', SECRET])
    def get_secret(_):
        if case == 'scope_changed':
            cfg = json.loads(config_path.read_text())
            cfg['notify']['route_epoch'] += 1
            config_path.write_text(json.dumps(cfg))
        return next(values)
    monkeypatch.setattr(cli.getpass, 'getpass', get_secret)
    monkeypatch.setattr(cli, '_sign', lambda *a: b'synthetic-signature')
    if case in ('post_write_changed', 'post_scope_changed'):
        original = cli.paths.fsync_dir
        def changed(directory):
            original(directory)
            if case == 'post_write_changed':
                path.write_text('synthetic-replacement')
            else:
                cfg = json.loads(config_path.read_text())
                cfg['notify']['route_epoch'] += 1
                config_path.write_text(json.dumps(cfg))
        monkeypatch.setattr(cli.paths, 'fsync_dir', changed)
    assert cli.main(['credentials', '--root', str(tmp_path)]) == 1
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err
    if case in ('post_write_changed', 'post_scope_changed'):
        if case == 'post_write_changed':
            assert path.read_text() == 'synthetic-replacement'
        else:
            assert path.exists()
        assert '登録結果を確認できません' in out.out
    else:
        assert not path.exists()
