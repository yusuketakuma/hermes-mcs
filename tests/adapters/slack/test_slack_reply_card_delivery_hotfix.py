"""Reply-card parts retain their own remote identity inside a shared Slack thread."""
import asyncio
import hashlib
import json

import pytest

from adapters.common.spec import token_map
import notify_parts
import notify_spool
import present_slack
from notify_testkit import NOW, _dispatch, _intent, _latest_render, _llm_extract, _msg, _seed_thread
from slack_card_testkit import _spec
from slack_testkit import ROOT_TS, SCOPE, SLACK, _granted_card, _mkworld, _updates
from slack_testkit import isolated_slack_ledger as led

__all__ = ["led"]


def _post_spec(mid, marker):
    spec = _spec()
    spec["card_key"] = f"synthetic-card-{mid}"
    spec["delivery"].update(SCOPE, thread_id=ROOT_TS)
    spec["parts"]["context"] = {"project_id": 1, "source_message_id": mid}
    if marker == "post_source_id":
        spec["parts"]["post_source_id"] = mid
    else:
        spec["card_key"] = f"synthetic-thread|reply:{mid}"
    return spec


def _context():
    return {"thread_id": ROOT_TS, "history": None, "consumed": set()}


@pytest.fixture
def world(led):
    notify_spool.ensure_dirs(notify_spool.data_root(led))
    world = _mkworld(led)
    assert asyncio.run(world.sender.bind())
    return world


@pytest.mark.parametrize("marker", ["post_source_id", "reply_key"])
def test_identical_reply_chunks_are_distinct_and_update_only_their_prior(world, marker):
    text = "合成長文 " + "あ" * 1800 + "\n合成後半 " + "い" * 1800
    chunks = notify_parts.split_body_chunks(
        text, wire_limit=present_slack.THREAD_PART_WIRE_LIMIT,
        wire_size=present_slack.thread_part_wire_size)
    assert len(chunks) > 1 and "".join(chunks) == text
    assert all(present_slack.thread_part_wire_size(chunk) <= 4000 for chunk in chunks)
    specs = [_post_spec(mid, marker) for mid in (201, 202)]
    remote = []
    for spec in specs:
        context, ids = _context(), []
        for index, text in enumerate(chunks, 1):
            part = {"kind": "body_part", "part_id": f"body:{index:04d}"}
            out = asyncio.run(world.worker._body_part(spec, text, context, part))
            assert out["result"] == "delivered"
            ids.append(out["remote_id"])
        remote.append(ids)
    assert len(set(remote[0] + remote[1])) == 2 * len(chunks)
    assert len(world.client.thread_posts) == 2 * len(chunks)

    # B's new text already exists on A: only B's exact prior may be edited.
    part = {"kind": "body_part", "part_id": "body:0001", "prior_remote_id": remote[1][0]}
    out = asyncio.run(world.worker._body_part(
        {**specs[1], "op": "update"}, chunks[1], _context(), part))
    assert out == {"result": "delivered", "remote_id": remote[1][0]}
    assert [update["ts"] for update in _updates(world)] == [remote[1][0]]
    rows = {row["ts"]: row["text"] for row in world.client.replies[ROOT_TS]}
    assert [rows[mid] for mid in remote[0]] == chunks
    assert [rows[mid] for mid in remote[1]] == [chunks[1], *chunks[1:]]
    assert len(world.client.thread_posts) == 2 * len(chunks)


@pytest.mark.parametrize("marker", ["post_source_id", "reply_key"])
@pytest.mark.parametrize("fault", ["missing", "foreign"])
def test_unverified_reply_prior_never_reuses_another_post_or_resends(world, marker, fault):
    other = {"ts": "1790000000.000010", "text": "合成同文", "bot_id": world.client.bot_id}
    prior = "1790000000.000020"
    world.client.replies[ROOT_TS] = [other]
    if fault == "foreign":
        world.client.replies[ROOT_TS].append({"ts": prior, "text": "合成旧文", "user": "U_OTHER"})
    part = {"kind": "body_part", "part_id": "body:0001", "prior_remote_id": prior}
    out = asyncio.run(world.worker._body_part(
        {**_post_spec(202, marker), "op": "update"}, "合成同文", _context(), part))
    assert out["result"] == "unknown"
    assert not world.client.thread_posts and not _updates(world)
    assert other["text"] == "合成同文"


def test_reply_disappearing_after_verification_stays_unknown_without_repost(world):
    prior = "1790000000.000020"
    world.client.replies[ROOT_TS] = [
        {"ts": prior, "text": "合成旧文", "bot_id": world.client.bot_id}]

    async def gone(**kwargs):
        world.client.calls.append(("update", kwargs))
        world.client.replies[ROOT_TS].clear()
        return {"ok": False, "error": "message_not_found"}

    world.client.chat_update = gone
    part = {"kind": "body_part", "part_id": "body:0001", "prior_remote_id": prior}
    out = asyncio.run(world.worker._body_part(
        {**_post_spec(202, "post_source_id"), "op": "update"}, "合成更新文", _context(), part))
    assert out["result"] == "unknown"
    assert [update["ts"] for update in _updates(world)] == [prior]
    assert not world.client.thread_posts


@pytest.mark.parametrize("marker", ["post_source_id", "reply_key"])
def test_identical_files_on_different_posts_upload_separately_then_reuse_exact_file(world, marker):
    blob = b"synthetic identical attachment"
    path = world.root / "attachments" / "same.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    part = {"kind": "attachment_part", "part_id": "attach:0001", "attachment_id": 1,
            "name": path.name, "path": str(path), "bytes": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest()}
    first = asyncio.run(world.worker._attachment_part(_post_spec(201, marker), part, _context()))
    second = asyncio.run(world.worker._attachment_part(
        _post_spec(202, marker), {**part, "attachment_id": 2}, _context()))
    assert first["result"] == second["result"] == "delivered"
    assert first["remote_id"] != second["remote_id"]
    assert len(world.client.upload_calls) == 2

    out = asyncio.run(world.worker._attachment_part(
        {**_post_spec(202, marker), "op": "update"},
        {**part, "attachment_id": 2, "prior_remote_id": second["remote_id"]}, _context()))
    assert out["result"] == "delivered" and out["remote_id"] == second["remote_id"]
    assert len(world.client.upload_calls) == 2


@pytest.mark.parametrize("fault", ["missing", "foreign"])
def test_unverified_file_prior_does_not_bind_an_identical_sibling_or_upload(world, fault):
    file = {"id": "F_OTHER", "name": "same.pdf", "size": 10, "sha256": "a" * 64}
    world.client.replies[ROOT_TS] = [
        {"ts": "1790000000.000010", "bot_id": world.client.bot_id, "files": [file]}]
    if fault == "foreign":
        world.client.replies[ROOT_TS].append(
            {"ts": "1790000000.000020", "user": "U_OTHER", "files": [{**file, "id": "F_PRIOR"}]})
    part = {"kind": "attachment_part", "part_id": "attach:0001", "prior_remote_id": "F_PRIOR",
            "name": "same.pdf", "bytes": 10, "sha256": "a" * 64}
    out = asyncio.run(world.worker._attachment_part(
        {**_post_spec(202, "post_source_id"), "op": "update"}, part, _context()))
    assert out["result"] == "unknown"
    assert not world.client.upload_calls and not _updates(world)


def test_unavailable_attachment_with_missing_prior_stays_unknown_without_repost(world):
    caption = "📎 same.pdf — 取得失敗"
    world.client.replies[ROOT_TS] = [
        {"ts": "1790000000.000010", "text": caption, "bot_id": world.client.bot_id}]
    part = {"kind": "attachment_part", "part_id": "attach:0001", "unavailable": True,
            "prior_remote_id": "1790000000.000020", "name": "same.pdf", "caption": caption}
    out = asyncio.run(world.worker._attachment_part(
        {**_post_spec(202, "post_source_id"), "op": "update"}, part, _context()))
    assert out["result"] == "unknown"
    assert not world.client.thread_posts and not world.client.upload_calls and not _updates(world)


def test_short_reply_face_pins_drug_actions_to_parent_thread_and_its_own_card(led, monkeypatch):
    import notify_cards

    clock = [NOW]
    monkeypatch.setattr(notify_cards.time, "time", lambda: clock[0])
    cfg = {**SLACK, "notify": {**SLACK["notify"], "reply_cards": True}}
    _seed_thread(led, mids=(100,))
    assert _dispatch(led, _intent(led, payload={"message_ids": [100]}), cfg)["dispatched"]
    world = _mkworld(led)
    asyncio.run(_granted_card(world.worker, led, world.root))
    clock[0] += 60
    _msg(led, 201, parent=100, body="合成の薬剤記録", ts=int(clock[0]))
    _llm_extract(led, 201, {"meds": [{"name": "合成薬", "dose": "5mg", "action": "start",
                                   "subject": "patient", "status": "current", "negated": False,
                                   "unverified": False, "evidence": "合成の薬剤記録"}]})
    assert _dispatch(led, _intent(led, payload={"message_ids": [201]}), cfg, now=clock[0])["dispatched"]
    asyncio.run(_granted_card(world.worker, led, world.root))
    reply = led.db.execute("SELECT * FROM notification_cards WHERE card_key LIKE '%|reply:201'").fetchone()
    assert reply["delivery_state"] == "delivered" and reply["message_id"] != ROOT_TS
    spec = json.loads(_latest_render(led, reply["card_id"])["spec_json"])
    assert spec["parts"]["slack"]["face_posts"] and not spec["parts"]["thread_body_parts"]
    tokens = [token for token, context in token_map(spec).items() if context["action"] == "meds"]
    assert tokens
    for token in tokens:
        pin = world.reg.token(token)
        assert pin["verified_thread_id"] == ROOT_TS
        assert pin["verified_card_message_id"] == reply["message_id"]
