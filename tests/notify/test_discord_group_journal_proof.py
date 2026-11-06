"""Logical Discord file groups must use the owning wire attempt's journal."""
import json

import pytest

import notify_recovery
from notify_testkit import (_dispatch, _intent, _latest_render, _seed_thread, led as led)
from test_discord_file_group_settlement import _att, _body_render, _groups


@pytest.fixture(params=["card", "body"])
def group_request(led, request):
    if request.param == "body":
        render, spec = _body_render(led)
    else:
        _seed_thread(led, mids=(100,))
        _att(led, 100, "f-a", "face-one.pdf", "aa" * 32, 11)
        assert _dispatch(led, _intent(led, payload={"message_ids": [100]}))["dispatched"]
        render = _latest_render(led)
        spec = json.loads(render["spec_json"])
    group = _groups(spec)[0]
    attempt = "c" * 16
    led.db.execute("UPDATE notification_render_parts SET attempt_id=? WHERE delivery_id=?",
                   (attempt, render["delivery_id"]))
    req = {"delivery_id": render["delivery_id"], "part_id": group["part_id"],
           "attempt_id": attempt, "result": "mark_not_sent",
           "evidence": {"proof": "no_journal_started"}}
    return req, group


def _check(led, req, rows):
    return notify_recovery._part_resolution_journal_error(
        led, req, journal_evidence=({"attempt": {"rows": rows}}, False))


def _row(req, group, **extra):
    value = {"delivery_id": req["delivery_id"], "attempt_id": req["attempt_id"],
             "phase": "started", **extra}
    if group["surface"] != "card":
        value["part_id"] = group["surface"]
    return value


@pytest.mark.parametrize("member", [False, True])
@pytest.mark.parametrize("phase,result", [("started", None), ("result", "delivered")])
def test_group_and_member_proof_sees_surface_wire(led, group_request, member, phase, result):
    req, group = group_request
    if member:
        req = {**req, "part_id": group["members"][0]}
    assert _check(led, req, [_row(req, group, phase=phase, result=result)]) == "journal_contradicts_proof"


@pytest.mark.parametrize("different", ["attempt_id", "delivery_id"])
def test_other_attempt_or_delivery_does_not_contaminate_proof(led, group_request, different):
    req, group = group_request
    row = _row(req, group)
    row[different] = "different"
    assert _check(led, req, [row]) is None


def test_api_rejection_needs_surface_result(led, group_request):
    req, group = group_request
    req["evidence"]["proof"] = "api_rejected"
    assert _check(led, req, []) == "api_rejection_unproven"
    assert _check(led, req, [_row(req, group, phase="result", result="not_sent")]) is None


def test_remote_absent_keeps_human_contract_but_not_known_delivery(led, group_request):
    req, group = group_request
    req["evidence"]["proof"] = "remote_absent"
    assert _check(led, req, [_row(req, group)]) is None
    assert _check(led, req, [_row(req, group, phase="result", result="delivered")]) == "journal_contradicts_proof"


def test_delivered_identity_uses_card_message_id_or_body_remote_id(led, group_request):
    req, group = group_request
    req.update(result="mark_delivered", remote_id="expected")
    key = "message_id" if group["surface"] == "card" else "remote_id"
    row = _row(req, group, phase="result", result="delivered", **{key: "other"})
    assert _check(led, req, [row]) == "remote_id_mismatch"
    row[key] = "expected"
    assert _check(led, req, [row]) is None


@pytest.mark.parametrize("fault", ["malformed", "hash", "attempt"])
def test_unbound_group_never_authorizes_no_journal_started(led, group_request, fault):
    req, group = group_request
    if fault == "malformed":
        led.db.execute("UPDATE notification_renders SET spec_json='{' WHERE delivery_id=?", (req["delivery_id"],))
    elif fault == "hash":
        led.db.execute("UPDATE notification_renders SET payload_hash=? WHERE delivery_id=?",
                       ("f" * 64, req["delivery_id"]))
    else:
        led.db.execute("UPDATE notification_render_parts SET attempt_id=? WHERE delivery_id=? AND part_id=?",
                       ("d" * 16, req["delivery_id"], group["surface"]))
    assert _check(led, req, []) == "file_group_identity_unverified"


def test_member_plan_redirects_to_whole_group(led, group_request):
    req, group = group_request
    led.db.execute("UPDATE notification_render_parts SET state='unknown' WHERE delivery_id=?",
                   (req["delivery_id"],))
    anchor = notify_recovery.card_part_resolve_plan(led, req["delivery_id"], group["part_id"])
    member = notify_recovery.card_part_resolve_plan(led, req["delivery_id"], group["members"][0])
    assert "error" not in anchor
    assert member == anchor
    assert member["part_id"] == group["part_id"]


@pytest.mark.parametrize("fault", ["terminal", "attempt", "hash"])
def test_member_plan_requires_unknown_bound_anchor(led, group_request, fault):
    req, group = group_request
    led.db.execute("UPDATE notification_render_parts SET state='unknown' WHERE delivery_id=?",
                   (req["delivery_id"],))
    if fault == "hash":
        led.db.execute("UPDATE notification_renders SET payload_hash=? WHERE delivery_id=?",
                       ("f" * 64, req["delivery_id"]))
    elif fault == "attempt":
        led.db.execute("UPDATE notification_render_parts SET attempt_id=? WHERE delivery_id=? AND part_id=?",
                       ("d" * 16, req["delivery_id"], group["part_id"]))
    else:
        led.db.execute("UPDATE notification_render_parts SET state='delivered' WHERE delivery_id=? AND part_id=?",
                       (req["delivery_id"], group["part_id"]))
    assert notify_recovery.card_part_resolve_plan(
        led, req["delivery_id"], group["members"][0]) == {"error": "file_group_identity_unverified"}
