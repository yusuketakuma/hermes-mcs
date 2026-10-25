"""Standalone config generation, transport scope, private init and sealed
attachment confinement (synthetic roots only, no network or SDK)."""
import hashlib
import io
import json
import os
import stat
from types import SimpleNamespace

import pytest

from adapters.common.paths import config_generation
from mcs_standalone import __main__ as cli
from mcs_standalone import config
from standalone_testkit import write_root

DISCORD_ENV = "DISCORD_BOT_TOKEN=synthetic-discord-token\n"


def _changed(tmp_path, before):
    after = config_generation(str(tmp_path), "discord")
    assert after is not None and after != before
    return after


def test_generation_observes_every_metadata_change(tmp_path):
    absent = config_generation(str(tmp_path), "discord")
    assert absent == (("absent",), ("absent",), ("absent",))
    write_root(tmp_path, env=DISCORD_ENV)
    current = _changed(tmp_path, absent)
    assert config_generation(str(tmp_path), "discord") == current   # unchanged files, same tuple
    os.chmod(tmp_path / ".env", 0o400)                              # chmod only
    current = _changed(tmp_path, current)
    cfg = tmp_path / "config.json"
    cfg.write_text(cfg.read_text() + " ")                           # content edit
    current = _changed(tmp_path, current)
    replacement = tmp_path / "config.json.new"
    replacement.write_text(cfg.read_text())
    os.replace(replacement, cfg)                                    # atomic replace
    current = _changed(tmp_path, current)
    credentials = tmp_path / "data" / "discord-credentials.json"
    credentials.parent.mkdir()
    credentials.write_text("{}")                                    # credentials take over from .env
    current = _changed(tmp_path, current)
    assert current[1] != ("absent",)
    credentials.unlink()
    assert _changed(tmp_path, current)[1] == ("absent",)


def test_generation_keeps_links_and_fifos_as_their_own_identity(tmp_path):
    write_root(tmp_path, env=DISCORD_ENV)
    (tmp_path / "data").mkdir()
    os.symlink(tmp_path / ".env", tmp_path / "data" / "discord-credentials.json")
    os.mkfifo(tmp_path / "data" / "slack-credentials.json")
    link = config_generation(str(tmp_path), "discord")[1]
    fifo = config_generation(str(tmp_path), "slack")[1]
    assert stat.S_ISLNK(link[2]) and stat.S_ISFIFO(fifo[2])          # lstat, never the target


def test_unreadable_metadata_forces_full_validation(tmp_path):
    write_root(tmp_path, env=DISCORD_ENV)
    (tmp_path / "data").mkdir()
    os.chmod(tmp_path / "data", 0)
    try:
        if os.access(tmp_path / "data", os.X_OK):
            pytest.skip("privileged user can traverse a 000 directory")
        assert config_generation(str(tmp_path), "discord") is None
    finally:
        os.chmod(tmp_path / "data", 0o700)


def test_generation_and_connector_settings_reject_unknown_transports(tmp_path):
    write_root(tmp_path, env=DISCORD_ENV)
    for transport in ("lineworks", "../x"):
        with pytest.raises(ValueError, match="^standalone_transport_invalid$"):
            config_generation(str(tmp_path), transport)
        with pytest.raises(ValueError, match="^standalone_transport_invalid$"):
            config.connector_settings(tmp_path, transport, require_interactive=False)


def test_init_publishes_private_credentials_without_overwrite_or_staging(tmp_path, monkeypatch):
    write_root(tmp_path, env=DISCORD_ENV)
    assert cli.initialize(str(tmp_path), "discord", yes=True) == 0
    path = tmp_path / "data" / "discord-credentials.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "data").stat().st_mode) == 0o700
    assert json.loads(path.read_text()) == {"bot_token": "synthetic-discord-token"}
    assert [p.name for p in path.parent.iterdir()] == [path.name]
    # a credentials file appearing after the existence check is never replaced
    path.write_text('{"bot_token": "kept-existing-token"}')
    monkeypatch.setattr(cli.os.path, "lexists", lambda _p: False)
    with pytest.raises(FileExistsError):
        cli.initialize(str(tmp_path), "discord", yes=True)
    assert json.loads(path.read_text()) == {"bot_token": "kept-existing-token"}
    assert [p.name for p in path.parent.iterdir()] == [path.name]


def _pin(path, blob):
    return {"name": "a.txt", "path": str(path), "bytes": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest()}


def test_sealed_attachments_stay_confined_to_data_attachments(tmp_path, monkeypatch):
    write_root(tmp_path, env=DISCORD_ENV)
    attachments = tmp_path / "data" / "attachments"
    attachments.mkdir(parents=True)
    blob = b"synthetic"
    (attachments / "a").write_bytes(blob)
    (tmp_path / "outside").write_bytes(blob)
    os.symlink(tmp_path / "outside", attachments / "escape")

    def payload(files):
        raw = json.dumps({"text": "合成", "files": files}).encode()
        monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw)))
        return cli._payload(str(tmp_path))

    assert payload([_pin(attachments / "a", blob)])["files"][0]["name"] == "a.txt"
    for pin in (_pin(tmp_path / "outside", blob), _pin(attachments / "escape", blob),
                {**_pin(attachments / "a", blob), "bytes": 8},
                {**_pin(attachments / "a", blob), "name": "../a"},
                {**_pin(attachments / "a", blob), "path": str(attachments / "missing")}):
        with pytest.raises(config.ConfigError, match="^payload_invalid$"):
            payload([pin])


def test_init_reports_a_failed_staging_cleanup_instead_of_success(tmp_path, monkeypatch, capsys):
    write_root(tmp_path, env=DISCORD_ENV)
    real_unlink = os.unlink

    data = str(tmp_path / "data")

    def failing(path, *args, **kwargs):
        # any staging copy beside the credentials file, whatever its name
        if os.path.dirname(os.fspath(path)) == data and not os.fspath(path).endswith("-credentials.json"):
            raise PermissionError("synthetic unlink failure")
        return real_unlink(path, *args, **kwargs)
    monkeypatch.setattr(cli.os, "unlink", failing)
    with pytest.raises(PermissionError):
        cli.initialize(str(tmp_path), "discord", yes=True)
    assert "認証設定を保存しました" not in capsys.readouterr().out


def test_symlinked_data_accepts_only_its_resolved_attachments(tmp_path, monkeypatch):
    write_root(tmp_path, env=DISCORD_ENV)
    real = tmp_path / "real-data"
    (real / "attachments").mkdir(parents=True)
    os.symlink(real, tmp_path / "data")
    blob = b"synthetic"
    (real / "attachments" / "a").write_bytes(blob)
    (real / "sibling").write_bytes(blob)
    (tmp_path / "other").write_bytes(blob)

    def payload(files):
        raw = json.dumps({"text": "合成", "files": files}).encode()
        monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw)))
        return cli._payload(str(tmp_path))

    for inside in (tmp_path / "data" / "attachments" / "a", real / "attachments" / "a"):
        assert payload([_pin(inside, blob)])["files"][0]["path"] == str(inside)
    for outside in (real / "sibling", tmp_path / "other", tmp_path / "data" / "sibling"):
        with pytest.raises(config.ConfigError, match="^payload_invalid$"):
            payload([_pin(outside, blob)])
