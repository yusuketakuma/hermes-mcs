"""Synthetic mcs discord delivery updates contracts with isolated shared fixtures."""
from __future__ import annotations
import asyncio
import types
import pytest
from discord_delivery_testkit import FOREIGN_USER, FakeHTTP, FakeThread, _chunks, _claim, _HistMsg, _mkworker, _sent_parts, _spec, _state
from discord_delivery_testkit import synthetic_discord_sdk as synthetic_discord_sdk
from mcs_discord_delivery_testkit import _prior_thread as _prior_thread, _update_spec as _update_spec


def test_update_backfill_dedupe_remote_verified(tmp_path):
    """An update render against an already-populated thread marks each
    part delivered against the REAL remote message it matched — never a
    second copy, never a claimed-but-unverified skip."""
    chunks = _chunks(3)
    w, reg, bot = _mkworker(tmp_path)
    th = FakeThread(7700)
    bot.channels[42].threads.append(th)
    # the create render already posted all chunks — with real remote ids
    for c in chunks:
        asyncio.run(th.send(c))
    spec = _spec(chunks, op="update", thread_id="7700")
    claim = _claim(spec)
    asyncio.run(w._deliver_parts(claim, "9001"))
    assert [m.content for m in th.sent] == chunks     # zero new posts
    parts = _sent_parts(_state(tmp_path))
    body = {k: v for k, v in parts.items() if k.startswith("body:")}
    assert len(body) == 3
    for part_id, row in body.items():
        i = int(part_id.split(":")[1]) - 1
        assert row["result"] == "delivered"
        # bound to the existing remote message, not a fresh post
        assert row["remote_id"] == str(th.sent[i].id)


def test_update_backfill_posts_only_missing_parts(tmp_path):
    chunks = _chunks(4)
    w, reg, bot = _mkworker(tmp_path)
    th = FakeThread(7700)
    bot.channels[42].threads.append(th)
    asyncio.run(th.send(chunks[0]))             # only part 1 got out
    spec = _spec(chunks, op="update", thread_id="7700")
    claim = _claim(spec)
    asyncio.run(w._deliver_parts(claim, "9001"))
    assert [m.content for m in th.sent] == chunks
    parts = _sent_parts(_state(tmp_path))
    assert parts["body:0001"]["remote_id"] == str(th.sent[0].id)
    for i in (1, 2, 3):
        assert parts[f"body:{i + 1:04d}"]["remote_id"] \
            == str(th.sent[i].id)


def test_remote_match_never_binds_foreign_content(tmp_path):
    """Unrelated remote content — even byte-identical, even inside the
    scan window — is not our part. Only messages this bot authored can
    be bound as remote-verified deliveries."""
    chunks = _chunks(2)
    w, reg, bot = _mkworker(tmp_path)
    th = FakeThread(7700)
    bot.channels[42].threads.append(th)
    # a foreign user happens to have posted the same text earlier
    th.sent.append(_HistMsg(7001, chunks[0], author=FOREIGN_USER))
    spec = _spec(chunks, op="update", thread_id="7700")
    claim = _claim(spec)
    asyncio.run(w._deliver_parts(claim, "9001"))
    # the part posted its own copy; the foreign id was never consumed
    assert len(th.sent) == 1 + len(chunks)
    parts = _sent_parts(_state(tmp_path))
    for i in range(len(chunks)):
        row = parts[f"body:{i + 1:04d}"]
        assert row["result"] == "delivered"
        assert row["remote_id"] != "7001"
        assert row["remote_id"] == str(th.sent[1 + i].id)


@pytest.mark.parametrize("identity", [None, types.SimpleNamespace()])
def test_remote_match_without_bot_identity_never_proves_foreign_delivery(tmp_path, identity):
    w, reg, bot = _mkworker(tmp_path)
    bot.user = identity
    th = FakeThread(7700)
    body = "架空の同一本文"
    th.sent.append(_HistMsg(7001, body, author=FOREIGN_USER))
    ctx = {"history": None, "consumed": set()}
    assert asyncio.run(w._remote_match(th, body, ctx)) is None
    assert ctx["consumed"] == set()


def test_update_rewrites_the_earlier_post_of_a_changed_chunk(tmp_path):
    """The chunk's text changed after its first post (the extraction
    arrived): the update edits that post — one post per chunk, the same
    remote id, never a second copy beside it."""
    w, bot, th = _prior_thread(tmp_path, ["記録 (構造化なし)"])
    new = ["記録 (構造化なし)\n📋 構造化 …"]
    spec = _update_spec(new, th, {0: 0})
    asyncio.run(w._deliver_parts(_claim(spec), "9001"))
    assert [m.content for m in th.sent] == new
    assert th.sent[0].edits == 1
    row = _sent_parts(_state(tmp_path))["body:0001"]
    assert row["result"] == "delivered"
    assert row["remote_id"] == str(th.sent[0].id)


def test_unchanged_chunk_is_bound_without_an_edit(tmp_path):
    w, bot, th = _prior_thread(tmp_path, ["同じ本文"])
    spec = _update_spec(["同じ本文"], th, {0: 0})
    asyncio.run(w._deliver_parts(_claim(spec), "9001"))
    assert len(th.sent) == 1 and th.sent[0].edits == 0
    row = _sent_parts(_state(tmp_path))["body:0001"]
    assert row["remote_id"] == str(th.sent[0].id)


def test_rewrite_of_a_deleted_post_falls_back_to_a_new_post(tmp_path):
    w, bot, th = _prior_thread(tmp_path, [])
    spec = _spec(["新しい本文"], op="update", thread_id="7700",
                 prior={"body:0001": "6001"})     # nobody holds 6001
    asyncio.run(w._deliver_parts(_claim(spec), "9001"))
    assert [m.content for m in th.sent] == ["新しい本文"]
    row = _sent_parts(_state(tmp_path))["body:0001"]
    assert row["result"] == "delivered"
    assert row["remote_id"] == str(th.sent[0].id)


def test_rewrite_never_edits_a_foreign_message(tmp_path):
    w, reg, bot = _mkworker(tmp_path)
    th = FakeThread(7700)
    bot.channels[42].threads.append(th)
    th.sent.append(_HistMsg(7001, "他人の投稿", author=FOREIGN_USER))
    spec = _spec(["新しい本文"], op="update", thread_id="7700",
                 prior={"body:0001": "7001"})
    asyncio.run(w._deliver_parts(_claim(spec), "9001"))
    assert th.sent[0].content == "他人の投稿" and th.sent[0].edits == 0
    assert th.sent[1].content == "新しい本文"
    row = _sent_parts(_state(tmp_path))["body:0001"]
    assert row["remote_id"] == str(th.sent[1].id)


@pytest.mark.parametrize("status", [400, 403])
def test_rejected_edit_falls_back_to_a_new_post(tmp_path, status):
    """An edit the API refuses outright (an archived thread answers 400)
    never committed, so the chunk is posted afresh — the behaviour
    before in-place rewrites, and no worse."""
    w, bot, th = _prior_thread(tmp_path, ["旧"])
    th.sent[0].edit_fail = FakeHTTP(status)
    spec = _update_spec(["新"], th, {0: 0})
    asyncio.run(w._deliver_parts(_claim(spec), "9001"))
    assert [m.content for m in th.sent] == ["旧", "新"]
    row = _sent_parts(_state(tmp_path))["body:0001"]
    assert row["result"] == "delivered"
    assert row["remote_id"] == str(th.sent[1].id)


def test_unknown_edit_outcome_never_posts_a_second_copy(tmp_path):
    w, bot, th = _prior_thread(tmp_path, ["旧"])
    th.sent[0].edit_fail = FakeHTTP(500)      # may or may not have landed
    spec = _update_spec(["新"], th, {0: 0})
    asyncio.run(w._deliver_parts(_claim(spec), "9001"))
    assert len(th.sent) == 1
    assert _sent_parts(_state(tmp_path))["body:0001"]["result"] == "unknown"


def test_one_post_is_rewritten_for_only_one_chunk(tmp_path):
    w, bot, th = _prior_thread(tmp_path, ["旧"])
    spec = _update_spec(["新1", "新2"], th, {0: 0, 1: 0})   # both name it
    asyncio.run(w._deliver_parts(_claim(spec), "9001"))
    assert [m.content for m in th.sent] == ["新1", "新2"]
    assert th.sent[0].edits == 1
    parts = _sent_parts(_state(tmp_path))
    assert parts["body:0001"]["remote_id"] == str(th.sent[0].id)
    assert parts["body:0002"]["remote_id"] == str(th.sent[1].id)


def test_rewrite_survives_a_crash_between_edit_and_result(tmp_path):
    """A rerun after the edit landed but before its result was journaled
    finds the new text already remote and binds to it — no second edit,
    no second post."""
    w, bot, th = _prior_thread(tmp_path, ["旧"])
    spec = _update_spec(["新"], th, {0: 0})
    part = next(p for p in spec["parts"]["manifest"]
                if p["part_id"] == "body:0001")

    def ctx():
        return {"card_message_id": "9001", "thread": None,
                "thread_id": "7700", "history": None, "consumed": set()}

    first = asyncio.run(w._perform_part(_claim(spec), part, ctx()))
    again = asyncio.run(w._perform_part(_claim(spec), part, ctx()))
    want = {"result": "delivered", "remote_id": str(th.sent[0].id)}
    assert first == again == want
    assert len(th.sent) == 1 and th.sent[0].edits == 1
