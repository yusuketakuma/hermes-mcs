"""Discord per-surface ``file_group`` manifest contract: members bind
to exactly one card/body_part surface, sit between it and the group,
and the group hash seals the ordered member set."""
import copy
import hashlib

import pytest
from adapters.common import spec
from adapters.common.envelopes import payload_hash
from discord_delivery_testkit import _spec


def _sha(t):
    return hashlib.sha256(t.encode()).hexdigest()


def _att(part_id, idx, aid):
    return {"part_id": part_id, "kind": "attachment_part", "index": idx,
            "attachment_id": aid, "name": f"f{aid}.png",
            "path": f"attachments/{aid}.png", "bytes": 10,
            "sha256": _sha(f"blob{aid}")}


def _group(surface, members, idx, value):
    resolved = [m for m in value["_members"] if m["part_id"] in members]
    return {"part_id": f"file_group:{surface}", "kind": "file_group",
            "index": idx, "surface": surface, "members": members,
            "sha256": spec.discord_file_group_sha256(value, surface, resolved)}


def planned():
    """card+thread, two card-surface files, one body-surface file."""
    value = _spec(["chunk one"], op="create")
    value["parts"]["chat_format_version"] = 1
    value["parts"]["containers"].insert(0, {"type": "text", "text": "合成の投稿", "rule": True})
    value["parts"]["discord"] = {"face_posts": [{"at": 0, "stamps": [], "original": "合成原文"}]}
    value["_members"] = [
        _att("attach:0001", 2, 1), _att("attach:0002", 3, 2),
        _att("attach:0003", 6, 3)]
    value["_members"][0]["face_at"] = 0
    value["_members"][1]["face_at"] = 0
    card = value["parts"]["manifest"][0]
    card["sha256"] = payload_hash(
        {k: value["parts"][k] for k in ("containers", "footer", "action_rows")})
    thread = value["parts"]["manifest"][1]
    body = value["parts"]["manifest"][2]
    body.update(index=5, sha256=_sha("chunk one"), bytes=len("chunk one"))
    # card surface members lead after the thread part; the body surface
    # member follows its own body part — each group right after its last
    # member.
    value["parts"]["manifest"] = [
        card, thread,
        value["_members"][0], value["_members"][1],
        _group("card", ["attach:0001", "attach:0002"], 4, value),
        body,
        value["_members"][2], _group("body:0001", ["attach:0003"], 7, value)]
    for i, p in enumerate(value["parts"]["manifest"]):
        p["index"] = i
    value.pop("_members")
    return value


def test_valid_discord_file_groups_validate():
    assert spec.validate(planned()) is not None


@pytest.mark.parametrize("fault", [
    "member_twice", "surface_twice", "bad_hash", "member_before_surface",
    "group_not_after_last_member", "bare_group_id", "unavailable_member",
    "too_many_members", "non_thread", "missing_surface_key",
    "unhashable_member", "oversize_file", "oversize_request"])
def test_discord_file_group_rejects(fault):
    value = copy.deepcopy(planned())
    manifest = value["parts"]["manifest"]
    groups = [p for p in manifest if p["kind"] == "file_group"]
    if fault == "member_twice":
        groups[1]["members"].append("attach:0001")
    elif fault == "surface_twice":
        groups[1]["surface"] = "card"
        groups[1]["part_id"] = "file_group:card"
    elif fault == "bad_hash":
        groups[0]["sha256"] = "0" * 64
    elif fault == "member_before_surface":
        manifest[2]["index"], manifest[5]["index"] = manifest[5]["index"], manifest[2]["index"]
        manifest[2], manifest[5] = manifest[5], manifest[2]
    elif fault == "group_not_after_last_member":
        manifest[4], manifest[5] = manifest[5], manifest[4]
    elif fault == "bare_group_id":
        groups[0]["part_id"] = "file_group"
    elif fault == "unavailable_member":
        manifest[2]["unavailable"] = True
    elif fault == "too_many_members":
        groups[0]["members"] = [f"attach:{i:04d}" for i in range(11)]
        groups[0]["sha256"] = "0" * 64
    elif fault == "non_thread":
        value["kind"] = "card"
    elif fault == "missing_surface_key":
        del groups[0]["surface"]
    elif fault == "unhashable_member":
        groups[0]["members"] = ["attach:0001", ["attach:0002"]]
    elif fault == "oversize_file":
        for p in manifest:
            if p["part_id"] == "attach:0001":
                p["bytes"] = spec.DISCORD_GROUP_MAX_FILE_BYTES + 1
    elif fault == "oversize_request":
        for p in manifest:
            if p["part_id"] in ("attach:0001", "attach:0002"):
                p["bytes"] = spec.DISCORD_GROUP_MAX_REQUEST_BYTES // 2
    with pytest.raises(ValueError):
        spec.validate(value)


def test_unmapped_available_attachment_is_held_not_dropped():
    """An available file whose post maps to no surface leaves the
    manifest but is returned to the caller — the render holds and the
    attachment record stays plannable, never silently deleted."""
    import sqlite3
    from mcs.notify import notify_parts
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE attachments(attachment_id INTEGER, message_id TEXT)")
    db.execute("INSERT INTO attachments VALUES(1,'m:100'),(2,'m:999')")
    manifest = [
        {"part_id": "card", "kind": "card", "index": 0},
        {"part_id": "thread", "kind": "thread", "index": 1},
        {"part_id": "body:0001", "kind": "body_part", "index": 2,
         "name": "m:100#1"}]
    entries = [
        {"part_id": "attach:0001", "kind": "attachment_part",
         "attachment_id": 1},
        {"part_id": "attach:0002", "kind": "attachment_part",
         "attachment_id": 2}]
    out, unmapped = notify_parts._order_discord_file_groups(
        db, manifest, entries, [("m:100", ["m:100"])], frozenset(), set())
    assert [e["part_id"] for e in unmapped] == ["attach:0002"]
    assert "attach:0002" not in {p["part_id"] for p in out}
    group = next(p for p in out if p["kind"] == "file_group")
    assert group["surface"] == "body:0001"
    assert group["members"] == ["attach:0001"]
