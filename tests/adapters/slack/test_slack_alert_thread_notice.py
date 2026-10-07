"""Native Slack thread notices never fall back to a channel post."""
import asyncio
from types import SimpleNamespace

import pytest

from adapters.slack.delivery import SlackCardAdapter
from slack_card_testkit import _spec
from slack_testkit import FakeClient, SCOPE


@pytest.mark.parametrize("kind", ["notice", "signal"])
@pytest.mark.parametrize("fault", [None, "foreign_root", "gone", "read_timeout", "wrong_thread"])
def test_native_alert_only_posts_in_verified_original_thread(fault, kind):
    async def scenario():
        client = FakeClient()
        sender = SlackCardAdapter(SimpleNamespace(client=client),
            team_id=SCOPE["team_id"], application_id=SCOPE["application_id"],
            channel_id=SCOPE["channel_id"], profile="cco", allowed_user_ids={"U_SYNTHETIC"})
        assert await sender.bind()
        spec = _spec(text="[MCS] 完全合成のアラート")
        spec["op"] = "notice" if kind == "notice" else "create"
        spec["kind"] = "signal"
        spec["parts"]["thread_notice" if kind == "notice" else "source_thread"] = True
        spec["parts"]["action_rows"] = []
        root = "1790000000.000001"
        spec["delivery"].update(message_id=root, thread_id=root)
        if fault in ("foreign_root", "gone", "read_timeout"):
            async def replies(**kwargs):
                if fault == "read_timeout":
                    raise TimeoutError("synthetic")
                return {"ok": True, "messages": [] if fault == "gone" else [
                    {"ts": root, "bot_id": "B_FOREIGN"}]}
            client.conversations_replies = replies
        elif fault == "wrong_thread":
            spec["delivery"]["thread_id"] = "1790000000.999999"
            if kind == "signal":
                async def wrong_root(**kwargs):
                    return {"ok": True, "messages": [{"ts": root, "bot_id": client.bot_id}]}
                client.conversations_replies = wrong_root
        outcome = await sender.perform(spec)
        posts = [kwargs for method, kwargs in client.calls if method == "create"]
        if fault:
            assert outcome["result"] == "not_sent" and posts == []
        else:
            assert outcome["result"] == "delivered" and len(posts) == 1
            assert posts[0]["thread_ts"] == root and posts[0]["channel"] == SCOPE["channel_id"]
            assert posts[0]["link_names"] is False
        assert not client.ephemeral_calls
    asyncio.run(scenario())


def test_slack_thread_root_is_verified_with_bounded_channel_history():
    async def scenario():
        client = FakeClient()
        calls = []
        root = "1790000000.000001"

        async def history(**kwargs):
            calls.append(kwargs)
            return {"ok": True, "messages": [{"ts": root, "bot_id": client.bot_id}]}

        client.conversations_history = history
        sender = SlackCardAdapter(SimpleNamespace(client=client),
            team_id=SCOPE["team_id"], application_id=SCOPE["application_id"],
            channel_id=SCOPE["channel_id"], profile="cco", allowed_user_ids={"U_SYNTHETIC"})
        assert await sender.bind()
        spec = _spec()
        spec["op"] = "notice"
        spec["parts"].update(thread_notice=True, action_rows=[])
        spec["delivery"].update(message_id=root, thread_id=root)
        assert (await sender.perform(spec))["result"] == "delivered"
        assert calls == [{"channel": SCOPE["channel_id"], "oldest": root,
                          "latest": root, "inclusive": True, "limit": 1}]
        assert client.thread_posts[0]["thread_ts"] == root
    asyncio.run(scenario())
