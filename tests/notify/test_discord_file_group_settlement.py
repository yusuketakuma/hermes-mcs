"""Discord ``file_group:<surface>`` settlement: members and their anchor
fan out inside the surface's own receipt transaction — never a second
wire attempt — and any unproven echo stays ``unknown``."""
import json

import pytest

import notify_transport
import notify_cmds
from adapters.common import envelopes
from adapters.common.spec import discord_file_name
from notify_testkit import (CFG, NOW, _dispatch, _intent, _latest_render,
                            _msg, _seed_thread, _uuid, led as led)


def _att(led, mid, fid, name, sha, nbytes):
    led.db.execute(
        "INSERT INTO attachments(message_id,file_id,name,local_path,"
        "bytes,sha256,state,downloaded_at,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (mid, fid, name, "/tmp/fake.txt", nbytes, sha, "downloaded", NOW, NOW))
    led.db.commit()


def _echo(part_id, name, nbytes, fid):
    sha = {"face-one.pdf": "aa", "face-two.pdf": "bb", "body-one.pdf": "bb", "body-two.pdf": "cc"}.get(name, "dd") * 32
    wire = discord_file_name({"part_id": part_id, "name": name, "sha256": sha})
    return {"part_id": part_id, "wire_name": wire, "bytes": nbytes,
            "remote_file_id": fid}


def _body_render(led):
    """Long posts overflow the face -> every file binds a body_part."""
    _seed_thread(led, mids=(100,))
    for i in range(1, 9):
        _msg(led, 100 + i, parent=100, body=f"本文{i} " + "あ" * 300)
    _att(led, 100, "f-a", "face-one.pdf", "aa" * 32, 11)
    _att(led, 107, "f-b", "body-one.pdf", "bb" * 32, 22)
    _att(led, 107, "f-c", "body-two.pdf", "cc" * 32, 33)
    assert _dispatch(led, _intent(led, payload={"message_ids": list(range(100, 109))}))["dispatched"]
    render = _latest_render(led)
    return render, json.loads(render["spec_json"])


def _begin(led, render, spec):
    claim = {"spec": spec, "attempt_id": "c" * 16, "worker_id": "b" * 16,
             "payload_hash": render["payload_hash"]}
    assert notify_transport.apply_transport_begin(led, envelopes.transport_begin(claim), CFG, now=NOW)["granted"]
    return claim


def _groups(spec):
    return [p for p in spec["parts"]["manifest"] if p["kind"] == "file_group"]


def _settle_surfaces(led, render, spec, claim, files_for):
    """Deliver card + thread, then every manifest part in order; surface
    parts carry the files echo for their own group. Grouped members and
    anchors never receive receipts — the worker skips them."""
    grouped = {mid for g in _groups(spec) for mid in g["members"]}
    grouped |= {g["part_id"] for g in _groups(spec)}
    assert notify_transport.apply_transport_receipt(
        led, envelopes.transport_receipt(claim, "delivered", message_id="card-msg"),
        CFG, now=NOW)["applied"]
    for p in spec["parts"]["manifest"]:
        if p["kind"] == "card" or p["part_id"] in grouped:
            continue
        result = "delivered"
        remote = f"remote-{p['part_id']}"
        files = files_for.get(p["part_id"])
        req = envelopes.part_receipt(claim, p, result, remote_id=remote,
                                     files=files)
        assert notify_transport.apply_part_receipt(led, req, CFG, now=NOW)["applied"], p["part_id"]
    return remote


def _member_rows(led, delivery_id):
    return {r["part_id"]: dict(r) for r in led.db.execute(
        "SELECT * FROM notification_render_parts WHERE delivery_id=?",
        (delivery_id,)).fetchall()}


def test_body_surface_groups_fan_out_inside_surface_receipts(led):
    render, spec = _body_render(led)
    claim = _begin(led, render, spec)
    groups = _groups(spec)
    assert {g["part_id"] for g in groups} == {"file_group:body:0002", "file_group:body:0016"}
    files_for = {
        "body:0002": [_echo("attach:0001", "face-one.pdf", 11, "F-1")],
        "body:0016": [_echo("attach:0002", "body-one.pdf", 22, "F-2"),
                      _echo("attach:0003", "body-two.pdf", 33, "F-3")],
    }
    _settle_surfaces(led, render, spec, claim, files_for)
    rows = _member_rows(led, render["delivery_id"])
    for g in groups:
        surface_remote = rows[g["surface"]]["remote_id"]
        assert rows[g["part_id"]]["state"] == "delivered"
        assert rows[g["part_id"]]["remote_id"] == surface_remote
        for mid in g["members"]:
            assert rows[mid]["state"] == "delivered", mid
            assert rows[mid]["remote_id"] == surface_remote
            assert rows[mid]["attempt_id"] == rows[g["surface"]]["attempt_id"]
    assert rows["attach:0001"]["remote_id"] == rows["body:0002"]["remote_id"]
    assert rows["attach:0002"]["remote_id"] == rows["body:0016"]["remote_id"]


def test_card_surface_group_fans_out_on_transport_receipt(led):
    _seed_thread(led, mids=(100,))
    _att(led, 100, "f-a", "face-one.pdf", "aa" * 32, 11)
    _att(led, 100, "f-b", "face-two.pdf", "bb" * 32, 22)
    assert _dispatch(led, _intent(led, payload={"message_ids": [100]}))["dispatched"]
    render = _latest_render(led)
    spec = json.loads(render["spec_json"])
    groups = _groups(spec)
    assert [g["part_id"] for g in groups] == ["file_group:card"]
    claim = _begin(led, render, spec)
    files = [_echo("attach:0001", "face-one.pdf", 11, "F-1"),
             _echo("attach:0002", "face-two.pdf", 22, "F-2")]
    assert notify_transport.apply_transport_receipt(
        led, envelopes.transport_receipt(claim, "delivered", message_id="card-msg",
                                         files=files), CFG, now=NOW)["applied"]
    rows = _member_rows(led, render["delivery_id"])
    assert rows["card"]["state"] == "delivered"
    for mid in groups[0]["members"] + [groups[0]["part_id"]]:
        assert rows[mid]["state"] == "delivered", mid
        assert rows[mid]["remote_id"] == "card-msg"


@pytest.mark.parametrize("fault", ["missing_member", "extra_file",
                                   "wrong_bytes", "wrong_wire", "no_echo"])
def test_unproven_echo_leaves_group_unknown_never_delivered(led, fault):
    render, spec = _body_render(led)
    claim = _begin(led, render, spec)
    good = {"body:0002": [_echo("attach:0001", "face-one.pdf", 11, "F-1")],
            "body:0016": [_echo("attach:0002", "body-one.pdf", 22, "F-2"),
                          _echo("attach:0003", "body-two.pdf", 33, "F-3")]}
    if fault == "missing_member":
        good["body:0016"] = good["body:0016"][:1]
    elif fault == "extra_file":
        good["body:0016"].append(_echo("attach:9999", "x.pdf", 1, "F-9"))
    elif fault == "dup_fid":
        good["body:0016"][1]["remote_file_id"] = "F-2"
    elif fault == "wrong_bytes":
        good["body:0016"][0]["bytes"] = 23
    elif fault == "wrong_wire":
        good["body:0016"][0]["wire_name"] = "other.pdf"
    elif fault == "no_echo":
        good["body:0016"] = None
    files_for = {k: v for k, v in good.items() if v is not None}
    _settle_surfaces(led, render, spec, claim, files_for)
    rows = _member_rows(led, render["delivery_id"])
    # the bad surface's group + members are unknown; the good one delivered
    assert rows["file_group:body:0016"]["state"] == "unknown"
    assert rows["file_group:body:0016"]["error_code"] == "file_group_unverified"
    for mid in ("attach:0002", "attach:0003"):
        assert rows[mid]["state"] == "unknown"
        assert rows[mid]["error_code"] == "file_group_unverified"
        assert rows[mid]["attempt_id"]  # resolvable, never retried blindly
    assert rows["file_group:body:0002"]["state"] == "delivered"
    assert rows["attach:0001"]["state"] == "delivered"


def test_group_anchor_operator_resolution_fans_out_members(led):
    """mark_delivered on the anchor settles every member in one tx after
    the surface-identity check; a wrong remote id is rejected."""
    import notify_recovery
    render, spec = _body_render(led)
    claim = _begin(led, render, spec)
    files_for = {"body:0002": [_echo("attach:0001", "face-one.pdf", 11, "F-1")],
                 "body:0016": [_echo("attach:0002", "body-one.pdf", 22, "F-2"),
                               _echo("attach:0003", "body-two.pdf", 33, "F-3")]}
    # deliver surfaces but leave body:0016's echo unproven
    assert notify_transport.apply_transport_receipt(
        led, envelopes.transport_receipt(claim, "delivered", message_id="card-msg"),
        CFG, now=NOW)["applied"]
    grouped = {mid for g in _groups(spec) for mid in g["members"]}
    grouped |= {g["part_id"] for g in _groups(spec)}
    for p in spec["parts"]["manifest"]:
        if p["kind"] == "card" or p["part_id"] in grouped:
            continue
        files = files_for.get(p["part_id"]) if p["part_id"] != "body:0016" else None
        req = envelopes.part_receipt(claim, p, "delivered",
                                     remote_id=f"remote-{p['part_id']}", files=files)
        assert notify_transport.apply_part_receipt(led, req, CFG, now=NOW)["applied"]
    rows = _member_rows(led, render["delivery_id"])
    assert rows["file_group:body:0016"]["state"] == "unknown"
    surface_remote = rows["body:0016"]["remote_id"]

    plan = notify_recovery.card_part_resolve_plan(
        led, render["delivery_id"], "file_group:body:0016")
    assert "error" not in plan, plan
    req = {**plan, "command_id": _uuid(940), "actor": "synthetic-operator",
           "human_confirmed": True, "reason": "合成のremote確認",
           "result": "mark_delivered", "remote_id": surface_remote,
           "evidence": {"method": "synthetic_lookup", "ref": "synthetic-proof",
                        "worker_stopped": True,
                        "files": files_for["body:0016"]}}
    no_files = {**req, "command_id": _uuid(942),
                "evidence": {k: v for k, v in req["evidence"].items() if k != "files"}}
    assert notify_recovery.apply_card_part_resolve(led, no_files, CFG, NOW + 1)["outcome"] == "rejected"
    bad = {**req, "remote_id": "remote-wrong", "command_id": _uuid(941)}
    rejected = notify_recovery.apply_card_part_resolve(led, bad, CFG, NOW + 1)
    assert rejected["outcome"] == "rejected"
    receipt = notify_recovery.apply_card_part_resolve(led, req, CFG, NOW + 2)
    assert receipt["outcome"] == "applied", receipt.get("error")
    rows = _member_rows(led, render["delivery_id"])
    for mid in ("attach:0002", "attach:0003", "file_group:body:0016"):
        assert rows[mid]["state"] == "delivered", mid
        assert rows[mid]["remote_id"] == surface_remote


@pytest.mark.parametrize("result", ["unknown", "not_sent"])
def test_failed_body_surface_propagates_state_to_group(led, result):
    """A body_part surface that never delivered leaves its members and
    anchor in the same factual state — resolvable or honestly unsent,
    never pending forever."""
    render, spec = _body_render(led)
    claim = _begin(led, render, spec)
    assert notify_transport.apply_transport_receipt(
        led, envelopes.transport_receipt(claim, "delivered", message_id="card-msg"),
        CFG, now=NOW)["applied"]
    grouped = {mid for g in _groups(spec) for mid in g["members"]}
    grouped |= {g["part_id"] for g in _groups(spec)}
    for p in spec["parts"]["manifest"]:
        if p["kind"] == "card" or p["part_id"] in grouped:
            continue
        if p["part_id"] == "body:0016":
            req = envelopes.part_receipt(claim, p, result,
                                         remote_id=None if result == "not_sent" else "remote-x",
                                         error_code="edit_result_unknown" if result == "unknown" else "http_500")
        else:
            req = envelopes.part_receipt(claim, p, "delivered",
                                         remote_id=f"remote-{p['part_id']}")
        assert notify_transport.apply_part_receipt(led, req, CFG, now=NOW)["applied"], p["part_id"]
    rows = _member_rows(led, render["delivery_id"])
    for pid in ("attach:0002", "attach:0003", "file_group:body:0016"):
        assert rows[pid]["state"] == result, (pid, rows[pid]["state"])
        assert rows[pid]["attempt_id"] == rows["body:0016"]["attempt_id"]
    # the other surface delivered without a files echo -> unproven unknown
    assert rows["file_group:body:0002"]["state"] == "unknown"
    assert rows["attach:0001"]["state"] == "unknown"


@pytest.mark.parametrize("surface", ["card", "body_part"])
def test_surface_file_echo_passes_command_boundary(led, surface):
    render, spec = _body_render(led)
    claim = _begin(led, render, spec)
    files = [_echo("attach:0001", "face-one.pdf", 11, "F-1")]
    if surface == "card":
        req = envelopes.transport_receipt(claim, "delivered", message_id="card-msg", files=files)
    else:
        part = next(p for p in spec["parts"]["manifest"] if p["kind"] == "body_part")
        req = envelopes.part_receipt(claim, part, "delivered", remote_id="body-msg", files=files)
    assert notify_cmds.validate_int(req) is None
    for invalid in [None, {}, [None], files * 2,
                    [{**files[0], "bytes": True}],
                    [{**files[0], "remote_file_id": ""}],
                    [{**files[0], "extra": "unexpected"}]]:
        assert notify_cmds.validate_int({**req, "files": invalid}) == "bad_files_echo"


def test_file_echo_is_discord_surface_only():
    files = [_echo("attach:0001", "a.pdf", 11, "F-1")]
    for transport in ("slack", "lineworks"):
        assert notify_transport.files_echo_error(
            {"transport": transport, "files": files}) == "bad_files_echo"
    assert notify_transport.files_echo_error(
        {"op": "part_receipt", "kind": "attachment_part", "files": files}) == "bad_files_echo"


def test_late_same_surface_echo_recovers_unknown_members_without_new_attempt(led):
    render, spec = _body_render(led)
    claim = _begin(led, render, spec)
    _settle_surfaces(led, render, spec, claim, {})
    part = next(p for p in spec["parts"]["manifest"] if p["part_id"] == "body:0016")
    files = [_echo("attach:0002", "body-one.pdf", 22, "F-2"),
             _echo("attach:0003", "body-two.pdf", 33, "F-3")]
    req = envelopes.part_receipt(claim, part, "delivered", remote_id="remote-body:0016", files=files)
    foreign = {**req, "attempt_id": "p:" + "d" * 32 + ":body:0016"}
    assert notify_transport.apply_part_receipt(led, foreign, CFG, NOW + 1)["error"] == "attempt_mismatch"
    assert notify_transport.apply_part_receipt(led, req, CFG, NOW + 2)["applied"]
    rows = _member_rows(led, render["delivery_id"])
    for pid, fid in [("attach:0002", "F-2"), ("attach:0003", "F-3")]:
        assert rows[pid]["state"] == "delivered"
        assert rows[pid]["remote_file_id"] == fid
    assert rows["file_group:body:0016"]["state"] == "delivered"
    # The durable file identity survives spec GC without changing source bytes.
    led.db.execute("UPDATE notification_renders SET spec_json=NULL WHERE delivery_id=?", (render["delivery_id"],))
    led.db.commit()
    assert _member_rows(led, render["delivery_id"])["attach:0002"]["remote_file_id"] == "F-2"


def test_same_attempt_surface_not_sent_settles_only_unresolved_dependents(led):
    import notify_cards
    render, spec = _body_render(led)
    claim = _begin(led, render, spec)
    assert notify_transport.apply_transport_receipt(
        led, envelopes.transport_receipt(claim, "delivered", message_id="card-msg"),
        CFG, NOW)["applied"]
    part = next(p for p in spec["parts"]["manifest"] if p["part_id"] == "body:0016")
    unknown = envelopes.part_receipt(claim, part, "unknown", error_code="synthetic_unknown")
    assert notify_transport.apply_part_receipt(led, unknown, CFG, NOW + 1)["applied"]
    # A resolved sibling and another attempt remain immutable.
    led.db.execute("UPDATE notification_render_parts SET state='delivered',remote_id='old-proof',remote_file_id='old-fid' "
                   "WHERE delivery_id=? AND part_id='attach:0002'", (render["delivery_id"],))
    led.db.execute("UPDATE notification_render_parts SET attempt_id='foreign-attempt' "
                   "WHERE delivery_id=? AND part_id='attach:0003'", (render["delivery_id"],))
    led.db.commit()
    not_sent = envelopes.part_receipt(claim, part, "not_sent", error_code="operator_part_resolve")
    assert notify_transport.apply_part_receipt(led, not_sent, CFG, NOW + 2)["applied"]
    rows = _member_rows(led, render["delivery_id"])
    assert rows["file_group:body:0016"]["state"] == "not_sent"
    assert rows["attach:0002"]["state"] == "delivered"
    assert rows["attach:0002"]["remote_file_id"] == "old-fid"
    assert rows["attach:0003"]["state"] == "unknown"
    assert rows["attach:0003"]["attempt_id"] == "foreign-attempt"
    assert notify_cards.connection(led).in_transaction is False


def test_card_surface_followup_completes_even_if_companion_thread_fails(led, monkeypatch):
    import notify_flush
    import present_discord
    from test_late_attachments_cross_transport import _late_file, _deliver, _state
    monkeypatch.setattr(present_discord, "FACE_ORIGINAL_MAX", 30000)
    cfg, card_mid, thread_rid, _aid, _blob, event = _late_file(led, monkeypatch, "discord")
    notify_flush.flush(led)
    spec = _deliver(led, cfg, card_mid, thread_rid, thread_result="not_sent")
    assert any(g.get("surface") == "card" for g in _groups(spec))
    assert _state(led, event) == "accepted"
