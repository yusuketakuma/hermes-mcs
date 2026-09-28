"""T14 — two-lane fair drain, persisted scheduler, per-job phase metrics.

All synthetic: stub LLM/Jev, temp SQLite. Covers:
  - arrival lane keeps priority while backfill gets a guaranteed share
  - max_jobs=1 alternates cohorts via a *persisted* turn marker
    (restart-safe: the Ledger is reopened between drains)
  - job_metrics phase split (queue wait vs llm vs jev vs post) + cohort
  - the drain-run artifact carries metrics, never message body text
  - observe() exposes queue ages / cohort split / scheduler / rates,
    with None (unknown) instead of fake zeros where unmeasured
"""
import json
import time

import pytest

import semantic
import semantic_drain
from semantic_observe import observe
from test_mcs_semantic import (_cfg, _FakeJev, _ledger, _llm, _message,
                               _patient)


def _job(db, mid, eligible=False, pid=1):
    """Direct fetch_jobs seed — arrival jobs carry eligible=True."""
    payload = {"targets": [mid],
               "origin": {"source": "unread" if eligible
                          else "history_import",
                          "event_id": mid if eligible else None},
               "generation": f"g{mid}",
               "source_generation": "sg1"}
    if eligible:
        payload["eligible"] = True
    db.db.execute(
        "INSERT INTO fetch_jobs(kind,project_id,message_id,parent_id,"
        "payload,state,next_try,created_at,updated_at) "
        "VALUES('semantic',?,?,NULL,?,'pending',0,?,?)",
        (pid, mid, json.dumps(payload), time.time(), time.time()))
    db.db.commit()


def _world(tmp_path, mids):
    db = _ledger(tmp_path)
    _patient(db)
    db.save_messages([_message(mid) for mid in mids], project_id=1)
    return db


def _due(db, **kw):
    args = {"jev_client": _FakeJev(), "llm_fn": _llm}
    args.update(kw)
    # a generous Jev budget keeps these tests on scheduling/metrics —
    # the daily cap has its own dedicated tests elsewhere
    return semantic.run_due(db, _cfg("shadow", budget=100000),
                            {"errors": []}, time.monotonic() + 300,
                            **args)


def test_backfill_guaranteed_share_under_arrival_stream(tmp_path):
    """10 arrivals + 3 backfill at max_jobs=4: every window gives the
    backlog >=1 slot, so a continuous arrival stream cannot starve it."""
    db = _world(tmp_path, list(range(10, 40)))
    for mid in range(10, 20):
        _job(db, mid, eligible=True)
    for mid in range(30, 33):
        _job(db, mid)
    out = _due(db, max_jobs=4)
    assert out["lanes"] == {"arrival": 3, "backfill": 1}
    done = {r["message_id"] for r in db.db.execute(
        "SELECT message_id FROM fetch_jobs WHERE kind='semantic' "
        "AND state='done'")}
    assert len(done & {30, 31, 32}) == 1
    # second window: arrivals keep priority but the backlog still moves
    out2 = _due(db, max_jobs=4)
    assert out2["lanes"]["backfill"] == 1
    done = {r["message_id"] for r in db.db.execute(
        "SELECT message_id FROM fetch_jobs WHERE kind='semantic' "
        "AND state='done'")}
    assert len(done & {30, 31, 32}) == 2
    sched = semantic_drain._sched_state(db)
    assert sched["arrival_selected"] == 6
    assert sched["backfill_selected"] == 2
    assert sched["backfill_last_served_at"] > 0
    db.close()


def test_single_slot_alternation_is_restart_safe(tmp_path):
    """max_jobs=1 must not permanently starve either cohort: the turn
    marker lives in fetch_jobs and survives a Ledger reopen."""
    db = _world(tmp_path, [10, 20, 30, 40])
    _job(db, 10, eligible=True)
    _job(db, 20)
    out = _due(db, max_jobs=1)
    assert out["lanes"] in ({"arrival": 1, "backfill": 0},
                            {"arrival": 0, "backfill": 1})
    first = "backfill" if out["lanes"]["backfill"] else "arrival"
    sched = semantic_drain._sched_state(db)
    assert sched["turn"] == first
    # restart: new Ledger over the same file — no process state
    db.close()
    from ledger import Ledger
    db = Ledger(str(tmp_path / "ledger.db"))
    with db.db:
        db.db.execute("UPDATE fetch_jobs SET next_try=0 "
                      "WHERE kind='semantic'")
    out = _due(db, max_jobs=1)
    second = "backfill" if out["lanes"]["backfill"] else "arrival"
    assert second != first            # the other cohort now wins
    # an uncontended window does not rewrite the marker
    assert semantic_drain._sched_state(db)["turn"] == first
    # contention again after restart: the cohort that lost the last
    # contested slot (drain1) wins — i.e. the marker's other side
    _job(db, 30, eligible=True)
    _job(db, 40)
    out = _due(db, max_jobs=1)
    third = "backfill" if out["lanes"]["backfill"] else "arrival"
    assert third != first and third == second
    assert semantic_drain._sched_state(db)["turn"] == third
    sched = semantic_drain._sched_state(db)
    assert sched["arrival_selected"] + sched["backfill_selected"] == 3
    db.close()


def test_no_backfill_means_arrivals_take_all_slots(tmp_path):
    db = _world(tmp_path, [10, 11, 12])
    for mid in (10, 11, 12):
        _job(db, mid, eligible=True)
    out = _due(db, max_jobs=4)
    assert out["lanes"] == {"arrival": 3, "backfill": 0}
    assert out["done"] == 3
    db.close()


@pytest.mark.parametrize('arrivals', [0, 1])
def test_backfill_uses_slots_left_by_small_arrival_lane(tmp_path, arrivals):
    db = _world(tmp_path, list(range(10, 16)))
    try:
        for mid in range(10, 16):
            _job(db, mid, eligible=mid < 10 + arrivals)
        out = _due(db, max_jobs=4)
        assert out['lanes'] == {'arrival': arrivals, 'backfill': 4 - arrivals}
        assert out['done'] == 4
    finally:
        db.close()


def test_due_backlog_count_is_not_silently_capped_at_fifty(tmp_path):
    db = _world(tmp_path, list(range(1, 66)))
    try:
        for mid in range(1, 66):
            _job(db, mid)
        out = _due(db, max_jobs=1)
        assert out['done'] == 1 and out['left'] == 64
    finally:
        db.close()


def test_drain_cli_does_not_initialize_writer_before_lock(tmp_path, monkeypatch):
    import ledger
    import mcs_util
    clock = [0.0]
    monkeypatch.setattr(semantic_drain.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(semantic_drain.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(semantic_drain, 'load_config', lambda: {})
    monkeypatch.setattr(semantic_drain.sys, 'argv', ['semantic_drain', '--drain', '--stop-after', '1'])
    def no_lock():
        clock[0] = 2.0
        return None
    monkeypatch.setattr(mcs_util, 'acquire_run_lock', no_lock)
    def no_writer(*args, **kwargs):
        raise AssertionError('writer opened without lock')
    monkeypatch.setattr(ledger, 'Ledger', no_writer)
    assert semantic_drain.main() == 0


@pytest.mark.parametrize('arguments', [
    ['--max-jobs', '0'], ['--max-jobs', '33'], ['--stop-after', 'nan'],
    ['--stop-after', 'inf'], ['--stop-after', '-1'],
])
def test_drain_cli_rejects_invalid_bounds_before_db(tmp_path, monkeypatch, arguments):
    import ledger
    monkeypatch.setattr(semantic_drain.sys, 'argv', ['semantic_drain', '--drain', *arguments])
    def no_writer(*args, **kwargs):
        raise AssertionError('writer opened for invalid arguments')
    monkeypatch.setattr(ledger, 'Ledger', no_writer)
    with pytest.raises(SystemExit) as error:
        semantic_drain.main()
    assert error.value.code == 2


def test_job_metrics_phase_split_and_cohort(tmp_path):
    db = _world(tmp_path, [10])
    _job(db, 10, eligible=True)
    slept = {"v": False}

    def slow_llm(prompt):
        slept["v"] = True
        time.sleep(0.05)
        return _llm(prompt)

    out = _due(db, llm_fn=slow_llm, max_jobs=1)
    assert out["done"] == 1 and slept["v"]
    metric = out["job_metrics"][0]
    assert metric["cohort"] == "arrival"
    assert metric["job_age_s"] >= 0 and metric["queue_wait_s"] >= 0
    assert metric["llm_s"] >= 0.04           # real wall time, not zero
    assert metric["jev_s"] >= 0 and metric["post_s"] >= 0
    assert metric["elapsed_s"] >= metric["llm_s"]
    assert metric["usage"]["unreported_requests"] >= 0
    assert type(metric["jev_requests"]) is int
    # durable run artifact — metrics live past the process (T14)
    row = db.db.execute(
        "SELECT content FROM artifacts WHERE kind='semantic_drain_run'"
    ).fetchone()
    assert row is not None
    run = json.loads(row["content"])
    assert run["lanes"] == {"arrival": 1, "backfill": 0}
    assert run["job_metrics"][0]["cohort"] == "arrival"
    # metrics never carry message body text (payload-hygiene rule)
    assert "カロナール" not in row["content"]
    db.close()


def test_sched_persist_failure_never_breaks_drain(tmp_path, monkeypatch):
    db = _world(tmp_path, [10])
    _job(db, 10, eligible=True)
    monkeypatch.setattr(semantic_drain, "_sched_write",
                        lambda *a, **k: 1 / 0)
    errors = {"errors": []}
    out = semantic.run_due(db, _cfg("shadow"), errors,
                           time.monotonic() + 300,
                           jev_client=_FakeJev(), llm_fn=_llm, max_jobs=1)
    assert out["done"] == 1
    assert "semantic: sched_persist_failed" in errors["errors"]
    db.close()


def test_observe_exposes_queue_fairness_and_rates(tmp_path):
    db = _world(tmp_path, [10, 20])
    _job(db, 10, eligible=True)
    out = _due(db, max_jobs=1)
    assert out["done"] == 1
    _job(db, 20)
    db.close()
    snap = observe(str(tmp_path / "ledger.db"), _cfg())
    ages = snap["queue_ages_s"]
    assert set(ages) == {"semantic", "extract_qc", "extract_llm"}
    assert ages["semantic"] is not None and ages["semantic"] >= 0
    assert ages["extract_qc"] is None        # no qc jobs -> unknown
    assert snap["cohorts"] == {"arrival": 0, "backfill": 1}
    assert snap["scheduler"]["arrival_selected"] == 1
    assert snap["scheduler"]["backfill_selected"] == 0
    recent = snap["recent_drain"]
    assert recent["runs"] == 1 and recent["done"] == 1
    assert recent["llm_s"] is not None and recent["llm_s"] >= 0
    assert recent["queue_wait_s_max"] >= 0


def test_observe_unknowns_are_null_not_zero(tmp_path):
    """A ledger that never ran the drain still reports — unknown
    metrics stay None instead of masquerading as zero."""
    db = _world(tmp_path, [10])
    db.close()
    snap = observe(str(tmp_path / "ledger.db"), _cfg())
    assert snap["queue_ages_s"]["extract_llm"] is not None
    assert snap["cohorts"] == {"arrival": 0, "backfill": 0}
    assert snap["scheduler"] == {"arrival_selected": None,
                                "backfill_selected": None,
                                "backfill_last_served_at": None}
    recent = snap["recent_drain"]
    assert recent["runs"] == 0
    assert recent["llm_s"] is None and recent["usage_tokens"] is None
    assert snap["extract_recent"]["artifacts"] == 0
    assert snap["extract_recent"]["calls"] is None


def test_observe_partial_measurements_remain_unknown(tmp_path):
    db = _world(tmp_path, [10])
    try:
        db.artifact_add('semantic_drain_run', json.dumps({
            'done': 1, 'job_metrics': [{'llm_s': 2, 'usage': {
                'input_tokens': 5, 'output_tokens': 0, 'unreported_requests': 1}}]}))
        db.artifact_add('extract_llm', '{}', meta={'integrity': {
            'calls': 1, 'timings': {'prompt_ms': 10}}})
        snap = observe(str(tmp_path / 'ledger.db'), _cfg())
        assert snap['recent_drain']['llm_s'] == 2
        assert snap['recent_drain']['jev_s'] is None
        assert snap['recent_drain']['post_s'] is None
        assert snap['recent_drain']['queue_wait_s_max'] is None
        assert snap['recent_drain']['usage_tokens'] is None
        assert snap['extract_recent']['prompt_ms'] == 10
        assert snap['extract_recent']['predicted_ms'] is None
    finally:
        db.close()


def test_observe_corrupt_records_are_visible_without_read_writes(tmp_path):
    db = _world(tmp_path, [10])
    try:
        db.artifact_add('semantic_usage', '{}', meta={'jev_requests': -9})
        with db.db:
            db.db.execute("INSERT INTO artifacts(kind,meta) VALUES('extract_llm','[1]')")
            db.db.execute("INSERT INTO fetch_jobs(kind,project_id,message_id,payload) "
                          "VALUES(?,0,0,'[]')", (semantic_drain.SCHED_KIND,))
        before = db.db.total_changes
        snap = observe(str(tmp_path / 'ledger.db'), {'semantic': []})
        assert snap['jev_requests_today'] is None
        assert snap['jev_usage_error'] == 'semantic_usage_invalid'
        assert snap['scheduler']['arrival_selected'] is None
        assert snap['jev_daily_budget'] == 0
        report = semantic.status_report(db)
        assert report['jev_requests_today'] is None
        assert report['jev_usage_error'] == 'semantic_usage_invalid'
        assert db.db.total_changes == before
    finally:
        db.close()
