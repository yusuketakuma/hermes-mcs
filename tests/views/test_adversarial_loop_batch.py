"""Synthetic loop read fan-out; copy into tests/views in an isolated review tree.

No wall-clock assertions. Keep complete history to preserve old resolution evidence.
The current UI needs one project history query per candidate; the already-existing
batch evaluator produces identical results with one project history query.
"""
import json

import ledger
import mcs_view
import request_loops
import pytest
from test_loop_relation_rejections_view import _setup


@pytest.mark.parametrize("candidate_count,events_per_candidate", [(2, 60), (8, 120)])
def test_view_loop_history_is_batched_without_losing_old_resolution(
        tmp_path, candidate_count, events_per_candidate):
    store, bundle, template, receipt, meta = _setup(tmp_path)
    try:
        base = json.loads(template["content"])
        for i in range(1, candidate_count):
            store.artifact_add("loop_candidate", json.dumps({**base, "description": f"合成依頼{i}"}),
                               project_id=1, message_id=1, meta=meta)
        candidates = list(store.db.execute(
            "SELECT * FROM artifacts WHERE kind='loop_candidate' ORDER BY artifact_id DESC"))
        with store.db:
            for row in candidates:
                event = {"loop_artifact_id": row["artifact_id"], "loop_origin_id": 1,
                         "origin_revision": bundle["members"][0]["revision"],
                         "trigger_message_id": 2, "trigger_revision": receipt["trigger_revision"],
                         "relation": "completion_report", "reported_relation": "completion_report",
                         "confidence": .9}
                # Old valid completion must survive a long tail of stale observations.
                for i in range(events_per_candidate):
                    value = event if i == 0 else {**event, "trigger_revision": "stale-synthetic"}
                    store.db.execute(
                        "INSERT INTO artifacts(kind,project_id,message_id,content,meta,created_at) "
                        "VALUES('loop_event',1,2,?,?,?)", (json.dumps(value), json.dumps(meta), i + 1))
        snapshot = ledger.publish_snapshot(str(tmp_path / "ledger.db"), str(tmp_path / "snapshot"))
        with_view = mcs_view.View(snapshot)
        try:
            trace = []
            with_view.db.set_trace_callback(trace.append)
            actual = with_view.read("loops", project=1, limit=candidate_count)["items"]
            with_view.db.set_trace_callback(None)
            history_queries = [s for s in trace if "FROM artifacts" in s and "loop_event" in s]
            assert len(actual) == candidate_count
            assert all(x["effective_state"] == "RESOLUTION_CANDIDATE" for x in actual)
            expected = request_loops.effective_loop_states(
                with_view.db, candidates, policy=request_loops.latest_policy(with_view.db))
            assert actual == expected
            # Current e885: candidate_count; shared batch implementation: one.
            assert len(history_queries) <= 1, {
                "history_queries": len(history_queries), "candidates": candidate_count,
                "events": candidate_count * events_per_candidate}
        finally:
            with_view.close()
    finally:
        store.close()
