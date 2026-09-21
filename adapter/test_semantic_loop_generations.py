"""Advisory loop relations must not outlive or cross their source scope."""
import json
import time

import semantic
import semantic_loops
from test_mcs_semantic import _FakeJev, _cfg, _llm, _message, _seeded


def _pending_fact(member):
    facts, complete, _ = semantic.extract_facts(_llm, member)
    assert complete
    facts[0]["kind"] = "pending_item"
    return facts


def test_relation_rechecks_edited_trigger_and_excludes_other_thread(tmp_path):
    db = _seeded(tmp_path)
    try:
        cfg, _ = semantic.semantic_config(_cfg())
        bundle = semantic.thread_bundle(db, 1, 1, [2])
        facts = {1: _pending_fact(bundle["members"][0])}
        fake = _FakeJev(choice_map={"relation": "completion_report"})
        semantic_loops.update_loops(db, 1, bundle, facts, fake, cfg,
                                   time.monotonic() + 30)
        assert fake.requests_made == 1
        original = db.artifacts("loop_event", project_id=1)[0]
        candidate = db.artifacts("loop_candidate", project_id=1)[0]
        db.artifact_add("loop_candidate", candidate["content"],
                        project_id=1, message_id=1, meta={})
        # A historical artifact lacking source/policy bindings is not eligible
        # for a fresh relation verdict, even if its origin body still matches.
        semantic_loops.update_loops(db, 1, bundle, facts, fake, cfg,
                                   time.monotonic() + 30)
        assert fake.requests_made == 1  # same generation is idempotent
        db.save_messages([_message(2, parent=1, body="まだ実施していません。")])
        edited = semantic.thread_bundle(db, 1, 1, [2])
        semantic_loops.update_loops(db, 1, edited, {}, fake, cfg,
                                   time.monotonic() + 30)
        assert fake.requests_made == 2
        events = db.artifacts("loop_event", project_id=1)
        assert len(events) == 2
        assert len({json.loads(e["content"])["trigger_revision"] for e in events}) == 2
        assert next(e for e in events if e["artifact_id"] == original["artifact_id"]) == original
        db.save_messages([_message(3, body="別の依頼が完了しました。")])
        other = semantic.thread_bundle(db, 1, 3, [3])
        semantic_loops.update_loops(db, 1, other, {}, fake, cfg,
                                   time.monotonic() + 30)
        assert fake.requests_made == 2
        assert db.db.execute("SELECT count(*) FROM requests").fetchone()[0] == 0
        with db.db:
            db.db.execute("UPDATE messages SET posted_at=? WHERE message_id=2",
                          ("2026-09-18T00:00:00+09:00",))
        older = semantic.thread_bundle(db, 1, 1, [2])
        semantic_loops.update_loops(db, 1, older, {}, fake, cfg,
                                   time.monotonic() + 30)
        event = json.loads(db.artifacts("loop_event", project_id=1)[-1]["content"])
        assert event["relation"] == "unclear"
    finally:
        db.close()


def test_candidate_revision_changes_without_reusing_old_confirmation(tmp_path):
    db = _seeded(tmp_path)
    try:
        cfg, _ = semantic.semantic_config(_cfg())
        bundle = semantic.thread_bundle(db, 1, 1, [1])
        facts = _pending_fact(bundle["members"][0])
        semantic_loops.update_loops(db, 1, bundle, {1: facts}, None, cfg,
                                   time.monotonic() + 30)
        db.save_messages([_message(1, body=semantic.jev_state(bundle, 1)["target"]["text"]
                                  + "ただし次回訪問まで保留してください。")])
        changed = semantic.thread_bundle(db, 1, 1, [1])
        new_facts = _pending_fact(changed["members"][0])
        semantic_loops.update_loops(db, 1, changed, {1: new_facts}, None, cfg,
                                   time.monotonic() + 30)
        candidates = [json.loads(r["content"]) for r in db.artifacts("loop_candidate", project_id=1)]
        assert len(candidates) == 2
        assert len({c["origin"]["revision"] for c in candidates}) == 2
        assert all(c["state"] == "PROPOSED" for c in candidates)
        assert db.db.execute("SELECT count(*) FROM command_receipts").fetchone()[0] == 0
    finally:
        db.close()
