"""A post new to an already delivered card's thread is also shown in the
channel (reply_broadcast) so a late reply still notifies; card creation,
rewrites, re-posts of an uneditable reply, later chunks, captions and
remote-matched retries stay thread-only. Synthetic world only."""
import asyncio

from test_mcs_slack import isolated_slack_ledger as led, _rewrite_world, ROOT_TS

__all__ = ["led"]


def _post(w, spec, part, text="合成の遅延返信本文", history=None):
    ctx = {"thread_id": ROOT_TS, "history": history, "consumed": set()}
    before = len(w.client.thread_posts)
    out = asyncio.run(w.worker._body_part(spec, text, ctx, part))
    return out, w.client.thread_posts[before:]


def test_a_new_post_on_an_existing_card_reaches_the_channel(led):
    w, spec, _ = _rewrite_world(led)
    out, posts = _post(w, spec, {"part_id": "body:0002", "kind": "body_part",
                                 "name": "m:202#1"})
    assert out["result"] == "delivered"
    assert len(posts) == 1 and posts[0]["reply_broadcast"] is True
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


def test_a_repost_of_an_uneditable_reply_is_not_broadcast(led):
    w, spec, _ = _rewrite_world(led)
    # the prior reply is gone from the thread: the rewrite falls back to
    # a fresh post, which replaces a post the channel already saw
    _, posts = _post(w, spec, {"part_id": "body:0002", "kind": "body_part",
                               "name": "m:202#1",
                               "prior_remote_id": "1790000000.000777"})
    assert len(posts) == 1 and "reply_broadcast" not in posts[0]


def test_a_retry_rebinds_the_broadcast_reply_without_a_second_post(led):
    w, spec, replies = _rewrite_world(led)
    part = {"part_id": "body:0002", "kind": "body_part", "name": "m:202#1"}
    _, first = _post(w, spec, part)
    assert len(first) == 1
    out, again = _post(w, spec, part)
    assert out == {"result": "delivered", "remote_id": replies[-1]["ts"]}
    assert again == []


def test_signal_and_legacy_posts_stay_in_the_thread(led):
    w, spec, _ = _rewrite_world(led)
    for name in ("s:0123456789abcdef#1", "legacy#1", "display#1", "truncated#1"):
        _, posts = _post(w, spec, {"part_id": "body:0009", "kind": "body_part",
                                   "name": name}, text="合成 " + name)
        assert len(posts) == 1 and "reply_broadcast" not in posts[0]
