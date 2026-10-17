"""Human-bound recovery of unknown parts, with synthetic ledger and journals."""
import json

import pytest

import mcs_requests
import notify_cards as cards
import notify_transport as transport
from notify_testkit import (CFG, NOW, _begin, _dispatch, _intent, _latest_render,
                            _part_receipt, _receipt, _seed_thread, _uuid, led as led)


def prepare(led, kind="body_part"):
    _seed_thread(led, mids=(100,))
    _dispatch(led, _intent(led, payload={"message_ids": [100]}))
    render = _latest_render(led)
    assert _begin(led, render)["granted"]
    assert _receipt(led, render, f"{1:016x}")["applied"]
    parts = json.loads(render["spec_json"])["parts"]["manifest"]
    target = next(p for p in parts if p["kind"] == kind)
    for part in parts[1:]:
        assert _part_receipt(led, render, part["part_id"],
                             result="unknown" if part == target else "delivered",
                             remote_id=None if part == target else "synthetic-" + part["part_id"])["applied"]
    plan = transport.card_part_resolve_plan(led, render["delivery_id"], target["part_id"])
    assert "error" not in plan
    return {**plan, "command_id": _uuid(910), "actor": "synthetic-operator",
            "human_confirmed": True, "reason": "合成の配送確認",
            "result": "mark_delivered", "remote_id": "verified-synthetic-post",
            "evidence": {"method": "synthetic_lookup", "ref": "synthetic-proof", "worker_stopped": True}}


def test_plan_is_read_only_and_resolution_replay_is_idempotent(led):
    req = prepare(led)
    before = led.db.total_changes
    assert transport.card_part_resolve_plan(led, req["delivery_id"], req["part_id"])["expected_state"] == "unknown"
    assert led.db.total_changes == before
    assert mcs_requests.validate(req) is None
    receipt = transport.apply_card_part_resolve(led, req, CFG, NOW + 1)
    assert receipt["outcome"] == "applied" and receipt["part_state"] == "delivered"
    assert transport.apply_card_part_resolve(led, req, CFG, NOW + 2) == receipt
    assert transport.apply_card_part_resolve(led, {**req, "reason": "changed"}, CFG, NOW + 2)["error"] == "command_id_conflict"


@pytest.mark.parametrize("field,value", [("manifest_sha256", "a" * 64), ("payload_sha256", "b" * 64),
                                         ("attempt_id", "wrong-attempt"), ("channel_id", "wrong-channel")])
def test_stale_plan_never_settles_a_part(led, field, value):
    req = prepare(led)
    result = transport.apply_card_part_resolve(led, {**req, field: value}, CFG, NOW + 1)
    assert result["error"] == "stale_part_plan"
    assert transport.card_part_resolve_plan(led, req["delivery_id"], req["part_id"])["expected_state"] == "unknown"


def journal(led, req, result, remote_id=None):
    from pathlib import Path
    root = Path(cards.notify_dirs(cards.data_root(led))["discord_state"])
    (root / "journal-synthetic.jsonl").write_text(json.dumps({
        "attempt_id": req["attempt_id"], "delivery_id": req["delivery_id"],
        "part_id": req["part_id"], "phase": "result", "result": result,
        "remote_id": remote_id}) + "\n")


def test_part_journal_delivered_cannot_be_overruled(led):
    req = prepare(led)
    journal(led, req, "delivered", "actual-post")
    assert transport.apply_card_part_resolve(led, req, CFG, NOW)["error"] == "remote_id_mismatch"
    req.update(command_id=_uuid(911), result="mark_not_sent",
               evidence={**req["evidence"], "proof": "remote_absent"})
    assert transport.apply_card_part_resolve(led, req, CFG, NOW)["error"] == "journal_contradicts_proof"


def test_part_not_sent_replans_only_missing_parts(led):
    req = prepare(led)
    req.update(result="mark_not_sent", evidence={**req["evidence"], "proof": "remote_absent"})
    receipt = transport.apply_card_part_resolve(led, req, CFG, NOW + 1)
    assert receipt["outcome"] == "applied"
    latest = _latest_render(led)
    assert latest["delivery_id"] != req["delivery_id"]
    parts = json.loads(latest["spec_json"])["parts"]["manifest"]
    assert any(p["kind"] == "body_part" and p.get("prior_remote_id") for p in parts)


def test_lineworks_remote_absence_needs_risk_acceptance(led):
    req = prepare(led)
    req.update(transport="lineworks", team_id="synthetic-team", result="mark_not_sent",
               evidence={**req["evidence"], "proof": "remote_absent"})
    del req["guild_id"]
    assert transport.validate_card_part_resolve(req) == "duplicate_risk_acknowledgement_required"
    req["evidence"]["duplicate_risk_accepted"] = True
    assert transport.validate_card_part_resolve(req) is None


@pytest.mark.parametrize("blocked", ["restore", "update_failed"])
def test_part_resolution_does_not_release_independent_holds(led, blocked):
    req = prepare(led)
    if blocked == "restore":
        with led.db:
            led.db.execute("INSERT INTO notification_restore_holds(card_id,reason,held_at) VALUES(1,'synthetic_restore',?)", (NOW,))
    else:
        with led.db:
            led.db.execute("UPDATE notification_cards SET delivery_state='update_failed'")
    req.update(result="mark_not_sent", evidence={**req["evidence"], "proof": "remote_absent"})
    receipt = transport.apply_card_part_resolve(led, req, CFG, NOW + 1)
    assert receipt["outcome"] == "applied"
    assert _latest_render(led)["delivery_id"] == req["delivery_id"]


def test_command_intake_and_operator_receipt_scope(led, monkeypatch):
    from pathlib import Path
    import job_ops
    import mcs_util
    import mcs_view
    req = prepare(led)
    monkeypatch.setattr(mcs_util, "load_config", lambda path: CFG)
    command_dir = Path(cards.data_root(led)) / "cmd"
    command_dir.mkdir()
    command = command_dir / "synthetic-resolution.json"
    command.write_text(json.dumps(req))
    result = {"errors": []}
    job_ops.drain_commands(led, result, str(command_dir))
    assert not command.exists() and result["ops_commands"] == 1
    assert result["errors"] == []
    receipt = json.loads(led.db.execute("SELECT receipt_json FROM command_receipts WHERE command_id=?",
                                      (req["command_id"],)).fetchone()[0])
    context = {**receipt["scope"], "actor": req["actor"], "projects": [1], "operator": False}
    assert mcs_view.View._scope_notification_receipt(receipt, context)["error"] == "operator_only"
    context["operator"] = True
    assert mcs_view.View._scope_notification_receipt(receipt, context)["outcome"] == "applied"


def test_unknown_card_remote_absent_can_be_human_resolved(led):
    from notify_testkit import SCOPE
    _seed_thread(led, mids=(100,))
    _dispatch(led, _intent(led, payload={'message_ids':[100]}))
    render = _latest_render(led)
    assert _begin(led, render)['granted']
    assert _receipt(led, render, f'{1:016x}', result='unknown', message_id=None)['applied']
    req={'version':1,'cmd':'ops.card_resolve','command_id':_uuid(987),
         'actor':'synthetic-review','human_confirmed':True,'reason':'synthetic remote absence confirmed',
         'delivery_id':render['delivery_id'],'attempt_id':f'{1:016x}',
         'result':'mark_not_sent',**SCOPE,
         'evidence':{'method':'synthetic_lookup','ref':'synthetic-only','worker_stopped':True,'proof':'remote_absent'}}
    result=transport.apply_card_resolve(led,req,CFG,NOW+1)
    assert result['outcome']=='applied', result
