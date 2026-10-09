"""An operator rebind of a restore hold whose render row the restore
erased never revives a revoked card: it stays revoked for both results,
a proven delivery only binds the message id the revoke must delete, and
returned events never post the card again. Synthetic ledger and journal."""
import uuid

import pytest

import notify_cards
import notify_reconcile
import notify_transport
from test_notify_reconcile import CFG, NOW, _journal_claim, _journal_result, world

__all__ = ["world"]


def _lost_render_hold(world):
    world.seed()
    world.dispatch()
    render = world.render()
    spec = world.spec(render["delivery_id"])
    aid = _journal_claim(str(world.data / "discord_state"), "w1", spec)
    _journal_result(str(world.data / "discord_state"), "w1", aid, render["delivery_id"], "unknown")
    notify_cards.mark_restored(str(world.data))
    notify_reconcile.reconcile_after_restore(world.led, CFG)
    with world.led.db:
        notify_cards.revoke_card(world.led.db, render["card_id"], NOW)
    db = world.led.db
    # the restore rewound to before the render/attempt existed; hold and card remain
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("DELETE FROM notification_delivery_attempts WHERE delivery_id=?", (render["delivery_id"],))
    db.execute("DELETE FROM notification_renders WHERE delivery_id=?", (render["delivery_id"],))
    db.commit()
    db.execute("PRAGMA foreign_keys=ON")
    return render, aid


def _resolve(world, render, aid, result):
    return notify_transport.apply_card_resolve(world.led, {
        "version": 1, "cmd": "ops.card_resolve", "command_id": str(uuid.uuid4()),
        "actor": "op-user", "human_confirmed": True, "reason": "合成の配送結果確認",
        "delivery_id": render["delivery_id"], "attempt_id": aid, "result": result,
        **CFG["notify"]["discord"], "message_id": "synthetic-rebound",
        "evidence": {"method": "synthetic", "ref": "synthetic-card",
                     "worker_stopped": True, "proof": "remote_absent"}}, CFG)


@pytest.mark.parametrize("result", ["mark_delivered", "mark_not_sent"])
def test_a_lost_render_rebind_never_revives_a_revoked_card(world, result):
    render, aid = _lost_render_hold(world)
    out = _resolve(world, render, aid, result)
    assert out["outcome"] == "applied" and out["rebound"] is True
    card = world.card(render["card_id"])
    assert card["delivery_state"] == "revoked"
    assert card["message_id"] == ("synthetic-rebound" if result == "mark_delivered" else None)
    assert world.led.db.execute("SELECT count(*) FROM notification_restore_holds "
                                "WHERE released_at IS NULL").fetchone()[0] == 0


def test_returned_events_never_post_the_revoked_card_again(world):
    render, aid = _lost_render_hold(world)
    _resolve(world, render, aid, "mark_not_sent")
    for row in world.led.db.execute("SELECT * FROM notify_outbox WHERE state='pending'").fetchall():
        notify_cards.dispatch_intent(world.led, dict(row), CFG)
    assert world.card(render["card_id"])["delivery_state"] == "revoked"
    assert world.led.db.execute(
        "SELECT count(*) FROM notification_renders WHERE card_id=? AND state='queued' "
        "AND op!='revoke'", (render["card_id"],)).fetchone()[0] == 0


def test_a_live_card_still_rebinds_as_before(world):
    world.seed()
    world.dispatch()
    render = world.render()
    spec = world.spec(render["delivery_id"])
    aid = _journal_claim(str(world.data / "discord_state"), "w1", spec)
    _journal_result(str(world.data / "discord_state"), "w1", aid, render["delivery_id"], "unknown")
    notify_cards.mark_restored(str(world.data))
    notify_reconcile.reconcile_after_restore(world.led, CFG)
    db = world.led.db
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("DELETE FROM notification_delivery_attempts WHERE delivery_id=?", (render["delivery_id"],))
    db.execute("DELETE FROM notification_renders WHERE delivery_id=?", (render["delivery_id"],))
    db.commit()
    db.execute("PRAGMA foreign_keys=ON")
    assert _resolve(world, render, aid, "mark_delivered")["outcome"] == "applied"
    card = world.card(render["card_id"])
    assert (card["delivery_state"], card["message_id"]) == ("delivered", "synthetic-rebound")


def test_oversized_scope_integer_is_rejected_without_releasing_hold(world):
    render, aid = _lost_render_hold(world)
    with world.led.db:
        world.led.db.execute("UPDATE notification_restore_holds SET scope_json=?",
                             ('{"synthetic":' + '1' * 5000 + '}',))
    before = dict(world.card(render["card_id"]))
    events = world.led.db.execute(
        "SELECT event_id,state,next_try FROM notify_outbox ORDER BY event_id").fetchall()
    out = _resolve(world, render, aid, "mark_not_sent")
    assert out["outcome"] == "rejected" and out["error"] == "hold_scope_corrupt"
    assert dict(world.card(render["card_id"])) == before
    assert world.led.db.execute(
        "SELECT event_id,state,next_try FROM notify_outbox ORDER BY event_id").fetchall() == events
    assert world.led.db.execute(
        "SELECT count(*) FROM notification_restore_holds WHERE released_at IS NULL").fetchone()[0] == 1
