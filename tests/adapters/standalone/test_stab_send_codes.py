"""Standalone send surfaces the connector's fixed code (synthetic clients only)."""
import asyncio
from types import SimpleNamespace

import pytest

from adapters.discord import standalone as discord
from adapters.slack import standalone as slack
from mcs_standalone import __main__ as cli

SETTINGS = {"team_id": "T_SYNTHETIC", "application_id": "A_SYNTHETIC"}


def _send(monkeypatch, outcome):
    async def send(root, target, payload):
        return outcome

    monkeypatch.setattr(cli.config, "load", lambda root: {})
    monkeypatch.setattr(cli.config, "channel", lambda cfg, target: ("slack", None))
    monkeypatch.setattr(cli.config, "tokens", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_payload", lambda root: {"text": "sample"})
    monkeypatch.setattr(cli, "_sdk_problem", lambda: None)
    monkeypatch.setattr(cli, "_runtime", lambda transport: SimpleNamespace(send=send))


@pytest.mark.parametrize("code,word", [
    ("slack_identity_unreachable", "slack_identity_unreachable"),
    ("http_429", "http_429"),
    (None, "delivery_not_accepted"),
    ("Free text with spaces", "delivery_not_accepted"),
])
def test_not_sent_reports_the_connector_code(tmp_path, monkeypatch, capsys, code, word):
    _send(monkeypatch, {"result": "not_sent", "error_code": code})
    assert cli.main(["send", "--root", str(tmp_path), "--to", "slack:C_SYNTHETIC"]) == cli.EX_TEMPFAIL
    assert capsys.readouterr().err.split()[-1] == word


def _client(auth=None, bot=None, error=None):
    async def auth_test():
        if error:
            raise error
        return auth

    async def bots_info(**_kwargs):
        return bot

    return SimpleNamespace(auth_test=auth_test, bots_info=bots_info)


class SlackApiError(Exception):
    """Same type name as the SDK's answered-error exception."""

    def __init__(self, error, status=200):
        super().__init__(error)
        self.response = SimpleNamespace(status_code=status, data={"ok": False, "error": error})


@pytest.mark.parametrize("client,expected", [
    (_client(error=TimeoutError()), "slack_identity_unreachable"),
    (_client(error=OSError("synthetic network down")), "slack_identity_unreachable"),
    (_client(error=SlackApiError("invalid_auth")), "slack_identity_unverified"),
    (_client(error=SlackApiError("account_inactive")), "slack_identity_unverified"),
    # answered, but "try later": a rate limit or a Slack-side failure
    (_client(error=SlackApiError("ratelimited", status=429)), "slack_identity_unreachable"),
    (_client(error=SlackApiError("internal_error", status=500)), "slack_identity_unreachable"),
    (_client(error=SlackApiError("service_unavailable")), "slack_identity_unreachable"),
    (_client(auth={"ok": True, "team_id": "T_FOREIGN", "bot_id": "B1"}), "slack_identity_unverified"),
    (_client(auth={"ok": True, "team_id": "T_SYNTHETIC", "bot_id": "B1"},
             bot={"ok": True, "bot": {"id": "B1", "app_id": "A_SYNTHETIC", "deleted": False}}), None),
])
def test_slack_identity_tells_an_outage_from_a_wrong_token(client, expected):
    assert asyncio.run(slack._identity(client, SETTINGS)) == expected


def test_discord_unanswered_login_is_reported_as_unreachable(tmp_path, monkeypatch):
    class Bot:
        async def login(self, token):
            raise OSError("synthetic network down")

        async def close(self):
            pass

    monkeypatch.setattr(discord, "_payload", lambda root, payload: ("sample", []))
    import mcs_standalone.config as config
    monkeypatch.setattr(config, "connector_settings",
                        lambda *a, **k: {"channel_id": "1", "application_id": "2"})
    monkeypatch.setattr(config, "load_credentials", lambda *a: {"bot_token": "synthetic"})
    monkeypatch.setattr(discord, "_bot", lambda settings, gateway: Bot())
    outcome = asyncio.run(discord.send(tmp_path, "discord:1", {"text": "sample"}))
    assert outcome == {"result": "not_sent", "error_code": "discord_unreachable"}

    class Refused(Bot):
        async def login(self, token):
            raise type("LoginFailure", (Exception,), {})("synthetic")

    monkeypatch.setattr(discord, "_bot", lambda settings, gateway: Refused())
    outcome = asyncio.run(discord.send(tmp_path, "discord:1", {"text": "sample"}))
    assert outcome == {"result": "not_sent", "error_code": "loginfailure"}
