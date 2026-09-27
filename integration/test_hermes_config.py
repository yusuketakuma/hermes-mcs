"""MCS setup writes synthetic credentials through the real public Hermes CLI."""

import json
import sys
from pathlib import Path

from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcs"))
import _mcs_path  # noqa: F401
import mcs_setup


def test_setup_stdin_credentials_follow_the_selected_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    # A local entry point invokes the current candidate without an installed CLI
    # or any process runner mocks. Record argv only, never the piped value.
    trace = tmp_path / "argv.json"
    executable = tmp_path / "hermes"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(Path.cwd())!r})\n"
        f"Path({str(trace)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        "from hermes_cli.main import main\n"
        "main()\n")
    executable.chmod(0o700)
    homes = [tmp_path / "a", tmp_path / "b"]
    values = ["synthetic-a-first", "synthetic-b", "synthetic-a-last"]
    for home, value in zip([homes[0], homes[1], homes[0]], values, strict=False):
        monkeypatch.setenv("HERMES_HOME", str(home))
        assert mcs_setup._hermes_config_set(
            str(executable), "", "DISCORD_BOT_TOKEN", value)
        assert json.loads(trace.read_text()) == [
            "config", "set", "DISCORD_BOT_TOKEN", "--stdin"]
        assert dotenv_values(home / ".env")["DISCORD_BOT_TOKEN"] == value
    assert dotenv_values(homes[0] / ".env")["DISCORD_BOT_TOKEN"] == values[2]
    assert dotenv_values(homes[1] / ".env")["DISCORD_BOT_TOKEN"] == values[1]
