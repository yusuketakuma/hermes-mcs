"""_hold_event が受領記録を保ったまま保留理由を記録し、救済と保留を1回の確定で行うことを確かめる。"""
import json

import pytest

import notify_flush
from common_testkit import ledger_fixture


store = ledger_fixture(name="store")


def _row(store, eid):
    return store.db.execute("SELECT * FROM notify_outbox WHERE event_id=?", (eid,)).fetchone()


@pytest.mark.parametrize("progress,expected", [
    (json.dumps({"next": 1, "sent": ["m1"]}), {"next": 1, "sent": ["m1"]}),
    ("not-json", {"invalid_progress": "not-json"}),
    ("[1]", {"invalid_progress": "[1]"}),
])
def test_hold_keeps_the_receipt_and_records_the_reason(store, progress, expected):
    eid = store.outbox_add("new_messages", 1, {"message_ids": [1]})
    store.db.execute("UPDATE notify_outbox SET progress=? WHERE event_id=?", (progress, eid))
    store.db.commit()
    notify_flush._hold_event(store, _row(store, eid), {}, reason="synthetic_reason")
    row = _row(store, eid)
    assert row["state"] == "failed" and row["next_try"] is None
    assert json.loads(row["progress"]) == {**expected, "hold_reason": "synthetic_reason"}
    assert not store.db.in_transaction


def test_rescue_and_hold_land_together(store):
    eid = store.outbox_add("new_messages", 1, {"message_ids": [1, 2]})
    notify_flush._hold_event(store, _row(store, eid), {}, reason="synthetic_reason")
    row = _row(store, eid)
    assert row["state"] == "failed" and row["next_try"] is None
    assert json.loads(row["progress"]) == {"hold_reason": "synthetic_reason"}
    rescued = store.db.execute("SELECT payload FROM notify_outbox WHERE event_id!=?",
                               (eid,)).fetchone()
    assert json.loads(rescued["payload"])["rescue_of"] == eid
    assert not store.db.in_transaction
