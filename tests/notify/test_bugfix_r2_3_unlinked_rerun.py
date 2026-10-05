"""Regression: a reconcile rerun must not mass-hold intents created after the restore."""
import json

from test_notify_reconcile import (  # noqa: F401
    CFG, _journal_claim, _journal_result, notify_cards, notify_reconcile,
    world)


def test_rerun_keeps_post_restore_interactive_event_pending(world):  # noqa: F811
    world.seed()
    world.dispatch()
    render = world.render()
    spec = world.spec(render["delivery_id"])
    state = str(world.data / "discord_state")
    aid = _journal_claim(state, "w1", spec)
    _journal_result(state, "w1", aid, render["delivery_id"], "delivered",
                    message_id="999001")
    (world.data / "discord_render" / (render["delivery_id"] + ".json")).unlink()
    (world.data / "discord_state" / "journal-w2.jsonl").write_text("{broken\n")
    notify_cards.mark_restored(str(world.data))
    marker = notify_cards.restore_pending(str(world.data))
    notify_reconcile.reconcile_after_restore(world.led, CFG)
    assert notify_cards.restore_pending(str(world.data)) is not None
    eid = world.led.outbox_add("new_messages", 1, {"message_ids": [101]})
    world.led.db.execute("UPDATE notify_outbox SET created_at=? WHERE event_id=?",
                         (marker["restored_at"] + 100, eid))
    world.led.db.commit()
    notify_reconcile.reconcile_after_restore(world.led, CFG)
    row = world.led.db.execute(
        "SELECT state,next_try,progress FROM notify_outbox WHERE event_id=?",
        (eid,)).fetchone()
    assert row["state"] == "pending" and row["next_try"] is not None
    assert json.loads(row["progress"] or "{}").get("hold_reason") is None
