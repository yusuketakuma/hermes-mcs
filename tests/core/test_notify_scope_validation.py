"""The LINE WORKS and standalone Slack/Discord project-scope checks keep their
exact messages and order (synthetic config only)."""
import pytest

import mcs_config

AUTO = "notify.{t}.project_ids_auto: true（有効）かfalse（無効）を指定してください"
IDS = "notify.{t}.project_ids: 0より大きい整数のリスト、またはproject_ids_auto=trueを指定してください"

CASES = [
    ({"project_ids": [1, 2**63 - 1]}, []),
    ({"project_ids": [], "project_ids_auto": True}, []),
    ({}, [IDS]),
    ({"project_ids": [], "project_ids_auto": False}, [IDS]),
    ({"project_ids": [0]}, [IDS]),
    ({"project_ids": [2**63]}, [IDS]),
    ({"project_ids": [True]}, [IDS]),
    ({"project_ids": [1.0]}, [IDS]),
    ({"project_ids": "1"}, [IDS]),
    ({"project_ids": [1], "project_ids_auto": "true"}, [AUTO]),
    ({"project_ids": [], "project_ids_auto": 1}, [AUTO, IDS]),     # auto first, then ids
]


def _scope_errors(errors, transport):
    prefix = f"notify.{transport}.project_ids"
    return [e for e in errors if e.startswith(prefix)]


@pytest.mark.parametrize("scope,expected", CASES)
def test_lineworks_scope_messages(scope, expected):
    block = {"profile": "p", "application_id": "1", "team_id": "1", "channel_id": "c",
             "allowed_user_ids": ["u"], **scope}
    errors = mcs_config.validate_notify({"interactive": "lineworks", "lineworks": block})
    assert _scope_errors(errors, "lineworks") == [m.format(t="lineworks") for m in expected]


@pytest.mark.parametrize("transport", ["slack", "discord"])
@pytest.mark.parametrize("scope,expected", CASES)
def test_standalone_scope_messages(transport, scope, expected):
    cfg = {"notify": {"interactive": transport,
                      transport: {"allowed_user_ids": ["u"], **scope}}}
    errors = mcs_config.validate_standalone_scope(cfg)
    assert _scope_errors(errors, transport) == [m.format(t=transport) for m in expected]
