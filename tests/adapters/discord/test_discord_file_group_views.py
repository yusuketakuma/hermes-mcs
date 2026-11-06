"""Native Components V2 keeps each sealed file below its own post original."""
from __future__ import annotations

import copy

import pytest

discord = pytest.importorskip("discord")

from adapters.common.spec import discord_file_name  # noqa: E402
from adapters.discord import cards  # noqa: E402


def _member(number, name, at=1):
    return {"part_id": f"attach:{number:04d}", "kind": "attachment_part",
            "name": name, "sha256": f"{number:064x}", "bytes": 10,
            "face_at": at}


def _spec(members, *, surface="card", reply=False):
    return {"kind": "thread", "card_key": "thread:1" + ("|reply:2" if reply else ""),
            "parts": {"chat_format_version": 1, "section_divider_version": 1,
                      "containers": [{"type": "heading", "text": "合成記録"},
                                     {"type": "text", "rule": True, "text": "親投稿"},
                                     {"type": "text", "text": "親の要約"},
                                     {"type": "text", "rule": True, "text": "返信投稿"},
                                     {"type": "text", "text": "返信の要約"}],
                      "discord": {"face_posts": [
                          {"at": 1, "original": "親の原文", "stamps": []},
                          {"at": 3, "original": "返信の原文", "stamps": []}]},
                      "action_rows": [], "footer": [],
                      "manifest": [*members, {"kind": "file_group", "part_id": f"file_group:{surface}",
                                               "surface": surface,
                                               "members": [m["part_id"] for m in members]}]}}


def _children(view):
    return [child for container in view.to_components() for child in container["components"]]


def _urls(component):
    if component["type"] == 12:
        return [item["media"]["url"] for item in component["items"]]
    if component["type"] == 13:
        return [component["file"]["url"]]
    return []


@pytest.mark.parametrize("reply", [False, True], ids=["parent", "thread-reply"])
def test_files_follow_their_own_originals_in_single_native_payload(reply):
    members = [_member(1, "same.jpg"), _member(2, "same.jpg"),
               _member(3, "report.pdf", 3), _member(4, "scan.png", 3)]
    spec = _spec(members, reply=reply)
    view = cards.message_payload(spec)["view"]
    inner = _children(view)
    p_original = next(i for i, c in enumerate(inner) if "親の原文" in c.get("content", ""))
    r_original = next(i for i, c in enumerate(inner) if "返信の原文" in c.get("content", ""))
    assert inner[p_original + 1]["type"] == 12
    assert _urls(inner[p_original + 1]) == ["attachment://" + discord_file_name(m) for m in members[:2]]
    assert [c["type"] for c in inner[r_original + 1:r_original + 3]] == [13, 12]
    assert [_urls(c)[0] for c in inner[r_original + 1:r_original + 3]] == [
        "attachment://" + discord_file_name(m) for m in members[2:]]
    assert len({url for c in inner for url in _urls(c)}) == len(members)
    assert all(item["spoiler"] is True for c in inner if c["type"] == 12 for item in c["items"])
    from discord.http import handle_message_parameters
    payload = handle_message_parameters(view=view).payload
    assert payload["flags"] & 32768
    assert payload["components"] == view.to_components()


def test_many_pdf_files_split_containers_without_dropping_original_or_files():
    members = [_member(i, f"report-{i}.pdf") for i in range(1, 11)]
    view = cards.build_view(_spec(members))
    assert len(view.to_components()) >= 2
    assert all(len(c["components"]) <= 10 for c in view.to_components())
    children = _children(view)
    assert sum(c["type"] == 13 for c in children) == 10
    assert "親の原文" in "".join(c.get("content", "") for c in children)
    assert "返信の原文" in "".join(c.get("content", "") for c in children)
    assert view.total_children_count <= 40


def test_budget_fallback_keeps_file_markers_and_originals():
    members = [_member(1, "report.pdf")]
    spec = _spec(members)
    spec["parts"]["discord"]["face_posts"][0]["stamps"] = ["長いスタンプ" * 700]
    children = _children(cards.build_view(spec))
    text = "".join(c.get("content", "") for c in children)
    assert "親の原文" in text and "返信の原文" in text
    assert "長いスタンプ" not in text
    assert sum(c["type"] == 13 for c in children) == 1


@pytest.mark.parametrize("overflow", ["text", "components"])
def test_budget_failure_never_returns_partial_originals_or_files(overflow):
    spec = _spec([_member(1, "report.pdf")])
    if overflow == "text":
        spec["parts"]["discord"]["face_posts"][0]["original"] = "あ" * 4001
    else:
        spec["parts"]["manifest"] = _spec([_member(i, f"r{i}.pdf") for i in range(1, 41)])["parts"]["manifest"]
    with pytest.raises(ValueError, match="discord_file_group_budget"):
        cards.build_view(spec)


def test_body_surface_keeps_escaped_original_then_files_then_medication_controls():
    members = [_member(1, "photo.jpg"), _member(2, "report.pdf")]
    spec = _spec(members, surface="body:0002")
    spec["parts"]["action_rows"] = [[{"ui": "button", "id": "meds", "label": "薬",
                                      "style": "secondary", "token": "a" * 32}]]
    body = cards.escape_md("返信本文 *合成記録*\n")
    view = cards.thread_body_view(spec, {"part_id": "body:0002"}, body,
                                  controls=cards.thread_drug_view(spec))
    children = _children(view)
    assert [c["type"] for c in children] == [10, 12, 13, 1]
    assert children[0]["content"] == body
    assert children[-1]["components"][0]["custom_id"] == "mcs:a:" + "a" * 32
    assert [_urls(c)[0] for c in children[1:3]] == ["attachment://" + discord_file_name(m) for m in members]
    untouched = copy.deepcopy(spec)
    cards.thread_body_view(spec, {"part_id": "body:0002"}, body)
    assert spec == untouched


def test_group_upload_uses_verified_bytes_and_sdk_file_owner(tmp_path):
    import asyncio
    import hashlib
    from adapters.discord.delivery import DeliveryWorker
    directory = tmp_path / 'attachments'
    directory.mkdir()
    path = directory / 'synthetic.png'
    blob = b'synthetic-image'
    path.write_bytes(blob)
    member = _member(1, 'synthetic.png')
    member.update(path=str(path), bytes=len(blob), sha256=hashlib.sha256(blob).hexdigest())
    instance = object.__new__(DeliveryWorker)
    instance._root = str(tmp_path)
    native, = asyncio.run(instance._member_files([member]))
    try:
        assert native.filename == discord_file_name(member)
        assert native.fp.read() == blob
        assert native.spoiler is True
    finally:
        native.close()
    path.write_bytes(b'x' * len(blob))
    assert asyncio.run(instance._member_files([member])) is None


@pytest.mark.parametrize(('name', 'spoiler'), [
    ('synthetic.png', True), ('report.pdf', False),
    ('SPOILER_report.pdf', True), ('SPOILER_SPOILER_report.pdf', True),
])
def test_native_file_name_matches_view_uri_and_sealed_spoiler(name, spoiler):
    member = _member(1, name)
    native = cards.attachment_file(b'synthetic', member)
    try:
        view = cards.message_payload(_spec([member]))['view']
        urls = [url for component in _children(view) for url in _urls(component)]
        assert urls == ['attachment://' + native.filename]
        assert native.filename == discord_file_name(member)
        assert native.spoiler is spoiler
        assert native.filename.count('SPOILER_') == int(spoiler)
    finally:
        native.close()
