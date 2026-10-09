"""Invalidated advisory history stays visible but cannot be current or adopted."""
import json
import time

import ledger
import mcs_view
import semantic
import semantic_loops
from semantic_testkit import _cfg, _pending_fact, _seeded


def test_invalidated_loop_is_stale_even_with_matching_source_and_policy(tmp_path):
    db = _seeded(tmp_path)
    try:
        config, _ = semantic.semantic_config(_cfg())
        db.artifact_add("semantic_policy", semantic.policy_fingerprint(config))
        bundle = semantic.thread_bundle(db, 1, 1, [1])
        semantic_loops.update_loops(db, 1, bundle,
                                   {1: _pending_fact(bundle["members"][0])},
                                   None, config, time.monotonic() + 30)
        old = db.artifacts("loop_candidate", project_id=1)[0]
        meta = json.loads(old["meta"])
        with db.db:
            db.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?",
                          (json.dumps({**meta, "invalidated": True}), old["artifact_id"]))
        snap = ledger.publish_snapshot(str(tmp_path / "ledger.db"), str(tmp_path / "snapshot"))
        view = mcs_view.View(snap)
        try:
            item = view.read("loops", project=1)["items"][0]
            assert item["artifact_id"] == old["artifact_id"]
            assert item["current"] is False
            assert item["effective_state"] == "STALE"
            assert item["adoption_eligible"] is False
        finally:
            view.close()
        assert db.db.execute("SELECT count(*) FROM requests").fetchone()[0] == 0
    finally:
        db.close()
