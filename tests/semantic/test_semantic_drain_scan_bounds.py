"""Regressions for the semantic drain (synthetic only)."""
import json
import time

import pytest

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


@pytest.mark.parametrize("fixed_stop", [False, True])
def test_slow_invalidation_yields_next_window_with_the_same_budget(tmp_path, monkeypatch, fixed_stop):
    db = _seeded(tmp_path)
    clock = [100.0]
    scans, served = [], []
    monkeypatch.setattr(semantic_drain.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(semantic_drain, "_invalidated_at", None)
    monkeypatch.setattr(v4, "reproject_stale", lambda *_: {})

    def invalidate(*_):
        scans.append(clock[0])
        clock[0] += 301
        return 0

    def process(ledger, scfg, job, _jev, _llm, deadline, **_kwargs):
        served.append(deadline)
        assert scfg["job_budget_seconds"] == 450
        assert semantic.runtime.job_deadline(scfg, deadline) == min(deadline, clock[0] + 450)
        assert semantic.runtime.transition(ledger, semantic.runtime.JobToken.from_row(job), "done")
        return "done"

    monkeypatch.setattr(semantic_store, "invalidate_projections", invalidate)
    monkeypatch.setattr(semantic, "_process_job", process)
    cfg = _cfg("shadow", job_budget_seconds=450)
    try:
        first = semantic_drain.run_due(db, cfg, {"errors": []}, 580,
                                      lane="backlog", max_jobs=1, jev_client=_FakeJev(), llm_fn=lambda *_: None)
        assert first["done"] == 0 and first["left"] == 1
        assert semantic_drain._invalidated_at == 401
        deadline = 580 if fixed_stop else clock[0] + 480
        second = semantic_drain.run_due(db, cfg, {"errors": []}, deadline,
                                       lane="backlog", max_jobs=1, jev_client=_FakeJev(), llm_fn=lambda *_: None)
        assert scans == [100]
        assert served == ([] if fixed_stop else [881])
        assert second["done"] == (0 if fixed_stop else 1)
        row = db.db.execute("SELECT state,attempts FROM fetch_jobs WHERE kind='semantic'").fetchone()
        assert row["state"] == ("pending" if fixed_stop else "done")
        assert row["attempts"] == 0
    finally:
        db.close()


def test_slow_backlog_throttle_does_not_skip_realtime_off_revocation(tmp_path, monkeypatch):
    db = _drained_old_version(tmp_path)
    clock = [100.0]
    original = semantic_store.invalidate_projections
    scans = []
    monkeypatch.setattr(semantic_drain.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(semantic_drain, "_invalidated_at", None)
    monkeypatch.setattr(v4, "reproject_stale", lambda *_: {})

    def invalidate(ledger, scfg):
        scans.append(scfg["mode"])
        result = original(ledger, scfg)
        clock[0] += 301
        return result

    monkeypatch.setattr(semantic_store, "invalidate_projections", invalidate)
    try:
        semantic_drain.run_due(db, _canonical_cfg(), {"errors": []}, clock[0] + 480,
                              lane="backlog", jev_client=_FakeJev(), llm_fn=lambda *_: None)
        semantic_drain.run_due(db, _cfg("off"), {"errors": []}, clock[0] + 480,
                              lane="realtime", jev_client=_FakeJev(), llm_fn=lambda *_: None)
        assert scans == ["enforce", "off"]
        rows = db.artifacts("canonical_projection") + db.artifacts("semantic_facts_v4")
        assert rows and all(json.loads(row["meta"]).get("invalidated") for row in rows)
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
