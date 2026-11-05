"""Synthetic mcs discord delivery attachments contracts with isolated shared fixtures."""
from __future__ import annotations
import asyncio
import hashlib
import sys
import pytest
from adapters.common.registry import Registry
from discord_delivery_testkit import FOREIGN_USER, FakeHTTP, FakeThread, _chunks, _claim, _HistMsg, _mkworker, _receipts, _sent_parts, _spec, _state
from discord_delivery_testkit import synthetic_discord_sdk as synthetic_discord_sdk
from mcs_discord_delivery_testkit import _attach_update as _attach_update, _part_ctx as _part_ctx, _sealed as _sealed, _token_world as _token_world


def test_second_attachment_failure_leaves_incomplete(tmp_path):
    """The second file's upload vanishing mid-wire is unknown — the
    first file's delivered receipt stands, no part resends."""
    b1, b2 = b"first-file", b"second-file"
    (tmp_path / "attachments").mkdir(exist_ok=True)
    f1, f2 = tmp_path / "attachments" / "a1.bin", tmp_path / "attachments" / "a2.bin"
    f1.write_bytes(b1)
    f2.write_bytes(b2)
    atts = [
        {"part_id": "attach:0007", "kind": "attachment_part",
         "attachment_id": 7, "name": "a1.bin", "path": str(f1),
         "sha256": hashlib.sha256(b1).hexdigest(), "bytes": len(b1)},
        {"part_id": "attach:0008", "kind": "attachment_part",
         "attachment_id": 8, "name": "a2.bin", "path": str(f2),
         "sha256": hashlib.sha256(b2).hexdigest(), "bytes": len(b2)}]
    w, reg, bot = _mkworker(tmp_path)
    th = FakeThread(7700)
    bot.channels[42].threads.append(th)
    th.fail_at = 2                  # sends: body(0) attach1(1) attach2(!)
    spec = _spec(_chunks(1), op="update", thread_id="7700")
    for a in atts:
        a["index"] = len(spec["parts"]["manifest"])
        spec["parts"]["manifest"].append(a)
    claim = _claim(spec)
    asyncio.run(w._deliver_parts(claim, "9001"))
    parts = _sent_parts(_state(tmp_path))
    assert parts["attach:0007"]["result"] == "delivered"
    assert parts["attach:0008"]["result"] == "unknown"
    # a replayed drive resends nothing — every part is journaled final
    n_sent = len(th.sent)
    asyncio.run(w._deliver_parts(claim, "9001"))
    assert len(th.sent) == n_sent


def test_attachment_part_uploads_verified_file(tmp_path):
    blob = b"synthetic-attachment"
    (tmp_path / "attachments").mkdir(exist_ok=True)
    f = tmp_path / "attachments" / "att.bin"
    f.write_bytes(blob)
    sha = hashlib.sha256(blob).hexdigest()
    att = {"part_id": "attach:0007", "kind": "attachment_part",
           "index": 0, "attachment_id": 7, "name": "att.bin",
           "path": str(f), "sha256": sha, "bytes": len(blob)}
    w, reg, bot = _mkworker(tmp_path)
    spec = _spec(_chunks(1))
    att["index"] = len(spec["parts"]["manifest"])
    spec["parts"]["manifest"].append(att)
    claim = _claim(spec)
    asyncio.run(w._deliver_parts(claim, "9001"))
    parts = _sent_parts(_state(tmp_path))
    assert parts["attach:0007"]["result"] == "delivered"
    assert parts["attach:0007"]["remote_id"]


@pytest.mark.parametrize("setup", ["deleted", "foreign", "no_file"])
def test_unusable_prior_file_post_uploads_afresh(tmp_path, setup):
    w, th, spec = _attach_update(tmp_path, "6100")
    if setup == "foreign":
        th.sent.append(_HistMsg(6100, None, author=FOREIGN_USER,
                                attachments=["att.bin"]))
    elif setup == "no_file":
        th.sent.append(_HistMsg(6100, "本文のみ"))
    asyncio.run(w._deliver_parts(_claim(spec), "9001"))
    uploads = [m for m in th.sent if m.id != 6100]
    assert len(uploads) == 1 and uploads[0].attachments
    row = _sent_parts(_state(tmp_path))["attach:0007"]
    assert row["result"] == "delivered"
    assert row["remote_id"] == str(uploads[0].id)


def test_attachment_part_may_name_its_prior_post(tmp_path):
    from adapters.common import spec as spec_mod
    _, _, update = _attach_update(tmp_path, "6100")
    spec = _spec([])
    att = dict(update["parts"]["manifest"][-1],
               index=len(spec["parts"]["manifest"]))
    spec["parts"]["manifest"].append(att)
    spec_mod.validate(_sealed(spec))


def test_attachment_part_hash_mismatch_not_sent(tmp_path):
    (tmp_path / "attachments").mkdir(exist_ok=True)
    f = tmp_path / "attachments" / "att.bin"
    f.write_bytes(b"mutated bytes")
    att = {"part_id": "attach:0007", "kind": "attachment_part",
           "index": 0, "attachment_id": 7, "name": "att.bin",
           "path": str(f), "sha256": "ab" * 32, "bytes": 13}
    w, reg, bot = _mkworker(tmp_path)
    spec = _spec(_chunks(1))
    att["index"] = len(spec["parts"]["manifest"])
    spec["parts"]["manifest"].append(att)
    claim = _claim(spec)
    asyncio.run(w._deliver_parts(claim, "9001"))
    parts = _sent_parts(_state(tmp_path))
    assert parts["attach:0007"]["result"] == "not_sent"
    assert parts["attach:0007"]["error_code"] == "attachment_mismatch"
    envs = [e for e in _receipts(tmp_path / "cmd_int")
            if e["op"] == "part_receipt" and e["part_id"] == "attach:0007"]
    assert envs and envs[0]["result"] == "not_sent"


def test_attachment_upload_uses_the_bytes_that_were_verified(tmp_path, monkeypatch):
    from pathlib import Path
    from adapters.discord import cards
    blob = b"sealed-synthetic"
    (tmp_path / "attachments").mkdir(exist_ok=True)
    path = tmp_path / "attachments" / "attachment.bin"
    path.write_bytes(blob)
    w, reg, bot = _mkworker(tmp_path)
    spec = _spec([])
    spec["parts"]["manifest"].append({
        "part_id": "attach:0001", "kind": "attachment_part", "index": 2,
        "attachment_id": 1, "name": path.name, "path": str(path),
        "sha256": hashlib.sha256(blob).hexdigest(), "bytes": len(blob)})
    uploaded = []

    async def send(target, source, name):
        path.write_bytes(b"changed-on-disk!")
        uploaded.append(source.read() if hasattr(source, "read")
                        else Path(source).read_bytes())
        return await target.send("synthetic-file")

    monkeypatch.setattr(cards, "send_attachment", send)
    asyncio.run(w._deliver_parts(_claim(spec), "9001"))
    assert uploaded == [blob]


def test_unavailable_attachment_entry_never_attempted(tmp_path):
    att = {"part_id": "attach:0009", "kind": "attachment_part",
           "index": 0, "attachment_id": 9, "name": "gone.bin",
           "unavailable": True}
    w, reg, bot = _mkworker(tmp_path)
    spec = _spec(_chunks(1))
    att["index"] = len(spec["parts"]["manifest"])
    spec["parts"]["manifest"].append(att)
    claim = _claim(spec)
    asyncio.run(w._deliver_parts(claim, "9001"))
    parts = _sent_parts(_state(tmp_path))
    assert "attach:0009" not in parts         # runner pre-settled it


@pytest.mark.parametrize("result,pruned", [
    ("delivered", True), ("not_sent", False), ("unknown", False)])
def test_delivered_update_prunes_replaced_card_tokens(
        tmp_path, monkeypatch, result, pruned):
    """Only a proven in-place edit retires the old view's tokens; other
    cards, result-issued task tokens and the new ones always stay."""
    w, reg, spec, _, _ = _token_world(tmp_path, monkeypatch)
    claim = _claim(spec)
    claim["phase"] = "result"
    claim["outcome"] = {"result": result, "message_id": "9001"} \
        if result == "delivered" else {"result": result, "error_code": "x"}
    asyncio.run(w._step_claim(claim, allow_parts=False))
    for t in ("new-ack", "new-next", "other-ack", "task-tok"):
        assert reg.token(t) is not None
    assert (reg.token("old-ack") is None) is pruned
    assert (reg.token("old-next") is None) is pruned
    # durable, not just in memory
    assert (Registry(str(_state(tmp_path))).token("old-ack") is None) \
        is pruned


def test_newer_render_tokens_survive_an_older_update_prune(
        tmp_path, monkeypatch):
    """A later render's tokens, put after this one's, are never pruned
    by this render's delivered receipt."""
    from adapters.common import spec as spec_mod
    w, reg, spec, clock, update = _token_world(tmp_path, monkeypatch)
    clock[0] = 3000.0
    reg.put_tokens(spec_mod.token_map(
        update(spec["card_key"], ack="newer-ack")))
    reg.prune_card_tokens(spec_mod.token_map(spec))
    assert reg.token("newer-ack") is not None
    assert reg.token("old-ack") is None and reg.token("new-ack")


def test_attachment_caption_rides_with_the_file(tmp_path, monkeypatch):
    from adapters.discord import cards
    blob = b"sealed-synthetic"
    (tmp_path / "attachments").mkdir(exist_ok=True)
    path = tmp_path / "attachments" / "a.bin"
    path.write_bytes(blob)
    w, _reg, _bot = _mkworker(tmp_path)
    seen = []

    async def send(target, source, name, content=None):
        seen.append(content)
        return await target.send(content)

    monkeypatch.setattr(cards, "send_attachment", send)
    part = {"part_id": "attach:0001", "kind": "attachment_part", "index": 2,
            "attachment_id": 1, "name": "a.bin", "path": str(path),
            "sha256": hashlib.sha256(blob).hexdigest(), "bytes": len(blob),
            "caption": "📎 a.bin — 合成様 10-01 09:40 合成さん"}
    out = asyncio.run(w._perform_part(_claim(_spec([])), part,
                                      _part_ctx(FakeThread(7001))))
    assert out["result"] == "delivered"
    assert seen == ["📎 a.bin — 合成様 10-01 09:40 合成さん"]


def test_unavailable_attachment_posts_its_caption_once(tmp_path):
    w, _reg, _bot = _mkworker(tmp_path)
    thread = FakeThread(7001)
    part = {"part_id": "attach:0002", "kind": "attachment_part", "index": 2,
            "attachment_id": 2, "name": "b.jpg", "unavailable": True,
            "caption": "📎 b.jpg — 取得失敗"}
    out = asyncio.run(w._perform_part(_claim(_spec([])), part,
                                      _part_ctx(thread)))
    assert out["result"] == "delivered"
    assert [m.content for m in thread.sent] == ["📎 b.jpg — 取得失敗"]
    again = asyncio.run(w._perform_part(
        _claim(_spec([])), {**part, "prior_remote_id": "6001"},
        _part_ctx(thread)))
    assert again == {"result": "delivered", "remote_id": "6001"}
    assert len(thread.sent) == 1


def test_thread_drug_actions_install_and_refresh_only_first_body(tmp_path, monkeypatch):
    from discord_testkit import _fake_discord
    monkeypatch.setitem(sys.modules, "discord", _fake_discord())
    w, _, bot = _mkworker(tmp_path)
    spec = _spec(["synthetic first", "synthetic second"])
    spec["parts"]["thread_drug_actions"] = True
    spec["parts"]["action_rows"] = [[
        {"id": action, "label": action, "token": "old-" + action}
        for action in ("meds", "drugsearch", "ack")]]
    asyncio.run(w._deliver_parts(_claim(spec), "9001"))
    thread = bot.channels[42].threads[0]
    first, second = thread.sent
    assert [b.custom_id for b in first.view.items] == [
        "mcs:a:old-meds", "mcs:a:old-drugsearch"]
    assert first.view.stopped and second.view is None
    assert _sent_parts(_state(tmp_path))["body:0001"]["remote_id"] == str(first.id)
    # Journal replay never adds messages or edits already settled parts.
    asyncio.run(w._deliver_parts(_claim(spec), "9001"))
    assert len(thread.sent) == 2 and first.edits == 0
    spec["op"] = "update"
    spec["delivery"]["thread_id"] = str(thread.id)
    for row in spec["parts"]["action_rows"]:
        for button in row:
            button["token"] = "fresh-" + button["id"]
    part = spec["parts"]["manifest"][2]
    ctx = {"thread": thread, "consumed": set()}
    result = asyncio.run(w._perform_part(_claim(spec), part, ctx))
    assert result == {"result": "delivered", "remote_id": str(first.id)}
    assert len(thread.sent) == 2 and first.content == "synthetic first"
    assert [b.custom_id for b in first.view.items] == [
        "mcs:a:fresh-meds", "mcs:a:fresh-drugsearch"]
    assert first.edits == 1 and second.edits == 0
    spec["parts"]["thread_body_parts"][0] = "synthetic changed"
    part["prior_remote_id"] = str(first.id)
    result = asyncio.run(w._perform_part(_claim(spec), part,
                                        {"thread": thread, "consumed": set()}))
    assert result["remote_id"] == str(first.id)
    assert len(thread.sent) == 2 and first.content == "synthetic changed"
    assert first.edits == 2
    # The flagged current empty action set must clear stale medication controls.
    spec["parts"]["action_rows"] = []
    result = asyncio.run(w._perform_part(_claim(spec), part,
                                        {"thread": thread, "consumed": set()}))
    assert result["remote_id"] == str(first.id)
    assert first.view is None and first.edits == 3 and len(thread.sent) == 2


def test_same_text_drug_view_edit_unknown_never_posts_duplicate(tmp_path, monkeypatch):
    from discord_testkit import _fake_discord
    monkeypatch.setitem(sys.modules, "discord", _fake_discord())
    w, _, bot = _mkworker(tmp_path)
    thread = FakeThread(7700)
    bot.channels[42].threads.append(thread)
    first = _HistMsg(6001, "synthetic first")
    first.edit_fail = FakeHTTP(500)
    thread.sent.append(first)
    spec = _spec([first.content], op="update", thread_id="7700")
    spec["parts"]["thread_drug_actions"] = True
    spec["parts"]["action_rows"] = [[
        {"id": "meds", "label": "薬剤", "token": "synthetic-token"}]]
    asyncio.run(w._deliver_parts(_claim(spec), "9001"))
    assert _sent_parts(_state(tmp_path))["body:0001"]["result"] == "unknown"
    # A recorded ambiguous edit is held on replay, even after HTTP recovers.
    first.edit_fail = None
    asyncio.run(w._deliver_parts(_claim(spec), "9001"))
    assert len(thread.sent) == 1 and first.edits == 0


@pytest.mark.parametrize("components_v2", [False, True])
def test_parent_card_drug_actions_only_appear_in_companion_thread(tmp_path, monkeypatch,
                                                                components_v2):
    from discord_testkit import _fake_discord
    from adapters.discord import cards
    monkeypatch.setitem(sys.modules, "discord", _fake_discord())
    spec = _spec(["synthetic first"])
    spec["parts"]["action_rows"] = [[
        {"id": action, "label": action, "token": "synthetic-" + action}
        for action in ("meds", "drugsearch", "ack")]]

    def tokens(item):
        out = [item.custom_id] if getattr(item, "custom_id", "").startswith("mcs:a:") else []
        out.extend(option.value for option in getattr(item, "options", []))
        for child in getattr(item, "items", []) + getattr(item, "children", []):
            out.extend(tokens(child))
        return out

    # Legacy/card_thread-off card behavior retains its drug action entries.
    legacy = cards.message_payload(spec, components_v2=components_v2)["view"]
    assert {"synthetic-meds", "synthetic-drugsearch"} <= set(tokens(legacy))
    spec["parts"]["thread_drug_actions"] = True
    parent = cards.message_payload(spec, components_v2=components_v2)["view"]
    assert tokens(parent) == ["synthetic-ack"]           # 確認する sits in the 操作 menu
    assert tokens(cards.thread_drug_view(spec)) == [
        "mcs:a:synthetic-meds", "mcs:a:synthetic-drugsearch"]


@pytest.mark.parametrize("status", [400, 403, 404])
def test_same_text_drug_view_rejected_edit_posts_verified_replacement(tmp_path, monkeypatch,
                                                                    status):
    from discord_testkit import _fake_discord
    monkeypatch.setitem(sys.modules, "discord", _fake_discord())
    w, _, bot = _mkworker(tmp_path)
    thread = FakeThread(7700)
    bot.channels[42].threads.append(thread)
    old = _HistMsg(5999, "synthetic first")
    old.edit_fail = FakeHTTP(status)
    thread.sent.append(old)
    spec = _spec([old.content], op="update", thread_id="7700",
                 prior={"body:0001": str(old.id)})
    spec["parts"]["thread_drug_actions"] = True
    spec["parts"]["action_rows"] = [[
        {"id": "meds", "label": "薬剤", "token": "a" * 32}]]
    asyncio.run(w._deliver_parts(_claim(spec), "9001"))
    record = _sent_parts(_state(tmp_path))["body:0001"]
    assert record["result"] == "delivered"
    assert record["remote_id"] == str(thread.sent[-1].id)
    assert len(thread.sent) == 2 and old.edits == 0
    assert thread.sent[-1].view.items[0].custom_id == "mcs:a:" + "a" * 32
    asyncio.run(w._deliver_parts(_claim(spec), "9001"))
    assert len(thread.sent) == 2
