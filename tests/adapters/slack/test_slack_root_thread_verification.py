"""A missing or unverified Slack root never certifies a thread part."""
import asyncio

import pytest
import notify_spool

from slack_testkit import ROOT_TS, _mkworld
from slack_testkit import isolated_slack_ledger as led

__all__ = ["led"]


@pytest.mark.parametrize("fault", ["deleted", "foreign", "wrong_root", "bad_row", "read_failure"])
def test_thread_part_requires_current_owned_root(led, fault):
    notify_spool.ensure_dirs(notify_spool.data_root(led))
    world = _mkworld(led)
    assert asyncio.run(world.sender.bind())

    async def replies(**kwargs):
        if fault == "read_failure":
            raise TimeoutError("synthetic failure")
        if fault == "deleted":
            return {"ok": True, "messages": []}
        if fault == "bad_row":
            return {"ok": True, "messages": [None]}
        return {"ok": True, "messages": [{
            "ts": ROOT_TS if fault == "foreign" else "1790000000.123456",
            "bot_id": "B_OTHER" if fault == "foreign" else world.client.bot_id}]}

    world.client.conversations_replies = replies
    result = asyncio.run(world.worker._perform_part(
        {"spec": {"delivery": {"thread_id": ROOT_TS}}},
        {"kind": "thread"}, {"card_message_id": ROOT_TS}))
    assert result == {"result": "not_sent", "error_code":
                      "thread_prefetch_failed" if fault == "read_failure" else "thread_root_unverified"}
    assert not world.client.thread_posts and not world.client.upload_calls
    assert not any(op in ("create", "update") for op, _ in world.client.calls)


def test_thread_part_reports_only_verified_root(led):
    notify_spool.ensure_dirs(notify_spool.data_root(led))
    world = _mkworld(led)
    assert asyncio.run(world.sender.bind())
    result = asyncio.run(world.worker._perform_part(
        {"spec": {"delivery": {"thread_id": ROOT_TS}}},
        {"kind": "thread"}, {"card_message_id": ROOT_TS}))
    assert result == {"result": "delivered", "remote_id": ROOT_TS}
