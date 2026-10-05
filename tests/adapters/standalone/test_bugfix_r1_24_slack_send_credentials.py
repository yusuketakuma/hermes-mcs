"""A bot-only .env for a notify-only Slack target is setup-incomplete
(the Slack connector loads both tokens to send): check fails and send
exits 75 (retry later), never 2 (held as a permanent refusal)."""
import io
import json
from types import SimpleNamespace

from mcs_standalone import __main__ as cli
from mcs_standalone import config
from tests.adapters.standalone.test_standalone_runtime import SLACK, XOXB, write_root


def _bot_only_slack(tmp_path):
    cfg = write_root(tmp_path, interactive="off", target="slack:C0PRIMARY99",
                     env="SLACK_BOT_TOKEN=" + XOXB + "-padding\n")
    cfg["notify"]["slack"] = {**SLACK, "channel_id": "C0PRIMARY99"}
    (tmp_path / "config.json").write_text(json.dumps(cfg))


def test_check_requires_the_app_token_slack_send_loads(tmp_path, monkeypatch):
    _bot_only_slack(tmp_path)
    monkeypatch.setattr(cli, "_sdk_problem", lambda: None)
    assert cli.main(["check", "--root", str(tmp_path)]) == 1


def test_send_with_bot_only_env_is_tempfail_not_refused(tmp_path, monkeypatch):
    _bot_only_slack(tmp_path)
    monkeypatch.setattr(cli, "_sdk_problem", lambda: None)
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b'{"text": "x"}')))
    assert cli.main(["send", "--root", str(tmp_path), "--to", "slack:C0PRIMARY99", "--quiet"]) == 75


def test_setup_code_raised_inside_the_adapter_keeps_exit_75(tmp_path, monkeypatch):
    write_root(tmp_path)

    async def fake_send(root, target, payload):
        raise config.ConfigError("standalone_credentials_missing")

    monkeypatch.setattr(cli, "_runtime", lambda t: SimpleNamespace(send=fake_send))
    monkeypatch.setattr(cli, "_sdk_problem", lambda: None)
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(
        buffer=io.BytesIO(json.dumps({"text": "x"}).encode())))
    assert cli.main(["send", "--root", str(tmp_path), "--to",
                     "discord:1000000000000000001", "--quiet"]) == 75
