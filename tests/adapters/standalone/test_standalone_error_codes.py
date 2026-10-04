"""init/check print the fixed error code instead of `failed <Type>`."""
from mcs_standalone import __main__ as cli

from tests.adapters.standalone.test_standalone_runtime import write_root


def test_invalid_getpass_token_prints_its_code(tmp_path, monkeypatch, capsys):
    write_root(tmp_path, env="")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "bad token")
    assert cli.main(["init", "--root", str(tmp_path), "--transport", "discord"]) == 1
    assert capsys.readouterr().err.strip() == "standalone_credentials_invalid"
    assert not (tmp_path / "data" / "discord-credentials.json").exists()


def test_lineworks_check_prints_its_code(tmp_path, monkeypatch, capsys):
    import adapters.lineworks.__main__ as lineworks
    from adapters.lineworks.client import ClientError

    def refuse(root):
        raise ClientError("private_key_permissions")

    write_root(tmp_path)
    monkeypatch.setattr(cli.config, "transports", lambda root: ("lineworks",))
    monkeypatch.setattr(lineworks, "check", refuse)
    monkeypatch.setattr(cli, "_sdk_problem", lambda: None)
    assert cli.main(["check", "--root", str(tmp_path)]) == 1
    assert capsys.readouterr().err.strip() == "private_key_permissions"
