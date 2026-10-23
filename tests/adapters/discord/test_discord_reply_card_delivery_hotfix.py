"""Reply-card parts retain their own remote identity inside a shared Discord thread."""
import asyncio
import hashlib
import sys
from types import SimpleNamespace

import pytest

from discord_delivery_testkit import FakeHTTP, FakeThread, _claim, _discord, _HistMsg, _mkworker, _spec


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "discord", _discord)
    worker, _, bot = _mkworker(tmp_path)
    thread = FakeThread(7700)
    thread.parent_id, thread.guild = 42, SimpleNamespace(id=7)
    bot.channels[7700] = thread
    return worker, thread


def _post_spec(mid, marker, chunks):
    spec = _spec(chunks, thread_id="7700")
    spec["card_key"] = f"synthetic-card-{mid}"
    spec["parts"]["context"] = {"project_id": 1, "source_message_id": mid}
    if marker == "post_source_id":
        spec["parts"]["post_source_id"] = mid
    else:
        spec["card_key"] = f"synthetic-thread|reply:{mid}"
    return spec


def _context(thread):
    return {"thread_id": "7700", "thread": thread, "history": None, "consumed": set()}


@pytest.mark.parametrize("marker", ["post_source_id", "reply_key"])
def test_identical_reply_chunks_are_distinct_and_update_only_their_prior(world, marker):
    worker, thread = world
    chunks = ["合成長文 " + "あ" * 1800, "合成後半 " + "い" * 1800]
    specs = [_post_spec(mid, marker, chunks) for mid in (201, 202)]
    remote = []
    for spec in specs:
        context, ids = _context(thread), []
        for part in spec["parts"]["manifest"]:
            if part["kind"] == "body_part":
                out = asyncio.run(worker._part(_claim(spec), part, context))
                assert out["result"] == "delivered"
                ids.append(out["remote_id"])
        remote.append(ids)
    assert len(set(remote[0] + remote[1])) == 4
    assert len(thread.sent) == 4

    # B's new text already exists on A: only B's exact prior may be edited.
    update = _post_spec(202, marker, [chunks[1]])
    update["op"] = "update"
    part = {**update["parts"]["manifest"][2], "prior_remote_id": remote[1][0]}
    out = asyncio.run(worker._part(_claim(update), part, _context(thread)))
    assert out == {"result": "delivered", "remote_id": remote[1][0]}
    assert [message.content for message in thread.sent[:2]] == chunks
    assert [message.edits for message in thread.sent] == [0, 0, 1, 0]
    assert thread.sent[2].content == chunks[1] and len(thread.sent) == 4


@pytest.mark.parametrize("marker", ["post_source_id", "reply_key"])
@pytest.mark.parametrize("fault", ["missing", "foreign"])
def test_unverified_reply_prior_never_reuses_another_post_or_resends(world, marker, fault):
    worker, thread = world
    other = _HistMsg(6010, "合成同文")
    thread.sent = [other]
    if fault == "foreign":
        thread.sent.append(_HistMsg(6020, "合成旧文", author=SimpleNamespace(id=999)))
    before = len(thread.sent)
    spec = _post_spec(202, marker, ["合成同文"])
    spec["op"] = "update"
    part = {**spec["parts"]["manifest"][2], "prior_remote_id": "6020"}
    out = asyncio.run(worker._part(_claim(spec), part, _context(thread)))
    assert out["result"] == "unknown"
    assert len(thread.sent) == before and all(message.edits == 0 for message in thread.sent)


@pytest.mark.parametrize("marker", ["post_source_id", "reply_key"])
@pytest.mark.parametrize("edit_only", [False, True])
def test_reply_disappearing_after_verification_stays_unknown_without_repost(world, marker, edit_only):
    worker, thread = world
    target = _HistMsg(6020, "合成旧文")
    thread.sent = [target]
    attempts = []

    async def gone(**kwargs):
        attempts.append(target.id)
        thread.sent.clear()
        raise FakeHTTP(404)

    target.edit = gone
    spec = _post_spec(202, marker, ["合成更新文"])
    spec["op"] = "update"
    part = {**spec["parts"]["manifest"][2], "prior_remote_id": "6020", "edit_only": edit_only}
    out = asyncio.run(worker._part(_claim(spec), part, _context(thread)))
    assert out["result"] == "unknown"
    assert attempts == [6020] and not thread.sent


@pytest.mark.parametrize("marker", ["post_source_id", "reply_key"])
def test_identical_files_on_different_posts_upload_separately_then_reuse_exact_file(world, tmp_path, marker):
    worker, thread = world
    blob = b"synthetic identical attachment"
    path = tmp_path / "attachments" / "same.pdf"
    path.parent.mkdir()
    path.write_bytes(blob)
    part = {"kind": "attachment_part", "part_id": "attach:0001", "attachment_id": 1,
            "name": path.name, "path": str(path), "bytes": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest()}
    first = asyncio.run(worker._part(_claim(_post_spec(201, marker, [])), part, _context(thread)))
    second = asyncio.run(worker._part(
        _claim(_post_spec(202, marker, [])), {**part, "attachment_id": 2}, _context(thread)))
    assert first["result"] == second["result"] == "delivered"
    assert first["remote_id"] != second["remote_id"] and len(thread.sent) == 2
    for message in thread.sent:
        message.attachments[0].size = len(blob)

    spec = _post_spec(202, marker, [])
    spec["op"] = "update"
    out = asyncio.run(worker._part(
        _claim(spec), {**part, "attachment_id": 2, "prior_remote_id": second["remote_id"]}, _context(thread)))
    assert out == {"result": "delivered", "remote_id": second["remote_id"]}
    assert len(thread.sent) == 2 and thread.sent[0].edits == 0


@pytest.mark.parametrize("fault", ["missing", "foreign"])
def test_unverified_file_prior_does_not_bind_an_identical_sibling_or_upload(world, fault):
    worker, thread = world
    attachment = SimpleNamespace(filename="same.pdf", size=10)
    thread.sent = [_HistMsg(6010, "", attachments=[attachment])]
    if fault == "foreign":
        thread.sent.append(_HistMsg(6020, "", author=SimpleNamespace(id=999), attachments=[attachment]))
    before = len(thread.sent)
    spec = _post_spec(202, "post_source_id", [])
    spec["op"] = "update"
    part = {"kind": "attachment_part", "part_id": "attach:0001", "prior_remote_id": "6020",
            "name": "same.pdf", "bytes": 10, "sha256": "a" * 64}
    out = asyncio.run(worker._part(_claim(spec), part, _context(thread)))
    assert out["result"] == "unknown"
    assert len(thread.sent) == before and all(message.edits == 0 for message in thread.sent)


def test_unavailable_attachment_with_missing_prior_stays_unknown_without_repost(world):
    worker, thread = world
    caption = "📎 same.pdf — 取得失敗"
    thread.sent = [_HistMsg(6010, caption)]
    spec = _post_spec(202, "post_source_id", [])
    spec["op"] = "update"
    part = {"kind": "attachment_part", "part_id": "attach:0001", "unavailable": True,
            "prior_remote_id": "6020", "name": "same.pdf", "caption": caption}
    out = asyncio.run(worker._part(_claim(spec), part, _context(thread)))
    assert out["result"] == "unknown"
    assert len(thread.sent) == 1 and thread.sent[0].id == 6010 and thread.sent[0].edits == 0


@pytest.mark.parametrize("marker", ["post_source_id", "reply_key"])
@pytest.mark.parametrize("unavailable", [False, True])
def test_edit_only_attachment_disappearing_after_verification_is_unknown(world, marker, unavailable):
    worker, thread = world
    attachment = SimpleNamespace(filename="synthetic.pdf", size=10)
    target = _HistMsg(6020, "合成旧説明", attachments=[] if unavailable else [attachment])
    thread.sent = [target]
    attempts = []
    async def gone(**kwargs):
        attempts.append(target.id)
        thread.sent.clear()
        raise FakeHTTP(404)
    target.edit = gone
    spec = _post_spec(202, marker, [])
    spec["op"] = "update"
    part = {"kind": "attachment_part", "part_id": "attach:0001", "edit_only": True,
            "unavailable": unavailable, "prior_remote_id": "6020", "name": "synthetic.pdf",
            "bytes": 10, "sha256": "a" * 64, "caption": "合成更新説明"}
    out = asyncio.run(worker._part(_claim(spec), part, _context(thread)))
    assert out["result"] == "unknown"
    assert attempts == [6020] and not thread.sent
