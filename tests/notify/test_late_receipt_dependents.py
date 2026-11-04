"""Synthetic late delivery evidence and dependency recovery without content drift."""
import json
from pathlib import Path

import pytest

import notify_cards
import notify_recovery
import notify_spool
from notify_testkit import (CFG, NOW, SCOPE, _begin, _dispatch, _intent,
                            _latest_render, _part_receipt, _receipt, _seed_thread,
                            _uuid, led as led)


def _parts(led, render):
    return [dict(row) for row in led.db.execute(
        "SELECT * FROM notification_render_parts WHERE delivery_id=? ORDER BY idx",
        (render["delivery_id"],))]


def _prepare(led, dependency):
    _seed_thread(led, mids=(100,))
    _dispatch(led, _intent(led, payload={"message_ids": [100]}))
    render = _latest_render(led)
    assert _begin(led, render)["granted"]
    if dependency == "card":
        assert _receipt(led, render, f"{1:016x}", result="unknown",
                        message_id=None)["applied"]
        req = {"version": 1, "cmd": "ops.card_resolve", **SCOPE,
               "delivery_id": render["delivery_id"], "attempt_id": f"{1:016x}",
               "message_id": "synthetic-card"}
    else:
        assert _receipt(led, render, f"{1:016x}", message_id="synthetic-card")["applied"]
        assert _part_receipt(led, render, "thread", result="unknown",
                             remote_id=None)["applied"]
        req = {**notify_recovery.card_part_resolve_plan(led, render["delivery_id"], "thread"),
               "remote_id": "synthetic-thread"}
    req.update(command_id=_uuid(990), actor="synthetic-operator", human_confirmed=True,
               reason="合成の配信確認", result="mark_delivered",
               evidence={"method": "synthetic_lookup", "ref": "synthetic-proof",
                         "worker_stopped": True})
    return render, req


def _resolve(led, req):
    apply = (notify_recovery.apply_card_resolve if req["cmd"] == "ops.card_resolve"
             else notify_recovery.apply_card_part_resolve)
    return apply(led, req, CFG, NOW + 1)


@pytest.mark.parametrize("dependency", ["card", "thread"])
def test_operator_late_delivery_replans_proven_unstarted_dependents_without_drift(led, dependency):
    render, req = _prepare(led, dependency)
    assert any(part["state"] == "held" for part in _parts(led, render))
    receipt = _resolve(led, req)
    assert receipt["outcome"] == "applied"
    successor = _latest_render(led)
    assert successor["delivery_id"] != render["delivery_id"]
    assert successor["op"] == "update"
    assert all(part["state"] != "held" for part in _parts(led, render))
    assert any(part["kind"] == "body_part" for part in _parts(led, successor))
    dirs = notify_spool.notify_dirs(notify_spool.data_root(led))
    assert not (Path(dirs["discord_render"]) / (render["delivery_id"] + ".json")).exists()
    assert (Path(dirs["discord_render"]) / (successor["delivery_id"] + ".json")).exists()
    assert _resolve(led, req) == receipt
    assert _latest_render(led)["delivery_id"] == successor["delivery_id"]


@pytest.mark.parametrize("dependency", ["card", "thread"])
def test_late_factual_dependent_receipts_settle_dependency_holds(led, dependency):
    render, _ = _prepare(led, dependency)
    # Factual receipts can overtake the parent receipt in the command queue.
    body = _part_receipt(led, render, "body:0001", remote_id="synthetic-body")
    assert body["applied"]
    assert _part_receipt(led, render, "body:0001", remote_id="synthetic-body")["applied"]
    assert _part_receipt(led, render, "body:0001", remote_id="other-body")["error"] == "part_conflict"
    if dependency == "card":
        assert _receipt(led, render, f"{1:016x}", message_id="synthetic-card")["applied"]
        assert _part_receipt(led, render, "thread", remote_id="synthetic-thread")["applied"]
    else:
        assert _part_receipt(led, render, "thread", remote_id="synthetic-thread")["applied"]
    for part in _parts(led, render):
        if part["kind"] == "body_part" and part["part_id"] != "body:0001":
            assert _part_receipt(led, render, part["part_id"],
                                 remote_id="synthetic-" + part["part_id"])["applied"]
    assert all(part["state"] == "delivered" for part in _parts(led, render))
    assert _latest_render(led)["delivery_id"] == render["delivery_id"]


@pytest.mark.parametrize("mutation,error", [("channel_id", "scope_mismatch"),
                                           ("payload_hash", "payload_hash_mismatch"),
                                           ("render_rev", "render_rev_mismatch"),
                                           ("attempt_id", "attempt_mismatch")])
def test_late_held_receipt_requires_exact_identity(led, mutation, error):
    render, _ = _prepare(led, "thread")
    value = 99 if mutation == "render_rev" else "foreign"
    out = _part_receipt(led, render, "body:0001", remote_id="synthetic-body",
                        mutate=lambda req: req.update({mutation: value}))
    assert not out["applied"] and out["error"] == error
    assert next(part for part in _parts(led, render) if part["kind"] == "body_part")["state"] == "held"


@pytest.mark.parametrize("block", ["unknown", "started", "tainted", "revoked", "attempt", "independent"])
def test_operator_recovery_does_not_guess_held_parts_unsent(led, block):
    render, req = _prepare(led, "thread")
    body = next(part for part in _parts(led, render) if part["kind"] == "body_part")
    if block in ("unknown", "attempt", "independent"):
        with led.db:
            if block == "unknown":
                led.db.execute("UPDATE notification_render_parts SET state='unknown',attempt_id=? "
                               "WHERE delivery_id=? AND part_id=?",
                               ("synthetic-inflight", render["delivery_id"], body["part_id"]))
            elif block == "attempt":
                led.db.execute("UPDATE notification_render_parts SET attempt_id='synthetic-inflight' "
                               "WHERE delivery_id=? AND part_id=?", (render["delivery_id"], body["part_id"]))
            else:
                led.db.execute("UPDATE notification_render_parts SET error_code='card_revoked' "
                               "WHERE delivery_id=? AND part_id=?", (render["delivery_id"], body["part_id"]))
    elif block == "revoked":
        with led.db:
            notify_cards.revoke_card(led.db, render["card_id"], NOW)
    else:
        dirs = notify_spool.notify_dirs(notify_spool.data_root(led))
        witness = {"attempt_id": f"p:{render['delivery_id'].replace('-', '')}:{body['part_id']}",
                   "delivery_id": render["delivery_id"], "part_id": body["part_id"], "phase": "started"}
        (Path(dirs["discord_state"]) / "journal-synthetic.jsonl").write_text(
            json.dumps(witness) + "\n" if block == "started" else "{broken\n")
    before = _parts(led, render)
    assert _resolve(led, req)["outcome"] == "applied"
    after = {part["part_id"]: part for part in _parts(led, render)}
    assert after[body["part_id"]]["state"] == next(part for part in before if part["part_id"] == body["part_id"])["state"]
    assert not any(part["error_code"] == "dependency_not_started" for part in after.values())


def test_independent_held_part_does_not_accept_a_late_fact(led):
    render, _ = _prepare(led, "thread")
    with led.db:
        led.db.execute("UPDATE notification_render_parts SET error_code='card_revoked' "
                       "WHERE delivery_id=? AND kind='body_part'", (render["delivery_id"],))
    assert _part_receipt(led, render, "body:0001", remote_id="synthetic-body")["error"] == "part_conflict"


def test_primary_resolution_without_worker_stop_does_not_replan(led):
    render, req = _prepare(led, "card")
    req["evidence"].pop("worker_stopped")
    assert _resolve(led, req)["outcome"] == "applied"
    assert _latest_render(led)["delivery_id"] == render["delivery_id"]
    assert any(part["state"] == "held" for part in _parts(led, render))


@pytest.mark.parametrize("dependency", ["card", "thread"])
def test_already_delivered_parent_can_recover_its_unstarted_holds(led, dependency):
    render, req = _prepare(led, dependency)
    if dependency == "card":
        assert _receipt(led, render, f"{1:016x}", message_id="synthetic-card")["applied"]
    else:
        assert _part_receipt(led, render, "thread", remote_id="synthetic-thread")["applied"]
        req = {"version": 1, "cmd": "ops.card_resolve", **SCOPE,
               "delivery_id": render["delivery_id"], "attempt_id": f"{1:016x}",
               "message_id": "synthetic-card", **{key: req[key] for key in
               ("command_id", "actor", "human_confirmed", "reason", "result", "evidence")}}
    receipt = _resolve(led, req)
    assert receipt["outcome"] == "applied"
    assert receipt["unstarted_parts"]
    assert _latest_render(led)["delivery_id"] != render["delivery_id"]
    assert _resolve(led, req) == receipt


def test_late_held_unknown_is_preserved_by_parent_resolution(led):
    render, req = _prepare(led, "thread")
    assert _part_receipt(led, render, "body:0001", result="unknown", remote_id=None)["applied"]
    assert _resolve(led, req)["outcome"] == "applied"
    assert next(part for part in _parts(led, render) if part["kind"] == "body_part")["state"] == "unknown"
    assert _latest_render(led)["delivery_id"] == render["delivery_id"]


@pytest.mark.parametrize("dependency", ["card", "thread"])
@pytest.mark.parametrize("after_unlink", [False, True])
def test_withdraw_failure_rolls_back_and_same_command_can_retry(led, monkeypatch, dependency, after_unlink):
    render, req = _prepare(led, dependency)
    before = _parts(led, render)
    dirs = notify_spool.notify_dirs(notify_spool.data_root(led))
    path = Path(dirs["discord_render"]) / (render["delivery_id"] + ".json")
    unlink = notify_recovery.os.unlink

    def fail_old_spec(candidate, *args, **kwargs):
        if str(candidate) == str(path):
            if after_unlink:
                unlink(candidate, *args, **kwargs)
            raise OSError("synthetic-withdraw-failure")
        return unlink(candidate, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(notify_recovery.os, "unlink", fail_old_spec)
        with pytest.raises(OSError, match="synthetic-withdraw-failure"):
            _resolve(led, req)
    assert _parts(led, render) == before
    assert _latest_render(led)["delivery_id"] == render["delivery_id"]
    assert led.db.execute("SELECT 1 FROM command_receipts WHERE command_id=?",
                          (req["command_id"],)).fetchone() is None
    assert led.db.execute("SELECT spec_json FROM notification_renders WHERE delivery_id=?",
                          (render["delivery_id"],)).fetchone()[0] is not None
    assert path.exists() is not after_unlink
    receipt = _resolve(led, req)
    assert receipt["outcome"] == "applied"
    assert _latest_render(led)["delivery_id"] != render["delivery_id"]
    assert _resolve(led, req) == receipt
