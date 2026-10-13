"""Standalone credential file and proxy gates with synthetic secrets in temp dirs only."""
import json
import os

import pytest

from mcs_standalone import __main__ as cli
from mcs_standalone import config

from standalone_testkit import write_root

JSON_TOKEN = "discord-synthetic-json"
LIMIT = 16384                       # _private_bytes default for <transport>-credentials.json


def _credentials(tmp_path, *, pad=0, mode=0o600):
    path = config.credentials_path(tmp_path, "discord")
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"bot_token": JSON_TOKEN})
    path.write_text(body + " " * pad)       # trailing whitespace keeps the JSON valid
    os.chmod(path, mode)
    return path, len(body)


def test_private_json_credentials_win_over_the_env_file(tmp_path):
    write_root(tmp_path)                    # .env holds a different, valid DISCORD_BOT_TOKEN
    assert config.tokens(tmp_path, "discord") == {"DISCORD_BOT_TOKEN": "synthetic"}
    _credentials(tmp_path)
    assert config.tokens(tmp_path, "discord") == {"DISCORD_BOT_TOKEN": JSON_TOKEN}


def test_json_credentials_accept_exactly_the_size_limit(tmp_path):
    write_root(tmp_path)
    path, size = _credentials(tmp_path)
    _credentials(tmp_path, pad=LIMIT - size)
    assert path.stat().st_size == LIMIT
    assert config.tokens(tmp_path, "discord") == {"DISCORD_BOT_TOKEN": JSON_TOKEN}


@pytest.mark.parametrize("problem", ["group_readable", "world_readable", "symlink",
                                     "oversize", "foreign_owner"])
def test_unsafe_json_credentials_fail_closed_without_env_fallback(tmp_path, monkeypatch, problem):
    write_root(tmp_path)                    # a valid .env must not rescue a bad JSON file
    if problem == "group_readable":
        _credentials(tmp_path, mode=0o640)
    elif problem == "world_readable":
        _credentials(tmp_path, mode=0o644)
    elif problem == "symlink":
        target = tmp_path / "elsewhere.json"
        target.write_text(json.dumps({"bot_token": JSON_TOKEN}))
        os.chmod(target, 0o600)
        link = config.credentials_path(tmp_path, "discord")
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
    elif problem == "oversize":
        path, size = _credentials(tmp_path)
        _credentials(tmp_path, pad=LIMIT - size + 1)
        assert path.stat().st_size == LIMIT + 1
    else:
        _credentials(tmp_path)
        real = os.getuid()
        monkeypatch.setattr(config.os, "getuid", lambda: real + 1)
    with pytest.raises(config.ConfigError, match="^standalone_credentials_invalid$"):
        config.tokens(tmp_path, "discord")
    with pytest.raises(config.ConfigError, match="^standalone_credentials_invalid$"):
        config.load_credentials(tmp_path, "discord")


PROXY_KEYS = ("HTTP_PROXY", "https_proxy", "ALL_PROXY", "ws_proxy", "WSS_PROXY")


def test_startup_strips_proxy_variables_before_anything_else(monkeypatch):
    for key in PROXY_KEYS:
        monkeypatch.setenv(key, "http://proxy.invalid:3128")
    monkeypatch.setenv("MCS_SYNTHETIC_UNRELATED", "kept")
    with pytest.raises(SystemExit):         # no subcommand: argparse exits after the strip
        cli.main([])
    assert not [key for key in os.environ
                if key.lower() in {k.lower() for k in PROXY_KEYS}]
    assert os.environ["MCS_SYNTHETIC_UNRELATED"] == "kept"
