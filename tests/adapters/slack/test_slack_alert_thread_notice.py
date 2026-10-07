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


@pytest.mark.parametrize("gone", [False, True])
def test_signal_revoke_deletes_only_sealed_reply_without_reading_or_resending_source(gone):
    async def scenario():
        client = FakeClient()
        sender = SlackCardAdapter(SimpleNamespace(client=client),
            team_id=SCOPE["team_id"], application_id=SCOPE["application_id"],
            channel_id=SCOPE["channel_id"], profile="cco", allowed_user_ids={"U_SYNTHETIC"})
        assert await sender.bind()
        calls = []
        async def denied_read(**kwargs):
            pytest.fail("revoke must not read a deleted original or resolve mentions")
        async def delete(**kwargs):
            calls.append(kwargs)
            return {"ok": False, "error": "message_not_found"} if gone else {"ok": True}
        client.conversations_history = denied_read
        client.conversations_replies = denied_read
        client.users_info = denied_read
        client.chat_delete = delete
        spec = _spec(text="PRIVATE-SYNTHETIC-OLD-CONTENT <@U_SYNTHETIC>")
        spec.update(op="revoke", kind="signal")
        spec["parts"]["source_thread"] = True
        spec["delivery"].update(message_id="1790000000.000123", thread_id="1790000000.000001")
        assert (await sender.perform(spec))["result"] == "delivered"
        assert calls == [{"channel": SCOPE["channel_id"], "ts": "1790000000.000123"}]
        assert not client.thread_posts
    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["unsupported", "malformed"])
def test_signal_revoke_rejects_bad_spec_before_delete(fault):
    async def scenario():
        client = FakeClient()
        sender = SlackCardAdapter(SimpleNamespace(client=client),
            team_id=SCOPE["team_id"], application_id=SCOPE["application_id"],
            channel_id=SCOPE["channel_id"], profile="cco", allowed_user_ids={"U_SYNTHETIC"})
        assert await sender.bind()
        spec = _spec()
        spec.update(op="revoke", kind="signal")
        spec["parts"]["source_thread"] = True
        spec["delivery"].update(message_id="1790000000.000123", thread_id="1790000000.000001")
        if fault == "unsupported":
            spec["parts"]["unsupported_future_feature"] = True
        else:
            spec["parts"]["containers"] = [{"type": "text", "text": 123}]
        outcome = await sender.perform(spec)
        assert outcome["result"] == "not_sent" and outcome["error_code"] == "bad_render"
        assert not any(method == "delete" for method, _kwargs in client.calls)
    asyncio.run(scenario())
