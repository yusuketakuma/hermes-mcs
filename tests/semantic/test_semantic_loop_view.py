"""Loop candidates are current only for their source and policy generation."""
import json
import time

import ledger
import mcs_view
import semantic
import semantic_loops
from semantic_testkit import _cfg, _FakeJev, _message, _pending_fact, _seeded


def test_edited_reply_marks_resolution_event_stale(tmp_path):
    db = _seeded(tmp_path)
    try:
        bundle = semantic.thread_bundle(db, 1, 1, [2])
        config, _ = semantic.semantic_config(_cfg())
        db.artifact_add("semantic_policy", semantic.policy_fingerprint(config))
        semantic_loops.update_loops(
            db, 1, bundle, {1: _pending_fact(bundle["members"][0])},
            _FakeJev(choice_map={"relation": "completion_report"}), config,
            time.monotonic() + 30)
        snap = ledger.publish_snapshot(str(tmp_path / "ledger.db"), str(tmp_path / "snap"))
        view = mcs_view.View(snap)
        try:
            assert view.read("loops", project=1)["items"][0]["effective_state"] == "RESOLUTION_CANDIDATE"
        finally:
            view.close()
        db.save_messages([_message(2, parent=1, body="完了の報告を訂正します。未実施です。")])
        snap = ledger.publish_snapshot(str(tmp_path / "ledger.db"), str(tmp_path / "snap"))
        view = mcs_view.View(snap)
        try:
            candidate = view.read("loops", project=1)["items"][0]
            assert candidate["current"] is False
            assert candidate["effective_state"] == "STALE"
            assert candidate["relation_events"][0]["stale"] is True
        finally:
            view.close()
        assert db.db.execute("SELECT count(*) FROM requests").fetchone()[0] == 0
    finally:
        db.close()


def test_policy_revision_marks_loop_candidate_stale(tmp_path):
    db = _seeded(tmp_path)
    try:
        config, _ = semantic.semantic_config(_cfg())
        db.artifact_add("semantic_policy", semantic.policy_fingerprint(config))
        bundle = semantic.thread_bundle(db, 1, 1, [1])
        semantic_loops.update_loops(
            db, 1, bundle, {1: _pending_fact(bundle["members"][0])},
            None, config, time.monotonic() + 30)
        db.artifact_add("semantic_policy", "policy-from-a-new-calibration")
        snap = ledger.publish_snapshot(str(tmp_path / "ledger.db"),
                                       str(tmp_path / "snap"))
        view = mcs_view.View(snap)
        try:
            candidate = view.read("loops", project=1)["items"][0]
            assert candidate["current"] is False
            assert candidate["effective_state"] == "STALE"
        finally:
            view.close()
    finally:
        db.close()


def test_same_source_and_policy_generation_reuses_candidate(tmp_path):
    db = _seeded(tmp_path)
    try:
        config, _ = semantic.semantic_config(_cfg())
        policy = semantic.policy_fingerprint(config)
        db.artifact_add("semantic_policy", policy)
        bundle = semantic.thread_bundle(db, 1, 1, [1])
        facts = {1: _pending_fact(bundle["members"][0])}
        semantic_loops.update_loops(
            db, 1, bundle, facts, None, config, time.monotonic() + 30)
        semantic_loops.update_loops(
            db, 1, bundle, facts, None, config, time.monotonic() + 30)
        candidates = db.artifacts("loop_candidate", project_id=1)
        assert len(candidates) == 1
        meta = json.loads(candidates[0]["meta"])
        assert meta["fingerprint"] == bundle["source_fingerprint"]
        assert meta["policy_fingerprint"] == policy
    finally:
        db.close()


def test_new_reply_promotes_current_candidate_generation(tmp_path):
    db = _seeded(tmp_path)
    try:
        config, _ = semantic.semantic_config(_cfg())
        policy = semantic.policy_fingerprint(config)
        db.artifact_add("semantic_policy", policy)
        initial = semantic.thread_bundle(db, 1, 1, [1])
        semantic_loops.update_loops(
            db, 1, initial, {1: _pending_fact(initial["members"][0])},
            None, config, time.monotonic() + 30)
        old = db.artifacts("loop_candidate", project_id=1)[0]

        db.save_messages([_message(3, parent=1, body="完了しました。")])
        current = semantic.thread_bundle(db, 1, 1, [3])
        fake = _FakeJev(choice_map={"relation": "completion_report"})
        semantic_loops.update_loops(
            db, 1, current, {}, fake, config, time.monotonic() + 30)
        assert fake.requests_made == 1
        assert len(db.artifacts("loop_candidate", project_id=1)) == 2
        semantic_loops.update_loops(
            db, 1, current, {}, fake, config, time.monotonic() + 30)
        assert fake.requests_made == 1
        assert len(db.artifacts("loop_candidate", project_id=1)) == 2

        snap = ledger.publish_snapshot(str(tmp_path / "ledger.db"),
                                       str(tmp_path / "snap"))
        view = mcs_view.View(snap)
        try:
            items = view.read("loops", project=1)["items"]
            live = next(item for item in items if item["current"])
            history = next(item for item in items
                           if item["artifact_id"] == old["artifact_id"])
            assert live["effective_state"] == "RESOLUTION_CANDIDATE"
            assert live["relation_events"][0]["stale"] is False
            assert live["relation_events"][0]["trigger_message_id"] == 3
            assert history["effective_state"] == "STALE"
        finally:
            view.close()
    finally:
        db.close()
