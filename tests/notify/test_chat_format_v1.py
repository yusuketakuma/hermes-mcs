"""chat_format_version=1: Discord V2 originals on the face and LINE WORKS
per-post attachment order, synthetic fixtures only."""
import hashlib
import json
import sys

import pytest

import notify_parts
import present_discord
import present_lineworks
from adapters.common import spec as spec_module
from adapters.lineworks import cards as lw_cards
from discord_testkit import _fake_discord
from notify_lineworks_testkit import LINEWORKS
from notify_testkit import (NOW, _dispatch, _intent, _latest_render,
                            _seed_thread, led as led, pinned_clock)

__all__ = ["led", "pinned_clock"]
pytestmark = pytest.mark.usefixtures("pinned_clock")


def _spec(led):
    return json.loads(_latest_render(led)["spec_json"])


def _manifest(led):
    render = _latest_render(led)
    return json.loads(render["spec_json"])["parts"]["manifest"]


def _v1_discord(led, monkeypatch, mids=(100,)):
    monkeypatch.setattr(present_discord, "CHAT_BODY_ON_FACE", True)
    _seed_thread(led, mids=mids)
    for mid in mids:
        led.db.execute("UPDATE messages SET body_text=? WHERE message_id=?",
                       (f"合成の原文{mid}", mid))
    _dispatch(led, _intent(led, payload={"message_ids": list(mids)}))
    return _spec(led)


def test_discord_v1_face_carries_original_and_renders_v2(led, monkeypatch):
    spec = _v1_discord(led, monkeypatch)
    parts = spec["parts"]
    assert parts["chat_format_version"] == 1
    posts = parts["discord"]["face_posts"]
    assert [p["original"] for p in posts] == ["合成の原文100"]
    assert not [p for p in parts["manifest"] if p["kind"] == "body_part"]
    assert spec_module.validate(spec) is spec
    monkeypatch.setitem(sys.modules, "discord", _fake_discord())
    from adapters.discord import cards as discord_cards
    texts = []
    for item in discord_cards.build_view(spec).items[0].children:
        texts.extend(t.content for t in (item, *getattr(item, "children", ()))
                     if hasattr(t, "content"))
    face = "\n".join(texts)
    assert "原文" in face and "合成の原文100" in face
    payload = discord_cards.message_payload(spec)
    assert "view" in payload and "embed" not in payload and "content" not in payload


def test_discord_v1_oversized_original_stays_in_thread(led, monkeypatch):
    monkeypatch.setattr(present_discord, "FACE_ORIGINAL_MAX", 5)
    spec = _v1_discord(led, monkeypatch)
    parts = spec["parts"]
    assert parts.get("chat_format_version") == 1
    assert not (parts.get("discord") or {}).get("face_posts")
    names = {p.get("name") for p in parts["manifest"] if p["kind"] == "body_part"}
    assert names and all(n and n.startswith("m:100") for n in names)
    assert "合成の原文100" in "".join(parts["thread_body_parts"])
    assert spec_module.validate(spec) is spec


def test_discord_legacy_spec_has_no_version_and_keeps_embed(led, monkeypatch):
    # the autouse fixture already pins CHAT_BODY_ON_FACE=False
    _seed_thread(led, mids=(100,))
    led.db.execute("UPDATE messages SET body_text='合成の原文' WHERE message_id=100")
    _dispatch(led, _intent(led, payload={"message_ids": [100]}))
    spec = _spec(led)
    assert "chat_format_version" not in spec["parts"]
    monkeypatch.setitem(sys.modules, "discord", _fake_discord())
    from adapters.discord import cards as discord_cards
    payload = discord_cards.message_payload(spec)
    assert "embed" in payload


def _lw_seed(led, monkeypatch, mids=(100, 101)):
    monkeypatch.setattr(present_lineworks, "CHAT_BODY_ON_FACE", True)
    _seed_thread(led, mids=mids)
    for mid in mids:
        led.db.execute("UPDATE messages SET body_text=? WHERE message_id=?",
                       (f"合成の原文{mid}", mid))


def _lw_v1(led, mids=(100, 101)):
    _dispatch(led, _intent(led, payload={"message_ids": list(mids)}), LINEWORKS)
    return _spec(led)


def _attach_mid(led, mid, name):
    blob = name.encode()
    led.db.execute(
        "INSERT INTO attachments(message_id,file_id,name,local_path,"
        "bytes,sha256,state,downloaded_at,created_at) "
        "VALUES(?,?,?,?,?,?,'downloaded',?,?)",
        (mid, f"file-{mid}-{name}", name, f"/tmp/{name}", len(blob),
         hashlib.sha256(blob).hexdigest(), NOW, NOW))
    led.db.commit()
    return led.db.execute("SELECT attachment_id FROM attachments "
                          "WHERE message_id=? AND name=?",
                          (mid, name)).fetchone()["attachment_id"]


def test_lw_v1_face_posts_lead_with_their_attachments(led, monkeypatch):
    _lw_seed(led, monkeypatch)
    a100 = _attach_mid(led, 100, "a100.bin")
    a101 = _attach_mid(led, 101, "a101.bin")
    spec = _lw_v1(led)
    parts = spec["parts"]
    assert parts["chat_format_version"] == 1
    posts = parts["lineworks"]["face_posts"]
    assert {p["original"] for p in posts} == {"合成の原文100", "合成の原文101"}
    manifest = parts["manifest"]
    att = {p.get("attachment_id"): i for i, p in enumerate(manifest)
           if p["kind"] == "attachment_part"}
    assert att[a100] < att[a101]
    # both posts sit whole on the face: their files lead, in post order,
    # right after the thread part and before any body content
    thread_at = next(i for i, p in enumerate(manifest) if p["kind"] == "thread")
    assert att[a100] == thread_at + 1 and att[a101] == thread_at + 2
    assert not [p for p in manifest if p["kind"] == "body_part"]
    assert lw_cards.validate(spec) is spec


def test_lw_v1_body_overflow_keeps_files_after_their_post(led, monkeypatch):
    monkeypatch.setattr(present_lineworks, "FACE_ORIGINAL_MAX", 5)
    _lw_seed(led, monkeypatch)
    a100 = _attach_mid(led, 100, "a100.bin")
    a101 = _attach_mid(led, 101, "b101.bin")
    spec = _lw_v1(led)
    parts = spec["parts"]
    assert parts["chat_format_version"] == 1
    assert not (parts.get("lineworks") or {}).get("face_posts")
    manifest = parts["manifest"]
    att = {p.get("attachment_id"): i for i, p in enumerate(manifest)
           if p["kind"] == "attachment_part"}
    # mid 100's file follows mid 100's last body chunk; mid 101's follows its own
    def group_of(idx):
        return next(p["name"] for p in manifest[:idx + 1][::-1]
                    if p["kind"] == "body_part")
    assert group_of(att[a100]).startswith("m:100")
    assert group_of(att[a101]).startswith("m:101")
    assert lw_cards.validate(spec) is spec


def test_lw_v1_unmapped_attachment_keeps_the_tail(led):
    """A file whose source post maps to no body group stays at the tail —
    the planner never drops or reorders it into a post's block."""
    manifest = [
        {"part_id": "thread", "kind": "thread", "index": 0, "name": "t"},
        {"part_id": "b1", "kind": "body_part", "index": 1, "name": "m:100#1"},
        {"part_id": "b2", "kind": "body_part", "index": 2, "name": "m:101#1"},
    ]
    entries = [
        {"part_id": "a1", "kind": "attachment_part", "index": 3,
         "attachment_id": 1, "name": "one.bin"},
        {"part_id": "a2", "kind": "attachment_part", "index": 4,
         "attachment_id": 2, "name": "two.bin"},
        {"part_id": "a3", "kind": "attachment_part", "index": 5,
         "attachment_id": 3, "name": "orphan.bin"},
    ]
    _seed_thread(led, mids=(100, 101))
    aid1 = _attach_mid(led, 100, "one.bin")
    aid2 = _attach_mid(led, 101, "two.bin")
    entries[0]["attachment_id"] = aid1
    entries[1]["attachment_id"] = aid2
    # attachment_id 3 has no attachments row -> mid_of misses -> tail
    groups = [("m:100", [100]), ("m:101", [101])]
    out = notify_parts._order_chat_attachment_parts(
        led.db, [dict(p) for p in manifest], [dict(e) for e in entries],
        groups, frozenset(), set())
    order = [(p["kind"], p.get("name")) for p in out]
    assert order == [
        ("thread", "t"), ("body_part", "m:100#1"),
        ("attachment_part", "one.bin"),
        ("body_part", "m:101#1"), ("attachment_part", "two.bin"),
        ("attachment_part", "orphan.bin")]
    assert [p["index"] for p in out] == list(range(len(out)))
