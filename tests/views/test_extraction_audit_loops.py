"""Audit loop counts use the view's source/policy guards and formal request status."""
import semantic_policy
import semantic_store
import json
import sqlite3
import time

import pytest

import ledger
import mcs_stats
import mcs_view
import semantic_loops
from semantic_testkit import BODY, _cfg, _message, _pending_fact
from common_testkit import make_ledger


def test_open_resolution_closed_and_stale_are_distinct_and_read_only(tmp_path):
    db = make_ledger(tmp_path)
    try:
        db.save_messages([_message(mid, body=BODY + "残薬は5錠です。") for mid in range(1, 5)])
        cfg, _ = semantic_policy.semantic_config(_cfg())
        policy = semantic_policy.policy_fingerprint(cfg)
        db.artifact_add("semantic_policy", policy)
        for mid in range(1, 5):
            bundle = semantic_store.thread_bundle(db, 1, mid, [mid])
            semantic_loops.update_loops(db, 1, bundle, {mid: _pending_fact(bundle["members"][0])},
                None, cfg, time.monotonic() + 30)
            candidate = db.artifacts("loop_candidate", message_id=mid)[0]
            if mid == 2:
                member = bundle["members"][0]
                db.artifact_add("loop_event", json.dumps({"loop_artifact_id": candidate["artifact_id"],
                    "loop_origin_id": mid, "origin_revision": member["revision"], "trigger_message_id": mid,
                    "trigger_revision": member["revision"], "relation": "completion_report",
                    "reported_relation": "completion_report", "confidence": 0.9}), project_id=1, message_id=mid,
                    meta={"source_fingerprint": bundle["source_fingerprint"], "policy_fingerprint": policy})
            elif mid == 3:
                db.db.execute("INSERT INTO requests(project_id,source_message_id,source_hash,title,status,revision,created_at,updated_at) "
                    "VALUES(1,3,'synthetic-hash','synthetic','done',1,1,1)")
                rid = db.db.execute("SELECT MAX(request_id) FROM requests").fetchone()[0]
                db.artifact_add("request_loop_link", json.dumps({"request_id": rid,
                    "loop_artifact_id": candidate["artifact_id"]}), project_id=1)
            elif mid == 4:
                db.save_messages([_message(mid, body=BODY + "残薬は6錠です。")])
        db.db.commit()
        snapshot = ledger.publish_snapshot(str(tmp_path / "ledger.db"), str(tmp_path / "snapshot"))
        view = mcs_view.View(snapshot)
        try:
            result = view.stats({"preset": "extraction_audit"})["stats"]["request_loop_state"]
            assert result["status"] == "ok"
            assert next(item for item in result["rows"]["items"] if item["class"] == "leftover") == {
                "class": "leftover", "open": 1, "resolution_candidate": 1, "closed": 1,
                "stale": 1, "evaluation_unavailable": 0}
            shown = view.read("loops", project=1)["items"]
            assert sum(item["current"] for item in shown) == 3
            assert sum(item["effective_state"] == "RESOLUTION_CANDIDATE" for item in shown) == 1
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                view.db.execute("CREATE TABLE forbidden_write(value)")
            assert mcs_stats.DEFINITION_VERSION == result["definition_version"]
        finally:
            view.close()
    finally:
        db.close()
