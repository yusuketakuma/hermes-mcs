"""Synthetic long histories retain relation failure suppression and resolution."""
import json

import pytest

from test_loop_relation_rejections_view import _setup, _view


@pytest.mark.parametrize("relation", ["unrelated", "completion_report"])
def test_old_current_relation_is_not_lost_after_fifty_new_events(tmp_path, relation):
    db, bundle, candidate, receipt, meta = _setup(tmp_path)
    try:
        db.artifact_add("loop_relation_rejected", json.dumps(receipt),
                        project_id=1, message_id=2, meta=meta)
        event = {"loop_artifact_id": candidate["artifact_id"], "loop_origin_id": 1,
                 "origin_revision": bundle["members"][0]["revision"],
                 "trigger_message_id": 2, "trigger_revision": receipt["trigger_revision"],
                 "relation": relation, "reported_relation": relation, "confidence": .9}
        db.artifact_add("loop_event", json.dumps(event), project_id=1, message_id=2, meta=meta)
        for _ in range(60):
            db.artifact_add("loop_event", json.dumps({**event, "trigger_revision": "old"}),
                            project_id=1, message_id=2, meta=meta)
        item = _view(db, tmp_path)
        assert item["relation_evaluation_unavailable"] == 0
        assert len(item["relation_events"]) <= 5
        assert item["effective_state"] == (
            "RESOLUTION_CANDIDATE" if relation == "completion_report" else "PROPOSED")
    finally:
        db.close()
