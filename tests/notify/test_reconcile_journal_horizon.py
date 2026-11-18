"""O5: a restore from a backup older than the journal's capped horizon holds every delivery."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import uuid

import pytest

import backup_names
import notify_cards
import notify_reconcile
import notify_recovery
import notify_spool
import notify_transport
from adapters.common import envelopes
from adapters.common.spec import discord_file_name
from notify_reconcile_testkit import CFG, NOW, world
from notify_testkit import CFG as TK_CFG, _dispatch, _intent, _latest_render, _seed_thread, pinned_clock
from notify_testkit import led as led
from slack_testkit import SLACK
from test_discord_file_group_settlement import _att, _body_render

__all__ = ["led", "pinned_clock", "world"]
pytestmark = pytest.mark.usefixtures("pinned_clock", "v1_thread_bodies")


def _restore_from(world, age_s, restored_at=NOW, *, data=None):
    data = world.data if data is None else data
    backup = data / "backups" / "preupdate-20260101-000000-x.db"
    backup.parent.mkdir(exist_ok=True)
    backup.write_bytes(b"synthetic")
    os.utime(backup, (restored_at - age_s, restored_at - age_s))
    notify_spool.mark_restored(str(data), backup_path=str(backup),
                               by="mcs_update", now=restored_at)


def _resolve(world, delivery_id, result, attempt_id=None, **extra):
    req = {"version": 1, "cmd": "ops.card_resolve",
           "command_id": str(uuid.uuid4()), "actor": "op-user",
           "human_confirmed": True, "reason": "合成の復元後確認",
           # a hold with no attempt is named by its delivery_id
           "delivery_id": delivery_id, "attempt_id": attempt_id or delivery_id,
           "result": result, **CFG["notify"]["discord"], **extra}
    return notify_recovery.apply_card_resolve(world.led, req, CFG, now=NOW)


def test_backup_older_than_the_cap_holds_every_unsettled_render(world):
    world.seed()
    world.dispatch()
    render = world.render()
    assert render["state"] not in ("held", "delivered")
    _restore_from(world, backup_names.JOURNAL_HORIZON_MAX_S + 86400)
    receipt = notify_reconcile.reconcile_after_restore(world.led, CFG, now=NOW)
    states = {r["state"] for r in world.led.db.execute(
        "SELECT state FROM notification_renders").fetchall()}
    assert states == {"held"}
    holds = world.holds()
    assert holds and {h["reason"] for h in holds} == {"restore_beyond_journal_horizon"}
    assert render["delivery_id"] in {h["delivery_id"] for h in receipt["held"]}
    # idempotent while the marker is still being reconciled
    notify_reconcile.reconcile_after_restore(world.led, CFG, now=NOW)
    assert len(world.holds()) == len(holds)


def test_undispatched_events_held_beyond_the_cap_alert_the_operator(world):
    world.seed()
    eid = world.led.outbox_add("new_messages", 1, {"message_ids": [100, 101]})
    world.led.db.commit()
    _restore_from(world, backup_names.JOURNAL_HORIZON_MAX_S + 86400)
    receipt = notify_reconcile.reconcile_after_restore(world.led, CFG, now=NOW)
    ev = world.led.db.execute(
        "SELECT state,next_try FROM notify_outbox WHERE event_id=?", (eid,)).fetchone()
    assert (ev["state"], ev["next_try"]) == ("failed", None)
    assert world.holds() == [] and receipt["events_held"] == 1
    notices = world.led.db.execute(
        "SELECT payload FROM notify_outbox WHERE kind='update_notice'").fetchall()
    assert len(notices) == 1
    assert "未配送の操作通知 held=1件" in json.loads(notices[0]["payload"])["text"]
    # a rerun moves nothing new, so the notice is not repeated
    notify_reconcile.reconcile_after_restore(world.led, CFG, now=NOW + 1)
    assert world.led.db.execute(
        "SELECT COUNT(*) FROM notify_outbox WHERE kind='update_notice'").fetchone()[0] == 1


def test_backup_inside_the_cap_keeps_the_journal_reading(world):
    world.seed()
    world.dispatch()
    state = world.render()["state"]
    _restore_from(world, 10 * 86400)
    notify_reconcile.reconcile_after_restore(world.led, CFG, now=NOW)
    assert world.render()["state"] == state
    assert world.holds() == []


@pytest.mark.parametrize("result, extra, state", [
    ("mark_not_sent", {"evidence": {"method": "synthetic", "ref": "channel-check",
                                    "proof": "remote_absent", "worker_stopped": True}},
     "not_sent"),
    ("mark_delivered", {"message_id": "synthetic-msg",
                        "evidence": {"method": "synthetic", "ref": "msg"}},
     "delivered")])
def test_card_resolve_releases_an_attempt_less_horizon_hold(world, result, extra, state):
    world.seed()
    world.dispatch()
    render = world.render()
    _restore_from(world, backup_names.JOURNAL_HORIZON_MAX_S + 86400)
    notify_reconcile.reconcile_after_restore(world.led, CFG, now=NOW)
    hold = next(h for h in world.holds() if h["delivery_id"] == render["delivery_id"])
    assert hold["attempt_id"] is None
    wrong = _resolve(world, render["delivery_id"], result, "not-the-delivery", **extra)
    assert (wrong["outcome"], wrong["error"]) == ("rejected", "attempt_mismatch")
    receipt = _resolve(world, render["delivery_id"], result, **extra)
    assert receipt["outcome"] == "applied", receipt
    assert receipt["rebound"] is True
    assert world.led.db.execute(
        "SELECT state FROM notification_renders WHERE delivery_id=?",
        (render["delivery_id"],)).fetchone()["state"] == state
    released = [h for h in world.holds() if h["card_id"] == render["card_id"]]
    assert released and all(h["released_at"] is not None for h in released)
    if state == "not_sent":
        # proven absent: the unfrozen card issues its next render to send
        assert world.render()["card_id"] == render["card_id"]
        assert world.render()["delivery_id"] != render["delivery_id"]
        assert world.render()["state"] == "queued"


def test_horizon_hold_rejects_a_journal_only_absence_proof(world):
    world.seed()
    world.dispatch()
    render = world.render()
    _restore_from(world, backup_names.JOURNAL_HORIZON_MAX_S + 86400)
    notify_reconcile.reconcile_after_restore(world.led, CFG, now=NOW)
    # the journal rows that could witness the send may be compacted away
    receipt = _resolve(world, render["delivery_id"], "mark_not_sent",
                       evidence={"method": "synthetic", "ref": "journal",
                                 "proof": "no_journal_started", "worker_stopped": True})
    assert (receipt["outcome"], receipt["error"]) == ("rejected", "journal_cannot_vouch")
    assert world.render()["state"] == "held"


def test_renders_after_the_restore_are_not_held_on_rerun(world):
    world.seed()
    _restore_from(world, backup_names.JOURNAL_HORIZON_MAX_S + 86400,
                  restored_at=NOW - 3600)
    # a tainted journal keeps the marker, so reconcile runs again each tick
    (world.data / "discord_state" / "journal-w1.jsonl").write_text("{broken\n")
    notify_reconcile.reconcile_after_restore(world.led, CFG, now=NOW)
    world.dispatch()                       # created at NOW, after the restore
    notify_reconcile.reconcile_after_restore(world.led, CFG, now=NOW + 1)
    notify_reconcile.reconcile_after_restore(world.led, CFG, now=NOW + 2)
    assert world.holds() == []
    assert world.render()["state"] != "held"


# ---------- a delivered card whose thread/body parts never reported back ----------

_SCOPE = {"profile": "mcs", "application_id": "1", "guild_id": "7", "channel_id": "42"}
_HOLD = "restore_beyond_journal_horizon"


def _card_render(world, card_id=1):
    return dict(world.led.db.execute(
        "SELECT * FROM notification_renders WHERE card_id=? ORDER BY render_rev DESC LIMIT 1",
        (card_id,)).fetchone())


def _deliver_card_only(world, card_id=1, n=1):
    """The card part lands; its thread and body parts are still pending."""
    render = _card_render(world, card_id)
    aid = f"{n:016x}"
    common = {"version": 1, "attempt_id": aid, "delivery_id": render["delivery_id"],
              "render_rev": render["render_rev"], "payload_hash": render["payload_hash"],
              "route_epoch": 1, **_SCOPE}
    assert notify_transport.apply_transport_begin(world.led, {
        **common, "op": "transport_begin", "command_id": str(uuid.UUID(int=n)),
        "worker_id": "bb" * 8}, CFG, now=NOW)["granted"]
    assert notify_transport.apply_transport_receipt(world.led, {
        **common, "op": "transport_receipt", "command_id": str(uuid.UUID(int=1000 + n)),
        "correlation": render["correlation"], "result": "delivered",
        "message_id": "synthetic-card"}, CFG, now=NOW)["applied"]
    return _card_render(world, card_id)


def _parts(world, delivery_id):
    return {r["part_id"]: dict(r) for r in world.led.db.execute(
        "SELECT * FROM notification_render_parts WHERE delivery_id=?", (delivery_id,))}


def _part_resolve(world, plan, remote_id, n):
    req = {**plan, "command_id": str(uuid.UUID(int=5000 + n)), "actor": "op-user",
           "human_confirmed": True, "reason": "合成の復元後確認", "result": "mark_delivered",
           "remote_id": remote_id,
           "evidence": {"method": "synthetic", "ref": "channel-check", "worker_stopped": True}}
    return notify_recovery.apply_card_part_resolve(world.led, req, CFG, now=NOW + n)


def test_delivered_card_with_pending_parts_is_made_resolvable(world):
    world.seed()
    world.dispatch()
    render = _deliver_card_only(world)
    assert render["state"] == "delivered"
    spec_path = world.data / "discord_render" / (render["delivery_id"] + ".json")
    original = spec_path.read_bytes()
    _restore_from(world, backup_names.JOURNAL_HORIZON_MAX_S + 86400)
    receipt = notify_reconcile.reconcile_after_restore(world.led, CFG, now=NOW)
    assert notify_spool.restore_pending(str(world.data)) is None
    assert not spec_path.exists(), "a worker can replay the old spec after the restore gate opens"
    assert (world.data / "restore_specs" / spec_path.name).read_bytes() == original
    assert notify_spool.recover(world.led, CFG, {})["republished"] == 0
    parts = _parts(world, render["delivery_id"])
    assert parts["card"]["state"] == "delivered"
    pending = sorted(p for p in parts if p != "card")
    assert pending == ["body:0001", "body:0002", "thread"]
    for part_id in pending:
        assert (parts[part_id]["state"], parts[part_id]["error_code"]) == ("unknown", _HOLD)
        assert parts[part_id]["attempt_id"] == envelopes.part_attempt_id(render["delivery_id"], part_id)
    assert sorted(p["part_id"] for p in receipt["parts_held"]
                  if p["delivery_id"] == render["delivery_id"]) == pending
    assert {p["reason"] for p in receipt["parts_held"]} == {_HOLD}
    notice = world.led.db.execute(
        "SELECT payload FROM notify_outbox WHERE kind='update_notice'").fetchone()
    assert "ops.card_part_resolve" in notice["payload"]
    assert _HOLD in notify_recovery.PART_HOLD_REASONS
    report = notify_recovery.part_hold_report(world.led)
    assert report["parts"]["unknown"][_HOLD] == 3
    # The parts block the card's next update until an operator settles them.
    world.led.db.execute("UPDATE messages SET body_text=?, content_hash=? WHERE message_id=100",
                         ("編集後の本文", "f" * 64))
    world.led.db.commit()
    notify_cards.sweep(world.led, CFG, now=NOW + 1)
    assert _card_render(world)["delivery_id"] == render["delivery_id"]
    for n, part_id in enumerate(pending, start=1):
        plan = notify_recovery.card_part_resolve_plan(world.led, render["delivery_id"], part_id)
        assert "error" not in plan and plan["expected_state"] == "unknown"
        result = _part_resolve(world, plan, f"synthetic-{part_id}", n)
        assert result["outcome"] == "applied", result
    notify_cards.sweep(world.led, CFG, now=NOW + 10)
    update = _card_render(world)
    assert update["delivery_id"] != render["delivery_id"] and update["op"] == "update"


def test_rerun_keeps_the_same_part_holds_and_announces_once(world):
    world.seed()
    world.dispatch()
    render = _deliver_card_only(world)
    _restore_from(world, backup_names.JOURNAL_HORIZON_MAX_S + 86400)
    # a tainted journal keeps the marker, so reconcile runs again each tick
    (world.data / "discord_state" / "journal-w1.jsonl").write_text("{broken\n")
    notify_reconcile.reconcile_after_restore(world.led, CFG, now=NOW)
    first = _parts(world, render["delivery_id"])
    receipt = json.loads((world.data / notify_spool.RESTORE_RECEIPT).read_text())
    again = notify_reconcile.reconcile_after_restore(world.led, CFG, now=NOW + 1)
    assert _parts(world, render["delivery_id"]) == first
    # the re-run's receipt still lists the parts awaiting ops.card_part_resolve
    assert again["parts_held"] == receipt["parts_held"] and len(again["parts_held"]) == 3
    notices = world.led.db.execute(
        "SELECT COUNT(*) FROM notify_outbox WHERE kind='update_notice'").fetchone()[0]
    assert notices == 1


def test_late_receipt_with_the_worker_attempt_settles_a_held_part(world):
    world.seed()
    world.dispatch()
    render = _deliver_card_only(world)
    _restore_from(world, backup_names.JOURNAL_HORIZON_MAX_S + 86400)
    notify_reconcile.reconcile_after_restore(world.led, CFG, now=NOW)
    late = notify_transport.apply_part_receipt(world.led, {
        "version": 1, "op": "part_receipt", "command_id": str(uuid.UUID(int=77)),
        "attempt_id": envelopes.part_attempt_id(render["delivery_id"], "body:0001"),
        "delivery_id": render["delivery_id"], "render_rev": render["render_rev"],
        "payload_hash": render["payload_hash"], "route_epoch": 1,
        "correlation": render["correlation"], **_SCOPE,
        "part_id": "body:0001", "result": "delivered", "remote_id": "synthetic-late"}, CFG, now=NOW + 1)
    assert late["applied"], late
    part = _parts(world, render["delivery_id"])["body:0001"]
    assert (part["state"], part["remote_id"]) == ("delivered", "synthetic-late")



def test_beyond_horizon_part_rejects_a_journal_only_absence_proof(world):
    world.seed()
    world.dispatch()
    render = _deliver_card_only(world)
    _restore_from(world, backup_names.JOURNAL_HORIZON_MAX_S + 86400)
    notify_reconcile.reconcile_after_restore(world.led, CFG, now=NOW)
    plan = notify_recovery.card_part_resolve_plan(world.led, render["delivery_id"], "body:0001")
    assert "error" not in plan
    guidance = notify_recovery.recovery_guidance("body_part", "discord", _HOLD, plan["attempt_id"])
    assert guidance["requirements"]["mark_not_sent"]["proof_any_of"] == ["api_rejected", "remote_absent"]
    check = notify_recovery.card_part_check(world.led, str(world.data), render["delivery_id"], "body:0001")
    # no journal row survives for the part, so the read-only check never passes
    assert (check["journal"]["state"], check["journal_check"]) == ("absent", "unknown")

    def resolve(proof, n):
        req = {**plan, "command_id": str(uuid.UUID(int=6000 + n)), "actor": "op-user",
               "human_confirmed": True, "reason": "合成の復元後確認", "result": "mark_not_sent",
               "evidence": {"method": "synthetic", "ref": "channel-check", "proof": proof,
                            "worker_stopped": True}}
        return notify_recovery.apply_card_part_resolve(world.led, req, CFG, now=NOW + n)
    # the journal rows that could witness the part's send may be compacted away
    receipt = resolve("no_journal_started", 1)
    assert (receipt["outcome"], receipt["error"]) == ("rejected", "journal_cannot_vouch")
    assert _parts(world, render["delivery_id"])["body:0001"]["state"] == "unknown"
    assert resolve("remote_absent", 2)["outcome"] == "applied"
    assert _parts(world, render["delivery_id"])["body:0001"]["state"] == "not_sent"

def test_inside_the_cap_pending_parts_are_left_to_the_journal(world):
    world.seed()
    world.dispatch()
    render = _deliver_card_only(world)
    before = _parts(world, render["delivery_id"])
    _restore_from(world, 10 * 86400)
    receipt = notify_reconcile.reconcile_after_restore(world.led, CFG, now=NOW)
    assert _parts(world, render["delivery_id"]) == before
    assert receipt["parts_held"] == []


def test_parts_of_a_render_created_after_the_restore_point_are_untouched(world):
    world.seed()
    world.dispatch()                       # created at NOW
    render = _deliver_card_only(world)
    before = _parts(world, render["delivery_id"])
    # the restore point predates the render, so the render is not from the old backup
    _restore_from(world, backup_names.JOURNAL_HORIZON_MAX_S + 86400, restored_at=NOW - 3600)
    receipt = notify_reconcile.reconcile_after_restore(world.led, CFG, now=NOW + 1)
    assert _parts(world, render["delivery_id"]) == before
    assert receipt["parts_held"] == []


# ---------- Discord file groups share their surface's attempt ----------

def _rows(led, delivery_id):
    return {r["part_id"]: dict(r) for r in led.db.execute(
        "SELECT * FROM notification_render_parts WHERE delivery_id=?", (delivery_id,))}


def _groups(spec):
    return [p for p in spec["parts"]["manifest"] if p["kind"] == "file_group"]


def _echo(spec, group):
    by_id = {p["part_id"]: p for p in spec["parts"]["manifest"]}
    return [{"part_id": mid, "wire_name": discord_file_name(by_id[mid]),
             "bytes": by_id[mid]["bytes"], "remote_file_id": f"F-{i}"}
            for i, mid in enumerate(group["members"], start=1)]


def _claim(led, render, spec):
    claim = {"spec": spec, "attempt_id": "c" * 16, "worker_id": "b" * 16,
             "payload_hash": render["payload_hash"]}
    assert notify_transport.apply_transport_begin(
        led, envelopes.transport_begin(claim), TK_CFG, now=NOW)["granted"]
    return claim


def _restore_led(led):
    _restore_from(None, backup_names.JOURNAL_HORIZON_MAX_S + 86400,
                  data=Path(notify_spool.data_root(led)))
    return notify_reconcile.reconcile_after_restore(led, TK_CFG, now=NOW)


def test_discord_body_surface_groups_take_the_surface_attempt(led):
    render, spec = _body_render(led)
    claim = _claim(led, render, spec)
    assert notify_transport.apply_transport_receipt(
        led, envelopes.transport_receipt(claim, "delivered", message_id="card-msg"),
        TK_CFG, now=NOW)["applied"]
    groups = _groups(spec)
    assert len(groups) == 2 and all(g["surface"].startswith("body:") for g in groups)
    receipt = _restore_led(led)
    rows = _rows(led, render["delivery_id"])
    for g in groups:
        aid = envelopes.part_attempt_id(render["delivery_id"], g["surface"])
        for pid in (g["surface"], g["part_id"], *g["members"]):
            assert (rows[pid]["state"], rows[pid]["error_code"], rows[pid]["attempt_id"]) == (
                "unknown", _HOLD, aid), pid
    assert {p["part_id"] for p in receipt["parts_held"]} >= {
        pid for g in groups for pid in (g["part_id"], *g["members"])}
    # a member's plan is the whole sealed group, resolvable through ops.card_part_resolve
    first = groups[0]
    plan = notify_recovery.card_part_resolve_plan(led, render["delivery_id"], first["members"][0])
    assert "error" not in plan, plan
    assert plan["part_id"] == first["part_id"] and plan["expected_state"] == "unknown"
    # a late real receipt for the surface still settles its whole group
    second = groups[1]
    surface = next(p for p in spec["parts"]["manifest"] if p["part_id"] == second["surface"])
    late = notify_transport.apply_part_receipt(led, envelopes.part_receipt(
        claim, surface, "delivered", remote_id="remote-late", files=_echo(spec, second)),
        TK_CFG, now=NOW + 1)
    assert late["applied"], late
    rows = _rows(led, render["delivery_id"])
    for pid in (second["surface"], second["part_id"], *second["members"]):
        assert (rows[pid]["state"], rows[pid]["remote_id"]) == ("delivered", "remote-late"), pid


def test_discord_card_surface_group_takes_the_card_attempt(led, monkeypatch):
    import present_discord
    monkeypatch.setattr(present_discord, "FACE_ORIGINAL_MAX", 3990)   # short originals stay on the face
    _seed_thread(led, mids=(100,))
    _att(led, 100, "f-a", "face-one.pdf", "aa" * 32, 11)
    _att(led, 100, "f-b", "face-two.pdf", "bb" * 32, 22)
    assert _dispatch(led, _intent(led, payload={"message_ids": [100]}))["dispatched"]
    render = dict(_latest_render(led))
    spec = json.loads(render["spec_json"])
    (group,) = _groups(spec)
    assert group["surface"] == "card"
    claim = _claim(led, render, spec)
    assert notify_transport.apply_transport_receipt(
        led, envelopes.transport_receipt(claim, "delivered", message_id="card-msg",
                                         files=_echo(spec, group)), TK_CFG, now=NOW)["applied"]
    # Model a backup that kept the card's receipt but not its group fan-out.
    with led.db:
        led.db.execute("UPDATE notification_render_parts SET state='pending',remote_id=NULL,"
                       "remote_file_id=NULL,attempt_id=NULL WHERE delivery_id=? AND part_id IN (%s)"
                       % ",".join("?" * (1 + len(group["members"]))),
                       (render["delivery_id"], group["part_id"], *group["members"]))
    _restore_led(led)
    rows = _rows(led, render["delivery_id"])
    assert rows["card"]["attempt_id"] == "c" * 16
    for pid in (group["part_id"], *group["members"]):
        assert (rows[pid]["state"], rows[pid]["error_code"], rows[pid]["attempt_id"]) == (
            "unknown", _HOLD, "c" * 16), pid
    plan = notify_recovery.card_part_resolve_plan(led, render["delivery_id"], group["members"][0])
    assert "error" not in plan, plan
    assert plan["part_id"] == group["part_id"]


def test_slack_unstarted_file_group_stays_pending_but_is_reported_beyond_horizon(led, tmp_path, monkeypatch):
    from test_file_group_dependency_refusal import _refused
    from adapters.common import journal
    slack, render, _spec, _stage, group = _refused(led, tmp_path, monkeypatch)
    aid = envelopes.part_attempt_id(render["delivery_id"], group["part_id"])
    # a legacy pending group whose journal evidence is gone
    journal.compact(slack.dirs["state"], active="", file_ok=lambda rows: True,
                    prunable=lambda candidate, rows: candidate == aid)
    with led.db:
        led.db.execute("UPDATE notification_render_parts SET state='pending',error_code=NULL,"
                       "attempt_id=NULL WHERE delivery_id=? AND part_id=?",
                       (render["delivery_id"], group["part_id"]))
    assert led.db.execute("SELECT state FROM notification_renders WHERE delivery_id=?",
                          (render["delivery_id"],)).fetchone()["state"] == "delivered"
    _restore_from(None, backup_names.JOURNAL_HORIZON_MAX_S + 86400,
                  data=Path(notify_spool.data_root(led)))
    receipt = notify_reconcile.reconcile_after_restore(led, SLACK, now=NOW)
    row = _rows(led, render["delivery_id"])[group["part_id"]]
    # #136: never started is not proven by a journal that may be compacted, so the
    # group keeps its pending state (not asserted unsent) yet is named and flagged
    assert (row["state"], row["attempt_id"], row["error_code"]) == ("pending", None, _HOLD)
    listed = [p for p in receipt["parts_held"] if p["part_id"] == group["part_id"]]
    assert [(p["delivery_id"], p["reason"]) for p in listed] == [(render["delivery_id"], _HOLD)]
    assert receipt["parts_unresolvable"] == []
    plan = notify_recovery.card_part_resolve_plan(led, render["delivery_id"], group["part_id"])
    assert plan["expected_state"] == "pending"
    worklist = notify_recovery.recovery_worklist(led.db)
    # the refused stage (attach:0001) stays unknown and listed in its own right
    assert [(i["part_id"], i["state"], i["status"]) for i in worklist["items"]
            if i["delivery_id"] == render["delivery_id"] and i["part_id"] == group["part_id"]
            ] == [(group["part_id"], "pending", "plannable")]

    def notices():
        return led.db.execute("SELECT payload FROM notify_outbox WHERE kind='update_notice'").fetchall()
    assert len(notices()) == 1 and "part unknown=1件" in notices()[0]["payload"]
    # a re-run (the marker stays while the journal is incomplete) reports the part
    # again through parts_held but flags nothing new, so no second notice follows
    assert notify_reconcile._unknown_beyond_horizon_parts(
        led.db, NOW, NOW + 1, notify_spool.notify_dirs(notify_spool.data_root(led))) == []
    assert [p["part_id"] for p in notify_reconcile._beyond_horizon_parts(led.db)[0]] == [group["part_id"]]
    assert len(notices()) == 1
    # the health-read hold report counts it by cause (state stays pending)
    assert notify_recovery.part_hold_report(led)["parts"]["pending"][_HOLD] == 1


# ---------- Discord group parts that cannot share a verified surface attempt ----------

def _delivered_body_render(led):
    render, spec = _body_render(led)
    claim = _claim(led, render, spec)
    assert notify_transport.apply_transport_receipt(
        led, envelopes.transport_receipt(claim, "delivered", message_id="card-msg"),
        TK_CFG, now=NOW)["applied"]
    return render, spec


def _assert_reported(led, receipt, render, part_ids, reason):
    rows = _rows(led, render["delivery_id"])
    for pid in part_ids:
        assert (rows[pid]["state"], rows[pid]["error_code"], rows[pid]["attempt_id"]) == (
            "unknown", reason, None), pid
        # never planned: no attempt id to settle and no evidence was invented
        assert "error" in notify_recovery.card_part_resolve_plan(led, render["delivery_id"], pid)
    listed = {p["part_id"]: p["reason"] for p in receipt["parts_unresolvable"]}
    assert all(listed[pid] == reason for pid in part_ids)
    assert not set(part_ids) & {p["part_id"] for p in receipt["parts_held"]}
    assert reason in notify_recovery.PART_HOLD_REASONS
    report = notify_recovery.part_hold_report(led)
    assert report["parts"]["unknown"][reason] == len(part_ids)
    notice = led.db.execute("SELECT payload FROM notify_outbox WHERE kind='update_notice'").fetchone()
    assert f"part 要調査={len(receipt['parts_unresolvable'])}件" in notice["payload"]
    assert "個別調査が必要" in notice["payload"]
    root = Path(notify_spool.data_root(led))
    spec_name = render["delivery_id"] + ".json"
    assert (root / "restore_specs" / spec_name).is_file()
    assert not (root / "discord_render" / spec_name).exists()
    assert notify_spool.publish_specs(led.db, notify_spool.notify_dirs(str(root)),
                                      [json.loads(render["spec_json"])], NOW + 1) == []


def test_unbound_group_spec_is_reported_not_left_pending(led):
    render, spec = _delivered_body_render(led)
    groups = _groups(spec)
    # a non-string surface must neither crash reconcile nor bind a sqlite parameter
    tampered = json.loads(render["spec_json"])
    next(p for p in tampered["parts"]["manifest"] if p["kind"] == "file_group")["surface"] = {"x": 1}
    with led.db:
        led.db.execute("UPDATE notification_renders SET spec_json=? WHERE delivery_id=?",
                       (json.dumps(tampered), render["delivery_id"]))
    receipt = _restore_led(led)
    grouped = [pid for g in groups for pid in (g["part_id"], *g["members"])]
    _assert_reported(led, receipt, render, grouped, notify_reconcile.GROUP_SPEC_UNVERIFIED)
    # the surfaces themselves stay operator-resolvable with their own attempt
    for g in groups:
        assert _rows(led, render["delivery_id"])[g["surface"]]["attempt_id"] == \
            envelopes.part_attempt_id(render["delivery_id"], g["surface"])


def test_group_spec_with_a_changed_payload_hash_is_reported(led):
    render, spec = _delivered_body_render(led)
    groups = _groups(spec)
    tampered = json.loads(render["spec_json"])
    tampered["parts"]["manifest"][0]["name"] = "synthetic-other"     # sealed hash no longer matches
    with led.db:
        led.db.execute("UPDATE notification_renders SET spec_json=? WHERE delivery_id=?",
                       (json.dumps(tampered), render["delivery_id"]))
    receipt = _restore_led(led)
    _assert_reported(led, receipt, render, [pid for g in groups for pid in (g["part_id"], *g["members"])],
                     notify_reconcile.GROUP_SPEC_UNVERIFIED)


def test_missing_or_unsettled_surface_is_reported_and_rerun_announces_once(led):
    render, spec = _delivered_body_render(led)
    first, second = _groups(spec)
    with led.db:
        led.db.execute("DELETE FROM notification_render_parts WHERE delivery_id=? AND part_id=?",
                       (render["delivery_id"], first["surface"]))
        led.db.execute("UPDATE notification_render_parts SET state='held' WHERE delivery_id=? AND part_id=?",
                       (render["delivery_id"], second["surface"]))
    (Path(notify_spool.data_root(led)) / "discord_state").mkdir(parents=True, exist_ok=True)
    (Path(notify_spool.data_root(led)) / "discord_state" / "journal-w1.jsonl").write_text("{broken\n")
    receipt = _restore_led(led)
    _assert_reported(led, receipt, render, [first["part_id"], *first["members"]],
                     notify_reconcile.GROUP_SURFACE_MISSING)
    _assert_reported(led, receipt, render, [second["part_id"], *second["members"]],
                     notify_reconcile.GROUP_SURFACE_UNRESOLVABLE)
    again = notify_reconcile.reconcile_after_restore(led, TK_CFG, now=NOW + 1)
    assert again["parts_unresolvable"] == receipt["parts_unresolvable"]
    assert led.db.execute("SELECT COUNT(*) FROM notify_outbox WHERE kind='update_notice'").fetchone()[0] == 1


def test_ungrouped_discord_parts_do_not_need_a_manifest(world):
    world.seed()
    world.dispatch()
    render = _deliver_card_only(world)
    with world.led.db:
        world.led.db.execute("UPDATE notification_renders SET spec_json='{' WHERE delivery_id=?",
                             (render["delivery_id"],))
    _restore_from(world, backup_names.JOURNAL_HORIZON_MAX_S + 86400)
    receipt = notify_reconcile.reconcile_after_restore(world.led, CFG, now=NOW)
    assert receipt["parts_unresolvable"] == [] and len(receipt["parts_held"]) == 3


def _held_surface_restore(led, *, keep_marker=False):
    render, spec = _delivered_body_render(led)
    _first, second = _groups(spec)
    with led.db:
        led.db.execute("UPDATE notification_render_parts SET state='held' WHERE delivery_id=? AND part_id=?",
                       (render["delivery_id"], second["surface"]))
    if keep_marker:
        (Path(notify_spool.data_root(led)) / "discord_state").mkdir(parents=True, exist_ok=True)
        (Path(notify_spool.data_root(led)) / "discord_state" / "journal-w1.jsonl").write_text("{broken\n")
    receipt = _restore_led(led)
    group_parts = [second["part_id"], *second["members"]]
    _assert_reported(led, receipt, render, group_parts, notify_reconcile.GROUP_SURFACE_UNRESOLVABLE)
    return render, spec, second, group_parts


@pytest.mark.parametrize("files", [True, False])
def test_unresolvable_group_takes_its_surface_attempt_once_the_surface_settles(led, files):
    render, spec, group, parts = _held_surface_restore(led)
    claim = {"spec": spec, "attempt_id": "c" * 16, "worker_id": "b" * 16,
             "payload_hash": render["payload_hash"]}
    surface = next(p for p in spec["parts"]["manifest"] if p["part_id"] == group["surface"])
    late = notify_transport.apply_part_receipt(led, envelopes.part_receipt(
        claim, surface, "delivered", remote_id="remote-late",
        files=_echo(spec, group) if files else None), TK_CFG, now=NOW + 1)
    assert late["applied"], late
    rows = _rows(led, render["delivery_id"])
    aid = rows[group["surface"]]["attempt_id"]
    assert aid == envelopes.part_attempt_id(render["delivery_id"], group["surface"])
    if files:
        # the surface's own receipt settles the promoted group, as a reconcile-time attempt would
        for pid in parts:
            assert (rows[pid]["state"], rows[pid]["remote_id"], rows[pid]["attempt_id"]) == (
                "delivered", "remote-late", aid), pid
    else:
        for pid in parts:
            assert (rows[pid]["state"], rows[pid]["error_code"], rows[pid]["attempt_id"]) == (
                "unknown", notify_reconcile.BEYOND_HORIZON, aid), pid
        plan = notify_recovery.card_part_resolve_plan(led, render["delivery_id"], group["members"][0])
        assert "error" not in plan, plan
        assert plan["part_id"] == group["part_id"] and plan["attempt_id"] == aid
    with led.db:
        assert notify_reconcile.promote_group_parts(led.db, NOW + 2) == []     # idempotent
    assert _rows(led, render["delivery_id"]) == rows


def test_reconcile_rerun_promotes_a_group_whose_surface_became_usable(led):
    render, _spec, group, parts = _held_surface_restore(led, keep_marker=True)
    aid = envelopes.part_attempt_id(render["delivery_id"], group["surface"])
    with led.db:   # the surface settled through a path that predates the promotion
        led.db.execute("UPDATE notification_render_parts SET state='delivered',remote_id='remote-late',"
                       "attempt_id=? WHERE delivery_id=? AND part_id=?",
                       (aid, render["delivery_id"], group["surface"]))
    again = notify_reconcile.reconcile_after_restore(led, TK_CFG, now=NOW + 1)
    held = {p["part_id"]: p for p in again["parts_held"]}
    assert all(held[pid]["reason"] == notify_reconcile.BEYOND_HORIZON and held[pid]["attempt_id"] == aid
               for pid in parts)
    assert not set(parts) & {p["part_id"] for p in again["parts_unresolvable"]}
    assert "error" not in notify_recovery.card_part_resolve_plan(led, render["delivery_id"], group["part_id"])
    third = notify_reconcile.reconcile_after_restore(led, TK_CFG, now=NOW + 2)
    assert third["parts_held"] == again["parts_held"]
    assert third["parts_unresolvable"] == again["parts_unresolvable"]
    assert led.db.execute("SELECT COUNT(*) FROM notify_outbox WHERE kind='update_notice'").fetchone()[0] == 1


def _held_attempt_row(world, render):
    from test_notify_reconcile import _attempt_row
    aid = envelopes.part_attempt_id(render["delivery_id"], "synthetic-lost")
    _attempt_row(world.led, aid, render["delivery_id"])
    return aid


def test_horizon_attempt_row_hold_rejects_a_journal_only_absence_proof(world):
    world.seed()
    world.dispatch()
    render = world.render()
    aid = _held_attempt_row(world, render)
    _restore_from(world, backup_names.JOURNAL_HORIZON_MAX_S + 86400)
    notify_reconcile.reconcile_after_restore(world.led, CFG, now=NOW)
    hold = next(h for h in world.holds() if h["attempt_id"] == aid)
    assert hold["reason"] == notify_recovery.UNKNOWN_ATTEMPT_BEYOND_HORIZON
    receipt = _resolve(world, render["delivery_id"], "mark_not_sent", aid,
                       evidence={"method": "synthetic", "ref": "journal",
                                 "proof": "no_journal_started", "worker_stopped": True})
    assert (receipt["outcome"], receipt["error"]) == ("rejected", "journal_cannot_vouch")


def test_attempt_row_hold_inside_the_cap_keeps_the_journal_proof(world):
    world.seed()
    world.dispatch()
    render = world.render()
    aid = _held_attempt_row(world, render)
    _restore_from(world, 10 * 86400)
    notify_reconcile.reconcile_after_restore(world.led, CFG, now=NOW)
    assert next(h for h in world.holds() if h["attempt_id"] == aid)["reason"] == "unknown_attempt"
    receipt = _resolve(world, render["delivery_id"], "mark_not_sent", aid,
                       evidence={"method": "synthetic", "ref": "journal",
                                 "proof": "no_journal_started", "worker_stopped": True})
    assert receipt.get("error") != "journal_cannot_vouch"


def test_rerun_receipt_still_lists_the_open_holds(world):
    world.seed()
    world.dispatch()
    render = world.render()
    _restore_from(world, backup_names.JOURNAL_HORIZON_MAX_S + 86400)
    first = notify_reconcile.reconcile_after_restore(world.led, CFG, now=NOW)
    assert render["delivery_id"] in {h["delivery_id"] for h in first["held"]}
    # a rerun holds nothing new (renders are already held) yet keeps the list
    again = notify_reconcile.reconcile_after_restore(world.led, CFG, now=NOW)
    assert again["held"] == first["held"]
    assert again["events_held"] == first["events_held"]


def _compacted_pending_group(led, tmp_path, monkeypatch, *, error_code=None):
    """A pre-restore Slack render whose file_group never started: the stage upload
    is human-confirmed delivered, and the journal rows of the stage and the group
    are gone, as a restore past the journal horizon leaves them. Rows that
    survived would make reconcile settle the group from the journal (not_sent) or
    hold the card over the stage's recorded failure, so neither would stay pending."""
    from test_file_group_dependency_refusal import _confirm_stage, _refused
    from adapters.common import journal
    slack, render, spec, stage, group = _refused(led, tmp_path, monkeypatch)
    _confirm_stage(led, render, stage)
    parts = {stage["part_id"], group["part_id"]}
    journal.compact(slack.dirs["state"], active="", file_ok=lambda rows: True,
                    prunable=lambda candidate, rows: any(r.get("part_id") in parts for r in rows))
    slack.worker._jview.invalidate()
    with led.db:
        led.db.execute("UPDATE notification_render_parts SET state='pending',error_code=?,"
                       "attempt_id=NULL WHERE delivery_id=? AND part_id=?",
                       (error_code, render["delivery_id"], group["part_id"]))
    return slack, render, spec, group


@pytest.mark.parametrize("error_code", [None, "stage_receipt_missing"])
def test_slack_unstarted_file_group_beyond_horizon_refuses_a_journal_only_proof(
        led, tmp_path, monkeypatch, error_code):
    """#136: a pending Slack file_group of a pre-restore render must not be
    resolved as unsent from journal rows that may be compacted away."""
    slack, render, spec, group = _compacted_pending_group(led, tmp_path, monkeypatch, error_code=error_code)
    data = Path(notify_spool.data_root(led))
    age = backup_names.JOURNAL_HORIZON_MAX_S + 86400
    published_spec = Path(slack.dirs["render"], render["delivery_id"] + ".json")
    assert published_spec.is_file()
    _restore_from(None, age, data=data)
    receipt = notify_reconcile.reconcile_after_restore(led, SLACK, now=NOW)
    assert [(p["part_id"], p["reason"]) for p in receipt["parts_held"]] == [(group["part_id"], _HOLD)]
    _restore_from(None, age, data=data)
    again = notify_reconcile.reconcile_after_restore(led, SLACK, now=NOW + 1)
    assert again["parts_held"] == receipt["parts_held"]
    assert led.db.execute("SELECT COUNT(*) FROM notify_outbox WHERE kind='update_notice'").fetchone()[0] == 1
    worklist = notify_recovery.recovery_worklist(led.db)
    assert [(item["part_id"], item["status"]) for item in worklist["items"]] == [(group["part_id"], "plannable")]
    assert notify_recovery.part_hold_report(led)["parts"]["pending"][_HOLD] == 1
    assert notify_spool.restore_pending(str(data)) is None
    for _ in range(2):
        assert notify_spool.recover(led, SLACK, {})["republished"] == 0
        assert not published_spec.exists()
    assert notify_spool.publish_specs(led.db, notify_spool.notify_dirs(str(data)), [spec], NOW + 1) == []
    assert not published_spec.exists()
    assert led.db.execute(
        "SELECT spec_published,spec_json FROM notification_renders WHERE delivery_id=?",
        (render["delivery_id"],),
    ).fetchone()[:] == (0, render["spec_json"])
    calls = list(slack.client.calls)
    asyncio.run(slack.worker._resume_parts(spec))
    assert slack.client.calls == calls
    row = _rows(led, render["delivery_id"])[group["part_id"]]
    assert (row["state"], row["attempt_id"], row["error_code"]) == ("pending", None, _HOLD)
    plan = notify_recovery.card_part_resolve_plan(led, render["delivery_id"], group["part_id"])

    def resolve(proof, n):
        req = dict(plan, command_id=str(uuid.UUID(int=7000 + n)), actor="op-user",
                   human_confirmed=True, reason="合成の復元後確認", result="mark_not_sent",
                   evidence=dict(method="synthetic", ref="thread-check", proof=proof,
                                 worker_stopped=True))
        return notify_recovery.apply_card_part_resolve(led, req, SLACK, now=NOW + n)
    receipt = resolve("no_journal_started", 1)
    assert (receipt["outcome"], receipt["error"]) == ("rejected", "journal_cannot_vouch")
    assert _rows(led, render["delivery_id"])[group["part_id"]]["state"] == "pending"
    # a journal-independent proof still resolves it (it is not left blocking updates)
    receipt = resolve("remote_absent", 2)
    assert receipt["outcome"] == "applied", receipt
    assert _rows(led, render["delivery_id"])[group["part_id"]]["state"] == "not_sent"
    assert led.db.execute("SELECT COUNT(*) FROM notification_renders").fetchone()[0] == 1
    assert slack.client.calls == calls


def test_slack_recovery_cannot_republish_with_another_pending_part(led, tmp_path, monkeypatch):
    slack, render, _spec, group = _compacted_pending_group(led, tmp_path, monkeypatch)
    data = Path(notify_spool.data_root(led))
    _restore_from(None, backup_names.JOURNAL_HORIZON_MAX_S + 86400, data=data)
    notify_reconcile.reconcile_after_restore(led, SLACK, now=NOW)
    with led.db:
        led.db.execute(
            "INSERT INTO notification_render_parts (delivery_id,part_id,idx,kind,state,updated_at) "
            "VALUES (?,?,?,?,?,?)", (render["delivery_id"], "body:9999", 9999, "body_part", "pending", NOW),
        )
    assert _rows(led, render["delivery_id"])[group["part_id"]]["error_code"] == _HOLD
    assert notify_spool.recover(led, SLACK, {})["republished"] == 0
    assert not Path(slack.dirs["render"], render["delivery_id"] + ".json").exists()


def test_slack_restore_keeps_gate_closed_if_the_worker_spec_cannot_be_withdrawn(led, tmp_path, monkeypatch):
    slack, render, _spec, group = _compacted_pending_group(led, tmp_path, monkeypatch)
    data = Path(notify_spool.data_root(led))
    _restore_from(None, backup_names.JOURNAL_HORIZON_MAX_S + 86400, data=data)
    published_spec = Path(slack.dirs["render"], render["delivery_id"] + ".json")
    replace = os.replace

    def deny_spec_withdrawal(src, dst):
        if os.fspath(src) == str(published_spec):
            raise PermissionError("synthetic refusal")
        return replace(src, dst)

    with monkeypatch.context() as patch:
        patch.setattr(notify_reconcile.os, "replace", deny_spec_withdrawal)
        with pytest.raises(OSError, match="restore_part_spec_withdraw_failed"):
            notify_reconcile.reconcile_after_restore(led, SLACK, now=NOW)
    assert published_spec.is_file()
    assert notify_spool.restore_pending(str(data)) is not None
    receipt = notify_reconcile.reconcile_after_restore(led, SLACK, now=NOW + 1)
    assert not published_spec.exists()
    assert notify_spool.restore_pending(str(data)) is None
    assert [part["part_id"] for part in receipt["parts_held"]] == [group["part_id"]]


@pytest.mark.parametrize("beyond,after_restore", [(False, False), (True, True)])
def test_slack_pending_group_outside_restore_scope_is_unflagged(
        led, tmp_path, monkeypatch, beyond, after_restore):
    slack, render, _spec, group = _compacted_pending_group(led, tmp_path, monkeypatch)
    age = backup_names.JOURNAL_HORIZON_MAX_S + 86400 if beyond else 86400
    restored_at = render["created_at"] - 1 if after_restore else NOW
    _restore_from(None, age, restored_at=restored_at, data=Path(notify_spool.data_root(led)))
    receipt = notify_reconcile.reconcile_after_restore(led, SLACK, now=NOW)
    assert not receipt["parts_held"]
    row = _rows(led, render["delivery_id"])[group["part_id"]]
    assert (row["state"], row["error_code"]) == ("pending", None)
    published_spec = Path(slack.dirs["render"], render["delivery_id"] + ".json")
    published_spec.unlink()
    assert notify_spool.recover(led, SLACK, {})["republished"] == 1
    assert published_spec.read_text() == render["spec_json"]
    assert notify_spool.recover(led, SLACK, {})["republished"] == 0


def test_slack_pending_group_on_superseded_render_is_unflagged(led, tmp_path, monkeypatch):
    _slack, render, _spec, group = _compacted_pending_group(led, tmp_path, monkeypatch)
    with led.db:
        led.db.execute(
            "INSERT INTO notification_renders "
            "(delivery_id,card_id,op,render_rev,route_epoch,transport,"
            "payload_hash,correlation,state,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.UUID(int=7199)), render["card_id"], "update", render["render_rev"] + 1,
             render["route_epoch"], "slack", render["payload_hash"], "synthetic-successor",
             "cancelled", NOW, NOW),
        )
    _restore_from(None, backup_names.JOURNAL_HORIZON_MAX_S + 86400,
                  data=Path(notify_spool.data_root(led)))
    receipt = notify_reconcile.reconcile_after_restore(led, SLACK, now=NOW)
    assert not receipt["parts_held"]
    assert _rows(led, render["delivery_id"])[group["part_id"]]["error_code"] is None


def test_slack_unstarted_file_group_beyond_horizon_resolves_as_delivered_when_observed(led, tmp_path, monkeypatch):
    """#136: a posted file set the operator observed still settles a flagged
    pending group (exact card id and file set re-checked, human approval kept)."""
    slack, render, _spec, group = _compacted_pending_group(led, tmp_path, monkeypatch)
    _restore_from(None, backup_names.JOURNAL_HORIZON_MAX_S + 86400,
                  data=Path(notify_spool.data_root(led)))
    notify_reconcile.reconcile_after_restore(led, SLACK, now=NOW)
    plan = notify_recovery.card_part_resolve_plan(led, render["delivery_id"], group["part_id"])
    card = led.db.execute("SELECT message_id FROM notification_cards WHERE card_id=?",
                          (render["card_id"],)).fetchone()
    req = dict(plan, command_id=str(uuid.UUID(int=7100)), actor="op-user", human_confirmed=True,
               reason="合成の復元後確認", result="mark_delivered", remote_id=card["message_id"],
               evidence=dict(method="synthetic", ref="thread-check", worker_stopped=True))
    receipt = notify_recovery.apply_card_part_resolve(led, req, SLACK, now=NOW + 1)
    assert receipt["outcome"] == "applied", receipt
    assert _rows(led, render["delivery_id"])[group["part_id"]]["state"] == "delivered"

    assert led.db.execute("SELECT COUNT(*) FROM notification_renders").fetchone()[0] == 1
    assert not any(kind == "update" and "file_ids" in fields for kind, fields in slack.client.calls)


def test_beyond_horizon_part_keeps_recover_from_reissuing_the_whole_spec(world):
    world.seed()
    world.dispatch()
    render = _deliver_card_only(world)
    _restore_from(world, backup_names.JOURNAL_HORIZON_MAX_S + 86400)
    notify_reconcile.reconcile_after_restore(world.led, CFG, now=NOW)
    parts = _parts(world, render["delivery_id"])
    assert parts["body:0001"]["error_code"] == notify_reconcile.BEYOND_HORIZON
    other = next(pid for pid, row in parts.items() if pid not in ("body:0001", "card"))
    with world.led.db:     # another part of the same render is still pending
        world.led.db.execute("UPDATE notification_render_parts SET state='pending',error_code=NULL "
                             "WHERE delivery_id=? AND part_id=?", (render["delivery_id"], other))
    root = notify_spool.data_root(world.led)
    spec = Path(notify_spool.notify_dirs(root)["discord_render"]) / (render["delivery_id"] + ".json")
    spec.unlink(missing_ok=True)
    # the compacted journal cannot show the marked part never ran: a replay would resend it
    assert notify_spool.recover(world.led, CFG, {})["republished"] == 0
    assert not spec.exists()
    with world.led.db:
        # every beyond-horizon mark on the delivery is settled before the
        # spec may be replayed — clearing only one part still blocks it
        world.led.db.execute("UPDATE notification_render_parts SET error_code='synthetic' "
                             "WHERE delivery_id=? AND error_code=?",
                             (render["delivery_id"], notify_reconcile.BEYOND_HORIZON))
    assert notify_spool.recover(world.led, CFG, {})["republished"] == 1
