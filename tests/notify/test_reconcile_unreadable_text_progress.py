"""A text outbox row whose stored progress cannot be read (too deeply
nested, or not an object) is skipped by the restore reconcile like any
other unreadable progress: the reconcile completes and clears its
marker, the row keeps its state and progress, and a readable
restore_text_unverified row is still reported. Synthetic ledger only."""
import json

import pytest

import notify_cards
import notify_reconcile
from test_notify_reconcile import CFG, world

__all__ = ["world"]


def _text_row(db, progress):
    return db.execute(
        "INSERT INTO notify_outbox(kind,project_id,payload,state,route,"
        "progress,created_at,updated_at) "
        "VALUES('new_messages',1,'{}','failed','text',?,1,1)",
        (progress,)).lastrowid


@pytest.mark.parametrize("progress", ["[" * 100000, "[1]", '"text"'],
                         ids=["deep", "list", "string"])
def test_unreadable_text_progress_never_wedges_the_reconcile(world, progress):
    world.seed()
    db = world.led.db
    bad = _text_row(db, progress)
    held = _text_row(db, json.dumps({"hold_reason": "restore_text_unverified"}))
    db.commit()
    notify_cards.mark_restored(str(world.data))
    receipt = notify_reconcile.reconcile_after_restore(world.led, CFG)
    assert notify_cards.restore_pending(str(world.data)) is None
    assert [h["event_id"] for h in receipt["text_held"]] == [held]
    row = db.execute("SELECT state,next_try,progress FROM notify_outbox "
                     "WHERE event_id=?", (bad,)).fetchone()
    assert (row["state"], row["next_try"], row["progress"]) == ("failed", None, progress)
