"""Synthetic late replies stay in their verified parent thread."""
import asyncio
import pytest

from slack_testkit import isolated_slack_ledger as led, _rewrite_world, ROOT_TS
from adapters.common.spec import post_scoped

__all__ = ["led"]
pytestmark = pytest.mark.thread_bodies


def _post(w, spec, part, text="合成の遅延返信本文", history=None):
    ctx = {"thread_id": ROOT_TS, "history": history, "consumed": set()}
    before = len(w.client.thread_posts)
    out = asyncio.run(w.worker._body_part(spec, text, ctx, part))
    return out, w.client.thread_posts[before:]


def test_a_new_post_on_an_existing_card_stays_in_thread(led):
    w, spec, _ = _rewrite_world(led)
    out, posts = _post(w, spec, {"part_id": "body:0002", "kind": "body_part",
                                 "name": "m:202#1"})
    assert out["result"] == "delivered"
    assert len(posts) == 1 and "reply_broadcast" not in posts[0]
    assert posts[0]["thread_ts"] == ROOT_TS


def test_card_creation_keeps_body_posts_in_the_thread(led):
    w, spec, _ = _rewrite_world(led)
    spec = {**spec, "op": "create"}
    _, posts = _post(w, spec, {"part_id": "body:0001", "kind": "body_part",
                               "name": "m:201#1"})
    assert len(posts) == 1 and "reply_broadcast" not in posts[0]


def test_later_chunks_and_captions_stay_in_the_thread(led):
    w, spec, _ = _rewrite_world(led)
    _, posts = _post(w, spec, {"part_id": "body:0003", "kind": "body_part",
                               "name": "m:202#2"})
    assert len(posts) == 1 and "reply_broadcast" not in posts[0]
    _, posts = _post(w, spec, {"part_id": "attach:0001", "kind": "attachment_part",
                               "name": "m:202#1"}, text="合成の添付見出し")
    assert all("reply_broadcast" not in p for p in posts)


def test_missing_scoped_reply_is_not_reposted_or_marked_delivered(led):
    w, spec, _ = _rewrite_world(led)
    spec = {**spec, "delivery": {**spec["delivery"], "thread_id": ROOT_TS}}
    assert post_scoped(spec)
    part = {"part_id": "body:0002", "kind": "body_part",
            "name": "m:202#1", "prior_remote_id": "1790000000.000777"}
    assert all(reply["ts"] != part["prior_remote_id"] for reply in w.client.replies[ROOT_TS])
    before = len(w.client.calls)
    for _ in range(2):
        out, posts = _post(w, spec, part)
        assert out == {"result": "unknown", "error_code": "edit_target_unverified"}
        assert posts == []
    calls = w.client.calls[before:]
    assert any(kind == "replies" for kind, _ in calls)
    assert not any(kind in {"create", "update"} for kind, _ in calls)


def test_a_scoped_update_rebinds_its_exact_prior_without_a_second_post(led):
    w, spec, replies = _rewrite_world(led)
    part = {"part_id": "body:0002", "kind": "body_part", "name": "m:202#1"}
    first_result, first = _post(w, spec, part)
    assert len(first) == 1
    assert first[0]["thread_ts"] == ROOT_TS and "reply_broadcast" not in first[0]
    assert first_result == {"result": "delivered", "remote_id": replies[-1]["ts"]}
    out, again = _post(w, spec, {**part, "prior_remote_id": first_result["remote_id"]})
    assert out == first_result
    assert again == []


def test_signal_and_legacy_posts_stay_in_the_thread(led):
    w, spec, _ = _rewrite_world(led)
    for name in ("s:0123456789abcdef#1", "legacy#1", "display#1", "truncated#1"):
        _, posts = _post(w, spec, {"part_id": "body:0009", "kind": "body_part",
                                   "name": name}, text="合成 " + name)
        assert len(posts) == 1 and "reply_broadcast" not in posts[0]
