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
        import request_loops
        source = db.db.execute("SELECT * FROM artifacts WHERE artifact_id=?",
                               (candidate["artifact_id"],)).fetchone()
        batch = request_loops.effective_loop_states(db.db, [source], relation_limit=None)[0]
        assert batch == request_loops.effective_loop_state(db.db, source, relation_limit=None)
        assert batch["relation_evaluation_unavailable"] == 0
    finally:
        db.close()


def test_batch_loop_evaluation_matches_complete_individual_histories_with_one_query(tmp_path):
    import request_loops

    db, bundle, candidate, receipt, meta = _setup(tmp_path)
    try:
        template = json.loads(candidate["content"])
        for index in range(12):
            db.artifact_add("loop_candidate", json.dumps({**template, "description": f"架空依頼{index}"}),
                            project_id=1, message_id=1, meta=meta)
        candidates = list(db.db.execute("SELECT * FROM artifacts WHERE kind='loop_candidate'"))
        for index, source in enumerate(candidates):
            event = {"loop_artifact_id": source["artifact_id"], "loop_origin_id": 1,
                     "origin_revision": bundle["members"][0]["revision"],
                     "trigger_message_id": 2, "trigger_revision": receipt["trigger_revision"],
                     "relation": "completion_report" if index % 2 else "progress_report",
                     "reported_relation": "completion_report" if index % 2 else "progress_report",
                     "confidence": .9}
            db.artifact_add("loop_event", json.dumps(event), project_id=1, message_id=2, meta=meta)
            db.artifact_add("loop_relation_rejected", json.dumps({**receipt,
                            "loop_artifact_id": source["artifact_id"]}), project_id=1, message_id=2, meta=meta)
        legacy = {**event, "relation": "acknowledges", "reported_relation": "acknowledges"}
        legacy.pop("loop_artifact_id")
        db.artifact_add("loop_event", json.dumps(legacy), project_id=1, message_id=2, meta=meta)
        for content in ("{", "null", json.dumps({**event, "loop_artifact_id": 999999})):
            db.artifact_add("loop_event", content, project_id=1, message_id=2, meta=meta)
        for content in ("{", "null"):
            db.artifact_add("loop_candidate", content, project_id=1, message_id=1, meta=meta)
        for change in ({"policy_fingerprint": "old"}, {"fingerprint": "old"}, {"invalidated": True}):
            db.artifact_add("loop_candidate", json.dumps(template), project_id=1, message_id=1,
                            meta={**meta, **change})
        candidates = list(db.db.execute("SELECT * FROM artifacts WHERE kind='loop_candidate'"))
        trace = []
        db.db.set_trace_callback(trace.append)
        batch = request_loops.effective_loop_states(db.db, candidates, relation_limit=None)
        db.db.set_trace_callback(None)
        assert sum("FROM artifacts" in sql and "'loop_event'" in sql for sql in trace) == 1
        individual = [request_loops.effective_loop_state(db.db, row, relation_limit=None) for row in candidates]
        assert batch == [row for row in individual if row is not None]
        eligible = request_loops.effective_loop_states(db.db, candidates, eligible_only=True, relation_limit=None)
        assert eligible == [row for row in individual if row and row["adoption_eligible"]]
        assert all(len(row["relation_events"]) == 2 for row in eligible)
        assert all(row["relation_evaluation_unavailable"] == 0 for row in eligible)
    finally:
        db.close()
