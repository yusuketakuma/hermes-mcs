"""Round-4 stability regressions for the semantic drain (synthetic only)."""
import json
import time

import semantic
import semantic_drain
import semantic_loops
import semantic_projection
import semantic_store
import semantic_v4 as v4
from semantic_policy import semantic_config
from semantic_testkit import (_canonical_cfg, _cfg, _drained_old_version,
                              _FakeJev, _message, _pending_fact,
                              _seeded)


def test_backlog_lane_throttles_invalidation(tmp_path, monkeypatch):
    db = _seeded(tmp_path)
    now = [1000.0]
    calls = []
    monkeypatch.setattr(semantic_drain.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(semantic_drain, "_invalidated_at", None)
    monkeypatch.setattr(semantic_store, "invalidate_projections",
                        lambda ledger, scfg: calls.append(1) or 0)
    try:
        def run(lane):
            semantic_drain.run_due(db, _cfg("off"), {"errors": []},
                                   now[0] + 60, lane=lane)
        run("backlog")
        run("backlog")
        assert len(calls) == 1
        run("realtime")              # the tick lane always refreshes
        assert len(calls) == 2
        now[0] += semantic_drain._BACKLOG_INVALIDATE_S
        run("backlog")
        assert len(calls) == 3
    finally:
        db.close()


def test_loop_rereads_ignore_other_generations_and_bad_meta(tmp_path):
    db = _seeded(tmp_path)
    try:
        bundle = semantic.thread_bundle(db, 1, 1, [2])
        config, _ = semantic.semantic_config(_cfg())
        fake = _FakeJev(choice_map={"relation": "completion_report"})
        facts = {1: _pending_fact(bundle["members"][0])}
        assert semantic_loops.update_loops(
            db, 1, bundle, facts, fake, config, time.monotonic() + 30)[1]
        asked = fake.requests_made
        # foreign rows: malformed event meta, another generation's event,
        # and a candidate outside the thread
        for meta in ("{not json", json.dumps({"fingerprint": "other"})):
            aid = db.artifact_add("loop_event", "{}", project_id=1,
                                  message_id=2)
            db.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?",
                          (meta, aid))
        db.save_messages([_message(3, body="別スレッドの合成本文")])
        db.artifact_add("loop_candidate", json.dumps(
            {"loop_id": "loop_x", "description": "x", "origin": {}}),
            project_id=1, message_id=3, meta={"candidate_fp": "x"})
        db.db.commit()
        assert semantic_loops.update_loops(
            db, 1, bundle, facts, fake, config,
            time.monotonic() + 30) == (0, True)
        assert fake.requests_made == asked
    finally:
        db.close()


def test_reproject_failure_is_marked_not_blocking(tmp_path, monkeypatch):
    db = _drained_old_version(tmp_path)
    try:
        scfg = semantic_config(_canonical_cfg())[0]

        def broken(doc):
            raise KeyError("facts")
        monkeypatch.setattr(semantic_projection, "project_v2_doc_legacy",
                            broken)
        out = v4.reproject_stale(db, scfg)
        assert out["reprojected"] == 0 and out["skipped"] > 0
        assert out["skip_reasons"] == {"projection_failed": out["skipped"]}
        # marked rows leave the queue instead of heading it forever
        assert v4.reproject_stale(db, scfg)["skipped"] == 0
    finally:
        db.close()


def test_job_locks_are_bucketed(tmp_path):
    db = _seeded(tmp_path)
    try:
        n = semantic_drain._JOB_LOCK_BUCKETS
        with semantic_drain._job_lock(db, 5) as held:
            assert held
            with semantic_drain._job_lock(db, 5 + n) as other:
                assert not other          # same bucket: skipped this pass
        with semantic_drain._job_lock(db, 5 + 2 * n) as held:
            assert held
        assert sorted(p.name for p in (tmp_path / "semantic_locks").iterdir()) \
            == ["5.lock"]
    finally:
        db.close()
