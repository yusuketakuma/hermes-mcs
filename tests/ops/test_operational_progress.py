"""Progress and conditional estimates use only synthetic generation-bound evidence."""
import json

import pytest

from ledger import Ledger
import operational_progress as progress
from semantic_runtime import config_generation
from semantic_testkit import _cfg, _message, _patient

NOW = 2_000_000_000.0


@pytest.fixture
def led(tmp_path):
    ledger = Ledger(str(tmp_path / "synthetic.db"))
    try:
        yield ledger
    finally:
        ledger.close()


def job(led, mid, *, state="done", generation=None, eligible=False, kind="semantic",
        pid=1, next_try=0):
    generation = generation or f"synthetic-generation-{mid}"
    jid = led.job_add(kind, pid, mid, payload={"generation": generation, "eligible": eligible})
    led.db.execute("UPDATE fetch_jobs SET state=?,created_at=?,updated_at=?,next_try=? "
                   "WHERE job_id=?", (state, NOW - 1000, NOW - 10, next_try, jid))
    return {"job_id": jid, "generation": generation, "status": "done", "kind": kind,
            "project_id": pid, "cohort": "qc" if kind == "extract_qc" else
            "arrival" if eligible else "backfill", "completed_at": NOW - 10}


def record(led, metrics, *, cfg=None, observation=None):
    obs = {"version": 1, "started_at": NOW - 600, "finished_at": NOW - 1,
           "complete": True, "config_generation": config_generation(cfg or _cfg())}
    if observation is not None:
        obs.update(observation)
    led.db.execute("INSERT INTO artifacts(kind,content,meta,created_at) VALUES(?,?,?,?)",
                   ("semantic_drain_run", json.dumps({"observation": obs, "job_metrics": metrics}),
                    "{}", NOW - 1))


def snapshot(led, cfg=None):
    return progress.pipeline_progress(led.db, cfg or _cfg(), now=NOW)


def test_measured_rate_uses_distinct_current_jobs_and_wall_clock(led):
    metrics = [job(led, mid) for mid in range(1, 4)]
    job(led, 4, state="pending")
    job(led, 5, state="pending")
    record(led, metrics + [metrics[0], {**metrics[0], "status": "retry"}])
    record(led, metrics)
    slot = snapshot(led)["queues"]["backfill"]
    assert slot["pending"] == 2 and slot["due_by_schedule"] == 2
    assert slot["throughput"]["completed"] == 3
    assert slot["throughput"]["per_hour"] == 18
    assert slot["throughput"]["window_s"] == 600
    assert slot["estimate"]["status"] == "conditional"
    assert slot["estimate"]["seconds"] == 400


def test_valid_window_crossing_cutoff_does_not_invalidate_recent_samples(led):
    metrics = [job(led, mid) for mid in range(1, 4)]
    record(led, [], observation={"started_at": NOW - 3700, "finished_at": NOW - 3590})
    led.db.execute("UPDATE artifacts SET created_at=?", (NOW - 3590,))
    record(led, metrics)
    slot = snapshot(led)["queues"]["backfill"]
    assert slot["throughput"]["per_hour"] == 18
    assert slot["estimate"]["status"] == "conditional"


def test_config_return_does_not_join_separate_measurement_periods(led):
    metrics = [job(led, mid) for mid in range(1, 4)]
    record(led, [{**m, "completed_at": NOW - 500} for m in metrics],
           observation={"finished_at": NOW - 450})
    record(led, [], observation={"config_generation": "different-config",
                                "started_at": NOW - 400, "finished_at": NOW - 200})
    record(led, metrics, observation={"started_at": NOW - 100})
    slot = snapshot(led)["queues"]["backfill"]
    assert slot["throughput"]["window_s"] == 100
    assert slot["estimate"]["seconds"] is None
    assert slot["estimate"]["reason"] == "insufficient_samples"


def test_replaced_generations_and_other_scopes_never_inflate_speed(led):
    metrics = [job(led, 1), job(led, 2, pid=2), job(led, 3, kind="extract_qc")]
    led.db.execute("UPDATE fetch_jobs SET payload=? WHERE job_id=?",
                   (json.dumps({"generation": "new-generation"}), metrics[0]["job_id"]))
    cfg = _cfg(project_ids=[1])
    record(led, metrics, cfg=cfg)
    queues = snapshot(led, cfg)["queues"]
    assert queues["backfill"]["throughput"]["completed"] == 0
    assert queues["qc"]["throughput"]["completed"] == 1
    assert queues["backfill"]["throughput"]["excluded_obsolete_or_out_of_scope"] == 2


@pytest.mark.parametrize("obs", [
    {"complete": False}, {"complete": 1}, {"started_at": NOW + 1},
    {"finished_at": NOW + 1}, {"started_at": float("nan")},
    {"started_at": NOW - 1, "finished_at": NOW - 100},
])
def test_incomplete_or_invalid_window_never_produces_a_rate(led, obs):
    record(led, [job(led, 1)], observation=obs)
    rate = snapshot(led)["queues"]["backfill"]["throughput"]
    assert rate["status"] == "unavailable" and rate["per_hour"] is None


def test_old_metrics_do_not_fabricate_zero_throughput(led):
    led.db.execute("INSERT INTO artifacts(kind,content,created_at) VALUES(?,?,?)",
                   ("semantic_drain_run", '{"done":100}', NOW - 1))
    rate = snapshot(led)["queues"]["backfill"]["throughput"]
    assert rate["reason"] == "incomplete_or_legacy_metrics"
    assert rate["completed"] is None


@pytest.mark.parametrize("obs,reason", [
    ({"config_generation": "different"}, "no_matching_generation"),
    ({"started_at": NOW - 1000, "finished_at": NOW - 700}, "stale_metrics"),
])
def test_different_config_and_stale_samples_are_unavailable(led, obs, reason):
    record(led, [], observation=obs)
    assert snapshot(led)["queues"]["backfill"]["throughput"]["reason"] == reason


def test_delayed_failed_and_unscheduled_jobs_have_no_finish_estimate(led):
    record(led, [job(led, mid) for mid in range(1, 4)])
    job(led, 4, state="pending", next_try=NOW + 100)
    job(led, 5, state="pending", next_try=None)
    job(led, 6, state="failed")
    slot = snapshot(led)["queues"]["backfill"]
    assert (slot["pending"], slot["retry_wait"], slot["schedule_unknown"], slot["failed"]) == (2, 1, 1, 1)
    assert slot["estimate"]["reason"] == "queue_requires_attention"


def test_new_arrivals_and_too_few_samples_do_not_have_eta(led):
    record(led, [job(led, 1), job(led, 2, eligible=True)])
    queues = snapshot(led)["queues"]
    assert queues["arrival"]["estimate"]["reason"] == "new_arrivals_not_forecast"
    assert queues["backfill"]["estimate"]["reason"] == "insufficient_samples"


def test_invalid_payload_is_counted_without_exporting_any_source(led):
    metric = job(led, 1, state="pending")
    secret = "synthetic-private-content"
    led.db.execute("UPDATE fetch_jobs SET payload=? WHERE job_id=?", (secret, metric["job_id"]))
    out = snapshot(led)
    assert out["queues"]["unclassified"]["pending"] == 1
    assert secret not in json.dumps(out)
    assert "job_id" not in json.dumps(out) and "project_id" not in json.dumps(out)


@pytest.mark.parametrize("cfg,reason", [(None, "config_not_supplied"),
                                        ({"semantic": {"mode": "invalid"}}, "config_invalid")])
def test_unknown_scope_is_not_an_empty_queue(led, cfg, reason):
    out = progress.pipeline_progress(led.db, cfg, now=NOW)
    assert out["reason"] == reason and "queues" not in out


def test_disabled_processing_has_no_eta_even_with_recorded_completions(led):
    cfg = _cfg("off")
    record(led, [job(led, mid) for mid in range(1, 4)], cfg=cfg)
    assert snapshot(led, cfg)["queues"]["backfill"]["estimate"]["reason"] == "processing_disabled"


def room(led, pid, *, coverage=NOW - 1000, state="pending", updated=NOW - 10,
         due=0, archived=False):
    _patient(led, pid)
    led.save_messages([_message(pid, pid=pid)])
    led.db.execute("UPDATE patients SET coverage_ts=?,is_archived=? WHERE project_id=?",
                   (coverage, archived, pid))
    led.db.execute("UPDATE messages SET posted_at_ts=? WHERE project_id=?", (NOW - 100, pid))
    if state is not None:
        jid = led.job_add("history_head", pid)
        led.db.execute("UPDATE fetch_jobs SET state=?,updated_at=?,next_try=? WHERE job_id=?",
                       (state, updated, due, jid))


def test_collection_distinguishes_failure_deferral_checkpoint_and_missing_evidence(led):
    room(led, 1, state="failed")
    room(led, 2, updated=NOW - 90000)
    room(led, 3, due=NOW + 100)
    room(led, 4)
    room(led, 5, state=None)
    room(led, 6, coverage=None)
    room(led, 7, coverage=NOW)
    room(led, 8, archived=True)
    out = progress.collection_progress(led.db, now=NOW)
    assert out["rooms"] == 7 and out["lagging"] == 5
    assert out["max_lag_s"] == 900
    assert out["by_reason"] == {
        "history_failed": 1, "history_not_advancing": 1, "history_retry_wait": 1,
        "history_recent_checkpoint": 1, "history_job_missing": 1,
        "coverage_unobserved": 1, "no_recorded_gap": 1}
    assert "project_id" not in json.dumps(out)


def test_observation_helpers_do_not_write_or_hide_cancellation(led):
    room(led, 1)
    before = led.db.total_changes
    progress.collection_progress(led.db, now=NOW)
    snapshot(led)
    assert led.db.total_changes == before

    def cancelled():
        raise TimeoutError("synthetic cancellation")

    with pytest.raises(TimeoutError):
        progress.collection_progress(led.db, now=NOW, checkpoint=cancelled)


def test_retained_jobs_do_not_exhaust_the_whole_snapshot(led, tmp_path, monkeypatch):
    import semantic_observe

    with led.db:
        led.db.executemany(
            "INSERT INTO fetch_jobs(kind,project_id,message_id,payload,state,created_at,next_try) "
            "VALUES('semantic',1,?,'{}','done',?,0)",
            ((mid, NOW - 7200) for mid in range(1, 101001)))
        record(led, [job(led, mid) for mid in range(200001, 200004)])
        job(led, 200004, state="pending")
        job(led, 200005, state="failed")
    monkeypatch.setattr(semantic_observe.time, "time", lambda: NOW)
    out = semantic_observe.observe(str(tmp_path / "synthetic.db"), _cfg())
    assert out.get("observation", {}).get("status") == "complete", out
    slot = out["pipeline_progress"]["queues"]["backfill"]
    assert (slot["done"], slot["pending"], slot["failed"]) == (101003, 1, 1)
    assert slot["throughput"]["completed"] == 3 and slot["throughput"]["per_hour"] == 18
    assert slot["estimate"]["reason"] == "queue_requires_attention"


@pytest.mark.parametrize("raw,cohort", [
    ('{"eligible":true}', "arrival"), ('{"eligible":1}', "arrival"),
    ('{"eligible":1.0}', "backfill"), ('{"eligible":"1"}', "backfill"),
    ('{"eligible":true,"eligible":false}', "backfill"),
    ('{"eligible":false,"eligible":true}', "arrival"),
    ('{"eligible":true,"other":NaN}', "arrival"),
    ('{"eligible":NaN}', "backfill"), ('{"eligible":Infinity}', "backfill"),
    ('{"eligible":true,"other":' + '9' * 5000 + '}', "unclassified"),
    ('[]', "unclassified"), ('null', "unclassified"), ('broken', "unclassified"),
    (b'{"eligible":true}', "arrival"), (None, "unclassified"),
], ids=["true", "integer", "float", "string", "duplicate-false", "duplicate-true",
        "unrelated-nan", "nan", "infinity", "oversized-integer", "array", "null",
        "malformed", "bytes", "none"])
def test_sql_grouping_keeps_python_payload_semantics(led, raw, cohort):
    metric = job(led, 1, state="pending")
    led.db.execute("UPDATE fetch_jobs SET payload=? WHERE job_id=?", (raw, metric["job_id"]))
    queues = snapshot(led)["queues"]
    assert queues[cohort]["pending"] == 1
    assert sum(slot["pending"] for slot in queues.values()) == 1


def test_deep_malformed_payload_remains_unclassified(led):
    # The missing object terminator is invalid independently of the Python
    # version's JSON nesting limit.
    raw = '{"eligible":true,"nested":' + '[' * 10000 + '0' + ']' * 10000
    metric = job(led, 1, state="pending")
    led.db.execute("UPDATE fetch_jobs SET payload=? WHERE job_id=?", (raw, metric["job_id"]))
    queues = snapshot(led)["queues"]
    assert queues["unclassified"]["pending"] == 1
    assert sum(slot["pending"] for slot in queues.values()) == 1


@pytest.mark.parametrize("due,created,expected", [
    (None, None, (0, 0, 1, None)),
    ("not-a-number", NOW + 1, (0, 0, 1, None)),
    (float("nan"), float("nan"), (0, 0, 1, None)),
    (float("inf"), float("inf"), (0, 0, 1, None)),
    (-1, -1, (0, 0, 1, None)),
    (NOW + 1, NOW - 10, (0, 1, 0, 10)),
    (0, NOW, (1, 0, 0, 0)),
])
def test_grouped_schedule_times_preserve_finite_number_boundary(led, due, created, expected):
    metric = job(led, 1, state="pending", next_try=due)
    led.db.execute("UPDATE fetch_jobs SET created_at=? WHERE job_id=?", (created, metric["job_id"]))
    slot = snapshot(led)["queues"]["backfill"]
    assert tuple(slot[k] for k in ("due_by_schedule", "retry_wait", "schedule_unknown",
                                  "oldest_age_s")) == expected


def test_grouped_unknown_states_and_project_scope(led):
    job(led, 1, state="unknown-state", pid=1)
    job(led, 2, state="pending", pid=2)
    job(led, 3, state=None, pid=1, kind="extract_qc")
    queues = snapshot(led, _cfg(project_ids=[1]))["queues"]
    assert queues["backfill"]["unknown"] == queues["qc"]["unknown"] == 1
    assert sum(s["pending"] for s in queues.values()) == 0
    assert sum(s["unknown"] for s in snapshot(led, _cfg(project_ids=[]))["queues"].values()) == 0


def test_udf_charges_payload_text_and_restores_callback_exception(led):
    job(led, 1)
    seen = []

    def limited(raw):
        seen.append(raw)
        raise TimeoutError("synthetic shared text budget")

    with pytest.raises(TimeoutError, match="synthetic shared text budget"):
        progress.pipeline_progress(led.db, _cfg(), now=NOW, payload_checkpoint=limited)
    assert len(seen) == 1
    assert snapshot(led)["queues"]["backfill"]["done"] == 1


@pytest.mark.parametrize("with_nul", [False, True])
def test_single_oversized_payload_remains_unavailable(led, with_nul):
    metric = job(led, 1)
    raw = ("\0" if with_nul else "") + "x" * (progress._MAX_JOB_PAYLOAD + 1)
    led.db.execute("UPDATE fetch_jobs SET payload=? WHERE job_id=?", (raw, metric["job_id"]))
    assert snapshot(led)["reason"] == "observation_size_exceeded"


def test_grouping_keeps_local_cumulative_text_bound(led, monkeypatch):
    for mid in range(1, 4):
        job(led, mid)
    monkeypatch.setattr(progress, "_MAX_JOB_TEXT", 100)
    assert snapshot(led)["reason"] == "observation_size_exceeded"


def test_shared_snapshot_text_budget_also_counts_udf_payloads(led, tmp_path, monkeypatch):
    import semantic_observe

    with led.db:
        for mid in range(1, 33):
            metric = job(led, mid)
            led.db.execute("UPDATE fetch_jobs SET payload=? WHERE job_id=?",
                           (json.dumps({"unused": "x" * 600}), metric["job_id"]))
    monkeypatch.setattr(semantic_observe, "OBSERVE_MAX_TEXT", 8192)
    out = semantic_observe.observe(str(tmp_path / "synthetic.db"), _cfg())
    assert out["status"] == "unavailable" and out["reason"] == "observation_size_exceeded"


def test_unhashable_legacy_generation_cannot_verify_completion(led):
    metric = job(led, 1)
    led.db.execute("UPDATE fetch_jobs SET payload=? WHERE job_id=?",
                   ('{"eligible":NaN}', metric["job_id"]))
    record(led, [metric, {**metric, "job_id": 10**400}])
    rate = snapshot(led)["queues"]["backfill"]["throughput"]
    assert rate["completed"] == 0 and rate["excluded_obsolete_or_out_of_scope"] == 2

