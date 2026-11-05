"""Synthetic mcs slack updates contracts with isolated shared fixtures."""
import asyncio
import json
import pytest
from adapters.common import envelopes
from notify_testkit import _dispatch, _intent, _latest_render, _seed_thread
from slack_testkit import ROOT_TS, SCOPE, SLACK, FakeClient, _granted_card, _mkworld, _rewrite_world, _updates
from slack_testkit import isolated_slack_ledger as isolated_slack_ledger
from mcs_slack_testkit import FakeSlackError as FakeSlackError, _pin_wall_clock as _pin_wall_clock, _rewrite as _rewrite


@pytest.mark.thread_bodies
def test_slack_update_binds_own_reply_foreign_never_binds(led):
    _seed_thread(led)
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    w = _mkworld(led)
    asyncio.run(_granted_card(w.worker, led, w.root))
    posted = len(w.client.thread_posts)
    spec = json.loads(_latest_render(led)["spec_json"])
    chunk = spec["parts"]["thread_body_parts"][0]
    our_ts = w.client.replies["1790000000.000001"][0]["ts"]

    # a foreign user typed the exact chunk text into the thread — it
    # must never satisfy a part's remote verification
    w.client.replies["1790000000.000001"].insert(0, {
        "ts": "1790000000.000099", "text": chunk, "user": "U_FOREIGN"})
    ctx = {"history": None, "consumed": set()}
    mid = asyncio.run(w.worker._remote_match(
        "1790000000.000001", chunk, ctx))
    assert mid == our_ts

    # with no provably-ours reply left, the part posts fresh rather
    # than impersonating the foreign message
    w.client.replies["1790000000.000001"] = [
        {"ts": "1790000000.000099", "text": chunk, "user": "U_FOREIGN"}]
    out = asyncio.run(w.worker._body_part(
        {**spec, "op": "update"}, chunk,
        {"thread_id": "1790000000.000001",
         "history": None, "consumed": set()}))
    assert out["result"] == "delivered"
    assert out["remote_id"] != "1790000000.000099"
    assert len(w.client.thread_posts) == posted + 1
    assert w.client.thread_posts[-1]["text"] == chunk


@pytest.mark.parametrize("kind", ["body_part", "attachment_part"])
@pytest.mark.parametrize("failure", ["timeout", "rejected", "malformed"])
@pytest.mark.thread_bodies
def test_unverified_slack_history_never_posts_or_uploads_again(
        led, monkeypatch, kind, failure):
    w, spec, _ = _rewrite_world(led)
    spec["delivery_id"] = "00000000-0000-4000-8000-00000000face"
    posted, uploaded = len(w.client.thread_posts), len(w.client.upload_calls)

    async def unreadable_replies(self, **kwargs):
        if failure == "timeout":
            raise TimeoutError("synthetic")
        if failure == "rejected":
            return {"ok": False, "error": "missing_scope"}
        return {"ok": True, "messages": [None]}

    monkeypatch.setattr(FakeClient, "conversations_replies", unreadable_replies)
    part = {"part_id": "body:0001" if kind == "body_part" else "file:0001",
            "kind": kind, "name": "synthetic.txt", "bytes": 1,
            "sha256": "a" * 64}
    claim = {"spec": spec, "payload_hash": envelopes.payload_hash(spec)}
    ctx = {"card_message_id": ROOT_TS, "thread_id": ROOT_TS,
           "history": None, "consumed": set()}

    async def scenario():
        await w.worker._attempt_part(claim, part, ctx)
        records = w.worker._jview.refresh()
        rows = records[envelopes.part_attempt_id(spec["delivery_id"], part["part_id"])]
        assert next(r for r in rows if r["phase"] == "result")["result"] == "unknown"
        await w.worker._drive_parts(claim, [part], ctx, records)

    asyncio.run(scenario())
    assert len(w.client.thread_posts) == posted
    assert len(w.client.upload_calls) == uploaded


@pytest.mark.parametrize("kind", ["body_part", "attachment_part"])
@pytest.mark.parametrize("pagination", [
    {"has_more": True}, {"response_metadata": {"next_cursor": "synthetic-next"}}],
    ids=["has_more", "next_cursor"])
@pytest.mark.thread_bodies
def test_incomplete_slack_history_never_authorizes_resend(
        led, monkeypatch, kind, pagination):
    w, spec, _ = _rewrite_world(led)
    spec["delivery_id"] = "00000000-0000-4000-8000-00000000fade"
    posted, uploaded = len(w.client.thread_posts), len(w.client.upload_calls)

    async def partial_replies(self, **kwargs):
        assert kwargs["limit"] == 200
        return {"ok": True, "messages": [], **pagination}

    monkeypatch.setattr(FakeClient, "conversations_replies", partial_replies)
    part = {"part_id": "body:0001" if kind == "body_part" else "file:0001",
            "kind": kind, "name": "synthetic.txt", "bytes": 1,
            "sha256": "a" * 64}
    claim = {"spec": spec, "payload_hash": envelopes.payload_hash(spec)}
    ctx = {"card_message_id": ROOT_TS, "thread_id": ROOT_TS,
           "history": None, "consumed": set()}

    async def scenario():
        await w.worker._attempt_part(claim, part, ctx)
        records = w.worker._jview.refresh()
        rows = records[envelopes.part_attempt_id(spec["delivery_id"], part["part_id"])]
        outcome = next(r for r in rows if r["phase"] == "result")
        assert outcome["result"] == "unknown"
        assert outcome["error_code"] == "runtimeerror"
        await w.worker._drive_parts(claim, [part], ctx, records)

    asyncio.run(scenario())
    assert len(w.client.thread_posts) == posted
    assert len(w.client.upload_calls) == uploaded


@pytest.mark.thread_bodies
def test_slack_update_rewrites_the_earlier_reply_of_a_changed_chunk(led):
    """The chunk's text changed after its first post (the extraction
    arrived): the update rewrites that reply — one reply per chunk, the
    same ts, never a second copy beside it."""
    w, spec, replies = _rewrite_world(led)
    posted, our_ts = len(w.client.thread_posts), replies[0]["ts"]
    out = _rewrite(w, spec, "変更後の本文", our_ts)
    assert out == {"result": "delivered", "remote_id": our_ts}
    assert len(w.client.thread_posts) == posted
    assert _updates(w) == [{"channel": SCOPE["channel_id"], "ts": our_ts,
                            "text": "変更後の本文", "link_names": False,
                            "mrkdwn": False}]
    assert replies[0]["text"] == "変更後の本文"


@pytest.mark.thread_bodies
def test_slack_body_special_characters_are_literal_and_rebind_exact_prior(led):
    from html import unescape
    w, spec, _ = _rewrite_world(led)
    body = "BP<90 & >60 <!channel> <@U0OP>"
    ctx = {"thread_id": ROOT_TS, "history": None, "consumed": set()}
    part = {"part_id": "body:0001", "kind": "body_part"}
    first = asyncio.run(w.worker._body_part(spec, body, ctx, part))
    posted = len(w.client.thread_posts)
    wire = w.client.thread_posts[-1]["text"]
    assert "<!channel>" not in wire and "<@U0OP>" not in wire
    assert unescape(wire) == body
    assert w.client.thread_posts[-1]["link_names"] is False
    bound = {**spec, "delivery": {**spec["delivery"], "thread_id": ROOT_TS, "message_id": ROOT_TS}}
    again = asyncio.run(w.worker._body_part(
        bound, body, {"thread_id": ROOT_TS, "history": None, "consumed": set()},
        {**part, "prior_remote_id": first["remote_id"]}))
    assert again == first and len(w.client.thread_posts) == posted


@pytest.mark.thread_bodies
def test_slack_rewrite_keeps_special_characters_literal_and_reuses_same_id(led):
    from html import unescape
    w, spec, replies = _rewrite_world(led)
    prior = replies[0]["ts"]
    body = "変更: <@U0OP> <!here> A&B <90"
    posted = len(w.client.thread_posts)
    out = _rewrite(w, spec, body, prior)
    assert out == {"result": "delivered", "remote_id": prior}
    update = _updates(w)[-1]
    assert "<!here>" not in update["text"] and "<@U0OP>" not in update["text"]
    assert unescape(update["text"]) == body and update["link_names"] is False
    assert _rewrite(w, spec, body, prior) == out
    assert len(_updates(w)) == 1 and len(w.client.thread_posts) == posted


@pytest.mark.thread_bodies
@pytest.mark.parametrize("legacy", [False, True])
def test_slack_rewrite_never_touches_a_foreign_or_missing_reply(led, legacy):
    w, spec, replies = _rewrite_world(led)
    if legacy:
        spec = {**spec, "parts": {k: v for k, v in spec["parts"].items() if k != "post_source_id"}}
    posted = len(w.client.thread_posts)
    replies.append({"ts": "1790000000.000099", "text": "他人",
                    "user": "U_FOREIGN"})
    priors = ["1790000000.000099",      # someone else's reply
              "1790000000.000555",      # not in the thread
              ROOT_TS,                  # the card itself
              "not-a-ts"]
    for i, prior in enumerate(priors):
        out = _rewrite(w, spec, f"変更後 {i}", prior)
        if legacy:
            assert out["result"] == "delivered" and out["remote_id"] != prior
        else:
            assert out == {"result": "unknown", "error_code": "edit_target_unverified"}
    assert not _updates(w)
    assert len(w.client.thread_posts) == posted + (len(priors) if legacy else 0)


@pytest.mark.parametrize("error", ["message_not_found",
                                   "cant_update_message"])
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.thread_bodies
def test_slack_rejected_update_respects_post_ownership(led, error, legacy):
    w, spec, replies = _rewrite_world(led)
    if legacy:
        spec = {**spec, "parts": {k: v for k, v in spec["parts"].items() if k != "post_source_id"}}
    posted, our_ts = len(w.client.thread_posts), replies[0]["ts"]
    w.client.update_failure = FakeSlackError(200, error)
    out = _rewrite(w, spec, "変更後の本文", our_ts)
    if legacy:
        assert out["result"] == "delivered" and out["remote_id"] != our_ts
        assert len(w.client.thread_posts) == posted + 1
        assert w.client.thread_posts[-1]["text"] == "変更後の本文"
    else:
        assert out == ({"result": "unknown", "error_code": "edit_target_unverified"}
                       if error == "message_not_found" else {"result": "not_sent", "error_code": error})
        assert len(w.client.thread_posts) == posted


@pytest.mark.parametrize("status,error", [(429, "ratelimited"),
                                          (200, "invalid_blocks")])
@pytest.mark.thread_bodies
def test_slack_rate_limited_update_never_posts_a_duplicate(led, status, error):
    """Regression: a 429 on chat.update fell back to chat.postMessage,
    leaving the old reply plus a notifying duplicate (mass re-render)."""
    w, spec, replies = _rewrite_world(led)
    posted, our_ts = len(w.client.thread_posts), replies[0]["ts"]
    w.client.update_failure = FakeSlackError(status, error)
    out = _rewrite(w, spec, "変更後の本文", our_ts)
    assert out == {"result": "not_sent", "error_code": error}
    assert len(w.client.thread_posts) == posted


@pytest.mark.thread_bodies
def test_slack_unknown_update_outcome_never_posts_a_second_reply(led):
    w, spec, replies = _rewrite_world(led)
    posted, our_ts = len(w.client.thread_posts), replies[0]["ts"]
    w.client.update_failure = TimeoutError("synthetic")   # may have landed
    out = _rewrite(w, spec, "変更後の本文", our_ts)
    assert out["result"] == "unknown"
    assert len(w.client.thread_posts) == posted


@pytest.mark.thread_bodies
@pytest.mark.parametrize("legacy", [False, True])
def test_slack_one_reply_is_rewritten_for_only_one_chunk(led, legacy):
    w, spec, replies = _rewrite_world(led)
    if legacy:
        spec = {**spec, "parts": {k: v for k, v in spec["parts"].items() if k != "post_source_id"}}
    posted, our_ts = len(w.client.thread_posts), replies[0]["ts"]
    ctx = {"thread_id": ROOT_TS, "history": None, "consumed": set()}
    first = _rewrite(w, spec, "新1", our_ts, ctx=ctx)
    second = _rewrite(w, spec, "新2", our_ts, "body:0002", ctx=ctx)
    assert first["remote_id"] == our_ts
    if legacy:
        assert second["result"] == "delivered" and second["remote_id"] != our_ts
    else:
        assert second == {"result": "unknown", "error_code": "edit_target_unverified"}
    assert len(w.client.thread_posts) == posted + int(legacy)


@pytest.mark.thread_bodies
def test_slack_rewrite_survives_a_crash_between_update_and_result(led):
    """A rerun after the update landed but before its result was
    journaled finds the new text already remote and binds to it — no
    second update, no second reply."""
    w, spec, replies = _rewrite_world(led)
    posted, our_ts = len(w.client.thread_posts), replies[0]["ts"]
    first = _rewrite(w, spec, "変更後の本文", our_ts)
    again = _rewrite(w, spec, "変更後の本文", our_ts)     # fresh ctx
    assert first == again == {"result": "delivered", "remote_id": our_ts}
    assert len(_updates(w)) == 1
    assert len(w.client.thread_posts) == posted
