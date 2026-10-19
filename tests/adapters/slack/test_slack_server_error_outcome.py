"""Slack's ok:false ``internal_error`` / ``fatal_error``
answers say "it's possible some aspect of the operation succeeded" —
the post may have landed, so the outcome must stay unknown (never
resent), not a definitive not_sent."""
import asyncio
from types import SimpleNamespace

import pytest

from adapters.slack.delivery import SlackCardAdapter
from slack_card_testkit import _spec
from slack_testkit import FakeClient


class SlackServerError(Exception):
    """SlackApiError-shaped: HTTP 200 body {ok: false, error: <code>}."""
    def __init__(self, error):
        super().__init__(error)
        self.response = SimpleNamespace(status_code=200,
                                        data={"ok": False, "error": error},
                                        headers={})


@pytest.mark.parametrize("error", ["internal_error", "fatal_error"])
def test_slack_server_side_error_after_post_is_unknown(error):
    async def scenario():
        client = FakeClient()
        adapter = SlackCardAdapter(
            SimpleNamespace(client=client), team_id="T_SYNTHETIC",
            application_id="A_SYNTHETIC", channel_id="C_SYNTHETIC",
            profile="cco", allowed_user_ids={"U_OPERATOR"})
        assert await adapter.bind()
        client.failure = SlackServerError(error)
        outcome = await adapter.perform(_spec())
        assert [k for k, _ in client.calls].count("create") == 1
        assert outcome["result"] == "unknown", outcome

    asyncio.run(scenario())
