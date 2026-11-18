"""The private provider constructs a LINE WORKS user-message request only."""
from urllib.parse import parse_qs

import pytest

from adapters.lineworks import delivery
from adapters.lineworks import private_previsit
from lineworks_client_testkit import TOKEN, Wire, client


@pytest.fixture
def private_provider(monkeypatch, tmp_path):
    user_id = "staff-synthetic@example.invalid"
    scope = {"transport": "lineworks", "profile": "synthetic",
             "application_id": "2000001", "team_id": "team-synthetic",
             "route_epoch": 4, "allowed_user_ids": frozenset({user_id}),
             "project_ids": frozenset({1}), "channel_id": "shared-synthetic",
             "data_root": str(tmp_path / "data")}
    wire = Wire([(200, TOKEN), (201, b"")])
    api = client(wire)
    monkeypatch.setattr(private_previsit, "settings",
                        lambda root, require_interactive=False: dict(scope))
    monkeypatch.setattr(private_previsit, "load_credentials",
                        lambda root, current: (api, "synthetic-secret"))
    monkeypatch.setattr(delivery.Sender, "route_current", lambda self, spec: True)
    account_scope = {key: scope[key] for key in
                     ("profile", "application_id", "team_id", "route_epoch")}
    return private_previsit.PrivatePrevisitProvider(str(tmp_path)), wire, user_id, account_scope


def test_private_provider_builds_user_targeted_request_with_bot_message_scope(private_provider):
    provider, wire, user_id, account_scope = private_provider
    result = provider.send("合成の訪問前メモ", project_id=1, recipient_id=user_id,
                           transport="lineworks", account_scope=account_scope,
                           verify_authorization=lambda: True)
    assert result == {"result": "delivered", "accepted_ref": "lineworks:201"}
    form = parse_qs(wire.calls[0][3].decode())
    assert form["scope"] == ["bot.message"]
    assert wire.calls[1][1].endswith("/bots/2000001/users/staff-synthetic%40example.invalid/messages")
    assert "/channels/" not in wire.calls[1][1]
    assert wire.calls[1][1] != "shared-synthetic"


@pytest.mark.parametrize("change", ["profile", "application_id", "team_id", "route_epoch",
                                     "recipient", "project", "transport"])
def test_private_provider_rejects_uncertain_scope_without_request(private_provider, change):
    provider, wire, user_id, account_scope = private_provider
    kwargs = {"project_id": 1, "recipient_id": user_id, "transport": "lineworks",
              "account_scope": dict(account_scope), "verify_authorization": lambda: True}
    if change in ("profile", "application_id", "team_id", "route_epoch"):
        kwargs["account_scope"][change] = "other" if change != "route_epoch" else 5
    elif change == "recipient":
        kwargs["recipient_id"] = "not-allowed@example.invalid"
    elif change == "project":
        kwargs["project_id"] = 2
    else:
        kwargs["transport"] = "slack"
    result = provider.send("合成", **kwargs)
    assert result["result"] == "not_sent"
    assert result["error_code"] == ("unsupported_transport" if change == "transport"
                                    else "private_verification_failed")
    assert wire.calls == []


def test_private_provider_requires_nonempty_explicit_user_id(private_provider):
    provider, wire, _user_id, account_scope = private_provider
    result = provider.send("合成", project_id=1, recipient_id="", transport="lineworks",
                           account_scope=account_scope)
    assert result == {"result": "not_sent", "error_code": "validation_invalid"}
    assert wire.calls == []


def test_private_provider_rejects_client_with_wrong_bot_id(private_provider, monkeypatch):
    provider, wire, user_id, account_scope = private_provider

    class WrongBotClient:
        credentials = type("Credentials", (), {"bot_id": "wrong-bot"})()

    monkeypatch.setattr(private_previsit, "load_credentials",
                        lambda root, current: (WrongBotClient(), "synthetic-secret"))
    result = provider.send("合成", project_id=1, recipient_id=user_id, transport="lineworks",
                           account_scope=account_scope)
    assert result == {"result": "not_sent", "error_code": "private_verification_failed"}
    assert wire.calls == []
