"""O5: a restore from a backup older than the journal's capped horizon holds every delivery."""
from __future__ import annotations

import os
import uuid

import pytest

import backup_names
import notify_reconcile
import notify_recovery
import notify_spool
from notify_reconcile_testkit import CFG, NOW, world
from notify_testkit import pinned_clock

__all__ = ["pinned_clock", "world"]
pytestmark = pytest.mark.usefixtures("pinned_clock", "v1_thread_bodies")


def _restore_from(world, age_s, restored_at=NOW):
    backup = world.data / "backups" / "preupdate-20260101-000000-x.db"
    backup.parent.mkdir(exist_ok=True)
    backup.write_bytes(b"synthetic")
    os.utime(backup, (restored_at - age_s, restored_at - age_s))
    notify_spool.mark_restored(str(world.data), backup_path=str(backup),
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
