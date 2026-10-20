"""Fully synthetic reproduction of independently found v1.0.24+ regressions."""
import json
from types import SimpleNamespace

import pytest
import patient_context_view as pcv
import run_check
from semantic_testkit import _message
from test_read_model import _artifact, _db, _hash_of
from test_patient_context_v3_views import _reply, T0, DAY


def test_probe_old_unanswered_request_survives_more_than_fifteen_new_replied_requests(tmp_path):
    db = _db(tmp_path, ())
    try:
        for mid in range(1, 17):
            db.save_messages([_message(mid, body=f"架空依頼{mid}の確認をお願いします。")], project_id=1)
            db.db.execute("UPDATE messages SET posted_at_ts=? WHERE message_id=?", (T0 + mid * DAY, mid))
            _artifact(db, "extract_llm", mid,
                      {"requests": [{"to": "pharmacy", "action": f"架空依頼{mid}"}]},
                      {"hash": _hash_of(db, mid)})
            if mid > 1:
                _reply(SimpleNamespace(db=db), mid + 100, mid, "done")
        talk = pcv.replies(db.db, 1)
        ev = {"project_id": 1, "items": [], "as_of": None, "coverage": {}}
        previsit = [i["message_id"] for i in pcv.previsit(db.db, ev, T0 + 20 * DAY)["unanswered"]]
        handoff = [i["message_id"] for i in pcv.handoff(db.db, ev)["unanswered"]]
        assert (len(talk["items"]), previsit, handoff) == (16, [1], [1])
    finally:
        db.close()


@pytest.mark.parametrize("content", [None, [], [1], 0, False, "synthetic"])
def test_probe_valid_nonobject_cursor_does_not_stop_patient_walk(tmp_path, content):
    db = _db(tmp_path, ())
    try:
        db.artifact_add(run_check.WALK_CURSOR_KIND, json.dumps(content), meta={"stage": "backfill"})
        assert [row["project_id"] for row in run_check._walk_order(db, "backfill")] == [1]
    finally:
        db.close()
